from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from core.settings import BASE_DIR


load_dotenv(BASE_DIR / ".env")


def _fernet() -> Fernet:
    configured = os.getenv("CONFIG_MASTER_KEY", "").strip()
    if not configured:
        raise RuntimeError(
            f"CONFIG_MASTER_KEY is missing from {BASE_DIR / '.env'}"
        )
    digest = hashlib.sha256(configured.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        raw = _fernet().decrypt(value.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Encrypted configuration cannot be decrypted") from exc
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"
