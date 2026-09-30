# Voice agent ↔ platform contract

This repository contains only the AI voice agent. The database schema, migrations,
dashboard, publication workflow, notification sending and document upload belong to the
fullstack platform repository. This document is the interface the agent depends on. Any
change to it must be coordinated with this repository.

The last migration set that satisfied this contract is preserved in git history
(commit `fbaae43`, `supabase/migrations/202609170001` … `202609260011`). Move those files to
the platform repository as the starting baseline, then apply the required changes below.

> Administrative system only. The agent is not a medical device and performs no clinical
> decision support, triage, diagnosis or treatment advice.

## 1. Database access

| Rule | Detail |
|---|---|
| Role | The agent connects as `clinic_runtime` (not superuser, no `BYPASSRLS`). Every connection is checked at runtime and rejected otherwise. |
| Tenant scope | Each transaction runs `set_config('app.clinic_id', <uuid>, true)`. `clinic_private.runtime_clinic()` returns it. All functions below must scope by it. |
| Source of `clinic_id` | Derived only from the trusted SIP destination (trunk + called number) or `CONSOLE_CLINIC_ID` in explicit console mode. Never from the model or caller. |
| Timeouts | `statement_timeout=5s`, `lock_timeout=2s` per transaction. |
| Connection string | `DATABASE_URL` (Supabase direct or session pooler, port 5432, `sslmode=require`) plus `SUPABASE_PROJECT_REF`. Read from the process environment; `.env.runtime` / `.env` are local fallbacks only. |
| Pool | One pool per worker process (`max_size=2`), reused across calls. Size Postgres/pooler limits as `worker processes × 2`. |

### Tables the agent reads directly

* `public.configuration_versions (id, clinic_id, status, snapshot jsonb)` — only in console
  mode, `status='published'`, read-only transaction.

All other access goes through `SECURITY DEFINER` functions granted to `clinic_runtime`.

### Functions the agent calls

| Function | Used for | Contract |
|---|---|---|
| `clinic_private.resolve_destination(provider text, number text, trunk text)` → `(clinic_id, phone_number_id, configuration_version_id, timezone, supported_languages)` | Tenant resolution | Exactly one row for an active number of an active clinic with a published version; otherwise zero rows (agent fails closed). |
| `clinic_private.start_call(phone uuid, expected_version uuid, provider text, account_ref text, call_ref text, room_ref text, test_call bool)` → `jsonb {id, configuration_version_id, deadline_at}` | Call record | Idempotent on `(provider, account_ref, call_ref)`. Raises `54000` on concurrency or monthly-minute limit, `40001` if the version changed, `42501` for an invalid route. |
| `clinic_private.update_call(session uuid, action text, event uuid)` → `bool` | Lifecycle | `action ∈ {heartbeat, ended, timeout, tool_failed, transfer_failed, safety_routed}`; idempotent on `event`. Returns `false` once the call has ended. |
| `clinic_private.note_call(session uuid, topic text, outcome text)` | Fixed-vocabulary summary | No free text. |
| `clinic_private.create_request(session uuid, request uuid, kind text, name bytea, phone bytea, key_version text, details jsonb)` → `uuid` | Callback requests | `kind='callback'`; `details` keys limited to `requested_time`, `reason_category`. Idempotent on `request`. |
| `clinic_private.record_usage(session uuid, event uuid, input_tokens int, output_tokens int, stt_seconds numeric, tts_characters int)` | Usage | Idempotent on `event`. |
| `clinic_private.booked_slots(date, doctor uuid)` → `jsonb ["HH:MM", …]` | Free-slot calculation | Current bookings for that doctor and date. |
| `clinic_private.book_calendar_slot(key uuid, date, start time, end time, doctor uuid, service uuid, name bytea, phone bytea, key_version text, source text)` → `jsonb {status: booked|taken, …, clinic_name, clinic_whatsapp}` | Booking | Idempotent on `key`; returns `taken` on conflict. Requires an active `calendar` integration. |
| `clinic_private.queue_whatsapp(booking uuid, items jsonb)` | Notification outbox | The agent only enqueues. **The platform sends** the messages and marks them delivered. |

Outbox item shape: `{audience: "caller"|"clinic", recipient: base64(ciphertext), key_version, body}`.
Recipients are encrypted with purpose `whatsapp_recipient` (see §4).

### Required platform changes (not yet in the baseline migrations)

```sql
-- 1. start_call rejects schema_version 3 snapshots, which the current publisher produces.
--    Accept both (in start_call: schema_version IN (2,3)).

-- 2. Prevent overlapping bookings of different lengths (the unique index only blocks equal
--    start times, e.g. after slot_minutes changes).
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE public.calendar_bookings ADD CONSTRAINT calendar_bookings_no_overlap
  EXCLUDE USING gist (
    clinic_id WITH =,
    (coalesce(doctor_id, '00000000-0000-0000-0000-000000000000'::uuid)) WITH =,
    tsrange(requested_date + start_time, requested_date + end_time) WITH &&
  ) WHERE (status = 'booked');
-- book_calendar_slot must map exclusion_violation (23P01) to {"status":"taken"}.

-- 3. start_call hard-codes a concurrency limit of 4. Make it a clinics column
--    (e.g. max_concurrent_calls) so it matches CLINIC_MAX_CONCURRENT_CALLS / plan limits.

-- 4. Call transfer (optional, later): add a verified clinics.transfer_number and set
--    snapshot.transfer_enabled=true only after Plivo SIP REFER has been tested.
```

## 2. Published snapshot (`configuration_versions.snapshot`)

Validated by [src/clinic/snapshot.py](../src/clinic/snapshot.py) (`extra="forbid"`; unknown
fields reject the snapshot). Summary:

* `schema_version` 2 or 3, `clinic_id`, `name`, `timezone` (IANA), `slot_minutes` (5–240, default 30)
* `default_language` ∈ `supported_languages` (BCP-47 like `hi-IN`, `en-IN`)
* `greeting`, `emergency_message` (spoken verbatim for emergencies), `fallback_message`
* `transfer_enabled` (currently must be `false`)
* `doctors`, `services`, `locations`, `doctor_services` (fees), `weekly_schedules`,
  `special_date_schedules`, `schedule_exceptions`, `temporary_notices`, `approved_faqs`,
  `document_sections` (reviewed prose only; no internal notes, no raw extraction)

A snapshot is immutable. A call is pinned to the version resolved at call start.

## 3. Qdrant (document retrieval)

* One collection (`QDRANT_COLLECTION`, default `clinic_document_sections`), payload-partitioned:
  `clinic_id` keyword index with `is_tenant=true`, plus `version_id` and `superseded_at`.
* Point id: `uuid5(clinic_id : version_id : section_id)`.
* Every query filters `clinic_id` and the pinned `version_id`.
* On publish: index the new version's `document_sections`, then mark older versions with
  `superseded_at`. Delete them after `QDRANT_VERSION_GRACE_SECONDS` (must exceed the maximum
  call duration). [src/clinic/vectors.py](../src/clinic/vectors.py) implements indexing; the
  platform's publish job must call equivalent logic.
* The embedding model (`QDRANT_EMBEDDING_MODEL`) must be the same at index and query time.

## 4. Encryption of personal data

`CLINIC_PII_KEYS` (JSON: version → base64 32-byte AES key) and `CLINIC_PII_KEY_VERSION`.
AES-GCM with associated data `(clinic_id, record_id, purpose)`; see
[src/clinic/privacy.py](../src/clinic/privacy.py). Purposes: `patient_name`, `callback_number`,
`whatsapp_recipient`, `name`, `phone`. The platform needs the same keys to display or send.

## 5. Redis

Prefix `clinic:v1:`. Optional; without Redis calls read Postgres and no concurrency limit
is enforced by the agent.

| Key | Value | TTL |
|---|---|---|
| `snap:{clinic}:{version}` | Snapshot JSON | 7 days |
| `dest:{provider}:{trunk}:{number}` | Resolved clinic scope | 60 s |
| `dests:{clinic}` | Set of destination keys | — |
| `active:{clinic}` | Sorted set of active call slots | 2 h |

**On publish or phone-number change the platform must delete `dest:*` keys listed in
`dests:{clinic}`** (see `Cache.invalidate_clinic`), otherwise new calls may keep the old
version for up to 60 s.

## 6. SIP / LiveKit

* Worker `agent_name="inbound-agent"`; dispatch rule `roomPrefix "call-"`.
* The agent trusts only LiveKit-set SIP attributes: `sip.trunkID`, `sip.trunkPhoneNumber`
  (called number), `sip.callID` (idempotency key for `start_call`) and `sip.phoneNumber`
  (caller ID, used as the default callback number, never treated as identity).
* `TELEPHONY_PROVIDER` (default `plivo`) must equal `phone_numbers.provider`.
* The inbound trunk must restrict `allowed_addresses` to the carrier's signalling IPs
  and/or use digest auth. See [sip/inbound-trunk.json](../sip/inbound-trunk.json).
