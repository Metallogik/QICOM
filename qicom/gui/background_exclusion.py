"""The "exclude the air around the patient" choice, as one small value.

Sits between the Background panel (which collects the choice) and
AppController (which turns it into the mask run_all_metrics is given), so
neither has to know the other: the panel never touches image data, and the
controller never reads a widget.

Only the numbers live here. *Where* the air is sampled is not a setting at
all - it is the union of the green circles on the canvas (see
DualImageView.add_air_roi_sample), together with whatever the user has
painted back into or out of the mask by hand. Both are canvas state the
controller reads directly.

The choice is deliberately not a process-wide singleton, unlike
qicom.gui.metric_selection: an air level or an intensity threshold only
means anything for one particular pair of images, so it travels with the
call (calculate/calculate_all_pairs) and is saved into the analysis'
config_json alongside the data range, rather than quietly persisting
across a project switch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from qicom.metrics.background import (
    DEFAULT_TOLERANCE_FACTOR,
    METHOD_MANUAL,
    BackgroundEstimate,
    build_tissue_mask,
    sample_air_level,
)
from qicom.metrics.types import ImageArray


@dataclass(frozen=True)
class BackgroundExclusionSettings:
    """What the user asked for, not what was measured - see
    qicom.metrics.background.BackgroundEstimate for the result.

    `tolerance`, when set, is an exact intensity band above the sampled air
    level - what the user typed into the threshold box - and overrides
    `tolerance_factor`, which otherwise scales the noise spread measured
    inside the air sample itself.
    """

    enabled: bool = False
    tolerance_factor: float = DEFAULT_TOLERANCE_FACTOR
    tolerance: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tolerance_factor": self.tolerance_factor,
            "tolerance": self.tolerance,
        }

    @classmethod
    def from_dict(cls, data: Any) -> BackgroundExclusionSettings:
        """Rebuild from a saved analysis' config_json - tolerant of
        anything at all (a project saved before this setting existed, one
        saved when it still carried a detection method and a ROI id, a
        hand-edited file), returning the plain "not excluded" default
        rather than raising, the same way every other restored config key
        treats absent/odd values."""
        if not isinstance(data, dict):
            return cls()
        tolerance = data.get("tolerance")
        factor = data.get("tolerance_factor")
        return cls(
            enabled=bool(data.get("enabled", False)),
            tolerance_factor=(
                float(factor) if isinstance(factor, (int, float)) else DEFAULT_TOLERANCE_FACTOR
            ),
            tolerance=float(tolerance) if isinstance(tolerance, (int, float)) else None,
        )


@dataclass(frozen=True, slots=True)
class BackgroundExclusionSnapshot:
    """Immutable, Qt-free inputs for one background-detection job."""

    cache_key: tuple[object, ...]
    reference_image: ImageArray
    sample_image: ImageArray
    sample_mask: ImageArray
    corrections: ImageArray | None
    settings: BackgroundExclusionSettings


def compute_background_exclusion(
    snapshot: BackgroundExclusionSnapshot,
    *,
    air_sampler: Any = sample_air_level,
    mask_builder: Any = build_tissue_mask,
) -> tuple[ImageArray, BackgroundEstimate]:
    """Detect and correct a tissue mask without touching GUI objects."""
    air_level, air_spread = air_sampler(snapshot.sample_image, snapshot.sample_mask)
    mask, estimate = mask_builder(
        snapshot.reference_image,
        method=METHOD_MANUAL,
        air_level=air_level,
        air_spread=air_spread,
        tolerance=snapshot.settings.tolerance,
        tolerance_factor=snapshot.settings.tolerance_factor,
    )
    corrections = snapshot.corrections
    if corrections is not None and corrections.shape == mask.shape and corrections.any():
        corrected = mask.copy()
        corrected[corrections > 0] = True
        corrected[corrections < 0] = False
        mask = corrected
        excluded = 1.0 - float(np.count_nonzero(mask)) / float(mask.size)
        estimate = replace(estimate, excluded_fraction=excluded)
    mask.setflags(write=False)
    return mask, estimate
