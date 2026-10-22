"""The local NSS model dialog: input checks and fitting off the GUI thread."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
from message_box_stubs import stub_message_box
from PIL import Image
from pytestqt.qtbot import QtBot

from qicom.gui.error_reporting import explained_error
from qicom.gui.widgets import nss_model_dialog as module
from qicom.gui.widgets.nss_model_dialog import NssModelDialog
from qicom.i18n import translator


@pytest.fixture
def warnings_shown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []
    stub_message_box(monkeypatch, module, warning=lambda _parent, _title, text: shown.append(text))
    return shown


@pytest.mark.parametrize("text", ["0", "-3", "abc", "nan", "inf"])
def test_an_invalid_data_range_is_reported_in_the_users_language(
    qtbot: QtBot, warnings_shown: list[str], text: str
) -> None:
    dialog = NssModelDialog()
    qtbot.addWidget(dialog)
    dialog._data_range.setText(text)

    dialog._fit_and_save()

    assert warnings_shown == [translator.tr("nss_model.invalid_data_range")]


def _corpus(tmp_path: Path, count: int = 2) -> list[Path]:
    paths = []
    for index in range(count):
        path = tmp_path / f"qa{index}.png"
        Image.fromarray(
            np.random.default_rng(index).integers(0, 255, (128, 128)).astype(np.uint8)
        ).save(path)
        paths.append(path)
    return paths


def test_the_corpus_is_fitted_off_the_gui_thread_and_saved(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings_shown: list[str]
) -> None:
    gui_thread = threading.get_ident()
    fitted_on: list[int] = []
    real_fit = module.fit_local_reference_model

    def recording_fit(*args, **kwargs):  # type: ignore[no-untyped-def]
        fitted_on.append(threading.get_ident())
        return real_fit(*args, **kwargs)

    monkeypatch.setattr(module, "fit_local_reference_model", recording_fit)
    target = tmp_path / "local.npz"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "npz"))
    activated: list[object] = []
    dialog = NssModelDialog()
    qtbot.addWidget(dialog)
    monkeypatch.setattr(dialog, "_activate", activated.append)
    dialog._name.setText("Local QA")
    dialog._data_range.setText("255")
    dialog._corpus_paths = _corpus(tmp_path)

    dialog._fit_and_save()
    qtbot.waitUntil(lambda: bool(activated), timeout=30000)

    assert fitted_on and fitted_on[0] != gui_thread
    assert target.is_file()
    assert warnings_shown == []
    assert dialog._job is None and dialog._fit.isEnabled()


def test_a_fit_can_be_cancelled_between_files(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings_shown: list[str]
) -> None:
    """The whole fit used to run on the GUI thread, with no way to stop it."""
    loads: list[Path] = []
    release = threading.Event()
    real_load = module.load_image

    def slow_load(path):  # type: ignore[no-untyped-def]
        loads.append(path)
        release.wait(timeout=10)
        return real_load(path)

    monkeypatch.setattr(module, "load_image", slow_load)
    saved: list[object] = []
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a, **k: saved.append(a))
    dialog = NssModelDialog()
    qtbot.addWidget(dialog)
    dialog._name.setText("Local QA")
    dialog._corpus_paths = _corpus(tmp_path, count=3)

    dialog._fit_and_save()
    qtbot.waitUntil(lambda: bool(loads), timeout=10000)  # the first file is being read
    assert dialog._progress is not None
    dialog._progress.canceled.emit()
    release.set()
    qtbot.waitUntil(lambda: dialog._job is None, timeout=10000)

    assert len(loads) == 1  # stopped before the second file
    assert saved == [] and warnings_shown == []
    assert dialog._status.text() == translator.tr("nss_model.fit_cancelled")


def test_a_failed_fit_is_reported_without_its_exception_type(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warnings_shown: list[str]
) -> None:
    def no_patches(*_args: object, **_kwargs: object) -> None:
        raise ValueError("No active patches found")

    monkeypatch.setattr(module, "fit_local_reference_model", no_patches)
    dialog = NssModelDialog()
    qtbot.addWidget(dialog)
    dialog._name.setText("Local QA")
    dialog._corpus_paths = _corpus(tmp_path, count=1)

    dialog._fit_and_save()
    qtbot.waitUntil(lambda: bool(warnings_shown), timeout=10000)

    assert warnings_shown == [
        explained_error("errors.summary.fit_model", "No active patches found")
    ]
