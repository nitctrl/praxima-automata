# Phase 1 — database and tenancy

## Implemented

- 22 public administrative tables with UUID keys, clinic-scoped foreign keys,
  restrictive deletion, tenant indexes, RLS, date/time checks and IANA timezone
  validation. All tenant-owned rows have non-null `clinic_id`; root clinics use `id`.
- `clinics`, `clinic_users`, `phone_numbers`, `locations`, `doctors`, `services`,
  `doctor_services`, `weekly_schedules`, `special_date_schedules`,
  `schedule_exceptions`, `temporary_notices`, `approved_faqs`,
  `configuration_versions`, `caller_profiles`, `consent_events`, `call_sessions`,
  `call_events`, `appointment_requests`, `callback_requests`,
  `knowledge_documents`, `usage_records`, `audit_logs`.
- Database-enforced nonoverlapping active fee intervals via `btree_gist` exclusion
  constraints, including half-open/open-ended ranges. Schedule conflict checks
  and publish validation are Phase 2; mutable authoring records are never runtime facts.
- Append-only audit/consent/call-event/usage history, immutable published content,
  per-clinic version numbers and same-clinic active pointers.
- Restricted async psycopg pool with bounded connection/query/lock waits and
  transaction-local scope. Both login and effective user must be `clinic_runtime`;
  migration credentials are rejected at runtime.
- Resolver matches exact trusted provider, called number and trunk and requires
  an active clinic/number with an active published configuration. It does not
  accept caller ID, spoken clinic names, model-selected UUIDs or untrusted
  participant attributes. No ingress adapter has been enabled yet.
- Configuration repository includes both clinic and pinned version predicates.
  Superseded content remains readable for pinned calls; drafts/archived versions
  do not. Fixture snapshots explicitly omit private notes.
- Staff repository checks active membership for **each** read/mutation, in
  addition to SQL RLS. It requires an authenticated DB context established by a
  future verified Auth adapter; it does not pretend to authenticate JWTs itself.

## Migrations

1. [202609170001_tenancy.sql](../supabase/migrations/202609170001_tenancy.sql): primary
   tenancy/content/call tables, RLS, role, private resolver, immutable versions.
2. [202609170002_supporting_entities.sql](../supabase/migrations/202609170002_supporting_entities.sql):
   date-specific schedules, optional profile/consent, events, callbacks, usage.
3. [202609170003_constraint_hardening.sql](../supabase/migrations/202609170003_constraint_hardening.sql):
   replace initial fee trigger with a concurrency-safe exclusion constraint;
   explicitly reset private-function grants (schema-local default privilege
   revocations alone do not remove global default EXECUTE grants).

All three were applied only to the selected Supabase Cloud development project.
The initial schema was empty. Two fictional clinics were seeded; a restricted
login was provisioned. Existing voice/SIP files and environment values were not
changed. Runtime credential creation is the only new secret artifact.

## Authorization matrix (deliberately restrictive)

| Principal | Allowed now | Denied now |
|---|---|---|
| `anon` | Nothing in clinic tables/private functions | All clinic data and routing |
| Active viewer/receptionist | Own clinic rows via RLS | All configuration writes, memberships, routing, publishing, requests |
| Active owner/manager | Own clinic reads; insert/update draft operational authoring tables | Cross-tenant writes, delete, role self-promotion, phone assignment, publishing, fee-history writes, session/PII/request writes |
| `clinic_runtime` | Private destination resolver; scoped published/superseded snapshots | Raw authoring/PII tables, memberships, phones, mutations, publishing |
| Migration administrator | Versioned setup and fictional seed | Not suitable for agent/dashboard runtime |

The limited authoring grants cover locations/doctors/services/weekly schedules/
exceptions/notices/FAQs. Later services will add authorized operations for staff
requests, publication, fee changes and membership management; they are not
silently enabled through generic CRUD. Recording is constrained off in this
foundation until an approved recording/retention workflow exists.

**Trust boundary:** runtime GUC scope is a backend convention plus RLS defense in
depth, not an unforgeable credential against a compromised SQL runtime login.
Anyone who holds that shared backend credential can set a different GUC or call
the exact-match resolver. Never expose it, accept arbitrary SQL, or accept a
caller/model-selected `ClinicScope`. Database exceptions require sanitized error
handling at the future ingress/tool boundary; do not log raw psycopg messages.

Keep `clinic_private` and `clinic_migrations` out of Supabase exposed API schemas.
Security-definer functions have empty fixed search paths and explicit EXECUTE
allowlists. The resolver is not executable by browser roles. No Storage buckets,
policies, grants or service-role secrets have been introduced.

## Verification evidence

- Full explicit DEV run: **58 tests passed**, including 29 real-database tests.
- Default offline run: **29 passed, 29 integration tests skipped** as intended.
- Ruff formatting/lint, strict mypy (7 new source/script modules), editor
  diagnostics, wheel/source build and `git diff --check` passed. The legacy
  voice file is intentionally outside the new formatter/type scope.
- No previously locked package versions changed; new database/tools were added
  without upgrading voice dependencies. Agent and tracked SIP dispatch are
  unchanged. Build archives exclude private environment files; the placeholder
  template has no populated secret fields. Runtime secret is ignored and mode 0600.
- Real Supabase `authenticated` role with temporary users/memberships: both
  directions of doctor isolation, all-table foreign-tenant read denial,
  cross-clinic schedule denial, role/inactive-membership denial and server checks.
- Real restricted login: resolution, wrong provider/trunk/unknown/inactive route
  denial, cross-clinic pinned-version denial, concurrent pool use, unscoped denial
  and rejection of the privileged migration connection.
- Composite foreign-key denial, duplicate active numbers, timezone validation,
  overlapping fee denial, append-only audits, immutable published content,
  superseded snapshot stability after publication and authoring edits, draft exclusion.
- Effective private function grants and anonymous table privileges checked in
  hosted PostgreSQL. Temporary auth/mutation fixtures roll back.
- Blank libpq query overrides and duplicate parameters rejected by unit tests;
  migration-drift/confirmation and accidental credential-rotation guards tested.

The isolation tests explicitly switch role before access assertions; schema
setup through the administrator is not counted as RLS evidence. Hosted Auth
HTTP/JWT validation and PostgREST/API tests remain Phase 4. No vector isolation
claim is made: vectors are not implemented.

## Remaining gates and safe rollback

Phase 2 must implement typed public snapshot validation, transactional
publish/preview/rollback, effective scheduling/precedence, normalization/ambiguity,
and controlled tools. Phase 3 must implement session orchestration, caller
confirmation, authenticated PII encryption/key rotation, consent, lifecycle,
sanitized telemetry/auditing, transfer and safety layers before granting runtime
mutations. Bytea columns alone are not an implemented encryption system; fictional
requests are explicitly anonymized rather than fake encrypted. Sample document
records are not scanned uploads, Storage objects or retrievable embeddings.

Phase 4 supplies verified Auth/RBAC endpoints, CSRF/session protection and UI.
Phase 5 adds private storage, reviewed ingestion and actual tenant-filtered
pgvector retrieval. Phase 6 covers retention/redaction, budgets, dependency
outages, concurrency load tests and the 50-call pilot. Call summaries/free-text
authoring need service-layer sanitization; no new runtime write grants exist yet.

No production deployment, worker integration, new live phone assignment, or
telephony end-to-end verification occurred. Keep clinic mode disabled until
those gates pass. Roll back deployment by keeping the existing agent detached
from the database; prefer a tested forward repair migration over dropping
tables/roles. Never delete migration history or edit checksums to force a run.
Disable the runtime login through authorized administration if credentials are
compromised, rotate deliberately, and test new connections before resuming.
Clinic-mode rollback later must fail closed, not route real clinic callers to
the generic business demo KB.