"""Formal inter-rater reliability and group-comparison statistics for
aggregated qualitative evaluations - Cohen's kappa (2 raters), Fleiss'
kappa (3+ raters), the intraclass correlation coefficient (ICC), and a
Wilcoxon/Mann-Whitney rank-based AUC between two groups of scores, each
with a bootstrap 95% confidence interval.

Deliberately built on nothing beyond numpy/scipy (already hard
dependencies - see requirements/base.txt): this app is packaged as a
standalone PyInstaller build (see packaging/qicom.spec), so a new
dependency (scikit-learn for cohen_kappa_score, statsmodels for
fleiss_kappa, pingouin for ICC) is a real, ongoing build-size/maintenance
cost, not a one-line convenience - the formulas below are standard,
well-documented, and small enough to implement and test directly instead.

Every function here works on a *complete-case* ratings matrix (no missing
values) - the caller is responsible for filtering to subjects every rater
actually scored (see qicom.session.evaluation_aggregation, which is the
only caller). All confidence intervals use the percentile bootstrap
(resampling subjects, not raters - raters are treated as the fixed,
complete panel being evaluated, subjects as the random sample from a
larger population of images/slices) rather than each statistic's own
asymptotic standard-error formula: one shared, directly-verifiable
implementation is a smaller risk of a silent formula bug than four
separate hand-derived large-sample variance formulas.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np
from scipy import stats as scipy_stats

ORDINAL_ALPHA_FORMULA_VERSION = 2
"""Version 2 replaced the uniform squared rank distance with Krippendorff's
ordinal difference function (which depends on the cumulative marginal category
frequencies) and weights each unit's observed disagreement by 1/(m_u - 1).
Version 1 results are not numerically comparable; the label is exported so a
stored result states which definition produced it."""

_ORDINAL_ALPHA_METHOD = f"Krippendorff alpha (ordinal) v{ORDINAL_ALPHA_FORMULA_VERSION}"

GWET_AC2_FORMULA_VERSION = 2
"""Version 2 uses Gwet's mean unit marginals (including singleton units) and
weighted chance agreement, as in the author's irrCAC gwet.ac1.raw implementation.
The original v1 chance calculation and pooled marginals were incorrect."""

_GWET_AC2_METHOD = f"Gwet AC2 (quadratic ordinal weights) v{GWET_AC2_FORMULA_VERSION}"

_DEFAULT_BOOTSTRAP_ITERATIONS = 1000
_DEFAULT_SEED = 0
_BOOTSTRAP_INDEX_BUDGET_BYTES = 8 * 1024 * 1024
MIN_SUBJECTS_FOR_RELIABILITY = 3
"""Below this many complete-case subjects, kappa/Fleiss'/ICC are too
unstable to report meaningfully - callers skip (report NaN) rather than
show a number computed from 1-2 points."""


@dataclass(frozen=True)
class StatResult:
    """A point estimate plus its percentile-bootstrap 95% CI - `None` for
    every field means the statistic could not be computed at all (too few
    subjects/raters, zero variance in a place the formula divides by)."""

    value: float | None
    ci_low: float | None
    ci_high: float | None
    n_subjects: int


@dataclass(frozen=True)
class AgreementResult:
    """Agreement estimate with transparent usable/missing counts and method."""

    value: float | None
    ci_low: float | None
    ci_high: float | None
    n_units: int
    n_raters: int
    n_ratings: int
    n_missing: int
    method: str
    limitation: str | None = None


def _empty_result(n_subjects: int) -> StatResult:
    return StatResult(value=None, ci_low=None, ci_high=None, n_subjects=n_subjects)


def _bootstrap_ci(
    n_subjects: int,
    stat_fn: Callable[[np.ndarray], float],
    *,
    n_boot: int = _DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = _DEFAULT_SEED,
) -> tuple[float | None, float | None]:
    """Resamples subject *indices* with replacement `n_boot` times, calling
    `stat_fn(resampled_indices)` for each - percentile method (2.5th/97.5th
    percentile of the bootstrap distribution). Returns (None, None) if
    every resample came back NaN (e.g. a statistic that's undefined
    whenever a resample happens to have zero variance)."""
    rng = np.random.default_rng(seed)
    value_chunks: list[np.ndarray] = []
    for indices in _bootstrap_index_chunks(rng, n_subjects, n_boot):
        value_chunks.append(np.asarray([stat_fn(row) for row in indices], dtype=float))
    values = np.concatenate(value_chunks)
    return _percentile_ci(values)


def _bootstrap_index_chunks(
    rng: np.random.Generator,
    n_subjects: int,
    n_boot: int = _DEFAULT_BOOTSTRAP_ITERATIONS,
) -> Iterator[np.ndarray]:
    """Yield deterministic bootstrap-index batches with bounded peak RAM.

    Generating ``(batch, n_subjects)`` is sequence-equivalent to the old
    one-row-at-a-time loop for a single fixed integer range.  Keeping the
    batches below a small explicit budget prevents a large qualitative
    project from turning the bootstrap into an unbounded index allocation.
    """
    bytes_per_row = max(1, n_subjects * np.dtype(np.int64).itemsize)
    batch_size = max(1, min(n_boot, _BOOTSTRAP_INDEX_BUDGET_BYTES // bytes_per_row))
    for start in range(0, n_boot, batch_size):
        count = min(batch_size, n_boot - start)
        yield rng.integers(0, n_subjects, size=(count, n_subjects), dtype=np.int64)


def _vectorized_bootstrap_ci(
    n_subjects: int,
    stat_batch_fn: Callable[[np.ndarray], np.ndarray],
    *,
    n_boot: int = _DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = _DEFAULT_SEED,
) -> tuple[float | None, float | None]:
    rng = np.random.default_rng(seed)
    values = np.concatenate(
        [stat_batch_fn(indices) for indices in _bootstrap_index_chunks(rng, n_subjects, n_boot)]
    )
    return _percentile_ci(values)


def _percentile_ci(values: np.ndarray) -> tuple[float | None, float | None]:
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    if values.size == 0:
        return None, None
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def cohens_kappa(ratings: np.ndarray, *, weighted: str = "quadratic") -> StatResult:
    """`ratings` is (n_subjects, 2) - exactly two raters' scores on the
    same n_subjects items, already restricted to complete cases (no NaN -
    see this module's own docstring). `weighted` is "quadratic" (the
    standard choice for ordinal/Likert data - a near-miss counts far less
    against agreement than a wildly different score) or "none" (every
    disagreement counted equally, the classic unweighted kappa).

    Formula (Cohen 1968): with O the observed n_categories x n_categories
    contingency table and E the table expected under independence (row
    marginal x column marginal / N), and w_ij a disagreement weight (0 on
    the diagonal, (i-j)^2/(K-1)^2 off it for "quadratic", 1 for "none"):

        kappa_w = 1 - sum(w * O) / sum(w * E)
    """
    ratings = np.asarray(ratings, dtype=float)
    n_subjects = ratings.shape[0]
    if n_subjects < MIN_SUBJECTS_FOR_RELIABILITY:
        return _empty_result(n_subjects)
    categories = np.unique(ratings)
    if categories.size < 2:
        return _empty_result(n_subjects)  # every rating identical - kappa is undefined (0/0)

    # Categories and disagreement weights are invariant across bootstrap
    # replicas.  Encode them once, then form every replica's contingency
    # table with one bincount instead of rebuilding dictionaries in Python.
    codes = np.searchsorted(categories, ratings).astype(np.int64, copy=False)
    k = categories.size
    pair_codes = codes[:, 0] * k + codes[:, 1]
    weights = _kappa_weights(k, weighted)

    def _kappa_batch(indices: np.ndarray) -> np.ndarray:
        return _cohen_kappa_values(pair_codes, indices, k, weights)

    value = float(_kappa_batch(np.arange(n_subjects, dtype=np.int64)[None, :])[0])
    ci_low, ci_high = _vectorized_bootstrap_ci(n_subjects, _kappa_batch)
    return StatResult(value=value, ci_low=ci_low, ci_high=ci_high, n_subjects=n_subjects)


def _kappa_weights(k: int, weighted: str) -> np.ndarray:
    i_idx, j_idx = np.meshgrid(np.arange(k), np.arange(k), indexing="ij")
    if weighted == "quadratic" and k > 1:
        return (i_idx - j_idx) ** 2 / (k - 1) ** 2
    return (i_idx != j_idx).astype(float)


def _cohen_kappa_values(
    pair_codes: np.ndarray,
    indices: np.ndarray,
    k: int,
    weights: np.ndarray,
) -> np.ndarray:
    batch_size, n_subjects = indices.shape
    offsets = np.arange(batch_size, dtype=np.int64)[:, None] * (k * k)
    observed = np.bincount(
        (pair_codes[indices] + offsets).ravel(), minlength=batch_size * k * k
    ).reshape(batch_size, k, k)
    row_marginal = observed.sum(axis=2)
    col_marginal = observed.sum(axis=1)
    expected = row_marginal[:, :, None] * col_marginal[:, None, :] / n_subjects
    numerator = (observed * weights).sum(axis=(1, 2))
    denominator = (expected * weights).sum(axis=(1, 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        return 1.0 - numerator / denominator


def fleiss_kappa(ratings: np.ndarray) -> StatResult:
    """`ratings` is (n_subjects, n_raters), n_raters >= 3, complete cases
    only (see this module's own docstring) - every rater must score every
    subject from the same fixed panel (Fleiss' kappa, unlike Cohen's,
    does not require the *same* raters to be identifiable across
    subjects, but this codebase's own evaluator-per-page model always has
    a fixed, identifiable panel, so the simpler complete-case form
    applies directly).

    Formula (Fleiss 1971): n_ij = count of raters assigning subject i to
    category j; p_j = overall proportion of all ratings in category j;
    P_i = per-subject agreement extent; kappa = (mean(P_i) - sum(p_j^2))
    / (1 - sum(p_j^2)).
    """
    ratings = np.asarray(ratings, dtype=float)
    n_subjects, n_raters = ratings.shape
    if n_subjects < MIN_SUBJECTS_FOR_RELIABILITY or n_raters < 3:
        return _empty_result(n_subjects)
    categories = np.unique(ratings)
    if categories.size < 2:
        return _empty_result(n_subjects)

    codes = np.searchsorted(categories, ratings).astype(np.int64, copy=False)
    counts = np.zeros((n_subjects, categories.size), dtype=np.int64)
    np.add.at(counts, (np.repeat(np.arange(n_subjects), n_raters), codes.ravel()), 1)
    per_subject = (np.square(counts).sum(axis=1) - n_raters) / (n_raters * (n_raters - 1))

    def _kappa_batch(indices: np.ndarray) -> np.ndarray:
        selected_counts = counts[indices]
        p_j = selected_counts.sum(axis=1) / (n_subjects * n_raters)
        p_bar_e = np.square(p_j).sum(axis=1)
        p_bar = per_subject[indices].mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            values = (p_bar - p_bar_e) / (1.0 - p_bar_e)
        values[p_bar_e >= 1.0] = np.nan
        return values

    value = float(_kappa_batch(np.arange(n_subjects, dtype=np.int64)[None, :])[0])
    ci_low, ci_high = _vectorized_bootstrap_ci(n_subjects, _kappa_batch)
    return StatResult(value=value, ci_low=ci_low, ci_high=ci_high, n_subjects=n_subjects)


def icc_2_1(ratings: np.ndarray) -> StatResult:
    """`ratings` is (n_subjects, n_raters), complete cases only (see this
    module's own docstring) - ICC(2,1) in the Shrout & Fleiss (1979) /
    McGraw & Wong (1996) "ICC(A,1)" naming: two-way random effects
    (raters treated as a random sample of a larger population of
    possible evaluators - the standard, most commonly recommended choice
    for a genuine inter-rater reliability study, per Koo & Li 2016),
    absolute agreement (not just consistent ranking - two raters who
    always disagree by a fixed offset still count against this form),
    single-rater reliability (as opposed to the reliability of the
    *panel's mean* - "single rater" is what a single new evaluator's
    score can be trusted to reproduce, the more conservative and more
    directly interpretable of the two for this app's per-evaluator
    scoring).
    """
    ratings = np.asarray(ratings, dtype=float)
    n_subjects, n_raters = ratings.shape
    if n_subjects < MIN_SUBJECTS_FOR_RELIABILITY or n_raters < 2:
        return _empty_result(n_subjects)

    def _icc_batch(indices: np.ndarray) -> np.ndarray:
        return _icc_2_1_values(ratings[indices])

    value = float(_icc_batch(np.arange(n_subjects, dtype=np.int64)[None, :])[0])
    ci_low, ci_high = _vectorized_bootstrap_ci(n_subjects, _icc_batch)
    return StatResult(value=value, ci_low=ci_low, ci_high=ci_high, n_subjects=n_subjects)


def _icc_2_1_values(ratings: np.ndarray) -> np.ndarray:
    """Batch form of ICC(2,1); first axis identifies bootstrap replicas."""
    _batch, n, k = ratings.shape
    grand_mean = ratings.mean(axis=(1, 2))
    subject_means = ratings.mean(axis=2)
    rater_means = ratings.mean(axis=1)
    ss_rows = k * np.square(subject_means - grand_mean[:, None]).sum(axis=1)
    ss_cols = n * np.square(rater_means - grand_mean[:, None]).sum(axis=1)
    residual = (
        ratings - subject_means[:, :, None] - rater_means[:, None, :] + grand_mean[:, None, None]
    )
    ss_error = np.square(residual).sum(axis=(1, 2))
    ms_rows = ss_rows / (n - 1)
    ms_cols = ss_cols / (k - 1)
    ms_error = ss_error / ((n - 1) * (k - 1))
    denominator = ms_rows + (k - 1) * ms_error + k * (ms_cols - ms_error) / n
    with np.errstate(divide="ignore", invalid="ignore"):
        return (ms_rows - ms_error) / denominator


def wilcoxon_auc(sample_a: np.ndarray, sample_b: np.ndarray) -> StatResult:
    """Mann-Whitney/Wilcoxon rank-sum AUC ("probability of superiority")
    between two *independent* groups of scores - P(a randomly picked
    value from sample_a exceeds one from sample_b), 0.5 for ties, so 0.5
    means the two groups are indistinguishable and 1.0 means every value
    in sample_a exceeds every value in sample_b. AUC = U / (n_a * n_b),
    the standard identity between the Mann-Whitney U statistic and the
    ROC AUC of a perfect rank-based classifier (used here to compare, per
    question, two comparison images' pooled score distributions across
    every evaluator/slice - the qualitative-evaluation analogue of the
    quantitative side's own "R vs C1" pairwise comparisons).

    `n_subjects` on the returned StatResult is min(len(sample_a),
    len(sample_b)) - the bootstrap resamples each group independently at
    its own size, so this is reported only as a rough "how much data was
    this built on" indicator, not literally either sample's size.
    """
    sample_a = np.asarray(sample_a, dtype=float)
    sample_b = np.asarray(sample_b, dtype=float)
    n_a, n_b = sample_a.size, sample_b.size
    n_subjects = min(n_a, n_b)
    if n_a < MIN_SUBJECTS_FOR_RELIABILITY or n_b < MIN_SUBJECTS_FOR_RELIABILITY:
        return _empty_result(n_subjects)

    value = _mann_whitney_auc_value(sample_a, sample_b)

    categories = np.unique(np.concatenate((sample_a, sample_b)))
    codes_a = np.searchsorted(categories, sample_a).astype(np.int64, copy=False)
    codes_b = np.searchsorted(categories, sample_b).astype(np.int64, copy=False)
    comparison = (categories[:, None] > categories[None, :]).astype(float)
    comparison += 0.5 * (categories[:, None] == categories[None, :])

    rng = np.random.default_rng(_DEFAULT_SEED)
    boot_chunks: list[np.ndarray] = []
    bytes_per_row = max(1, (n_a + n_b) * np.dtype(np.int64).itemsize)
    batch_size = max(
        1, min(_DEFAULT_BOOTSTRAP_ITERATIONS, _BOOTSTRAP_INDEX_BUDGET_BYTES // bytes_per_row)
    )
    for start in range(0, _DEFAULT_BOOTSTRAP_ITERATIONS, batch_size):
        count = min(batch_size, _DEFAULT_BOOTSTRAP_ITERATIONS - start)
        # Bounds differ between the groups, so keep the historical A/B/A/B
        # RNG-call order while batching the expensive rank/count arithmetic.
        indices_a = np.empty((count, n_a), dtype=np.int64)
        indices_b = np.empty((count, n_b), dtype=np.int64)
        for row in range(count):
            indices_a[row] = rng.integers(0, n_a, size=n_a)
            indices_b[row] = rng.integers(0, n_b, size=n_b)
        counts_a = _row_category_counts(codes_a[indices_a], categories.size)
        counts_b = _row_category_counts(codes_b[indices_b], categories.size)
        # This is U/(n_a*n_b): each strict win contributes one and each
        # tied pair one half, exactly the midrank convention used by SciPy.
        boot_chunks.append(
            np.einsum("bi,ij,bj->b", counts_a, comparison, counts_b, optimize=True) / (n_a * n_b)
        )
    ci_low, ci_high = _percentile_ci(np.concatenate(boot_chunks))
    return StatResult(value=value, ci_low=ci_low, ci_high=ci_high, n_subjects=n_subjects)


def _mann_whitney_auc_value(sample_a: np.ndarray, sample_b: np.ndarray) -> float:
    if np.unique(sample_a).size < 1 or np.unique(sample_b).size < 1:
        return float("nan")
    try:
        u_statistic, _p_value = scipy_stats.mannwhitneyu(
            sample_a, sample_b, alternative="two-sided"
        )
    except ValueError:
        # scipy raises when every value in both samples is identical (no
        # ranks to compare) - indistinguishable groups, AUC = 0.5.
        return 0.5
    return float(u_statistic) / (sample_a.size * sample_b.size)


def _row_category_counts(codes: np.ndarray, n_categories: int) -> np.ndarray:
    rows = codes.shape[0]
    offsets = np.arange(rows, dtype=np.int64)[:, None] * n_categories
    return np.bincount((codes + offsets).ravel(), minlength=rows * n_categories).reshape(
        rows, n_categories
    )


def wilcoxon_auc_p_value(sample_a: np.ndarray, sample_b: np.ndarray) -> float | None:
    """The Mann-Whitney/Wilcoxon rank-sum test's own two-sided p-value for
    "these two groups' distributions are the same" - kept as a separate
    call (not bundled into wilcoxon_auc's StatResult) since a p-value
    isn't a bootstrap-CI-shaped quantity the way the other statistics in
    this module are."""
    sample_a = np.asarray(sample_a, dtype=float)
    sample_b = np.asarray(sample_b, dtype=float)
    if sample_a.size < MIN_SUBJECTS_FOR_RELIABILITY or sample_b.size < MIN_SUBJECTS_FOR_RELIABILITY:
        return None
    try:
        _u_statistic, p_value = scipy_stats.mannwhitneyu(
            sample_a, sample_b, alternative="two-sided"
        )
    except ValueError:
        return None
    return float(p_value)


def paired_probability_of_superiority(sample_a: np.ndarray, sample_b: np.ndarray) -> StatResult:
    """Paired common-language effect size with an evaluator bootstrap.

    ``sample_a`` and ``sample_b`` must contain one matched aggregate per
    evaluator, in identical evaluator order.  The value is the fraction of
    evaluators for whom A is greater than B, with ties worth one half.  The
    confidence interval resamples evaluator pairs together, preserving the
    repeated-measures design.
    """
    sample_a = np.asarray(sample_a, dtype=float)
    sample_b = np.asarray(sample_b, dtype=float)
    if sample_a.shape != sample_b.shape:
        raise ValueError("Paired samples must have the same shape.")
    n_subjects = sample_a.size
    if n_subjects < MIN_SUBJECTS_FOR_RELIABILITY:
        return _empty_result(n_subjects)

    differences = sample_a - sample_b

    def _effect(indices: np.ndarray) -> float:
        selected = differences[indices]
        return float(np.mean((selected > 0).astype(float) + 0.5 * (selected == 0)))

    value = _effect(np.arange(n_subjects))
    ci_low, ci_high = _bootstrap_ci(n_subjects, _effect)
    return StatResult(value=value, ci_low=ci_low, ci_high=ci_high, n_subjects=n_subjects)


def paired_wilcoxon_p_value(sample_a: np.ndarray, sample_b: np.ndarray) -> float | None:
    """Two-sided Wilcoxon signed-rank p-value for matched evaluator means."""
    sample_a = np.asarray(sample_a, dtype=float)
    sample_b = np.asarray(sample_b, dtype=float)
    if sample_a.shape != sample_b.shape:
        raise ValueError("Paired samples must have the same shape.")
    if sample_a.size < MIN_SUBJECTS_FOR_RELIABILITY:
        return None
    differences = sample_a - sample_b
    if np.all(differences == 0):
        return 1.0
    try:
        result = scipy_stats.wilcoxon(sample_a, sample_b, alternative="two-sided")
    except ValueError:
        return None
    return float(result.pvalue)


def krippendorff_alpha_ordinal(ratings: np.ndarray) -> AgreementResult:
    """Krippendorff's alpha for ordinal ratings with missing values.

    Rows are independent units and columns are identifiable raters. Units
    with fewer than two observed ratings cannot contribute to observed
    disagreement and are reported through the effective counts instead of
    being silently treated as agreement.
    """

    matrix = np.asarray(ratings, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("ratings must be a two-dimensional matrix.")
    n_units, n_raters = matrix.shape
    n_missing = int(np.isnan(matrix).sum())
    usable = matrix[np.sum(~np.isnan(matrix), axis=1) >= 2]
    n_effective = usable.shape[0]
    observed_values = matrix[~np.isnan(matrix)]
    if n_effective < 2 or observed_values.size < 3:
        return AgreementResult(
            None,
            None,
            None,
            n_effective,
            n_raters,
            int(observed_values.size),
            n_missing,
            _ORDINAL_ALPHA_METHOD,
            "At least two units with paired ratings and three ratings are required.",
        )
    categories = np.unique(observed_values)
    if categories.size < 2:
        return AgreementResult(
            None,
            None,
            None,
            n_effective,
            n_raters,
            int(observed_values.size),
            n_missing,
            _ORDINAL_ALPHA_METHOD,
            "All observed ratings are identical; expected disagreement is zero.",
        )
    usable_values = usable[~np.isnan(usable)]
    pooled_counts = np.bincount(
        np.searchsorted(categories, usable_values), minlength=categories.size
    ).astype(float)
    # The ordinal metric is a property of the scale as the raters actually used
    # it, so it is derived once from the full-sample marginals and held fixed
    # across bootstrap resamples rather than redefined by every resample.
    distances = _krippendorff_ordinal_disagreement(pooled_counts)
    unit_counts, unit_disagreement, _unit_pairs = _agreement_unit_components(
        usable, categories, distances
    )

    def _alpha_batch(indices: np.ndarray) -> np.ndarray:
        counts = unit_counts[indices]
        sizes = counts.sum(axis=2)
        pooled = counts.sum(axis=1)
        totals = pooled.sum(axis=1)
        # Krippendorff weights each unit's disagreement by 1/(m_u - 1) before
        # averaging over the n pairable values. Pooling the raw pair counts
        # instead over-weights the units that happen to have more raters, which
        # is exactly the case created by missing ratings.
        per_unit = unit_disagreement[indices] / (sizes - 1.0)
        observed = per_unit.sum(axis=1) / totals
        expected = np.einsum("bi,ij,bj->b", pooled, distances, pooled, optimize=True)
        expected /= totals * (totals - 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            return 1.0 - observed / expected

    value = float(_alpha_batch(np.arange(n_effective)[None, :])[0])
    ci_low, ci_high = _vectorized_bootstrap_ci(n_effective, _alpha_batch)
    return AgreementResult(
        None if np.isnan(value) else value,
        ci_low,
        ci_high,
        n_effective,
        n_raters,
        int(observed_values.size),
        n_missing,
        _ORDINAL_ALPHA_METHOD,
    )


def _krippendorff_ordinal_disagreement(counts: np.ndarray) -> np.ndarray:
    """Krippendorff's ordinal difference function.

    The ordinal metric is not a rank distance. How far apart two categories are
    depends on how heavily the categories between them were used::

        delta(c, k) = ( sum_{g=c..k} n_g - (n_c + n_k) / 2 ) ** 2

    with ``n_g`` the marginal frequency of category ``g`` over all pairable
    values. Two adjacent categories that both attracted many ratings are
    therefore further apart than two adjacent categories that were barely used,
    which a uniform rank distance cannot express. The metric is unnormalised on
    purpose: the same scale factor appears in observed and expected
    disagreement and cancels in alpha.
    """

    cumulative = np.cumsum(counts)
    # Inclusive mass between the two categories, for c <= k.
    spans = cumulative[None, :] - cumulative[:, None] + counts[:, None]
    endpoints = (counts[:, None] + counts[None, :]) / 2.0
    upper = np.triu(spans - endpoints)
    # The diagonal is zero by construction, so mirroring needs no correction.
    return np.square(upper + upper.T)


def _quadratic_rank_disagreement(categories: np.ndarray) -> np.ndarray:
    """Squared normalized rank distance, used for Gwet's AC2 weights."""

    ranks = np.arange(categories.size, dtype=float)
    scale = max(1.0, float(categories.size - 1))
    return np.square((ranks[:, None] - ranks[None, :]) / scale)


def _agreement_unit_components(
    matrix: np.ndarray, categories: np.ndarray, disagreement: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    counts = np.zeros((matrix.shape[0], categories.size), dtype=float)
    pairs = np.zeros(matrix.shape[0], dtype=float)
    for index, row in enumerate(matrix):
        values = row[~np.isnan(row)]
        codes = np.searchsorted(categories, values)
        counts[index] = np.bincount(codes, minlength=categories.size)
        pairs[index] = values.size * (values.size - 1)
    numerator = np.einsum("bi,ij,bj->b", counts, disagreement, counts, optimize=True)
    return counts, numerator, pairs


def gwet_ac2(ratings: np.ndarray) -> AgreementResult:
    """Gwet's AC2 with quadratic ordinal agreement weights.

    AC2 is offered as an advanced complement to ordinal alpha because it is
    less sensitive to prevalence-induced kappa paradoxes. Missing values are
    accepted and effective counts are always returned. Category probabilities
    are averaged equally across nonempty units, including single-rating units;
    observed agreement uses only units with at least two ratings. Bootstrap
    samples nonempty units and retains the original observed-category weights.

    Reference: K. L. Gwet, irrCAC, R/agree.coeff3.raw.r, gwet.ac1.raw.
    """

    matrix = np.asarray(ratings, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("ratings must be a two-dimensional matrix.")
    if np.isinf(matrix).any():
        raise ValueError("ratings must be finite or NaN.")
    n_units, n_raters = matrix.shape
    n_missing = int(np.isnan(matrix).sum())
    usable = matrix[np.sum(~np.isnan(matrix), axis=1) >= 2]
    observed_values = matrix[~np.isnan(matrix)]
    if usable.shape[0] < 2 or n_raters < 2 or np.unique(observed_values).size < 2:
        return AgreementResult(
            None,
            None,
            None,
            usable.shape[0],
            n_raters,
            int(observed_values.size),
            n_missing,
            _GWET_AC2_METHOD,
            "At least two usable units, two raters, and two categories are required.",
        )
    categories = np.unique(observed_values)
    disagreement = _quadratic_rank_disagreement(categories)
    agreement = 1.0 - disagreement
    nonempty = matrix[np.sum(~np.isnan(matrix), axis=1) >= 1]
    unit_counts, _unused_disagreement, unit_pairs = _agreement_unit_components(
        nonempty, categories, disagreement
    )
    agreement_numerator = np.einsum(
        "bi,ij,bj->b", unit_counts, agreement, unit_counts, optimize=True
    )
    # Remove self-pairs, whose agreement weight is one.
    agreement_numerator -= unit_counts.sum(axis=1)
    unit_agreement = np.divide(
        agreement_numerator,
        unit_pairs,
        out=np.zeros_like(agreement_numerator),
        where=unit_pairs > 0,
    )
    unit_proportions = unit_counts / unit_counts.sum(axis=1)[:, None]
    chance_scale = float(agreement.sum()) / (categories.size * (categories.size - 1))

    def _ac2_batch(indices: np.ndarray) -> np.ndarray:
        p = unit_proportions[indices].mean(axis=1)
        chance = chance_scale * np.sum(p * (1.0 - p), axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            observed = unit_agreement[indices].sum(axis=1) / (unit_pairs[indices] > 0).sum(axis=1)
            return (observed - chance) / (1.0 - chance)

    value = float(_ac2_batch(np.arange(nonempty.shape[0])[None, :])[0])
    ci_low, ci_high = _vectorized_bootstrap_ci(nonempty.shape[0], _ac2_batch)
    return AgreementResult(
        None if np.isnan(value) else value,
        ci_low,
        ci_high,
        usable.shape[0],
        n_raters,
        int(observed_values.size),
        n_missing,
        _GWET_AC2_METHOD,
    )


def kendalls_w(rankings: np.ndarray) -> StatResult:
    """Kendall's coefficient of concordance for complete rankings.

    ``rankings`` has one rater/evaluation instance per row and one ranked
    object per column. Values are ranks (ties are accepted); the returned
    ``n_subjects`` is the number of complete rankings.
    """

    matrix = np.asarray(rankings, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("rankings must be a two-dimensional matrix.")
    complete = matrix[~np.isnan(matrix).any(axis=1)]
    n_judges, n_items = complete.shape
    if n_judges < 2 or n_items < 2:
        return _empty_result(n_judges)

    def _w(selected: np.ndarray) -> float:
        chosen = complete[selected]
        rank_sums = chosen.sum(axis=0)
        s = float(np.square(rank_sums - rank_sums.mean()).sum())
        tie_correction = 0.0
        for row in chosen:
            _values, counts = np.unique(row, return_counts=True)
            tie_correction += float(np.sum(counts**3 - counts))
        denominator = chosen.shape[0] ** 2 * (n_items**3 - n_items)
        denominator -= chosen.shape[0] * tie_correction
        return float("nan") if denominator <= 0 else 12.0 * s / denominator

    value = _w(np.arange(n_judges))
    ci_low, ci_high = _bootstrap_ci(n_judges, _w)
    return StatResult(value, ci_low, ci_high, n_judges)


def holm_adjust(p_values: list[float | None]) -> list[float | None]:
    """Holm family-wise correction, preserving ``None`` positions."""

    valid = [(index, float(value)) for index, value in enumerate(p_values) if value is not None]
    ordered = sorted(valid, key=lambda item: item[1])
    adjusted: dict[int, float] = {}
    running = 0.0
    total = len(ordered)
    for rank, (index, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[index] = running
    return [None if value is None else adjusted[index] for index, value in enumerate(p_values)]


def clustered_paired_median_difference(
    sample_a: np.ndarray,
    sample_b: np.ndarray,
    evaluator_ids: np.ndarray,
    case_ids: np.ndarray,
    *,
    n_boot: int = _DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = _DEFAULT_SEED,
) -> StatResult:
    """Paired median difference with a two-way evaluator/case bootstrap.

    Evaluator and case clusters are independently resampled (pigeonhole
    bootstrap), preserving all selected cells and avoiding slice-level
    pseudo-replication.
    """

    a = np.asarray(sample_a, dtype=float)
    b = np.asarray(sample_b, dtype=float)
    evaluators = np.asarray(evaluator_ids)
    cases = np.asarray(case_ids)
    if not (a.shape == b.shape == evaluators.shape == cases.shape):
        raise ValueError("Paired scores and cluster identifiers must have the same shape.")
    valid = ~(np.isnan(a) | np.isnan(b))
    differences = a[valid] - b[valid]
    evaluators = evaluators[valid]
    cases = cases[valid]
    if differences.size < MIN_SUBJECTS_FOR_RELIABILITY:
        return _empty_result(int(differences.size))
    unique_evaluators = np.unique(evaluators)
    unique_cases = np.unique(cases)
    rng = np.random.default_rng(seed)
    estimates: list[float] = []
    for _ in range(n_boot):
        sampled_evaluators = rng.choice(unique_evaluators, unique_evaluators.size, replace=True)
        sampled_cases = rng.choice(unique_cases, unique_cases.size, replace=True)
        selected: list[float] = []
        for evaluator in sampled_evaluators:
            for case in sampled_cases:
                cells = differences[(evaluators == evaluator) & (cases == case)]
                selected.extend(cells.tolist())
        estimates.append(float(np.median(selected)) if selected else float("nan"))
    ci_low, ci_high = _percentile_ci(np.asarray(estimates))
    return StatResult(float(np.median(differences)), ci_low, ci_high, int(differences.size))
