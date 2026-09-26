"""Time-ordered UUIDv7 identifiers (RFC 9562) for index locality; no clock or host leakage."""

import os
import time
import uuid


def new_id() -> uuid.UUID:
    """Return a UUIDv7: 48-bit Unix milliseconds, version 7, RFC 4122 variant, 74 random bits."""
    value = (time.time_ns() // 1_000_000) << 80 | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version 7
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # variant 10xx
    return uuid.UUID(int=value)
