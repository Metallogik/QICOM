"""Evaluator accounts on a shared computer: who is answering, and their encryption key.

Before accounts, every unfinished evaluation on a computer belonged to the
computer: whoever opened Take Evaluation could continue, or delete, someone
else's. An account is an ID, a random key, and that key encrypted under one
derived from the password with scrypt. The password itself is stored
nowhere - it is right when it opens the key - and changing it re-encrypts
only the key, never the evaluations.

There is no recovery, by design: any way back in without the password would
be a way in for anyone. A forgotten password costs that account's unfinished
evaluations on this computer; the packages themselves can be taken again.

Accounts separate people inside QICOM. They are not an operating-system
boundary: whoever shares the OS account can still delete the files, though
not read or alter them unnoticed (see SECURITY.md).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import platformdirs

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.schema_version import is_supported_version
from qicom.session.encrypted_files import EncryptedFileError, new_key, open_sealed, seal
from qicom.session.key_derivation import derive_key, new_kdf, valid_kdf
from qicom.session.models import utc_now_iso
from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

ACCOUNTS_FORMAT_VERSION = 1
_VERSION_KEY = "qicom_evaluator_accounts_version"
_ACCOUNTS_FILE_NAME = "accounts.json"
MIN_PASSWORD_LENGTH = 5
MAX_USERNAME_LENGTH = 64
_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")

logger = logging.getLogger(__name__)


class AccountError(ValueError):
    """Base of every refusal; each subclass is one message the GUI can give."""


class InvalidCredentials(AccountError):
    """Unknown ID or wrong password - deliberately the same error."""


class UsernameTaken(AccountError):
    pass


class InvalidUsername(AccountError):
    pass


class PasswordTooShort(AccountError):
    pass


class AccountsUnavailable(AccountError):
    """The accounts file is damaged or was written by a newer QICOM. It is
    never overwritten: every account's key is in it."""


@dataclass(frozen=True)
class EvaluatorAccount:
    """A logged-in account. Lives in memory only, for as long as the
    evaluation window that needs it."""

    account_id: str
    username: str
    key: bytes = field(repr=False)

    @property
    def data_dir(self) -> Path:
        return accounts_root() / self.account_id


def accounts_root() -> Path:
    return Path(platformdirs.user_data_dir("qicom", appauthor=False)) / "evaluators"


def _accounts_path() -> Path:
    return accounts_root() / _ACCOUNTS_FILE_NAME


def normalize_username(username: str) -> str:
    return unicodedata.normalize("NFC", username.strip())


def _username_key(username: str) -> str:
    return normalize_username(username).casefold()


def validate_username(username: str) -> str:
    name = normalize_username(username)
    if not name or len(name) > MAX_USERNAME_LENGTH:
        raise InvalidUsername(name)
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise InvalidUsername(name)
    return name


def validate_password(password: str) -> None:
    if len(unicodedata.normalize("NFC", password)) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort(str(MIN_PASSWORD_LENGTH))


def _valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    account_id = record.get("account_id")
    username = record.get("username")
    return (
        isinstance(account_id, str)
        and _ACCOUNT_ID.fullmatch(account_id) is not None
        and isinstance(username, str)
        and bool(username)
        and valid_kdf(record.get("kdf"))
        and isinstance(record.get("key"), dict)
    )


def _read_records() -> list[dict[str, Any]]:
    path = _accounts_path()
    try:
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AccountsUnavailable("unreadable") from error
    if not isinstance(payload, dict):
        raise AccountsUnavailable("unreadable")
    if not is_supported_version(payload.get(_VERSION_KEY), {ACCOUNTS_FORMAT_VERSION}):
        raise AccountsUnavailable("version")
    records = payload.get("accounts")
    if not isinstance(records, list) or not all(_valid_record(r) for r in records):
        raise AccountsUnavailable("unreadable")
    return list(records)


def _write_records(records: list[dict[str, Any]]) -> None:
    path = _accounts_path()
    ensure_private_directory(path.parent)
    temp_path = temporary_sibling(path)
    try:
        with temp_path.open("x", encoding="utf-8") as f:
            json.dump({_VERSION_KEY: ACCOUNTS_FORMAT_VERSION, "accounts": records}, f, indent=2)
        ensure_private_file(temp_path)
        commit_temporary(temp_path, path)
        ensure_private_file(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _key_context(account_id: str) -> str:
    return f"account-key/{account_id}"


def _find(records: list[dict[str, Any]], username: str) -> dict[str, Any] | None:
    wanted = _username_key(username)
    return next((r for r in records if _username_key(r["username"]) == wanted), None)


def _unlock(record: dict[str, Any], password: str) -> bytes:
    try:
        return open_sealed(
            record["key"], derive_key(password, record["kdf"]), _key_context(record["account_id"])
        )
    except EncryptedFileError as error:
        raise InvalidCredentials() from error


def create_account(username: str, password: str) -> EvaluatorAccount:
    name = validate_username(username)
    validate_password(password)
    records = _read_records()
    if _find(records, name) is not None:
        raise UsernameTaken(name)
    account_id = uuid4().hex
    key = new_key()
    kdf = new_kdf()
    record = {
        "account_id": account_id,
        "username": name,
        "created_at": utc_now_iso(),
        "kdf": kdf,
        "key": seal(key, derive_key(password, kdf), _key_context(account_id)),
    }
    _write_records([*records, record])
    return EvaluatorAccount(account_id=account_id, username=name, key=key)


def log_in(username: str, password: str) -> EvaluatorAccount:
    records = _read_records()
    record = _find(records, username)
    if record is None:
        # Spend the same time as a wrong password, so a login does not say
        # which IDs exist faster than reading the accounts file would.
        derive_key(password, new_kdf())
        raise InvalidCredentials()
    key = _unlock(record, password)
    return EvaluatorAccount(account_id=record["account_id"], username=record["username"], key=key)


def change_password(username: str, old_password: str, new_password: str) -> None:
    validate_password(new_password)
    records = _read_records()
    record = _find(records, username)
    if record is None:
        raise InvalidCredentials()
    key = _unlock(record, old_password)
    kdf = new_kdf()
    record["kdf"] = kdf
    record["key"] = seal(key, derive_key(new_password, kdf), _key_context(record["account_id"]))
    _write_records(records)


def delete_account(username: str, password: str) -> None:
    """Delete the account and every evaluation it kept on this computer.
    The evaluations go first: an account left without them is harmless,
    files left without their key could never be opened or told apart."""
    records = _read_records()
    record = _find(records, username)
    if record is None:
        raise InvalidCredentials()
    _unlock(record, password)
    folder = accounts_root() / record["account_id"]
    if folder.exists():
        shutil.rmtree(folder)
    _write_records([r for r in records if r is not record])
