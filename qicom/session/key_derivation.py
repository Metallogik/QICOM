"""Turning something a person types - a password, an access code - into an encryption key.

One place for scrypt and its parameters, shared by evaluator accounts and
access-code-protected evaluation files, so the two cannot drift apart in
cost or in how they read parameters back from a file.

The cost is stored with every key, so changing SCRYPT_N later leaves
existing accounts and packages readable. Read back, it is bounded: an edited
file must not make QICOM allocate gigabytes before finding out the secret is
wrong.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import unicodedata
from typing import Any

from qicom.session.encrypted_files import KEY_BYTES

# About 0.2 s and 128 MiB per derivation on a current laptop: OWASP's
# minimum for scrypt. A short secret stays guessable by someone holding a
# copy of the file; this only makes every guess expensive.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
_SALT_BYTES = 16


def normalize_secret(secret: str) -> bytes:
    """The same text, typed on any keyboard or OS, gives the same bytes."""
    return unicodedata.normalize("NFC", secret).encode("utf-8")


def new_kdf() -> dict[str, Any]:
    return {
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(os.urandom(_SALT_BYTES)).decode("ascii"),
    }


def _is_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def valid_kdf(kdf: object) -> bool:
    if not isinstance(kdf, dict) or kdf.get("algorithm") != "scrypt":
        return False
    n, r, p = kdf.get("n"), kdf.get("r"), kdf.get("p")
    if not (_is_int(n, 2, 2**20) and _is_int(r, 1, 16) and _is_int(p, 1, 4)):
        return False
    assert isinstance(n, int)
    if n & (n - 1):
        return False
    try:
        return len(base64.b64decode(str(kdf.get("salt")), validate=True)) >= _SALT_BYTES
    except (binascii.Error, ValueError):
        return False


def derive_key(secret: str, kdf: dict[str, Any]) -> bytes:
    """`kdf` must have passed valid_kdf."""
    n, r, p = kdf["n"], kdf["r"], kdf["p"]
    return hashlib.scrypt(
        normalize_secret(secret),
        salt=base64.b64decode(kdf["salt"], validate=True),
        n=n,
        r=r,
        p=p,
        maxmem=128 * n * r * (p + 1) + 2**20,
        dklen=KEY_BYTES,
    )
