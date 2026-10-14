import hashlib
import hmac
import os
import re
from typing import Tuple

import bcrypt


PREFERRED_HASH_PREFIX = "$lawneeds-bcrypt-sha256$v1$"
LEGACY_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _bcrypt_rounds() -> int:
    configured_rounds = int(os.getenv("BCRYPT_ROUNDS", "12"))
    if configured_rounds < 4 or configured_rounds > 31:
        raise ValueError("BCRYPT_ROUNDS must be between 4 and 31")
    return configured_rounds


def _bcrypt_input(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(
        _bcrypt_input(password),
        bcrypt.gensalt(rounds=_bcrypt_rounds()),
    ).decode("utf-8")
    return f"{PREFERRED_HASH_PREFIX}{hashed}"


def verify_password(password: str, stored_hash: str) -> Tuple[bool, bool]:
    """
    Returns (is_valid, needs_rehash).

    Legacy SHA-256 hashes are accepted so existing users can still sign in. When
    one verifies successfully, needs_rehash tells the caller to replace it with
    the preferred bcrypt-based hash.
    """
    if not stored_hash:
        return False, False

    if stored_hash.startswith(PREFERRED_HASH_PREFIX):
        bcrypt_hash = stored_hash[len(PREFERRED_HASH_PREFIX):].encode("utf-8")
        verified = _verify_bcrypt(password, bcrypt_hash)
        return verified, verified and _bcrypt_hash_needs_rehash(bcrypt_hash)

    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        bcrypt_hash = stored_hash.encode("utf-8")
        verified = _verify_raw_bcrypt(password, bcrypt_hash)
        return verified, verified

    if LEGACY_SHA256_RE.fullmatch(stored_hash):
        legacy_hash = hashlib.sha256(password.encode()).hexdigest()
        verified = hmac.compare_digest(legacy_hash, stored_hash)
        return verified, verified

    return False, False


def _verify_bcrypt(password: str, bcrypt_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(_bcrypt_input(password), bcrypt_hash)
    except ValueError:
        return False


def _verify_raw_bcrypt(password: str, bcrypt_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), bcrypt_hash)
    except ValueError:
        return False


def _bcrypt_hash_needs_rehash(bcrypt_hash: bytes) -> bool:
    parts = bcrypt_hash.decode("utf-8").split("$")
    if len(parts) < 3:
        return True

    try:
        rounds = int(parts[2])
    except ValueError:
        return True

    return rounds != _bcrypt_rounds()
