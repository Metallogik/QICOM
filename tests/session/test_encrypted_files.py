"""Tests for qicom.session.encrypted_files."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from qicom.session.encrypted_files import (
    ENCRYPTED_FILE_FORMAT_VERSION,
    MAX_ENCRYPTED_FILE_BYTES,
    EncryptedFileError,
    new_key,
    open_sealed,
    read_encrypted_json,
    seal,
    write_encrypted_json,
)


def test_a_payload_round_trips(tmp_path: Path) -> None:
    key = new_key()
    path = tmp_path / "file.qdraft"
    payload = {"answers": [1, 2, 3], "name": "Studio TC"}

    write_encrypted_json(path, payload, key, "owner/file")

    assert read_encrypted_json(path, key, "owner/file") == payload
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["qicom_encrypted_version"] == ENCRYPTED_FILE_FORMAT_VERSION
    assert "Studio" not in path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_file_is_private_to_this_os_account(tmp_path: Path) -> None:
    path = tmp_path / "file.qdraft"
    write_encrypted_json(path, {}, new_key(), "c")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_another_key_cannot_open_it(tmp_path: Path) -> None:
    path = tmp_path / "file.qdraft"
    write_encrypted_json(path, {"a": 1}, new_key(), "c")

    with pytest.raises(EncryptedFileError):
        read_encrypted_json(path, new_key(), "c")


def test_a_file_moved_to_another_context_is_refused(tmp_path: Path) -> None:
    """Copied into another person's folder, or swapped for another file."""
    key = new_key()
    path = tmp_path / "file.qdraft"
    write_encrypted_json(path, {"a": 1}, key, "draft/mario/one")

    with pytest.raises(EncryptedFileError):
        read_encrypted_json(path, key, "draft/luigi/one")
    with pytest.raises(EncryptedFileError):
        read_encrypted_json(path, key, "draft/mario/two")


def test_an_altered_byte_is_detected() -> None:
    key = new_key()
    envelope = seal(b"secret answers", key, "c")
    ciphertext = bytearray(__import__("base64").b64decode(envelope["ciphertext"]))
    ciphertext[0] ^= 1
    envelope["ciphertext"] = __import__("base64").b64encode(bytes(ciphertext)).decode()

    with pytest.raises(EncryptedFileError):
        open_sealed(envelope, key, "c")


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        [],
        {"qicom_encrypted_version": 2, "algorithm": "AES-256-GCM"},
        {"qicom_encrypted_version": True, "algorithm": "AES-256-GCM"},
        {"qicom_encrypted_version": 1, "algorithm": "ROT13", "nonce": "", "ciphertext": ""},
        {"qicom_encrypted_version": 1, "algorithm": "AES-256-GCM", "nonce": "!", "ciphertext": ""},
        {
            "qicom_encrypted_version": 1,
            "algorithm": "AES-256-GCM",
            "nonce": "AA==",
            "ciphertext": "",
        },
    ],
)
def test_anything_else_is_refused_not_guessed(envelope: object) -> None:
    with pytest.raises(EncryptedFileError):
        open_sealed(envelope, new_key(), "c")


def test_unreadable_or_oversized_files_are_refused(tmp_path: Path) -> None:
    key = new_key()
    garbage = tmp_path / "garbage.qdraft"
    garbage.write_bytes(b"\xff\xfe not json")
    with pytest.raises(EncryptedFileError):
        read_encrypted_json(garbage, key, "c")

    huge = tmp_path / "huge.qdraft"
    with huge.open("wb") as f:
        f.truncate(MAX_ENCRYPTED_FILE_BYTES + 1)
    with pytest.raises(EncryptedFileError):
        read_encrypted_json(huge, key, "c")


def test_a_payload_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    key = new_key()
    path = tmp_path / "list.qdraft"
    path.write_text(json.dumps(seal(b"[1, 2]", key, "c")), encoding="utf-8")

    with pytest.raises(EncryptedFileError):
        read_encrypted_json(path, key, "c")
