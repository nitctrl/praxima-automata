"""Seal and open personal data, and compute phone lookup digests (one object to inject)."""

import uuid
from dataclasses import dataclass

from praxima.shared.security.lookup import PhoneLookup
from praxima.shared.security.privacy import PiiCipher


@dataclass(frozen=True)
class Vault:
    cipher: PiiCipher
    lookup: PhoneLookup

    @classmethod
    def from_environment(cls) -> "Vault":
        """Raises ValueError when keys are missing: personal data is never stored in clear."""
        return cls(PiiCipher.from_environment(), PhoneLookup.from_environment())

    @property
    def version(self) -> str:
        return self.cipher.current

    def seal(
        self, workspace_id: uuid.UUID, record_id: uuid.UUID, field: str, value: str | None
    ) -> bytes | None:
        """Encrypt bound to workspace, record and field (copied ciphertext won't decrypt)."""
        return None if value is None else self.cipher.encrypt(value, workspace_id, record_id, field)

    def open(
        self,
        workspace_id: uuid.UUID,
        record_id: uuid.UUID,
        field: str,
        value: bytes | None,
        version: str | None,
    ) -> str | None:
        if value is None or version is None:
            return None
        return self.cipher.decrypt(value, version, workspace_id, record_id, field)

    def phone_digest(self, workspace_id: uuid.UUID, phone: str) -> str:
        return self.lookup.digest(workspace_id, phone)
