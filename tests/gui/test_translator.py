"""Tests for qicom.gui.i18n.translator (pure Python, no Qt import needed)."""

from __future__ import annotations

import gc
import re
import weakref
from pathlib import Path

import pytest

from qicom.gui.i18n.translator import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, Translator

_DESCRIPTOR_IDS = {
    path.stem for path in (Path(__file__).parents[2] / "qicom/metrics/descriptors").glob("*.yaml")
}


def _is_per_metric_key(key: str) -> bool:
    """metric.<id>.name_full / .description / .value_range / .param.<name> -
    intentionally absent from en.json (see Translator.tr_or): the YAML
    descriptor cards are the English source of truth for these, never
    duplicated into en.json, so only it/fr/de carry them."""
    parts = key.split(".")
    return len(parts) >= 3 and parts[0] == "metric" and parts[1] in _DESCRIPTOR_IDS


class TestTranslator:
    def test_defaults_to_english(self) -> None:
        translator = Translator()
        assert translator.language == DEFAULT_LANGUAGE

    def test_all_supported_languages_load_without_error(self) -> None:
        for language in SUPPORTED_LANGUAGES:
            translator = Translator(language)
            assert translator.language == language

    def test_tr_returns_translated_string(self) -> None:
        translator = Translator("it")
        assert translator.tr("menu.file") == "File"
        assert translator.tr("status.ready") == "Pronto"

    def test_set_language_switches_translations(self) -> None:
        translator = Translator("en")
        assert translator.tr("status.ready") == "Ready"
        translator.set_language("fr")
        assert translator.tr("status.ready") == "Prêt"

    def test_unsupported_language_raises(self) -> None:
        translator = Translator()
        with pytest.raises(ValueError):
            translator.set_language("xx")

    def test_missing_key_falls_back_to_english_then_to_key_itself(self) -> None:
        translator = Translator("it")
        assert translator.tr("this.key.does.not.exist") == "this.key.does.not.exist"

    def test_language_change_notifies_listeners(self) -> None:
        translator = Translator("en")
        calls: list[str] = []
        translator.on_language_changed(lambda: calls.append(translator.language))

        translator.set_language("de")
        translator.set_language("fr")

        assert calls == ["de", "fr"]

    def test_tr_or_returns_translation_when_present(self) -> None:
        translator = Translator("it")
        assert translator.tr_or("menu.file", "fallback") == "File"

    def test_tr_or_returns_default_when_key_is_missing_in_every_language(self) -> None:
        translator = Translator("it")
        assert translator.tr_or("this.key.does.not.exist", "the English original") == (
            "the English original"
        )

    def test_tr_or_does_not_fall_back_to_english_translations_json(self) -> None:
        """Unlike tr(), tr_or() must not consult en.json at all - a key that
        only exists in en.json (never the current, non-English language)
        should still yield `default`, not the English JSON value."""
        translator = Translator("it")
        translator._strings["en"]["only.in.english.json"] = "english json value"
        assert translator.tr_or("only.in.english.json", "default") == "default"

    def test_tr_supports_keyword_formatting(self) -> None:
        translator = Translator("en")
        translator._strings["en"]["greeting"] = "Hello, {name}!"
        assert translator.tr("greeting", name="World") == "Hello, World!"

    def test_a_listener_that_raises_runtimeerror_is_pruned_without_blocking_others(self) -> None:
        # Simulates a widget destroyed by Qt without unregistering its
        # retranslate_ui callback (PySide raises RuntimeError when a deleted
        # C/C++ object's method is called) - one dead listener must not
        # break every future language switch for everyone else.
        translator = Translator("en")

        def dead_listener() -> None:
            raise RuntimeError("wrapped C/C++ object of type QLabel has been deleted")

        calls: list[str] = []
        translator.on_language_changed(dead_listener)
        translator.on_language_changed(lambda: calls.append("alive"))

        translator.set_language("it")  # must not raise
        assert calls == ["alive"]

        translator.set_language("de")  # dead_listener should have been pruned already
        assert calls == ["alive", "alive"]

    def test_bound_listener_does_not_keep_its_owner_alive(self) -> None:
        translator = Translator("en")

        class Listener:
            def notify(self) -> None:
                pass

        listener = Listener()
        listener_reference = weakref.ref(listener)
        translator.on_language_changed(listener.notify)

        del listener
        gc.collect()

        assert listener_reference() is None
        translator.set_language("it")
        assert len(translator._listeners) == 0

    def test_every_locale_file_has_the_same_keys(self) -> None:
        translators = {lang: Translator(lang) for lang in SUPPORTED_LANGUAGES}
        key_sets = {
            lang: {k for k in t._strings[lang] if not _is_per_metric_key(k)}
            for lang, t in translators.items()
        }
        reference = key_sets[DEFAULT_LANGUAGE]
        for language, keys in key_sets.items():
            assert keys == reference, f"{language}.json keys differ from {DEFAULT_LANGUAGE}.json"

    def test_every_literal_key_the_code_asks_for_exists(self) -> None:
        """A missing key renders as the key itself: the NSS model dialog
        offered its users a file filter literally named "file_filter.images",
        which also matched no file. Dynamic keys cannot be checked here."""
        english = Translator(DEFAULT_LANGUAGE)._strings[DEFAULT_LANGUAGE]
        literal_key = re.compile(r"""\btr\(\s*(["'])([A-Za-z0-9_.\-]+)\1""")
        source = Path(__file__).parents[2] / "qicom"
        missing = sorted(
            f"{path.relative_to(source.parent)}: {match.group(2)}"
            for path in source.rglob("*.py")
            for match in literal_key.finditer(path.read_text(encoding="utf-8"))
            if match.group(2) not in english and not _is_per_metric_key(match.group(2))
        )
        assert missing == []

    def test_every_value_passed_to_a_message_has_a_place_in_it(self) -> None:
        """str.format ignores an argument with no placeholder: the seed
        intensity passed to radiomics.status.seed_selected was never shown."""
        import ast
        import string

        source = Path(__file__).parents[2] / "qicom"
        problems: list[str] = []
        for language in SUPPORTED_LANGUAGES:
            strings = Translator(language)._strings[language]
            for path in source.rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if not (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "tr"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and node.args[0].value in strings
                    ):
                        continue
                    passed = {keyword.arg for keyword in node.keywords if keyword.arg}
                    fields = {
                        field.split(".")[0].split("[")[0]
                        for _text, field, _spec, _conv in string.Formatter().parse(
                            strings[node.args[0].value]
                        )
                        if field
                    }
                    for missing in sorted(passed - fields):
                        problems.append(f"{language}: {node.args[0].value} has no {{{missing}}}")
        assert problems == []

    def test_english_per_metric_text_comes_only_from_the_descriptor_cards(self) -> None:
        """A copy in en.json overrides the YAML card and drifts from it: the
        canonical BRISQUE/NIQE descriptions had diverged."""
        english = Translator(DEFAULT_LANGUAGE)._strings[DEFAULT_LANGUAGE]

        assert sorted(key for key in english if _is_per_metric_key(key)) == []

    def test_non_english_locales_have_every_per_metric_key(self) -> None:
        """The per-metric text (metric.<id>.name_full/description/
        value_range/param.<name>) is sourced from the YAML descriptor cards
        for English (see MetricDescriptor.as_docstring/Translator.tr_or) and
        therefore is NOT expected in en.json - but it must exist, non-empty,
        in every other language."""
        import yaml

        descriptors_dir = Path(__file__).parents[2] / "qicom/metrics/descriptors"
        expected_keys: set[str] = set()
        for path in descriptors_dir.glob("*.yaml"):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            metric_id = raw["id"]
            expected_keys.update(
                {
                    f"metric.{metric_id}.name_full",
                    f"metric.{metric_id}.description",
                    f"metric.{metric_id}.value_range",
                }
            )
            for param_name in raw.get("parameters") or {}:
                expected_keys.add(f"metric.{metric_id}.param.{param_name}")

        for language in SUPPORTED_LANGUAGES:
            if language == DEFAULT_LANGUAGE:
                continue
            strings = Translator(language)._strings[language]
            for key in expected_keys:
                assert strings.get(key), f"{language}.json is missing a translation for {key}"
