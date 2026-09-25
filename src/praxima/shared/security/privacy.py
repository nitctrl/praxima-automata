"""Authenticated PII encryption bound to tenant, resource and field.

Keys belong in secret management, never in the database or model context.
Retain old key versions until protected records are re-encrypted/erased.
"""

import base64
import json
import os
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class PiiCipher:
    keys: dict[str, bytes] = field(repr=False)
    current: str

    @classmethod
    def from_environment(cls) -> "PiiCipher":
        try:
            values = json.loads(os.environ["CLINIC_PII_KEYS"])
            if not isinstance(values, dict) or not values:
                raise ValueError
            if any(
                not isinstance(name, str) or not isinstance(value, str) or not 1 <= len(name) <= 80
                for name, value in values.items()
            ):
                raise ValueError
            keys = {name: base64.b64decode(value, validate=True) for name, value in values.items()}
            current = os.environ["CLINIC_PII_KEY_VERSION"]
            if current not in keys or any(len(key) != 32 for key in keys.values()):
                raise ValueError
            return cls(keys, current)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Valid versioned clinic encryption keys are required") from None

    @staticmethod
    def _aad(clinic: UUID, resource: UUID, name: str) -> bytes:
        return f"clinic-pii-v1:{clinic}:{resource}:{name}".encode()

    def encrypt(self, text: str, clinic: UUID, resource: UUID, name: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self.keys[self.current]).encrypt(
            nonce, text.encode(), self._aad(clinic, resource, name)
        )

    def decrypt(self, value: bytes, version: str, clinic: UUID, resource: UUID, name: str) -> str:
        return (
            AESGCM(self.keys[version])
            .decrypt(value[:12], value[12:], self._aad(clinic, resource, name))
            .decode()
        )
