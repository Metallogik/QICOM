"""Keep the optional external NIQE oracle outside QICOM and its dependencies.

Run with the pinned BasicSR NIQE and resize source files as command arguments.
Only its grayscale numerical entry point is executed; the optional OpenCV color
wrapper and package registration are not needed. No external implementation is
copied into this repository. Inputs are QICOM-authored synthetic test fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import sys
import types
from pathlib import Path

import numpy as np
from scipy.io import loadmat

PINNED_SOURCES = {
    "niqe.py": "d8052aa4617d2c0f7712f1871078d0244209481aa00cfce8a9ce1998f83a0088",
    "matlab_functions.py": "29a3a3d209ce15724202bfb01415e5d4e574e7b853090551a7938c7b78ec4975",
    "modelparameters.mat": "fa2aa4d12dd9595c3fb6bf8b4e1a15c79d5d1469238d0901c88dead195be578d",
}


def external_function(niqe_path: Path, resize_path: Path):
    for name in (
        "cv2",
        "basicsr",
        "basicsr.metrics",
        "basicsr.utils",
        "basicsr.metrics.metric_util",
        "basicsr.utils.registry",
        "basicsr.utils.matlab_functions",
    ):
        sys.modules[name] = types.ModuleType(name)
    util = sys.modules["basicsr.metrics.metric_util"]
    util.reorder_image = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unused color path"))
    util.to_y_channel = util.reorder_image
    sys.modules["basicsr.utils.registry"].METRIC_REGISTRY = types.SimpleNamespace(
        register=lambda: lambda function: function
    )
    resize = runpy.run_path(str(resize_path))["imresize"]
    sys.modules["basicsr.utils.matlab_functions"].imresize = resize
    return runpy.run_path(str(niqe_path))["niqe"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("niqe_source", type=Path)
    parser.add_argument("resize_source", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    sources = {
        label: hashlib.sha256(path.read_bytes()).hexdigest()
        for label, path in (
            ("niqe.py", args.niqe_source),
            ("matlab_functions.py", args.resize_source),
            ("modelparameters.mat", args.model),
            ("inputs.npz", args.inputs),
        )
    }
    for label, expected_digest in PINNED_SOURCES.items():
        if sources[label] != expected_digest:
            raise ValueError(f"The supplied {label} does not match the pinned oracle source.")
    oracle = external_function(args.niqe_source, args.resize_source)
    model = loadmat(args.model)
    coordinates = np.arange(-3, 4, dtype=np.float64)
    window = np.exp(-((coordinates[:, None] ** 2 + coordinates[None, :] ** 2) / (2 * (7 / 6) ** 2)))
    window /= window.sum()
    scores = {}
    with np.load(args.inputs, allow_pickle=False) as data:
        for name in data.files:
            scores[name] = float(
                oracle(
                    data[name].astype(np.float64),
                    model["mu_prisparam"],
                    model["cov_prisparam"],
                    window,
                )
            )
    result = {
        "oracle": "BasicSR grayscale niqe; float64 input, upstream float32 resize",
        "revision": "8d56e3a045f9fb3e1d8872f92ee4a4f07f886b0a",
        "sources": sources,
        "numpy": np.__version__,
        "scores": scores,
        "tolerance": {"atol": 0.0001, "rtol": 0.00001},
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
