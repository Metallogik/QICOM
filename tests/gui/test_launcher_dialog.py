"""Tests for qicom.gui.widgets.launcher_dialog (LauncherDialog)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from PySide6.QtWidgets import QDialog, QPushButton
from pytestqt.qtbot import QtBot

from qicom.gui.tools.catalog import TOOLS
from qicom.gui.widgets import evaluator_login_dialog as login_module
from qicom.gui.widgets import my_evaluations_dialog as my_evaluations_module
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
    LauncherDialog,
)
from qicom.session import recent_projects as recent_projects_module
from qicom.session.evaluator_accounts import EvaluatorAccount, create_account
from qicom.session.evaluator_drafts import DraftStore, DraftSummary
from qicom.session.protected_files import (
    AccessKey,
    AccessKeyring,
    new_access_key,
    protect_package,
)
from qicom.session.recent_projects import record_recent_project

_ACCOUNT = EvaluatorAccount(account_id="a" * 32, username="mario", key=b"k" * 32)


class _FakeLogin:
    """Stands in for EvaluatorLoginDialog: logs in as `account`, goes on as
    a guest when it is None, or is cancelled."""

    DialogCode = QDialog.DialogCode
    account: EvaluatorAccount | None = _ACCOUNT
    cancelled = False

    def __init__(self, _parent: object = None) -> None:
        self.guest = self.account is None

    def exec(self) -> int:
        if self.cancelled:
            return QDialog.DialogCode.Rejected.value
        return QDialog.DialogCode.Accepted.value


class _FakeMyEvaluations:
    """Stands in for MyEvaluationsDialog; `script` is what the evaluator
    does on each showing: "other" or "cancel" (a test that resumes
    replaces exec)."""

    DialogCode = QDialog.DialogCode
    script: list[str] = []
    shown: list[EvaluatorAccount] = []
    draft: DraftSummary | None = None

    def __init__(self, account: EvaluatorAccount, _parent: object = None) -> None:
        type(self).shown.append(account)
        self.resume: DraftSummary | None = None
        self.open_other = False

    def exec(self) -> int:
        step = type(self).script.pop(0)
        if step == "cancel":
            return QDialog.DialogCode.Rejected.value
        self.open_other = True
        return QDialog.DialogCode.Accepted.value


def _draft(tmp_path: Path) -> DraftSummary:
    return DraftSummary(
        draft_file="b" * 32 + ".qdraft",
        package_path=tmp_path / "study.zip",
        name="study",
        answered=2,
        page_position=1,
        page_count=2,
        page_id=1,
        saved_at="2026-10-12T09:30:00+00:00",
    )


def _unexpected_file_dialog(*_args: object, **_kwargs: object) -> tuple[str, str]:
    raise AssertionError("the evaluator's own list comes first")


@pytest.fixture
def take_flow(monkeypatch: pytest.MonkeyPatch) -> type[_FakeMyEvaluations]:
    """Log in through the fakes; each test scripts the list."""

    class Login(_FakeLogin):
        pass

    class Mine(_FakeMyEvaluations):
        script: list[str] = []
        shown: list[EvaluatorAccount] = []

    monkeypatch.setattr(login_module, "EvaluatorLoginDialog", Login)
    monkeypatch.setattr(my_evaluations_module, "MyEvaluationsDialog", Mine)
    Mine.login = Login  # type: ignore[attr-defined]
    return Mine


@pytest.fixture(autouse=True)
def _fake_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Recent-projects entries live under platformdirs' real user-data
    directory by default - every test in this module must stay off it, or
    it would read/write the developer's own actual app data."""
    fake_dir = tmp_path / "platformdirs_home"
    monkeypatch.setattr(
        recent_projects_module.platformdirs, "user_data_dir", lambda *a, **k: str(fake_dir)
    )
    return fake_dir


class TestLauncherNavigation:
    def test_starts_on_the_mode_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert dialog._stack.currentIndex() == 0

    def test_clicking_quantitative_shows_the_quantitative_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        dialog._quantitative_button.click()

        assert dialog._stack.currentIndex() == 1

    def test_clicking_qualitative_shows_the_qualitative_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        dialog._qualitative_button.click()

        assert dialog._stack.currentIndex() == 2

    def test_clicking_radiomics_shows_the_radiomics_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        dialog._radiomics_button.click()

        assert dialog._stack.currentIndex() == 3

    def test_clicking_utility_tools_shows_the_tool_catalog(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        dialog._tools_button.click()

        assert dialog._stack.currentIndex() == 4
        assert list(dialog._tool_buttons) == [spec.tool_id for spec in TOOLS]

    def test_back_from_utility_tools_returns_to_mode_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._tools_button.click()

        dialog._back_button.click()

        assert dialog._stack.currentIndex() == 0

    def test_back_from_quantitative_returns_to_mode_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._quantitative_button.click()

        dialog._back_button.click()

        assert dialog._stack.currentIndex() == 0

    def test_back_from_qualitative_returns_to_mode_page(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._back_button.click()

        assert dialog._stack.currentIndex() == 0

    def test_closing_without_choosing_leaves_choice_none(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        dialog.reject()

        assert dialog.choice is None
        assert dialog.result() == QDialog.DialogCode.Rejected.value

    def test_launcher_offers_no_demo_bundle_action(self, qtbot: QtBot) -> None:
        """The synthetic bundle is a support tool, not a way into the app:
        it lives in Settings > Diagnostics and Help > Report a Bug now, so
        the launcher's front page must not carry it again."""
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert not hasattr(dialog, "_demo_button")
        labels = {button.text() for button in dialog.findChildren(QPushButton) if button.text()}
        assert not any("demo" in label.lower() for label in labels)


class TestSingleScreenFeel:
    """Explicit design ask: picking a mode must not feel like navigating to
    a different screen - the logo/title/disclaimer header stays fixed in
    place; only the button cluster (self._stack, which also carries its
    own embedded Recent Project copy on the mode/quantitative pages - see
    LauncherDialog's own module docstring) and the Back button's own
    visibility change."""

    def test_back_button_hidden_on_the_mode_page_only(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert dialog._back_button.isHidden()

        dialog._quantitative_button.click()
        assert not dialog._back_button.isHidden()

        dialog._back_button.click()
        assert dialog._back_button.isHidden()

        dialog._qualitative_button.click()
        assert not dialog._back_button.isHidden()

    def test_recent_project_is_hidden_on_home_and_shown_only_in_quantitative(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        record_recent_project(project_dir, MODE_QUANTITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog.show()  # QStackedWidget only actually hides non-current pages once shown
        assert not hasattr(dialog, "_recent_mode_section")
        assert not dialog._recent_quantitative_section.isVisible()

        dialog._quantitative_button.click()
        assert dialog._recent_quantitative_section.isVisible()

        dialog._back_button.click()
        dialog._qualitative_button.click()
        assert not dialog._recent_quantitative_section.isVisible()


class TestNewProjectAndNewEvaluation:
    def test_new_project_sets_choice_and_accepts(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._quantitative_button.click()

        dialog._new_project_button.click()

        assert dialog.choice is not None
        assert dialog.choice.mode == MODE_QUANTITATIVE
        assert dialog.choice.action == ACTION_NEW
        assert dialog.choice.project_path is None
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_new_evaluation_sets_choice_and_accepts(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._new_evaluation_button.click()

        assert dialog.choice is not None
        assert dialog.choice.mode == MODE_QUALITATIVE
        assert dialog.choice.action == ACTION_NEW
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_new_radiomics_project_is_a_separate_mode(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._radiomics_button.click()

        dialog._new_radiomics_project_button.click()

        assert dialog.choice == LauncherChoice(MODE_RADIOMICS, ACTION_NEW)
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    @pytest.mark.parametrize("tool_id", [spec.tool_id for spec in TOOLS])
    def test_picking_a_tool_opens_the_tools_window_on_it(self, qtbot: QtBot, tool_id: str) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._tools_button.click()

        dialog._tool_buttons[tool_id].click()

        assert dialog.choice == LauncherChoice(MODE_TOOLS, ACTION_OPEN_TOOL, tool_id=tool_id)
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_the_tool_page_follows_the_language(self, qtbot: QtBot) -> None:
        from qicom.i18n import translator

        previous = translator.language
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        try:
            translator.set_language("it")
            button = dialog._tool_buttons[TOOLS[0].tool_id]
            assert button.text() == translator.tr(TOOLS[0].title_key)
            assert button.toolTip() == translator.tr(TOOLS[0].description_key)
            assert dialog._tools_button.text() == "Strumenti di utilità"
        finally:
            translator.set_language(previous)


class TestLoadProjectAndLoadEvaluation:
    def test_load_radiomics_project_uses_its_own_choice(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "radiomics.zip"
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._radiomics_button.click()
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(zip_path), "QICOM Radiomics Project (*.zip)"),
        )

        dialog._load_radiomics_project_button.click()

        assert dialog.choice == LauncherChoice(MODE_RADIOMICS, ACTION_LOAD, zip_path)
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_load_project_with_a_chosen_zip_sets_choice_and_accepts(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "project.zip"
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._quantitative_button.click()
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(zip_path), "QICOM Project (*.zip)"),
        )

        dialog._load_project_button.click()

        assert dialog.choice is not None
        assert dialog.choice.mode == MODE_QUANTITATIVE
        assert dialog.choice.action == ACTION_LOAD
        assert dialog.choice.project_path == zip_path
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_cancelling_the_file_picker_stays_on_the_quantitative_page(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._quantitative_button.click()
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: ("", ""),
        )

        dialog._load_project_button.click()

        assert dialog.choice is None
        assert dialog._stack.currentIndex() == 1  # still on the quantitative page, not closed

    def test_edit_evaluation_with_a_chosen_zip_sets_choice_and_accepts(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(zip_path), "QICOM Evaluation (*.zip)"),
        )

        dialog._edit_evaluation_button.click()

        assert dialog.choice is not None
        assert dialog.choice.mode == MODE_QUALITATIVE
        assert dialog.choice.action == ACTION_EDIT
        assert dialog.choice.project_path == zip_path
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_take_evaluation_with_a_chosen_zip_sets_choice_and_accepts(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        """A guest goes straight to the file dialog, and takes it without an account."""
        take_flow.login.account = None  # type: ignore[attr-defined]
        zip_path = tmp_path / "evaluation.zip"
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(zip_path), "QICOM Evaluation (*.zip)"),
        )

        dialog._take_evaluation_button.click()

        assert dialog.choice is not None
        assert dialog.choice.mode == MODE_QUALITATIVE
        assert dialog.choice.action == ACTION_TAKE
        assert dialog.choice.project_path == zip_path
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_a_logged_in_evaluator_resumes_from_their_own_list(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        moved = tmp_path / "moved.zip"
        take_flow.draft = _draft(tmp_path)

        def resume_at_moved_path(self: _FakeMyEvaluations) -> int:
            self.resume = take_flow.draft
            self.package_path = moved
            return QDialog.DialogCode.Accepted.value

        monkeypatch.setattr(take_flow, "exec", resume_at_moved_path)
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            _unexpected_file_dialog,
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._take_evaluation_button.click()

        assert take_flow.shown == [_ACCOUNT]
        assert dialog.choice == LauncherChoice(
            mode=MODE_QUALITATIVE,
            action=ACTION_TAKE,
            project_path=moved,
            account=_ACCOUNT,
            draft=take_flow.draft,
        )

    def test_open_another_evaluation_keeps_the_account(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        take_flow.script = ["other"]
        other = tmp_path / "other.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(other), ""),
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._take_evaluation_button.click()

        assert dialog.choice == LauncherChoice(
            mode=MODE_QUALITATIVE, action=ACTION_TAKE, project_path=other, account=_ACCOUNT
        )

    def test_cancelling_the_file_dialog_goes_back_to_the_list_not_to_the_login(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        take_flow.script = ["other", "cancel"]
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: ("", ""),
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._take_evaluation_button.click()

        assert take_flow.shown == [_ACCOUNT, _ACCOUNT]
        assert dialog.choice is None
        assert dialog._stack.currentIndex() == 2

    def test_cancelling_the_login_stays_on_the_qualitative_page(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        take_flow.login.cancelled = True  # type: ignore[attr-defined]
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            _unexpected_file_dialog,
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._take_evaluation_button.click()

        assert take_flow.shown == []
        assert dialog.choice is None

    def test_cancelling_the_edit_evaluation_file_picker_stays_on_the_qualitative_page(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: ("", ""),
        )

        dialog._edit_evaluation_button.click()

        assert dialog.choice is None
        assert dialog._stack.currentIndex() == 2  # still on the qualitative page, not closed


class TestAggregateResults:
    def test_clicking_it_opens_the_dialog_without_setting_choice_or_closing(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[object] = []

        class _FakeAggregationDialog:
            def __init__(self, parent: object = None) -> None:
                opened.append(parent)

            def exec(self) -> int:
                return QDialog.DialogCode.Rejected.value

        fake_module = ModuleType("qicom.gui.widgets.aggregation_dialog")
        fake_module.AggregationDialog = _FakeAggregationDialog  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._aggregate_results_button.click()

        assert len(opened) == 1
        assert dialog.choice is None
        assert dialog._stack.currentIndex() == 2  # still on the qualitative page


class TestLazyDialogImports:
    def test_importing_launcher_does_not_import_optional_dialogs(self) -> None:
        code = """
import json
import sys
import qicom.gui.widgets.launcher_dialog
names = (
    'qicom.gui.widgets.about_dialog',
    'qicom.gui.widgets.aggregation_dialog',
    'qicom.gui.widgets.settings_dialog',
)
print(json.dumps({name: name in sys.modules for name in names}))
"""
        environment = os.environ.copy()
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

        assert not any(json.loads(completed.stdout).values())

    @pytest.mark.parametrize(
        ("button_name", "module_name", "class_name"),
        (
            ("_about_button", "qicom.gui.widgets.about_dialog", "AboutDialog"),
            ("_settings_button", "qicom.gui.widgets.settings_dialog", "SettingsDialog"),
        ),
    )
    def test_header_dialog_is_resolved_when_clicked(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        button_name: str,
        module_name: str,
        class_name: str,
    ) -> None:
        opened: list[object] = []

        class _FakeDialog:
            def __init__(self, parent: object = None) -> None:
                opened.append(parent)

            def exec(self) -> int:
                return QDialog.DialogCode.Rejected.value

        fake_module = ModuleType(module_name)
        setattr(fake_module, class_name, _FakeDialog)
        monkeypatch.setitem(sys.modules, module_name, fake_module)
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        getattr(dialog, button_name).click()

        assert opened == [dialog]


class TestRetranslation:
    def test_retranslate_updates_button_text(self, qtbot: QtBot) -> None:
        from qicom.gui.i18n import translator

        translator.set_language("en")
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        translator.set_language("it")
        try:
            assert dialog._quantitative_button.text() == "Analisi quantitativa"
            assert dialog._qualitative_button.text() == "Analisi qualitativa"
            assert dialog._radiomics_button.text() == "Radiomica"
        finally:
            translator.set_language("en")


class TestRecentProjects:
    def test_hidden_with_no_recent_projects(self, qtbot: QtBot) -> None:
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert dialog._recent_quantitative_layout.count() == 0

    def test_shows_one_row_per_recorded_quantitative_project_newest_first(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        first = tmp_path / "first"
        first.mkdir()
        record_recent_project(first, MODE_QUANTITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert not hasattr(dialog, "_recent_mode_layout")
        assert dialog._recent_quantitative_layout.count() == 1
        assert dialog._recent_quantitative_layout.itemAt(0).widget().text() == "first"

    def test_past_qualitative_evaluations_never_appear_on_the_main_screen(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        # Explicit ask: this, the app's very first screen, must only ever
        # show quantitative analyses - a recorded qualitative evaluation
        # (still written to disk by QualitativeMainWindow's own Save/Edit
        # flows) must never be surfaced here. There is no longer any
        # recent-qualitative widget on the dialog at all to assert against.
        evaluation_zip = tmp_path / "my_evaluation.zip"
        evaluation_zip.touch()
        record_recent_project(evaluation_zip, MODE_QUALITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert not hasattr(dialog, "_recent_mode_layout")
        assert not hasattr(dialog, "_recent_qualitative_section")

    def test_home_page_has_no_recent_quantitative_row(self, qtbot: QtBot, tmp_path: Path) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        record_recent_project(project_dir, MODE_QUANTITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        assert dialog._stack.currentIndex() == 0
        assert not hasattr(dialog, "_recent_mode_layout")

    def test_clicking_a_recent_row_on_the_quantitative_page_sets_choice_and_accepts(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        record_recent_project(project_dir, MODE_QUANTITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._quantitative_button.click()
        button = dialog._recent_quantitative_layout.itemAt(0).widget()

        button.click()

        assert dialog.choice is not None
        assert dialog.choice.mode == MODE_QUANTITATIVE
        assert dialog.choice.action == ACTION_LOAD
        assert dialog.choice.project_path == project_dir
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_caps_the_visible_rows_below_the_full_stored_maximum(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        from qicom.gui.widgets.launcher_dialog import _VISIBLE_RECENT_PROJECTS
        from qicom.session.recent_projects import MAX_RECENT_PROJECTS

        assert _VISIBLE_RECENT_PROJECTS < MAX_RECENT_PROJECTS
        for i in range(MAX_RECENT_PROJECTS):
            folder = tmp_path / f"p{i}"
            folder.mkdir()
            record_recent_project(folder, MODE_QUANTITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert dialog._recent_quantitative_layout.count() == _VISIBLE_RECENT_PROJECTS


class TestIcons:
    def test_mode_buttons_and_recent_rows_get_a_non_null_icon(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "p"
        project_dir.mkdir()
        record_recent_project(project_dir, MODE_QUANTITATIVE)

        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert not dialog._quantitative_button.icon().isNull()
        assert not dialog._qualitative_button.icon().isNull()
        assert not dialog._radiomics_button.icon().isNull()
        assert not dialog._recent_quantitative_layout.itemAt(0).widget().icon().isNull()


class TestLanguageButton:
    def test_button_shows_the_current_language_code(self, qtbot: QtBot) -> None:
        from qicom.gui.i18n import translator

        translator.set_language("en")
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        assert dialog._language_button.text() == "EN"
        assert dialog._language_actions["en"].isChecked()

    def test_picking_a_language_switches_the_translator_and_updates_the_button(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.i18n import translator

        translator.set_language("en")
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        try:
            dialog._language_actions["it"].trigger()

            assert translator.language == "it"
            assert dialog._language_button.text() == "IT"
            assert dialog._language_actions["it"].isChecked()
        finally:
            translator.set_language("en")

    def test_the_button_shows_only_the_base_language_subtag(self, qtbot: QtBot) -> None:
        from qicom.gui.i18n import translator

        translator.set_language("en")
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)

        try:
            dialog._language_actions["zh-Hans"].trigger()

            assert dialog._language_button.text() == "ZH"
        finally:
            translator.set_language("en")


class TestAccessCodes:
    """A protected package's code is asked in the launcher, before any
    window exists - so a cancelled code never leaves an empty one behind."""

    def _protected(self, tmp_path: Path) -> tuple[Path, AccessKey]:
        import zipfile

        plain = tmp_path / "plain.zip"
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("evaluation.json", "{}")
        access = new_access_key("STUDY-CODE-2026")
        protected = tmp_path / "protected.zip"
        protect_package(plain, protected, access)
        return protected, access

    def _asked(self, monkeypatch: pytest.MonkeyPatch, answer: AccessKey | None) -> list[str]:
        from qicom.gui.widgets import access_code_dialog

        asked: list[str] = []

        def ask(_parent: object, keyring: AccessKeyring, header: object, name: str) -> object:
            known = keyring.key_for(header)  # type: ignore[arg-type]
            if known is not None:
                return known
            asked.append(name)
            return answer

        monkeypatch.setattr(access_code_dialog, "ask_access_key", ask)
        return asked

    def test_editing_a_protected_package_asks_for_its_code(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        protected, access = self._protected(tmp_path)
        asked = self._asked(monkeypatch, access)
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(protected), ""),
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._edit_evaluation_button.click()

        assert asked == ["protected"]
        assert dialog.choice == LauncherChoice(
            mode=MODE_QUALITATIVE, action=ACTION_EDIT, project_path=protected, access=access
        )

    def test_a_cancelled_code_opens_nothing(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        protected, _access = self._protected(tmp_path)
        self._asked(monkeypatch, None)
        take_flow.login.account = None  # type: ignore[attr-defined]
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(protected), ""),
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._take_evaluation_button.click()

        assert dialog.choice is None

    def test_resuming_uses_the_key_kept_in_the_draft(
        self,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        take_flow: type[_FakeMyEvaluations],
    ) -> None:
        protected, access = self._protected(tmp_path)
        account = create_account("mario", "secret")
        take_flow.login.account = account  # type: ignore[attr-defined]
        draft = replace(_draft(tmp_path), package_path=protected)
        DraftStore(account).save(draft, {}, access)
        asked = self._asked(monkeypatch, None)

        def resume(self: _FakeMyEvaluations) -> int:
            self.resume = draft
            self.package_path = protected
            return QDialog.DialogCode.Accepted.value

        monkeypatch.setattr(take_flow, "exec", resume)
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._take_evaluation_button.click()

        assert asked == []
        assert dialog.choice is not None and dialog.choice.access == access

    def test_a_damaged_protected_package_is_said_plainly(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import zipfile

        from message_box_stubs import stub_message_box

        from qicom.gui.widgets import launcher_dialog as launcher_module

        damaged = tmp_path / "damaged.zip"
        with zipfile.ZipFile(damaged, "w") as archive:
            archive.writestr("qicom_protected.json", "{not json")
            archive.writestr("payload.bin", b"")
        warned: list[str] = []
        stub_message_box(
            monkeypatch, launcher_module, warning=lambda _p, _t, text, *a: warned.append(text)
        )
        monkeypatch.setattr(
            "qicom.gui.widgets.launcher_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: (str(damaged), ""),
        )
        dialog = LauncherDialog()
        qtbot.addWidget(dialog)
        dialog._qualitative_button.click()

        dialog._edit_evaluation_button.click()

        assert dialog.choice is None
        assert warned and "cannot be opened" in warned[0]
