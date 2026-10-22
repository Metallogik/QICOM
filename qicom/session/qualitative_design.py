"""Deterministic presentation design for blinded qualitative studies.

The authoring model and the evaluator-facing order are deliberately separate:
the former keeps stable page/question/panel identifiers, while a
``PresentationPlan`` contains the reproducible order and neutral labels seen by
one delivery.  Only an author-side deblinding map relates those labels to source
roles; answer files contain the plan but never the map.

A blinded delivery's seed also mixes in a secret only its author holds: the
test and delivery IDs travel with the evaluator's package, and a master seed
an author types is small enough to guess, so neither may be all that stands
between an evaluator and the plan that names which panel is which.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class _Question(Protocol):
    @property
    def question_id(self) -> int: ...


class _Panel(Protocol):
    @property
    def role(self) -> str: ...


class _Page(Protocol):
    @property
    def page_id(self) -> int: ...

    @property
    def questions(self) -> Sequence[_Question]: ...

    @property
    def panels(self) -> Sequence[_Panel]: ...


#: What one answer covers on a multi-slice image. Under ``series`` an evaluator
#: answers each question once per page and comparison, and every such answer is
#: recorded at slice index ``SERIES_ANSWER_SLICE_INDEX``; under ``slice`` each
#: slice of the reference series is answered on its own, at its own index.
ANSWER_SCOPE_SERIES = "series"
ANSWER_SCOPE_SLICE = "slice"
ANSWER_SCOPES = (ANSWER_SCOPE_SERIES, ANSWER_SCOPE_SLICE)
SERIES_ANSWER_SLICE_INDEX = 0


@dataclass(frozen=True)
class QualitativeDesign:
    """Author choices that affect presentation and prespecified analysis."""

    general_instructions: str | None = None
    randomize_pages: bool = False
    randomize_questions: bool = False
    randomize_panels: bool = False
    counterbalance_panels: bool = False
    neutral_panel_labels: bool = False
    master_seed: int = 0
    hidden_repeat_percent: float = 0.0
    hidden_repeat_count: int = 0
    hidden_repeat_min_distance: int = 2
    advanced_statistics: bool = False
    collect_timing: bool = True
    disclose_timing: bool = True
    answer_scope: str = ANSWER_SCOPE_SERIES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> QualitativeDesign:
        if value is None:
            # No design at all is a package from before designs existed:
            # its answers were per slice, like every other stored design
            # without answer_scope below.
            return cls(answer_scope=ANSWER_SCOPE_SLICE)
        if not isinstance(value, dict):
            raise ValueError("design must be a JSON object or null.")
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"design contains unsupported fields: {sorted(unknown)!r}.")
        if "answer_scope" not in value:
            # Migration: a design stored before answer_scope existed was
            # answered slice by slice, and its answers carry those slices.
            # The new default (one answer per series) would show them
            # all as missing but the first slice's.
            value = {**value, "answer_scope": ANSWER_SCOPE_SLICE}
        design = cls(**value)
        design.validate()
        return design

    @property
    def answers_per_series(self) -> bool:
        return self.answer_scope == ANSWER_SCOPE_SERIES

    def validate(self) -> None:
        if self.general_instructions is not None and not isinstance(self.general_instructions, str):
            raise ValueError("general_instructions must be a string or null.")
        for field_name in (
            "randomize_pages",
            "randomize_questions",
            "randomize_panels",
            "counterbalance_panels",
            "neutral_panel_labels",
            "advanced_statistics",
            "collect_timing",
            "disclose_timing",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"design.{field_name} must be a boolean.")
        if isinstance(self.master_seed, bool) or not isinstance(self.master_seed, int):
            raise ValueError("design.master_seed must be an integer.")
        if isinstance(self.hidden_repeat_percent, bool) or not isinstance(
            self.hidden_repeat_percent, (int, float)
        ):
            raise ValueError("design.hidden_repeat_percent must be numeric.")
        if not 0.0 <= float(self.hidden_repeat_percent) <= 100.0:
            raise ValueError("design.hidden_repeat_percent must be between 0 and 100.")
        if (
            isinstance(self.hidden_repeat_count, bool)
            or not isinstance(self.hidden_repeat_count, int)
            or self.hidden_repeat_count < 0
        ):
            raise ValueError("design.hidden_repeat_count must be an integer >= 0.")
        if (
            isinstance(self.hidden_repeat_min_distance, bool)
            or not isinstance(self.hidden_repeat_min_distance, int)
            or self.hidden_repeat_min_distance < 1
        ):
            raise ValueError("design.hidden_repeat_min_distance must be an integer >= 1.")
        if self.answer_scope not in ANSWER_SCOPES:
            raise ValueError(f"design.answer_scope must be one of {list(ANSWER_SCOPES)!r}.")


#: The seed a public plan carries in place of the real one: a blinded delivery
#: keeps its seed in the author's map only. The real seed is a 64-bit hash,
#: so 0 never names one in practice.
WITHHELD_SEED = 0


@dataclass(frozen=True)
class PresentationPlan:
    """The complete, persisted order for one evaluator delivery."""

    delivery_id: str
    seed: int
    page_order: tuple[int, ...]
    question_order: dict[int, tuple[int, ...]]
    panel_order: dict[int, tuple[str, ...]]
    panel_aliases: dict[int, dict[str, str]]
    repeat_of: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "seed": self.seed,
            "page_order": list(self.page_order),
            "question_order": {
                str(page_id): list(order) for page_id, order in self.question_order.items()
            },
            "panel_order": {
                str(page_id): list(order) for page_id, order in self.panel_order.items()
            },
            "panel_aliases": {
                str(page_id): aliases for page_id, aliases in self.panel_aliases.items()
            },
            "repeat_of": {str(page_id): source_id for page_id, source_id in self.repeat_of.items()},
        }

    @classmethod
    def from_dict(cls, value: object) -> PresentationPlan | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("presentation_plan must be a JSON object or null.")
        delivery_id = value.get("delivery_id")
        seed = value.get("seed")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError("presentation_plan.delivery_id must be a non-empty string.")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("presentation_plan.seed must be an integer.")
        page_order = _integer_tuple(value.get("page_order"), "presentation_plan.page_order")
        question_order = _integer_tuple_mapping(
            value.get("question_order"), "presentation_plan.question_order"
        )
        panel_order = _string_tuple_mapping(
            value.get("panel_order"), "presentation_plan.panel_order"
        )
        aliases = _aliases_mapping(value.get("panel_aliases"))
        raw_repeat = value.get("repeat_of", {})
        if not isinstance(raw_repeat, dict):
            raise ValueError("presentation_plan.repeat_of must be a JSON object.")
        repeat_of = {int(key): int(source) for key, source in raw_repeat.items()}
        return cls(
            delivery_id=delivery_id,
            seed=seed,
            page_order=page_order,
            question_order=question_order,
            panel_order=panel_order,
            panel_aliases=aliases,
            repeat_of=repeat_of,
        )


def stable_delivery_seed(
    test_id: str, delivery_id: str, master_seed: int = 0, secret: str | None = None
) -> int:
    """Return a platform-independent 64-bit seed for one delivery.

    Without `secret` the seed is a function of public identifiers and the
    master seed - the per-evaluator order of a package without a delivery
    plan. A blinded delivery passes the secret it records in its map.
    """

    if secret is None:
        material = f"qicom-qualitative-v1\0{test_id}\0{delivery_id}\0{master_seed}".encode()
    else:
        material = (
            f"qicom-qualitative-v2\0{test_id}\0{delivery_id}\0{master_seed}\0{secret}".encode()
        )
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


def build_presentation_plan(
    test_id: str,
    delivery_id: str,
    pages: Sequence[_Page],
    design: QualitativeDesign,
    *,
    secret: str | None = None,
) -> PresentationPlan:
    """Create the deterministic evaluator order without mutating author data."""

    design.validate()
    seed = stable_delivery_seed(test_id, delivery_id, design.master_seed, secret)
    page_ids = [page.page_id for page in pages]
    if len(page_ids) != len(set(page_ids)):
        raise ValueError("Page IDs must be unique before randomization.")
    if design.randomize_pages:
        random.Random(f"{seed}:pages").shuffle(page_ids)

    question_order: dict[int, tuple[int, ...]] = {}
    panel_order: dict[int, tuple[str, ...]] = {}
    panel_aliases: dict[int, dict[str, str]] = {}
    for position, page in enumerate(pages):
        questions = [question.question_id for question in page.questions]
        if design.randomize_questions:
            random.Random(f"{seed}:questions:{page.page_id}").shuffle(questions)
        question_order[page.page_id] = tuple(questions)

        roles = [panel.role for panel in page.panels]
        if design.counterbalance_panels and len(roles) > 1:
            shift = (seed + position) % len(roles)
            roles = roles[shift:] + roles[:shift]
            if (seed + position) // max(1, len(roles)) % 2:
                roles.reverse()
        elif design.randomize_panels:
            random.Random(f"{seed}:panels:{page.page_id}").shuffle(roles)
        panel_order[page.page_id] = tuple(roles)
        panel_aliases[page.page_id] = {
            role: _neutral_label(index) for index, role in enumerate(roles)
        }

    repeat_of = _place_hidden_repeats(page_ids, design, seed)
    delivered_order = tuple(
        _inject_repeat_ids(page_ids, repeat_of, design.hidden_repeat_min_distance)
    )
    return PresentationPlan(
        delivery_id=delivery_id,
        seed=seed,
        page_order=delivered_order,
        question_order=question_order,
        panel_order=panel_order,
        panel_aliases=panel_aliases,
        repeat_of=repeat_of,
    )


#: Version 2 maps carry opaque presented IDs (`analysis_page_id` names the
#: page the analysis keeps) and the delivery secret; version 1 maps, whose
#: presented IDs were the author's own, stay readable.
DEBLINDING_MAP_VERSION = 2
SUPPORTED_DEBLINDING_MAP_VERSIONS = frozenset({1, DEBLINDING_MAP_VERSION})


def deblinding_payload(
    test_id: str, plan: PresentationPlan, pages: Sequence[_Page]
) -> dict[str, Any]:
    """Author-only mapping; this payload must never enter an evaluator zip."""

    page_by_id = {page.page_id: page for page in pages}
    return {
        "qicom_deblinding_version": DEBLINDING_MAP_VERSION,
        "test_id": test_id,
        "delivery_id": plan.delivery_id,
        "presentation_seed": plan.seed,
        "pages": [
            {
                "presented_page_id": page_id,
                "source_page_id": plan.repeat_of.get(page_id, page_id),
                "panels": [
                    {
                        "alias": plan.panel_aliases.get(
                            plan.repeat_of.get(page_id, page_id), {}
                        ).get(role, role),
                        "source_role": role,
                    }
                    for role in plan.panel_order.get(plan.repeat_of.get(page_id, page_id), ())
                ],
                "source_exists": plan.repeat_of.get(page_id, page_id) in page_by_id,
            }
            for page_id in plan.page_order
        ],
        "integrity_sha256": hashlib.sha256(
            json.dumps(plan.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _place_hidden_repeats(
    page_ids: list[int], design: QualitativeDesign, seed: int
) -> dict[int, int]:
    if not page_ids:
        return {}
    requested = design.hidden_repeat_count
    if requested == 0 and design.hidden_repeat_percent > 0:
        requested = max(1, round(len(page_ids) * design.hidden_repeat_percent / 100.0))
    requested = min(requested, len(page_ids))
    candidates = list(page_ids)
    random.Random(f"{seed}:repeats").shuffle(candidates)
    next_page_id = max(page_ids) + 1
    return {next_page_id + index: source for index, source in enumerate(candidates[:requested])}


def _inject_repeat_ids(
    page_ids: list[int], repeat_of: dict[int, int], minimum_distance: int
) -> list[int]:
    if repeat_of and minimum_distance >= len(page_ids):
        # Even with all first presentations before all repeats, at least one
        # pair can have no more than n_original - 1 intervening presentations.
        raise ValueError(
            "Hidden-repeat minimum distance requires more original pages than "
            f"the requested {minimum_distance} intervening pages."
        )
    result = list(page_ids)
    for repeat_id, source_id in repeat_of.items():
        source_position = result.index(source_id)
        earliest = min(len(result), source_position + minimum_distance + 1)
        result.insert(earliest, repeat_id)
    if any(
        result.index(repeat_id) - result.index(source_id) - 1 < minimum_distance
        for repeat_id, source_id in repeat_of.items()
    ):
        # A sampled source near the tail may not leave enough intervening
        # pages. Reschedule first presentations, retaining their relative
        # order within each block, then append repeats in source order. This
        # guarantees a gap of n_original - 1 without dropping sampled cases.
        repeat_by_source = {source: repeat for repeat, source in repeat_of.items()}
        sources = [page_id for page_id in page_ids if page_id in repeat_by_source]
        other_pages = [page_id for page_id in page_ids if page_id not in repeat_by_source]
        result = sources + other_pages + [repeat_by_source[source] for source in sources]
    return result


def _neutral_label(index: int) -> str:
    # Excel-like A..Z, AA..AZ, ... keeps labels short but scales beyond 26.
    index += 1
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _integer_tuple(value: object, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ValueError(f"{field_name} must be an array of integers.")
    return tuple(value)


def _integer_tuple_mapping(value: object, field_name: str) -> dict[int, tuple[int, ...]]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return {int(key): _integer_tuple(items, f"{field_name}.{key}") for key, items in value.items()}


def _string_tuple_mapping(value: object, field_name: str) -> dict[int, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    result: dict[int, tuple[str, ...]] = {}
    for key, items in value.items():
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"{field_name}.{key} must be an array of strings.")
        result[int(key)] = tuple(items)
    return result


def _aliases_mapping(value: object) -> dict[int, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("presentation_plan.panel_aliases must be a JSON object.")
    result: dict[int, dict[str, str]] = {}
    for key, aliases in value.items():
        if not isinstance(aliases, dict) or any(
            not isinstance(role, str) or not isinstance(alias, str)
            for role, alias in aliases.items()
        ):
            raise ValueError(f"presentation_plan.panel_aliases.{key} must map strings to strings.")
        result[int(key)] = dict(aliases)
    return result
