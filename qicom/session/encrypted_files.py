"""Authenticated encryption for what QICOM keeps for one person on a shared computer.

AES-256-GCM, from `cryptography`: the standard library has a password hash
(scrypt) but no cipher. GCM authenticates as well as encrypts, so a file that
was altered, or opened with the wrong key, is refused rather than read as
something else.

Every file is also bound to a *context* - its owner and its own name - through
the associated data. A file copied into another person's folder, or swapped
for another file of the same person, then fails exactly like a damaged one.

The envelope is plain JSON, so a reader can tell an encrypted QICOM file and
its version apart without the key; nothing else in it is readable.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.schema_version import is_supported_version
from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

ENCRYPTED_FILE_FORMAT_VERSION = 1
KEY_BYTES = 32
_VERSION_KEY = "qicom_encrypted_version"
_ALGORITHM = "AES-256-GCM"
_NONCE_BYTES = 12
# Far above any evaluation's answers; a bound, so a planted file cannot make
# QICOM read gigabytes before finding out it is not one of its own.
MAX_ENCRYPTED_FILE_BYTES = 64 * 1024 * 1024


class EncryptedFileError(ValueError):
    """Not a readable QICOM encrypted file for this key and context: damaged,
    altered, someone else's, or written by a newer QICOM."""


def new_key() -> bytes:
    return AESGCM.generate_key(bit_length=8 * KEY_BYTES)


def _associated_data(context: str) -> bytes:
    return f"qicom/{ENCRYPTED_FILE_FORMAT_VERSION}/{context}".encode()


def seal(plaintext: bytes, key: bytes, context: str) -> dict[str, Any]:
    """Encrypt `plaintext` into a JSON-serializable envelope."""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _associated_data(context))
    return {
        _VERSION_KEY: ENCRYPTED_FILE_FORMAT_VERSION,
        "algorithm": _ALGORITHM,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def open_sealed(envelope: object, key: bytes, context: str) -> bytes:
    if not isinstance(envelope, dict):
        raise EncryptedFileError("Not an encrypted QICOM file.")
    if not is_supported_version(envelope.get(_VERSION_KEY), {ENCRYPTED_FILE_FORMAT_VERSION}):
        raise EncryptedFileError("Unsupported encrypted file version.")
    if envelope.get("algorithm") != _ALGORITHM:
        raise EncryptedFileError("Unsupported encryption algorithm.")
    try:
        nonce = base64.b64decode(str(envelope.get("nonce")), validate=True)
        ciphertext = base64.b64decode(str(envelope.get("ciphertext")), validate=True)
    except (binascii.Error, ValueError) as error:
        raise EncryptedFileError("Damaged encrypted file.") from error
    if len(nonce) != _NONCE_BYTES or len(key) != KEY_BYTES:
        raise EncryptedFileError("Damaged encrypted file.")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _associated_data(context))
    except InvalidTag as error:
        raise EncryptedFileError("Wrong key, or the file was altered.") from error


def write_encrypted_json(path: Path, payload: dict[str, Any], key: bytes, context: str) -> None:
    """Atomically replace `path` with `payload`, encrypted; private to this OS account."""
    envelope = seal(json.dumps(payload).encode("utf-8"), key, context)
    ensure_private_directory(path.parent)
    temp_path = temporary_sibling(path)
    try:
        with temp_path.open("x", encoding="utf-8") as f:
            json.dump(envelope, f)
        ensure_private_file(temp_path)
        commit_temporary(temp_path, path)
        ensure_private_file(path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_encrypted_json(path: Path, key: bytes, context: str) -> dict[str, Any]:
    """The payload of `path`. OSError if it cannot be read at all,
    EncryptedFileError if it is not what this key and context sealed."""
    if path.stat().st_size > MAX_ENCRYPTED_FILE_BYTES:
        raise EncryptedFileError("Encrypted file is too large.")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:  # JSON and UTF-8 decoding
        raise EncryptedFileError("Not an encrypted QICOM file.") from error
    plaintext = open_sealed(envelope, key, context)
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except ValueError as error:
        raise EncryptedFileError("Damaged encrypted content.") from error
    if not isinstance(payload, dict):
        raise EncryptedFileError("Damaged encrypted content.")
    return payload
