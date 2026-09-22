"""One hybrid-RRF corpus for uploaded prose and structured clinic facts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from clinic.documents import DocumentIndex, excerpt, tokens
from clinic.snapshot import DocumentSection, Snapshot
from clinic.vectors import VectorSearch

logger = logging.getLogger(__name__)
RAG_NAMESPACE = UUID("86a6e73c-9761-4a9b-a2ae-0db02d1489cd")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _money(value: Decimal, currency: str) -> str:
    return f"{value:.2f} {currency}"


def snapshot_sections(snapshot: Snapshot) -> tuple[DocumentSection, ...]:
    """Render every published fact into stable chunks and append reviewed uploads."""
    rows: list[DocumentSection] = []

    def add(key: str, topic: str, heading: str, text: str, doctor_id: UUID | None = None) -> None:
        identifier = uuid5(RAG_NAMESPACE, f"{snapshot.clinic_id}:{key}")
        rows.append(DocumentSection(
            id=identifier,
            document_id=identifier,
            document_title="Published clinic information",
            document_version=1,
            topic=topic,
            heading=heading,
            text=text,
            doctor_id=doctor_id,
            keywords=(),
        ))

    add(
        "clinic", "clinic", snapshot.name,
        f"Clinic name: {snapshot.name}. Timezone: {snapshot.timezone}. "
        f"Greeting: {snapshot.greeting} Emergency information: {snapshot.emergency_message}",
    )
    doctors = {row.id: row for row in snapshot.doctors}
    services = {row.id: row for row in snapshot.services}
    locations = {row.id: row for row in snapshot.locations}
    add(
        "doctor-list", "doctors", "Published doctors",
        "Doctors and physicians working at this clinic; published doctor list: " + "; ".join(
            f"{row.display_name} — {row.speciality}" for row in snapshot.doctors
        ),
    )
    for doctor_row in snapshot.doctors:
        add(
            f"doctor:{doctor_row.id}", "doctor", doctor_row.display_name,
            f"Doctor: {doctor_row.display_name}. Speciality: {doctor_row.speciality}. "
            f"Languages: {', '.join(doctor_row.languages)}. "
            f"Biography: {doctor_row.short_public_bio}. "
            f"Accepting new patients: {'yes' if doctor_row.accepts_new_patients else 'no'}. "
            f"Effective from {doctor_row.effective_from} to "
            f"{doctor_row.effective_until or 'open ended'}.",
            doctor_row.id,
        )
    for location_row in snapshot.locations:
        add(
            f"location:{location_row.id}", "location", location_row.name,
            f"Location: {location_row.name}. Address: {location_row.address}. "
            f"Landmark: {location_row.landmark or 'not published'}. "
            f"Directions: {location_row.directions or 'not published'}. "
            f"Parking: {location_row.parking_information or 'not published'}.",
        )
    for service_row in snapshot.services:
        add(
            f"service:{service_row.id}", "service", service_row.name,
            f"Service: {service_row.name}. Description: "
            f"{service_row.short_approved_description}. Appointment required: "
            f"{'yes' if service_row.appointment_required else 'no'}.",
        )
    for fee_row in snapshot.doctor_services:
        doctor, service = doctors.get(fee_row.doctor_id), services.get(fee_row.service_id)
        add(
            f"fee:{fee_row.id}", "fee", "Consultation fee",
            f"Published fee for {doctor.display_name if doctor else fee_row.doctor_id}, "
            f"{service.name if service else fee_row.service_id}: "
            f"{_money(fee_row.current_fee, fee_row.currency)}. Effective from "
            f"{fee_row.effective_from} to {fee_row.effective_until or 'open ended'}.",
            fee_row.doctor_id,
        )
    grouped: dict[UUID | None, list[str]] = {}
    for schedule_row in snapshot.weekly_schedules:
        place = locations.get(schedule_row.location_id)
        grouped.setdefault(schedule_row.doctor_id, []).append(
            f"{DAY_NAMES[schedule_row.day_of_week]} "
            f"{schedule_row.start_time.isoformat(timespec='minutes')}–"
            f"{schedule_row.end_time.isoformat(timespec='minutes')} at "
            f"{place.name if place else schedule_row.location_id} "
            f"({schedule_row.availability_type}), effective {schedule_row.effective_from} "
            f"to {schedule_row.effective_until or 'open ended'}"
        )
    for doctor_id, hour_lines in grouped.items():
        name = doctors[doctor_id].display_name if doctor_id in doctors else snapshot.name
        add(
            f"weekly:{doctor_id or 'clinic'}", "schedule", f"{name} weekly hours",
            f"Published weekly working hours for {name}: " + "; ".join(hour_lines)
            + ". These are working hours, not confirmed appointment slots.",
            doctor_id,
        )
    for special_row in snapshot.special_date_schedules:
        doctor = doctors.get(special_row.doctor_id) if special_row.doctor_id else None
        place = locations.get(special_row.location_id)
        add(
            f"special:{special_row.id}", "schedule", "Special-date hours",
            f"Special hours on {special_row.schedule_date} for "
            f"{doctor.display_name if doctor else snapshot.name}: "
            f"{special_row.start_time.isoformat(timespec='minutes')}–"
            f"{special_row.end_time.isoformat(timespec='minutes')} at "
            f"{place.name if place else special_row.location_id}.",
            special_row.doctor_id,
        )
    for exception_row in snapshot.schedule_exceptions:
        doctor = doctors.get(exception_row.doctor_id) if exception_row.doctor_id else None
        changed_hours = (
            f" {exception_row.start_time.isoformat(timespec='minutes')}–"
            f"{exception_row.end_time.isoformat(timespec='minutes')}"
            if exception_row.start_time and exception_row.end_time else ""
        )
        add(
            f"exception:{exception_row.id}", "schedule", "Schedule change",
            f"Schedule change on {exception_row.exception_date} for "
            f"{doctor.display_name if doctor else snapshot.name}: "
            f"{exception_row.status}{changed_hours}. {exception_row.public_message}",
            exception_row.doctor_id,
        )
    for notice_row in snapshot.temporary_notices:
        add(
            f"notice:{notice_row.id}", "daily_update", "Quick daily information",
            f"Current clinic notice ({notice_row.notice_type}) from "
            f"{notice_row.starts_at.isoformat()} to {notice_row.expires_at.isoformat()}: "
            f"{notice_row.public_message}", notice_row.doctor_id,
        )
    for faq_row in snapshot.approved_faqs:
        add(
            f"faq:{faq_row.id}", "faq", faq_row.canonical_question,
            f"Question: {faq_row.canonical_question} Alternative wording: "
            f"{'; '.join(faq_row.alternative_phrasings)} Answer: {faq_row.approved_answer}",
        )
    rows.extend(snapshot.document_sections)
    return tuple(rows)


class HybridRetriever:
    """The shared retrieval implementation used by dashboard and phone calls."""

    def __init__(self, snapshot: Snapshot, version: UUID | None, vectors: VectorSearch | None):
        self.snapshot = snapshot
        self.version = version
        self.sections = snapshot_sections(snapshot)
        self.index = DocumentIndex(self.sections)
        self.vectors = vectors

    async def search(self, question: str, *, limit: int = 4) -> list[tuple[float, DocumentSection]]:
        semantic: tuple[UUID, ...] = ()
        if self.vectors is not None and self.version is not None:
            try:
                semantic = tuple(await self.vectors.search(
                    question,
                    clinic=self.snapshot.clinic_id,
                    version=self.version,
                    doctor_id=None,
                    limit=max(limit * 3, 10),
                ))
            except Exception as exc:
                logger.warning(
                    "Semantic retrieval unavailable (%s); using lexical rank",
                    type(exc).__name__,
                )
        return self.index.search(question, semantic=semantic, limit=limit)

    async def result(self, question: str, *, limit: int = 4) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            return {"status": "unavailable", "data": {"passages": []}}
        hits = await self.search(question, limit=limit)
        wanted = tokens(question)
        passages = [
            {
                "source": section.document_title,
                "topic": section.topic,
                "heading": section.heading,
                "text": excerpt(section.text, wanted),
            }
            for _, section in hits
        ]
        return {
            "status": "success" if passages else "unavailable",
            "retrieval": "hybrid_rrf" if self.vectors is not None else "lexical_fallback",
            "data": {"passages": passages},
        }


def active_quick_info(snapshot: Snapshot) -> tuple[str, ...]:
    now = datetime.now(timezone.utc)
    return tuple(
        row.public_message
        for row in sorted(snapshot.temporary_notices, key=lambda item: -item.priority)
        if row.starts_at <= now < row.expires_at
    )
