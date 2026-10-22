"""Confidence interval on a stack-aggregate metric's mean, treating the
volume's slices as the sample it was averaged over.

Deliberately built on nothing beyond numpy/scipy (already hard
dependencies - see requirements/base.txt), matching the same reasoning as
qicom.session.evaluation_statistics: this app ships as a standalone
PyInstaller build, so a new dependency is a real, ongoing build-size cost
for a formula this small.

*** What this interval is, and what it is not ***

Every reference metric without a native 3D implementation is computed
slice by slice and collapsed to a mean (see qicom.metrics.aggregation.
apply_2d_metric). The interval below is the textbook Student-t interval
around that mean, with the stack's slices playing the role of the sample.

The textbook interval assumes independent observations. Slices of one
volume are *not* independent: they image the same anatomy a millimetre
apart, so neighbouring slices carry largely the same structure, and the
metric's slice-to-slice variation is correspondingly smaller than it
would be across genuinely separate samples. The effective sample size is
therefore smaller than n, and this interval is narrower - optimistically
so - than a correctly-specified one would be.

It is honest as a *descriptive* summary: "how much does this metric move
as you walk through the stack, and how precisely is its average pinned
down under an independence assumption we know is violated". It is not a
formal inference about a population, and must not be reported as one.
Every user-facing surface that shows these numbers says so (see the
results_table.ci_note string in qicom/gui/i18n/locales).

Not offered here on purpose: a block bootstrap or an effective-sample-size
correction, either of which would narrow the honesty gap. Both need a
defensible estimate of the spatial correlation length in slices, which
depends on slice thickness and on the anatomy being imaged - data this
module does not have and would have to invent. A visible caveat on a
simple, universally understood formula is the smaller lie.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

DEFAULT_CONFIDENCE_LEVEL = 0.95

MIN_SLICES_FOR_CONFIDENCE_INTERVAL = 2
"""One slice is not a sample: the t interval needs n-1 >= 1 degrees of
freedom, so a single-slice stack reports NaN rather than a made-up width."""


def mean_confidence_interval(
    values: Sequence[float], level: float = DEFAULT_CONFIDENCE_LEVEL
) -> tuple[float, float]:
    """Two-sided Student-t confidence interval on the mean of `values`.

        mean +/- t(n-1, (1+level)/2) * s / sqrt(n)

    with `s` the *sample* standard deviation (ddof=1), as the t interval
    requires. Note this is deliberately not the same estimator as the
    `std` column of the results table, which is and stays the population
    standard deviation (ddof=0, see qicom.metrics.aggregation.
    apply_2d_metric) so that already-computed and already-persisted runs
    keep reading back unchanged - the interval below will therefore not
    reproduce exactly from that displayed `std`, by a factor of
    sqrt(n/(n-1)).

    The returned midpoint is exactly the results table's `value` for the
    same row: both are a plain np.mean over the identical list of
    per-slice floats.

    Returns (nan, nan) - never a fabricated number - whenever no interval
    can be estimated:
    - fewer than MIN_SLICES_FOR_CONFIDENCE_INTERVAL values, including the
      2D single-image case that has no per-slice sample at all;
    - any non-finite value, e.g. PSNR's +inf on a pair of identical
      slices (which already leaves `std` NaN today).

    A stack whose slices all scored identically yields a zero-width
    interval (both endpoints equal to the mean) rather than NaN: that is
    a real, correctly-estimated result - zero observed variability - not
    a failure to estimate one.
    """
    if not 0.0 < level < 1.0:
        raise ValueError(f"level must be strictly between 0 and 1, got {level!r}.")

    sample = np.asarray(values, dtype=float)
    if sample.size < MIN_SLICES_FOR_CONFIDENCE_INTERVAL or not np.all(np.isfinite(sample)):
        return float("nan"), float("nan")

    mean = float(np.mean(sample))
    standard_error = float(np.std(sample, ddof=1) / np.sqrt(sample.size))
    critical = float(scipy_stats.t.ppf(0.5 + level / 2.0, df=sample.size - 1))
    half_width = critical * standard_error
    return mean - half_width, mean + half_width


def has_confidence_intervals(dataframe: pd.DataFrame) -> bool:
    """Whether any row of a run_all_metrics-shaped DataFrame actually
    carries an interval - the single rule for "is the caveat worth
    showing here", shared by the results table's note label and the
    export's own notes sheet so the two can never disagree.

    False for an all-2D run (every ci_lower is NaN), for an empty table,
    and for a DataFrame predating these columns entirely - a results
    workbook or project saved before confidence intervals existed reads
    back without them, and must not grow a note about a column it has no
    values in.
    """
    if "ci_lower" not in dataframe.columns:
        return False
    return bool(dataframe["ci_lower"].notna().any())
