"""Startup launcher for the three independent QICOM workflows and the Utility Tools.

The initial page offers Quantitative, Qualitative, Radiomics and Utility
Tools.  The radiomics branch has its own New/Load Project page and opens a
dedicated window/controller; it never falls through to quantitative
analysis.  The Utility Tools page lists the tool catalog (see
qicom.gui.tools.catalog) and opens the Tools window on the one picked.

Qualitative's three actions are deliberately separate, not one "Load"
that guesses what the user wants: Take Evaluation always opens locked,
answer-only (view images, answer questions, generate a report - nothing
else); New/Edit Evaluation always open fully editable (build/modify
questions and pages). Which one a window ends up in is decided here, by
which button was clicked - never re-guessed later from whose machine
saved the file (see qicom.session.evaluation_package's own module
docstring for why that old auto-detection broke).

The logo/title/disclaimer header, the language button, and the Back button
are all fixed in place on this single screen - only the middle button
cluster (self._stack) swaps content between the mode-choice pair, the
quantitative pair, and the qualitative quartet. Explicit design ask:
picking a mode must not feel like navigating to a different screen/tab,
just like the same two buttons turned into two different ones (and, for
qualitative, grew two more).

The Recent Project shortcut list exists only inside the quantitative
page.  The initial mode-choice home stays focused on selecting a workflow,
and neither qualitative nor radiomics surfaces quantitative history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.preferences import remember_language
from qicom.gui.resources import app_icon
from qicom.gui.theming import theme_manager
from qicom.gui.tools.catalog import TOOLS
from qicom.gui.widgets.adaptive_layout import DpiIconLabel, ScreenFitDialog, make_panel_scrollable
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.session.evaluator_accounts import EvaluatorAccount
from qicom.session.evaluator_drafts import DraftStore, DraftSummary, discard_legacy_pending_index
from qicom.session.protected_files import (
    AccessKey,
    AccessKeyring,
    is_protected_package,
    read_package_header,
)
from qicom.session.recent_projects import RecentProject, load_recent_projects

MODE_QUANTITATIVE = "quantitative"
MODE_QUALITATIVE = "qualitative"
MODE_RADIOMICS = "radiomics"
MODE_TOOLS = "tools"
ACTION_NEW = "new"
ACTION_LOAD = "load"
# Qualitative-only: New/Load Project (quantitative) stay ACTION_NEW/
# ACTION_LOAD unchanged - splitting "create new evaluation" into a create
# half and a modify half (ACTION_EDIT) plus a third, deliberately more
# restrictive "just answer it" half (ACTION_TAKE) is a qualitative-specific
# ask (see LauncherDialog's own module docstring), not something the
# quantitative side needs.
ACTION_EDIT = "edit"
ACTION_TAKE = "take"
# Tools only: open the Tools window on LauncherChoice.tool_id.
ACTION_OPEN_TOOL = "open_tool"

# Indices into the button-cluster self._stack - the only thing that changes
# between the states this single screen can be in.
_PAGE_MODE = 0
_PAGE_QUANTITATIVE = 1
_PAGE_QUALITATIVE = 2
_PAGE_RADIOMICS = 3
_PAGE_TOOLS = 4

# How many of recent_projects.MAX_RECENT_PROJECTS to actually show here -
# keeps this still-small startup dialog from growing tall on a machine with
# a long history; the rest stay on disk, just not surfaced. Quantitative-
# only (see LauncherDialog's own docstring - past qualitative evaluations
# must never appear on this screen), so there's only ever this one list to
# size for.
_VISIBLE_RECENT_PROJECTS = 3

_MODE_ICONS = {
    MODE_QUANTITATIVE: "chart_panel",
    MODE_QUALITATIVE: "eye",
    MODE_RADIOMICS: "heatmap_panel",
    MODE_TOOLS: "toolbox",
}


@dataclass(frozen=True)
class LauncherChoice:
    mode: str  # MODE_QUANTITATIVE | MODE_QUALITATIVE | MODE_RADIOMICS | MODE_TOOLS
    action: str  # ACTION_NEW | ACTION_LOAD | ACTION_EDIT | ACTION_TAKE | ACTION_OPEN_TOOL
    project_path: Path | None = None  # set for every action except ACTION_NEW
    # ACTION_TAKE only: who logged in (None takes it as a guest, keeping
    # nothing), and the evaluation they picked from their own list.
    account: EvaluatorAccount | None = None
    draft: DraftSummary | None = None
    # ACTION_TAKE/ACTION_EDIT: the key of a package protected by an access
    # code, asked for here so that a cancelled code never leaves an empty
    # window behind.
    access: AccessKey | None = None
    # ACTION_OPEN_TOOL only: which catalog entry the Tools window opens on.
    tool_id: str | None = None


class LauncherDialog(ScreenFitDialog):
    """A small modal that fits the available screen, shown before any main window exists.
    `choice` stays None if the user closes the dialog without picking
    anything - the caller (qicom.gui.app.main) quits cleanly in that case
    rather than falling back to some default mode.

    Picking "Load Project"/"Take"/"Edit Evaluation" opens the native file
    picker right there (mirrors OpenImagesDialog's own "the dialog
    performs the actual picking, cancelling the native picker just
    returns to this dialog" convention) - a project or an evaluation is
    always one self-contained .zip file, never a folder (see
    qicom.session.project_folder/qicom.session.evaluation_package's own
    module docstrings). The launcher only closes once something was
    actually picked. A "Recent Project" shortcut list (see qicom.session.
    recent_projects) lets a returning user skip that picker entirely for
    anything opened recently - quantitative only, by explicit request: past
    qualitative evaluations must never appear on this, the app's very first
    screen. Edit Evaluation's own file picker still reaches them directly;
    only the shortcut list is quantitative-only.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(520, 680)
        self.choice: LauncherChoice | None = None
        # Forward-declared: referenced by the mode-card buttons' click
        # handlers below, assigned further down once the pages themselves
        # exist.
        self._stack: QStackedWidget

        # A single button, top-right, popping up every SUPPORTED_LANGUAGES
        # choice - the same checkable-QActionGroup pattern MainWindow's own
        # Language menu uses, just in a QMenu instead of a QMenuBar entry
        # (this dialog has no menu bar). translator is a process-wide
        # singleton, so picking a language here also applies to whichever
        # window the launcher goes on to open.
        self._language_button = QToolButton()
        self._language_button.setObjectName("launcherLanguageButton")
        self._language_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        language_menu = QMenu(self._language_button)
        self._language_button.setMenu(language_menu)
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        self._language_actions: dict[str, QAction] = {}
        for language in SUPPORTED_LANGUAGES:
            action = QAction(self)
            action.setCheckable(True)
            action.setChecked(language == translator.language)
            action.triggered.connect(lambda _checked, lang=language: remember_language(lang))
            language_group.addAction(action)
            language_menu.addAction(action)
            self._language_actions[language] = action
        # Next to it, the way into the About window: the launcher is the
        # one screen every session passes through, and it is also where the
        # research-use disclaimer is shown - so the full statement of what
        # QICOM is, what it may be used for and how to cite it has to be
        # reachable from here, not only from a main window's Help menu.
        self._about_button = QToolButton()
        self._about_button.setObjectName("launcherLanguageButton")
        self._about_button.setText("\u24d8")
        self._about_button.clicked.connect(self._open_about_dialog)
        self._settings_button = QToolButton()
        self._settings_button.setObjectName("launcherLanguageButton")
        self._settings_button.clicked.connect(self._open_settings_dialog)

        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(self._about_button)
        top_row.addWidget(self._settings_button)
        top_row.addWidget(self._language_button)

        # QICOM's own logo, the title, and the disclaimer sit directly in
        # the dialog's own top-level layout (not inside any button-cluster
        # page) - fixed in place across every state, see this class's
        # own module-level docstring for why.
        logo_label = DpiIconLabel(app_icon(), 64)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label = QLabel()
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet("font-size: 20px; font-weight: 700;")

        self._subtitle_label = QLabel()
        self._subtitle_label.setObjectName("launcherSubtitle")
        self._subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle_label.setWordWrap(True)

        # Shown on every launch (not just once) since this dialog is the one
        # screen every session passes through before either main window
        # opens - keeps the "research tool, not a diagnostic device" scope
        # visible regardless of language or how long ago the user last saw it.
        # Coloured by each theme (QLabel#launcherDisclaimer): the palette's
        # "mid" it used was a dark grey on the gray and dark backgrounds,
        # which left the statement unreadable exactly where it must be read.
        self._disclaimer_label = QLabel()
        self._disclaimer_label.setObjectName("launcherDisclaimer")
        self._disclaimer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._disclaimer_label.setWordWrap(True)

        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        header_layout.addWidget(logo_label)
        header_layout.addWidget(self._title_label)
        header_layout.addWidget(self._subtitle_label)
        header_layout.addWidget(self._disclaimer_label)

        # --- The button-cluster pages - the only thing that changes
        # between states. Each page pins its content to the top (a single
        # trailing stretch, no leading one - explicit ask: everything
        # starts right below the header, nothing centered/pushed down) and
        # relies on self._stack being tall enough for the tallest page
        # (Qt sizes a QStackedWidget to fit all its pages, i.e. the
        # qualitative one here), so switching states never visibly resizes
        # or shifts the header/language button/Back button on screen.

        self._quantitative_button = QPushButton()
        self._qualitative_button = QPushButton()
        self._radiomics_button = QPushButton()
        self._tools_button = QPushButton()
        for card in (
            self._quantitative_button,
            self._qualitative_button,
            self._radiomics_button,
            self._tools_button,
        ):
            card.setObjectName("launcherModeCard")
            card.setIconSize(QSize(22, 22))
        self._quantitative_button.clicked.connect(self._show_quantitative_buttons)
        self._qualitative_button.clicked.connect(self._show_qualitative_buttons)
        self._radiomics_button.clicked.connect(self._show_radiomics_buttons)
        self._tools_button.clicked.connect(self._show_tools_buttons)
        self._quantitative_description = QLabel()
        self._qualitative_description = QLabel()
        self._radiomics_description = QLabel()
        self._tools_description = QLabel()
        for description in (
            self._quantitative_description,
            self._qualitative_description,
            self._radiomics_description,
            self._tools_description,
        ):
            description.setObjectName("launcherModeDescription")
            description.setWordWrap(True)
        mode_buttons_page = QWidget()
        mode_buttons_layout = QVBoxLayout(mode_buttons_page)
        mode_buttons_layout.setContentsMargins(0, 0, 0, 0)
        mode_buttons_layout.addWidget(self._quantitative_button)
        mode_buttons_layout.addWidget(self._quantitative_description)
        mode_buttons_layout.addSpacing(5)
        mode_buttons_layout.addWidget(self._qualitative_button)
        mode_buttons_layout.addWidget(self._qualitative_description)
        mode_buttons_layout.addSpacing(5)
        mode_buttons_layout.addWidget(self._radiomics_button)
        mode_buttons_layout.addWidget(self._radiomics_description)
        mode_buttons_layout.addSpacing(5)
        mode_buttons_layout.addWidget(self._tools_button)
        mode_buttons_layout.addWidget(self._tools_description)
        mode_buttons_layout.addStretch(1)

        self._new_project_button = QPushButton()
        self._load_project_button = QPushButton()
        self._new_project_button.clicked.connect(self._on_new_project_clicked)
        self._load_project_button.clicked.connect(self._on_load_project_clicked)
        (
            self._recent_quantitative_heading,
            self._recent_quantitative_layout,
            self._recent_quantitative_section,
        ) = self._build_recent_section()
        quantitative_buttons_page = QWidget()
        quantitative_buttons_layout = QVBoxLayout(quantitative_buttons_page)
        quantitative_buttons_layout.setContentsMargins(0, 0, 0, 0)
        quantitative_buttons_layout.addWidget(self._new_project_button)
        quantitative_buttons_layout.addWidget(self._load_project_button)
        quantitative_buttons_layout.addSpacing(12)
        quantitative_buttons_layout.addWidget(self._recent_quantitative_section)
        quantitative_buttons_layout.addStretch(1)

        # Take Evaluation goes first and stands out (its own object name,
        # styled green in both themes' QSS) - most people who ever open
        # this app in qualitative mode are here to answer an evaluation
        # someone else authored, not to build one, so that's the action
        # that should catch the eye first rather than blend in with
        # New/Edit/Aggregate. No Recent Project section here at all - past
        # qualitative evaluations must never be shown (see this class's own
        # docstring), and the quantitative recents shown on the other two
        # pages have no business appearing here.
        self._take_evaluation_button = QPushButton()
        self._take_evaluation_button.setObjectName("launcherTakeEvaluationButton")
        self._new_evaluation_button = QPushButton()
        self._edit_evaluation_button = QPushButton()
        self._aggregate_results_button = QPushButton()
        self._take_evaluation_button.clicked.connect(self._on_take_evaluation_clicked)
        self._new_evaluation_button.clicked.connect(self._on_new_evaluation_clicked)
        self._edit_evaluation_button.clicked.connect(self._on_edit_evaluation_clicked)
        self._aggregate_results_button.clicked.connect(self._on_aggregate_results_clicked)
        qualitative_buttons_page = QWidget()
        qualitative_buttons_layout = QVBoxLayout(qualitative_buttons_page)
        qualitative_buttons_layout.setContentsMargins(0, 0, 0, 0)
        qualitative_buttons_layout.addWidget(self._take_evaluation_button)
        qualitative_buttons_layout.addSpacing(8)
        qualitative_buttons_layout.addWidget(self._new_evaluation_button)
        qualitative_buttons_layout.addWidget(self._edit_evaluation_button)
        qualitative_buttons_layout.addWidget(self._aggregate_results_button)
        qualitative_buttons_layout.addStretch(1)

        self._new_radiomics_project_button = QPushButton()
        self._load_radiomics_project_button = QPushButton()
        self._new_radiomics_project_button.clicked.connect(self._on_new_radiomics_project_clicked)
        self._load_radiomics_project_button.clicked.connect(self._on_load_radiomics_project_clicked)
        radiomics_buttons_page = QWidget()
        radiomics_buttons_layout = QVBoxLayout(radiomics_buttons_page)
        radiomics_buttons_layout.setContentsMargins(0, 0, 0, 0)
        radiomics_buttons_layout.addWidget(self._new_radiomics_project_button)
        radiomics_buttons_layout.addWidget(self._load_radiomics_project_button)
        radiomics_buttons_layout.addStretch(1)

        # One compact button per catalog entry, the description as its
        # tooltip: the stack is as tall as its tallest page, and a catalog
        # with a paragraph per tool made every other page scroll. One column,
        # because two did not fit the longer translated titles.
        self._tool_buttons: dict[str, QPushButton] = {}
        self._tools_hint = QLabel()
        self._tools_hint.setObjectName("launcherModeDescription")
        self._tools_hint.setWordWrap(True)
        tools_grid = QGridLayout()
        tools_grid.setContentsMargins(0, 0, 0, 0)
        for index, spec in enumerate(TOOLS):
            button = QPushButton()
            button.setIconSize(QSize(18, 18))
            button.setStyleSheet("text-align: left; padding: 4px 10px;")
            button.clicked.connect(lambda _checked, tool=spec.tool_id: self._on_tool_clicked(tool))
            tools_grid.addWidget(button, index, 0)
            self._tool_buttons[spec.tool_id] = button
        tools_buttons_page = QWidget()
        tools_buttons_layout = QVBoxLayout(tools_buttons_page)
        tools_buttons_layout.setContentsMargins(0, 0, 0, 0)
        tools_buttons_layout.addWidget(self._tools_hint)
        tools_buttons_layout.addLayout(tools_grid)
        tools_buttons_layout.addStretch(1)

        self._stack = QStackedWidget()
        self._stack.addWidget(mode_buttons_page)
        self._stack.addWidget(quantitative_buttons_page)
        self._stack.addWidget(qualitative_buttons_page)
        self._stack.addWidget(radiomics_buttons_page)
        self._stack.addWidget(tools_buttons_page)
        self._stack.currentChanged.connect(self._apply_page_visibility)

        self._back_button = QPushButton()
        self._back_button.clicked.connect(self._show_mode_page)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addLayout(header_layout)
        layout.addSpacing(8)
        layout.addWidget(self._stack, 1)
        layout.addWidget(self._back_button)

        make_panel_scrollable(self)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_recent_projects()
        self._refresh_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_icons)
        self._apply_page_visibility()

    def _build_recent_section(self) -> tuple[QLabel, QVBoxLayout, QWidget]:
        """Build the quantitative page's Recent Project shortcut list."""
        heading = QLabel()
        heading.setStyleSheet("font-size: 11px; font-weight: 700;")
        row_layout = QVBoxLayout()
        row_layout.setSpacing(4)
        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.addWidget(heading)
        section_layout.addLayout(row_layout)
        return heading, row_layout, section

    def _apply_page_visibility(self) -> None:
        """The Back button lives outside self._stack (see __init__'s own
        comment) - its visibility still needs to track which button-
        cluster page is current, so this runs on every self._stack.
        currentChanged as well as once at construction (currentChanged
        only fires on an actual index *change*, never for the initial
        default index)."""
        self._back_button.setVisible(self._stack.currentIndex() != _PAGE_MODE)

    def _refresh_recent_projects(self) -> None:
        all_entries = load_recent_projects()
        recent_quantitative = [e for e in all_entries if e.mode == MODE_QUANTITATIVE][
            :_VISIBLE_RECENT_PROJECTS
        ]
        self._fill_recent_section(
            self._recent_quantitative_layout,
            self._recent_quantitative_section,
            recent_quantitative,
        )

    def _fill_recent_section(
        self, layout: QVBoxLayout, section: QWidget, entries: list[RecentProject]
    ) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
        for entry in entries:
            button = QPushButton(entry.name)
            button.setObjectName("launcherRecentButton")
            button.setIcon(paint_icon("folder", ink_color(theme_manager.theme)))
            button.setToolTip(f"{entry.path}  ·  {entry.opened_at}")
            button.clicked.connect(lambda _checked, e=entry: self._on_recent_project_clicked(e))
            layout.addWidget(button)
        section.setVisible(bool(entries))

    def _refresh_icons(self, theme: str) -> None:
        color = ink_color(theme)
        self._quantitative_button.setIcon(paint_icon(_MODE_ICONS[MODE_QUANTITATIVE], color))
        self._qualitative_button.setIcon(paint_icon(_MODE_ICONS[MODE_QUALITATIVE], color))
        self._radiomics_button.setIcon(paint_icon(_MODE_ICONS[MODE_RADIOMICS], color))
        self._tools_button.setIcon(paint_icon(_MODE_ICONS[MODE_TOOLS], color))
        for spec in TOOLS:
            self._tool_buttons[spec.tool_id].setIcon(paint_icon(spec.icon, color))
        self._settings_button.setIcon(paint_icon("tune", color))
        for i in range(self._recent_quantitative_layout.count()):
            item = self._recent_quantitative_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QPushButton):
                widget.setIcon(paint_icon("folder", color))

    def _on_recent_project_clicked(self, entry: RecentProject) -> None:
        # Only quantitative entries are ever shown/clickable here now (see
        # this dialog's own mode_page construction) - always ACTION_LOAD.
        self.choice = LauncherChoice(
            mode=MODE_QUANTITATIVE, action=ACTION_LOAD, project_path=entry.path
        )
        self.accept()

    def _show_mode_page(self) -> None:
        self._stack.setCurrentIndex(_PAGE_MODE)

    def _show_quantitative_buttons(self) -> None:
        self._stack.setCurrentIndex(_PAGE_QUANTITATIVE)

    def _show_qualitative_buttons(self) -> None:
        self._stack.setCurrentIndex(_PAGE_QUALITATIVE)

    def _show_radiomics_buttons(self) -> None:
        self._stack.setCurrentIndex(_PAGE_RADIOMICS)

    def _show_tools_buttons(self) -> None:
        self._stack.setCurrentIndex(_PAGE_TOOLS)

    def _on_tool_clicked(self, tool_id: str) -> None:
        self.choice = LauncherChoice(mode=MODE_TOOLS, action=ACTION_OPEN_TOOL, tool_id=tool_id)
        self.accept()

    def _on_new_project_clicked(self) -> None:
        self.choice = LauncherChoice(mode=MODE_QUANTITATIVE, action=ACTION_NEW)
        self.accept()

    def _on_load_project_clicked(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("launcher.load_project_title"),
            "",
            translator.tr("file_filter.project"),
        )
        if file_path:
            # Not recorded here - a file merely *picked* isn't necessarily
            # one that actually opens cleanly. MainWindow.open_project_zip
            # (which _build_window calls next with this exact path)
            # records it once the load has actually succeeded.
            self.choice = LauncherChoice(
                mode=MODE_QUANTITATIVE, action=ACTION_LOAD, project_path=Path(file_path)
            )
            self.accept()

    def _on_new_radiomics_project_clicked(self) -> None:
        self.choice = LauncherChoice(mode=MODE_RADIOMICS, action=ACTION_NEW)
        self.accept()

    def _on_load_radiomics_project_clicked(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("launcher.load_radiomics_project_title"),
            "",
            translator.tr("file_filter.radiomics_project"),
        )
        if file_path:
            self.choice = LauncherChoice(
                mode=MODE_RADIOMICS, action=ACTION_LOAD, project_path=Path(file_path)
            )
            self.accept()

    def _on_new_evaluation_clicked(self) -> None:
        self.choice = LauncherChoice(mode=MODE_QUALITATIVE, action=ACTION_NEW)
        self.accept()

    def _on_edit_evaluation_clicked(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("launcher.edit_evaluation_title"),
            "",
            translator.tr("file_filter.evaluation"),
        )
        if file_path:
            unlocked, access = self._unlock(Path(file_path), AccessKeyring())
            if not unlocked:
                return
            # See _on_load_project_clicked's own comment - recorded once
            # QualitativeMainWindow.open_evaluation_for_editing actually
            # succeeds, not merely on picking a file here.
            self.choice = LauncherChoice(
                mode=MODE_QUALITATIVE,
                action=ACTION_EDIT,
                project_path=Path(file_path),
                access=access,
            )
            self.accept()

    def _unlock(self, path: Path, keyring: AccessKeyring) -> tuple[bool, AccessKey | None]:
        """(True, None) for a package without an access code; (True, key)
        once its code was given; (False, None) when the evaluator cancelled
        or the file is not a readable protected package."""
        if not is_protected_package(path):
            return True, None
        try:
            header = read_package_header(path)
        except ValueError:
            QMessageBox.warning(
                self, translator.tr("app.title"), translator.tr("access_code.unreadable")
            )
            return False, None
        from qicom.gui.widgets.access_code_dialog import ask_access_key

        access = ask_access_key(self, keyring, header, path.stem)
        return access is not None, access

    def _on_take_evaluation_clicked(self) -> None:
        """Log in first (see EvaluatorLoginDialog), then offer the
        evaluator's own evaluations (see MyEvaluationsDialog); the file
        dialog is what "open another evaluation" leads to, and what a guest
        goes straight to."""
        from qicom.gui.widgets.evaluator_login_dialog import EvaluatorLoginDialog

        login = EvaluatorLoginDialog(self)
        if login.exec() != EvaluatorLoginDialog.DialogCode.Accepted:
            return
        account = login.account
        if account is None:
            self._choose_evaluation_file(None)
            return
        discard_legacy_pending_index()
        from qicom.gui.widgets.my_evaluations_dialog import MyEvaluationsDialog

        while True:
            dialog = MyEvaluationsDialog(account, self)
            if dialog.exec() != MyEvaluationsDialog.DialogCode.Accepted:
                return
            if dialog.resume is not None and dialog.package_path is not None:
                keyring = AccessKeyring()
                stored = DraftStore(account).package_key(dialog.resume.draft_file)
                if stored is not None:
                    keyring.add(stored)
                unlocked, access = self._unlock(dialog.package_path, keyring)
                if not unlocked:
                    continue
                self.choice = LauncherChoice(
                    mode=MODE_QUALITATIVE,
                    action=ACTION_TAKE,
                    project_path=dialog.package_path,
                    account=account,
                    draft=dialog.resume,
                    access=access,
                )
                self.accept()
                return
            # "Open another evaluation": cancelling its file dialog comes
            # back to the list, not to a second login.
            if self._choose_evaluation_file(account):
                return

    def _choose_evaluation_file(self, account: EvaluatorAccount | None) -> bool:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("launcher.take_evaluation_title"),
            "",
            translator.tr("file_filter.evaluation"),
        )
        if not file_path:
            return False
        unlocked, access = self._unlock(Path(file_path), AccessKeyring())
        if not unlocked:
            return False
        self.choice = LauncherChoice(
            mode=MODE_QUALITATIVE,
            action=ACTION_TAKE,
            project_path=Path(file_path),
            account=account,
            access=access,
        )
        self.accept()
        return True

    def _open_about_dialog(self) -> None:
        from qicom.gui.widgets.about_dialog import AboutDialog

        AboutDialog(self).exec()

    def _open_settings_dialog(self) -> None:
        from qicom.gui.widgets.settings_dialog import SettingsDialog

        SettingsDialog(self).exec()

    def _on_aggregate_results_clicked(self) -> None:
        """Doesn't set `choice`/close the launcher - unlike New/Edit/Take
        Evaluation, aggregating needs no rating UI open at all, just a
        package + answer files + an output path (see AggregationDialog's
        own docstring), so it's a self-contained side action the user
        returns from straight back to this same qualitative page."""
        from qicom.gui.widgets.aggregation_dialog import AggregationDialog

        dialog = AggregationDialog(self)
        dialog.exec()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("launcher.title"))
        self._title_label.setText(translator.tr("launcher.title"))
        self._subtitle_label.setText(translator.tr("launcher.subtitle"))
        self._disclaimer_label.setText(translator.tr("launcher.research_disclaimer"))
        # The base subtag only: "zh-Hans" would not fit the button, and the
        # menu it opens names every language in full.
        self._language_button.setText(translator.language.split("-")[0].upper())
        self._language_button.setToolTip(translator.tr("launcher.change_language"))
        self._about_button.setToolTip(translator.tr("about.title"))
        self._settings_button.setToolTip(translator.tr("settings.title"))
        for language, action in self._language_actions.items():
            action.setText(translator.tr(f"language.{language}"))
            action.setChecked(language == translator.language)
        self._quantitative_button.setText(translator.tr("launcher.quantitative"))
        self._qualitative_button.setText(translator.tr("launcher.qualitative"))
        self._radiomics_button.setText(translator.tr("launcher.radiomics"))
        self._tools_button.setText(translator.tr("launcher.tools"))
        self._quantitative_description.setText(translator.tr("launcher.quantitative_description"))
        self._qualitative_description.setText(translator.tr("launcher.qualitative_description"))
        self._radiomics_description.setText(translator.tr("launcher.radiomics_description"))
        self._tools_description.setText(translator.tr("launcher.tools_description"))
        self._tools_hint.setText(translator.tr("launcher.tools_hint"))
        for spec in TOOLS:
            button = self._tool_buttons[spec.tool_id]
            button.setText(translator.tr(spec.title_key))
            button.setToolTip(translator.tr(spec.description_key))
        self._recent_quantitative_heading.setText(translator.tr("launcher.recent_project"))
        self._new_project_button.setText(translator.tr("launcher.new_project"))
        self._load_project_button.setText(translator.tr("launcher.load_project"))
        self._back_button.setText(translator.tr("launcher.back"))
        self._take_evaluation_button.setText(translator.tr("launcher.take_evaluation"))
        self._new_evaluation_button.setText(translator.tr("launcher.new_evaluation"))
        self._edit_evaluation_button.setText(translator.tr("launcher.edit_evaluation"))
        self._aggregate_results_button.setText(translator.tr("launcher.aggregate_results"))
        self._new_radiomics_project_button.setText(translator.tr("launcher.new_radiomics_project"))
        self._load_radiomics_project_button.setText(
            translator.tr("launcher.load_radiomics_project")
        )
