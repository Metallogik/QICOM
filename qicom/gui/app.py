"""QApplication bootstrap."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEventLoop, QRect, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox, QSplashScreen

from qicom.build_identity import build_identity
from qicom.gui.error_reporting import install_exception_hook
from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.preferences import preferences_store
from qicom.gui.resources import LOGO_PATH, app_icon
from qicom.gui.widgets.launcher_dialog import (
    ACTION_EDIT,
    ACTION_LOAD,
    ACTION_TAKE,
    MODE_QUALITATIVE,
    MODE_QUANTITATIVE,
    MODE_RADIOMICS,
    MODE_TOOLS,
    LauncherChoice,
    LauncherDialog,
)
from qicom.i18n import translator
from qicom.session.app_logging import configure_logging, default_log_path

if TYPE_CHECKING:
    from qicom.gui.main_window import MainWindow
    from qicom.gui.radiomics_main_window import RadiomicsMainWindow
    from qicom.gui.tools.tools_window import ToolsMainWindow
    from qicom.gui.widgets.qualitative_main_window import QualitativeMainWindow

    TopLevelWindow = MainWindow | QualitativeMainWindow | RadiomicsMainWindow | ToolsMainWindow
else:
    TopLevelWindow = QMainWindow

_logger = logging.getLogger(__name__)

# The splash screen's logo is a square this fraction of the screen's own
# height - "roughly 20%" per the design ask, not a pixel-exact contract.
_SPLASH_HEIGHT_FRACTION = 0.2
_SPLASH_MINIMUM_DURATION_MS = 1000


class RadiomicsUnavailableError(RuntimeError):
    """The optional radiomics workflow was selected without its extra."""


def _learned_policy_probe() -> dict[str, object]:
    """Demonstrate, in the bundle itself, what the learned-metric policy says.

    Model weights are not bundled: they are downloaded once, on demand, only
    when the user asks for it, verified against a pinned SHA-256 (see
    qicom.metrics.learned_weights). The half of that policy that matters most
    is the half nobody sees - what happens when the weights are *not* there.
    An analysis must then omit the metric, and must not reach for the network
    to fix it.

    So this asks LPIPS to build itself and reports which of the two documented
    outcomes happened:

    * "computed" - the checkpoints were present and verified;
    * "unavailable-offline" - they were not, and the metric refused without
      any network access, which is what a first run on a fresh machine does.

    The packaging workflow runs the bundle twice, once with an empty cache,
    and asserts on this field. Nothing here downloads anything: the refusal
    comes from _require_verified_checkpoint, which raises rather than fetch.
    """
    if importlib.util.find_spec("torch") is None:
        return {"outcome": "not-built", "checkpoints_installed": []}

    from qicom.metrics import learned_weights, reference_structural

    installed = sorted(
        file_name
        for file_name in learned_weights.CHECKPOINTS
        if learned_weights.is_installed(file_name)
    )
    try:
        reference_structural._get_lpips_model()
    except ImportError as exc:
        # The documented offline path: stated, never silently skipped.
        return {
            "outcome": "unavailable-offline",
            "checkpoints_installed": installed,
            "reason": str(exc),
        }
    return {"outcome": "computed", "checkpoints_installed": installed}


def _bundled_reference_sets() -> list[str]:
    """Load every IBSI 1 manifest the radiomics verification window offers."""
    from qicom.radiomics.ibsi_validation import (
        available_reference_set_ids,
        load_reference_manifest,
    )

    identifiers = list(available_reference_set_ids())
    if not identifiers:
        raise RuntimeError("The IBSI reference sets are missing from this build")
    for identifier in identifiers:
        load_reference_manifest(identifier)
    return identifiers


#: One 4x4 12-bit frame in lossless JPEG (SV1), the syntax most CT and MRI
#: exports use, and the pixels it decodes to. Only GDCM decodes it: a bundle
#: that lost GDCM would refuse those studies with nothing else failing.
_LOSSLESS_JPEG_FRAME = bytes.fromhex(
    "ffd8ffc3000b100004000401011100ffc40019000002030100000000000000000000000001"
    "070a0b0c10ffda0008010100010000e24eff00fa002bff008ffe007634af34fe375c8e4727ffd9"
)
_LOSSLESS_JPEG_PIXELS = (
    (0, 1, 2, 3),
    (4095, 2048, 1024, 512),
    (7, 77, 777, 3333),
    (100, 200, 300, 400),
)


def _bundled_lossless_jpeg_decoder() -> str:
    """Decode one lossless-JPEG frame through pydicom, as a DICOM file would be."""
    import numpy as np
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEGLosslessSV1

    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.TransferSyntaxUID = JPEGLosslessSV1
    dataset.Rows = dataset.Columns = 4
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated, dataset.BitsStored, dataset.HighBit = 16, 12, 11
    dataset.PixelRepresentation = 0
    dataset.PixelData = encapsulate([_LOSSLESS_JPEG_FRAME])
    try:
        decoded = dataset.pixel_array
    except RuntimeError as exc:
        raise RuntimeError("The lossless-JPEG decoder (GDCM) is missing from this build") from exc
    if not np.array_equal(decoded, np.asarray(_LOSSLESS_JPEG_PIXELS)):
        raise RuntimeError("The bundled lossless-JPEG decoder returned the wrong pixels")
    return "verified"


def _run_frozen_smoke_test(output_path: Path | None = None) -> int:
    """Exercise frozen-only imports and one Qt/pyqtgraph widget headlessly.

    A Windows ``console=False`` bundle has no stdout. The packaging workflow
    therefore supplies ``output_path`` and reads the JSON from disk; printing
    remains available for source-level diagnostics and backwards compatibility.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([sys.argv[0], "-platform", "offscreen"])

    # These imports cover the quantitative controller, Excel writer and the
    # QtSvg/OpenGL paths pyqtgraph initializes. They intentionally avoid user
    # preferences, the session database and any network access.
    from openpyxl import Workbook

    from qicom.gui import controller as _controller
    from qicom.gui.widgets.dual_image_view import DualImageView

    assert _controller.AppController is not None
    workbook = Workbook()
    assert workbook.active is not None
    widget = DualImageView()
    widget.close()

    has_radiomics = importlib.util.find_spec("radiomics") is not None
    has_simpleitk = importlib.util.find_spec("SimpleITK") is not None
    if has_radiomics != has_simpleitk:
        raise RuntimeError("Frozen radiomics dependencies are only partially present")
    profile = "full" if has_radiomics else "base"
    if has_radiomics:
        import SimpleITK as _simpleitk
        from radiomics import featureextractor as _featureextractor

        from qicom.gui.radiomics_main_window import RadiomicsMainWindow

        assert _simpleitk.Image is not None
        assert _featureextractor.RadiomicsFeatureExtractor is not None
        assert RadiomicsMainWindow is not None
    reference_sets = _bundled_reference_sets() if has_radiomics else []
    # Bundled data files, not imports: a missing one only fails when a user
    # reaches it, so the smoke loads each instead of trusting the spec.
    from qicom.metrics import riqe_model

    riqe_model.load_model()
    lossless_jpeg = _bundled_lossless_jpeg_decoder()

    # The build identity travels in the smoke output so the packaging
    # workflow can assert that a bundle knows which commit produced it -
    # without a .git directory to consult, which a bundle does not have
    # (see qicom.build_identity and packaging/generate_build_identity.py).
    identity = build_identity()
    payload = json.dumps(
        {
            "status": "ok",
            "profile": profile,
            "learned_available": importlib.util.find_spec("torch") is not None,
            "learned_policy": _learned_policy_probe(),
            "lossless_jpeg": lossless_jpeg,
            "reference_sets": reference_sets,
            "riqe_model": "verified",
            "version": identity.version,
            "revision": identity.revision,
            "source_dirty": identity.source_dirty,
            "identity_source": identity.resolved_from,
        },
        sort_keys=True,
    )
    if output_path is None:
        print(payload)
    else:
        output_path.write_text(payload + "\n", encoding="utf-8")
    app.processEvents()
    return 0


def _show_splash_screen(app: QApplication) -> QSplashScreen:
    """QICOM's logo, centered on the screen it appears on, scaled to a
    square roughly _SPLASH_HEIGHT_FRACTION of that screen's own height.
    Returns the (already shown) splash screen - main() keeps it up for at
    least _SPLASH_MINIMUM_DURATION_MS before closing it, ahead of the
    launcher ever appearing."""
    screen = app.primaryScreen()
    geometry = screen.availableGeometry() if screen is not None else QRect(0, 0, 1024, 768)
    side = max(1, int(geometry.height() * _SPLASH_HEIGHT_FRACTION))
    pixmap = QPixmap(str(LOGO_PATH)).scaled(
        side,
        side,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    splash = QSplashScreen(pixmap)
    splash.move(geometry.center() - splash.rect().center())
    splash.show()
    return splash


def _build_window(
    choice: LauncherChoice,
) -> TopLevelWindow:
    """Routes a launcher choice to the right top-level window."""
    if choice.mode == MODE_QUANTITATIVE:
        from qicom.gui.main_window import MainWindow

        metric_selection_settings.start_new_session_from_defaults()
        window = MainWindow()
        if choice.action == ACTION_LOAD:
            assert choice.project_path is not None
            window.open_project_zip(choice.project_path)
        return window

    if choice.mode == MODE_RADIOMICS:
        try:
            from qicom.gui.radiomics_main_window import RadiomicsMainWindow
        except ImportError as exc:
            raise RadiomicsUnavailableError(
                translator.tr("launcher.radiomics_unavailable")
            ) from exc
        radiomics_window = RadiomicsMainWindow()
        if choice.action == ACTION_LOAD:
            assert choice.project_path is not None
            radiomics_window.open_project(choice.project_path)
        return radiomics_window

    if choice.mode == MODE_TOOLS:
        from qicom.gui.tools.tools_window import ToolsMainWindow

        return ToolsMainWindow(initial_tool=choice.tool_id)

    assert choice.mode == MODE_QUALITATIVE
    from qicom.gui.widgets.qualitative_main_window import QualitativeMainWindow

    qualitative_window = QualitativeMainWindow()
    if choice.action == ACTION_EDIT:
        assert choice.project_path is not None
        qualitative_window.open_evaluation_for_editing(choice.project_path, access=choice.access)
    elif choice.action == ACTION_TAKE:
        assert choice.project_path is not None
        qualitative_window.open_evaluation_to_take(
            choice.project_path, account=choice.account, draft=choice.draft, access=choice.access
        )
    else:
        qualitative_window.open_new_evaluation()
    return qualitative_window


def main() -> int:
    if "--smoke-test" in sys.argv:
        output_path = None
        if "--smoke-output" in sys.argv:
            index = sys.argv.index("--smoke-output")
            if index + 1 >= len(sys.argv):
                raise ValueError("--smoke-output requires a path")
            output_path = Path(sys.argv[index + 1])
        return _run_frozen_smoke_test(output_path)
    configure_logging()
    # Before any window exists: an exception escaping a Qt slot is logged
    # with its traceback and shown in the open windows' error logs, instead
    # of reaching only a console that a packaged build does not have.
    install_exception_hook()
    # The application used to configure a rotating JSON log, ship a viewer
    # for it and never write a single record: a user reporting "it failed"
    # left nothing behind to read. These few records bracket every session
    # and every window it opens; the background-job logger (see
    # qicom.gui.workers) covers what happens in between. Deliberately no
    # file paths or patient-derived text in any of them.
    _logger.info(
        "QICOM %s starting on %s (Python %s), logging to %s",
        build_identity().describe(),
        platform.platform(terse=True),
        platform.python_version(),
        default_log_path(),
    )
    app = QApplication(sys.argv)
    app.setApplicationName("QICOM")
    # On Linux desktops that resolve a running window's taskbar icon via
    # its associated .desktop file (e.g. GNOME Shell) rather than the
    # in-process QIcon, a mismatched/missing desktop-file association
    # falls back to a generic icon - matching this to packaging/qicom.desktop
    # (installed as ~/.local/share/applications/qicom.desktop) is what
    # actually fixes that, setWindowIcon alone is not enough there.
    app.setDesktopFileName("qicom")
    # Sets the taskbar/dock icon and the default for every window that
    # doesn't set its own (MainWindow also sets it explicitly below, for
    # the title bar and Alt-Tab switcher).
    app.setWindowIcon(app_icon())
    # Preferences are applied before the splash/launcher is constructed so
    # neither briefly flashes in the default language or light theme.
    preferences_store.activate(app)

    # Shown once, for 1 second, before the launcher itself ever
    # appears. A local QEventLoop (rather than time.sleep) keeps the splash
    # screen actually painted and the app responsive while waiting - a
    # plain sleep would freeze the event loop and leave it unpainted/frozen
    # for the same duration instead.
    splash = _show_splash_screen(app)
    wait_loop = QEventLoop()
    QTimer.singleShot(_SPLASH_MINIMUM_DURATION_MS, wait_loop.quit)
    wait_loop.exec()
    splash.close()

    # File > Close Project (both MainWindow and QualitativeMainWindow) sets
    # its own return_to_launcher flag and closes itself - closing the last
    # visible top-level window ends the app.exec() below on its own
    # (QApplication's default quitOnLastWindowClosed), so this loop just
    # checks that flag afterward to tell "close project" (show the launcher
    # again) apart from a real exit (return out of main() and end the
    # process).
    while True:
        launcher = LauncherDialog()
        if launcher.exec() != QDialog.DialogCode.Accepted or launcher.choice is None:
            _logger.info("QICOM exiting from the launcher")
            return 0
        _logger.info(
            "Opening the %s window (%s)", launcher.choice.mode, launcher.choice.action or "new"
        )
        try:
            window = _build_window(launcher.choice)
        except RadiomicsUnavailableError as exc:
            _logger.warning("Radiomics workflow unavailable: %s", exc)
            QMessageBox.critical(None, translator.tr("app.title"), str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - back to the launcher, never out of QICOM
            # Building a window opens what the launcher chose: a damaged
            # project or session database used to end the process here.
            _logger.exception("The chosen workflow could not be opened")
            QMessageBox.critical(
                None,
                translator.tr("app.title"),
                translator.tr("launcher.open_failed", error=f"{type(exc).__name__}: {exc}"),
            )
            continue

        # Calling showMaximized() immediately, before the window has ever
        # been mapped by the window manager, is a known Qt/X11 quirk: the
        # maximized state silently doesn't take effect until some later
        # geometry event (e.g. the user dragging the window) forces the WM
        # to reconsider it - exactly the "opens windowed, then snaps to
        # fullscreen the moment I move it" symptom. Deferring to the next
        # event-loop iteration (after the window is already mapped) makes
        # it apply immediately instead.
        QTimer.singleShot(0, window.showMaximized)

        app.exec()
        if not window.return_to_launcher:
            _logger.info("QICOM exiting")
            return 0
        _logger.info("Returning to the launcher")


if __name__ == "__main__":
    sys.exit(main())
