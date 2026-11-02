"""Image.fromarray must not use the `mode=` parameter Pillow 13 no longer honours.

Pillow 11.3 deprecated passing `mode` to reinterpret an array's data type, and
Pillow 13 (2026-10-15) refuses it. QICOM requires `Pillow>=13.0`: the 12.x
line carries the PYSEC-2026-43xx advisories, so the parameter was removed from
every call site before the bound was raised, not after.

The array's dtype and shape determine the mode correctly for every image QICOM
writes, so the parameter was redundant everywhere it appeared. These tests pin
that equivalence - the same bytes, not merely the same mode - for the four
modes QICOM produces, against a construction that states the mode explicitly
(`Image.frombytes`), and that no call site reintroduces the parameter.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = Path(__file__).parents[2]

_CASES = {
    "L": np.arange(256, dtype=np.uint8).reshape(16, 16),
    "I;16": (np.arange(256, dtype=np.uint16) * 257).reshape(16, 16),
    "RGB": np.arange(16 * 16 * 3, dtype=np.uint8).reshape(16, 16, 3),
    "RGBA": np.arange(16 * 16 * 4, dtype=np.uint8).reshape(16, 16, 4),
}


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestModeIsInferred:
    @pytest.mark.parametrize("mode", sorted(_CASES))
    def test_the_array_alone_yields_the_same_mode(self, mode: str) -> None:
        assert Image.fromarray(_CASES[mode]).mode == mode

    @pytest.mark.parametrize("mode", sorted(_CASES))
    def test_the_encoded_file_is_byte_identical_either_way(self, mode: str) -> None:
        array = _CASES[mode]
        # I;16 is little-endian whatever the host: state the byte order.
        raw = array.astype("<u2").tobytes() if mode == "I;16" else array.tobytes()
        size = (array.shape[1], array.shape[0])
        explicit = _png_bytes(Image.frombytes(mode, size, raw))

        inferred = _png_bytes(Image.fromarray(array))

        assert hashlib.sha256(inferred).hexdigest() == hashlib.sha256(explicit).hexdigest()

    @pytest.mark.parametrize("mode", sorted(_CASES))
    def test_the_pixels_survive_a_round_trip(self, mode: str, tmp_path: Path) -> None:
        array = _CASES[mode]
        target = tmp_path / f"{mode.replace(';', '_')}.png"

        Image.fromarray(array).save(target)

        with Image.open(target) as reopened:
            assert np.array_equal(np.asarray(reopened), array)

    def test_inferring_the_mode_emits_no_deprecation_warning(self) -> None:
        """The one that was already firing on every test run: uint16 with an
        explicit "I;16"."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            for array in _CASES.values():
                Image.fromarray(array)

    def test_the_explicit_form_fails_on_the_required_pillow(self) -> None:
        """Documents why this matters rather than assuming it: the call that
        used to warn on every test run is an error on Pillow 13."""
        with pytest.raises(ValueError):
            Image.fromarray(_CASES["I;16"], mode="I;16")


class TestNoCallSiteUsesTheParameter:
    def test_no_source_or_test_file_passes_mode_to_fromarray(self) -> None:
        offenders = []
        for path in sorted(REPO_ROOT.glob("**/*.py")):
            if any(
                part in {".venv", "build", "dist", ".git", ".mypy_cache", ".ruff_cache"}
                for part in path.parts
            ):
                continue
            if path.resolve() == Path(__file__).resolve():
                continue  # this file uses the deprecated form on purpose, above
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if "fromarray(" in line and 'mode="' in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")

        assert offenders == []
