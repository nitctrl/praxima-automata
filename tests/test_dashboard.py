import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_structured_knowledge import content  # noqa: F401

from clinic.dashboard import SupabaseGateway, WebSettings, create_app

CLINIC, OTHER, USER, VERSION = uuid4(), uuid4(), uuid4(), uuid4()
DOCTOR, DOCUMENT = uuid4(), uuid4()
CONFIG = WebSettings("https://example.supabase.co", "sb_publishable_test", "https://testserver")


@pytest.fixture
def web():
    state = {"role": "manager", "calls": [], "revoked": False}

    def handle(request):
        state["calls"].append(request)
        path = request.url.path
        if path == state.get("unavailable_path"):
            raise httpx.ReadTimeout("private upstream error", request=request)
        if path == "/auth/v1/token":
            return httpx.Response(
                200, json={"access_token": "opaque-provider-token", "expires_in": 900}
            )
        if path == "/auth/v1/user":
            return httpx.Response(401 if state["revoked"] else 200, json={"id": str(USER)})
        assert request.headers["Authorization"] == "Bearer opaque-provider-token"
        if path == "/rest/v1/clinic_users":
            if request.url.params.get("clinic_id") == f"eq.{OTHER}":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "clinic_id": str(CLINIC),
                        "role": state["role"],
                        "clinics": {"name": "Test clinic"},
                    }
                ],
            )
        if path == "/rest/v1/rpc/clinic_platform_overview":
            return httpx.Response(403)
        if path == "/rest/v1/knowledge_documents":
            if request.method == "GET":
                return httpx.Response(200, json=state.get("documents", []))
            return httpx.Response(200, json=[{"id": str(DOCUMENT), "status": "needs_review"}])
        if path == "/rest/v1/doctors" and "doctors" in state:
            return httpx.Response(200, json=state["doctors"])
        if path == "/rest/v1/configuration_versions" and "snapshot" in state:
            return httpx.Response(
                200, json=[{"id": str(VERSION), "snapshot": state["snapshot"]}]
            )
        if path in {
            "/rest/v1/appointment_requests",
            "/rest/v1/callback_requests",
            "/rest/v1/call_sessions",
        }:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"id": str(uuid4())}])

    gateway = SupabaseGateway(CONFIG, httpx.MockTransport(handle))
    with TestClient(create_app(CONFIG, gateway=gateway), base_url=CONFIG.origin) as client:
        yield client, state


def login(client):
    res = client.post(
        "/api/login",
        json={"email": "test@example.invalid", "password": "not-real"},
        headers={"Origin": CONFIG.origin},
    )
    assert res.status_code == 200
    return {"Origin": CONFIG.origin, "X-CSRF-Token": res.json()["csrf"]}


def test_anonymous_shell_and_protected_api(web):
    client, _ = web
    page = client.get("/")
    assert page.status_code == 200
    assert "Sign in to your clinic" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert client.get(f"/api/clinics/{CLINIC}/rows/doctors").status_code == 401
    assert client.get("/api/me", headers={"Host": "evil.example"}).status_code == 400


def test_secure_opaque_cookie_and_no_token_exposure(web):
    client, _ = web
    result = client.post(
        "/api/login", json={"email": "x", "password": "y"}, headers={"Origin": CONFIG.origin}
    )
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "opaque-provider-token" not in cookie + result.text
    assert client.get("/api/me").status_code == 200


def test_origin_csrf_and_oversized_body(web):
    client, _ = web
    headers = login(client)
    path = f"/api/clinics/{CLINIC}/rows/doctors"
    assert client.post(path, json={}, headers={"Origin": CONFIG.origin}).status_code == 403
    assert (
        client.post(path, json={}, headers=headers | {"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.post(path, json={"x": "a" * 17000}, headers=headers).status_code == 413


@pytest.mark.parametrize("role", ["viewer", "receptionist"])
def test_read_only_roles_cannot_author(web, role):
    client, state = web
    headers = login(client)
    state["role"] = role
    assert client.get(f"/api/clinics/{CLINIC}/rows/doctors").status_code == 200
    assert (
        client.post(
            f"/api/clinics/{CLINIC}/rows/doctors", json={"display_name": "Test"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/clinics/{CLINIC}/preview", json={}, headers=headers).status_code == 403
    )


MARKDOWN = b"# Our clinic\n\nWe opened in a two room building.\n"


def send(client, headers, path, payload):
    return client.post(
        path, content=payload, headers=headers | {"Content-Type": "application/octet-stream"}
    )


def test_uploaded_document_is_parsed_and_stored_for_review(web):
    client, state = web
    headers = login(client)
    state["doctors"] = [{"id": str(DOCTOR), "display_name": "Dr Anaya Sharma", "aliases": []}]
    result = send(
        client,
        headers,
        f"/api/clinics/{CLINIC}/documents/upload?filename=story.md&category=story",
        MARKDOWN,
    )
    assert result.status_code == 200
    stored = json.loads(
        [c for c in state["calls"] if c.url.path == "/rest/v1/knowledge_documents"][-1].content
    )
    assert stored["status"] == "needs_review"
    assert stored["clinic_id"] == str(CLINIC)
    assert stored["storage_path"].startswith(f"{CLINIC}/")
    assert stored["sections"][0]["text"] == "We opened in a two room building."
    assert "two room" in stored["extracted_text"]


def test_unsupported_and_oversized_uploads_are_refused(web):
    client, state = web
    headers = login(client)
    base = f"/api/clinics/{CLINIC}/documents/upload"
    bad = send(client, headers, f"{base}?filename=x.pdf&category=story", b"%PDF")
    assert bad.status_code == 400
    assert send(client, headers, f"{base}?filename=x.md&category=hack", MARKDOWN).status_code == 400
    assert (
        send(client, headers, f"{base}?filename=x.md&category=story", b"x" * 5_242_881).status_code
        == 413
    )
    # The upload route is the only non-JSON surface; everything else still requires JSON.
    assert (
        client.post(
            f"/api/clinics/{CLINIC}/documents/{DOCUMENT}",
            content=b"{}",
            headers=headers | {"Content-Type": "application/octet-stream"},
        ).status_code
        == 415
    )
    state["documents"] = [{"id": str(DOCUMENT)}]
    assert (
        send(client, headers, f"{base}?filename=x.md&category=story", MARKDOWN).status_code == 409
    )


def test_document_review_rejects_foreign_doctors_and_needs_manager(web):
    client, state = web
    headers = login(client)
    state["doctors"] = [{"id": str(DOCTOR), "display_name": "Dr Anaya Sharma", "aliases": []}]
    section = {
        "id": str(uuid4()),
        "position": 0,
        "heading": "About",
        "text": "Reviewed wording.",
        "doctor_id": str(uuid4()),
        "keywords": [],
    }
    path = f"/api/clinics/{CLINIC}/documents/{DOCUMENT}"
    assert client.post(path, json={"sections": [section]}, headers=headers).status_code == 400
    section["doctor_id"] = str(DOCTOR)
    assert client.post(path, json={"sections": [section]}, headers=headers).status_code == 200
    assert client.post(path, json={"status": "published"}, headers=headers).status_code == 400
    state["role"] = "receptionist"
    assert client.get(f"/api/clinics/{CLINIC}/documents").status_code == 403
    assert client.post(path, json={"title": "Story"}, headers=headers).status_code == 403


def test_document_approval_requires_reviewed_sections(web):
    client, state = web
    headers = login(client)
    path = f"/api/clinics/{CLINIC}/documents/{DOCUMENT}/status"
    state["documents"] = [{"id": str(DOCUMENT), "sections": []}]
    assert client.post(path, json={"status": "published"}, headers=headers).status_code == 409
    assert client.post(path, json={"status": "deleted"}, headers=headers).status_code == 400
    state["documents"] = [{"id": str(DOCUMENT), "sections": [{"text": "Reviewed."}]}]
    assert client.post(path, json={"status": "published"}, headers=headers).status_code == 200
    approval = json.loads(state["calls"][-1].content)
    assert approval["status"] == "published" and approval["published_at"]
    assert approval["effective_until"] is None


def test_cross_clinic_and_protected_fields_denied(web):
    client, _ = web
    headers = login(client)
    assert client.get(f"/api/clinics/{OTHER}/rows/doctors").status_code == 403
    assert client.get(f"/api/clinics/{CLINIC}/rows/caller_profiles").status_code == 404
    for body in [{"clinic_id": str(OTHER)}, {"normalized_name": "forged"}]:
        assert (
            client.post(
                f"/api/clinics/{CLINIC}/rows/doctors", json=body, headers=headers
            ).status_code
            == 400
        )
    assert client.get("/api/platform").status_code == 403


def test_authoring_normalizes_name_binds_tenant_and_fees_append_only(web):
    client, state = web
    headers = login(client)
    result = client.post(
        f"/api/clinics/{CLINIC}/rows/doctors", json={"display_name": "Dr. Sharma"}, headers=headers
    )
    assert result.json() == {"saved": True, "live": False}
    sent = json.loads(state["calls"][-1].content)
    assert sent["clinic_id"] == str(CLINIC) and sent["normalized_name"] == "dr sharma"
    assert (
        client.post(
            f"/api/clinics/{CLINIC}/rows/doctor_services",
            json={"id": str(uuid4()), "current_fee": 12},
            headers=headers,
        ).status_code
        == 403
    )


def test_revocation_and_logout(web):
    client, state = web
    headers = login(client)
    state["revoked"] = True
    assert client.get("/api/me").status_code == 401
    state["revoked"] = False
    assert client.post("/api/logout", json={}, headers=headers).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_publish_requires_review_and_pii_not_available_to_viewer(web):
    client, state = web
    headers = login(client)
    assert (
        client.post(f"/api/clinics/{CLINIC}/publish", json={}, headers=headers).status_code == 409
    )
    state["role"] = "viewer"
    assert (
        client.post(
            f"/api/clinics/{CLINIC}/requests/callback/{uuid4()}/detail", json={}, headers=headers
        ).status_code
        == 403
    )


def test_settings_reject_privileged_key_and_non_tls_origin():
    with pytest.raises(ValueError):
        WebSettings(CONFIG.supabase_url, "sb_secret_bad", CONFIG.origin)
    with pytest.raises(ValueError):
        WebSettings(CONFIG.supabase_url, CONFIG.publishable_key, "http://public.example")


def test_agent_test_uses_one_rag_path_and_stays_tenant_scoped(web, content):  # noqa: F811
    client, state = web
    endpoint = f"/api/clinics/{CLINIC}/test"
    assert client.post(endpoint, json={}).status_code in {401, 403}
    headers = login(client)
    state["snapshot"] = content | {"clinic_id": str(CLINIC)}
    question = "Which doctors work at this clinic?"
    result = client.post(endpoint, json={"question": question}, headers=headers)
    assert result.status_code == 200
    assert result.json()["action"] == "rag"
    assert result.json()["result"]["status"] == "success"
    assert "Dr Anaya Sharma" in result.json()["answer"]
    denied = client.post(f"/api/clinics/{OTHER}/test", headers=headers,
                         json={"question": question})
    assert denied.status_code == 403

    state["snapshot"] = content | {
        "clinic_id": str(CLINIC),
        "schema_version": 3,
        "document_sections": [{
            "id": str(uuid4()),
            "document_id": str(uuid4()),
            "document_title": "Doctor biography",
            "document_version": 1,
            "topic": "doctor_bio",
            "heading": "Dr Suresh Kumar Mahto",
            "text": "Dr Suresh Kumar Mahto has an MBBS and over 30 years of experience.",
            "doctor_id": None,
            "keywords": ["qualification", "experience"],
        }],
    }
    document = client.post(
        endpoint,
        json={"question": "What qualification does Dr. Suresh have?"},
        headers=headers,
    )
    assert document.status_code == 200
    assert document.json()["action"] == "rag"
    assert "MBBS" in document.json()["answer"]
    degree = client.post(
        endpoint,
        json={"question": "What degree did Dr. Suresh earn?"},
        headers=headers,
    )
    assert degree.status_code == 200
    assert degree.json()["action"] == "rag"


def test_today_requires_auth_and_recovers_after_upstream_timeout(web, content):  # noqa: F811
    client, state = web
    endpoint = f"/api/clinics/{CLINIC}/today"
    assert client.get(endpoint).status_code == 401
    login(client)
    state["snapshot"] = content | {"clinic_id": str(CLINIC)}
    state["unavailable_path"] = "/rest/v1/callback_requests"
    unavailable = client.get(endpoint)
    assert unavailable.status_code == 503
    assert "private upstream error" not in unavailable.text
    del state["unavailable_path"]
    result = client.get(endpoint)
    assert result.status_code == 200
    assert result.json()["pending_requests"] == {
        "appointment_requests": [],
        "callback_requests": [],
    }
    assert result.json()["unresolved_calls"] == []


@pytest.mark.parametrize("failure", ["connect", "timeout", "invalid_json", "server_error"])
def test_gateway_unavailable_is_sanitized_and_diagnosable(caplog, failure):
    private = "sensitive-provider-response"

    def handle(request):
        if failure == "connect":
            raise httpx.ConnectError(private, request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text=private)
        return httpx.Response(503, text=private)

    async def exercise():
        gateway = SupabaseGateway(CONFIG, httpx.MockTransport(handle))
        try:
            with pytest.raises(HTTPException) as caught:
                await gateway.call("GET", "/rest/v1/configuration_versions", token=private)
            assert caught.value.status_code == 503
            assert private not in str(caught.value.detail)
        finally:
            await gateway.client.aclose()

    asyncio.run(exercise())
    assert "Dashboard upstream" in caplog.text
    assert private not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 409, 429])
def test_gateway_keeps_authorization_and_conflict_status(status):
    async def exercise():
        gateway = SupabaseGateway(
            CONFIG, httpx.MockTransport(lambda request: httpx.Response(status))
        )
        try:
            with pytest.raises(HTTPException) as caught:
                await gateway.call("GET", "/rest/v1/clinic_users", token="test-token")
            assert caught.value.status_code == status
        finally:
            await gateway.client.aclose()

    asyncio.run(exercise())
