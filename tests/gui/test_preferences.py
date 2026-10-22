"""Persistent desktop preference tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

import qicom.metrics.runner  # noqa: F401 - populate the metric catalog
from qicom import runtime
from qicom.gui import preferences as preferences_module
from qicom.gui.preferences import PreferencesStore, UserPreferences
from qicom.gui.widgets import settings_dialog as settings_module
from qicom.gui.widgets.settings_dialog import SettingsDialog
from qicom.i18n import SUPPORTED_LANGUAGES
from qicom.metrics.catalog import DESCRIPTORS_DIR


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path):
    store = PreferencesStore(tmp_path / "preferences.json")
    monkeypatch.setattr(settings_module, "preferences_store", store)
    monkeypatch.setattr(settings_module, "detect_runtime_profile", lambda: runtime.STANDARD_PROFILE)
    monkeypatch.setattr(
        preferences_module, "detect_runtime_profile", lambda: runtime.STANDARD_PROFILE
    )
    return store


def test_preferences_round_trip_between_app_sessions(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    store._active = True

    store.update(
        language="it",
        theme="dark",
        number_notation="auto",
        number_precision=3,
        default_metric_ids=("mse", "ssim"),
        evaluator_name="Ada Lovelace",
        keep_learned_models=False,
        radiomics_cache_mib=256,
        pair_workers=1,
    )

    loaded = PreferencesStore(path)._load()
    assert loaded.language == "it"
    assert loaded.theme == "dark"
    assert loaded.number_notation == "auto"
    assert loaded.number_precision == 3
    assert loaded.default_metric_ids == ("mse", "ssim")
    assert loaded.evaluator_name == "Ada Lovelace"
    assert loaded.keep_learned_models is False
    assert loaded.radiomics_cache_mib == 256
    assert loaded.pair_workers == 1
    assert loaded.version == 2


def test_invalid_preferences_fall_back_without_crashing(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "language": "xx",
                "theme": "neon",
                "number_notation": "hex",
                "number_precision": 40,
                "default_metric_ids": ["mse", "not-a-metric"],
                "evaluator_name": 42,
                "keep_learned_models": "sometimes",
                "radiomics_cache_mib": -1,
            }
        ),
        encoding="utf-8",
    )

    loaded = PreferencesStore(path)._load()

    assert loaded.language == "en"
    assert loaded.theme == "system"
    assert loaded.number_notation == "full"
    assert loaded.number_precision is None
    assert loaded.default_metric_ids == ("mse",)
    assert loaded.evaluator_name == ""
    assert loaded.keep_learned_models is None
    assert loaded.radiomics_cache_mib is None
    assert loaded.resolved_resources() == runtime.STANDARD_PROFILE.resources


def test_settings_dialog_exposes_general_metrics_identity_and_diagnostics(
    qtbot: QtBot,
) -> None:
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    assert dialog._tabs.count() == 4
    assert len(dialog.metric_panel._rows) == len(tuple(DESCRIPTORS_DIR.glob("*.yaml")))
    assert dialog._language_combo.count() == len(SUPPORTED_LANGUAGES)
    assert dialog._theme_combo.findData("system") >= 0
    assert dialog._name_edit.placeholderText()
    assert dialog._keep_learned_models.currentData() is None
    assert dialog._keep_learned_models.toolTip()
    assert dialog._radiomics_cache_mib.value() == -1
    assert dialog._radiomics_cache_mib.toolTip()
    assert "512" in dialog._radiomics_cache_mib.text()
    assert dialog._pair_workers.value() == 0
    assert "2" in dialog._pair_workers.text()


@pytest.mark.parametrize("manual", [False, True])
def test_resources_recompute_only_automatic_fields(isolated_preferences, manual):
    store = isolated_preferences
    if manual:
        store.update(radiomics_cache_mib=0, keep_learned_models=False, pair_workers=2)
    original = store.preferences
    for profile in (
        runtime.CONSTRAINED_PROFILE,
        runtime.WORKSTATION_PROFILE,
        runtime.STANDARD_PROFILE,
    ):
        expected = runtime.RuntimeResources(0, False, 2) if manual else profile.resources
        assert store.preferences.resolved_resources(profile) == expected
        assert store.preferences == original


def test_automatic_round_trip_does_not_persist_detected_values(isolated_preferences, monkeypatch):
    store = isolated_preferences
    store._active = True
    monkeypatch.setattr(
        preferences_module, "detect_runtime_profile", lambda: runtime.CONSTRAINED_PROFILE
    )
    assert store.update().resolved_resources() == runtime.CONSTRAINED_PROFILE.resources
    payload = json.loads(store.path.read_text())
    assert payload["version"] == 2
    assert payload["radiomics_cache_mib"] is None
    assert payload["keep_learned_models"] is None
    assert payload["pair_workers"] is None
    monkeypatch.setattr(
        preferences_module, "detect_runtime_profile", lambda: runtime.WORKSTATION_PROFILE
    )
    assert store._load().resolved_resources() == runtime.WORKSTATION_PROFILE.resources


@pytest.mark.parametrize("version", [{}, {"version": 1}])
@pytest.mark.parametrize(
    "old_values",
    [
        {},
        {"radiomics_cache_mib": 512, "keep_learned_models": True},
        {"radiomics_cache_mib": 0, "keep_learned_models": False},
    ],
)
def test_v1_migration_keeps_present_values_manual_and_round_trips(
    isolated_preferences, version, old_values
):
    store = isolated_preferences
    payload = {**version, **old_values, "language": "it", "evaluator_name": "Ada"}
    store.path.write_text(json.dumps(payload))
    loaded = store._load()
    assert loaded.version == 2
    assert loaded.language == "it" and loaded.evaluator_name == "Ada"
    assert loaded.radiomics_cache_mib == old_values.get("radiomics_cache_mib")
    assert loaded.keep_learned_models is old_values.get("keep_learned_models")
    assert loaded.pair_workers is None
    store._preferences = loaded
    store._active = True
    store.update()
    assert store._load() == loaded
    assert json.loads(store.path.read_text())["version"] == 2


@pytest.mark.parametrize("version", [0, 3, "1", True, 1.5, None])
def test_unknown_or_invalid_version_falls_back_to_automatic(isolated_preferences, version):
    store = isolated_preferences
    store.path.write_text(json.dumps({"version": version, "radiomics_cache_mib": 100}))
    assert store._load() == UserPreferences()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("radiomics_cache_mib", True),
        ("radiomics_cache_mib", 4097),
        ("radiomics_cache_mib", 1.5),
        ("pair_workers", True),
        ("pair_workers", 1.0),
        ("pair_workers", 0),
        ("pair_workers", 3),
        ("keep_learned_models", 1),
    ],
)
def test_invalid_resource_values_become_automatic(isolated_preferences, name, value):
    assert getattr(isolated_preferences.update(**{name: value}), name) is None


def test_reset_persists_automatic_choices(isolated_preferences):
    store = isolated_preferences
    store._active = True
    store.update(radiomics_cache_mib=512, keep_learned_models=True, pair_workers=1)
    assert store.reset() == UserPreferences()
    assert store._load() == UserPreferences()


@pytest.mark.parametrize("keep", [False, True])
def test_settings_toggle_explicit_and_automatic_resources(
    qtbot, isolated_preferences, monkeypatch, keep
):
    monkeypatch.setattr(
        settings_module, "detect_runtime_profile", lambda: runtime.CONSTRAINED_PROFILE
    )
    monkeypatch.setattr(
        preferences_module, "detect_runtime_profile", lambda: runtime.CONSTRAINED_PROFILE
    )
    applied = []
    monkeypatch.setattr(settings_module, "set_keep_learned_models", applied.append)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert "128" in dialog._radiomics_cache_mib.text()
    dialog._radiomics_cache_mib.setValue(0)
    dialog._pair_workers.setValue(2)
    dialog._keep_learned_models.setCurrentIndex(dialog._keep_learned_models.findData(keep))
    assert isolated_preferences.preferences.resolved_resources() == runtime.RuntimeResources(
        0, keep, 2
    )
    assert applied[-1] is keep

    # Reopening must retain manual zero and False, without relabelling them automatic.
    reopened = SettingsDialog()
    qtbot.addWidget(reopened)
    assert reopened._radiomics_cache_mib.value() == 0
    assert reopened._keep_learned_models.currentData() is keep
    assert reopened._pair_workers.value() == 2
    assert "128" in reopened._radiomics_cache_mib.specialValueText()
    reopened._radiomics_cache_mib.setValue(-1)
    reopened._pair_workers.setValue(0)
    reopened._keep_learned_models.setCurrentIndex(0)
    assert isolated_preferences.preferences == UserPreferences()
    assert applied[-1] is False


def test_activation_applies_the_automatic_model_policy(qapp, isolated_preferences, monkeypatch):
    applied = []
    monkeypatch.setattr(preferences_module, "set_keep_learned_models", applied.append)
    monkeypatch.setattr(
        preferences_module, "detect_runtime_profile", lambda: runtime.CONSTRAINED_PROFILE
    )
    isolated_preferences.activate(qapp)
    assert applied == [False]
    assert isolated_preferences.preferences.keep_learned_models is None


@pytest.mark.parametrize(
    "content", ['{"version": 99, "language": "it"}', "{not json", '["a list"]']
)
def test_unreadable_preferences_are_kept_aside_not_overwritten(
    tmp_path: Path, content: str
) -> None:
    """A file written by a newer QICOM, or damaged, used to be replaced by
    defaults on the first change of any setting: language, default metrics
    and evaluator name were lost without a trace."""
    path = tmp_path / "preferences.json"
    path.write_text(content, encoding="utf-8")
    store = PreferencesStore(path)
    store._preferences = store._load()
    store._active = True

    store.update(theme="dark")

    kept = list(tmp_path.glob("preferences.json.unreadable-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == content
    assert json.loads(path.read_text(encoding="utf-8"))["theme"] == "dark"
    store.update(theme="light")
    assert len(list(tmp_path.glob("preferences.json.unreadable-*"))) == 1  # once only
