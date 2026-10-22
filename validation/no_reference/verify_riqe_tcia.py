"""Check QICOM's RIQE against the released code on the real CT pairs of its paper.

Needs a checkout of https://github.com/Metallogik/RIQE with the public TCIA data
downloaded by its scripts (data/dicom) and the experiment table
experiments/exp5_dose_pairs.csv. Every full-dose/reduced-dose pair of the 20
Siemens test patients is read with QICOM's own DICOM loader and scored by QICOM
(reduced dose on the full-dose masks, as in the paper) and by the reference
scoring API on the same arrays. For GE slices, which carry PixelPaddingValue
-3024 HU, the reference score that reads the tag is compared with QICOM, which
does not. The share of air (<= -900 HU) in the field of view is measured on the
fitting corpus, the pairs and a sample of every full-dose series including its
ends, to justify the threshold below which QICOM refuses a slice as not HU.
Patient data stay in the checkout; only aggregate numbers are written.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from qicom.io.loader import load_image_with_metadata
from qicom.metrics.riqe import AIR_HU, FOV_FLOOR_HU, MIN_AIR_FRACTION, riqe


def _air_fraction(path: Path) -> float:
    hu = load_image_with_metadata(path).pixels
    fov = hu >= FOV_FLOOR_HU
    return float(np.count_nonzero(fov & (hu <= AIR_HU)) / max(np.count_nonzero(fov), 1))


def _air_summary(paths: list[Path]) -> dict[str, float | int]:
    values = np.array([_air_fraction(path) for path in paths])
    return {
        "slices": len(values),
        "min": float(values.min()),
        "p1": float(np.percentile(values, 1)),
        "median": float(np.median(values)),
        "below_threshold": int((values < MIN_AIR_FRACTION).sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("riqe_checkout", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ge-slices", type=int, default=20)
    args = parser.parse_args()
    root = args.riqe_checkout
    sys.path.insert(0, str(root))
    from riqe.dicomio import read_hu
    from riqe.extract import Spec, features_from_hu, masks_for
    from riqe.model import RCOND, mahalanobis_mixed

    with np.load(root / "artifacts" / "riqe-v1.0.npz", allow_pickle=False) as model:
        mean, covariance = model["nu"], model["sigma"]
    spec = Spec(P=24, C=0.01, p=0.5)

    def reference(hu: np.ndarray, padding: float | None, masks=None) -> float:
        features = features_from_hu(hu, padding, spec, fitting=False, masks=masks)
        selected = features.feat[features.valid]
        covariance_image = np.cov(selected, rowvar=False)
        return mahalanobis_mixed(mean, covariance, selected.mean(axis=0), covariance_image, RCOND)

    table = root / "experiments" / "exp5_dose_pairs.csv"
    with table.open(encoding="utf-8", newline="") as handle:
        pairs = [row for row in csv.DictReader(handle) if row["model"] == "RIQE (CT)"]
    identical = 0
    published_deviation = 0.0
    ranked = {"chest": [0, 0], "abdomen": [0, 0]}
    for row in pairs:
        full = load_image_with_metadata(root / row["path_full"]).pixels
        low = load_image_with_metadata(root / row["path_low"]).pixels
        ours = (riqe(full, "HU"), riqe(low, "HU", domain_image=full, domain_unit="HU"))
        masks = masks_for(full, None)
        theirs = (reference(full, None, masks), reference(low, None, masks))
        identical += ours == theirs
        published = (float(row["score_full"]), float(row["score_low"]))
        published_deviation = max(
            published_deviation, *(abs(a - b) / b for a, b in zip(ours, published, strict=True))
        )
        ranked[row["region"]][0] += int(ours[1] > ours[0])
        ranked[row["region"]][1] += 1

    ge = []
    for series in sorted((root / "data" / "dicom").glob("*/full")):
        files = sorted(series.glob("*.dcm"))
        path = files[len(files) // 2]
        _, padding, meta = read_hu(path, with_pixels=False)
        if meta["manufacturer"].startswith("GE") and padding is not None:
            ge.append(path)
        if len(ge) == args.ge_slices:
            break
    ge_deviation = 0.0
    paddings = set()
    for path in ge:
        hu, padding, _ = read_hu(path)
        paddings.add(padding)
        ours_ge = riqe(load_image_with_metadata(path).pixels, "HU")
        ge_deviation = max(ge_deviation, abs(ours_ge - reference(hu, padding)))

    artifact = json.loads((root / "artifacts" / "riqe-v1.0.json").read_text(encoding="utf-8"))
    series_sample: list[Path] = []
    for series in sorted((root / "data" / "dicom").glob("*/full")):
        files = sorted(series.glob("*.dcm"))
        series_sample += files[:3] + files[-3:] + files[::40]
    air = {
        "threshold": {"air_hu": AIR_HU, "min_fraction_of_fov": MIN_AIR_FRACTION},
        "fitting_corpus": _air_summary([root / s["path"] for s in artifact["corpus_slices_used"]]),
        "test_pairs_full_dose": _air_summary([root / row["path_full"] for row in pairs]),
        "test_pairs_reduced_dose": _air_summary([root / row["path_low"] for row in pairs]),
        "series_ends_and_every_40th_slice": _air_summary(series_sample),
    }

    result = {
        "reference_code": "Metallogik/RIQE riqe-v1.0 scoring API",
        "pair_table_sha256": hashlib.sha256(table.read_bytes()).hexdigest(),
        "dose_pairs": len(pairs),
        "dose_pairs_bit_identical_to_reference": identical,
        "max_relative_deviation_from_published_scores": published_deviation,
        "published_scores_note": "the paper pipeline caches features in float32",
        "reduced_dose_ranked_worse": {
            region: {"correct": correct, "pairs": total}
            for region, (correct, total) in ranked.items()
        },
        "ge_slices_with_padding": len(ge),
        "ge_padding_values_hu": sorted(paddings),
        "ge_max_absolute_difference_vs_reference_reading_padding": ge_deviation,
        "air_fraction_of_field_of_view": air,
        "numpy": np.__version__,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
