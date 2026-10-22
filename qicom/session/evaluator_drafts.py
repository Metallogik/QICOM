"""One evaluator account's evaluations on this computer, encrypted: unfinished and finished.

The unfinished ones are there to be resumed, the finished ones until the
evaluator removes them.

Each evaluation is one file in the account's folder, encrypted with the
account's key (see evaluator_accounts, encrypted_files). It holds the answers
- the answers-file content an evaluator sends back, unchanged - and what the
list shows about the evaluation: where its package is, how far it got.

Encrypted at every autosave, not only when the evaluator suspends: otherwise
the answers would sit in clear for the whole session, and after any crash or
power cut. No separate index: an index can disagree with the files it
describes, and listing decrypts only a handful of small files. File names are
a hash of the package's identity, so the folder does not say which studies
are in it.

Taking an evaluation without logging in keeps nothing here - no draft, no
resume (see QualitativeController).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import platformdirs

from qicom.schema_version import is_supported_version
from qicom.session.encrypted_files import (
    EncryptedFileError,
    read_encrypted_json,
    write_encrypted_json,
)
from qicom.session.evaluation_package import set_aside_resume_file
from qicom.session.evaluator_accounts import EvaluatorAccount
from qicom.session.protected_files import AccessKey

DRAFT_FORMAT_VERSION = 1
_VERSION_KEY = "qicom_draft_version"
_SUFFIX = ".qdraft"
_DRAFT_FILE = re.compile(r"[0-9a-f]{32}\.qdraft")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftSummary:
    """What the evaluator's list shows about one evaluation."""

    draft_file: str
    package_path: Path
    name: str
    answered: int
    page_position: int
    page_count: int
    page_id: int | None
    """The page to reopen on, by id: the presentation order may differ
    from the package's own."""
    saved_at: str
    completed_at: str | None = None
    """When its report was saved; None while it is unfinished."""
    readable: bool = True
    """False for a file this account's key cannot open (damaged or
    altered): listed so it can be removed, never resumed."""

    @property
    def completed(self) -> bool:
        return self.completed_at is not None

    @property
    def package_available(self) -> bool:
        try:
            return self.package_path.is_file()
        except OSError:
            return False


def draft_file_name(test_id: str, delivery_id: str | None) -> str:
    digest = hashlib.sha256(f"{test_id}\0{delivery_id or ''}".encode()).hexdigest()
    return digest[:32] + _SUFFIX


def is_draft_file_name(name: object) -> bool:
    return isinstance(name, str) and _DRAFT_FILE.fullmatch(name) is not None


def _summary_payload(summary: DraftSummary) -> dict[str, Any]:
    return {
        "package_path": str(summary.package_path),
        "name": summary.name,
        "answered": summary.answered,
        "page_position": summary.page_position,
        "page_count": summary.page_count,
        "page_id": summary.page_id,
        "saved_at": summary.saved_at,
        "completed_at": summary.completed_at,
    }


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EncryptedFileError("Damaged draft summary.")
    return value


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise EncryptedFileError("Damaged draft summary.")
    return value


def _parse_summary(draft_file: str, data: object) -> DraftSummary:
    if not isinstance(data, dict):
        raise EncryptedFileError("Damaged draft summary.")
    package_path = data.get("package_path")
    name = data.get("name")
    saved_at = data.get("saved_at")
    if not isinstance(package_path, str) or not isinstance(name, str):
        raise EncryptedFileError("Damaged draft summary.")
    if not isinstance(saved_at, str):
        raise EncryptedFileError("Damaged draft summary.")
    page_id = data.get("page_id")
    return DraftSummary(
        draft_file=draft_file,
        package_path=Path(package_path),
        name=name,
        answered=_count(data.get("answered")),
        page_position=_count(data.get("page_position")),
        page_count=_count(data.get("page_count")),
        page_id=None if page_id is None else _count(page_id),
        saved_at=saved_at,
        completed_at=_optional_text(data.get("completed_at")),
    )


class DraftStore:
    """The encrypted evaluations of one logged-in account."""

    def __init__(self, account: EvaluatorAccount) -> None:
        self._account = account

    @property
    def account(self) -> EvaluatorAccount:
        return self._account

    def _path(self, draft_file: str) -> Path:
        if not is_draft_file_name(draft_file):
            raise ValueError("Not a draft file name.")
        return self._account.data_dir / draft_file

    def _context(self, draft_file: str) -> str:
        return f"draft/{self._account.account_id}/{draft_file}"

    def save(
        self,
        summary: DraftSummary,
        answers: dict[str, Any],
        package_key: AccessKey | None = None,
    ) -> None:
        """`package_key` opens a package protected by an access code; kept
        here, encrypted with the rest, so resuming does not ask for the code
        again."""
        payload: dict[str, Any] = {
            _VERSION_KEY: DRAFT_FORMAT_VERSION,
            "summary": _summary_payload(summary),
            "answers": answers,
        }
        if package_key is not None:
            payload["package_key"] = package_key.to_payload()
        path = self._path(summary.draft_file)
        write_encrypted_json(path, payload, self._account.key, self._context(summary.draft_file))

    def load(self, draft_file: str) -> tuple[DraftSummary, dict[str, Any]] | None:
        """None when there is no such draft; EncryptedFileError when there is
        one this account cannot read."""
        path = self._path(draft_file)
        if not path.is_file():
            return None
        payload = self._payload(draft_file, path)
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise EncryptedFileError("Damaged draft.")
        return _parse_summary(draft_file, payload.get("summary")), answers

    def _payload(self, draft_file: str, path: Path) -> dict[str, Any]:
        payload = read_encrypted_json(path, self._account.key, self._context(draft_file))
        if not is_supported_version(payload.get(_VERSION_KEY), {DRAFT_FORMAT_VERSION}):
            raise EncryptedFileError("Unsupported draft version.")
        return payload

    def package_key(self, draft_file: str) -> AccessKey | None:
        """The access key this evaluation's package was opened with, if it
        is protected and the draft can be read."""
        path = self._path(draft_file)
        try:
            payload = self._payload(draft_file, path) if path.is_file() else {}
        except (OSError, ValueError):
            return None
        return AccessKey.from_payload(payload.get("package_key"))

    def summaries(self) -> list[DraftSummary]:
        """Every evaluation of this account, most recently saved first."""
        folder = self._account.data_dir
        try:
            names = sorted(p.name for p in folder.iterdir() if is_draft_file_name(p.name))
        except OSError:
            return []
        result: list[DraftSummary] = []
        for name in names:
            try:
                loaded = self.load(name)
            except (OSError, ValueError):
                logger.warning("An evaluation of this account could not be read")
                loaded = None
                result.append(_unreadable(name))
            if loaded is not None:
                result.append(loaded[0])
        return sorted(result, key=lambda summary: summary.saved_at, reverse=True)

    def mark_completed(self, draft_file: str, completed_at: str) -> None:
        loaded = self.load(draft_file)
        if loaded is None:
            return
        summary, answers = loaded
        self.save(
            replace(summary, completed_at=completed_at), answers, self.package_key(draft_file)
        )

    def set_aside(self, draft_file: str) -> Path | None:
        """Move an unreadable draft out of the way of the next autosave,
        without deleting what may still be someone's answers."""
        return set_aside_resume_file(self._path(draft_file), "unreadable")

    def delete(self, draft_file: str) -> None:
        """Remove one evaluation's answers from this computer."""
        self._path(draft_file).unlink(missing_ok=True)


def _unreadable(draft_file: str) -> DraftSummary:
    return DraftSummary(
        draft_file=draft_file,
        package_path=Path(),
        name=draft_file,
        answered=0,
        page_position=0,
        page_count=0,
        page_id=None,
        saved_at="",
        readable=False,
    )


_LEGACY_INDEX_FILE_NAME = "pending_evaluations.json"


def discard_legacy_pending_index() -> None:
    """Delete the plaintext list of unfinished evaluations an earlier
    development build kept for the whole computer: it names packages and
    where they are, which is exactly what accounts now keep encrypted. The
    plaintext answers it pointed at stay, until their owner claims them by
    opening the package again (see QualitativeController)."""
    path = Path(platformdirs.user_data_dir("qicom", appauthor=False)) / _LEGACY_INDEX_FILE_NAME
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("The old list of unfinished evaluations could not be deleted")
