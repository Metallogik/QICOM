"""Score the RIQE fixtures with the released reference code, kept outside QICOM.

Run with a checkout of https://github.com/Metallogik/RIQE whose scoring sources
match the hashes recorded in the riqe-v1.0 artifact. The reference package is
imported from that checkout; QICOM's implementation is not imported. Each input
is scored on its own masks (the reference single-image API) and, where the pair
protocol applies, on the masks of the full-dose image, as in the published
reduced-dose evaluation. Inputs are QICOM-authored synthetic HU slices, plus
two exact derivations defined in make_riqe_inputs.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

PINNED_SOURCES = {
    "riqe/hu.py": "ba1ff78f31d4afbdff6cdc08fac2d0c56d47ddc2043d92aae5e20d7a0caf6706",
    "riqe/nss.py": "4ea83a96ad3b09a61a3f4ca1f642723776e9bb6afa8673b21290ed1eb41e1d31",
    "riqe/extract.py": "05c889a5d7f65b634a8a85db59e6acc59b9b85797d9ba6579a3f4844231fb4ac",
    "riqe/model.py": "e1312f9093b7830f38e96d36ac744111585e5e13da1f0177e47a81cd9b1dccec",
    "riqe-v1.0.npz": "b1a4ffdf8288c7c6667fd3b0ca674756c69e5f1c85e9b5a53d596bf19b32f595",
}
#: Inputs scored on the masks of another input, as (input, mask source).
PAIRED = (("low", "full"), ("blurred", "full"))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("riqe_checkout", type=Path)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    model_path = args.riqe_checkout / "artifacts" / "riqe-v1.0.npz"
    sources = {
        label: _digest(model_path if label.endswith(".npz") else args.riqe_checkout / label)
        for label in PINNED_SOURCES
    }
    for label, expected in PINNED_SOURCES.items():
        if sources[label] != expected:
            raise ValueError(f"The supplied {label} does not match the released riqe-v1.0 code.")
    sources["inputs.npz"] = _digest(args.inputs)

    sys.path.insert(0, str(args.riqe_checkout))
    from riqe.extract import Spec, features_from_hu, masks_for
    from riqe.model import RCOND, mahalanobis_mixed

    with np.load(model_path, allow_pickle=False) as model:
        mean, covariance = model["nu"], model["sigma"]
    spec = Spec(P=24, C=0.01, p=0.5)

    def score(hu: np.ndarray, masks: tuple[np.ndarray, np.ndarray] | None) -> float:
        # read_hu returns float32 HU; the fixtures store the same integers.
        features = features_from_hu(hu, None, spec, fitting=False, masks=masks)
        selected = features.feat[features.valid]
        covariance_image = np.cov(selected, rowvar=False)
        return mahalanobis_mixed(mean, covariance, selected.mean(axis=0), covariance_image, RCOND)

    sys.path.insert(0, str(Path(__file__).parent))
    from make_riqe_inputs import derived_inputs

    with np.load(args.inputs, allow_pickle=False) as data:
        stored = {name: data[name] for name in data.files}
    derived = derived_inputs(stored["full"])
    sources.update(
        {
            f"{name} (derived)": hashlib.sha256(array.tobytes()).hexdigest()
            for name, array in derived.items()
        }
    )
    slices = {name: array.astype(np.float32) for name, array in {**stored, **derived}.items()}
    own = {name: score(hu, None) for name, hu in slices.items()}
    paired = {
        f"{name}@{source}": score(slices[name], masks_for(slices[source], None))
        for name, source in PAIRED
    }
    result = {
        "reference": "Metallogik/RIQE riqe-v1.0 scoring API (features_from_hu, mahalanobis_mixed)",
        "revision": "73a1d420524cc80821d2b092416f650e78ad77fe",
        "sources": sources,
        "numpy": np.__version__,
        "own_masks": own,
        "reference_masks": paired,
        "tolerance": {"atol": 0.0, "rtol": 1e-9},
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
