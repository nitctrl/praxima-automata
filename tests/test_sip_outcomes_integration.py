"""Rollback-only summary contracts against the currently published development clinic."""

from uuid import uuid4

import psycopg
import pytest

from clinic.development import fixture_id

pytestmark = pytest.mark.integration


def test_scoped_allowlisted_summary_and_finalization(db):
    clinic = fixture_id("A")
    version = db.execute(
        "SELECT active_configuration_version_id FROM clinics WHERE id=%s", (clinic,),
    ).fetchone()[0]
    phone = db.execute(
        "SELECT id FROM phone_numbers WHERE clinic_id=%s AND provider='test'", (clinic,),
    ).fetchone()[0]
    db.execute("SET LOCAL ROLE clinic_runtime")
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(clinic),))
    context = db.execute(
        "SELECT clinic_private.start_call(%s,%s,'test','rollback-outcome',%s,%s,true)",
        (phone, version, str(uuid4()), str(uuid4())),
    ).fetchone()[0]
    for topic, outcome in [("connected", "success"), ("availability", "ambiguous"),
                           ("availability", "success")]:
        db.execute("SELECT clinic_private.note_call(%s,%s,%s)", (context["id"], topic, outcome))
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("SELECT clinic_private.note_call(%s,'raw private text','success')",
                   (context["id"],))
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(fixture_id("B")),))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT clinic_private.note_call(%s,'fees','success')", (context["id"],))
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(clinic),))
    db.execute("SELECT clinic_private.update_call(%s,'ended',%s)", (context["id"], uuid4()))
    db.execute("SELECT clinic_private.note_call(%s,'voice','failed')", (context["id"],))
    db.execute("RESET ROLE")
    row = db.execute(
        "SELECT short_administrative_summary,ended_at,duration_seconds,is_test "
        "FROM call_sessions WHERE id=%s", (context["id"],),
    ).fetchone()
    assert row[0].startswith("Doctor availability lookup: completed.")
    assert row[1] is not None and row[2] >= 0 and row[3]
    db.execute("SET LOCAL ROLE authenticated")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT clinic_private.note_call(%s,'fees','success')", (context["id"],))