"""A label map built one sampled region at a time, every step undoable.

The Utility Tools segmenter works the way the user describes a scan: "this
is air, take it away; this is bone, keep it". Each step samples the pixels
under a ROI, turns them into an intensity range, selects every pixel in that
range - anywhere, or only where it connects to the ROI - and assigns the
selection to a named label. Removing what is not wanted and keeping only what
is wanted are then the same operation seen from two sides: the view shows
the unassigned remainder, or only the chosen labels.

One label per pixel, 0 meaning unassigned, at most 255 labels, so the map is
one byte per voxel. A step only takes pixels that are still unassigned unless
the user asks it to overwrite, so the order of steps resolves overlapping
intensity classes the way the user chose rather than by accident.

The method separates intensity classes, not organs: liver, spleen and muscle
share a CT range, and MR has no absolute scale at all. The presets' CT ranges
are starting points to adjust against the histogram, never thresholds QICOM
vouches for.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

import numpy as np

from qicom.i18n import translator
from qicom.segmentation.primitives import (
    close_mask,
    fill_mask_holes,
    keep_components_touching,
    median_smoothed,
    open_mask,
    remove_small_components,
)

UNASSIGNED = 0
MAX_LABELS = 255
MAX_LABEL_NAME = 64
RANGE_PERCENTILE = "percentile"
RANGE_SIGMA = "sigma"
# Undo keeps every step until their stored changes reach this many bytes;
# the oldest go first. A changed-voxel set is stored as indices when sparse
# and as a packed bit mask when dense, so one step never costs more than
# one bit per voxel plus one byte per changed voxel.
HISTORY_BYTE_BUDGET = 256 * 1024 * 1024

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
PALETTE = (
    "#4cc9f0",
    "#f72585",
    "#ffd166",
    "#06d6a0",
    "#ef476f",
    "#8338ec",
    "#fb8500",
    "#3a86ff",
    "#80ed99",
    "#ff006e",
)


@dataclass(frozen=True)
class LabelPreset:
    """A starting point for a label: its name, colour and, for CT in HU,
    an indicative intensity range the user is expected to adjust."""

    preset_id: str
    name_key: str
    color: str
    ct_range: tuple[float, float] | None = None


PRESETS: tuple[LabelPreset, ...] = (
    LabelPreset("air", "segmentation.preset.air", "#4cc9f0", (-3100.0, -900.0)),
    LabelPreset("lung", "segmentation.preset.lung", "#80ed99", (-900.0, -500.0)),
    LabelPreset("fat", "segmentation.preset.fat", "#ffd166", (-190.0, -30.0)),
    LabelPreset("soft_tissue", "segmentation.preset.soft_tissue", "#ef476f", (-29.0, 150.0)),
    LabelPreset("contrast", "segmentation.preset.contrast", "#ff006e", (150.0, 400.0)),
    LabelPreset("bone", "segmentation.preset.bone", "#f4a261", (400.0, 3000.0)),
    LabelPreset("metal", "segmentation.preset.metal", "#adb5bd", (3000.0, 31000.0)),
    LabelPreset("table", "segmentation.preset.table", "#8338ec", None),
)


def preset(preset_id: str) -> LabelPreset:
    for item in PRESETS:
        if item.preset_id == preset_id:
            return item
    raise KeyError(preset_id)


@dataclass(frozen=True)
class Label:
    value: int
    name: str
    color: str
    preset: str = ""


@dataclass(frozen=True)
class IntensityRange:
    low: float
    high: float


def range_from_samples(
    samples: np.ndarray,
    *,
    method: str = RANGE_PERCENTILE,
    lower_percentile: float = 2.0,
    upper_percentile: float = 98.0,
    sigmas: float = 2.0,
) -> IntensityRange:
    """The intensity range a ROI's pixels describe.

    Percentiles by default: a ROI drawn in air routinely clips the edge of
    the body, and the extremes that brings in should not stretch the range.
    Mean +/- k standard deviations suits a homogeneous region better.
    """
    values = np.asarray(samples, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError(translator.tr("errors.segmentation.empty_sample"))
    if method == RANGE_SIGMA:
        mean = float(values.mean())
        spread = float(values.std()) * float(sigmas)
        return IntensityRange(mean - spread, mean + spread)
    if method != RANGE_PERCENTILE:
        raise ValueError(f"Unknown range method {method!r}")
    if not 0.0 <= lower_percentile < upper_percentile <= 100.0:
        raise ValueError(translator.tr("errors.segmentation.percentiles"))
    low, high = np.percentile(values, [lower_percentile, upper_percentile])
    return IntensityRange(float(low), float(high))


@dataclass(frozen=True)
class SelectionRequest:
    """Which pixels one step proposes.

    `slice_index` limits a volume to one slice (None: the whole image);
    `seeds`, when given, keeps only the regions that touch it - the ROI the
    range was sampled from, in the full image's shape.
    """

    low: float
    high: float
    slice_index: int | None = None
    seeds: np.ndarray | None = field(default=None, compare=False)
    smoothing: int = 0
    fill_holes: bool = False
    minimum_voxels: int = 0
    opening: int = 0
    closing: int = 0
    fully_connected: bool = False


def select_pixels(
    image: np.ndarray,
    request: SelectionRequest,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """A boolean mask, in `image`'s shape, of what `request` selects.

    `progress_callback(done, total)` counts the stages the request asks for:
    the range itself, then each refinement.
    """
    array = np.asarray(image)
    if array.ndim not in (2, 3):
        raise ValueError("Segmentation requires a 2D image or a 3D volume.")
    low, high = sorted((float(request.low), float(request.high)))
    if not np.isfinite((low, high)).all():
        raise ValueError(translator.tr("errors.segmentation.range_not_finite"))
    if request.slice_index is not None and array.ndim == 3:
        index = int(request.slice_index)
        if not 0 <= index < array.shape[0]:
            raise ValueError(f"Slice {index} is outside a volume of {array.shape[0]} slices.")
        region = (slice(index, index + 1),)
    else:
        region = (slice(None),)
    stages = 1 + sum(
        (
            request.opening > 0,
            request.closing > 0,
            bool(request.fill_holes),
            request.seeds is not None,
            request.minimum_voxels > 1,
        )
    )
    done = 0

    def advance() -> None:
        nonlocal done
        done += 1
        if progress_callback is not None:
            progress_callback(done, stages)

    if progress_callback is not None:
        progress_callback(0, stages)
    values = median_smoothed(array[region], request.smoothing)
    selected = (values >= low) & (values <= high)
    advance()
    # Refinements stay within each slice, like the smoothing: a slice is
    # usually much thicker than a pixel is wide.
    if request.opening > 0:
        selected = open_mask(selected, request.opening, in_plane=True)
        advance()
    if request.closing > 0:
        selected = close_mask(selected, request.closing, in_plane=True)
        advance()
    if request.fill_holes:
        selected = _fill_per_plane(selected)
        advance()
    if request.seeds is not None:
        seeds = np.asarray(request.seeds, dtype=bool)
        if seeds.shape != array.shape:
            raise ValueError("The ROI mask must have the image's shape.")
        selected = keep_components_touching(
            selected, seeds[region], fully_connected=request.fully_connected
        )
        advance()
    if request.minimum_voxels > 1:
        selected = remove_small_components(
            selected, request.minimum_voxels, fully_connected=request.fully_connected
        )
        advance()
    result = np.zeros(array.shape, dtype=bool)
    result[region] = selected
    return result


def _fill_per_plane(mask: np.ndarray) -> np.ndarray:
    """Fill holes slice by slice: a volume's cavity "enclosed" in 3D is rare,
    one enclosed within each slice (a vessel in the liver) is the usual case."""
    if mask.ndim == 2:
        return fill_mask_holes(mask)
    return np.stack([fill_mask_holes(plane) for plane in mask])


@dataclass(frozen=True)
class StepSummary:
    """What one step did, for the history list and the saved provenance."""

    kind: str  # "assign" | "erase" | "remove_label" | "fill_unassigned"
    label: int
    label_name: str
    changed: int
    parameters: dict[str, object] = field(default_factory=dict, compare=False)


@dataclass
class _Step:
    summary: StepSummary
    changed: np.ndarray  # flat indices (int64) or packed bits (uint8)
    packed: bool
    previous: np.ndarray  # uint8 values before the step, at the changed voxels
    new_value: int
    removed_label: Label | None = None

    @property
    def nbytes(self) -> int:
        return int(self.changed.nbytes + self.previous.nbytes)


class LabelMapEditor:
    """The label map, its label table and the undo/redo history."""

    def __init__(
        self,
        shape: tuple[int, ...],
        *,
        labels: Iterable[Label] = (),
        data: np.ndarray | None = None,
        history_byte_budget: int = HISTORY_BYTE_BUDGET,
    ) -> None:
        self._shape = tuple(int(axis) for axis in shape)
        if data is None:
            self._data = np.zeros(self._shape, dtype=np.uint8)
        else:
            array = np.asarray(data)
            if array.shape != self._shape:
                raise ValueError("The label map does not have the image's shape.")
            self._data = array.astype(np.uint8, copy=True)
        self._labels: dict[int, Label] = {}
        for label in labels:
            self._validate_label(label)
            if label.value in self._labels:
                raise ValueError(f"Label {label.value} is defined twice.")
            self._labels[label.value] = label
        unknown = set(np.unique(self._data).tolist()) - set(self._labels) - {UNASSIGNED}
        if unknown:
            raise ValueError(
                translator.tr(
                    "errors.segmentation.undefined_values",
                    values=", ".join(str(value) for value in sorted(unknown)),
                )
            )
        self._budget = history_byte_budget
        self._undo: list[_Step] = []
        self._redo: list[_Step] = []

    # -- the label table ------------------------------------------------------

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    @property
    def data(self) -> np.ndarray:
        """The map itself; read-only, so every change goes through a step."""
        view = self._data.view()
        view.flags.writeable = False
        return view

    @property
    def labels(self) -> tuple[Label, ...]:
        return tuple(self._labels[value] for value in sorted(self._labels))

    def label(self, value: int) -> Label:
        return self._labels[value]

    def add_label(self, name: str, color: str | None = None, *, preset_id: str = "") -> Label:
        free = [value for value in range(1, MAX_LABELS + 1) if value not in self._labels]
        if not free:
            raise ValueError(translator.tr("errors.segmentation.too_many_labels", count=MAX_LABELS))
        value = free[0]
        chosen = color or PALETTE[(value - 1) % len(PALETTE)]
        label = Label(value=value, name=_clean_name(name), color=chosen, preset=preset_id)
        self._validate_label(label)
        self._labels[value] = label
        return label

    def rename_label(self, value: int, name: str) -> Label:
        label = replace(self._labels[value], name=_clean_name(name))
        self._validate_label(label)
        self._labels[value] = label
        return label

    def recolor_label(self, value: int, color: str) -> Label:
        label = replace(self._labels[value], color=color)
        self._validate_label(label)
        self._labels[value] = label
        return label

    def remove_label(self, value: int) -> StepSummary:
        """Drop a label and unassign its pixels, as one undoable step."""
        label = self._labels[value]
        changed = self._data == value
        step = self._apply(
            changed,
            UNASSIGNED,
            StepSummary("remove_label", value, label.name, int(changed.sum())),
            removed_label=label,
        )
        del self._labels[value]
        return step

    @staticmethod
    def _validate_label(label: Label) -> None:
        if isinstance(label.value, bool) or not 1 <= int(label.value) <= MAX_LABELS:
            raise ValueError(f"A label value must be between 1 and {MAX_LABELS}.")
        if not label.name.strip():
            raise ValueError(translator.tr("errors.segmentation.empty_name"))
        if len(label.name) > MAX_LABEL_NAME:
            raise ValueError(translator.tr("errors.segmentation.long_name", count=MAX_LABEL_NAME))
        if not _COLOR.match(label.color):
            raise ValueError(f"{label.color!r} is not a #rrggbb colour.")

    # -- steps ------------------------------------------------------------------

    def assign(
        self,
        selection: np.ndarray,
        value: int,
        *,
        overwrite: bool = False,
        parameters: dict[str, object] | None = None,
    ) -> StepSummary:
        """Give `value` to the selected pixels - only the unassigned ones
        unless `overwrite` - and return what the step changed."""
        label = self._labels[value]
        selected = self._selection(selection)
        eligible = selected if overwrite else selected & (self._data == UNASSIGNED)
        eligible &= self._data != value
        summary = StepSummary("assign", value, label.name, int(eligible.sum()), parameters or {})
        return self._apply(eligible, value, summary)

    def erase(
        self, selection: np.ndarray, value: int, *, parameters: dict[str, object] | None = None
    ) -> StepSummary:
        """Unassign the selected pixels of `value`: a correction, not a class."""
        label = self._labels[value]
        changed = self._selection(selection) & (self._data == value)
        summary = StepSummary("erase", value, label.name, int(changed.sum()), parameters or {})
        return self._apply(changed, UNASSIGNED, summary)

    def fill_unassigned(self, value: int) -> StepSummary:
        """Everything still unassigned goes to `value` - "the rest is tissue"."""
        label = self._labels[value]
        changed = self._data == UNASSIGNED
        summary = StepSummary("fill_unassigned", value, label.name, int(changed.sum()))
        return self._apply(changed, value, summary)

    def _selection(self, selection: np.ndarray) -> np.ndarray:
        mask = np.asarray(selection, dtype=bool)
        if mask.shape != self._shape:
            raise ValueError("The selection does not have the label map's shape.")
        return mask

    def _apply(
        self,
        changed: np.ndarray,
        new_value: int,
        summary: StepSummary,
        *,
        removed_label: Label | None = None,
    ) -> StepSummary:
        flat = changed.ravel()
        indices = np.flatnonzero(flat)
        if indices.size == 0 and removed_label is None:
            # Nothing changed: no history entry, and the redo stack survives.
            return summary
        previous = self._data.ravel()[indices].copy()
        # Indices cost 8 bytes per changed voxel, a bit mask one bit per voxel.
        packed = indices.size * 8 > flat.size // 8
        stored = np.packbits(flat) if packed else indices
        self._data.ravel()[indices] = new_value
        self._undo.append(
            _Step(summary, stored, packed, previous, new_value, removed_label=removed_label)
        )
        self._redo.clear()
        self._trim_history()
        return summary

    def _indices(self, step: _Step) -> np.ndarray:
        if not step.packed:
            return step.changed
        bits = np.unpackbits(step.changed, count=int(np.prod(self._shape)))
        return np.flatnonzero(bits)

    def _trim_history(self) -> None:
        while len(self._undo) > 1 and sum(step.nbytes for step in self._undo) > self._budget:
            self._undo.pop(0)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def history(self) -> tuple[StepSummary, ...]:
        """The steps that can still be undone, oldest first."""
        return tuple(step.summary for step in self._undo)

    def undo(self) -> StepSummary | None:
        if not self._undo:
            return None
        step = self._undo.pop()
        self._data.ravel()[self._indices(step)] = step.previous
        if step.removed_label is not None:
            self._labels[step.removed_label.value] = step.removed_label
        self._redo.append(step)
        return step.summary

    def redo(self) -> StepSummary | None:
        if not self._redo:
            return None
        step = self._redo.pop()
        self._data.ravel()[self._indices(step)] = step.new_value
        if step.removed_label is not None:
            self._labels.pop(step.removed_label.value, None)
        self._undo.append(step)
        return step.summary

    # -- reading the map ----------------------------------------------------------

    def counts(self) -> dict[int, int]:
        """Voxels per value, unassigned (0) included, defined labels only."""
        totals = np.bincount(self._data.ravel(), minlength=MAX_LABELS + 1)
        return {value: int(totals[value]) for value in (UNASSIGNED, *sorted(self._labels.keys()))}

    def mask_of(self, values: Iterable[int]) -> np.ndarray:
        """Where the map holds any of `values` (0 standing for unassigned)."""
        chosen = np.asarray(sorted(set(values)), dtype=np.uint8)
        return np.isin(self._data, chosen)


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())


def masked_image(image: np.ndarray, keep: np.ndarray, fill_value: float) -> np.ndarray:
    """`image` where `keep`, `fill_value` elsewhere, as float32."""
    array = np.asarray(image, dtype=np.float32)
    mask = np.asarray(keep, dtype=bool)
    if mask.shape != array.shape:
        raise ValueError("The mask does not have the image's shape.")
    return np.where(mask, array, np.float32(fill_value)).astype(np.float32)


__all__ = [
    "HISTORY_BYTE_BUDGET",
    "MAX_LABELS",
    "MAX_LABEL_NAME",
    "PALETTE",
    "PRESETS",
    "RANGE_PERCENTILE",
    "RANGE_SIGMA",
    "UNASSIGNED",
    "IntensityRange",
    "Label",
    "LabelMapEditor",
    "LabelPreset",
    "SelectionRequest",
    "StepSummary",
    "masked_image",
    "preset",
    "range_from_samples",
    "select_pixels",
]
