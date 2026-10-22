"""Tests for qicom.gui.app._build_window - the launcher-choice router.

Routing only: whether the right window type gets built and whether a
Load action's project_path gets handed off - the actual load/round-trip
behavior is already covered by test_controller.py's
TestProjectFolderPersistence and test_qualitative_controller.py, so those
methods are monkeypatched here rather than exercised for real.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import logging
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from qicom.build_identity import build_identity
from qicom.gui.app import (
    _SPLASH_MINIMUM_DURATION_MS,
    RadiomicsUnavailableError,
    _build_window,
    _learned_policy_probe,
    _run_frozen_smoke_test,
    _show_splash_screen,
)
from qicom.gui.main_window import MainWindow
from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.tools.tools_window import ToolsMainWindow
from qicom.gui.widgets.launcher_dialog import (
    ACTION_EDIT,
    ACTION_LOAD,
    ACTION_NEW,
    ACTION_OPEN_TOOL,
    ACTION_TAKE,
    MODE_QUALITATIVE,
    MODE_QUANTITATIVE,
    MODE_RADIOMICS,
    MODE_TOOLS,
    LauncherChoice,
)
from qicom.gui.widgets.qualitative_main_window import QualitativeMainWindow
from qicom.gui.workers import run_in_thread, wait_for_active_jobs
from qicom.session.storage import SessionStorage


@pytest.fixture(autouse=True)
def _in_memory_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    # MainWindow() with no override touches the real on-disk session DB
    # (AppController.__init__ defaults to SessionStorage()) - every test
    # in this module must stay off of it.
    monkeypatch.setattr(
        "qicom.gui.controller.SessionStorage", lambda *a, **k: SessionStorage(":memory:")
    )


class TestQuantitativeRouting:
    def test_new_project_returns_a_plain_main_window(self, qtbot: QtBot) -> None:
        choice = LauncherChoice(mode=MODE_QUANTITATIVE, action=ACTION_NEW, project_path=None)

        window = _build_window(choice)
        qtbot.addWidget(window)

        assert isinstance(window, MainWindow)

    def test_load_project_hands_the_chosen_path_to_open_project_zip(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[Path] = []
        monkeypatch.setattr(
            MainWindow, "open_project_zip", lambda self, path: received.append(path)
        )
        choice = LauncherChoice(
            mode=MODE_QUANTITATIVE, action=ACTION_LOAD, project_path=tmp_path / "a_project.zip"
        )

        window = _build_window(choice)
        qtbot.addWidget(window)

        assert isinstance(window, MainWindow)
        assert received == [tmp_path / "a_project.zip"]


class TestQualitativeRouting:
    def test_new_evaluation_returns_a_qualitative_main_window(self, qtbot: QtBot) -> None:
        choice = LauncherChoice(mode=MODE_QUALITATIVE, action=ACTION_NEW, project_path=None)

        window = _build_window(choice)
        qtbot.addWidget(window)

        assert isinstance(window, QualitativeMainWindow)

    def test_edit_evaluation_hands_the_chosen_path_to_open_evaluation_for_editing(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[Path] = []
        monkeypatch.setattr(
            QualitativeMainWindow,
            "open_evaluation_for_editing",
            lambda self, path, **_kwargs: received.append(path),
        )
        choice = LauncherChoice(
            mode=MODE_QUALITATIVE, action=ACTION_EDIT, project_path=tmp_path / "an_evaluation.zip"
        )

        window = _build_window(choice)
        qtbot.addWidget(window)

        assert isinstance(window, QualitativeMainWindow)
        assert received == [tmp_path / "an_evaluation.zip"]

    def test_take_evaluation_hands_the_chosen_path_to_open_evaluation_to_take(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[Path] = []
        monkeypatch.setattr(
            QualitativeMainWindow,
            "open_evaluation_to_take",
            lambda self, path, **_kwargs: received.append(path),
        )
        choice = LauncherChoice(
            mode=MODE_QUALITATIVE, action=ACTION_TAKE, project_path=tmp_path / "an_evaluation.zip"
        )

        window = _build_window(choice)
        qtbot.addWidget(window)

        assert isinstance(window, QualitativeMainWindow)
        assert received == [tmp_path / "an_evaluation.zip"]


class TestRadiomicsRouting:
    def test_new_project_returns_dedicated_radiomics_window(self, qtbot: QtBot) -> None:
        choice = LauncherChoice(mode=MODE_RADIOMICS, action=ACTION_NEW)

        window = _build_window(choice)
        qtbot.addWidget(window)

        assert isinstance(window, RadiomicsMainWindow)
        assert not isinstance(window, MainWindow)

    def test_load_project_hands_path_to_radiomics_loader(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[Path] = []
        monkeypatch.setattr(
            RadiomicsMainWindow, "open_project", lambda self, path: received.append(Path(path))
        )
        path = tmp_path / "radiomics.zip"

        window = _build_window(LauncherChoice(MODE_RADIOMICS, ACTION_LOAD, path))
        qtbot.addWidget(window)

        assert isinstance(window, RadiomicsMainWindow)
        assert received == [path]

    def test_missing_optional_extra_has_an_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "qicom.gui.radiomics_main_window":
                raise ModuleNotFoundError("No module named 'SimpleITK'", name="SimpleITK")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", blocked_import)

        with pytest.raises(RadiomicsUnavailableError, match=r"qicom\[radiomics\]"):
            _build_window(LauncherChoice(MODE_RADIOMICS, ACTION_NEW))


class TestToolsRouting:
    def test_a_tool_choice_opens_the_tools_window_on_that_tool(self, qtbot: QtBot) -> None:
        window = _build_window(
            LauncherChoice(MODE_TOOLS, ACTION_OPEN_TOOL, tool_id="dicom_compare")
        )
        qtbot.addWidget(window)

        assert isinstance(window, ToolsMainWindow)
        assert window.current_tool == "dicom_compare"
        assert window.return_to_launcher is False


def test_launcher_import_defers_all_heavy_windows_and_radiomics_backend() -> None:
    code = """
import sys
import qicom.gui.app

for name in (
    'qicom.gui.main_window',
    'qicom.gui.radiomics_main_window',
    'qicom.gui.widgets.qualitative_main_window',
    'qicom.gui.tools.tools_window',
    'qicom.gui.tools.dicom_tags',
    'qicom.radiomics',
    'SimpleITK',
):
    assert name not in sys.modules, name
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False
    )

    assert completed.returncode == 0, completed.stderr


def test_frozen_smoke_path_checks_current_full_environment(capsys) -> None:
    assert _run_frozen_smoke_test() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["profile"] == "full"
    assert "ibsi1_digital_phantom" in payload["reference_sets"]
    assert len(payload["reference_sets"]) == 7
    assert payload["riqe_model"] == "verified"
    assert payload["lossless_jpeg"] == "verified"


def test_the_smoke_fails_when_the_bundle_lacks_gdcm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most CT and MRI exports are lossless JPEG, which only GDCM decodes."""
    from pydicom.pixels import get_decoder
    from pydicom.uid import JPEGLosslessSV1

    monkeypatch.setattr(get_decoder(JPEGLosslessSV1), "_available", {})

    with pytest.raises(RuntimeError, match="lossless-JPEG decoder"):
        _run_frozen_smoke_test()


def test_the_smoke_fails_when_the_bundle_lacks_its_reference_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qicom.radiomics import ibsi_validation

    monkeypatch.setattr(ibsi_validation, "available_reference_set_ids", lambda: ())

    with pytest.raises(RuntimeError, match="IBSI reference sets are missing"):
        _run_frozen_smoke_test()


def test_frozen_smoke_can_write_json_without_relying_on_stdout(capsys, tmp_path: Path) -> None:
    target = tmp_path / "smoke.json"

    assert _run_frozen_smoke_test(target) == 0

    assert capsys.readouterr().out == ""
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "ok"


def test_frozen_smoke_path_reports_the_build_identity(capsys) -> None:
    """The packaging workflow asserts on this: a bundle that cannot name
    the commit it was built from has no build identity at all, and the whole
    point of generating one at build time is that it survives without a
    repository to ask (see qicom.build_identity)."""
    assert _run_frozen_smoke_test() == 0
    payload = json.loads(capsys.readouterr().out)
    identity = build_identity()

    assert payload["version"] == identity.version
    assert payload["revision"] == identity.revision
    assert payload["source_dirty"] == identity.source_dirty
    assert payload["identity_source"] == identity.resolved_from


class TestSplashScreen:
    """_show_splash_screen only builds/shows the splash - main() itself owns
    the "keep it up for 1 second" wait (a plain QTimer.singleShot
    tied to a nested QEventLoop, not worth exercising here: main() runs a
    real, indefinite app.exec() and has no test coverage of its own for
    that reason, see this module's own docstring)."""

    def test_startup_logo_duration_is_one_second(self) -> None:
        assert _SPLASH_MINIMUM_DURATION_MS == 1000

    def test_pixmap_fits_within_a_square_about_20_percent_of_screen_height(
        self, qtbot: QtBot
    ) -> None:
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        screen = app.primaryScreen()
        assert screen is not None
        expected_side = int(screen.availableGeometry().height() * 0.2)

        splash = _show_splash_screen(app)
        try:
            pixmap = splash.pixmap()
            assert pixmap.width() <= expected_side
            assert pixmap.height() <= expected_side
            # KeepAspectRatio only guarantees one dimension actually hits
            # the target - the other is whatever the logo's own aspect
            # ratio leaves it at.
            assert max(pixmap.width(), pixmap.height()) >= expected_side - 2
        finally:
            splash.close()

    def test_is_shown_and_centered_on_the_screen(self, qtbot: QtBot) -> None:
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        screen = app.primaryScreen()
        assert screen is not None
        geometry = screen.availableGeometry()

        splash = _show_splash_screen(app)
        try:
            assert splash.isVisible()
            assert splash.geometry().center() == geometry.center()
        finally:
            splash.close()


class TestSessionLogging:
    """QICOM configures a rotating JSON log and ships a viewer for it; until
    now nothing ever wrote a record, so a user reporting a failure left
    nothing behind to read. These pin the few records that bracket a
    session and every background operation in it."""

    def test_background_job_failure_is_logged_with_its_traceback(
        self, qtbot: QtBot, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom() -> None:
            raise ValueError("no images")

        errors: list[str] = []
        with caplog.at_level(logging.INFO, logger="qicom.gui.workers"):
            run_in_thread(_boom, on_success=lambda _r: None, on_error=errors.append)
            assert wait_for_active_jobs(10_000)

        assert errors  # the GUI still gets its message
        records = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert records, caplog.text
        assert "_boom" in records[0].getMessage()
        assert records[0].exc_info is not None

    def test_background_job_success_is_logged_without_its_arguments(
        self, qtbot: QtBot, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _load(secret_path: str) -> str:
            return "done"

        with caplog.at_level(logging.INFO, logger="qicom.gui.workers"):
            run_in_thread(
                _load,
                "/data/DOE^JOHN/IM0001",
                on_success=lambda _r: None,
                on_error=lambda _m: None,
            )
            assert wait_for_active_jobs(10_000)

        assert "_load" in caplog.text
        assert "DOE" not in caplog.text  # arguments are never logged


class TestLearnedMetricPolicy:
    """QICOM does not bundle model weights: they are downloaded once, on
    demand, only when the user asks, and verified against a pinned SHA-256.
    The half that used to be undemonstrated is what a bundle does when they
    are absent - the state every first run is in."""

    def test_a_build_without_the_learned_extra_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name: None if name == "torch" else object()
        )

        probe = _learned_policy_probe()

        assert probe["outcome"] == "not-built"
        assert probe["checkpoints_installed"] == []

    @pytest.mark.requires_learned
    def test_an_empty_cache_refuses_offline_instead_of_downloading(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The one behaviour the frozen smoke asserts on a clean runner: no
        checkpoints, no network, a stated refusal."""
        from qicom.metrics import learned_weights, reference_structural

        monkeypatch.setenv("TORCH_HOME", str(tmp_path / "empty-cache"))
        # The model is kept for the process: a fresh cache location only
        # takes effect once it is dropped.
        reference_structural.release_learned_models()
        assert learned_weights.missing_checkpoints(["lpips"])

        def no_network(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("an analysis must never open a network connection")

        monkeypatch.setattr(urllib.request, "urlopen", no_network)

        probe = _learned_policy_probe()

        assert probe["outcome"] == "unavailable-offline"
        assert probe["checkpoints_installed"] == []
        assert "never downloads model weights during analysis" in str(probe["reason"])

    @pytest.mark.requires_learned
    def test_a_prepared_cache_computes_the_metric(self) -> None:
        """The other documented outcome: once the user has explicitly
        prepared the weights, the metric really works in the bundle.

        The weights are the ones packaging/prepare_learned_weights.py put in
        the real cache; tests/conftest.py links them into this test's
        profile, because the profile redirect would otherwise hide them and
        turn this into a second empty-cache test.
        """
        from qicom.metrics import reference_structural

        # The sibling above may have left a model behind.
        reference_structural.release_learned_models()

        probe = _learned_policy_probe()

        assert probe["outcome"] == "computed"
        assert probe["checkpoints_installed"]
