"""Synthetic CT-like HU slices give the RIQE comparison inputs without patient data.

Each slice is an elliptical body with soft tissue, lung-like and bone-like inserts
and correlated noise, stored as integer HU like a rescaled CT slice. The arrays are
QICOM-authored; only their numbers matter, not their realism. Run once to write
tests/fixtures/riqe/inputs.npz; the committed file, not this generator, is what the
tests and the reference comparison read. Two more inputs, a GE-style -3024 HU
padding outside a circular field of view and an odd-sized crop, are exact
derivations of the full-dose slice (``derived_inputs``).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

SIZE = 320
SEED = 20260930


def _noise(rng: np.random.Generator, sigma_hu: float) -> np.ndarray:
    # Mild spatial correlation, as a reconstruction kernel would leave.
    field = gaussian_filter(rng.standard_normal((SIZE, SIZE)), 0.8)
    return field * (sigma_hu / field.std())


def _phantom() -> np.ndarray:
    rows, columns = np.mgrid[:SIZE, :SIZE].astype(np.float64)
    centre = (SIZE - 1) / 2

    def inside(dy: float, dx: float, radius_y: float, radius_x: float) -> np.ndarray:
        return ((rows - centre - dy) / radius_y) ** 2 + (
            (columns - centre - dx) / radius_x
        ) ** 2 < 1

    hu = np.full((SIZE, SIZE), -1000.0)
    hu[inside(0, 0, 145, 152)] = 45.0
    hu[inside(-35, -60, 45, 38)] = -840.0
    hu[inside(-35, 60, 45, 38)] = -820.0
    hu[inside(70, 0, 22, 22)] = 650.0
    hu[inside(20, 0, 30, 55)] = 120.0
    hu[inside(-100, 90, 10, 10)] = 25.0
    return gaussian_filter(hu, 1.0)


def make_inputs() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)
    phantom = _phantom()
    full = np.rint(phantom + _noise(rng, 12.0))
    low = np.rint(phantom + _noise(rng, 30.0))
    blurred = np.rint(gaussian_filter(full, 1.5))
    return {
        "full": full.astype(np.int16),
        "low": low.astype(np.int16),
        "blurred": blurred.astype(np.int16),
    }


def derived_inputs(full: np.ndarray) -> dict[str, np.ndarray]:
    """Exact integer derivations, recomputed rather than stored to keep the fixture small."""
    size = full.shape[0]
    rows, columns = np.mgrid[:size, : full.shape[1]]
    padded = full.copy()
    padded[np.hypot(rows - (size - 1) / 2, columns - (size - 1) / 2) > size / 2 - 2] = -3024
    # 309x317: odd, and one of the sizes on which the reference code is defined.
    return {"padded": padded, "odd_size": full[3:312, 1:318].copy()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    np.savez_compressed(args.output, **make_inputs())


if __name__ == "__main__":
    main()
