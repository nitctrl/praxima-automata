# Phase 2 — structured knowledge and administrative tools

## Scope and deployment state

Phase 2 adds **offline/backend functionality**, not a deployed clinic voice agent.
The LiveKit/Sarvam/Anthropic pipeline, worker name, phone assignments and SIP
configuration remain unchanged. No database functions are attached to live calls.
Requests, transfers, clinical-safety routing, prompts and lifecycle integration
remain Phase 3; verified browser Auth and the dashboard remain Phase 4.

Implemented modules:

- [snapshot.py](../src/clinic/snapshot.py): frozen version-2 public-only models,
  effective ranges, references, duplicate/conflict checks and Unicode normalization.
- [publication.py](../src/clinic/publication.py): authorized draft preview,
  preview-checked atomic publication and rollback-as-new-publication.
- [knowledge.py](../src/clinic/knowledge.py): effective doctors/services/fees,
  clinic-local schedules and notices, public location and approved FAQ answers.
- [tools.py](../src/clinic/tools.py): seven typed LiveKit tools with consistent
  `status`, caller-safe `data` and `next_action` envelopes.
- [publication migration](../supabase/migrations/202609170004_publication.sql):
  public snapshot projection, validation, role-gated publication, history/audits,
  and serialization with authoring writes. Applied to the dedicated DEV project.

Pydantic and typing-extensions are now explicit dependencies; both were already
present in the voice stack's lockfile. No previously locked package version was
upgraded. Python remains 3.10-compatible.

## Publication workflow and authorization

1. An active owner/manager works through a future **verified** Supabase Auth
   adapter. `ClinicConfigurationService` accepts an `authenticated` connection
   whose identity has already been verified by that adapter. Supplying arbitrary
   JWT claims or a clinic UUID is not authentication.
2. `preview()` calls a SQL function that independently checks active membership,
   builds a complete public snapshot using explicit field allowlists, validates
   references/conflicts/settings and returns its SHA-256 digest and active version.
   Python validates the frozen typed model as a second boundary.
3. The preview is transient, not a writable published row. Pass its frozen
   snapshot to `StructuredKnowledge` for offline answer previews. Preview does not
   change the clinic's active pointer or affect calls.
4. `publish(preview)` requires READ COMMITTED, locks the clinic, rechecks
   authorization, rebuilds and validates content, and checks both the preview
   digest and expected active version. Stale edits/publications require another
   preview; they are not retried blindly.
5. One transaction supersedes the prior version, inserts immutable version-2
   content, updates the same-clinic active pointer and appends a verified-actor
   audit. A validation/query failure rolls back the whole operation.
6. `preview(source_version=...)` followed by `rollback(preview)` republishes an
   existing version-2 snapshot with a new ID/version and source provenance.
   It does not rewrite old snapshots, historical calls, or authoring rows.

When the service is used inside an outer transaction, **the caller owns its final
commit**. The service savepoint is not a durable outer commit. In normal request
handling use a bounded connection with transaction ownership explicitly defined.
Do not expose raw exceptions in future endpoints. SQL errors `40001` mean stale
preview, `42501` means forbidden, `23514` means invalid configuration, and `55P03`
means lock contention; the staff UI must provide safe actionable messages.

The `authenticated` role receives EXECUTE only on the preview/publication functions
in addition to its existing membership helper. Neither `anon` nor `clinic_runtime`
can publish. Private projection/validation/trigger helpers are not callable by
application roles. Keep `clinic_private` out of Supabase exposed API schemas.
Generic configuration-table mutation remains denied. Authoring changes are serialized
with publication; the database tests exercise two actual concurrent connections.

The snapshot includes active doctor/service/location/fee/weekly rows. Exceptions,
special-date schedules, notices and FAQs additionally require authoring
`publication_status='published'`. An explicit staff approval action must mark
those rows before they are eligible for the overall clinic publication; saving a
`draft` notice/FAQ never makes it caller-visible. Publication does not silently
approve every draft. Historical effective rows may remain in snapshots; current
and requested-date filtering, including notice expiry, happens deterministically.

Validation rejects missing clinic/location hours, overlapping weekly/date-specific
hours, references to missing/inactive entities, unsupported notice types/scopes,
invalid timezone/languages, duplicate identifiers and conflicting effective fees.
Transfer-enabled configurations are rejected until Phase 3 verifies handoff.
All document text/references are excluded in Phase 2, so an unscanned or failed
document cannot enter the tools through publication. Reviewed document ingestion
and retrieval require a later schema/provider decision, not a fake success here.

**Legacy seed compatibility:** Phase 1's immutable seed snapshots are version 1.
They are intentionally neither rewritten nor silently accepted by version-2 tools.
The existing resolver/data-layer tests can still read them, but `ClinicTools`
returns unavailable until an authorized version-2 publication exists. The new
integration tests create version-2 publications through the real service under
temporary authenticated memberships, then roll everything back. No permanent
Auth identities or automatic production publications were created. An owner
must explicitly preview/publish authoring data before future clinic-mode rollout;
rollback to a version-1 seed is rejected. This is a deliberate fail-closed gate.

## Runtime tool behavior

Construct `ClinicTools(ConfigurationRepository(database, scope), scope)` only
after trusted destination/trunk resolution. The future agent can discover the
tools with the installed SDK's `find_function_tools` convention. Do not allow
callers/models to create the repository or `ClinicScope`.

Each invocation reads **both clinic ID and the pinned version ID** through the
existing restricted repository, validates version-2 content and checks clinic,
timezone and language consistency. An ordinary new publication does not change
the pinned data. Missing/draft/archived/legacy/cross-clinic content fails closed.
Public models reject extra fields, including internal notes. Tool schemas expose
no clinic, configuration or SQL parameters. References returned for doctors,
services and locations are matched only within that call's snapshot.

| Tool | Behavior |
|---|---|
| `find_doctors` | NFKC/casefold/Unicode-aware names and aliases, speciality/service filters; ambiguous surname requires clarification |
| `get_current_clinic_status` | Current clinic-local open/closed status, active notices and walk-in restriction; optional ISO datetime must include offset and not be historical |
| `get_doctor_availability` | Doctor/location/service-scoped published hours for today/tomorrow/ISO date; morning/afternoon/evening and local time boundaries; never bookable slots |
| `get_consultation_fee` | Effective decimal fee and currency for a resolved doctor/service, or explicit missing/ambiguous result |
| `get_service_information` | Approved service description, appointment requirement, effective associated doctors and fees; not a service-slot/booking availability assertion |
| `get_clinic_location` | Effective public address, directions, landmark, map and parking; ambiguous locations require clarification |
| `search_approved_knowledge` | Exact normalized approved FAQ/alternate phrasing and optional category; no vector/document search or ungrounded fallback |

Availability is the intersection of clinic and doctor hours. Date-specific
exceptions replace special-date hours, which replace effective weekly hours.
Manual closure/temporary closure/unavailability intervals are then subtracted,
so **a clinic closure overrides an available doctor** and a two-hour closure
does not incorrectly close an entire day. Scoped service notices apply when
that service is requested; they do not close unrelated services.

No-walk-in periods are reported separately while preserving appointment-only
working hours. They are not described as open booking inventory. Expired notices
are not returned, and today's availability excludes already-past hours. Intervals
are half-open; adjacent fee/time ranges do not conflict. Unknown doctors/fees
and ambiguous matches never use model knowledge or another clinic as fallback.

Relative dates come from the backend clock in the pinned clinic timezone, with
a maximum 366-day future horizon. Ambiguous Hindi `kal` is not interpreted as
tomorrow automatically. `aaj`/`आज`, today, tomorrow and explicit ISO dates are
supported. DST-nonexistent/ambiguous schedule boundaries fail closed rather than
guessing an offset; normal offset-aware ranges are intersected in UTC.

Tool I/O has a five-second async deadline and validated argument lengths/enums;
database pool/query limits remain active. CPU validation is synchronous and
bounded by snapshot collection limits, not a hard preemptive CPU deadline.
Exceptions return a sanitized failed/unavailable envelope. The later orchestrator
must turn that into an appropriate human handoff/fallback; Phase 2 is not itself
an outage audio implementation or a medical safety classifier.

## Verification

See [tests](../tests/test_structured_knowledge.py) and
[real publication tests](../tests/test_publication_integration.py).

- Full regression: 104 tests (61 offline, 43 real-DB), including Phase 1 isolation.
- 32 new structured tests cover ambiguity/Hindi, effectivity, tomorrow-evening,
  leave/closure/special-date precedence, partial/expired notices, fee boundaries,
  DST, walk-in restrictions, service scope, argument bounds, immutable/public-only
  snapshots, actual LiveKit tool discovery/calls and sanitized failure handling.
- 14 new real-DB tests cover member roles, cross-clinic denial, public projections,
  atomic publish/rollback provenance, stale previews, draft exclusion, invalid
  references/hours/notices, runtime-role pinned tool reads and two-connection
  publication/authoring lock contention. All mutation fixtures and Auth users roll back.
- Formatter/lint, strict mypy, editor diagnostics and wheel/source build checked.
  Default pytest intentionally skips the real-DB tests; a skipped test is not
  evidence of isolation.

## Remaining release gates

No live call or production deployment was performed. Phase 3 must add deterministic
medical/emergency routing, confirmation state, request persistence, encryption,
session/timeout lifecycle and transfer/fallback before attaching these tools to
clinic calls. Phrase matching is not semantic retrieval or clinical classification.
Staff-auth HTTP/JWT verification and publication UI remain Phase 4. Vector/storage
isolation remains Phase 5. Vendor privacy/retention, global load budgets, replayable
end-to-end conversations and the 50-call pilot remain hardening gates.

Do not modify an applied migration or rewrite published snapshots to roll back.
Use a forward repair migration for schema changes; use authorized preview/rollback
for clinic facts. Keep the legacy generic business agent detached from clinic
traffic until explicit product-mode gating exists. No compliance or medical-device
certification is implied.