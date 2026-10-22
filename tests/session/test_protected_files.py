"""Tests for qicom.session.protected_files."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from qicom.session import protected_files as protected_module
from qicom.session.protected_files import (
    HEADER_NAME,
    KIND_ANSWERS,
    KIND_DEBLINDING_MAP,
    MIN_ACCESS_CODE_LENGTH,
    PAYLOAD_NAME,
    AccessCodeRequired,
    AccessCodeTooShort,
    AccessKey,
    AccessKeyring,
    ProtectedFileError,
    WrongAccessCode,
    decrypt_package,
    generate_access_code,
    is_protected_package,
    load_json_document,
    new_access_key,
    protect_package,
    read_package_header,
    readable_package,
    write_protected_json,
)

CODE = "correct-horse-1"


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> int:
    """Several chunks without megabytes of test data."""
    monkeypatch.setattr(protected_module, "CHUNK_BYTES", 64)
    return 64


def _plain_zip(path: Path, payload: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("evaluation.json", payload)
    return path


def test_a_generated_code_is_long_and_unambiguous() -> None:
    code = generate_access_code()

    groups = code.split("-")
    assert [len(group) for group in groups] == [4, 4, 4, 4]
    assert not set("".join(groups)) & set("01IOL")
    assert generate_access_code() != code


def test_ten_characters_are_the_minimum() -> None:
    assert MIN_ACCESS_CODE_LENGTH == 10
    with pytest.raises(AccessCodeTooShort):
        new_access_key("  123456789  ")
    assert new_access_key("1234567890").key


@pytest.mark.parametrize("size", [0, 10, 64, 128, 64 * 3 + 5])
def test_a_package_round_trips_across_chunk_boundaries(
    tmp_path: Path, small_chunks: int, size: int
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * (size // 256) + bytes(range(size % 256)))
    access = new_access_key(CODE)
    protected = tmp_path / "protected.zip"

    protect_package(source, protected, access)
    decrypt_package(protected, access, tmp_path / "out.bin")

    assert (tmp_path / "out.bin").read_bytes() == source.read_bytes()
    assert is_protected_package(protected)


def test_nothing_of_the_package_is_readable_without_the_code(tmp_path: Path) -> None:
    source = _plain_zip(tmp_path / "plain.zip", b'{"title": "Studio TC torace"}')
    protected = tmp_path / "protected.zip"

    protect_package(source, protected, new_access_key(CODE))

    with zipfile.ZipFile(protected) as archive:
        assert sorted(archive.namelist()) == sorted([HEADER_NAME, PAYLOAD_NAME])
    assert b"Studio TC torace" not in protected.read_bytes()
    assert not is_protected_package(source)
    assert not is_protected_package(tmp_path / "missing.zip")


def test_the_right_code_opens_and_a_wrong_one_does_not(tmp_path: Path) -> None:
    source = _plain_zip(tmp_path / "plain.zip", b"{}")
    protected = tmp_path / "protected.zip"
    protect_package(source, protected, new_access_key(CODE))
    header = read_package_header(protected)
    keyring = AccessKeyring()

    with pytest.raises(WrongAccessCode):
        keyring.unlock(header, "wrong-code-123")
    access = keyring.unlock(header, f"  {CODE}  ")

    assert keyring.key_for(header) == access
    with pytest.raises(WrongAccessCode):
        decrypt_package(protected, new_access_key("another-code-1"), tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()


def _rewrite_payload(protected: Path, target: Path, change: object) -> None:
    with zipfile.ZipFile(protected) as archive:
        header = archive.read(HEADER_NAME)
        payload = archive.read(PAYLOAD_NAME)
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(HEADER_NAME, header)
        archive.writestr(PAYLOAD_NAME, change(payload))  # type: ignore[operator]


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload[: 64 + 16],  # last chunk dropped
        lambda payload: payload[64 + 16 :] + payload[: 64 + 16],  # chunks swapped
        lambda payload: payload + payload[-20:],  # extended
        lambda payload: payload[:-1] + bytes([payload[-1] ^ 1]),  # one bit flipped
    ],
)
def test_a_truncated_reordered_or_altered_payload_is_refused(
    tmp_path: Path, small_chunks: int, change: object
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * (64 * 2 + 7))
    access = new_access_key(CODE)
    protected = tmp_path / "protected.zip"
    protect_package(source, protected, access)
    tampered = tmp_path / "tampered.zip"
    _rewrite_payload(protected, tampered, change)

    with pytest.raises(ProtectedFileError):
        decrypt_package(tampered, access, tmp_path / "out.bin")
    assert not (tmp_path / "out.bin").exists()


def test_an_extra_member_or_an_edited_header_is_refused(tmp_path: Path) -> None:
    source = _plain_zip(tmp_path / "plain.zip", b"{}")
    access = new_access_key(CODE)
    protected = tmp_path / "protected.zip"
    protect_package(source, protected, access)
    with zipfile.ZipFile(protected) as archive:
        header = json.loads(archive.read(HEADER_NAME))
        payload = archive.read(PAYLOAD_NAME)

    extra = tmp_path / "extra.zip"
    with zipfile.ZipFile(extra, "w") as archive:
        archive.writestr(HEADER_NAME, json.dumps(header))
        archive.writestr(PAYLOAD_NAME, payload)
        archive.writestr("evaluation.json", "{}")
    with pytest.raises(ProtectedFileError):
        read_package_header(extra)

    header["qicom_protected_version"] = 2
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(newer, "w") as archive:
        archive.writestr(HEADER_NAME, json.dumps(header))
        archive.writestr(PAYLOAD_NAME, payload)
    with pytest.raises(ProtectedFileError):
        read_package_header(newer)


def test_readable_package_cleans_up_after_itself(tmp_path: Path) -> None:
    plain = _plain_zip(tmp_path / "plain.zip", b"{}")
    access = new_access_key(CODE)
    protected = tmp_path / "protected.zip"
    protect_package(plain, protected, access)

    with readable_package(plain, None) as same:
        assert same == plain
    with pytest.raises(AccessCodeRequired):
        with readable_package(protected, None):
            pass
    with readable_package(protected, access) as decrypted:
        assert decrypted.read_bytes() == plain.read_bytes()
    assert not decrypted.exists()


@pytest.mark.parametrize("kind", [KIND_ANSWERS, KIND_DEBLINDING_MAP])
def test_json_files_round_trip_and_need_the_code(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "file.json"
    write_protected_json(
        path, {"test_id": "study-1", "secret": "R = filtered"}, kind, new_access_key(CODE)
    )

    assert "filtered" not in path.read_text(encoding="utf-8")
    with pytest.raises(AccessCodeRequired) as required:
        load_json_document(path, kind, None)
    keyring = AccessKeyring()
    keyring.unlock(required.value.header, CODE)
    assert load_json_document(path, kind, keyring)["secret"] == "R = filtered"


def test_a_json_file_of_another_kind_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    write_protected_json(path, {}, KIND_ANSWERS, new_access_key(CODE))
    keyring = AccessKeyring()
    keyring.remember_code(CODE)

    with pytest.raises(ProtectedFileError):
        load_json_document(path, KIND_DEBLINDING_MAP, keyring)


def test_a_remembered_code_opens_files_with_their_own_salt(tmp_path: Path) -> None:
    """Each protected file has its own salt; the code, typed once, opens all."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    write_protected_json(first, {"n": 1}, KIND_ANSWERS, new_access_key(CODE))
    write_protected_json(second, {"n": 2}, KIND_ANSWERS, new_access_key(CODE))
    keyring = AccessKeyring()
    keyring.remember_code(CODE)

    assert load_json_document(first, KIND_ANSWERS, keyring) == {"n": 1}
    assert load_json_document(second, KIND_ANSWERS, keyring) == {"n": 2}


def test_plain_json_is_read_as_before(tmp_path: Path) -> None:
    path = tmp_path / "plain.json"
    path.write_text('{"test_id": "x"}', encoding="utf-8")

    assert load_json_document(path, KIND_ANSWERS, None) == {"test_id": "x"}


def test_an_access_key_survives_a_draft_round_trip() -> None:
    access = new_access_key(CODE)

    assert AccessKey.from_payload(access.to_payload()) == access
    assert AccessKey.from_payload({"kdf": access.kdf, "key": "short"}) is None
    assert AccessKey.from_payload(None) is None
