"""Persistent, non-clinical user preferences for the desktop GUI.

Project/session data stays in the existing QICOM project formats.  This
module stores only choices that belong to the person using this operating-
system account: language, visual presentation, default quantitative metrics
and the optional qualitative evaluator name.

The process-wide store is deliberately dormant until :func:`activate` is
called by ``qicom.gui.app``.  GUI unit tests construct windows directly and
must never write to a developer's real profile merely because they click a
menu action.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import platformdirs
from PySide6.QtWidgets import QApplication

from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.number_format import FULL, NOTATIONS, number_format_settings
from qicom.gui.theming import SYSTEM_THEME, THEME_CHOICES, theme_manager
from qicom.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, translator
from qicom.metrics.catalog import DESCRIPTORS_DIR
from qicom.metrics.reference_structural import set_keep_learned_models
from qicom.runtime import RuntimeProfile, RuntimeResources, detect_runtime_profile

_logger = logging.getLogger(__name__)

PREFERENCES_VERSION = 2
DEFAULT_PRECISION: int | None = None
MAX_RADIOMICS_CACHE_MIB = 4096


@dataclass(frozen=True)
class UserPreferences:
    version: int = PREFERENCES_VERSION
    language: str = DEFAULT_LANGUAGE
    theme: str = SYSTEM_THEME
    number_notation: str = FULL
    number_precision: int | None = DEFAULT_PRECISION
    default_metric_ids: tuple[str, ...] | None = None
    evaluator_name: str = ""
    # None means automatic. Explicit values, including zero/False, are manual.
    keep_learned_models: bool | None = None
    radiomics_cache_mib: int | None = None
    pair_workers: int | None = None
    llm_export_warning_seen: bool = False
    """Whether the "read it before you upload it" notice has been shown once.
    Absent from older preferences files, which therefore see the notice."""

    def resolved_resources(self, profile: RuntimeProfile | None = None) -> RuntimeResources:
        """Resolve automatic fields for this snapshot without rewriting preferences."""
        defaults = (detect_runtime_profile() if profile is None else profile).resources
        return RuntimeResources(
            radiomics_cache_mib=(
                defaults.radiomics_cache_mib
                if self.radiomics_cache_mib is None
                else self.radiomics_cache_mib
            ),
            keep_learned_models=(
                defaults.keep_learned_models
                if self.keep_learned_models is None
                else self.keep_learned_models
            ),
            pair_workers=defaults.pair_workers if self.pair_workers is None else self.pair_workers,
        )


def _migrate_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep every valid v1 resource value manual, even if it was an old default."""
    version = payload.get("version", 1)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in (1, PREFERENCES_VERSION)
    ):
        raise ValueError(f"Unsupported preferences version {version!r}.")
    migrated = dict(payload)
    if version == 1:
        # Missing fields become automatic; present fields keep their old values.
        migrated.setdefault("radiomics_cache_mib", None)
        migrated.setdefault("keep_learned_models", None)
        migrated["pair_workers"] = None
    migrated["version"] = PREFERENCES_VERSION
    return migrated


def default_preferences_path() -> Path:
    return Path(platformdirs.user_config_dir("qicom", appauthor=False)) / "preferences.json"


class PreferencesStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._preferences = UserPreferences()
        self._active = False
        self._unreadable_file = False

    @property
    def preferences(self) -> UserPreferences:
        return self._preferences

    @property
    def path(self) -> Path:
        return self._path or default_preferences_path()

    def activate(self, app: QApplication) -> UserPreferences:
        """Load, validate and apply preferences, then enable persistence."""
        self._preferences = self._load()
        self._active = True
        self.apply(app)
        return self._preferences

    def apply(self, app: QApplication) -> None:
        prefs = self._preferences
        translator.set_language(prefs.language)
        number_format_settings.set_notation(prefs.number_notation)
        number_format_settings.set_precision(prefs.number_precision)
        metric_selection_settings.set_default_enabled_ids(prefs.default_metric_ids)
        metric_selection_settings.start_new_session_from_defaults()
        set_keep_learned_models(prefs.resolved_resources().keep_learned_models)
        theme_manager.apply(app, prefs.theme)

    def update(self, **changes: Any) -> UserPreferences:
        candidate = replace(self._preferences, **changes)
        self._preferences = self._validated(asdict(candidate))
        if self._active:
            self._save()
        return self._preferences

    def reset(self, app: QApplication | None = None) -> UserPreferences:
        self._preferences = UserPreferences()
        if self._active:
            self._save()
        if app is not None:
            self.apply(app)
        return self._preferences

    def _load(self) -> UserPreferences:
        """The saved preferences, or the defaults when there are none or they
        cannot be read. An unreadable file - damaged, or written by a newer
        QICOM - is remembered, so the first save keeps it aside instead of
        overwriting the user's settings with defaults (see _save)."""
        self._unreadable_file = False
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return UserPreferences()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            _logger.warning("Ignoring invalid QICOM preferences: %s", exc)
            self._unreadable_file = True
            return UserPreferences()
        if not isinstance(payload, dict):
            _logger.warning("Ignoring QICOM preferences that are not a JSON object")
            self._unreadable_file = True
            return UserPreferences()
        try:
            return self._validated(payload)
        except ValueError as exc:
            _logger.warning("Ignoring invalid QICOM preferences: %s", exc)
            self._unreadable_file = True
            return UserPreferences()

    def _keep_unreadable_file_aside(self) -> None:
        if not self._unreadable_file or not self.path.exists():
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.unreadable-{stamp}")
        try:
            os.replace(self.path, target)
            _logger.warning("Unreadable preferences kept aside as %s", target.name)
        except OSError:
            _logger.warning("Unreadable preferences could not be kept aside")
        self._unreadable_file = False

    @staticmethod
    def _validated(payload: dict[str, Any]) -> UserPreferences:
        payload = _migrate_preferences(payload)
        language = payload.get("language", DEFAULT_LANGUAGE)
        if language not in SUPPORTED_LANGUAGES:
            language = DEFAULT_LANGUAGE

        theme = payload.get("theme", SYSTEM_THEME)
        if theme not in THEME_CHOICES:
            theme = SYSTEM_THEME

        notation = payload.get("number_notation", FULL)
        if notation not in NOTATIONS:
            notation = FULL

        precision = payload.get("number_precision", DEFAULT_PRECISION)
        if precision is not None and (not isinstance(precision, int) or not 2 <= precision <= 8):
            precision = DEFAULT_PRECISION

        raw_metrics = payload.get("default_metric_ids")
        metric_ids: tuple[str, ...] | None
        if raw_metrics is None:
            metric_ids = None
        elif isinstance(raw_metrics, (list, tuple)) and all(
            isinstance(item, str) for item in raw_metrics
        ):
            # Descriptor files are the stable catalog source of truth and
            # are available before metric modules have run their decorator
            # registration side effects during application startup.
            known = {path.stem for path in DESCRIPTORS_DIR.glob("*.yaml")}
            metric_ids = tuple(item for item in raw_metrics if item in known)
        else:
            metric_ids = None

        evaluator_name = payload.get("evaluator_name", "")
        if not isinstance(evaluator_name, str):
            evaluator_name = ""

        keep_learned_models = payload.get("keep_learned_models")
        if keep_learned_models is not None and not isinstance(keep_learned_models, bool):
            keep_learned_models = None

        radiomics_cache_mib = payload.get("radiomics_cache_mib")
        if radiomics_cache_mib is not None and (
            isinstance(radiomics_cache_mib, bool)
            or not isinstance(radiomics_cache_mib, int)
            or not 0 <= radiomics_cache_mib <= MAX_RADIOMICS_CACHE_MIB
        ):
            radiomics_cache_mib = None

        pair_workers = payload.get("pair_workers")
        if pair_workers is not None and (
            isinstance(pair_workers, bool)
            or not isinstance(pair_workers, int)
            or pair_workers not in (1, 2)
        ):
            pair_workers = None

        llm_export_warning_seen = payload.get("llm_export_warning_seen", False)
        if not isinstance(llm_export_warning_seen, bool):
            llm_export_warning_seen = False

        return UserPreferences(
            version=PREFERENCES_VERSION,
            language=language,
            theme=theme,
            number_notation=notation,
            number_precision=precision,
            default_metric_ids=metric_ids,
            evaluator_name=evaluator_name.strip()[:200],
            keep_learned_models=keep_learned_models,
            radiomics_cache_mib=radiomics_cache_mib,
            pair_workers=pair_workers,
            llm_export_warning_seen=llm_export_warning_seen,
        )

    def _save(self) -> None:
        destination = self.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._keep_unreadable_file_aside()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(self._preferences), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


preferences_store = PreferencesStore()


def remember_language(language: str) -> None:
    translator.set_language(language)
    preferences_store.update(language=language)


def remember_theme(app: QApplication, theme: str) -> None:
    theme_manager.apply(app, theme)
    preferences_store.update(theme=theme)


def remember_number_notation(notation: str) -> None:
    number_format_settings.set_notation(notation)
    preferences_store.update(number_notation=notation)


__all__ = [
    "PreferencesStore",
    "UserPreferences",
    "default_preferences_path",
    "preferences_store",
    "remember_language",
    "remember_number_notation",
    "remember_theme",
]
