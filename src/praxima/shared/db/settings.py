"""Explicit database configuration. Never log connection strings or parser errors."""

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit


class ConfigurationError(ValueError):
    """A caller-safe setup error that does not include configuration values."""


@dataclass(frozen=True)
class DatabaseSettings:
    dsn: str = field(repr=False)
    project_ref: str

    @classmethod
    def validate(cls, dsn: str, project_ref: str) -> "DatabaseSettings":
        try:
            uri = urlsplit(dsn)
            if not re.fullmatch(r"[a-z0-9]{20}", project_ref):
                raise ValueError
            direct = uri.hostname == f"db.{project_ref}.supabase.co"
            pooler = bool(
                uri.hostname
                and re.fullmatch(r"[a-z0-9.-]+\.pooler\.supabase\.com", uri.hostname)
                and unquote(uri.username or "").endswith(f".{project_ref}")
            )
            if (
                uri.scheme not in {"postgres", "postgresql"}
                or not (direct or pooler)
                or uri.path != "/postgres"
                or uri.port not in {None, 5432}
                or not uri.password
                or not uri.username
                or uri.netloc.count("@") != 1
                or any(c in dsn for c in "[]<> \n\r\t")
                or uri.fragment
                or parse_qs(uri.query, keep_blank_values=True) != {"sslmode": ["require"]}
                or re.search(r"%(?![0-9a-fA-F]{2})", dsn)
            ):
                raise ValueError
        except ValueError:
            raise ConfigurationError(
                "Use this project's direct/session-pooler PostgreSQL URI on port 5432 "
                "with database postgres, an encoded password and sslmode=require."
            ) from None
        return cls(dsn=dsn, project_ref=project_ref)
