"""Tests for what every image tool shares: the job window, the canvas, the guidance."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QProgressDialog, QPushButton
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from qicom.gui.guidance import GUIDANCE_DONE, GUIDANCE_NEXT, guide, is_next
from qicom.gui.tools import image_source as source_module
from qicom.gui.tools import job_dialog
from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.tools.image_source import ImageSource
from qicom.gui.tools.job_dialog import JobDialog, percent, run_job, staged


class TestJobDialog:
    def test_percent_is_bounded(self) -> None:
        assert percent(0, 0) == 0
        assert percent(1, 3) == 33
        assert percent(5, 4) == 100
        assert percent(-1, 4) == 0

    def test_staged_reports_start_and_end(self) -> None:
        seen: list[tuple[int, int]] = []

        result = staged(lambda value: value * 2)(21, progress_callback=lambda *p: seen.append(p))

        assert result == 42 and seen == [(0, 1), (1, 1)]
        assert staged(lambda: "plain")() == "plain"

    def test_a_job_shows_its_percentage_and_closes(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = QPushButton()
        qtbot.addWidget(parent)
        values: list[int] = []
        original = QProgressDialog.setValue
        monkeypatch.setattr(
            JobDialog, "setValue", lambda self, value: (values.append(value), original(self, value))
        )

        def work(*, progress_callback: object) -> str:
            for done in range(5):
                progress_callback(done, 4)  # type: ignore[operator]
            return "done"

        results: list[object] = []
        dialog = run_job(parent, "Working", work, on_success=results.append, on_error=print)
        assert isinstance(dialog, JobDialog) and dialog.labelText() == "Working"
        assert dialog.isVisible()

        qtbot.waitUntil(lambda: results == ["done"])
        assert not isValid(dialog) or not dialog.isVisible()
        assert 25 in values and values[-1] == 100

    def test_a_failed_job_closes_and_reports(self, qtbot: QtBot) -> None:
        parent = QPushButton()
        qtbot.addWidget(parent)
        errors: list[str] = []

        def broken(*, progress_callback: object) -> None:
            raise ValueError("no luck")

        dialog = run_job(parent, "Working", broken, on_success=print, on_error=errors.append)

        qtbot.waitUntil(lambda: bool(errors))
        assert "no luck" in errors[0]
        assert not isValid(dialog) or not dialog.isVisible()

    def test_the_dialog_has_no_cancel_button(self, qtbot: QtBot) -> None:
        dialog = JobDialog("x")
        qtbot.addWidget(dialog)
        assert dialog.findChild(QPushButton) is None
        assert dialog.windowModality() == Qt.WindowModality.WindowModal
        assert job_dialog.translator  # the title comes from the translation


class TestGuidance:
    def test_one_or_more_buttons_are_coloured_and_the_rest_muted(self, qtbot: QtBot) -> None:
        buttons = [QPushButton() for _index in range(3)]
        for button in buttons:
            qtbot.addWidget(button)

        guide(buttons, buttons[0], buttons[2])
        assert [button.property("guidance") for button in buttons] == [
            GUIDANCE_NEXT,
            GUIDANCE_DONE,
            GUIDANCE_NEXT,
        ]
        guide(buttons, None)
        assert not any(is_next(button) for button in buttons)


class _Wheel:
    def __init__(
        self,
        delta: int,
        modifiers: Qt.KeyboardModifier,
        *,
        phase: Qt.ScrollPhase = Qt.ScrollPhase.NoScrollPhase,
        orientation: Qt.Orientation = Qt.Orientation.Vertical,
    ) -> None:
        self._delta, self._modifiers, self.accepted = delta, modifiers, False
        self._phase, self._orientation = phase, orientation

    def delta(self) -> int:
        return self._delta

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers

    def phase(self) -> Qt.ScrollPhase:
        return self._phase

    def orientation(self) -> Qt.Orientation:
        return self._orientation

    def accept(self) -> None:
        self.accepted = True


class TestCanvas:
    def _canvas(self, qtbot: QtBot) -> ImageCanvas:
        canvas = ImageCanvas(roi_tools=False)
        qtbot.addWidget(canvas)
        return canvas

    def test_the_wheel_steps_slices_like_the_workflows(self, qtbot: QtBot) -> None:
        canvas = self._canvas(qtbot)
        canvas.set_image(np.zeros((5, 8, 8), np.float32))
        start = canvas.current_slice

        down = _Wheel(-120, Qt.KeyboardModifier.NoModifier)
        canvas._view_box.wheelEvent(down)
        assert down.accepted and canvas.current_slice == start + 1
        canvas._view_box.wheelEvent(_Wheel(120, Qt.KeyboardModifier.NoModifier))
        canvas._view_box.wheelEvent(_Wheel(120, Qt.KeyboardModifier.NoModifier))
        assert canvas.current_slice == start - 1
        for _step in range(10):
            canvas.step_slice(1)
        assert canvas.current_slice == 4  # stops at the last slice

    def test_a_touchpad_swipe_moves_by_whole_notches_only(self, qtbot: QtBot) -> None:
        """A touchpad sends many small events per swipe, then coasts and ends
        with no movement: none of that may move a slice per event."""
        canvas = self._canvas(qtbot)
        canvas.set_image(np.zeros((5, 8, 8), np.float32))
        start = canvas.current_slice
        no_modifier = Qt.KeyboardModifier.NoModifier

        canvas._view_box.wheelEvent(_Wheel(-40, no_modifier, phase=Qt.ScrollPhase.ScrollBegin))
        canvas._view_box.wheelEvent(_Wheel(-40, no_modifier, phase=Qt.ScrollPhase.ScrollUpdate))
        assert canvas.current_slice == start
        canvas._view_box.wheelEvent(_Wheel(-40, no_modifier, phase=Qt.ScrollPhase.ScrollUpdate))
        assert canvas.current_slice == start + 1
        canvas._view_box.wheelEvent(_Wheel(0, no_modifier, phase=Qt.ScrollPhase.ScrollEnd))
        canvas._view_box.wheelEvent(_Wheel(-400, no_modifier, phase=Qt.ScrollPhase.ScrollMomentum))
        assert canvas.current_slice == start + 1

    def test_a_sideways_swipe_moves_no_slice(self, qtbot: QtBot) -> None:
        canvas = self._canvas(qtbot)
        canvas.set_image(np.zeros((5, 8, 8), np.float32))
        start = canvas.current_slice
        sideways = _Wheel(
            -240, Qt.KeyboardModifier.NoModifier, orientation=Qt.Orientation.Horizontal
        )

        canvas._view_box.wheelEvent(sideways)

        assert sideways.accepted and canvas.current_slice == start

    def test_ctrl_wheel_still_zooms(self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
        canvas = self._canvas(qtbot)
        canvas.set_image(np.zeros((5, 8, 8), np.float32))
        start = canvas.current_slice
        zoomed: list[object] = []
        monkeypatch.setattr(
            pg.ViewBox, "wheelEvent", lambda _self, ev, axis=None: zoomed.append(ev)
        )

        canvas._view_box.wheelEvent(_Wheel(-120, Qt.KeyboardModifier.ControlModifier))

        assert zoomed and canvas.current_slice == start

    def test_an_empty_canvas_is_the_place_to_load(self, qtbot: QtBot) -> None:
        canvas = self._canvas(qtbot)
        assert canvas._stack.currentWidget() is canvas._empty_prompt
        assert canvas._empty_prompt.text()

        with qtbot.waitSignal(canvas.load_requested):
            canvas._empty_prompt.click()
        with qtbot.waitSignal(canvas.file_dropped) as blocker:
            canvas._empty_prompt.file_dropped.emit("/data/scan.nii.gz")
        assert blocker.args == ["/data/scan.nii.gz"]

        canvas.set_image(np.zeros((8, 8), np.float32))
        assert canvas._stack.currentWidget() is canvas._image_view
        canvas.set_image(None)
        assert canvas._stack.currentWidget() is canvas._empty_prompt

    def test_the_empty_text_can_name_what_to_load(self, qtbot: QtBot) -> None:
        canvas = self._canvas(qtbot)
        canvas.set_empty_text("tools.masks.empty_prompt")
        assert "NIfTI" in canvas._empty_prompt.text()


class TestImageSourceOnTheCanvas:
    def test_a_drop_opens_and_a_click_asks(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        source = ImageSource()
        canvas = ImageCanvas(roi_tools=False)
        qtbot.addWidget(source)
        qtbot.addWidget(canvas)
        opened: list[object] = []
        monkeypatch.setattr(source, "open_path", lambda path, *_a: opened.append(str(path)))
        source.attach_canvas(canvas)

        canvas.file_dropped.emit(str(tmp_path / "a.nii.gz"))

        def accept(dialog: source_module.OpenImageDialog) -> int:
            dialog.path = str(tmp_path / "series")
            return int(QDialog.DialogCode.Accepted)

        monkeypatch.setattr(source_module.OpenImageDialog, "exec", accept)
        canvas.load_requested.emit()

        assert opened == [str(tmp_path / "a.nii.gz"), str(tmp_path / "series")]

    def test_the_chooser_offers_a_folder_or_a_file(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dialog = source_module.OpenImageDialog()
        qtbot.addWidget(dialog)
        monkeypatch.setattr(
            source_module.QFileDialog, "getExistingDirectory", lambda *_a, **_k: str(tmp_path)
        )
        dialog._folder_button.click()
        assert dialog.path == str(tmp_path) and dialog.result() == QDialog.DialogCode.Accepted
        monkeypatch.setattr(
            source_module.QFileDialog, "getOpenFileName", lambda *_a, **_k: ("", "")
        )
        dialog.path = ""
        dialog._file_button.click()
        assert dialog.path == ""


def test_paths_under_home_are_shown_with_a_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    from qicom.gui.file_names import display_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    inside = tmp_path / "data" / "out_anonymized"
    if os.name != "nt":
        assert display_path(inside) == "~/data/out_anonymized"
        assert display_path(tmp_path) == "~"
    assert display_path(Path("/elsewhere/x")).endswith("x")
