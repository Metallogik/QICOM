"""Evaluation files an author locks with an access code, so only those who have it can use them.

Three kinds: the package sent to evaluators, the answers they send back, the
de-blinding map.

The code is enforced by decryption, not by QICOM. A package is a zip anyone
can open with any tool, so a flag inside it that only QICOM honours would
protect nothing; here the key scrypt derives from the code encrypts the whole
package, and whoever lacks the code cannot open, edit or aggregate it, in
QICOM or out of it.

A protected package keeps the .zip name and shape - an outer zip holding a
small clear header and one encrypted payload, which is the ordinary package
zip - so the file dialogs stay the same, and after decryption the package
goes through every check an unprotected one does. The payload is encrypted in
1 MiB chunks, each AES-256-GCM with its index and a last-chunk flag in the
nonce: a package of several gigabytes never has to fit in memory, and one
that was truncated, reordered or extended is refused.

Answers and de-blinding maps are JSON, wrapped the same way in one piece.
Every protected file carries the scrypt parameters it was made with and a
check value, so a wrong code is told apart from a damaged file at once.

One code is shared by every evaluator: it decides who can open the
evaluation, not who answered. There is no recovery.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.schema_version import is_supported_version
from qicom.session.archive_safety import EVALUATION_PACKAGE_LIMITS, load_bounded_json_object
from qicom.session.encrypted_files import EncryptedFileError, open_sealed, seal
from qicom.session.key_derivation import derive_key, new_kdf, valid_kdf
from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

PROTECTED_FORMAT_VERSION = 1
MIN_ACCESS_CODE_LENGTH = 10
_VERSION_KEY = "qicom_protected_version"
HEADER_NAME = "qicom_protected.json"
PAYLOAD_NAME = "payload.bin"
KIND_PACKAGE = "evaluation-package"
KIND_ANSWERS = "answers"
KIND_DEBLINDING_MAP = "deblinding-map"
_KINDS = frozenset({KIND_PACKAGE, KIND_ANSWERS, KIND_DEBLINDING_MAP})
CHUNK_BYTES = 1024 * 1024
_TAG_BYTES = 16
_NONCE_PREFIX_BYTES = 7
_MAX_HEADER_BYTES = 64 * 1024
# Unambiguous when read aloud or copied by hand: no 0/O, 1/I/L.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class AccessCodeTooShort(ValueError):
    pass


class WrongAccessCode(ValueError):
    pass


class ProtectedFileError(ValueError):
    """Damaged, altered, truncated, or written by a newer QICOM."""


class AccessCodeRequired(ValueError):
    """The file is protected and no code known so far opens it. Carries the
    header, so the caller can ask for a code and try again."""

    def __init__(self, header: dict[str, Any]) -> None:
        super().__init__("This file is protected by an access code.")
        self.header = header


@dataclass(frozen=True)
class AccessKey:
    kdf: dict[str, Any]
    key: bytes = field(repr=False)

    @property
    def salt(self) -> str:
        return str(self.kdf["salt"])

    def to_payload(self) -> dict[str, Any]:
        """For an evaluator's own draft, which is itself encrypted: resuming
        a protected package then does not ask for its code again."""
        return {"kdf": self.kdf, "key": base64.b64encode(self.key).decode("ascii")}

    @classmethod
    def from_payload(cls, payload: object) -> AccessKey | None:
        if not isinstance(payload, dict) or not valid_kdf(payload.get("kdf")):
            return None
        try:
            key = base64.b64decode(str(payload.get("key")), validate=True)
        except (binascii.Error, ValueError):
            return None
        return cls(kdf=payload["kdf"], key=key) if len(key) == 32 else None


def validate_access_code(code: str) -> str:
    normalized = unicodedata.normalize("NFC", code.strip())
    if len(normalized) < MIN_ACCESS_CODE_LENGTH:
        raise AccessCodeTooShort(str(MIN_ACCESS_CODE_LENGTH))
    return normalized


def generate_access_code() -> str:
    """16 characters in four groups, about 79 bits: beyond guessing even for
    someone who holds the package and tries codes offline."""
    characters = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(16))
    return "-".join(characters[i : i + 4] for i in range(0, 16, 4))


def new_access_key(code: str) -> AccessKey:
    """The key a new protected file is made with; refuses a short code."""
    normalized = validate_access_code(code)
    kdf = new_kdf()
    return AccessKey(kdf=kdf, key=derive_key(normalized, kdf))


def _header(kind: str, access: AccessKey) -> dict[str, Any]:
    return {
        _VERSION_KEY: PROTECTED_FORMAT_VERSION,
        "kind": kind,
        "kdf": access.kdf,
        "check": seal(b"", access.key, f"check/{kind}"),
    }


def _validated_header(header: object, kind: str) -> dict[str, Any]:
    if not isinstance(header, dict):
        raise ProtectedFileError("Not a protected QICOM file.")
    if not is_supported_version(header.get(_VERSION_KEY), {PROTECTED_FORMAT_VERSION}):
        raise ProtectedFileError("Unsupported protected file version.")
    if header.get("kind") != kind or not valid_kdf(header.get("kdf")):
        raise ProtectedFileError("Not a protected QICOM file of the expected kind.")
    if not isinstance(header.get("check"), dict):
        raise ProtectedFileError("Damaged protected file.")
    return header


def _opens(access: AccessKey, header: dict[str, Any]) -> bool:
    if access.salt != header["kdf"]["salt"]:
        return False
    try:
        open_sealed(header["check"], access.key, f"check/{header['kind']}")
    except EncryptedFileError:
        return False
    return True


class AccessKeyring:
    """The codes entered during one task and the keys derived from them, so
    a code is asked for once, not once per file. Every protected file has
    its own salt; a code already entered is tried on each new one before
    asking again."""

    def __init__(self) -> None:
        self._codes: list[str] = []
        self._keys: dict[str, AccessKey] = {}

    def add(self, access: AccessKey) -> None:
        self._keys[access.salt] = access

    def key_for(self, header: dict[str, Any]) -> AccessKey | None:
        known = self._keys.get(header["kdf"]["salt"])
        if known is not None and _opens(known, header):
            return known
        for code in self._codes:
            candidate = AccessKey(kdf=header["kdf"], key=derive_key(code, header["kdf"]))
            if _opens(candidate, header):
                self.add(candidate)
                return candidate
        return None

    def remember_code(self, code: str) -> None:
        """A code to try on every protected file met from now on, for a
        caller that has it up front (a headless batch) rather than asking."""
        self._codes.append(unicodedata.normalize("NFC", code.strip()))

    def unlock(self, header: dict[str, Any], code: str) -> AccessKey:
        normalized = unicodedata.normalize("NFC", code.strip())
        candidate = AccessKey(kdf=header["kdf"], key=derive_key(normalized, header["kdf"]))
        if not _opens(candidate, header):
            raise WrongAccessCode()
        self._codes.append(normalized)
        self.add(candidate)
        return candidate


# -- Packages -------------------------------------------------------------


def is_protected_package(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return HEADER_NAME in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def read_package_header(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(archive.namelist())
            if names != sorted((HEADER_NAME, PAYLOAD_NAME)):
                raise ProtectedFileError("Unexpected members in a protected package.")
            info = archive.getinfo(HEADER_NAME)
            if info.file_size > _MAX_HEADER_BYTES:
                raise ProtectedFileError("Protected package header is too large.")
            header = json.loads(archive.read(HEADER_NAME).decode("utf-8"))
    except (zipfile.BadZipFile, ValueError, KeyError) as error:
        if isinstance(error, ProtectedFileError):
            raise
        raise ProtectedFileError("Damaged protected package.") from error
    header = _validated_header(header, KIND_PACKAGE)
    try:
        prefix = base64.b64decode(str(header.get("nonce_prefix")), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ProtectedFileError("Damaged protected package.") from error
    if len(prefix) != _NONCE_PREFIX_BYTES or header.get("chunk_bytes") != CHUNK_BYTES:
        raise ProtectedFileError("Damaged protected package.")
    return header


def _associated_data(header: dict[str, Any]) -> bytes:
    canonical = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return b"qicom-protected-payload/" + hashlib.sha256(canonical).digest()


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    if index >= 2**32:
        raise ProtectedFileError("Protected payload is too large.")
    return prefix + index.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            break
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def protect_package(source_zip: Path, destination: Path, access: AccessKey) -> None:
    """Write `source_zip`, encrypted, as a protected package at
    `destination`, atomically."""
    prefix = os.urandom(_NONCE_PREFIX_BYTES)
    header = {
        **_header(KIND_PACKAGE, access),
        "chunk_bytes": CHUNK_BYTES,
        "nonce_prefix": base64.b64encode(prefix).decode("ascii"),
    }
    cipher = AESGCM(access.key)
    associated = _associated_data(header)
    temp_path = temporary_sibling(destination)
    try:
        with zipfile.ZipFile(temp_path, "x") as archive:
            archive.writestr(HEADER_NAME, json.dumps(header, indent=2), zipfile.ZIP_DEFLATED)
            info = zipfile.ZipInfo(PAYLOAD_NAME)
            info.compress_type = zipfile.ZIP_STORED  # ciphertext does not compress
            with source_zip.open("rb") as source, archive.open(info, "w", force_zip64=True) as out:
                index = 0
                chunk = _read_exact(source, CHUNK_BYTES)
                while True:
                    following = _read_exact(source, CHUNK_BYTES) if chunk else b""
                    last = not following
                    out.write(cipher.encrypt(_nonce(prefix, index, last), chunk, associated))
                    if last:
                        break
                    chunk = following
                    index += 1
        commit_temporary(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def decrypt_package(protected: Path, access: AccessKey, destination: Path) -> None:
    """Decrypt a protected package into the ordinary package zip at
    `destination`. WrongAccessCode before anything is written when the key
    does not open it; ProtectedFileError for a damaged one, after which
    nothing is left at `destination`."""
    header = read_package_header(protected)
    if not _opens(access, header):
        raise WrongAccessCode()
    prefix = base64.b64decode(header["nonce_prefix"])
    cipher = AESGCM(access.key)
    associated = _associated_data(header)
    sealed = CHUNK_BYTES + _TAG_BYTES
    try:
        with zipfile.ZipFile(protected) as archive:
            payload = archive.getinfo(PAYLOAD_NAME)
            limit = EVALUATION_PACKAGE_LIMITS.max_uncompressed_bytes
            if payload.file_size > limit + (limit // CHUNK_BYTES + 1) * _TAG_BYTES:
                raise ProtectedFileError("Protected package is too large.")
            free = shutil.disk_usage(destination.parent).free
            if payload.file_size * EVALUATION_PACKAGE_LIMITS.free_space_margin > free:
                raise ProtectedFileError("Not enough free disk space to open the package.")
            with archive.open(payload) as source, destination.open("xb") as out:
                index = 0
                chunk = _read_exact(source, sealed)
                while True:
                    following = _read_exact(source, sealed) if len(chunk) == sealed else b""
                    last = not following
                    out.write(cipher.decrypt(_nonce(prefix, index, last), chunk, associated))
                    if last:
                        break
                    chunk = following
                    index += 1
    except (InvalidTag, zipfile.BadZipFile, KeyError) as error:
        destination.unlink(missing_ok=True)
        raise ProtectedFileError("Damaged or altered protected package.") from error
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


@contextmanager
def readable_package(path: Path, access: AccessKey | None) -> Iterator[Path]:
    """The ordinary package zip for `path`, for as long as the block runs:
    `path` itself when it is not protected, a decrypted temporary copy -
    deleted afterwards - when it is."""
    if not is_protected_package(path):
        yield path
        return
    if access is None:
        raise AccessCodeRequired(read_package_header(path))
    folder = Path(tempfile.mkdtemp(prefix="qicom_protected_"))
    try:
        target = folder / "package.zip"
        decrypt_package(path, access, target)
        yield target
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def decrypt_package_to_temporary(path: Path, access: AccessKey) -> tuple[Path, Path]:
    """Like readable_package, for a caller that keeps the decrypted copy for
    a whole session (the evaluation window reads pages from it lazily).
    Returns (package zip, folder to remove with shutil.rmtree when done)."""
    folder = Path(tempfile.mkdtemp(prefix="qicom_protected_"))
    try:
        target = folder / "package.zip"
        decrypt_package(path, access, target)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return target, folder


# -- JSON files (answers, de-blinding maps) -------------------------------


def write_protected_json(path: Path, payload: dict[str, Any], kind: str, access: AccessKey) -> None:
    if kind not in _KINDS - {KIND_PACKAGE}:
        raise ValueError(f"Not a protected JSON kind: {kind!r}")
    document = {
        **_header(kind, access),
        "envelope": seal(json.dumps(payload).encode("utf-8"), access.key, f"{kind}/content"),
    }
    ensure_private_directory(path.parent)
    temp_path = temporary_sibling(path)
    try:
        with temp_path.open("x", encoding="utf-8") as f:
            json.dump(document, f, indent=2)
        ensure_private_file(temp_path)
        commit_temporary(temp_path, path)
        ensure_private_file(path)
    finally:
        temp_path.unlink(missing_ok=True)


def is_protected_document(document: object) -> bool:
    return isinstance(document, dict) and _VERSION_KEY in document


def open_protected_document(
    document: dict[str, Any], kind: str, keyring: AccessKeyring | None
) -> dict[str, Any]:
    header = _validated_header(document, kind)
    access = None if keyring is None else keyring.key_for(header)
    if access is None:
        raise AccessCodeRequired(header)
    try:
        plaintext = open_sealed(document.get("envelope"), access.key, f"{kind}/content")
        payload = json.loads(plaintext.decode("utf-8"))
    except (EncryptedFileError, ValueError) as error:
        raise ProtectedFileError("Damaged or altered protected file.") from error
    if not isinstance(payload, dict):
        raise ProtectedFileError("Damaged protected file.")
    return payload


def load_json_document(
    path: Path, kind: str, keyring: AccessKeyring | None, *, max_bytes: int | None = None
) -> dict[str, Any]:
    """A JSON file of `kind`, whether protected or not."""
    document = (
        load_bounded_json_object(path)
        if max_bytes is None
        else load_bounded_json_object(path, max_bytes=max_bytes)
    )
    if is_protected_document(document):
        return open_protected_document(document, kind, keyring)
    return document
