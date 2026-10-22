"""Tests for qicom.file_locks: writers of one pair of files take turns."""

from __future__ import annotations

import threading
from pathlib import Path

from qicom.file_locks import exclusive_locks


def test_a_held_path_blocks_another_writer_until_released(tmp_path: Path) -> None:
    first, second = tmp_path / "delivery.zip", tmp_path / "delivery.deblinding.json"
    entered = threading.Event()

    def wait_for_second() -> None:
        with exclusive_locks(second):
            entered.set()

    with exclusive_locks(first, second):
        waiter = threading.Thread(target=wait_for_second)
        waiter.start()
        assert not entered.wait(0.3)
    waiter.join(5)
    assert entered.is_set()


def test_overlapping_pairs_asked_in_opposite_orders_do_not_deadlock(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    done: list[str] = []

    def lock(order: tuple[Path, Path], name: str) -> None:
        for _round in range(50):
            with exclusive_locks(*order):
                pass
        done.append(name)

    threads = [
        threading.Thread(target=lock, args=((a, b), "ab")),
        threading.Thread(target=lock, args=((b, a), "ba")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(done) == ["ab", "ba"]


def test_one_path_by_two_spellings_is_one_lock(tmp_path: Path) -> None:
    target = tmp_path / "x.zip"
    entered = threading.Event()

    def other_spelling() -> None:
        with exclusive_locks(tmp_path / "." / "x.zip"):
            entered.set()

    with exclusive_locks(target):
        waiter = threading.Thread(target=other_spelling)
        waiter.start()
        assert not entered.wait(0.3)
    waiter.join(5)
    assert entered.is_set()
    assert not list(tmp_path.iterdir())  # nothing left beside the user's files
