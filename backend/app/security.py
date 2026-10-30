from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import Settings

PBKDF2_ROUNDS = 200_000
TEMPORARY_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*"
TEMPORARY_PASSWORD_LENGTH = 16


def generate_temporary_password(length: int = TEMPORARY_PASSWORD_LENGTH) -> str:
    """Generate a cryptographically secure temporary password.

    Guarantees upper-case, lower-case, digit, and symbol characters so the value
    satisfies the application password policy without being derived from any
    student data.
    """
    while True:
        password = "".join(secrets.choice(TEMPORARY_PASSWORD_ALPHABET) for _ in range(max(length, 12)))
        has_upper = any(char.isupper() for char in password)
        has_lower = any(char.islower() for char in password)
        has_digit = any(char.isdigit() for char in password)
        has_symbol = any(not char.isalnum() for char in password)
        if has_upper and has_lower and has_digit and has_symbol and validate_password(password) is None:
            return password


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ROUNDS).hex()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(rounds)).hex()
    return hmac.compare_digest(candidate, digest)


def validate_password(password: str) -> str | None:
    if len(password) < 10 or len(password) > 128:
        return "Use a password between 10 and 128 characters."
    if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        return "Use a password that includes a letter and a number."
    return None


def create_token(settings: Settings, user_id: str, role: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "ver": int(token_version or 0),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.token_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(settings: Settings, token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
