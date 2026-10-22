"""Tests for the Utility Tools: the catalog, the Tools window and the DICOM header tools."""

from __future__ import annotations

import csv
import itertools
import json
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from message_box_stubs import stub_message_box
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget
from pytestqt.qtbot import QtBot

from qicom.gui.tools import dicom_tags, tools_window
from qicom.gui.tools.catalog import TOOLS, ToolSpec, tool_spec
from qicom.gui.tools.dicom_editor import DicomTagEditor
from qicom.gui.tools.dicom_tags import DicomHeaderComparison, DicomHeaderSource
from qicom.gui.tools.tools_window import ToolsMainWindow
from qicom.i18n import SUPPORTED_LANGUAGES, translator

_LOCALES = Path(__file__).parents[2] / "qicom" / "gui" / "i18n" / "locales"
_UIDS = itertools.count(1)


def _write(
    path: Path,
    *,
    kernel: str = "B30f",
    instance: int = 1,
    series_uid: str | None = None,
    patient: str = "Doe^John",
) -> Path:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    # A fixed length: the file meta group length must not differ between two
    # files only because one random UID came out a digit shorter.
    file_meta.MediaStorageSOPInstanceUID = f"1.2.826.0.1.3680043.8.498.{10**19 + next(_UIDS)}"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.PatientName = patient
    dataset.Modality = "CT"
    dataset.ConvolutionKernel = kernel
    dataset.SeriesInstanceUID = series_uid or generate_uid()
    dataset.SeriesNumber = 3
    dataset.SeriesDescription = "Abdomen"
    dataset.InstanceNumber = instance
    item = Dataset()
    item.CodeValue = "C1"
    dataset.ProcedureCodeSequence = Sequence([item])
    dataset.add_new(0x00090010, "LO", "ACME")
    dataset.add_new(0x00091010, "LO", "private text")
    dataset.Rows = dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelData = np.zeros((2, 2), np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)
    return path


def _texts(tree: QTreeWidget, column: int = 0) -> list[str]:
    found: list[str] = []

    def visit(item: QTreeWidgetItem) -> None:
        found.append(item.text(column))
        for index in range(item.childCount()):
            visit(item.child(index))

    for index in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(index)
        assert top is not None
        visit(top)
    return found


@pytest.fixture
def english() -> Iterator[None]:
    previous = translator.language
    translator.set_language("en")
    yield
    translator.set_language(previous)


class TestCatalog:
    def test_every_tool_is_named_and_described_in_every_language(self) -> None:
        for language in SUPPORTED_LANGUAGES:
            strings = json.loads((_LOCALES / f"{language}.json").read_text(encoding="utf-8"))
            for spec in TOOLS:
                assert strings.get(spec.title_key), (language, spec.title_key)
                assert strings.get(spec.description_key), (language, spec.description_key)

    def test_tool_ids_are_unique(self) -> None:
        assert len({spec.tool_id for spec in TOOLS}) == len(TOOLS)

    @pytest.mark.parametrize("spec", TOOLS, ids=lambda spec: spec.tool_id)
    def test_every_entry_builds_its_widget(self, spec: ToolSpec, qtbot: QtBot) -> None:
        widget = spec.create_widget()
        qtbot.addWidget(widget)

        assert isinstance(widget, QWidget)

    def test_an_entry_that_is_not_a_widget_is_refused(self) -> None:
        spec = ToolSpec("x", "t", "d", "tag", "builtins:object")

        with pytest.raises(TypeError, match="is not a QWidget"):
            spec.create_widget()

    def test_an_unknown_tool_is_a_key_error(self) -> None:
        with pytest.raises(KeyError):
            tool_spec("not_a_tool")


class TestToolsWindow:
    def test_opens_on_the_requested_tool(self, qtbot: QtBot) -> None:
        window = ToolsMainWindow(initial_tool="dicom_compare")
        qtbot.addWidget(window)

        assert window.current_tool == "dicom_compare"
        assert isinstance(window.tool_widget("dicom_compare"), DicomHeaderComparison)
        assert window.tool_widget("dicom_editor") is None  # built only once picked

    def test_opens_on_the_first_tool_by_default(self, qtbot: QtBot) -> None:
        window = ToolsMainWindow()
        qtbot.addWidget(window)

        assert window.current_tool == TOOLS[0].tool_id

    def test_switching_tools_keeps_what_each_one_has_open(self, qtbot: QtBot) -> None:
        window = ToolsMainWindow(initial_tool="dicom_editor")
        qtbot.addWidget(window)
        inspector = window.tool_widget("dicom_editor")

        window.open_tool("dicom_compare")
        window.open_tool("dicom_editor")

        assert window.tool_widget("dicom_editor") is inspector
        assert window._tool_title.text() == translator.tr("tools.editor.title")

    def test_the_catalog_lists_every_tool(self, qtbot: QtBot, english: None) -> None:
        window = ToolsMainWindow()
        qtbot.addWidget(window)

        titles = [window._catalog.item(row).text() for row in range(window._catalog.count())]

        assert titles == [translator.tr(spec.title_key) for spec in TOOLS]

    def test_close_returns_to_the_launcher(self, qtbot: QtBot) -> None:
        window = ToolsMainWindow()
        qtbot.addWidget(window)
        window.show()

        window._close_tools()

        assert window.return_to_launcher

    def test_close_waits_for_a_running_read(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window = ToolsMainWindow()
        qtbot.addWidget(window)
        window.show()
        monkeypatch.setattr(tools_window, "has_active_jobs", lambda: True)

        window._close_tools()

        assert window.isVisible()
        assert not window.return_to_launcher

    def test_a_tool_error_is_shown_and_kept_for_the_bug_report(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shown: list[str] = []
        stub_message_box(
            monkeypatch, tools_window, warning=lambda _parent, _title, text: shown.append(text)
        )
        window = ToolsMainWindow(initial_tool="dicom_editor")
        qtbot.addWidget(window)
        page = window.tool_widget("dicom_editor")
        assert isinstance(page, DicomTagEditor)

        page.error_occurred.emit("it failed")

        assert shown == ["it failed"]
        assert [record.message for record in window.error_records()] == ["it failed"]

    def test_a_tool_status_message_reaches_the_status_bar(self, qtbot: QtBot) -> None:
        window = ToolsMainWindow(initial_tool="dicom_editor")
        qtbot.addWidget(window)
        page = window.tool_widget("dicom_editor")
        assert isinstance(page, DicomTagEditor)

        page.status_message.emit("Saved x.csv.")

        assert window.statusBar().currentMessage() == "Saved x.csv."

    def test_a_language_change_retitles_the_window(self, qtbot: QtBot, english: None) -> None:
        window = ToolsMainWindow()
        qtbot.addWidget(window)

        translator.set_language("it")

        assert window.windowTitle() == "QICOM - Strumenti di utilità"
        row = [spec.tool_id for spec in TOOLS].index("dicom_editor")
        assert window._catalog.item(row).text() == "Ispeziona e modifica tag DICOM"


class TestHeaderSource:
    def test_a_folder_offers_its_series_and_steps_through_instances(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        series = generate_uid()
        _write(tmp_path / "a.dcm", instance=2, series_uid=series, kernel="second")
        _write(tmp_path / "b.dcm", instance=1, series_uid=series, kernel="first")
        source = DicomHeaderSource()
        qtbot.addWidget(source)

        source.open_folder(tmp_path)
        qtbot.waitUntil(lambda: source.header is not None)

        assert source._series_combo.count() == 1
        assert "2" in source._series_combo.itemText(0)
        assert source.header.source.name == "b.dcm"
        source._instance_spin.setValue(2)
        qtbot.waitUntil(lambda: source.header is not None and source.header.source.name == "a.dcm")

    def test_a_newer_request_wins_over_an_older_one(self, qtbot: QtBot, tmp_path: Path) -> None:
        first = _write(tmp_path / "a.dcm")
        second = _write(tmp_path / "b.dcm")
        source = DicomHeaderSource()
        qtbot.addWidget(source)
        received: list[object] = []
        source.header_changed.connect(received.append)

        source.open_file(first)
        source.open_file(second)
        qtbot.waitUntil(lambda: source.header is not None)
        qtbot.wait(50)

        assert [header.source.name for header in received] == ["b.dcm"]  # type: ignore[attr-defined]

    def test_an_unreadable_file_is_reported(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("x")
        source = DicomHeaderSource()
        qtbot.addWidget(source)

        with qtbot.waitSignal(source.error_occurred) as blocker:
            source.open_file(path)

        assert "notes.txt" in blocker.args[0]
        assert source.header is None

    def test_a_failed_folder_leaves_no_stale_series(self, qtbot: QtBot, tmp_path: Path) -> None:
        good = tmp_path / "good"
        good.mkdir()
        _write(good / "a.dcm")
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "README").write_text("x")
        source = DicomHeaderSource()
        qtbot.addWidget(source)
        source.open_folder(good)
        qtbot.waitUntil(lambda: source.header is not None)

        with qtbot.waitSignal(source.error_occurred):
            source.open_folder(empty)

        assert source.header is None
        assert source._series_row.isHidden()
        assert source._source_label.text() == translator.tr("tools.dicom.no_source")

    def test_a_damaged_file_in_a_series_keeps_the_series_open(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        series = generate_uid()
        _write(tmp_path / "a.dcm", instance=1, series_uid=series)
        damaged = _write(tmp_path / "b.dcm", instance=2, series_uid=series)
        source = DicomHeaderSource()
        qtbot.addWidget(source)
        source.open_folder(tmp_path)
        qtbot.waitUntil(lambda: source.header is not None)
        damaged.write_bytes(b"not dicom any more")

        with qtbot.waitSignal(source.error_occurred):
            source._instance_spin.setValue(2)

        assert source.header is None
        assert not source._series_row.isHidden()
        assert source._source_label.text() == "b.dcm"


class TestHeaderComparison:
    def _loaded(self, qtbot: QtBot, tmp_path: Path) -> DicomHeaderComparison:
        series = generate_uid()
        comparison = DicomHeaderComparison()
        qtbot.addWidget(comparison)
        comparison.reference_source.open_file(
            _write(tmp_path / "a.dcm", kernel="B30f", series_uid=series)
        )
        comparison.comparison_source.open_file(
            _write(tmp_path / "b.dcm", kernel="B70f", series_uid=series)
        )
        qtbot.waitUntil(lambda: bool(comparison._differences))
        return comparison

    def test_waits_for_both_headers(self, qtbot: QtBot, english: None) -> None:
        comparison = DicomHeaderComparison()
        qtbot.addWidget(comparison)

        assert comparison._summary.text() == translator.tr("tools.compare.waiting")
        assert not comparison._export_button.isEnabled()

    def test_shows_both_whole_headers_by_default(self, qtbot: QtBot, tmp_path: Path) -> None:
        comparison = self._loaded(qtbot, tmp_path)

        assert not comparison._only_differences.isChecked()
        assert not comparison._hide_uids.isChecked()
        everything = _texts(comparison._tree)
        assert "(0010,0010)" in everything and "(0002,0003)" in everything

    def test_the_filters_narrow_the_view_to_what_differs(
        self, qtbot: QtBot, tmp_path: Path, english: None
    ) -> None:
        comparison = self._loaded(qtbot, tmp_path)
        everything = _texts(comparison._tree)

        comparison._only_differences.setChecked(True)
        with_uids = _texts(comparison._tree)
        comparison._hide_uids.setChecked(True)

        assert "(0002,0003)" in with_uids and len(with_uids) < len(everything)
        assert _texts(comparison._tree) == ["(0018,1210)"]
        assert _texts(comparison._tree, 4) == ["Different"]
        assert comparison._summary.text().startswith("1 different,")

    def test_export_writes_the_shown_differences(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comparison = self._loaded(qtbot, tmp_path)
        comparison._only_differences.setChecked(True)
        comparison._hide_uids.setChecked(True)
        target = tmp_path / "diff.csv"
        monkeypatch.setattr(
            dicom_tags.QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(target), "")
        )

        comparison._export_button.click()

        with target.open(encoding="utf-8", newline="") as stream:
            table = list(csv.reader(stream))
        assert table[1][2:] == [
            "ConvolutionKernel",
            "Convolution Kernel",
            "SH",
            "different",
            "B30f",
            "B70f",
        ]

    def test_losing_one_header_clears_the_comparison(
        self, qtbot: QtBot, tmp_path: Path, english: None
    ) -> None:
        comparison = self._loaded(qtbot, tmp_path)
        broken = tmp_path / "notes.txt"
        broken.write_text("x")

        with qtbot.waitSignal(comparison.error_occurred):
            comparison.comparison_source.open_file(broken)

        assert comparison._tree.topLevelItemCount() == 0
        assert comparison._summary.text() == translator.tr("tools.compare.waiting")

    def test_a_theme_change_redraws_the_statuses(self, qtbot: QtBot, tmp_path: Path) -> None:
        comparison = self._loaded(qtbot, tmp_path)
        comparison._only_differences.setChecked(True)
        comparison._hide_uids.setChecked(True)
        before = comparison._tree.topLevelItem(0)

        comparison._on_theme_changed("dark")

        assert comparison._tree.topLevelItem(0) is not before
        assert _texts(comparison._tree) == ["(0018,1210)"]
