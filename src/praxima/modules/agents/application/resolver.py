"""Tenant resolution from trusted ingress destination, never caller speech/ID."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from praxima.shared.db.pool import RuntimeDatabase


class ClinicUnavailable(LookupError):
    """Unknown, inactive, unpublished or incorrectly routed clinic. Fail closed."""


@dataclass(frozen=True)
class InboundDestination:
    """Construct only in a trusted SIP adapter; not a model-visible tool argument."""

    called_number: str
    trunk_id: str
    provider: str = "plivo"

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"\+[1-9][0-9]{7,14}", self.called_number)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.trunk_id)
            or self.provider not in {"plivo", "test"}
        ):
            raise ClinicUnavailable("Invalid trusted destination.")


@dataclass(frozen=True)
class ClinicScope:
    clinic_id: UUID
    phone_number_id: UUID
    configuration_version_id: UUID
    timezone: str
    supported_languages: tuple[str, ...]


class ResolutionRepository(Protocol):
    async def resolve(self, destination: InboundDestination) -> Mapping[str, Any] | None: ...


class PostgresResolutionRepository:
    def __init__(self, database: RuntimeDatabase) -> None:
        self.database = database

    async def resolve(self, destination: InboundDestination) -> Mapping[str, Any] | None:
        async with self.database.connection() as conn:
            return await (
                await conn.execute(
                    "SELECT * FROM clinic_private.resolve_destination(%s, %s, %s)",
                    (destination.provider, destination.called_number, destination.trunk_id),
                )
            ).fetchone()


class ClinicResolver:
    def __init__(self, repository: ResolutionRepository) -> None:
        self.repository = repository

    async def resolve(self, destination: InboundDestination) -> ClinicScope:
        row = await self.repository.resolve(destination)
        if not row:
            raise ClinicUnavailable("No active published clinic for this destination.")
        ZoneInfo(row["timezone"])
        return ClinicScope(
            clinic_id=UUID(str(row["clinic_id"])),
            phone_number_id=UUID(str(row["phone_number_id"])),
            configuration_version_id=UUID(str(row["configuration_version_id"])),
            timezone=row["timezone"],
            supported_languages=tuple(row["supported_languages"]),
        )


class ConfigurationRepository:
    def __init__(self, database: RuntimeDatabase, scope: ClinicScope) -> None:
        self.database = database
        self.scope = scope

    async def load(self) -> dict[str, Any]:
        async with self.database.connection(self.scope.clinic_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT snapshot FROM public.configuration_versions "
                    "WHERE clinic_id = %s AND id = %s "
                    "AND status IN ('published', 'superseded')",
                    (self.scope.clinic_id, self.scope.configuration_version_id),
                )
            ).fetchone()
        if not row:
            raise ClinicUnavailable("Pinned configuration unavailable.")
        result: dict[str, Any] = row["snapshot"]
        return result
