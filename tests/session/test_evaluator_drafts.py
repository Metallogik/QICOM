"""Tests for qicom.session.evaluator_drafts."""

from __future__ import annotations

import shutil
from pathlib import Path

import platformdirs
import pytest

from qicom.session.evaluator_accounts import create_account
from qicom.session.evaluator_drafts import (
    DraftStore,
    DraftSummary,
    discard_legacy_pending_index,
    draft_file_name,
)


def _summary(draft_file: str, package: Path, **overrides: object) -> DraftSummary:
    values: dict[str, object] = {
        "draft_file": draft_file,
        "package_path": package,
        "name": "Studio TC",
        "answered": 3,
        "page_position": 2,
        "page_count": 4,
        "page_id": 7,
        "saved_at": "2026-10-12T09:30:00+00:00",
    }
    values.update(overrides)
    return DraftSummary(**values)  # type: ignore[arg-type]


def test_a_draft_round_trips_encrypted(tmp_path: Path) -> None:
    account = create_account("mario", "secret")
    store = DraftStore(account)
    name = draft_file_name("test-id", None)
    summary = _summary(name, tmp_path / "study.zip")

    store.save(summary, {"answers": ["the answers"]})

    assert store.load(name) == (summary, {"answers": ["the answers"]})
    on_disk = (account.data_dir / name).read_text(encoding="utf-8")
    assert "Studio" not in on_disk and "the answers" not in on_disk


def test_the_file_name_says_nothing_about_the_study() -> None:
    name = draft_file_name("chest-ct-study", "delivery-2")

    assert "chest" not in name and "delivery" not in name
    assert name != draft_file_name("chest-ct-study", None)


def test_summaries_list_newest_first_and_only_this_accounts(tmp_path: Path) -> None:
    mario = DraftStore(create_account("mario", "secret"))
    luigi = DraftStore(create_account("luigi", "secret"))
    older = draft_file_name("a", None)
    newer = draft_file_name("b", None)
    mario.save(_summary(older, tmp_path / "a.zip", saved_at="2026-10-01T00:00:00+00:00"), {})
    mario.save(_summary(newer, tmp_path / "b.zip", saved_at="2026-10-02T00:00:00+00:00"), {})
    luigi.save(_summary(older, tmp_path / "a.zip"), {})

    assert [s.draft_file for s in mario.summaries()] == [newer, older]
    assert len(luigi.summaries()) == 1


def test_a_copied_draft_cannot_be_opened_by_another_account(tmp_path: Path) -> None:
    mario = create_account("mario", "secret")
    luigi = create_account("luigi", "secret")
    name = draft_file_name("a", None)
    DraftStore(mario).save(_summary(name, tmp_path / "a.zip"), {})
    luigi.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(mario.data_dir / name, luigi.data_dir / name)

    [listed] = DraftStore(luigi).summaries()

    assert not listed.readable
    with pytest.raises(ValueError):
        DraftStore(luigi).load(name)


def test_mark_completed_moves_it_to_the_finished_ones(tmp_path: Path) -> None:
    store = DraftStore(create_account("mario", "secret"))
    name = draft_file_name("a", None)
    store.save(_summary(name, tmp_path / "a.zip"), {"keep": True})

    store.mark_completed(name, "2026-10-12T10:00:00+00:00")

    summary, answers = store.load(name) or (None, None)
    assert summary is not None and summary.completed
    assert answers == {"keep": True}


def test_delete_and_set_aside(tmp_path: Path) -> None:
    store = DraftStore(create_account("mario", "secret"))
    first, second = draft_file_name("a", None), draft_file_name("b", None)
    store.save(_summary(first, tmp_path / "a.zip"), {})
    store.save(_summary(second, tmp_path / "b.zip"), {})

    store.delete(first)
    moved = store.set_aside(second)

    assert store.summaries() == []
    assert moved is not None and moved.exists() and "unreadable" in moved.name


def test_only_draft_file_names_are_accepted() -> None:
    store = DraftStore(create_account("mario", "secret"))

    with pytest.raises(ValueError):
        store.delete("../accounts.json")


def test_the_plaintext_list_of_an_earlier_build_is_deleted() -> None:
    legacy = Path(platformdirs.user_data_dir("qicom", appauthor=False)) / "pending_evaluations.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"pending": [{"package_path": "/patients/x.zip"}]}', encoding="utf-8")

    discard_legacy_pending_index()

    assert not legacy.exists()
