"""The Utility Tools window: the catalog on the left, the chosen tool on the right.

A fourth top-level window beside the three workflows, opened from the
launcher, and like them it returns there on Close; a workflow window can
also open one of its own from its Tools menu (qicom.gui.tools.workflow_menu),
which then only closes. It holds no project and
no analysis: each tool is self-contained, so switching between them keeps
whatever each one has open rather than asking to discard it.

A tool's widget is built the first time it is picked (see
qicom.gui.tools.catalog), and the window does nothing tool-specific beyond
two optional signals a tool may expose: ``error_occurred`` lands in the
window's error log and bug report, shown in a message box; ``error_logged``
lands in the same log with only a status-bar line, for a tool that has
already put the matter in front of the user; ``status_message`` goes to the
status bar.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import unhandled_error_hub
from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.preferences import remember_language, remember_theme
from qicom.gui.resources import app_icon
from qicom.gui.theming import SUPPORTED_THEMES, theme_manager
from qicom.gui.tools.catalog import TOOLS, ToolSpec, tool_spec
from qicom.gui.widgets.about_dialog import AboutDialog
from qicom.gui.widgets.adaptive_layout import DpiIconLabel
from qicom.gui.widgets.bug_report_dialog import BugReportDialog
from qicom.gui.widgets.errors_panel import ErrorRecord, as_reported_errors, new_error_record
from qicom.gui.widgets.settings_dialog import SettingsDialog
from qicom.gui.workers import has_active_jobs
from qicom.i18n import SUPPORTED_LANGUAGES, translator


class ToolsMainWindow(QMainWindow):
    """The catalog of utility tools and the one currently in use."""

    def __init__(
        self,
        initial_tool: str | None = None,
        parent: QWidget | None = None,
        *,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowIcon(app_icon())
        self.return_to_launcher = False
        # Opened from a workflow window's Tools menu rather than from the
        # launcher: closing it only closes it, and there is no Exit.
        self._embedded = embedded
        self._error_records: list[ErrorRecord] = []
        self._pages: dict[str, QWidget] = {}

        self._catalog = QListWidget()
        self._catalog.setObjectName("toolsCatalog")
        for spec in TOOLS:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, spec.tool_id)
            self._catalog.addItem(item)
        self._catalog.currentRowChanged.connect(self._on_catalog_row_changed)

        self._tool_title = QLabel()
        self._tool_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        self._tool_description = QLabel()
        self._tool_description.setObjectName("panelSectionHint")
        self._tool_description.setWordWrap(True)
        self._stack = QStackedWidget()
        tool_area = QWidget()
        tool_layout = QVBoxLayout(tool_area)
        tool_layout.addWidget(self._tool_title)
        tool_layout.addWidget(self._tool_description)
        tool_layout.addWidget(self._stack, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._catalog)
        splitter.addWidget(tool_area)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 1140])
        self.setCentralWidget(splitter)

        self._build_menus()
        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(self._refresh_icons)
        unhandled_error_hub().error_raised.connect(self._report_unhandled_error)
        self.retranslate_ui()
        self._refresh_icons(theme_manager.theme)
        self.open_tool(initial_tool or TOOLS[0].tool_id)
        self.resize(1400, 900)

    # -- the catalog ---------------------------------------------------------

    @property
    def current_tool(self) -> str | None:
        item = self._catalog.currentItem()
        return None if item is None else str(item.data(Qt.ItemDataRole.UserRole))

    def tool_widget(self, tool_id: str) -> QWidget | None:
        """The tool's widget once it has been opened, else None."""
        return self._pages.get(tool_id)

    def open_tool(self, tool_id: str) -> QWidget:
        spec = tool_spec(tool_id)
        self._catalog.setCurrentRow(TOOLS.index(spec))
        return self._page(spec)

    def _on_catalog_row_changed(self, row: int) -> None:
        if 0 <= row < len(TOOLS):
            self._show(TOOLS[row])

    def _page(self, spec: ToolSpec) -> QWidget:
        page = self._pages.get(spec.tool_id)
        if page is None:
            page = spec.create_widget()
            error_signal = getattr(page, "error_occurred", None)
            if error_signal is not None:
                error_signal.connect(self._show_error)
            logged_signal = getattr(page, "error_logged", None)
            if logged_signal is not None:
                logged_signal.connect(self._report_unhandled_error)
            status_signal = getattr(page, "status_message", None)
            if status_signal is not None:
                status_signal.connect(lambda message: self.statusBar().showMessage(message, 8000))
            self._pages[spec.tool_id] = page
            self._stack.addWidget(page)
        return page

    def _show(self, spec: ToolSpec) -> None:
        self._stack.setCurrentWidget(self._page(spec))
        self._tool_title.setText(translator.tr(spec.title_key))
        self._tool_description.setText(translator.tr(spec.description_key))

    def _refresh_icons(self, theme: str) -> None:
        color = ink_color(theme)
        for row, spec in enumerate(TOOLS):
            item = self._catalog.item(row)
            if item is not None:
                item.setIcon(paint_icon(spec.icon, color))

    # -- menus -----------------------------------------------------------------

    def _build_menus(self) -> None:
        container = QWidget()
        bar_layout = QHBoxLayout(container)
        bar_layout.setContentsMargins(8, 2, 10, 2)
        bar_layout.setSpacing(8)
        bar_layout.addWidget(DpiIconLabel(app_icon(), 20))
        menu_bar = QMenuBar()
        bar_layout.addWidget(menu_bar, 1)
        self.setMenuWidget(container)

        self._file_menu = menu_bar.addMenu("")
        assert self._file_menu is not None
        self._close_action = QAction(self)
        self._close_action.triggered.connect(self._close_tools)
        self._exit_action = QAction(self)
        self._exit_action.triggered.connect(self.close)
        self._file_menu.addAction(self._close_action)
        if not self._embedded:
            self._file_menu.addAction(self._exit_action)

        self._view_menu = menu_bar.addMenu("")
        assert self._view_menu is not None
        self._theme_actions: dict[str, QAction] = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for theme in SUPPORTED_THEMES:
            action = QAction(self, checkable=True)
            action.triggered.connect(lambda _checked, value=theme: self._set_theme(value))
            theme_group.addAction(action)
            self._view_menu.addAction(action)
            self._theme_actions[theme] = action

        self._tools_menu = menu_bar.addMenu("")
        assert self._tools_menu is not None
        self._language_menu = self._tools_menu.addMenu("")
        assert self._language_menu is not None
        self._language_actions: dict[str, QAction] = {}
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        for language in SUPPORTED_LANGUAGES:
            action = QAction(self, checkable=True)
            action.triggered.connect(lambda _checked, value=language: remember_language(value))
            language_group.addAction(action)
            self._language_menu.addAction(action)
            self._language_actions[language] = action
        self._tools_menu.addSeparator()
        self._settings_action = QAction(self)
        self._settings_action.triggered.connect(lambda: SettingsDialog(self).exec())
        self._tools_menu.addAction(self._settings_action)

        self._help_menu = menu_bar.addMenu("")
        assert self._help_menu is not None
        self._bug_action = QAction(self)
        self._bug_action.triggered.connect(
            lambda: BugReportDialog(
                self,
                error_records=lambda: as_reported_errors(self._error_records),
                workflow="tools",
            ).exec()
        )
        self._help_menu.addAction(self._bug_action)
        self._about_action = QAction(self)
        self._about_action.triggered.connect(lambda: AboutDialog(self).exec())
        self._help_menu.addSeparator()
        self._help_menu.addAction(self._about_action)

    def _set_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            remember_theme(app, theme)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("tools.window.title"))
        for row, spec in enumerate(TOOLS):
            item = self._catalog.item(row)
            if item is not None:
                item.setText(translator.tr(spec.title_key))
                item.setToolTip(translator.tr(spec.description_key))
        current = self.current_tool
        if current is not None:
            spec = tool_spec(current)
            self._tool_title.setText(translator.tr(spec.title_key))
            self._tool_description.setText(translator.tr(spec.description_key))
        self._file_menu.setTitle(translator.tr("menu.file"))
        self._close_action.setText(translator.tr("tools.menu.close"))
        self._exit_action.setText(translator.tr("menu.file.exit"))
        self._view_menu.setTitle(translator.tr("menu.view"))
        for theme, action in self._theme_actions.items():
            action.setText(translator.tr(f"menu.view.theme_{theme}"))
            action.setChecked(theme == theme_manager.theme)
        self._tools_menu.setTitle(translator.tr("menu.tools"))
        self._language_menu.setTitle(translator.tr("menu.language"))
        for language, action in self._language_actions.items():
            action.setText(translator.tr(f"language.{language}"))
            action.setChecked(language == translator.language)
        self._settings_action.setText(translator.tr("settings.title"))
        self._help_menu.setTitle(translator.tr("menu.help"))
        self._bug_action.setText(translator.tr("menu.help.report_bug"))
        self._about_action.setText(translator.tr("menu.help.about"))

    # -- errors and closing ------------------------------------------------------

    def _show_error(self, message: str) -> None:
        self._error_records.append(new_error_record(message))
        QMessageBox.warning(self, translator.tr("tools.window.title"), message)

    def _report_unhandled_error(self, message: str) -> None:
        """An exception that escaped a slot: kept for the bug report and shown
        in the status bar, without a modal box on top of whatever failed."""
        self._error_records.append(new_error_record(message))
        self.statusBar().showMessage(message, 8000)

    def error_records(self) -> list[ErrorRecord]:
        return list(self._error_records)

    @property
    def embedded(self) -> bool:
        return self._embedded

    def _close_tools(self) -> None:
        self.close()
        if not self.isVisible() and not self._embedded:
            self.return_to_launcher = True

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802
        # From the launcher, a job still running would deliver its result to
        # a window the launcher loop is about to drop; jobs here are short, so
        # the close waits for them. An embedded window is only hidden and
        # reused (see workflow_menu), and must not wait for the workflow's
        # own jobs, which has_active_jobs also counts.
        if not self._embedded and has_active_jobs():
            self.statusBar().showMessage(translator.tr("tools.busy"), 5000)
            if event is not None:
                event.ignore()
            return
        if event is not None:
            event.accept()


__all__ = ["ToolsMainWindow"]
