"""The Utility Tools submenu every workflow window carries in its Tools menu.

The tools are reachable from the launcher, but a user in the middle of an
analysis should not have to close it to read a header or convert a file.
Each workflow window therefore lists the same catalog in its Tools menu and
opens one Tools window of its own, reused for as long as the workflow
window lives and closed with it, so the application still ends when the user closes
what they were working in.

"Inspect DICOM tags of the current image" opens the header of the image the
window has loaded in the tag inspector and editor. The qualitative window
offers the catalog without it: an evaluator answering a blinded evaluation
is not handed a way to the header of the image being rated.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMenu, QWidget

from qicom.gui.tools.catalog import TOOLS
from qicom.i18n import translator

CurrentSource = Callable[[], Path | None]
_NOT_DICOM = (".nii", ".nii.gz", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")


def dicom_candidate(path: Path | None) -> Path | None:
    """`path` when it may hold DICOM - a folder, or a file not named as
    another format - else None: a NIfTI or a PNG has no DICOM header."""
    if path is None:
        return None
    if path.is_dir() or not path.name.lower().endswith(_NOT_DICOM):
        return path
    return None


class UtilityToolsMenu(QObject):
    """Adds the catalog to `parent_menu` and owns the Tools window it opens."""

    def __init__(
        self,
        window: QMainWindow,
        parent_menu: QMenu,
        *,
        current_source: CurrentSource | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._current_source = current_source
        self._tools: QMainWindow | None = None
        menu = parent_menu.addMenu("")
        assert menu is not None
        self.menu = menu
        self._actions: dict[str, QAction] = {}
        for spec in TOOLS:
            action = self.menu.addAction("")
            action.triggered.connect(partial(self._open_from_menu, spec.tool_id))
            self._actions[spec.tool_id] = action
        self._inspect_action: QAction | None = None
        if current_source is not None:
            self.menu.addSeparator()
            self._inspect_action = self.menu.addAction("")
            self._inspect_action.triggered.connect(self.inspect_current)
            self.menu.aboutToShow.connect(self._refresh)
        window.installEventFilter(self)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def tools_window(self) -> QMainWindow | None:
        return self._tools

    def _open_from_menu(self, tool_id: str, _checked: bool = False) -> None:
        self.open(tool_id)

    def open(self, tool_id: str) -> QWidget:
        from qicom.gui.tools.tools_window import ToolsMainWindow

        tools = self._tools
        if not isinstance(tools, ToolsMainWindow):
            tools = ToolsMainWindow(initial_tool=tool_id, embedded=True)
            self._tools = tools
        # One window for the life of the workflow window, shown again rather
        # than rebuilt: a job a tool started may still deliver its result.
        tools.show()
        page = tools.open_tool(tool_id)
        tools.raise_()
        tools.activateWindow()
        return page

    def inspect_current(self) -> None:
        source = dicom_candidate(self._current_source()) if self._current_source else None
        if source is None:
            return
        page = self.open("dicom_editor")
        header_source = getattr(page, "source", None)
        if header_source is None:
            return
        if source.is_dir():
            header_source.open_folder(source)
        else:
            header_source.open_file(source)

    def _refresh(self) -> None:
        if self._inspect_action is not None and self._current_source is not None:
            self._inspect_action.setEnabled(dicom_candidate(self._current_source()) is not None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        # Close follows the workflow window, but only once its own close
        # was accepted: an "unsaved changes - cancel" keeps both open.
        if watched is self._window and event.type() == QEvent.Type.Close:
            QTimer.singleShot(0, self._close_if_owner_closed)
        return False

    def _close_if_owner_closed(self) -> None:
        if self._tools is not None and not self._window.isVisible():
            self._tools.close()

    def retranslate_ui(self) -> None:
        self.menu.setTitle(translator.tr("launcher.tools"))
        for spec in TOOLS:
            self._actions[spec.tool_id].setText(translator.tr(spec.title_key))
        if self._inspect_action is not None:
            self._inspect_action.setText(translator.tr("tools.menu.inspect_current"))


__all__ = ["UtilityToolsMenu", "dicom_candidate"]
