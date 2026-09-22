"""Small server-rendered dashboard with Supabase-verified bearer sessions.

The server never uses migration/service-role credentials. Supabase Auth verifies
the user on every protected request; PostgREST additionally validates the JWT and
applies RLS. Tokens stay in bounded server memory, never localStorage/HTML/cookies.
Use one worker behind HTTPS; restart logs everyone out. Distributed sessions and
shared edge rate limits are a production gate, not an implicit in-memory promise.
"""

import base64
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from clinic.answers import GroundedAnswerer
from clinic.documents import (
    CATEGORIES,
    MAX_UPLOAD_BYTES,
    DocumentRejected,
    DraftSection,
    extract,
    tag_doctors,
)
from clinic.knowledge import Query, StructuredKnowledge
from clinic.privacy import PiiCipher
from clinic.rag import HybridRetriever, snapshot_sections
from clinic.snapshot import Snapshot, normalize
from clinic.vectors import VectorSearch

ASSETS = Path(__file__).parent / "web"
logger = logging.getLogger(__name__)
EDIT_FIELDS: dict[str, str] = {
    "doctors": "display_name,speciality,aliases,languages,short_public_bio,accepts_new_patients,"
    "effective_from,effective_until,status",
    "services": "name,aliases,short_approved_description,appointment_required,active,"
    "effective_from,effective_until",
    "locations": "name,address,landmark,directions,map_url,parking_information,status,"
    "effective_from,effective_until",
    "doctor_services": "doctor_id,service_id,current_fee,currency,"
    "effective_from,effective_until,status",
    "weekly_schedules": "doctor_id,location_id,day_of_week,start_time,end_time,availability_type,"
    "status,effective_from,effective_until",
    "special_date_schedules": "doctor_id,location_id,schedule_date,"
    "start_time,end_time,publication_status",
    "schedule_exceptions": "doctor_id,location_id,exception_date,status,start_time,end_time,"
    "public_message,internal_note,publication_status",
    "temporary_notices": "location_id,doctor_id,service_id,notice_type,"
    "public_message,internal_note,"
    "starts_at,expires_at,priority,publication_status",
    "approved_faqs": "category,canonical_question,alternative_phrasings,approved_answer,"
    "effective_from,effective_until,publication_status",
}
READ_FIELDS = {table: "id," + fields for table, fields in EDIT_FIELDS.items()} | {
    "appointment_requests": "id,call_session_id,preferred_date,status,"
    "doctor_id,service_id,created_at",
    "callback_requests": "id,call_session_id,requested_time,reason_category,status,created_at",
    "call_sessions": "id,started_at,ended_at,duration_seconds,disposition,failure_code,safety_flag,"
    "short_administrative_summary,configuration_version_id,is_test",
    "configuration_versions": "id,version_number,status,schema_version,"
    "published_at,source_version_id",
    "clinic_users": "id,auth_user_id,role,status",
    "usage_records": "id,call_session_id,llm_input_tokens,llm_output_tokens,"
    "stt_seconds,tts_characters,"
    "rate_version,recorded_at",
    "audit_logs": "id,action,resource_type,resource_id,actor_id,occurred_at",
}
DOCUMENT_FIELDS = (
    "id,title,original_filename,document_category,doctor_id,status,version,checksum,"
    "created_at,updated_at,published_at,extraction_warnings"
)
MANAGERS = {"owner", "manager"}
STAFF = MANAGERS | {"receptionist"}


@dataclass(frozen=True)
class WebSettings:
    supabase_url: str
    publishable_key: str = field(repr=False)
    origin: str

    def __post_init__(self) -> None:
        sb, site = urlsplit(self.supabase_url), urlsplit(self.origin)
        if (
            sb.scheme != "https"
            or not sb.hostname
            or not sb.hostname.endswith(".supabase.co")
            or sb.path not in {"", "/"}
            or sb.query
            or sb.fragment
            or sb.username
        ):
            raise ValueError("Use the dedicated Supabase HTTPS origin")
        local = site.hostname in {"localhost", "127.0.0.1"}
        if (site.scheme != "https" and not (local and site.scheme == "http")) or (
            site.path not in {"", "/"}
            or site.query
            or site.fragment
            or site.username
            or not site.hostname
        ):
            raise ValueError("Dashboard requires HTTPS, except loopback development")
        if not self.publishable_key.startswith("sb_publishable_"):
            raise ValueError("Use a Supabase publishable key, never a privileged key")

    @classmethod
    def from_environment(cls) -> "WebSettings":
        return cls(
            os.environ["SUPABASE_URL"].rstrip("/"),
            os.environ["SUPABASE_PUBLISHABLE_KEY"],
            os.environ.get("CLINIC_DASHBOARD_ORIGIN", "http://127.0.0.1:8080").rstrip("/"),
        )


@dataclass
class WebSession:
    token: str = field(repr=False)
    user_id: UUID
    csrf: str = field(repr=False)
    expires: float


class SupabaseGateway:
    def __init__(self, settings: WebSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.supabase_url,
            headers={"apikey": settings.publishable_key},
            timeout=10,
            transport=transport,
            follow_redirects=False,
        )

    async def call(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if method in {"POST", "PATCH"} and path.startswith("/rest/v1/"):
            headers["Prefer"] = "return=representation"
        try:
            result = await self.client.request(
                method, path, headers=headers, json=body, params=params
            )
            if result.status_code >= 500:
                logger.warning("Dashboard upstream unavailable: HTTP %s", result.status_code)
                raise HTTPException(503, "Clinic service is temporarily unavailable. Try again.")
            if result.status_code >= 400:
                status = result.status_code if result.status_code in {401, 403, 409, 429} else 400
                raise HTTPException(status, "Operation denied or invalid; no changes confirmed.")
            return result.json() if result.content else None
        except (httpx.HTTPError, ValueError) as exc:
            # Never log exception text, URLs, headers, bodies, tokens or user input.
            logger.warning("Dashboard upstream request failed: %s", type(exc).__name__)
            raise HTTPException(503, "Clinic service is temporarily unavailable.") from None

    async def user(self, token: str) -> UUID:
        value = await self.call("GET", "/auth/v1/user", token=token)
        try:
            # This is a remotely verified Auth response, not a decoded unverified JWT.
            return UUID(value["id"])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, "Sign in again.") from None


def create_app(
    settings: WebSettings | None = None,
    *,
    gateway: SupabaseGateway | None = None,
    cipher: PiiCipher | None = None,
    answerer: GroundedAnswerer | None = None,
) -> FastAPI:
    config = settings or WebSettings.from_environment()
    backend = gateway or SupabaseGateway(config)
    sessions: dict[str, WebSession] = {}
    buckets: dict[str, tuple[float, int]] = {}
    previews: dict[tuple[str, UUID], dict[str, Any]] = {}
    vectors = VectorSearch.from_environment()
    templates = Environment(loader=FileSystemLoader(ASSETS), autoescape=select_autoescape())

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        sessions.clear()
        await backend.client.aclose()

    app = FastAPI(
        title="Clinic reception", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    hostname = urlsplit(config.origin).hostname
    assert hostname is not None  # WebSettings rejects an origin without a hostname.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[hostname])
    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    def rate(key: str, maximum: int) -> None:
        now = time.monotonic()
        if len(buckets) > 5000:
            for k, (start, _) in list(buckets.items()):
                if now - start >= 60:
                    del buckets[k]
            if len(buckets) > 5000:
                raise HTTPException(429, "Try again later.")
        start, count = buckets.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if count >= maximum:
            raise HTTPException(429, "Too many requests. Try again shortly.")
        buckets[key] = start, count + 1

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Any:
        try:
            peer = request.client.host if request.client else "unknown"
            rate(peer, 180)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("origin") != config.origin:
                    raise HTTPException(403, "Origin verification failed.")
                # Document review is the only non-JSON, larger-than-form request surface.
                upload = request.url.path.endswith("/documents/upload")
                expected = "application/octet-stream" if upload else "application/json"
                if expected not in request.headers.get("content-type", ""):
                    raise HTTPException(415, "Unsupported content type.")
                maximum = MAX_UPLOAD_BYTES if upload else 16384
                if not upload and "/documents/" in request.url.path:
                    maximum = 262144
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > maximum:
                        raise HTTPException(413, "Request too large.")
                request._body = bytes(body)
            result = await call_next(request)
        except HTTPException as exc:
            result = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        result.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
                "Permissions-Policy": "microphone=(), camera=(), geolocation=()",
            }
        )
        if config.origin.startswith("https:"):
            result.headers["Strict-Transport-Security"] = "max-age=31536000"
        return result

    async def data(request: Request) -> dict[str, Any]:
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid JSON object.") from None

    async def identity(request: Request) -> WebSession:
        sid = request.cookies.get("clinic_session", "")
        session = sessions.get(sid)
        if session is None or session.expires <= time.monotonic():
            sessions.pop(sid, None)
            for key in list(previews):
                if key[0] == sid:
                    del previews[key]
            raise HTTPException(401, "Sign in to continue.")
        if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf
        ):
            raise HTTPException(403, "CSRF verification failed.")
        if await backend.user(session.token) != session.user_id:
            sessions.pop(sid, None)
            raise HTTPException(401, "Sign in again.")
        return session

    async def authorize(
        request: Request, clinic: UUID, roles: set[str] | None = None
    ) -> WebSession:
        session = await identity(request)
        rows = await backend.call(
            "GET",
            "/rest/v1/clinic_users",
            token=session.token,
            params={
                "select": "role",
                "clinic_id": f"eq.{clinic}",
                "auth_user_id": f"eq.{session.user_id}",
                "status": "eq.active",
                "limit": "1",
            },
        )
        if not rows or (roles is not None and rows[0]["role"] not in roles):
            raise HTTPException(403, "Clinic access forbidden.")
        return session

    async def rpc(session: WebSession, name: str, values: dict[str, Any]) -> Any:
        return await backend.call("POST", f"/rest/v1/rpc/{name}", token=session.token, body=values)

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return templates.get_template("index.html").render()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "clinic_voice": "not_activated"}

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        rate("login:" + (request.client.host if request.client else "unknown"), 10)
        values = await data(request)
        email, password = values.get("email"), values.get("password")
        if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254:
            raise HTTPException(400, "Email and password required.")
        result = await backend.call(
            "POST",
            "/auth/v1/token?grant_type=password",
            body={"email": email, "password": password},
        )
        token = result["access_token"]
        user = await backend.user(token)
        now = time.monotonic()
        for key, value in list(sessions.items()):
            if value.expires <= now:
                del sessions[key]
                for pkey in list(previews):
                    if pkey[0] == key:
                        del previews[pkey]
        if len(sessions) >= 1000:
            raise HTTPException(503, "Session capacity reached.")
        old = request.cookies.get("clinic_session", "")
        sessions.pop(old, None)
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = min(int(result.get("expires_in", 900)), 900)
        sessions[sid] = WebSession(token, user, csrf, now + ttl)
        reply = JSONResponse({"csrf": csrf})
        reply.set_cookie(
            "clinic_session",
            sid,
            httponly=True,
            secure=config.origin.startswith("https:"),
            samesite="strict",
            max_age=ttl,
            path="/",
        )
        return reply

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        session = await identity(request)
        rows = await backend.call(
            "GET",
            "/rest/v1/clinic_users",
            token=session.token,
            params={
                "select": "clinic_id,role,clinics(name)",
                "auth_user_id": f"eq.{session.user_id}",
                "status": "eq.active",
                "limit": "100",
            },
        )
        return {"csrf": session.csrf, "memberships": rows}

    @app.post("/api/logout")
    async def logout(request: Request) -> JSONResponse:
        await identity(request)
        sid = request.cookies.get("clinic_session", "")
        sessions.pop(sid, None)
        for key in list(previews):
            if key[0] == sid:
                del previews[key]
        reply = JSONResponse({"ok": True})
        reply.delete_cookie("clinic_session")
        return reply

    @app.get("/api/platform")
    async def platform(request: Request) -> Any:
        return await rpc(await identity(request), "clinic_platform_overview", {})

    @app.get("/api/clinics/{clinic}/rows/{table}")
    async def rows(clinic: UUID, table: str, request: Request, offset: int = 0) -> Any:
        session = await authorize(request, clinic)
        if table not in READ_FIELDS or offset < 0 or offset > 10000:
            raise HTTPException(404, "Page not found.")
        return await backend.call(
            "GET",
            f"/rest/v1/{table}",
            token=session.token,
            params={
                "select": READ_FIELDS[table],
                "clinic_id": f"eq.{clinic}",
                "limit": "50",
                "offset": str(offset),
                "order": "started_at.desc,id.desc" if table == "call_sessions" else "id.asc",
            },
        )

    @app.post("/api/clinics/{clinic}/rows/{table}")
    async def save(clinic: UUID, table: str, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        if table not in EDIT_FIELDS:
            raise HTTPException(403, "This resource is not directly editable.")
        values = await data(request)
        row_id = values.pop("id", None)
        if not values or set(values) - set(EDIT_FIELDS[table].split(",")):
            raise HTTPException(400, "Unknown or protected fields.")
        if row_id and table == "doctor_services":
            raise HTTPException(403, "Fees are append-only; use a new effective interval.")
        if table in {"doctors", "services"}:
            name_key = "display_name" if table == "doctors" else "name"
            if name_key in values:
                if not isinstance(values[name_key], str):
                    raise HTTPException(400, "Invalid name.")
                values["normalized_name"] = normalize(values[name_key])
        if row_id:
            try:
                row_id = str(UUID(row_id))
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(400, "Invalid row identifier.") from None
            result = await backend.call(
                "PATCH",
                f"/rest/v1/{table}",
                token=session.token,
                body=values,
                params={"clinic_id": f"eq.{clinic}", "id": f"eq.{row_id}"},
            )
            if not result:
                raise HTTPException(404, "Record not found in this clinic.")
            return {"saved": True, "live": False}
        await backend.call(
            "POST",
            f"/rest/v1/{table}",
            token=session.token,
            body=values | {"clinic_id": str(clinic)},
        )
        return {"saved": True, "live": False}

    @app.get("/api/clinics/{clinic}/settings")
    async def get_settings(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic)
        return await backend.call(
            "GET",
            "/rest/v1/clinics",
            token=session.token,
            params={
                "select": "name,timezone,greeting,emergency_message,"
                "fallback_message,default_language,"
                "supported_languages,maximum_call_duration_seconds,monthly_minute_limit,"
                "active_configuration_version_id,status,recording_enabled,transfer_enabled",
                "id": f"eq.{clinic}",
                "limit": "1",
            },
        )

    @app.post("/api/clinics/{clinic}/settings")
    async def settings_save(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        return await rpc(
            session, "clinic_settings", {"target": str(clinic), "settings": await data(request)}
        )

    @app.post("/api/clinics/{clinic}/membership")
    async def membership(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, {"owner"})
        values = await data(request)
        if set(values) != {"user_id", "new_role", "active"}:
            raise HTTPException(400, "User ID, role and active state required.")
        return await rpc(session, "clinic_membership", values | {"target": str(clinic)})

    @app.post("/api/clinics/{clinic}/preview")
    async def preview(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        values = await data(request)
        source = values.get("source")
        try:
            source = str(UUID(source)) if source else None
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid source version.") from None
        # Read the pointer before preview: concurrent publication makes publish fail safely.
        active = await backend.call(
            "GET",
            "/rest/v1/clinics",
            token=session.token,
            params={"select": "active_configuration_version_id", "id": f"eq.{clinic}"},
        )
        result = await rpc(session, "clinic_preview", {"target": str(clinic), "source": source})
        try:
            snapshot = Snapshot.model_validate(result[0]["snapshot"])
            if snapshot.clinic_id != clinic:
                raise ValueError
        except (ValueError, IndexError, KeyError):
            raise HTTPException(409, "Draft configuration is incomplete or invalid.") from None
        saved = {
            "target": str(clinic),
            "source": source,
            "expected_digest": result[0]["digest"],
            "expected_active": active[0]["active_configuration_version_id"],
        }
        previews[(request.cookies["clinic_session"], clinic)] = saved
        return {
            "snapshot": snapshot.model_dump(mode="json"),
            "digest": result[0]["digest"],
            "rollback": source is not None,
        }

    @app.post("/api/clinics/{clinic}/publish")
    async def publish(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        key = (request.cookies["clinic_session"], clinic)
        saved = previews.get(key)
        if saved is None:
            raise HTTPException(409, "Preview and review this configuration first.")
        result = await rpc(session, "clinic_publish", saved)
        previews.pop(key, None)
        return {"published": result[0]["version_id"], "indexed": await reindex(result[0], clinic)}

    async def reindex(published_version: dict[str, Any], clinic: UUID) -> int | None:
        """Refresh the optional semantic index. Publication is never blocked by search."""
        if vectors is None:
            return None
        try:
            snapshot = Snapshot.model_validate(published_version["snapshot"])
            return await vectors.index(
                clinic, UUID(str(published_version["version_id"])), snapshot_sections(snapshot)
            )
        except Exception:
            logger.warning("Semantic index refresh failed; lexical search still serves calls")
            return None

    async def clinic_doctors(session: WebSession, clinic: UUID) -> list[dict[str, Any]]:
        rows = await backend.call(
            "GET",
            "/rest/v1/doctors",
            token=session.token,
            params={
                "select": "id,display_name,aliases",
                "clinic_id": f"eq.{clinic}",
                "status": "eq.active",
                "limit": "2000",
            },
        )
        return list(rows or [])

    async def document(session: WebSession, clinic: UUID, document_id: UUID) -> dict[str, Any]:
        rows = await backend.call(
            "GET",
            "/rest/v1/knowledge_documents",
            token=session.token,
            params={
                "select": DOCUMENT_FIELDS + ",sections,extracted_text",
                "clinic_id": f"eq.{clinic}",
                "id": f"eq.{document_id}",
                "limit": "1",
            },
        )
        if not rows:
            raise HTTPException(404, "Document not found.")
        row: dict[str, Any] = rows[0]
        return row

    @app.get("/api/clinics/{clinic}/documents")
    async def document_list(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        return await backend.call(
            "GET",
            "/rest/v1/knowledge_documents",
            token=session.token,
            params={
                "select": DOCUMENT_FIELDS,
                "clinic_id": f"eq.{clinic}",
                "order": "created_at.desc",
                "limit": "50",
            },
        )

    @app.get("/api/clinics/{clinic}/documents/{document_id}")
    async def document_detail(clinic: UUID, document_id: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        return await document(session, clinic, document_id)

    @app.post("/api/clinics/{clinic}/documents/upload")
    async def document_upload(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, MANAGERS)
        category = request.query_params.get("category", "")
        filename = request.query_params.get("filename", "")
        replaces = request.query_params.get("replaces", "")
        if category not in CATEGORIES or not 1 <= len(filename) <= 200:
            raise HTTPException(400, "Choose a document type and a valid file name.")
        try:
            parsed = extract(filename, await request.body())
        except DocumentRejected as exc:
            raise HTTPException(400, str(exc)) from None
        duplicate = await backend.call(
            "GET",
            "/rest/v1/knowledge_documents",
            token=session.token,
            params={
                "select": "id",
                "clinic_id": f"eq.{clinic}",
                "checksum": f"eq.{parsed.checksum}",
                "limit": "1",
            },
        )
        if duplicate:
            raise HTTPException(409, "This document was already uploaded.")
        version, superseded = 1, None
        if replaces:
            try:
                previous = await document(session, clinic, UUID(replaces))
            except ValueError:
                raise HTTPException(400, "Invalid document to replace.") from None
            version, superseded = int(previous["version"]) + 1, previous["id"]
        doctors = [
            (UUID(row["id"]), [row["display_name"], *(row.get("aliases") or [])])
            for row in await clinic_doctors(session, clinic)
        ]
        sections = tag_doctors(parsed.sections, doctors)
        created = await backend.call(
            "POST",
            "/rest/v1/knowledge_documents",
            token=session.token,
            body={
                "clinic_id": str(clinic),
                # Logical provenance path; the upload itself is not retained server-side.
                "storage_path": f"{clinic}/{parsed.checksum[:32]}",
                "original_filename": filename,
                "mime_type": parsed.mime_type,
                "document_category": category,
                "checksum": parsed.checksum,
                "status": "needs_review",
                "version": version,
                "supersedes_document_id": superseded,
                "title": parsed.title,
                "sections": [section.model_dump(mode="json") for section in sections],
                "extracted_text": "\n\n".join(
                    f"{section.heading}\n{section.text}".strip() for section in sections
                )[:100000],
                "extraction_warnings": list(parsed.warnings),
                "uploaded_by": str(session.user_id),
            },
        )
        return created[0] if isinstance(created, list) else created

    @app.post("/api/clinics/{clinic}/documents/{document_id}")
    async def document_save(clinic: UUID, document_id: UUID, request: Request) -> Any:
        """Save the reviewed wording. Nothing reaches callers until a version is published."""
        session = await authorize(request, clinic, MANAGERS)
        values = await data(request)
        if set(values) - {"title", "document_category", "doctor_id", "sections"}:
            raise HTTPException(400, "Unsupported document field.")
        allowed = {UUID(row["id"]) for row in await clinic_doctors(session, clinic)}
        update: dict[str, Any] = {"reviewed_by": str(session.user_id)}
        if "sections" in values:
            try:
                sections = [DraftSection.model_validate(row) for row in values["sections"]]
            except (ValidationError, TypeError) as exc:
                raise HTTPException(400, "A section is empty or too long.") from exc
            if len(sections) > 200 or sum(len(s.text) for s in sections) > 100000:
                raise HTTPException(400, "Keep the reviewed text under the supported size.")
            if any(s.doctor_id is not None and s.doctor_id not in allowed for s in sections):
                raise HTTPException(400, "A section names a doctor from another clinic.")
            update["sections"] = [s.model_dump(mode="json") for s in sections]
        if "title" in values:
            title = values["title"]
            if not isinstance(title, str) or not 1 <= len(title) <= 200:
                raise HTTPException(400, "Give the document a short title.")
            update["title"] = title
        if values.get("document_category") is not None:
            if values["document_category"] not in CATEGORIES:
                raise HTTPException(400, "Choose a supported document type.")
            update["document_category"] = values["document_category"]
        if "doctor_id" in values:
            doctor = values["doctor_id"]
            try:
                chosen = UUID(doctor) if isinstance(doctor, str) and doctor else None
            except ValueError:
                raise HTTPException(400, "Choose a doctor from this clinic.") from None
            if (chosen is None and doctor not in (None, "")) or (
                chosen is not None and chosen not in allowed
            ):
                raise HTTPException(400, "Choose a doctor from this clinic.")
            update["doctor_id"] = str(chosen) if chosen else None
        return await backend.call(
            "PATCH",
            "/rest/v1/knowledge_documents",
            token=session.token,
            body=update,
            params={"clinic_id": f"eq.{clinic}", "id": f"eq.{document_id}"},
        )

    @app.post("/api/clinics/{clinic}/documents/{document_id}/status")
    async def document_status(clinic: UUID, document_id: UUID, request: Request) -> Any:
        """Approve reviewed prose for publication, or withdraw it again."""
        session = await authorize(request, clinic, MANAGERS)
        status = (await data(request)).get("status")
        if status not in {"needs_review", "published", "rejected", "archived"}:
            raise HTTPException(400, "Unsupported document status.")
        current = await document(session, clinic, document_id)
        if status == "published" and not current.get("sections"):
            raise HTTPException(409, "Review the extracted text before approving it.")
        update = {"status": status, "reviewed_by": str(session.user_id)}
        stamp = datetime.now(timezone.utc).isoformat()
        if status == "published":
            update["published_at"] = stamp
            # Republishing an archived document must make it effective again. Leaving
            # the archival expiry in place makes preview silently omit every section.
            update["effective_until"] = None
        if status == "archived":
            update["effective_until"] = stamp
        return await backend.call(
            "PATCH",
            "/rest/v1/knowledge_documents",
            token=session.token,
            body=update,
            params={"clinic_id": f"eq.{clinic}", "id": f"eq.{document_id}"},
        )

    @app.post("/api/clinics/{clinic}/requests/{kind}/{request_id}/status")
    async def request_status(clinic: UUID, kind: str, request_id: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, STAFF)
        return await rpc(
            session,
            "clinic_request_status",
            {
                "target": str(clinic),
                "request_id": str(request_id),
                "request_kind": kind,
                "new_status": (await data(request)).get("status"),
            },
        )

    @app.post("/api/clinics/{clinic}/requests/{kind}/{request_id}/detail")
    async def request_detail(clinic: UUID, kind: str, request_id: UUID, request: Request) -> Any:
        session = await authorize(request, clinic, STAFF)
        try:
            keyring = cipher or PiiCipher.from_environment()
            result = await rpc(
                session,
                "clinic_request_detail",
                {"target": str(clinic), "request_id": str(request_id), "request_kind": kind},
            )
            if result["erased"]:
                return {"erased": True}
            return {
                name: keyring.decrypt(
                    base64.b64decode(result[name]), result["version"], clinic, request_id, name
                )
                for name in ("name", "phone")
            }
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                503, "Protected details unavailable; check key provisioning."
            ) from None

    async def published_version(session: WebSession, clinic: UUID) -> tuple[UUID, Snapshot]:
        rows = await backend.call(
            "GET",
            "/rest/v1/configuration_versions",
            token=session.token,
            params={
                "select": "id,snapshot",
                "clinic_id": f"eq.{clinic}",
                "status": "eq.published",
                "limit": "1",
            },
        )
        try:
            snapshot = Snapshot.model_validate(rows[0]["snapshot"])
            if snapshot.clinic_id != clinic:
                raise ValueError
            return UUID(str(rows[0]["id"])), snapshot
        except (ValueError, IndexError, KeyError):
            raise HTTPException(409, "Publish a valid version 2 configuration first.") from None

    async def published(session: WebSession, clinic: UUID) -> Snapshot:
        return (await published_version(session, clinic))[1]

    @app.get("/api/clinics/{clinic}/today")
    async def today(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic)
        snapshot = await published(session, clinic)
        knowledge = StructuredKnowledge(snapshot)
        pending = {}
        for table in ("appointment_requests", "callback_requests"):
            pending[table] = await backend.call(
                "GET",
                f"/rest/v1/{table}",
                token=session.token,
                params={
                    "select": READ_FIELDS[table],
                    "clinic_id": f"eq.{clinic}",
                    "status": "eq.new",
                    "order": "created_at.desc",
                    "limit": "50",
                },
            )
        unresolved = await backend.call(
            "GET",
            "/rest/v1/call_sessions",
            token=session.token,
            params={
                "select": READ_FIELDS["call_sessions"],
                "clinic_id": f"eq.{clinic}",
                "failure_code": "not.is.null",
                "is_test": "eq.false",
                "order": "started_at.desc",
                "limit": "50",
            },
        )
        return {
            "status": knowledge.current_status().model_dump(mode="json"),
            "doctors": knowledge.find_doctors(Query()).model_dump(mode="json"),
            "pending_requests": pending,
            "unresolved_calls": unresolved,
            "doctor_hours": [
                knowledge.availability(Query(doctor=str(doctor.id))).model_dump(mode="json")
                for doctor in snapshot.doctors[:50]
            ],
            "limit": 50,
        }

    @app.post("/api/clinics/{clinic}/test")
    async def agent_test(clinic: UUID, request: Request) -> Any:
        session = await authorize(request, clinic)
        values = await data(request)
        text = values.get("question", "")
        if not isinstance(text, str) or len(text) > 500:
            raise HTTPException(400, "Use a short administrative question.")
        version, snapshot = await published_version(session, clinic)
        result = await HybridRetriever(snapshot, version, vectors).result(text)
        passages = result["data"]["passages"]
        answer = snapshot.fallback_message
        generated = False
        if passages and answerer is not None:
            try:
                answer = await answerer(text, snapshot, passages)
                generated = True
            except Exception as exc:
                logger.warning("Dashboard grounded answer failed (%s)", type(exc).__name__)
                answer = (
                    "Relevant published information was found, but a reliable answer could not "
                    "be generated. Please review the source facts below."
                )
        return {
            "is_test": True,
            "action": "rag",
            "answer": answer,
            "generated": generated,
            "result": result,
        }

    return app
