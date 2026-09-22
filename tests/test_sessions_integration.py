"""Forward-migration contracts exercised in rollback-only development transactions."""

import json
from datetime import date, timedelta
from uuid import uuid4

import psycopg
import pytest

from clinic.development import fixture_id
from clinic.privacy import PiiCipher

pytestmark = pytest.mark.integration
A, B = fixture_id("A"), fixture_id("B")


def setup_call(db):
    user = uuid4()
    db.execute("INSERT INTO auth.users(id) VALUES(%s)", (user,))
    db.execute(
        "INSERT INTO clinic_users(clinic_id,auth_user_id,role) VALUES(%s,%s,'manager')", (A, user)
    )
    db.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(user),))
    db.execute("SET LOCAL ROLE authenticated")
    snapshot, digest = db.execute("SELECT * FROM public.clinic_preview(%s,NULL)", (A,)).fetchone()
    version = db.execute(
        "SELECT version_id FROM public.clinic_publish(%s,%s,%s,NULL)",
        (A, digest, fixture_id("A-version")),
    ).fetchone()[0]
    db.execute("RESET ROLE")
    phone = db.execute("SELECT id FROM phone_numbers WHERE clinic_id=%s", (A,)).fetchone()[0]
    db.execute("SET LOCAL ROLE clinic_runtime")
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(A),))
    call_id, room = str(uuid4()), str(uuid4())
    args = (phone, version, "test", "rollback-test", call_id, room, True)
    context = db.execute("SELECT clinic_private.start_call(%s,%s,%s,%s,%s,%s,%s)", args).fetchone()[
        0
    ]
    return context, args, snapshot, user


def test_start_idempotency_scope_pinning_and_lifecycle(db):
    context, args, _, _ = setup_call(db)
    assert (
        db.execute("SELECT clinic_private.start_call(%s,%s,%s,%s,%s,%s,%s)", args).fetchone()[0]
        == context
    )
    assert db.execute(
        "SELECT clinic_private.update_call(%s,'heartbeat',%s)", (context["id"], uuid4())
    ).fetchone()[0]
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(B),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT clinic_private.update_call(%s,'ended',%s)", (context["id"], uuid4()))
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(A),))
    assert not db.execute(
        "SELECT clinic_private.update_call(%s,'ended',%s)", (context["id"], uuid4())
    ).fetchone()[0]
    assert not db.execute(
        "SELECT clinic_private.update_call(%s,'ended',%s)", (context["id"], uuid4())
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT clinic_private.start_call(%s,%s,%s,%s,%s,%s,%s)", args)


@pytest.mark.parametrize("kind", ["appointment", "callback"])
def test_encrypted_request_idempotency_and_staff_only_status(db, kind):
    context, _, snapshot, _ = setup_call(db)
    request_id = uuid4()
    cipher = PiiCipher({"v1": b"x" * 32}, "v1")
    fields = {
        "doctor_id": snapshot["doctors"][0]["id"],
        "preferred_date": (date.today() + timedelta(days=2)).isoformat(),
        "reason_category": "human_requested",
    }
    args = (
        context["id"],
        request_id,
        kind,
        cipher.encrypt("Test", A, request_id, "name"),
        cipher.encrypt("+12025550101", A, request_id, "phone"),
        "v1",
        json.dumps(fields),
    )
    for _ in range(2):
        assert (
            db.execute(
                "SELECT clinic_private.create_request(%s,%s,%s,%s,%s,%s,%s::jsonb)", args
            ).fetchone()[0]
            == request_id
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute(
            "SELECT public.clinic_request_status(%s,%s,%s,'confirmed_externally')",
            (A, request_id, kind),
        )
    db.execute("SET LOCAL ROLE authenticated")
    detail = db.execute(
        "SELECT public.clinic_request_detail(%s,%s,%s)", (A, request_id, kind)
    ).fetchone()[0]
    assert detail["version"] == "v1" and not detail["erased"]
    db.execute("SELECT public.clinic_request_status(%s,%s,%s,'contacted')", (A, request_id, kind))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT callback_number_ciphertext FROM callback_requests")
    db.execute("RESET ROLE")
    assert (
        db.execute(
            "SELECT count(*) FROM audit_logs WHERE resource_id=%s "
            "AND action='sensitive_request_access'",
            (request_id,),
        ).fetchone()[0]
        == 1
    )


def test_request_rejects_foreign_reference_and_model_status(db):
    context, _, _, _ = setup_call(db)
    args = (context["id"], uuid4(), "appointment", b"x" * 40, b"y" * 40, "test")
    for fields in [
        {"status": "confirmed_externally"},
        {
            "doctor_id": str(fixture_id("B-doctor-0")),
            "preferred_date": (date.today() + timedelta(days=2)).isoformat(),
        },
    ]:
        with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
            db.execute(
                "SELECT clinic_private.create_request(%s,%s,%s,%s,%s,%s,%s::jsonb)",
                args + (json.dumps(fields),),
            )


def test_usage_is_idempotent_and_unpriced(db):
    context, _, _, _ = setup_call(db)
    event = uuid4()
    for _ in range(2):
        db.execute("SELECT clinic_private.record_usage(%s,%s,12,3,4,40)", (context["id"], event))
    db.execute("RESET ROLE")
    assert db.execute(
        "SELECT count(*),min(rate_version) FROM usage_records WHERE clinic_id=%s AND event_key=%s",
        (A, event),
    ).fetchone() == (1, "unpriced-units-v1")


def test_reconciliation_is_maintenance_only(db):
    context, _, _, _ = setup_call(db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT clinic_private.reconcile_calls()")
    db.execute("RESET ROLE")
    db.execute(
        "UPDATE call_sessions SET heartbeat_at=now()-interval '3 minutes' WHERE id=%s",
        (context["id"],),
    )
    assert db.execute("SELECT clinic_private.reconcile_calls()").fetchone()[0] >= 1
    assert (
        db.execute(
            "SELECT disposition FROM call_sessions WHERE id=%s", (context["id"],)
        ).fetchone()[0]
        == "worker_lost"
    )


def test_platform_requires_separate_membership_and_settings_are_allowlisted(db):
    _, _, _, user = setup_call(db)
    db.execute("SET LOCAL ROLE authenticated")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT public.clinic_platform_overview()")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute('SELECT public.clinic_settings(%s,\'{"status":"suspended"}\')', (A,))
    db.execute('SELECT public.clinic_settings(%s,\'{"greeting":"Draft greeting"}\')', (A,))
    db.execute("RESET ROLE")
    db.execute("INSERT INTO clinic_private.platform_admins(auth_user_id) VALUES(%s)", (user,))
    db.execute("SET LOCAL ROLE authenticated")
    result = db.execute("SELECT public.clinic_platform_overview()").fetchone()[0]
    assert len(result["clinics"]) >= 2
    assert "ciphertext" not in json.dumps(result)
