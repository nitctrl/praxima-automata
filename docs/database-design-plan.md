# Plan: Enterprise, multi-domain PostgreSQL schema for the voice-agent platform

Status: **approved plan, implementation not started.**

## Context

Today's schema (`supabase/migrations/2026091*.sql`, 9 files) is hard-wired to one domain:
tables are named `clinics`, `doctors`, `appointment_requests`, and the code is Supabase-bound
(`auth.users` FKs, PostgREST, `authenticated` role). The goal is a **vendor-neutral, enterprise-grade
PostgreSQL design** where the same platform serves clinics, real estate, and any other domain by
**configuration, not new tables or migrations**.

Current live agent path (verified): one query at call start reads the published snapshot from
`configuration_versions`; retrieval then runs in memory over reviewed document sections + live-update
notices. Its strengths must survive the redesign: immutable published snapshots, one-published-version
guarantee, composite `(tenant_id, id)` FKs, restricted read-only runtime role with `set_config`
scoping, encrypted PII, idempotency keys, append-only audit.

## Assumptions (veto any before building)

1. **Plain PostgreSQL 16+** (portable: RDS/Cloud SQL/self-hosted/Supabase). No dependency on `auth.*`.
   Own identity tables; auth (OIDC/JWT) happens in the API layer.
2. **Hierarchy:** Organization -> Workspace (business/branch/brand) -> Agent. Phone numbers bind to an agent.
3. **Hybrid domain model:** typed core tables for cross-domain concepts + one generic `entities` table
   (JSONB validated by per-domain JSON Schema) for domain data (doctor, property, project...).
4. **Greenfield:** existing data is fictional Clinic A test data, so design fresh and re-seed; no data migration.
5. Agent actions in scope: answer questions, capture leads/requests, callbacks, optional transfer.
   Requests are records for staff, never confirmed bookings (existing product rule).
6. **This plan = database design + migrations + docs + tests.** Rewiring the Python app (agent loader,
   dashboard that currently calls Supabase PostgREST) is a separate, later phase (see Phase 7).

## Design principles

- Tenant key on every tenant-owned row; composite FKs `(workspace_id, id)` so cross-tenant references are impossible at the constraint level, not just RLS.
- Time-ordered UUIDs (UUIDv7: native in PG18, else `pg_uuidv7` or app-generated) for index locality.
- `timestamptz` everywhere; validity windows as `tstzrange` with **exclusion constraints** (`btree_gist`) instead of Python overlap checks.
- Immutable publication: authoring rows are drafts; the agent reads only `agent_releases.snapshot`.
- Restrictive deletes; soft-delete + retention jobs; privacy erasure anonymizes, keeps audit.
- Expand/contract migrations only; never edit an applied migration.

## Schema map (schemas and tables)

**`iam`** — `users`, `identities` (OIDC subject links), `memberships` (user x org/workspace, role: owner/admin/manager/agent/viewer, status), `api_keys` (hashed, scoped, expiring), `platform_admins` (separate).

**`tenancy`** — `organizations` (slug, plan, status, data-region), `workspaces` (org FK, name, **industry**, timezone, default/supported languages, status, limits: max call seconds, monthly minutes).

**`agent`** — `agents` (workspace FK, persona/prompt template + version, voice config: STT/LLM/TTS provider+model+language, greeting/emergency/fallback messages, transfer policy), `phone_numbers` (E.164, provider, trusted trunk id, direction, status; unique active inbound binding -> agent), `agent_releases` (immutable `snapshot jsonb`, digest, version_no, status draft/published/superseded/archived, `schema_version`, published_by; partial unique index = one published per agent), `agent_tools` (which tools an agent may call, with JSON-schema args).

**`catalog`** (the multi-domain core)
- `entity_types` (workspace or platform-template scoped; key e.g. `doctor`, `property`; **versioned JSON Schema** for attributes; searchable-fields list; display template).
- `entities` (workspace_id, entity_type_id, `key`, `name`, `aliases text[]`, `attributes jsonb` validated against schema, status, `valid_during tstzrange`, `search tsvector`, created/updated). GIN on `attributes`, trigram on `name`/`aliases`, generated columns + btree for hot filters (e.g. price, bedrooms).
- `entity_relations` (from/to entity, relation type e.g. `doctor_offers_service`, `property_in_project`, with optional `attributes` such as fee, and `valid_during`; exclusion constraint prevents overlapping conflicting rows).
- `availability_rules` / `availability_exceptions` (entity or location scope, RRULE-style weekly rules + dated exceptions, tz-aware) — reusable for doctor hours and property viewing windows.
- `entity_type_templates` seed data: **clinic pack** (doctor, service, location, fee relation) and **real-estate pack** (property, project, developer, agent, location; price/BHK/area/possession attrs).

**`knowledge`** — `sources` (upload/url/connector), `documents` (title, category, lifecycle: processing/needs_review/published/rejected/archived, effective range), `document_versions` (checksum, storage_uri, extraction warnings, reviewer), `chunks` (version FK, heading, text, `embedding vector(N)` via **pgvector** with HNSW index, `tsvector` with GIN, `entity_id` optional link, model+dimension recorded), `announcements` (live updates: message, scope, `valid_during`, priority).

**`engage`** — `contacts` (encrypted name/phone via envelope encryption + key version; **HMAC lookup column** per workspace; consent + retention), `consents` (append-only), `conversations`/`calls` (provider call id unique, agent + release pinned, times, language, disposition, sanitized summary, cost, `is_test`, retention_until; **partitioned monthly by started_at**), `call_events` (append-only, partitioned), `work_items` (generic request/lead: `kind` = lead_inquiry / site_visit_request / appointment_request / callback_request, pipeline `stage`, `payload jsonb` validated per kind, encrypted PII refs, idempotency key unique per call, assignee, SLA due), `work_item_events` (status history), `tasks` (staff follow-ups).

**`billing`** — `usage_events` (partitioned; STT seconds, LLM tokens, TTS chars, telephony seconds, idempotent event key), `rate_cards` (versioned, currency), `usage_rollups` (daily per workspace, maintained by job).

**`audit`** — `audit_log` (append-only, partitioned by month; actor, action, resource, allow-listed change diff, correlation id; UPDATE/DELETE blocked by trigger and revoked grants; optional hash chain column for tamper evidence).

**`ops`** — `outbox_events` (transactional outbox for webhooks/CRM sync), `idempotency_keys`, `jobs` (retention, partition maintenance, rollups), `schema_migrations` (checksums).

### How the same schema serves two domains

| Concept | Clinic | Real estate |
| --- | --- | --- |
| Entity types | doctor, service, location | property, project, developer, agent |
| Relations | doctor_offers_service (fee attr) | property_in_project |
| Availability | doctor weekly hours | site-visit windows |
| Work items | appointment_request, callback_request | lead_inquiry, site_visit_request |
| Knowledge docs | doctor bios, policies | brochures, RERA/possession FAQs, pricing sheets |

## Security model

- **Roles (NOLOGIN group roles + per-service LOGIN roles):** `db_owner` (migrations only), `app_api` (dashboard/API), `app_agent_runtime` (read-only on `agent_releases`, write only via `SECURITY DEFINER` functions for calls/work_items/usage), `app_worker` (jobs), `app_readonly` (analytics/replica). No `BYPASSRLS`, no superuser at runtime.
- **RLS on every tenant table**, keyed on `current_setting('app.workspace_id')` (set with `SET LOCAL` by trusted backend code — already the project's pattern, and safe under PgBouncer transaction pooling). Force RLS on table owners too (`FORCE ROW LEVEL SECURITY`).
- `SECURITY DEFINER` functions pin `search_path = ''`, revoke `PUBLIC` execute, explicit grants.
- PII: application-side envelope encryption (existing `src/clinic/privacy.py` model), key version columns, HMAC lookup; recording off by default; erasure = null ciphertext + `pii_erased_at`.
- Audit for all config publish, membership, and PII-access actions.

## Performance and scale

- Partition `calls`, `call_events`, `usage_events`, `audit_log` by month (`pg_partman` or a job in `ops.jobs`); detach/archive by retention.
- Index policy: every FK indexed; `(workspace_id, <status>, created_at)` for work queues; partial indexes for open items; no speculative indexes (verify with `pg_stat_statements`).
- Hot voice path stays cheap: **one indexed read of the published release per call**, kept in memory (current design retained). Vector search runs in Postgres (pgvector) as an alternative to Qdrant; keep the lexical + semantic RRF logic in `src/clinic/documents.py`.
- Per-role `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, connection limits.
- Autovacuum tuning on high-churn tables; `fillfactor` on hot-update tables.

## Operations

- PITR + daily base backups, tested restores; documented RPO/RTO. HA (streaming replica, automatic failover), read replica for dashboards/analytics.
- PgBouncer (transaction mode) in front; verify no session-state dependence.
- Migrations: forward-only, transactional, checksummed; lint with `squawk` (no unsafe locks: `CREATE INDEX CONCURRENTLY`, `NOT VALID` then `VALIDATE`). Reuse/extend the existing runner `scripts/database.py` (drop its "empty public schema" and Supabase-project-ref assumptions).
- Monitoring: `pg_stat_statements`, replication lag, bloat, connection saturation, partition-creation alerts.

## Phases

0. **Decisions + ERD** — confirm assumptions; write `docs/database-design.md` with mermaid ERD and the domain-mapping table.
1. **Foundation** — extensions (`pgcrypto`, `btree_gist`, `pg_trgm`, `vector`), roles, schemas, helper functions (`touch_updated_at`, tenant-scope check), `iam` + `tenancy` tables, RLS scaffold.
2. **Domain-flex core** — `entity_types`, `entities`, `entity_relations`, availability tables, JSON-Schema validation (trigger using `pg_jsonschema` if available, else app-side validation + CHECK on shape), clinic and real-estate seed packs.
3. **Knowledge + releases** — documents/versions/chunks/announcements, `agent_releases`, `build_snapshot`/`preview`/`publish` SQL functions (port from migration 009, generalized to entity types), one-published-per-agent index.
4. **Engagement** — contacts/consents, partitioned calls/call_events, work_items + events, usage, audit, outbox.
5. **Security hardening** — RLS policies + grants per role, definer functions, append-only triggers, PII constraints.
6. **Performance + ops** — partition maintenance job, index review with `EXPLAIN`, backup/HA/pooling runbook.
7. **App adaptation (separate approval)** — new snapshot schema (`schema_version` 4) in `src/clinic/snapshot.py`; loader query in `src/clinic/agent_knowledge.py`; replace Supabase-PostgREST dashboard calls in `src/clinic/dashboard.py` with an API over `app_api`; rename `clinic` package/prompts to be domain-neutral.

## Files (to create / adapt)

- New: `db/migrations/0001..00NN_*.sql`, `db/seeds/{clinic,real_estate}.sql`, `docs/database-design.md`, `tests/db/` (isolation, constraints, partitions).
- Adapt: `scripts/database.py` (runner; remove Supabase-only guards), `tests/conftest.py`.
- Reuse patterns from: `src/clinic/db.py` (role check + `set_config` scoping), `src/clinic/snapshot.py` (immutable snapshot validation), `src/clinic/privacy.py` (PII encryption), `supabase/migrations/202609170004_publication.sql` and `..._0009_document_knowledge.sql` (preview/publish/digest pattern).

## Verification

1. Start local Postgres 16 with pgvector (`docker run pgvector/pgvector:pg16`) and apply all migrations from empty; re-apply must be a no-op (checksums).
2. `squawk` migration lint passes.
3. Isolation tests: two workspaces, two roles; assert cross-tenant SELECT/INSERT/UPDATE and FK references are denied; `app_agent_runtime` cannot read drafts or other tenants.
4. Constraint tests: overlapping validity ranges rejected, one published release per agent, idempotency keys, append-only audit rejects UPDATE/DELETE.
5. Domain tests: load the clinic pack and the real-estate pack; build a snapshot for each; confirm the same functions work for both.
6. Performance: `pgbench` scripts for the release-load and work-item queue queries; `EXPLAIN (ANALYZE, BUFFERS)` shows index use; partition pruning verified on calls queries.
7. Backup/restore drill on the local instance; PgBouncer transaction-mode smoke test.

## Open questions treated as defaults unless you object

- Vector search: pgvector in Postgres (default) vs keep Qdrant.
- Region/data-residency and HA target (single-region multi-AZ assumed).
- Whether staff auth stays on Supabase Auth (allowed via `identities` mapping) or moves to your own OIDC provider.
