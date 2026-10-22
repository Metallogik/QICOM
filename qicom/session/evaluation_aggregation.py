"""Pure pandas aggregation of multiple evaluators' answers to one
qualitative evaluation package - no Qt/openpyxl dependency, mirrors
qicom.session.history's own "pure data in, pure data out" role. The
resulting DataFrames are what qicom.gui.evaluation_export turns into a
native-chart Excel report.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from qicom.schema_version import is_supported_version
from qicom.session.evaluation_folder import EvaluationPage, EvaluationQuestion
from qicom.session.evaluation_package import EvaluationPackage, EvaluatorAnswers
from qicom.session.evaluation_statistics import (
    MIN_SUBJECTS_FOR_RELIABILITY,
    StatResult,
    clustered_paired_median_difference,
    cohens_kappa,
    fleiss_kappa,
    gwet_ac2,
    holm_adjust,
    icc_2_1,
    kendalls_w,
    krippendorff_alpha_ordinal,
    paired_probability_of_superiority,
    paired_wilcoxon_p_value,
)
from qicom.session.qualitative_design import (
    DEBLINDING_MAP_VERSION,
    SUPPORTED_DEBLINDING_MAP_VERSIONS,
    WITHHELD_SEED,
)

ANSWERS_DATAFRAME_COLUMNS = [
    "evaluator_id",
    "evaluator_display_name",
    "page_id",
    "page_title",
    "comparison_role",
    "question_id",
    "question_text",
    "slice_index",
    "question_type",
    "response",
    "score",
    "free_text",
]

EXTENDED_ANSWERS_DATAFRAME_COLUMNS = [
    *ANSWERS_DATAFRAME_COLUMNS[:-2],
    "answer_status",
    "not_assessable_reason",
    "elapsed_seconds",
    "exclude_from_statistics",
    "is_calibration",
    "repeat_of_page_id",
    "delivery_id",
    "presentation_seed",
    *ANSWERS_DATAFRAME_COLUMNS[-2:],
]


def _numeric_question_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep ratings while tolerating legacy frames without type metadata."""
    if "question_type" not in df.columns:
        return df
    question_types = df["question_type"]
    return df[question_types.isna() | question_types.eq("rating")]


_QuestionSetLookups = tuple[
    dict[int, tuple[EvaluationPage, EvaluationQuestion]],
    dict[int, str],
    dict[int, set[str]],
]


def _question_set_lookups(package: EvaluationPackage) -> _QuestionSetLookups:
    question_by_id = {q.question_id: (page, q) for page in package.pages for q in page.questions}
    if len(question_by_id) != sum(len(page.questions) for page in package.pages):
        raise ValueError("Evaluation package contains duplicate question IDs.")
    page_title_by_id = {page.page_id: page.title for page in package.pages}
    if len(page_title_by_id) != len(package.pages):
        raise ValueError("Evaluation package contains duplicate page IDs.")
    roles_by_page = {
        page.page_id: {panel.role for panel in page.panels if panel.role != "reference"}
        for page in package.pages
    }
    return question_by_id, page_title_by_id, roles_by_page


def build_answers_dataframe(
    package: EvaluationPackage,
    answer_files: list[EvaluatorAnswers],
    deblinding_maps: Sequence[Mapping[str, object]] = (),
) -> pd.DataFrame:
    """One row per (evaluator, page, comparison_role, question, slice) that
    was ever visited (answered OR explicitly skipped) - a skip is a NaN
    score row, not an absent one, so "answered/skipped/total" is a plain
    groupby count rather than needing a separately-tracked "expected"
    count. `question_id` is unique across every page of `package` (see
    EvaluationPage's own docstring), so `question_text`/`page_title` are
    both looked up from the same flat id -> value maps.

    Each evaluator's answers are checked against the question set they
    were given (:func:`package_for_answers`): a blinded delivery numbers
    its pages and questions in its own presentation order, so another
    delivery's answers are checked against that delivery as its
    `deblinding_maps` entry describes it. Rows stay in presented IDs;
    :func:`apply_deblinding_maps` turns them into source IDs."""
    rows = []
    # One question set per delivery: answers of one delivery share it.
    lookups: dict[str | None, _QuestionSetLookups] = {}
    seen_evaluators: set[str] = set()
    for evaluator in answer_files:
        plan = evaluator.presentation_plan
        delivery = None if plan is None else plan.delivery_id
        if delivery not in lookups:
            lookups[delivery] = _question_set_lookups(
                package_for_answers(package, evaluator, deblinding_maps)
            )
        question_by_id, page_title_by_id, roles_by_page = lookups[delivery]
        if evaluator.test_id != package.test_id:
            raise ValueError(
                f"Answers for evaluator {evaluator.evaluator_id!r} belong to a different test."
            )
        if not evaluator.evaluator_id:
            raise ValueError("Evaluator ID must not be empty.")
        if evaluator.evaluator_id in seen_evaluators:
            raise ValueError(f"Duplicate evaluator ID: {evaluator.evaluator_id!r}.")
        seen_evaluators.add(evaluator.evaluator_id)
        seen_answers: set[tuple[int, str, int, int]] = set()
        for answer in evaluator.answers:
            page = page_title_by_id.get(answer.page_id)
            if page is None:
                raise ValueError(f"Answer references unknown page ID {answer.page_id!r}.")
            question_context = question_by_id.get(answer.question_id)
            if question_context is None:
                raise ValueError(f"Answer references unknown question ID {answer.question_id!r}.")
            question_page, question = question_context
            if question_page.page_id != answer.page_id:
                raise ValueError(
                    f"Question {answer.question_id!r} does not belong to page {answer.page_id!r}."
                )
            declared_roles = roles_by_page[answer.page_id]
            if declared_roles and answer.comparison_role not in declared_roles:
                raise ValueError(
                    f"Answer references unknown comparison role {answer.comparison_role!r} "
                    f"on page {answer.page_id!r}."
                )
            if isinstance(answer.slice_index, bool) or not isinstance(answer.slice_index, int):
                raise ValueError("Answer slice_index must be an integer.")
            if answer.slice_index < 0:
                raise ValueError("Answer slice_index must not be negative.")
            if answer.score is not None:
                if isinstance(answer.score, bool) or not isinstance(answer.score, int):
                    raise ValueError("Answer score must be an integer or null.")
                if not question.score_min <= answer.score <= question.score_max:
                    raise ValueError(
                        f"Score {answer.score!r} is outside question {answer.question_id!r}'s "
                        f"allowed range [{question.score_min}, {question.score_max}]."
                    )
            if question.response_type == "rating" and answer.response is not None:
                raise ValueError("A rating answer cannot contain a structured response.")
            if question.response_type != "rating" and answer.score is not None:
                raise ValueError("A non-rating answer cannot contain a numeric score.")
            if question.response_type != "rating" and answer.response is not None:
                values = (answer.response,) if isinstance(answer.response, str) else answer.response
                if question.options and any(value not in question.options for value in values):
                    raise ValueError(
                        f"Answer contains an unknown option for question {answer.question_id!r}."
                    )
                if question.response_type in {
                    "single_choice",
                    "dropdown",
                    "text",
                    "strict_2afc",
                    "paired_preference",
                } and not isinstance(answer.response, str):
                    raise ValueError("This question requires one textual response.")
                if question.response_type == "multiple_choice" and (
                    not isinstance(answer.response, tuple)
                    or not answer.response
                    or len(set(answer.response)) != len(answer.response)
                ):
                    raise ValueError("A multiple-choice answer must contain unique choices.")
                if question.response_type == "ranking" and (
                    not isinstance(answer.response, tuple)
                    or len(answer.response) != len(question.options)
                    or set(answer.response) != set(question.options)
                ):
                    raise ValueError("A ranking answer must rank every option.")
            status = (
                "skipped"
                if answer.answer_status == "answered"
                and answer.score is None
                and answer.response is None
                else answer.answer_status
            )
            if status not in {"answered", "skipped", "not_assessable"}:
                raise ValueError("Answer status is not supported.")
            if status != "answered" and (answer.score is not None or answer.response is not None):
                raise ValueError("A skipped/not-assessable response cannot contain an answer.")
            if (
                status == "not_assessable"
                and question.not_assessable_requires_reason
                and not (answer.not_assessable_reason and answer.not_assessable_reason.strip())
            ):
                raise ValueError("A not-assessable reason is required for this question.")
            if (
                status == "answered"
                and question.comment_required_below is not None
                and answer.score is not None
                and answer.score <= question.comment_required_below
                and not (answer.free_text and answer.free_text.strip())
            ):
                raise ValueError("A comment is required for this score.")
            answer_key = (
                answer.page_id,
                answer.comparison_role,
                answer.question_id,
                answer.slice_index,
            )
            if answer_key in seen_answers:
                raise ValueError(
                    f"Evaluator {evaluator.evaluator_id!r} has duplicate answer {answer_key!r}."
                )
            seen_answers.add(answer_key)
            rows.append(
                {
                    "evaluator_id": evaluator.evaluator_id,
                    "evaluator_display_name": evaluator.evaluator_display_name
                    or evaluator.evaluator_id,
                    "page_id": answer.page_id,
                    "page_title": page,
                    "comparison_role": answer.comparison_role,
                    "question_id": answer.question_id,
                    "question_text": question.text,
                    "slice_index": answer.slice_index,
                    "question_type": question.response_type,
                    "response": (
                        " → ".join(answer.response)
                        if isinstance(answer.response, tuple)
                        else answer.response
                    ),
                    "score": float(answer.score) if answer.score is not None else float("nan"),
                    "free_text": answer.free_text,
                    "answer_status": status,
                    "not_assessable_reason": answer.not_assessable_reason,
                    "elapsed_seconds": (
                        answer.elapsed_seconds if evaluator.timing_disclosed else None
                    ),
                    "exclude_from_statistics": question_page.exclude_from_statistics,
                    "is_calibration": question_page.is_calibration,
                    "repeat_of_page_id": question_page.repeat_of_page_id,
                    "delivery_id": (
                        evaluator.presentation_plan.delivery_id
                        if evaluator.presentation_plan is not None
                        else None
                    ),
                    "presentation_seed": (
                        evaluator.presentation_plan.seed
                        if evaluator.presentation_plan is not None
                        else None
                    ),
                }
            )
    # Preserve the exact legacy frame shape for v1-v3 packages. New v4
    # packages expose explicit response/provenance fields in raw exports.
    columns = (
        EXTENDED_ANSWERS_DATAFRAME_COLUMNS if package.version >= 4 else ANSWERS_DATAFRAME_COLUMNS
    )
    return pd.DataFrame(rows, columns=columns)


def _analysis_rows(df: pd.DataFrame, *, include_repeats: bool = False) -> pd.DataFrame:
    result = df
    if "exclude_from_statistics" in result.columns:
        result = result[~result["exclude_from_statistics"].fillna(False).astype(bool)]
    if not include_repeats and "repeat_of_page_id" in result.columns:
        result = result[result["repeat_of_page_id"].isna()]
    return result


def _maps_by_delivery(
    deblinding_maps: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    """The maps keyed by the delivery each names; refuses an unknown
    version, a map without a delivery and two maps for one delivery."""
    maps_by_delivery: dict[str, Mapping[str, object]] = {}
    for payload in deblinding_maps:
        version = payload.get("qicom_deblinding_version")
        if not is_supported_version(version, SUPPORTED_DEBLINDING_MAP_VERSIONS):
            raise ValueError(
                f"Unsupported deblinding map version {version!r} "
                f"(this version of QICOM understands versions up to {DEBLINDING_MAP_VERSION})."
            )
        delivery = payload.get("delivery_id")
        if not isinstance(delivery, str) or not delivery.strip():
            raise ValueError("A deblinding map must name the delivery it belongs to.")
        if delivery in maps_by_delivery:
            raise ValueError(f"Two deblinding maps were given for delivery {delivery!r}.")
        maps_by_delivery[delivery] = payload
    return maps_by_delivery


def _source_catalog(
    package: EvaluationPackage, maps_by_delivery: Mapping[str, Mapping[str, object]]
) -> EvaluationPackage | None:
    """`package`'s pages in the author's own IDs: the package itself when it
    is the author's (no presentation plan), a delivery translated through
    its own map, or None when that map was not given."""
    plan = package.presentation_plan
    if plan is None:
        return package
    payload = maps_by_delivery.get(plan.delivery_id)
    if payload is None:
        return None
    presented_pages = {page.page_id: page for page in package.pages}
    pages: list[EvaluationPage] = []
    for presented_id, page_map in _page_maps(payload).items():
        page = presented_pages.get(presented_id)
        if page is None:
            if page_map.complete:
                raise ValueError(
                    f"The deblinding map of delivery {plan.delivery_id!r} names presented page "
                    f"{presented_id}, which this package does not contain."
                )
            continue
        if page_map.is_hidden_repeat:
            continue
        pages.append(
            replace(
                page,
                page_id=page_map.source_page_id,
                questions=[
                    replace(question, question_id=page_map.source_question(question.question_id))
                    for question in page.questions
                ],
                panels=[
                    replace(panel, role=page_map.source_role(panel.role)) for panel in page.panels
                ],
            )
        )
    return replace(package, pages=pages, presentation_plan=None)


def _delivery_view(catalog: EvaluationPackage, payload: Mapping[str, object]) -> EvaluationPackage:
    """The question set one delivery presented, rebuilt from the source
    pages and that delivery's map - every page, hidden repeats included, in
    the delivery's own presented IDs and roles."""
    source_pages = {page.page_id: page for page in catalog.pages}
    pages: list[EvaluationPage] = []
    for presented_id, page_map in _page_maps(payload).items():
        source = source_pages.get(page_map.source_page_id)
        if source is None:
            raise ValueError(
                f"The deblinding map of delivery {page_map.delivery!r} names "
                f"source page {page_map.source_page_id!r}, which this package does not contain."
            )
        pages.append(
            replace(
                source,
                page_id=presented_id,
                questions=[
                    replace(question, question_id=page_map.presented_question(question.question_id))
                    for question in source.questions
                ],
                panels=[
                    replace(panel, role=page_map.presented_role(panel.role))
                    for panel in source.panels
                ],
                repeat_of_page_id=None,
            )
        )
    return replace(catalog, pages=pages)


def package_for_answers(
    package: EvaluationPackage,
    answers: EvaluatorAnswers,
    deblinding_maps: Sequence[Mapping[str, object]] = (),
) -> EvaluationPackage:
    """The question set `answers` were actually given against.

    The package itself, for answers to it; for answers from another blinded
    delivery, that delivery as its map describes it, built on the author's
    package or on this delivery's own map. A delivery written by QICOM
    1.1 or later numbers its pages and questions in its own presentation
    order (its public plan withholds the seed), so its answers can only be
    checked that way; older deliveries kept the author's IDs and are
    still checked against the package directly when no map applies.
    """
    plan = answers.presentation_plan
    package_plan = package.presentation_plan
    if plan is None or (package_plan is not None and plan.delivery_id == package_plan.delivery_id):
        return package
    maps = _maps_by_delivery(deblinding_maps)
    payload = maps.get(plan.delivery_id)
    catalog = _source_catalog(package, maps)
    if payload is not None and catalog is not None:
        return _delivery_view(catalog, payload)
    if plan.seed != WITHHELD_SEED:
        return package
    raise ValueError(
        f"The answers of delivery {plan.delivery_id!r} follow that delivery's own "
        "presentation, like those of any other blinded deliveries: load the deblinding map "
        "of every delivery, together with the author's package or this delivery's own map."
    )


@dataclass(frozen=True)
class _PageMap:
    """One presented page of a deblinding map, validated.

    `complete` holds for a version 2 map, whose presented IDs are opaque: a
    question or role it does not map has no meaning of its own, and leaving
    it in presented form would attribute the answer to whichever source
    happens to share the number. A version 1 map presented the author's IDs,
    so what it leaves out stays as it is, as before.
    """

    delivery: object
    presented_page_id: int
    source_page_id: int
    analysis_page_id: int
    is_hidden_repeat: bool
    questions: dict[int, int]
    panels: dict[str, str]
    complete: bool

    def _missing(self, what: str, value: object) -> ValueError:
        return ValueError(
            f"The deblinding map of delivery {self.delivery!r} does not map {what} {value!r} "
            f"of presented page {self.presented_page_id}; an incomplete map would attribute "
            "answers to the wrong source."
        )

    def source_question(self, presented: int) -> int:
        if presented in self.questions:
            return self.questions[presented]
        if self.complete:
            raise self._missing("question", presented)
        return presented

    def source_role(self, presented: str) -> str:
        if presented in self.panels:
            return self.panels[presented]
        if self.complete:
            raise self._missing("panel", presented)
        return presented

    def presented_question(self, source: int) -> int:
        reverse = {value: key for key, value in self.questions.items()}
        if source in reverse:
            return reverse[source]
        if self.complete:
            raise self._missing("source question", source)
        return source

    def presented_role(self, source: str) -> str:
        reverse = {value: key for key, value in self.panels.items()}
        if source in reverse:
            return reverse[source]
        if self.complete:
            raise self._missing("source panel", source)
        return source


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _one_to_one(pairs: list[tuple[object, object]], what: str, delivery: object) -> None:
    if len({key for key, _value in pairs}) != len(pairs) or len(
        {value for _key, value in pairs}
    ) != len(pairs):
        raise ValueError(
            f"The deblinding map of delivery {delivery!r} maps {what} ambiguously "
            "(one ID given twice)."
        )


def _page_maps(deblinding: Mapping[str, object]) -> dict[int, _PageMap]:
    """Every presented page of a map, validated (see _PageMap)."""
    delivery = deblinding.get("delivery_id")
    version = deblinding.get("qicom_deblinding_version")
    complete = _is_integer(version) and int(version) >= 2  # type: ignore[call-overload]
    pages = deblinding.get("pages")
    if not isinstance(pages, list):
        raise ValueError("Deblinding map pages must be a JSON array.")
    page_maps: dict[int, _PageMap] = {}
    for entry in pages:
        if not isinstance(entry, dict):
            raise ValueError("Each deblinding page must be a JSON object.")
        presented_id = entry.get("presented_page_id")
        source_id = entry.get("source_page_id")
        analysis_id = entry.get("analysis_page_id", None if complete else presented_id)
        if not (_is_integer(presented_id) and _is_integer(source_id) and _is_integer(analysis_id)):
            raise ValueError("Deblinding page IDs must be integers.")
        assert isinstance(presented_id, int) and isinstance(source_id, int)
        assert isinstance(analysis_id, int)
        if presented_id in page_maps:
            raise ValueError(
                f"The deblinding map of delivery {delivery!r} lists presented page "
                f"{presented_id} twice."
            )
        raw_questions = entry.get("questions", None if complete else [])
        raw_panels = entry.get("panels", None if complete else [])
        if not isinstance(raw_questions, list) or not isinstance(raw_panels, list):
            raise ValueError("Deblinding question/panel mappings must be arrays.")
        question_pairs: list[tuple[object, object]] = []
        for question in raw_questions:
            if not isinstance(question, dict):
                if complete:
                    raise ValueError("Each deblinding question must be a JSON object.")
                continue
            public = question.get("presented_question_id")
            source = question.get("source_question_id")
            if not _is_integer(public) or not _is_integer(source):
                raise ValueError("Deblinding question IDs must be integers.")
            question_pairs.append((public, source))
        panel_pairs: list[tuple[object, object]] = []
        for panel in raw_panels:
            if not isinstance(panel, dict):
                if complete:
                    raise ValueError("Each deblinding panel must be a JSON object.")
                continue
            presented_role = panel.get("presented_role", panel.get("alias"))
            source_role = panel.get("source_role")
            if not isinstance(presented_role, str) or not isinstance(source_role, str):
                raise ValueError("Deblinding panel roles must be strings.")
            panel_pairs.append((presented_role, source_role))
        if complete:
            _one_to_one(question_pairs, "questions", delivery)
            _one_to_one(panel_pairs, "panels", delivery)
        page_maps[presented_id] = _PageMap(
            delivery=delivery,
            presented_page_id=presented_id,
            source_page_id=source_id,
            analysis_page_id=analysis_id,
            is_hidden_repeat=bool(entry.get("is_hidden_repeat", False)),
            questions={int(key): int(value) for key, value in question_pairs},  # type: ignore[call-overload]
            panels={str(key): str(value) for key, value in panel_pairs},
            complete=complete,
        )
    if complete and len({page.analysis_page_id for page in page_maps.values()}) != len(page_maps):
        raise ValueError(
            f"The deblinding map of delivery {delivery!r} gives two pages the same analysis ID."
        )
    return page_maps


def _apply_page_maps(
    result: pd.DataFrame,
    source: pd.DataFrame,
    rows: pd.Series,
    page_maps: dict[int, _PageMap],
) -> None:
    """Rewrite, in `result`, the selected `rows` of `source` from presented
    to source IDs and roles. `source` is read, never written: overlapping
    IDs and role swaps are one simultaneous permutation, never a chain of
    in-place substitutions. A row on a page the map does not list is
    refused: its delivery's map is incomplete or belongs to another one."""
    unmapped = sorted(set(source.loc[rows, "page_id"].tolist()) - set(page_maps))
    if unmapped:
        delivery = next(iter(page_maps.values())).delivery if page_maps else None
        raise ValueError(
            f"The deblinding map of delivery {delivery!r} has no entry for presented page(s) "
            f"{', '.join(str(value) for value in unmapped)}; load the map written with that "
            "delivery."
        )
    for presented_page_id, page_map in page_maps.items():
        page_mask = rows & source["page_id"].eq(presented_page_id)
        if not page_mask.any():
            continue
        if page_map.is_hidden_repeat:
            result.loc[page_mask, "repeat_of_page_id"] = page_map.source_page_id
        # Version 2 presents opaque page IDs; the analysis keeps the
        # author's. A version 1 map presented the author's IDs already.
        result.loc[page_mask, "page_id"] = page_map.analysis_page_id
        result.loc[page_mask, "question_id"] = [
            page_map.source_question(int(value)) for value in source.loc[page_mask, "question_id"]
        ]
        result.loc[page_mask, "comparison_role"] = [
            page_map.source_role(str(value)) for value in source.loc[page_mask, "comparison_role"]
        ]


def _with_repeat_column(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    if "repeat_of_page_id" not in result.columns:
        result["repeat_of_page_id"] = np.nan
    return result


def _delivery_label(value: object) -> str:
    return (
        "(none)" if value is None or (isinstance(value, float) and np.isnan(value)) else str(value)
    )


def apply_deblinding_maps(
    df: pd.DataFrame,
    deblinding_maps: Sequence[Mapping[str, object]],
    *,
    blinded_package: bool = False,
) -> pd.DataFrame:
    """Give each delivery's answers back their source pages, questions and
    roles, using that delivery's own author map.

    Every blinded delivery randomises its own presentation - its seed is
    derived from its delivery ID - so the same presented label names a
    different image in each delivery. A map therefore applies only to the
    rows of the delivery it names, and nothing is guessed:

    * once any map is given, an answer row whose delivery has no map is
      refused rather than left in presented labels next to source ones;
    * answers from several deliveries of a blinded package (`blinded_package`)
      are refused without maps, since pooling their presented labels pools
      different images;
    * two maps for the same delivery, a map without a delivery, or an
      unknown map version are refused.

    Answers from a single delivery may still be analysed without its map:
    that is a blinded analysis, consistent within the one presentation.
    """
    maps_by_delivery = _maps_by_delivery(deblinding_maps)

    if "delivery_id" in df.columns:
        deliveries = df["delivery_id"]
    else:
        deliveries = pd.Series([None] * len(df), index=df.index, dtype=object)
    present = list(dict.fromkeys(deliveries.tolist()))
    uncovered = [
        value for value in present if not isinstance(value, str) or value not in maps_by_delivery
    ]
    if maps_by_delivery and uncovered:
        raise ValueError(
            "No deblinding map was loaded for the answers of delivery "
            f"{', '.join(_delivery_label(value) for value in uncovered)}; load the map "
            "written with each delivery, or the scores would be attributed to the wrong images."
        )
    if not maps_by_delivery and blinded_package and len(present) > 1:
        raise ValueError(
            f"These answers come from {len(present)} blinded deliveries, each with its own "
            "presentation order. Load the deblinding map of every delivery before aggregating."
        )

    result = _with_repeat_column(df)
    source = result.copy()
    for delivery, payload in maps_by_delivery.items():
        delivery_rows = deliveries.eq(delivery)
        _apply_page_maps(result, source, delivery_rows, _page_maps(payload))
        seed = payload.get("presentation_seed")
        if "presentation_seed" in result.columns and isinstance(seed, int):
            # The public plan withholds it; the author's map is where it
            # lives. Unsigned 64-bit, so wider than the column may be.
            result["presentation_seed"] = result["presentation_seed"].astype(object)
            result.loc[delivery_rows, "presentation_seed"] = seed
    return result


def apply_deblinding_map(df: pd.DataFrame, deblinding: dict[str, object]) -> pd.DataFrame:
    """Apply one author map after importing public answer files.

    This is intentionally explicit: evaluator packages and response files do
    not contain source roles or hidden-repeat relationships. A map that names
    its delivery - every map QICOM writes does - goes through
    :func:`apply_deblinding_maps`, so it is applied to that delivery's rows
    only and answers of any other delivery are refused. A map without a
    delivery (hand-built, pre-dating the field) is applied to every row,
    which is correct only when all the answers come from one delivery.
    """
    if "delivery_id" in deblinding:
        return apply_deblinding_maps(df, [deblinding])
    result = _with_repeat_column(df)
    source = result.copy()
    rows = pd.Series(True, index=source.index)
    _apply_page_maps(result, source, rows, _page_maps(deblinding))
    return result


def aggregate_by_question(df: pd.DataFrame) -> pd.DataFrame:
    """mean/std/count of score per (page, comparison_role, question) -
    count is of non-null scores only (an explicit skip contributes to
    neither the mean nor the count, exactly like a plain pandas groupby
    already does by default)."""
    columns = [
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
        "mean",
        "std",
        "count",
    ]
    df = _numeric_question_rows(_analysis_rows(df))
    if df.empty:
        return pd.DataFrame(columns=columns)
    grouped = df.groupby(
        ["page_id", "page_title", "comparison_role", "question_id", "question_text"],
        as_index=False,
    )["score"].agg(["mean", "std", "count"])
    return grouped.reindex(columns=columns)


def _assigned_response_rows(
    observed: pd.DataFrame, expected_assignments: pd.DataFrame, group_cols: list[str]
) -> pd.DataFrame:
    """Materialize absent rows from explicit assignments, never a guessed cross-product."""
    keys = ["evaluator_id", "page_id", "comparison_role", "question_id", "slice_index"]
    required = list(dict.fromkeys([*keys, *group_cols]))
    if not set(required).issubset(expected_assignments.columns):
        raise ValueError(f"Expected assignments require columns {required!r}.")
    assigned = _numeric_question_rows(_analysis_rows(expected_assignments))
    if assigned[keys].isna().any().any() or assigned.duplicated(keys).any():
        raise ValueError("Expected assignment keys must be non-null and unique.")
    if not observed.empty:
        if observed.duplicated(keys).any():
            raise ValueError("Response accounting contains duplicate assignment keys.")
        if (
            not pd.MultiIndex.from_frame(observed[keys])
            .isin(pd.MultiIndex.from_frame(assigned[keys]))
            .all()
        ):
            raise ValueError("Received response has no matching expected assignment.")
    else:
        observed = pd.DataFrame(columns=[*required, "score", "answer_status"])
    result = assigned[required].merge(
        observed,
        on=keys,
        how="left",
        suffixes=("_assignment", ""),
        indicator=True,
        validate="one_to_one",
    )
    for column in group_cols:
        if column not in keys:
            result[column] = result[column].fillna(result.pop(f"{column}_assignment"))
    if "answer_status" not in result.columns:
        result["answer_status"] = np.where(result["score"].notna(), "answered", "skipped")
    result.loc[result["_merge"].eq("left_only"), "answer_status"] = "missing"
    return result.drop(columns="_merge")


def descriptive_by_question(
    df: pd.DataFrame, *, expected_assignments: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Ordinal summaries with an explicit response-accounting denominator.

    ``expected_assignments`` supplies one row for every assigned evaluator /
    page / panel / question / slice, with the same descriptive group metadata
    as the response frame. This supports incomplete study designs and readers
    who return no row. Without assignments, missing is unknown (NaN) and the
    response rate describes received records only; no crossed design is inferred.
    """

    columns = [
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
        "median",
        "q1",
        "q3",
        "iqr",
        "answered",
        "skipped",
        "not_assessable",
        "missing",
        "response_rate",
        "response_denominator",
        "response_denominator_definition",
        "top_box_count",
        "top_box_percent",
        "bottom_box_count",
        "bottom_box_percent",
        "divergent_count",
        "divergent_percent",
    ]
    numeric = _numeric_question_rows(_analysis_rows(df))
    if numeric.empty and expected_assignments is None:
        return pd.DataFrame(columns=columns)
    group_cols = [
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
    ]
    if expected_assignments is not None:
        numeric = _assigned_response_rows(numeric, expected_assignments, group_cols)
    rows: list[dict[str, object]] = []
    for key, group in numeric.groupby(group_cols, dropna=False):
        scores = group["score"].dropna().astype(float)
        status = (
            group["answer_status"].fillna("skipped")
            if "answer_status" in group.columns
            else pd.Series(np.where(group["score"].notna(), "answered", "skipped"))
        )
        answered = int(scores.size)
        skipped = int(status.eq("skipped").sum())
        not_assessable = int(status.eq("not_assessable").sum())
        missing = (
            int(status.eq("missing").sum()) if expected_assignments is not None else float("nan")
        )
        if scores.empty:
            median = q1 = q3 = float("nan")
            top_count = bottom_count = divergent_count = 0
        else:
            q1, median, q3 = (float(value) for value in scores.quantile([0.25, 0.5, 0.75]))
            minimum, maximum = float(scores.min()), float(scores.max())
            top_threshold = maximum - 1.0
            bottom_threshold = minimum + 1.0
            top_count = int((scores >= top_threshold).sum())
            bottom_count = int((scores <= bottom_threshold).sum())
            divergent_count = int(((scores < q1) | (scores > q3)).sum())
        denominator = len(group)
        rows.append(
            {
                **dict(zip(group_cols, key, strict=True)),
                "median": median,
                "q1": q1,
                "q3": q3,
                "iqr": q3 - q1,
                "answered": answered,
                "skipped": skipped,
                "not_assessable": not_assessable,
                "missing": missing,
                "response_rate": answered / denominator if denominator else float("nan"),
                "response_denominator": denominator,
                "response_denominator_definition": (
                    "explicit_expected_assignments"
                    if expected_assignments is not None
                    else "received_records; unrecorded_assignments_unknown"
                ),
                "top_box_count": top_count,
                "top_box_percent": top_count / answered if answered else float("nan"),
                "bottom_box_count": bottom_count,
                "bottom_box_percent": bottom_count / answered if answered else float("nan"),
                "divergent_count": divergent_count,
                "divergent_percent": (divergent_count / answered if answered else float("nan")),
            }
        )
    return pd.DataFrame(rows, columns=columns)


#: How the ``percent`` column of :func:`category_distribution` is normalised.
CATEGORY_PERCENT_DENOMINATOR = "answered responses for the question"

#: How the ``percent`` column of :func:`likert_distribution` is normalised.
LIKERT_PERCENT_DENOMINATOR = "answered ratings for the question"


def category_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """Counts/percentages for categorical, multiple-choice, and preference items.

    Percentages are shares of the *responses*, not of the selections. A
    multiple-choice item lets one respondent tick several categories, so the
    selections sum to more than the number of respondents: dividing by the
    selection total makes every category look rarer the more boxes people tick
    (two respondents, one choosing A and B and one choosing A, would report
    A=67% and B=33% instead of A=100% and B=50%). The denominator and its
    definition are exported alongside the percentage so a reader never has to
    infer which one was used.

    Skipped, not-assessable, and missing responses contribute no category and
    are not counted in the denominator; they stay distinct in the separate
    response-accounting output.
    """

    columns = [
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
        "question_type",
        "category",
        "count",
        "percent",
        "denominator",
        "denominator_definition",
    ]
    analysis = _analysis_rows(df)
    if analysis.empty or "question_type" not in analysis.columns:
        return pd.DataFrame(columns=columns)
    categorical = analysis[~analysis["question_type"].isin(["rating", "text", "ranking"])]
    categorical = categorical[categorical["response"].notna()].copy()
    if categorical.empty:
        return pd.DataFrame(columns=columns)
    group_cols = columns[:6]
    # One row per answered response, counted before the multiple-choice split.
    respondents = categorical.groupby(group_cols, dropna=False).size().rename("denominator")
    # Multiple-choice responses use the existing visible separator.
    categorical["category"] = categorical["response"].astype(str).str.split(" → ")
    categorical = categorical.explode("category")
    counts = (
        categorical.groupby([*group_cols, "category"], dropna=False)
        .size()
        .reset_index(name="count")
    )
    counts = counts.merge(respondents.reset_index(), on=group_cols, how="left")
    counts["percent"] = counts["count"] / counts["denominator"]
    counts["denominator_definition"] = CATEGORY_PERCENT_DENOMINATOR
    return counts.reindex(columns=columns)


def likert_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """Per-category frequencies and percentages for rating (Likert) items.

    Complements the ordinal descriptives: a median of 4 says nothing about
    whether the panel clustered on 4 or split between 2 and 5. Percentages are
    shares of the answered ratings for that question; skipped, not-assessable,
    and missing responses are excluded from both numerator and denominator and
    remain reported as distinct states by the response accounting.
    """

    columns = [
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
        "category",
        "count",
        "percent",
        "denominator",
        "denominator_definition",
    ]
    analysis = _analysis_rows(df)
    if analysis.empty or "question_type" not in analysis.columns:
        return pd.DataFrame(columns=columns)
    ratings = analysis[analysis["question_type"].eq("rating")]
    ratings = ratings[ratings["score"].notna()].copy()
    if ratings.empty:
        return pd.DataFrame(columns=columns)
    group_cols = columns[:5]
    ratings["category"] = ratings["score"].astype(float)
    counts = (
        ratings.groupby([*group_cols, "category"], dropna=False).size().reset_index(name="count")
    )
    totals = ratings.groupby(group_cols, dropna=False).size().rename("denominator")
    counts = counts.merge(totals.reset_index(), on=group_cols, how="left")
    counts["percent"] = counts["count"] / counts["denominator"]
    counts["denominator_definition"] = LIKERT_PERCENT_DENOMINATOR
    return counts.reindex(columns=columns)


def ranking_concordance(df: pd.DataFrame) -> pd.DataFrame:
    """Kendall's W for every ranking question/role, with effective counts."""

    columns = [
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
        "n_rankings",
        "kendall_w",
        "ci_low",
        "ci_high",
    ]
    analysis = _analysis_rows(df)
    if analysis.empty or "question_type" not in analysis.columns:
        return pd.DataFrame(columns=columns)
    rankings = analysis[analysis["question_type"].eq("ranking") & analysis["response"].notna()]
    rows: list[dict[str, object]] = []
    group_cols = columns[:5]
    for key, group in rankings.groupby(group_cols):
        parsed = [str(response).split(" → ") for response in group["response"]]
        options = sorted({option for ranking in parsed for option in ranking})
        if not options:
            continue
        matrix = np.asarray(
            [[ranking.index(option) + 1 for option in options] for ranking in parsed], dtype=float
        )
        result = kendalls_w(matrix)
        rows.append(
            {
                **dict(zip(group_cols, key, strict=True)),
                "n_rankings": result.n_subjects,
                "kendall_w": result.value,
                "ci_low": result.ci_low,
                "ci_high": result.ci_high,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def filter_open_text(
    df: pd.DataFrame,
    *,
    evaluator_id: str | None = None,
    question_id: int | None = None,
    contains: str | None = None,
) -> pd.DataFrame:
    """Filterable verbatim comments/open text without scoring or recoding them."""

    result = df
    if evaluator_id is not None:
        result = result[result["evaluator_id"].eq(evaluator_id)]
    if question_id is not None:
        result = result[result["question_id"].eq(question_id)]
    if contains:
        response = result.get("response", pd.Series(index=result.index, dtype=object))
        comments = result.get("free_text", pd.Series(index=result.index, dtype=object))
        mask = response.fillna("").astype(str).str.contains(contains, case=False, regex=False)
        mask |= comments.fillna("").astype(str).str.contains(contains, case=False, regex=False)
        result = result[mask]
    return result.copy()


def aggregate_by_evaluator_and_question(df: pd.DataFrame) -> pd.DataFrame:
    """Mean score per (evaluator, page, comparison_role, question) - the
    "By Evaluator" breakdown, one row per evaluator per question."""
    columns = [
        "evaluator_id",
        "evaluator_display_name",
        "page_id",
        "page_title",
        "comparison_role",
        "question_id",
        "question_text",
        "mean",
    ]
    df = _numeric_question_rows(_analysis_rows(df))
    if df.empty:
        return pd.DataFrame(columns=columns)
    grouped = df.groupby(
        [
            "evaluator_id",
            "evaluator_display_name",
            "page_id",
            "page_title",
            "comparison_role",
            "question_id",
            "question_text",
        ],
        as_index=False,
    )["score"].mean()
    return grouped.rename(columns={"score": "mean"}).reindex(columns=columns)


def inter_rater_spread(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (page, comparison_role, question) - the mean, across
    every slice, of that slice's own across-evaluator standard deviation
    (lower = evaluators agreed more). Computed in two steps: first the
    per-slice std (needs at least 2 evaluators to have answered that exact
    slice, else NaN - excluded from the rollup mean like any other NaN),
    then its mean rolled up per question/comparison/page. A simple,
    well-understood first cut at inter-rater agreement, not a formal
    statistic like Cohen's/Fleiss' kappa or ICC (see the plan's own Scope
    Decisions).
    """
    columns = ["page_id", "page_title", "comparison_role", "question_id", "question_text", "std"]
    df = _numeric_question_rows(_analysis_rows(df))
    if df.empty:
        return pd.DataFrame(columns=columns)
    per_slice = df.groupby(
        ["page_id", "page_title", "comparison_role", "question_id", "question_text", "slice_index"]
    )["score"].std()
    per_slice = per_slice.reset_index(name="std")
    rolled_up = per_slice.groupby(
        ["page_id", "page_title", "comparison_role", "question_id", "question_text"],
        as_index=False,
    )["std"].mean()
    return rolled_up.reindex(columns=columns)


_RELIABILITY_COLUMNS = [
    "page_id",
    "page_title",
    "comparison_role",
    "question_id",
    "question_text",
    "n_evaluators",
    "n_subjects",
    "n_ratings",
    "n_missing",
    "primary_method",
    "ordinal_alpha",
    "alpha_ci_low",
    "alpha_ci_high",
    "ac2",
    "ac2_method",
    "ac2_ci_low",
    "ac2_ci_high",
    "method_limitations",
    "kappa_type",
    "kappa",
    "kappa_ci_low",
    "kappa_ci_high",
    "icc",
    "icc_ci_low",
    "icc_ci_high",
]


def inter_rater_reliability(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (page, comparison_role, question): a formal inter-rater
    reliability statistic - Cohen's kappa with exactly 2 evaluators,
    Fleiss' kappa with 3+ (`kappa_type` names which one), and the
    intraclass correlation coefficient (ICC(2,1)) either way - each with
    its own 95% bootstrap CI (see qicom.session.evaluation_statistics).

    Complete-case only, unlike inter_rater_spread above: a slice only
    contributes if *every* evaluator who touched this question answered
    that exact slice - kappa/Fleiss'/ICC are all undefined for a ragged
    (partially missing) ratings matrix, so a slice with a partial answer
    is dropped from this statistic entirely rather than silently
    distorting it. `kappa`/`icc` (and their CIs) are NaN when there isn't
    enough complete-case data to compute them (see MIN_SUBJECTS_FOR_
    RELIABILITY) - the row still appears, so the report always accounts
    for every question even when it has nothing to report for it yet.
    """
    df = _numeric_question_rows(_analysis_rows(df))
    if df.empty:
        return pd.DataFrame(columns=_RELIABILITY_COLUMNS)
    group_cols = ["page_id", "page_title", "comparison_role", "question_id", "question_text"]
    rows = []
    for key, group in df.groupby(group_cols):
        page_id, page_title, comparison_role, question_id, question_text = key
        if group.duplicated(subset=["slice_index", "evaluator_id"]).any():
            raise ValueError("Reliability input contains duplicate evaluator/slice ratings.")
        pivot = group.pivot(index="slice_index", columns="evaluator_id", values="score")
        complete = pivot.dropna(axis=0, how="any")
        n_evaluators = complete.shape[1]
        n_subjects = complete.shape[0]

        kappa_type: str | None = None
        kappa_result = StatResult(None, None, None, n_subjects)
        icc_result = StatResult(None, None, None, n_subjects)
        alpha_result = krippendorff_alpha_ordinal(pivot.to_numpy(dtype=float))
        ac2_result = gwet_ac2(pivot.to_numpy(dtype=float))
        if n_evaluators >= 2 and n_subjects >= MIN_SUBJECTS_FOR_RELIABILITY:
            matrix = complete.to_numpy()
            if n_evaluators == 2:
                kappa_type = "cohen"
                kappa_result = cohens_kappa(matrix)
            else:
                kappa_type = "fleiss"
                kappa_result = fleiss_kappa(matrix)
            icc_result = icc_2_1(matrix)

        rows.append(
            {
                "page_id": page_id,
                "page_title": page_title,
                "comparison_role": comparison_role,
                "question_id": question_id,
                "question_text": question_text,
                "n_evaluators": n_evaluators,
                "n_subjects": n_subjects,
                "n_ratings": alpha_result.n_ratings,
                "n_missing": alpha_result.n_missing,
                "primary_method": alpha_result.method,
                "ordinal_alpha": alpha_result.value,
                "alpha_ci_low": alpha_result.ci_low,
                "alpha_ci_high": alpha_result.ci_high,
                "ac2": ac2_result.value,
                "ac2_method": ac2_result.method,
                "ac2_ci_low": ac2_result.ci_low,
                "ac2_ci_high": ac2_result.ci_high,
                "method_limitations": alpha_result.limitation,
                "kappa_type": kappa_type,
                "kappa": kappa_result.value,
                "kappa_ci_low": kappa_result.ci_low,
                "kappa_ci_high": kappa_result.ci_high,
                "icc": icc_result.value,
                "icc_ci_low": icc_result.ci_low,
                "icc_ci_high": icc_result.ci_high,
            }
        )
    return pd.DataFrame(rows, columns=_RELIABILITY_COLUMNS)


_PAIRWISE_AUC_COLUMNS = [
    "page_id",
    "page_title",
    "question_id",
    "question_text",
    "comparison_role_a",
    "comparison_role_b",
    "n_a",
    "n_b",
    "auc",
    "auc_ci_low",
    "auc_ci_high",
    "p_value",
    "p_value_holm",
    "median_difference",
    "median_difference_ci_low",
    "median_difference_ci_high",
    "statistical_unit",
]


def pairwise_auc_by_comparison(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (page, question, comparison_role_a, comparison_role_b) -
    every unordered pair of comparison images rated on that question -
    with a paired probability-of-superiority effect size. Scores are first
    averaged across slices within each evaluator/role, then roles are paired
    by evaluator. This preserves the repeated-measures design and avoids
    pretending correlated slices from the same evaluator are independent
    observations. Its 95% bootstrap CI resamples evaluator pairs, and the
    p-value comes from a two-sided Wilcoxon signed-rank test.

    A page with only one comparison role produces no rows at all (there's
    nothing to pair it against) - a legitimate, common case (most
    evaluations only ever compare one candidate against the reference),
    not an error.
    """
    df = _numeric_question_rows(_analysis_rows(df))
    if df.empty:
        return pd.DataFrame(columns=_PAIRWISE_AUC_COLUMNS)
    group_cols = ["page_id", "page_title", "question_id", "question_text"]
    rows = []
    for key, group in df.groupby(group_cols):
        page_id, page_title, question_id, question_text = key
        roles = sorted(str(role) for role in group["comparison_role"].unique())
        evaluator_means = group.groupby(["evaluator_id", "comparison_role"])["score"].mean()
        paired = evaluator_means.unstack("comparison_role")
        for role_a, role_b in itertools.combinations(roles, 2):
            complete = paired[[role_a, role_b]].dropna()
            sample_a = complete[role_a].to_numpy()
            sample_b = complete[role_b].to_numpy()
            auc_result = paired_probability_of_superiority(sample_a, sample_b)
            p_value = paired_wilcoxon_p_value(sample_a, sample_b)
            median_result = clustered_paired_median_difference(
                sample_a,
                sample_b,
                complete.index.to_numpy(),
                np.full(sample_a.shape, page_id),
            )
            rows.append(
                {
                    "page_id": page_id,
                    "page_title": page_title,
                    "question_id": question_id,
                    "question_text": question_text,
                    "comparison_role_a": role_a,
                    "comparison_role_b": role_b,
                    "n_a": sample_a.size,
                    "n_b": sample_b.size,
                    "auc": auc_result.value,
                    "auc_ci_low": auc_result.ci_low,
                    "auc_ci_high": auc_result.ci_high,
                    "p_value": p_value,
                    "p_value_holm": None,
                    "median_difference": median_result.value,
                    "median_difference_ci_low": median_result.ci_low,
                    "median_difference_ci_high": median_result.ci_high,
                    "statistical_unit": "paired evaluator mean within page",
                }
            )
    adjusted = holm_adjust([row["p_value"] for row in rows])
    for row, corrected in zip(rows, adjusted, strict=True):
        row["p_value_holm"] = corrected
    return pd.DataFrame(rows, columns=_PAIRWISE_AUC_COLUMNS)


def paired_comparison_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Across-case paired analysis with independent evaluator/case clusters.

    Pages are cases, scores are first averaged across slices in each
    evaluator/page/role cell, and question text identifies the same endpoint
    across case pages. This is the non-pseudoreplicated study-level analysis.
    """

    columns = [
        "question_text",
        "comparison_role_a",
        "comparison_role_b",
        "n_pairs",
        "n_evaluators",
        "n_cases",
        "median_difference",
        "median_difference_ci_low",
        "median_difference_ci_high",
        "probability_superiority",
        "superiority_ci_low",
        "superiority_ci_high",
        "wilcoxon_p_raw",
        "wilcoxon_p_holm",
        "statistical_unit",
    ]
    numeric = _numeric_question_rows(_analysis_rows(df))
    if numeric.empty:
        return pd.DataFrame(columns=columns)
    cells = (
        numeric.groupby(
            ["question_text", "evaluator_id", "page_id", "comparison_role"], as_index=False
        )["score"]
        .mean()
        .dropna(subset=["score"])
    )
    rows: list[dict[str, object]] = []
    for question_text, group in cells.groupby("question_text"):
        roles = sorted(str(role) for role in group["comparison_role"].unique())
        pivot = group.pivot_table(
            index=["evaluator_id", "page_id"], columns="comparison_role", values="score"
        )
        for role_a, role_b in itertools.combinations(roles, 2):
            complete = pivot[[role_a, role_b]].dropna()
            a = complete[role_a].to_numpy(dtype=float)
            b = complete[role_b].to_numpy(dtype=float)
            evaluators = complete.index.get_level_values("evaluator_id").to_numpy()
            cases = complete.index.get_level_values("page_id").to_numpy()
            median_result = clustered_paired_median_difference(a, b, evaluators, cases)
            superiority = paired_probability_of_superiority(a, b)
            p_value = paired_wilcoxon_p_value(a, b)
            rows.append(
                {
                    "question_text": question_text,
                    "comparison_role_a": role_a,
                    "comparison_role_b": role_b,
                    "n_pairs": len(complete),
                    "n_evaluators": len(np.unique(evaluators)),
                    "n_cases": len(np.unique(cases)),
                    "median_difference": median_result.value,
                    "median_difference_ci_low": median_result.ci_low,
                    "median_difference_ci_high": median_result.ci_high,
                    "probability_superiority": superiority.value,
                    "superiority_ci_low": superiority.ci_low,
                    "superiority_ci_high": superiority.ci_high,
                    "wilcoxon_p_raw": p_value,
                    "wilcoxon_p_holm": None,
                    "statistical_unit": "evaluator × case; slices averaged within cell",
                }
            )
    p_values = [
        float(row["wilcoxon_p_raw"]) if isinstance(row["wilcoxon_p_raw"], (int, float)) else None
        for row in rows
    ]
    adjusted = holm_adjust(p_values)
    for row, corrected in zip(rows, adjusted, strict=True):
        row["wilcoxon_p_holm"] = corrected
    return pd.DataFrame(rows, columns=columns)
