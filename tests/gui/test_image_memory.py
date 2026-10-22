"""Memory decisions stay on the GUI thread while image decoders wait for an answer."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from qicom import runtime
from qicom.gui import image_memory, workers
from qicom.gui import radiomics_main_window as radiomics_window_module
from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.io.loader import load_image_with_metadata
from qicom.io.memory_preflight import ImageAllocationWarning, ImageLoadCancelled
from qicom.radiomics.io import load_radiomics_image


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "image.nii.gz"
    nib.save(
        nib.Nifti1Image(np.arange(8 * 8 * 8, dtype=np.float32).reshape(8, 8, 8), np.eye(4)), path
    )
    return path


def install_answer(monkeypatch, approve, asked):
    gui_thread = threading.get_ident()

    def answer(*args):
        assert threading.get_ident() == gui_thread
        assert args[-1] == QMessageBox.StandardButton.Cancel
        asked.append(args[2])
        return QMessageBox.StandardButton.Yes if approve else QMessageBox.StandardButton.Cancel

    # Static QMessageBox stubs belong only to the module presenting this dialog.
    monkeypatch.setattr(
        image_memory,
        "QMessageBox",
        SimpleNamespace(question=answer, StandardButton=QMessageBox.StandardButton),
    )
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)


@pytest.mark.parametrize("approve", [False, True])
def test_panel_waits_for_gui_confirmation_and_preserves_previous_image_on_cancel(
    qtbot, monkeypatch, image_path, approve
):
    panel = ImagePanel("reference", "panelHeaderReference")
    qtbot.addWidget(panel)
    previous = np.full((8, 8), 42.0)
    panel.set_image(previous)
    asked = []
    errors = []
    panel.load_failed.connect(errors.append)
    install_answer(monkeypatch, approve, asked)
    original = nib.arrayproxy.ArrayProxy.__array__
    decoded = []

    def decode(self, *args, **kwargs):
        assert asked, "Pixel decoding ran before the user saw the warning"
        decoded.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(nib.arrayproxy.ArrayProxy, "__array__", decode)
    panel.load_from_path(image_path)
    qtbot.waitUntil(lambda: panel._load_job is None, timeout=5000)
    assert len(asked) == 1
    assert not errors
    assert bool(decoded) is approve
    if approve:
        np.testing.assert_array_equal(
            panel.image, np.arange(512, dtype=np.float32).reshape(8, 8, 8)
        )
    else:
        np.testing.assert_array_equal(panel.image, previous)


@pytest.mark.parametrize("approve", [False, True])
def test_radiomics_window_receives_the_same_preallocation_decision(
    qtbot, monkeypatch, image_path, approve
):
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    asked = []
    errors = []
    install_answer(monkeypatch, approve, asked)
    monkeypatch.setattr(window, "_show_error", errors.append)
    window._start_image_load("reference", image_path)
    qtbot.waitUntil(lambda: window._job is None, timeout=5000)
    assert len(asked) == 1
    assert not errors
    assert (window.controller.reference is not None) is approve
    window.controller.dirty = False


def test_synchronous_worker_bridge_preserves_a_declined_load(qtbot, monkeypatch, image_path):
    asked = []
    install_answer(monkeypatch, False, asked)
    with pytest.raises(ImageLoadCancelled):
        workers.run_in_thread_and_wait(load_image_with_metadata, image_path)
    assert len(asked) == 1


def test_labelmap_is_not_attached_if_images_change_while_the_worker_reads(
    qtbot, monkeypatch, image_path
):
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    previous = load_radiomics_image(image_path)
    replacement = load_radiomics_image(image_path)
    window.controller.reference = previous
    window.controller.comparison = previous
    errors = []
    asked = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    monkeypatch.setattr(
        radiomics_window_module,
        "QFileDialog",
        SimpleNamespace(getOpenFileName=lambda *args: (str(image_path), "")),
    )
    install_answer(monkeypatch, True, asked)
    QTimer.singleShot(0, lambda: setattr(window.controller, "reference", replacement))
    window._import_labelmap()
    # One decision for reading the labelmap, one for the masks it expands into.
    assert len(asked) == 2
    assert window.controller.reference is replacement
    assert not window.controller.rois
    assert not errors
    assert window.statusBar().currentMessage() == translator.tr("memory.load.cancelled")


@pytest.mark.parametrize("new_stack", [False, True])
def test_declining_replacement_preserves_source_identity(qtbot, monkeypatch, tmp_path, new_stack):
    from PIL import Image

    panel = ImagePanel("reference", "panelHeaderReference")
    qtbot.addWidget(panel)
    old_paths = [tmp_path / f"old-{index}.png" for index in range(2)]
    new_paths = [tmp_path / f"new-{index}.png" for index in range(2)]
    for index, path in enumerate([*old_paths, *new_paths]):
        Image.fromarray(np.full((8, 8), index + 1, np.uint8)).save(path)
    panel.load_from_paths(old_paths)
    qtbot.waitUntil(lambda: panel._load_job is None, timeout=5000)
    previous = panel.image.copy()
    metadata = panel.loaded_image
    old_path = panel.path
    assert panel.source_files == old_paths
    install_answer(monkeypatch, False, [])
    if new_stack:
        panel.load_from_paths(new_paths)
    else:
        panel.load_from_path(new_paths[0])
    qtbot.waitUntil(lambda: panel._load_job is None, timeout=5000)
    assert panel.source_files == old_paths
    assert panel.path == old_path
    assert panel.loaded_image is metadata
    np.testing.assert_array_equal(panel.image, previous)


def test_pending_question_can_be_cancelled_without_an_answer(qtbot, monkeypatch, image_path):
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    requests = []
    monkeypatch.setattr(
        workers._FunctionThread,
        "_answer_memory_request",
        lambda self, request: requests.append(request),
    )
    cancelled = []
    errors = []
    successes = []
    job = workers.run_in_thread(
        load_image_with_metadata,
        image_path,
        on_success=successes.append,
        on_error=errors.append,
        on_cancel=lambda: cancelled.append(True),
    )
    qtbot.waitUntil(lambda: bool(requests), timeout=3000)
    job.cancel_pending_allocation()
    qtbot.waitUntil(lambda: bool(cancelled), timeout=3000)
    assert requests[0].answered.is_set()
    assert not errors and not successes


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_memory_warning_is_translated_and_contains_the_estimate(qtbot, monkeypatch, language):
    previous_language = translator.language
    asked = []
    install_answer(monkeypatch, False, asked)
    try:
        translator.set_language(language)
        assert not image_memory.confirm_image_memory(
            ImageAllocationWarning("NIfTI", (512, 512, 512), 1024**3, 2 * 1024**3)
        )
        assert "512 × 512 × 512" in asked[0]
        assert "1.0 GiB" in asked[0] and "2.0 GiB" in asked[0]
        assert "memory.load.message" not in asked[0]
    finally:
        translator.set_language(previous_language)
