"""qicom.gui.updates: consent, the offer, the download dialog, the install
handover and the Help-menu entries - with the network and the installers
replaced, so nothing is ever downloaded or run."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QDialogButtonBox,
    QMenu,
    QMessageBox,
    QWidget,
)
from pytestqt.qtbot import QtBot

from qicom import updates
from qicom.gui import updates as gui_updates
from qicom.gui.preferences import PreferencesStore
from qicom.i18n import translator

REVISION = "a" * 40


@pytest.fixture(autouse=True)
def fresh_state(qapp: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PreferencesStore:
    """A dormant preferences store and an empty session for every test, with
    the QApplication every dialog needs (Qt aborts the process without one)."""
    store = PreferencesStore(tmp_path / "preferences.json")
    monkeypatch.setattr(gui_updates, "preferences_store", store)
    monkeypatch.setattr(gui_updates, "_session", gui_updates._Session())
    return store


def _click(monkeypatch: pytest.MonkeyPatch, label_key: str) -> list[str]:
    """Answer every QMessageBox by clicking the button labelled ``label_key``;
    returns the texts of the boxes shown."""
    shown: list[str] = []
    label = translator.tr(label_key)

    def clicked(box: QMessageBox) -> QAbstractButton | None:
        return next((button for button in box.buttons() if button.text() == label), None)

    def exec_(box: QMessageBox) -> int:
        shown.append(box.text())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", clicked)
    return shown


def _check(kind: updates.InstallKind, *, size: int = 140_000_000) -> updates.UpdateCheck:
    manifest = updates.UpdateManifest(
        channel="latest-build",
        version="1.1.0.dev0",
        revision=REVISION,
        commit_time=datetime(2026, 11, 14, 20, 0, tzinfo=UTC),
        files={
            updates.WINDOWS_ARCHIVE: updates.ManifestFile(size, "c" * 64),
            updates.LINUX_DEB: updates.ManifestFile(size, "c" * 64),
            updates.LINUX_TARBALL: updates.ManifestFile(size, "c" * 64),
        },
    )
    build = updates.LocalBuild(
        version="1.1.0.dev0",
        revision="b" * 40,
        commit_time=datetime(2026, 11, 1, tzinfo=UTC),
        channel="latest-build",
        frozen=True,
    )
    return updates.UpdateCheck(build, manifest, updates.Installation(kind, directory=Path(".")))


class _FakeUpdate:
    def __init__(self, error: Exception | None = None) -> None:
        self.applied = 0
        self._error = error

    def apply(self) -> None:
        self.applied += 1
        if self._error is not None:
            raise self._error


class TestConsent:
    def test_a_run_from_source_is_never_asked(
        self, monkeypatch: pytest.MonkeyPatch, fresh_state: PreferencesStore
    ) -> None:
        shown = _click(monkeypatch, "updates.consent.yes")
        gui_updates.ask_consent_if_needed()
        assert shown == []
        assert fresh_state.preferences.update_check is None

    def test_yes_is_remembered(
        self, monkeypatch: pytest.MonkeyPatch, fresh_state: PreferencesStore
    ) -> None:
        monkeypatch.setattr(gui_updates, "is_supported", lambda: True)
        shown = _click(monkeypatch, "updates.consent.yes")

        gui_updates.ask_consent_if_needed()

        assert shown == [translator.tr("updates.consent.text")]
        assert fresh_state.preferences.update_check is True

    def test_closing_the_question_is_a_no(
        self, monkeypatch: pytest.MonkeyPatch, fresh_state: PreferencesStore
    ) -> None:
        """conftest answers every box with no button at all: consent is never
        assumed from a dismissed question."""
        monkeypatch.setattr(gui_updates, "is_supported", lambda: True)
        gui_updates.ask_consent_if_needed()
        assert fresh_state.preferences.update_check is False

    def test_the_question_is_asked_once(
        self, monkeypatch: pytest.MonkeyPatch, fresh_state: PreferencesStore
    ) -> None:
        monkeypatch.setattr(gui_updates, "is_supported", lambda: True)
        fresh_state.update(update_check=False)
        shown = _click(monkeypatch, "updates.consent.yes")
        gui_updates.ask_consent_if_needed()
        assert shown == []

    def test_no_check_starts_without_consent(
        self, monkeypatch: pytest.MonkeyPatch, fresh_state: PreferencesStore
    ) -> None:
        def no_thread(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("an update check started without consent")

        monkeypatch.setattr(gui_updates, "is_supported", lambda: True)
        monkeypatch.setattr(gui_updates, "run_in_thread", no_thread)
        gui_updates.start_startup_check()
        fresh_state.update(update_check=False)
        gui_updates.start_startup_check()


class TestManualCheck:
    def test_a_run_from_source_says_why_it_does_not_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shown = _click(monkeypatch, "updates.consent.yes")
        gui_updates.check_now()
        assert shown == [translator.tr("updates.unavailable.source")]

    def test_up_to_date_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shown = _click(monkeypatch, "updates.consent.yes")
        check = _check(updates.InstallKind.MANUAL)
        current = updates.UpdateCheck(
            build=updates.LocalBuild(
                version="1.1.0.dev0",
                revision=REVISION,
                commit_time=check.manifest.commit_time,
                channel="latest-build",
                frozen=True,
            ),
            manifest=check.manifest,
            installation=check.installation,
        )
        gui_updates._session.busy = True
        gui_updates._on_manual_check(current)
        assert not gui_updates._session.busy
        assert len(shown) == 1 and "1.1.0.dev0" in shown[0]


class TestOffer:
    def test_the_download_page_opens_where_qicom_cannot_update_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []
        monkeypatch.setattr(
            gui_updates.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
        )
        _click(monkeypatch, "updates.available.open_page")

        gui_updates.offer_update(_check(updates.InstallKind.MANUAL), None)

        assert opened == [updates.DOWNLOAD_PAGE_URL]

    def test_later_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_download(*_args: object) -> None:
            raise AssertionError("downloaded after Later")

        monkeypatch.setattr(gui_updates, "UpdateDownloadDialog", no_download)
        _click(monkeypatch, "updates.available.later")
        gui_updates.offer_update(_check(updates.InstallKind.WINDOWS_INSTALLER), None)
        assert not gui_updates.has_pending_update()

    def test_update_now_downloads_then_asks_the_windows_to_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prepared = _FakeUpdate()

        class _Dialog:
            def __init__(self, *_args: object) -> None:
                pass

            def exec(self) -> int:
                return 1

            def prepared(self) -> _FakeUpdate:
                return prepared

        installs: list[object] = []
        monkeypatch.setattr(gui_updates, "UpdateDownloadDialog", _Dialog)
        monkeypatch.setattr(gui_updates, "request_install", lambda p, _parent: installs.append(p))
        shown = _click(monkeypatch, "updates.available.update_now")

        gui_updates.offer_update(_check(updates.InstallKind.LINUX_DEB), None)

        check = _check(updates.InstallKind.LINUX_DEB)
        assert installs == [prepared]
        assert shown == [
            translator.tr(
                "updates.available.text",
                new_date=gui_updates._build_date(check.manifest.commit_time, REVISION),
                current=gui_updates._build_date(check.build.commit_time, check.build.revision),
            )
        ]
        assert not gui_updates._session.busy

    def test_one_offer_at_a_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shown = _click(monkeypatch, "updates.available.later")
        gui_updates._session.busy = True
        gui_updates.offer_update(_check(updates.InstallKind.MANUAL), None)
        assert shown == []


class TestInstalling:
    def test_the_update_waits_for_the_windows_to_close(self, qtbot: QtBot) -> None:
        prepared = _FakeUpdate()
        gui_updates.request_install(prepared)  # type: ignore[arg-type]

        assert gui_updates.has_pending_update()
        assert gui_updates.apply_pending_update() is True
        assert prepared.applied == 1
        assert not gui_updates.has_pending_update()

    def test_a_window_that_stays_open_defers_the_update(
        self, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
    ) -> None:
        """Unsaved work the user chose to keep: the update waits for QICOM to exit."""

        class _Stubborn(QDialog):
            keep_open = True

            def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
                if self.keep_open:
                    event.ignore()
                else:
                    event.accept()

        stubborn = _Stubborn()
        qtbot.addWidget(stubborn)
        stubborn.show()
        shown = _click(monkeypatch, "updates.download.close")

        gui_updates.request_install(_FakeUpdate())  # type: ignore[arg-type]

        assert shown == [translator.tr("updates.deferred")]
        assert gui_updates.has_pending_update()
        stubborn.keep_open = False

    def test_a_failed_install_is_reported_and_qicom_carries_on(
        self, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
    ) -> None:
        shown = _click(monkeypatch, "updates.download.close")
        gui_updates._session.pending = _FakeUpdate(RuntimeError("no password"))  # type: ignore[assignment]

        assert gui_updates.apply_pending_update() is False

        assert len(shown) == 1 and "no password" in shown[0]
        assert not gui_updates.has_pending_update()

    def test_nothing_pending_installs_nothing(self) -> None:
        assert gui_updates.apply_pending_update() is False


class TestDownloadDialog:
    def test_progress_failure_and_success(self, qtbot: QtBot) -> None:
        dialog = gui_updates.UpdateDownloadDialog(_check(updates.InstallKind.LINUX_TARBALL))
        qtbot.addWidget(dialog)
        assert "140" in dialog._description_label.text()

        dialog._on_progress(70_000_000, 140_000_000)
        assert dialog._progress_bar.value() == 50

        dialog._on_failed("connection reset")
        assert "connection reset" in dialog._status_label.text()
        assert dialog._button_box.button(QDialogButtonBox.StandardButton.Close) is not None

        prepared = _FakeUpdate()
        dialog._on_succeeded(prepared)  # type: ignore[arg-type]
        assert dialog.prepared() is prepared

    def test_a_download_finishing_after_close_is_not_installed(self, qtbot: QtBot) -> None:
        dialog = gui_updates.UpdateDownloadDialog(_check(updates.InstallKind.LINUX_DEB))
        qtbot.addWidget(dialog)
        dialog.reject()
        dialog._on_succeeded(_FakeUpdate())  # type: ignore[arg-type]
        assert dialog.prepared() is None


class TestMenu:
    def test_the_entries_follow_the_preference_and_the_language(
        self, qtbot: QtBot, fresh_state: PreferencesStore
    ) -> None:
        window = QWidget()
        qtbot.addWidget(window)
        menu = QMenu(window)
        actions = gui_updates.UpdateMenuActions(menu, window)

        assert actions.check_action.text() == translator.tr("menu.help.check_updates")
        assert actions.check_action in menu.actions()
        # A run from source cannot check at startup, so the choice is not offered.
        assert not actions.startup_action.isEnabled()

        actions.startup_action.setEnabled(True)
        actions.startup_action.setChecked(True)
        assert fresh_state.preferences.update_check is True

        fresh_state.update(update_check=False)
        actions.refresh()
        assert not actions.startup_action.isChecked()

    def test_every_window_offers_the_entries(self) -> None:
        root = Path(gui_updates.__file__).resolve().parent
        for relative in (
            "main_window.py",
            "radiomics_main_window.py",
            "widgets/qualitative_main_window.py",
            "tools/tools_window.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            assert "UpdateMenuActions(self._help_menu, self)" in source, relative
