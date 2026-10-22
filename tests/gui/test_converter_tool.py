"""Tests for the converter tool."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pytestqt.qtbot import QtBot

from qicom.gui.guidance import is_next
from qicom.gui.tools import converter as converter_module
from qicom.gui.tools.converter import ConverterTool
from qicom.gui.tools.image_source import ToolImage
from qicom.io.conversion import FORMAT_NIFTI, FORMAT_PNG, FORMAT_TIFF
from qicom.io.geometry import unknown_geometry
from qicom.io.models import make_loaded_image


def _image(name: str = "scan.nii.gz") -> ToolImage:
    pixels = np.arange(2 * 3 * 4, dtype=np.float32).reshape(2, 3, 4)
    loaded = make_loaded_image(
        pixels,
        data_range=24.0,
        source_format="nifti",
        source_dtype="float32",
        source_paths=(),
        source="x",
    )
    return ToolImage(loaded, unknown_geometry(pixels.shape), name)


def _tool(qtbot: QtBot) -> ConverterTool:
    tool = ConverterTool()
    qtbot.addWidget(tool)
    tool.set_image(_image())
    return tool


@pytest.mark.parametrize(
    ("image_format", "folder", "files"),
    [
        (FORMAT_NIFTI, "scan_nifti", ["scan.nii.gz"]),
        (FORMAT_PNG, "scan_png8", ["scan_0001.png", "scan_0002.png"]),
        (FORMAT_TIFF, "scan_tiff", ["scan_0001.tiff", "scan_0002.tiff"]),
    ],
)
def test_each_format_lands_in_a_folder_of_its_own(
    qtbot: QtBot, tmp_path: Path, image_format: str, folder: str, files: list[str]
) -> None:
    (tmp_path / "already_here.txt").write_text("x")
    tool = _tool(qtbot)
    tool.set_format(image_format)
    assert "geometry" in tool._hint.text() or image_format != FORMAT_NIFTI

    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool.convert_into(tmp_path)

    assert tool.last_folder == tmp_path / folder
    assert sorted(path.name for path in (tmp_path / folder).iterdir()) == files
    # The chosen folder keeps only what it had, plus the new folder.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["already_here.txt", folder]


def test_a_second_conversion_gets_a_numbered_folder(qtbot: QtBot, tmp_path: Path) -> None:
    tool = _tool(qtbot)
    for _round in range(2):
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.convert_into(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["scan_nifti", "scan_nifti_2"]


def test_png_slices_can_be_16_bit(qtbot: QtBot, tmp_path: Path) -> None:
    tool = _tool(qtbot)
    tool.set_format(FORMAT_PNG)
    assert tool._bit_depth.isVisibleTo(tool)
    tool.set_bit_depth(16)
    assert "65,536" in tool._hint.text()

    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool.convert_into(tmp_path)

    image = Image.open(tmp_path / "scan_png16" / "scan_0002.png")
    assert image.mode == "I;16"
    assert np.asarray(image).max() > 255


def test_the_bit_depth_is_only_offered_for_png(qtbot: QtBot) -> None:
    tool = _tool(qtbot)
    for image_format, shown in ((FORMAT_NIFTI, False), (FORMAT_PNG, True), (FORMAT_TIFF, False)):
        tool.set_format(image_format)
        assert tool._bit_depth.isVisibleTo(tool) is shown


def test_a_failure_is_reported(qtbot: QtBot, tmp_path: Path) -> None:
    tool = _tool(qtbot)
    with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
        tool.convert_into(tmp_path / "missing")
    assert tool._convert_button.isEnabled()


def test_the_dialog_picks_where_the_folder_is_created(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool(qtbot)
    monkeypatch.setattr(
        converter_module.QFileDialog, "getExistingDirectory", lambda *_a, **_k: str(tmp_path)
    )
    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool._convert_button.click()
    assert (tmp_path / "scan_nifti" / "scan.nii.gz").is_file()


def test_the_guidance_moves_from_opening_to_converting(qtbot: QtBot) -> None:
    tool = ConverterTool()
    qtbot.addWidget(tool)
    assert all(is_next(button) for button in tool.source.open_buttons)
    assert not is_next(tool._convert_button)

    tool.set_image(_image())

    assert is_next(tool._convert_button)
    assert not any(is_next(button) for button in tool.source.open_buttons)


def test_converting_the_same_thing_again_is_not_the_next_step(qtbot: QtBot, tmp_path: Path) -> None:
    tool = _tool(qtbot)
    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool.convert_into(tmp_path)
    assert not is_next(tool._convert_button)

    tool.set_format(FORMAT_TIFF)
    assert is_next(tool._convert_button)
