"""Image.fromarray must not use the deprecated `mode=` parameter.

Pillow deprecated passing `mode` to reinterpret an array's data type, and
removes it in Pillow 13 (2026-10-15). QICOM's own pin is `Pillow>=12.3,<13`,
so the calls still work - but every one of them is code that stops working the
moment that bound is raised, and one of them was already emitting a warning on
every test run.

The array's dtype and shape determine the mode correctly for every image QICOM
writes, so the parameter was redundant everywhere it appeared. These tests pin
that equivalence - the same bytes, not merely the same mode - for the four
modes QICOM produces, and that no call site reintroduces the parameter.
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
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            explicit = _png_bytes(Image.fromarray(array, mode=mode))

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

    def test_the_explicit_form_is_what_pillow_13_removes(self) -> None:
        """Documents why this matters rather than assuming it: raising the
        Pillow bound past 13 has to be preceded by this change, not follow it."""
        with pytest.warns(DeprecationWarning, match="Pillow 13"):
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
