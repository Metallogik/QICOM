"""Tests for qicom.session.evaluator_accounts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qicom.session import evaluator_accounts as accounts_module
from qicom.session import key_derivation
from qicom.session.encrypted_files import write_encrypted_json
from qicom.session.evaluator_accounts import (
    ACCOUNTS_FORMAT_VERSION,
    MIN_PASSWORD_LENGTH,
    AccountsUnavailable,
    InvalidCredentials,
    InvalidUsername,
    PasswordTooShort,
    UsernameTaken,
    change_password,
    create_account,
    delete_account,
    log_in,
)


def _accounts_file() -> Path:
    return accounts_module._accounts_path()


def test_the_production_cost_is_owasps_minimum_for_scrypt(_cheap_password_hashing: int) -> None:
    """tests/conftest.py lowers the cost for speed; the shipped value is this."""
    assert (_cheap_password_hashing, key_derivation.SCRYPT_R, key_derivation.SCRYPT_P) == (
        2**17,
        8,
        1,
    )


def test_create_then_log_in_gives_back_the_same_key() -> None:
    created = create_account("  Mario  ", "secret")

    logged = log_in("mario", "secret")

    assert created.username == "Mario"
    assert logged == created
    assert len(logged.key) == 32


def test_the_password_and_the_key_are_never_stored_in_clear() -> None:
    account = create_account("mario", "a-password")
    text = _accounts_file().read_text(encoding="utf-8")

    assert "a-password" not in text
    assert account.key.hex() not in text
    payload = json.loads(text)
    assert payload["qicom_evaluator_accounts_version"] == ACCOUNTS_FORMAT_VERSION
    assert payload["accounts"][0]["kdf"]["algorithm"] == "scrypt"


def test_a_wrong_password_and_an_unknown_id_fail_the_same_way() -> None:
    create_account("mario", "secret")

    with pytest.raises(InvalidCredentials):
        log_in("mario", "wrong")
    with pytest.raises(InvalidCredentials):
        log_in("nobody", "secret")


def test_ids_are_unique_ignoring_case() -> None:
    create_account("Mario", "secret")

    with pytest.raises(UsernameTaken):
        create_account("MARIO", "other1")


@pytest.mark.parametrize("username", ["", "   ", "x" * 65, "tab\there"])
def test_invalid_ids_are_refused(username: str) -> None:
    with pytest.raises(InvalidUsername):
        create_account(username, "secret")


def test_five_characters_are_enough_and_four_are_not() -> None:
    assert MIN_PASSWORD_LENGTH == 5
    with pytest.raises(PasswordTooShort):
        create_account("mario", "1234")
    create_account("mario", "12345")


def test_changing_the_password_keeps_the_key() -> None:
    account = create_account("mario", "secret")

    change_password("mario", "secret", "new-secret")

    with pytest.raises(InvalidCredentials):
        log_in("mario", "secret")
    assert log_in("mario", "new-secret").key == account.key


def test_changing_needs_the_current_password_and_a_long_enough_new_one() -> None:
    create_account("mario", "secret")

    with pytest.raises(InvalidCredentials):
        change_password("mario", "wrong", "new-secret")
    with pytest.raises(PasswordTooShort):
        change_password("mario", "secret", "abc")
    assert log_in("mario", "secret")


def test_deleting_removes_the_account_and_its_evaluations_only() -> None:
    mario = create_account("mario", "secret")
    luigi = create_account("luigi", "secret")
    for account in (mario, luigi):
        write_encrypted_json(account.data_dir / "x.qdraft", {}, account.key, "c")

    with pytest.raises(InvalidCredentials):
        delete_account("mario", "wrong")
    delete_account("mario", "secret")

    assert not mario.data_dir.exists()
    assert (luigi.data_dir / "x.qdraft").exists()
    with pytest.raises(InvalidCredentials):
        log_in("mario", "secret")
    assert log_in("luigi", "secret") == luigi


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"qicom_evaluator_accounts_version": 2, "accounts": []}),
        json.dumps({"qicom_evaluator_accounts_version": 1, "accounts": [{"account_id": "x"}]}),
    ],
)
def test_a_damaged_or_newer_file_is_refused_and_never_overwritten(content: str) -> None:
    _accounts_file().parent.mkdir(parents=True, exist_ok=True)
    _accounts_file().write_text(content, encoding="utf-8")

    with pytest.raises(AccountsUnavailable):
        log_in("mario", "secret")
    with pytest.raises(AccountsUnavailable):
        create_account("mario", "secret")
    assert _accounts_file().read_text(encoding="utf-8") == content


def test_an_edited_cost_cannot_make_a_login_allocate_gigabytes() -> None:
    create_account("mario", "secret")
    payload = json.loads(_accounts_file().read_text(encoding="utf-8"))
    payload["accounts"][0]["kdf"]["n"] = 2**30
    _accounts_file().write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AccountsUnavailable):
        log_in("mario", "secret")
