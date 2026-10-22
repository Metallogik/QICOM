"""Workbench-level tests for the radiomics ROI/results state model.

Covers the P0 review findings that involve the window and the viewer
together: a mask must be extracted from the slice it was drawn on, the
comparison panel must never present a ROI it is not actually measuring,
edits must mark the project dirty, and a reopened project must give the
manual ROIs back as editable objects.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import SimpleITK as sitk
from message_box_stubs import stub_message_box
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.widgets.radiomics_panels import DataFrameTable
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.project import load_radiomics_project

_SHAPE = (6, 32, 32)


@pytest.fixture(autouse=True)
def _stub_static_message_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Static QMessageBox calls block forever headlessly (they never route
    through the QMessageBox.exec stub in tests/gui/conftest.py)."""
    stub_message_box(
        monkeypatch,
        "qicom.gui.radiomics_main_window",
        question=lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )
    stub_message_box(
        monkeypatch,
        "qicom.gui.radiomics_main_window",
        critical=lambda *_a, **_k: QMessageBox.StandardButton.Ok,
    )


def _image(
    *, shift: float = 0.0, origin: tuple[float, float, float] | None = None
) -> RadiomicsImage:
    pixels = np.linspace(0.0, 1.0, int(np.prod(_SHAPE)), dtype=np.float32).reshape(_SHAPE) + shift
    geometry = ImageGeometry.unit(pixels.shape)
    if origin is not None:
        geometry = ImageGeometry(
            size=geometry.size,
            spacing=geometry.spacing,
            origin=origin,
            direction=geometry.direction,
        )
    return RadiomicsImage(pixels=pixels, geometry=geometry, source_format="array")


def _window(qtbot: QtBot, *, comparison: RadiomicsImage | None = None) -> RadiomicsMainWindow:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    window._on_image_loaded("reference", Path("reference.nii.gz"), _image())
    window._on_image_loaded(
        "comparison",
        Path("comparison.nii.gz"),
        _image(shift=0.25) if comparison is None else comparison,
    )
    return window


def test_a_manual_roi_is_extracted_on_the_slice_it_was_drawn_on(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)

    # The user scrolls elsewhere before pressing Extract; the ROI itself is
    # hidden on this slice, so extracting it here measures nothing visible.
    window.dual_image_view.reference_panel.set_current_index(4)
    rois = window._materialize_manual_rois()

    assert len(rois) == 1
    occupied = {int(index) for index in np.unique(np.argwhere(rois[0].reference_mask)[:, 0])}
    assert occupied == {0}


def test_moving_a_roi_marks_the_project_dirty_and_the_results_stale(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)
    window.controller.features = pd.DataFrame({"roi_id": ["manual-1"], "value": [1.0]})
    window.controller.mark_results_current()
    window.controller.dirty = False

    # A drag, as pyqtgraph reports one: started, moved, finished.
    pair = window.dual_image_view.list_roi_pairs()[0]
    pair.reference_roi.sigRegionChangeStarted.emit(pair.reference_roi)
    pair.reference_roi.setPos((1.0, 1.0))
    pair.reference_roi.sigRegionChangeFinished.emit(pair.reference_roi)

    assert window.controller.dirty
    assert window.controller.results_stale


def test_removing_a_live_roi_marks_the_project_dirty(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)
    window.controller.dirty = False

    window._remove_roi("manual-1")

    assert window.controller.dirty
    assert window.dual_image_view.list_roi_pairs() == []


def test_export_is_blocked_while_the_results_are_stale(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)
    window.controller.features = pd.DataFrame({"roi_id": ["manual-1"], "value": [1.0]})
    window.controller.mark_results_current()
    window._refresh_results_state()
    assert window.results_panel._export_excel.isEnabled()

    window._remove_roi("manual-1")

    assert window.controller.results_stale
    assert not window.results_panel._export_excel.isEnabled()
    assert not window.results_panel._export_csv.isEnabled()
    window.workflow_rail.set_current_stage("results")
    assert window.results_panel._stale_banner.isVisibleTo(window.results_panel)


def test_comparison_panel_rois_cannot_be_dragged(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)

    pair = window.dual_image_view.list_roi_pairs()[0]
    assert pair.reference_roi.translatable
    assert not pair.comparison_roi.translatable
    assert not pair.comparison_roi.resizable


def test_comparison_outlines_are_hidden_when_the_geometries_differ(qtbot: QtBot) -> None:
    window = _window(qtbot, comparison=_image(shift=0.25, origin=(10.0, 0.0, 0.0)))
    window._add_manual_roi("rectangle", False)

    pair = window.dual_image_view.list_roi_pairs()[0]
    assert pair.reference_roi.isVisible()
    # An index-mirrored outline would sit 10 mm away from the mask that is
    # actually measured - show the transferred mask instead of a lie.
    assert not pair.comparison_roi.isVisible()


def test_aligned_geometries_keep_the_comparison_outline_visible(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)

    pair = window.dual_image_view.list_roi_pairs()[0]
    assert pair.comparison_roi.isVisible()


def test_reopening_a_project_restores_editable_manual_rois(qtbot: QtBot, tmp_path: Path) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)
    window._synchronize_all_rois()
    saved = window.controller.save_project(tmp_path / "project.zip")

    reopened = RadiomicsMainWindow()
    qtbot.addWidget(reopened)
    reopened._on_project_loaded(saved, load_radiomics_project(saved))

    pairs = reopened.dual_image_view.list_roi_pairs()
    assert [pair.roi_id for pair in pairs] == [1]
    assert pairs[0].shape_type == "rectangle"
    assert pairs[0].reference_roi.translatable
    # Re-materialising the restored item must reproduce the saved mask,
    # otherwise reopening a project would silently invalidate its results.
    assert not reopened.controller.results_stale


def test_replacing_a_loaded_image_requires_confirmation(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot)
    window._add_manual_roi("rectangle", False)
    stub_message_box(
        monkeypatch,
        "qicom.gui.radiomics_main_window",
        question=lambda *_a, **_k: QMessageBox.StandardButton.Cancel,
    )

    window._start_image_load("comparison", Path("other.nii.gz"))

    assert window._job is None
    assert len(window.dual_image_view.list_roi_pairs()) == 1


def test_numeric_columns_sort_numerically(qtbot: QtBot) -> None:
    table = DataFrameTable()
    qtbot.addWidget(table)
    table.set_dataframe(pd.DataFrame({"feature": ["a", "b", "c"], "value": [2.0, 10.0, 1.0]}))

    table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    ordered = [table.model().index(row, 1).data() for row in range(3)]

    assert [float(value) for value in ordered] == [1.0, 2.0, 10.0]


def test_gui_imports_multilabel_nifti_and_exports_selected_mask(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot)
    labels = np.zeros(_SHAPE, dtype=np.uint16)
    labels[1:3, 4:9, 5:10] = 1
    labels[3:5, 12:18, 15:21] = 2
    labelmap_path = tmp_path / "labels.nii.gz"
    labelmap = sitk.GetImageFromArray(labels)
    labelmap.SetSpacing(window.controller.reference.geometry.spacing)
    labelmap.SetOrigin(window.controller.reference.geometry.origin)
    labelmap.SetDirection(window.controller.reference.geometry.direction)
    sitk.WriteImage(labelmap, str(labelmap_path))
    monkeypatch.setattr(
        "qicom.gui.radiomics_main_window.QFileDialog.getOpenFileName",
        lambda *_args: (str(labelmap_path), "NIfTI"),
    )

    window._import_labelmap()

    assert len(window.controller.rois) == 2
    assert {roi.parameters["label"] for roi in window.controller.rois} == {1, 2}
    assert all(roi.method == "nifti-labelmap" for roi in window.controller.rois)

    output_path = tmp_path / "exported-mask.nii.gz"
    window.segmentation_panel._roi_table.selectRow(0)
    monkeypatch.setattr(
        "qicom.gui.radiomics_main_window.QFileDialog.getSaveFileName",
        lambda *_args: (str(output_path), "NIfTI"),
    )
    window._export_selected_mask("original")

    assert output_path.is_file()
    assert (tmp_path / "exported-mask.nii.gz.json").is_file()
