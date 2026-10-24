"""Symmetric encryption of secrets stored in the database."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key_material: str):
        digest = hashlib.sha256(key_material.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @classmethod
    def from_settings(cls, secret_key: str | None, data_dir: Path) -> "SecretBox":
        """Use MP_SECRET_KEY, otherwise generate a persistent key in data_dir."""
        if secret_key:
            return cls(secret_key)
        key_file = data_dir / "secret.key"
        if not key_file.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            key_file.write_text(secrets.token_urlsafe(48))
            key_file.chmod(0o600)
        return cls(key_file.read_text().strip())

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        if token is None:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:  # key changed or corrupted data
            raise ValueError("Cannot decrypt a stored secret: did MP_SECRET_KEY change?") from exc

    def encrypt_json(self, value: Any) -> str:
        return self.encrypt(json.dumps(value))  # type: ignore[return-value]

    def decrypt_json(self, token: str | None, default: Any = None) -> Any:
        if not token:
            return default
        return json.loads(self.decrypt(token))  # type: ignore[arg-type]
