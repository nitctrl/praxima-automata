"""Outbound WhatsApp through an open-wa (wa-automate) EASY API session.

open-wa is an unofficial bridge, so delivery is best effort and the durable
`whatsapp_messages` outbox stays authoritative. Replacing `OpenWa.send_text`
with the Meta Cloud API later does not change any caller of `deliver`.
Recipient numbers are stored encrypted and decrypted only here, in process.
"""

import base64
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from clinic.db import RuntimeDatabase
from clinic.privacy import PiiCipher

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class OpenWa:
    url: str
    key: str = field(repr=False, default="")
    qr_path: str = "/qr"

    @classmethod
    def from_environment(cls) -> "OpenWa | None":
        """Return a configured sender, or None when WhatsApp is not wired up yet."""
        url = os.environ.get("OPENWA_API_URL", "").rstrip("/")
        parts = urlsplit(url)
        loopback = parts.hostname in {"localhost", "127.0.0.1"}
        if parts.scheme == "https" or (parts.scheme == "http" and loopback):
            qr = os.environ.get("OPENWA_QR_PATH", "/qr")
            key = os.environ.get("OPENWA_API_KEY", "")
            return cls(url, key, qr if qr.startswith("/") else "/qr")
        if url:
            logger.warning("Ignoring open-wa endpoint: HTTPS is required off loopback")
        return None

    def _headers(self) -> dict[str, str]:
        return {"api_key": self.key} if self.key else {}

    async def send_text(self, client: httpx.AsyncClient, number: str, body: str) -> None:
        result = await client.post(
            f"{self.url}/sendText",
            headers=self._headers(),
            json={"args": {"to": number.lstrip("+") + "@c.us", "content": body}},
        )
        result.raise_for_status()

    async def link_qr(self, client: httpx.AsyncClient) -> tuple[bytes, str]:
        """Fetch the pairing QR so an administrator can link the tenant's handset."""
        result = await client.get(self.url + self.qr_path, headers=self._headers())
        result.raise_for_status()
        media = result.headers.get("content-type", "")
        if not media.startswith("image/") or len(result.content) > 1_000_000:
            raise ValueError("open-wa did not return a QR image")
        return result.content, media


async def deliver(
    database: RuntimeDatabase, clinic: UUID, cipher: PiiCipher, limit: int = 10
) -> int:
    """Send this clinic's queued notifications. Never raises into a live call."""
    sender = OpenWa.from_environment()
    async with database.connection(clinic) as conn:
        row = await (
            await conn.execute("SELECT clinic_private.whatsapp_outbox(%s) AS queued", (limit,))
        ).fetchone()
    queued = list(row["queued"]) if row else []
    delivered = 0
    if not queued:
        return delivered
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for item in queued:
            reason = ""
            try:
                if sender is None:
                    raise RuntimeError("whatsapp_not_configured")
                number = cipher.decrypt(
                    base64.b64decode(item["recipient"], validate=True),
                    item["key_version"],
                    clinic,
                    UUID(item["booking_id"]),
                    "whatsapp_recipient",
                )
                await sender.send_text(client, number, item["body"])
                delivered += 1
            except Exception as exc:
                # Never log the recipient, the body or the exception text.
                reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                logger.warning("WhatsApp delivery failed (%s)", reason)
            try:
                async with database.connection(clinic) as conn:
                    await conn.execute(
                        "SELECT clinic_private.whatsapp_mark(%s,%s,%s)",
                        (item["id"], not reason, reason),
                    )
            except Exception:
                logger.warning("WhatsApp delivery state could not be recorded")
    return delivered
