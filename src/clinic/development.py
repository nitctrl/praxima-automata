"""Fictional, non-routable development fixtures. Never import in the voice worker."""

from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from psycopg import Connection
from psycopg.types.json import Jsonb

SEED_NAMESPACE = UUID("f7aee0b6-4d83-4f1a-a24e-133c2a54d27d")


def fixture_id(name: str) -> UUID:
    return uuid5(SEED_NAMESPACE, name)


def seed_fictional_clinics(conn: Connection[Any]) -> None:
    """Insert once, atomically. Never overwrite edits to existing seeded clinics."""
    for label, start, fee in [("A", "09:00", 450), ("B", "14:00", 850)]:
        clinic_id = fixture_id(label)
        existing = conn.execute(
            "SELECT 1 FROM public.clinics WHERE id = %s", (clinic_id,)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO public.clinics (id, name, slug, greeting, emergency_message, "
            "fallback_message, status) VALUES (%s, %s, %s, %s, %s, %s, 'active')",
            (
                clinic_id,
                f"Fictional Clinic {label} — development only",
                f"fictional-clinic-{label.lower()}",
                "This is a fictional automated administrative assistant.",
                "This assistant cannot provide medical advice. Contact local emergency services "
                "for urgent assistance. Development wording only; not clinic-approved for use.",
                "Confirmed information is unavailable. Please contact reception.",
            ),
        )
        location_id = fixture_id(f"{label}-location")
        conn.execute(
            "INSERT INTO public.locations (id, clinic_id, name, address, effective_from) "
            "VALUES (%s, %s, 'Fictional main location', 'Example address — not a real clinic', %s)",
            (location_id, clinic_id, date(2026, 1, 1)),
        )
        # NANP reserved fictional 555-01xx numbers, test provider/trunk only.
        number = "+12025550101" if label == "A" else "+12025550102"
        conn.execute(
            "INSERT INTO public.phone_numbers "
            "(id, clinic_id, provider, e164_number, trusted_trunk_id, status) "
            "VALUES (%s, %s, 'test', %s, 'fixture-only-not-a-live-trunk', 'active')",
            (fixture_id(f"{label}-phone"), clinic_id, number),
        )
        for index, name in enumerate(["Anaya Sharma", "Dev Sharma"]):
            doctor_id = fixture_id(f"{label}-doctor-{index}")
            conn.execute(
                "INSERT INTO public.doctors (id, clinic_id, display_name, normalized_name, "
                "aliases, speciality, languages, effective_from) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    doctor_id,
                    clinic_id,
                    f"Dr {name} ({label}, fictional)",
                    name.casefold(),
                    ["Sharma"],
                    "General consultation",
                    ["hi-IN", "en-IN"],
                    date(2026, 1, 1),
                ),
            )
            conn.execute(
                "INSERT INTO public.weekly_schedules (id, clinic_id, doctor_id, location_id, "
                "day_of_week, start_time, end_time, effective_from) "
                "VALUES (%s,%s,%s,%s,0,%s,%s,%s)",
                (
                    fixture_id(f"{label}-schedule-{index}"),
                    clinic_id,
                    doctor_id,
                    location_id,
                    start,
                    "12:00" if label == "A" else "18:00",
                    date(2026, 1, 1),
                ),
            )
        conn.execute(
            "INSERT INTO public.weekly_schedules (clinic_id, location_id, day_of_week, "
            "start_time, end_time, availability_type, effective_from) "
            "VALUES (%s,%s,0,%s,%s,'clinic_hours',%s)",
            (clinic_id, location_id, start, "12:00" if label == "A" else "18:00", date(2026, 1, 1)),
        )
        for index, name in enumerate(["Consultation", "Follow-up consultation", "Registration"]):
            service_id = fixture_id(f"{label}-service-{index}")
            conn.execute(
                "INSERT INTO public.services (id, clinic_id, name, normalized_name, "
                "short_approved_description, effective_from) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    service_id,
                    clinic_id,
                    name,
                    name.casefold(),
                    "Fictional administrative service description.",
                    date(2026, 1, 1),
                ),
            )
            conn.execute(
                "INSERT INTO public.doctor_services (clinic_id, doctor_id, service_id, "
                "current_fee, effective_from) VALUES (%s,%s,%s,%s,%s)",
                (
                    clinic_id,
                    fixture_id(f"{label}-doctor-0"),
                    service_id,
                    fee + index * 50,
                    date(2026, 1, 1),
                ),
            )
        conn.execute(
            "INSERT INTO public.schedule_exceptions (clinic_id, doctor_id, location_id, "
            "exception_date, status, public_message, internal_note, publication_status) "
            "VALUES (%s,%s,%s,CURRENT_DATE + 1,'unavailable',%s,%s,'published')",
            (
                clinic_id,
                fixture_id(f"{label}-doctor-0"),
                location_id,
                "This fictional doctor is unavailable tomorrow.",
                "PRIVATE fixture note, never public",
            ),
        )
        conn.execute(
            "INSERT INTO public.temporary_notices (clinic_id, notice_type, public_message, "
            "internal_note, starts_at, expires_at, priority, publication_status) "
            "VALUES (%s,'closure',%s,%s,now(),now()+interval '1 day',90,'published')",
            (clinic_id, "Fictional temporary closure for maintenance.", "PRIVATE maintenance note"),
        )
        conn.execute(
            "INSERT INTO public.approved_faqs (clinic_id, category, canonical_question, "
            "approved_answer, publication_status) VALUES (%s,'registration',%s,%s,'published')",
            (
                clinic_id,
                "Can this assistant confirm a booking?",
                "No. Appointment requests require confirmation from clinic staff.",
            ),
        )
        document_id = fixture_id(f"{label}-document")
        conn.execute(
            "INSERT INTO public.knowledge_documents (id,clinic_id,storage_path,original_filename,"
            "mime_type,document_category,checksum,status,version,extracted_text,published_at) "
            "VALUES (%s,%s,%s,'fictional-registration.txt','text/plain','registration',"
            "'fictional-fixture-not-an-upload','published',1,%s,now())",
            (
                document_id,
                clinic_id,
                f"{clinic_id}/fixtures/registration.txt",
                "Fictional registration policy: reception confirms all appointment requests.",
            ),
        )
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "development_only": True,
            "timezone": "Asia/Kolkata",
            "supported_languages": ["hi-IN", "en-IN"],
        }
        # Explicit public allowlists, never row_to_json(table.*) with private notes.
        projections = {
            "doctors": "id, display_name, normalized_name, aliases, speciality, status",
            "services": "id, name, short_approved_description, active",
            "locations": "id, name, address",
            "doctor_services": "id, doctor_id, service_id, current_fee, currency, effective_from, "
            "effective_until, status",
            "weekly_schedules": "id, doctor_id, location_id, day_of_week, start_time, end_time, "
            "effective_from, effective_until, status, availability_type",
            "schedule_exceptions": "id, doctor_id, location_id, exception_date, status, "
            "start_time, end_time, public_message",
            "temporary_notices": "id, notice_type, public_message, starts_at, expires_at, priority",
            "approved_faqs": (
                "id, canonical_question, approved_answer, effective_from, effective_until"
            ),
        }
        for table, columns in projections.items():
            # Both identifiers are static application constants, not inputs.
            row = conn.execute(
                f"SELECT coalesce(jsonb_agg(to_jsonb(p)), '[]'::jsonb) "
                f"FROM (SELECT {columns} FROM public.{table} WHERE clinic_id = %s) p",
                (clinic_id,),
            ).fetchone()
            assert row is not None
            snapshot[table] = row[0]
        snapshot["document_ids"] = [str(document_id)]
        version_id = fixture_id(f"{label}-version")
        conn.execute(
            "INSERT INTO public.configuration_versions (id,clinic_id,version_number,status,"
            "snapshot,prompt_version,published_at) "
            "VALUES (%s,%s,1,'published',%s,'fixture-v1',now())",
            (version_id, clinic_id, Jsonb(snapshot)),
        )
        conn.execute(
            "UPDATE public.clinics SET active_configuration_version_id = %s WHERE id = %s",
            (version_id, clinic_id),
        )
        session_id = fixture_id(f"{label}-call")
        conn.execute(
            "INSERT INTO public.call_sessions (id,clinic_id,phone_number_id,provider,"
            "provider_account_reference,provider_call_id,livekit_room_id,configuration_version_id,"
            "ended_at,disposition,is_test,retention_until) "
            "VALUES (%s,%s,%s,'test','fixture',%s,%s,%s,now(),'request_collected',true,"
            "now()+interval '30 days')",
            (
                session_id,
                clinic_id,
                fixture_id(f"{label}-phone"),
                f"fictional-call-{label}",
                f"fictional-room-{label}",
                version_id,
            ),
        )
        # Fictional request already anonymized; never pretend plaintext is encrypted.
        conn.execute(
            "INSERT INTO public.appointment_requests (clinic_id,call_session_id,idempotency_key,"
            "is_new_patient,doctor_id,preferred_date,pii_erased_at) "
            "VALUES (%s,%s,%s,true,%s,%s,now())",
            (
                clinic_id,
                session_id,
                uuid4(),
                fixture_id(f"{label}-doctor-0"),
                date.today() + timedelta(days=1),
            ),
        )
        conn.execute(
            "INSERT INTO public.audit_logs "
            "(clinic_id,action,resource_type,resource_id,correlation_id) "
            "VALUES (%s,'fictional_fixture_created','clinic',%s,%s)",
            (clinic_id, clinic_id, uuid4()),
        )
