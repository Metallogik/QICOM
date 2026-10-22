"""Tests for the DICOM decompressor tool."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from dicom_fixtures import write_ct_series
from pydicom.uid import RLELossless
from pytestqt.qtbot import QtBot

from qicom.gui.guidance import is_next
from qicom.gui.tools import dicom_decompressor as decompressor_module
from qicom.gui.tools.dicom_decompressor import DicomDecompressorTool
from qicom.i18n import translator


def _rle_multiframe(path: Path, frames: int = 3) -> Path:
    import pydicom

    source = write_ct_series(path.parent / "raw", slices=1)[0]
    dataset = pydicom.dcmread(source)
    first = dataset.pixel_array
    pixels = np.stack([first + index for index in range(frames)]).astype(first.dtype)
    dataset.NumberOfFrames = frames
    dataset.PixelData = pixels.tobytes()
    dataset.compress(RLELossless, generate_instance_uid=False)
    dataset.save_as(path, enforce_file_format=True)
    return path


def _opened(qtbot: QtBot, path: Path) -> DicomDecompressorTool:
    tool = DicomDecompressorTool()
    qtbot.addWidget(tool)
    tool.open_file(path)
    qtbot.waitUntil(lambda: tool.info is not None, timeout=10_000)
    return tool


def test_the_header_is_shown_before_anything_is_written(qtbot: QtBot, tmp_path: Path) -> None:
    source = _rle_multiframe(tmp_path / "volume.dcm")
    tool = _opened(qtbot, source)

    assert tool._file_label.text() == "volume.dcm"
    assert "RLE Lossless" in tool._summary.text()
    assert translator.tr("tools.decompressor.will_write_many", count=3) in tool._summary.text()
    assert tool._decompress_button.isEnabled() and is_next(tool._decompress_button)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["raw", "volume.dcm"]


def test_decompressing_writes_the_folder_and_says_where(qtbot: QtBot, tmp_path: Path) -> None:
    source = _rle_multiframe(tmp_path / "volume.dcm")
    tool = _opened(qtbot, source)

    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool.decompress_into(tmp_path)

    report = tool.last_report
    assert report is not None and report.folder == tmp_path / "volume"
    assert len(report.files) == 3 and all(path.is_file() for path in report.files)
    assert "volume" in tool._result.text()
    assert not is_next(tool._decompress_button)


def test_an_uncompressed_single_frame_offers_nothing_to_do(qtbot: QtBot, tmp_path: Path) -> None:
    tool = _opened(qtbot, write_ct_series(tmp_path / "in", slices=1)[0])

    assert not tool._decompress_button.isEnabled()
    assert translator.tr("tools.decompressor.nothing_to_do") in tool._summary.text()
    assert is_next(tool._file_button)


def test_failures_are_reported(qtbot: QtBot, tmp_path: Path) -> None:
    tool = _opened(qtbot, _rle_multiframe(tmp_path / "volume.dcm"))

    with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
        tool.decompress_into(tmp_path / "missing")
    (tmp_path / "notes.dcm").write_text("not DICOM")
    with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
        tool.open_file(tmp_path / "notes.dcm")


def test_dialog_entry_points(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _rle_multiframe(tmp_path / "volume.dcm")
    (tmp_path / "out").mkdir()
    starts: list[str] = []
    monkeypatch.setattr(
        decompressor_module.QFileDialog,
        "getOpenFileName",
        lambda *_a, **_k: (str(source), ""),
    )

    def choose_folder(_parent: object, _title: str, start: str) -> str:
        starts.append(start)
        return str(tmp_path / "out")

    monkeypatch.setattr(decompressor_module.QFileDialog, "getExistingDirectory", choose_folder)
    tool = DicomDecompressorTool()
    qtbot.addWidget(tool)

    tool._file_button.click()
    qtbot.waitUntil(lambda: tool.info is not None, timeout=10_000)
    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool._decompress_button.click()

    assert starts == [str(tmp_path)]  # the dialog opens where the source is
    assert (tmp_path / "out" / "volume" / "volume_0001.dcm").is_file()
