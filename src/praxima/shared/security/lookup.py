"""Keyed phone lookup digests: find a returning contact without storing the phone in clear.

The workspace id is part of the MAC input, so one phone gives unrelated digests in different
workspaces (tenants can't be correlated). The key lives in secret management only.
"""

import base64
import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass, field

E164 = re.compile(r"\+[1-9][0-9]{7,14}")


@dataclass(frozen=True)
class PhoneLookup:
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValueError("The lookup key must be 32 random bytes")

    @classmethod
    def from_environment(cls) -> "PhoneLookup":
        try:
            return cls(base64.b64decode(os.environ["PRAXIMA_LOOKUP_KEY"], validate=True))
        except (KeyError, ValueError):
            raise ValueError("A valid PRAXIMA_LOOKUP_KEY (base64, 32 bytes) is required") from None

    def digest(self, workspace_id: uuid.UUID, phone: str) -> str:
        if not E164.fullmatch(phone):
            raise ValueError("Phone numbers must be E.164")
        message = f"phone-v1:{workspace_id}:{phone}".encode()
        return hmac.new(self.key, message, hashlib.sha256).hexdigest()
