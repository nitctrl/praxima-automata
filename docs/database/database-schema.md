# Database Schema: Praxima Automata

Status: **revised draft for sign-off** (2026-09-26). It supersedes the first draft of this
document, and `docs/database-design-plan.md` remains the planning history. SQL migrations are
implemented separately in `db/migrations/`. §15 lists what changed and why.

## 1. Purpose

This document defines the target, vendor-neutral PostgreSQL 16+ data model for the Praxima
Automata backend.

The platform serves many business domains through **domain packs** (configuration), not
domain-specific tables. The first two packs are clinic and real estate. Adding an industry
means adding a pack, with no new tables and no migrations.

This document describes entities, relationships, ownership and the rules the migrations
must enforce. The conceptual ERD (§8) is intentionally simpler than the SQL. §11 lists what
the migrations add on top of it.

## 2. Design goals

- Multi-tenant isolation at the `workspace` boundary, enforced by the database, not only by
  application code.
- Hierarchy: `Organization → Workspace → Agent → Phone Number`.
- Domain-neutral core tables. Domain data lives in versioned `entity_types` / `entities` /
  `entity_relations`, installed per workspace from a pack.
- Authoring data is draft until published. The voice agent reads **only** an immutable
  published release snapshot, and each call is pinned to the release it loaded at start.
- Knowledge is reviewed before publication.
- Work items are requests or leads for staff, never confirmed bookings or commitments.
- PII is encrypted per field, and every access to decrypted PII is audited.
- High-volume append-only tables are partitioned by month.

## 3. Tenancy model

**Shared database, shared schema, row-level isolation.** The tenant is the **workspace**.
Every workspace belongs to one **organization**.

Isolation is layered, so no single mistake exposes another tenant's data:

| Layer | Mechanism |
| --- | --- |
| Tenant key | Every tenant-owned row has `workspace_id NOT NULL` |
| Referential | Composite FKs `(workspace_id, parent_id) → parent(workspace_id, id)`, so a row cannot reference another workspace's row |
| Query | RLS **enabled and forced** on every tenant table, keyed on `current_setting('app.workspace_id')`, set with `SET LOCAL` by trusted backend code (safe with PgBouncer transaction pooling) |
| Ingress | The runtime resolves the tenant from the called number → agent → workspace. The API re-checks the path's workspace against the user's memberships on every request |
| Roles | Per-process DB roles (§11), with no `BYPASSRLS` and no superuser at runtime |
| Crypto | PII ciphertext is bound to workspace, resource and field (AEAD associated data), so copied ciphertext won't decrypt elsewhere |
| Capacity | Per-workspace call length, monthly minutes, rate limits and per-role `statement_timeout` |

Table classes:

| Class | `workspace_id` | Examples | Access |
| --- | --- | --- | --- |
| **Tenant** | `NOT NULL` | entities, documents, conversations, work items | app roles, RLS by workspace |
| **Organization-scoped** | nullable (null means organization-wide) | memberships, api_keys, audit_log | RLS by organization; never the target of a tenant composite FK |
| **Platform** | none | pack_versions, rate_cards, jobs, schema_migrations | read-only to app roles, or worker/owner only |

Rule: **a table in the tenant FK graph never has a nullable `workspace_id`.** Shared
templates (pack entity types, work item kinds) are **installed (copied) into each workspace**
rather than referenced across tenants.

Future options without a redesign: give a large customer a dedicated database with the
same migrations (hybrid tenancy), and route by `organizations.data_region` for data
residency.

## 4. Conventions

- **Schemas:** one Postgres schema per module (§5). A module's code may query only its own
  schema. Cross-module reads go through the owning module's interface or the release
  snapshot.
- **IDs:** `uuid` with UUIDv7 (time-ordered). Tenant tables also have `UNIQUE (workspace_id, id)`
  so they can be targets of composite FKs.
- **Types:** `text` with `CHECK` constraints for statuses and kinds (not `varchar(n)`);
  `text[]` for languages, aliases and scopes; `timestamptz` everywhere; validity windows as
  `tstzrange`; money as `numeric` plus an ISO-4217 `currency`; email as `citext`.
- **Authoring columns** (on every staff-editable table): `created_at`, `updated_at`,
  `created_by`, `updated_by`, `deleted_at` (soft delete), `row_version integer`
  (optimistic concurrency: stale writes get HTTP 409).
- **Publication status** on authoring rows that feed a release:
  `publication_status IN ('draft','published','archived')`. Only `published`, non-deleted
  rows inside their validity window enter a snapshot.
- **Append-only tables** (consents, call events, work item events, usage events, audit log):
  `UPDATE` and `DELETE` are revoked and blocked by trigger.
- **PII columns** end in `_ciphertext` (`bytea`), with a `pii_key_version` and, where lookup is
  needed, an `_hmac` column keyed per workspace. Erasure nulls the ciphertext and sets
  `pii_erased_at`.
- **Errors in tables** are stored as `error_code`, never free text (free text can leak PII).

## 5. Module and schema ownership

| Module = schema | Tables | Responsibility |
| --- | --- | --- |
| `iam` | `users`, `identities`, `memberships`, `api_keys`, `platform_admins` | Identity and authorization |
| `tenancy` | `organizations`, `workspaces`, `pack_versions` | Tenant hierarchy, workspace config, installed pack |
| `agents` | `agents`, `phone_numbers`, `agent_tools` | Agent configuration and ingress routing |
| `releases` | `agent_releases` | Build, preview, publish, rollback; immutable snapshots |
| `catalog` | `entity_types`, `entities`, `entity_relations`, `availability_rules`, `availability_exceptions` | Domain-neutral business data |
| `knowledge` | `sources`, `documents`, `document_versions`, `document_sections`, `chunks`, `faqs`, `announcements` | Reviewed knowledge and the retrieval corpus |
| `engagement` | `contacts`, `consents`, `conversations`, `call_events`, `work_item_kinds`, `work_items`, `work_item_events`, `tasks` | Caller interactions and staff follow-up |
| `billing` | `rate_cards`, `workspace_rate_overrides`, `usage_events`, `usage_rollups` | Metering and cost |
| `audit` | `audit_log` | Append-only security and business audit trail |
| `ops` | `outbox_events`, `idempotency_keys`, `jobs`, `schema_migrations` | Shared operational infrastructure |

## 6. Core relationships

```text
iam.users ─┬─ iam.identities
           ├─ iam.memberships ──── tenancy.organizations
           │                  └─── tenancy.workspaces (null = organization-wide)
           └─ iam.platform_admins

tenancy.pack_versions (platform)
        │ installed into
tenancy.organizations
    └── tenancy.workspaces (pack_key, pack_version)
            ├── agents.agents
            │     ├── agents.phone_numbers
            │     ├── agents.agent_tools
            │     └── releases.agent_releases ── source_release_id (rollback)
            ├── catalog.entity_types
            │     └── catalog.entities
            │           ├── catalog.entity_relations (from/to)
            │           └── catalog.availability_rules
            │                 └── catalog.availability_exceptions
            ├── knowledge.documents ── published_version_id
            │     └── knowledge.document_versions
            │           └── knowledge.document_sections
            │                 └── knowledge.chunks
            ├── knowledge.faqs
            ├── knowledge.announcements (optional entity scope)
            ├── engagement.contacts
            │     └── engagement.consents
            ├── engagement.conversations (agent, release, phone number, contact)
            │     └── engagement.call_events          [partitioned]
            ├── engagement.work_item_kinds
            │     └── engagement.work_items (conversation, contact, entity)
            │           ├── engagement.work_item_events
            │           └── engagement.tasks
            ├── billing.usage_events                 [partitioned]
            └── billing.usage_rollups
audit.audit_log (organization, optional workspace)  [partitioned]
```

## 7. Partitioning

Monthly range partitions apply to **`engagement.call_events`, `billing.usage_events` and
`audit.audit_log`** only.

PostgreSQL requires every primary key or unique constraint on a partitioned table to include
the partition column. So these tables use `PRIMARY KEY (id, <time column>)`, and **no foreign
key may point at them**.

**`engagement.conversations` is not partitioned.** It holds one row per call, which stays
manageable for years. Keeping it plain lets `work_items`, `call_events`, `usage_events` and
`consents` hold normal foreign keys to it, and keeps `UNIQUE (provider, provider_call_id)`
enforceable. Retention deletes or anonymizes old rows through a job. Revisit this only if
volume demands it, and then drop inbound foreign keys consciously.

## 8. DBML / dbdiagram.io model (conceptual ERD)

Schema-qualified names work in dbdiagram.io. `Ref`s are shown as simple foreign keys; the
migrations implement tenant tables as composite `(workspace_id, …)` foreign keys (§11).
Authoring columns (§4) are written out on each table that has them.

```dbml
///////////////////////////////////////////////////////
// IAM
///////////////////////////////////////////////////////

Table iam.users {
  id uuid [pk]
  email citext [not null, unique]
  display_name text
  status text [not null, note: 'CHECK active | disabled']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
}

Table iam.identities {
  id uuid [pk]
  user_id uuid [not null]
  provider text [not null, note: 'oidc issuer or supabase']
  provider_subject text [not null]
  last_login_at timestamptz
  created_at timestamptz [not null]

  indexes {
    (provider, provider_subject) [unique]
    user_id
  }
}

Table iam.memberships {
  id uuid [pk]
  user_id uuid [not null]
  organization_id uuid [not null]
  workspace_id uuid [note: 'null = organization-wide']
  role text [not null, note: 'CHECK owner | admin | manager | staff | viewer']
  status text [not null, note: 'CHECK invited | active | suspended | revoked']
  created_by uuid
  created_at timestamptz [not null]
  updated_at timestamptz [not null]

  indexes {
    (user_id, organization_id, workspace_id) [unique, note: 'NULLS NOT DISTINCT']
    organization_id
    workspace_id
  }
}

Table iam.api_keys {
  id uuid [pk]
  organization_id uuid [not null]
  workspace_id uuid [note: 'null = organization-wide key']
  name text [not null]
  key_prefix text [not null, note: 'shown in UI to identify the key']
  key_hash text [not null, unique]
  scopes text[] [not null]
  expires_at timestamptz
  last_used_at timestamptz
  revoked_at timestamptz
  created_by uuid
  created_at timestamptz [not null]
}

Table iam.platform_admins {
  user_id uuid [pk]
  granted_by uuid
  created_at timestamptz [not null]
}

///////////////////////////////////////////////////////
// TENANCY
///////////////////////////////////////////////////////

Table tenancy.organizations {
  id uuid [pk]
  slug text [not null, unique]
  name text [not null]
  plan text
  status text [not null, note: 'CHECK active | suspended | closed']
  data_region text
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  deleted_at timestamptz
}

Table tenancy.pack_versions {
  pack_key text [not null, note: 'clinic | real_estate | ...']
  version text [not null, note: 'semver']
  manifest jsonb [not null, note: 'validated pack manifest']
  checksum text [not null]
  status text [not null, note: 'CHECK available | deprecated | withdrawn']
  released_at timestamptz [not null]

  indexes {
    (pack_key, version) [pk]
  }
  Note: 'Platform table (no tenant). Read-only to app roles.'
}

Table tenancy.workspaces {
  id uuid [pk]
  organization_id uuid [not null]
  slug text [not null]
  name text [not null]
  industry text [not null]
  pack_key text [not null]
  pack_version text [not null]
  timezone text [not null]
  default_language text [not null]
  supported_languages text[] [not null, note: 'CHECK default_language = ANY(supported_languages)']
  status text [not null, note: 'CHECK active | suspended | archived']
  max_call_seconds integer
  monthly_minutes_limit integer
  recording_enabled boolean [not null, default: false]
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  indexes {
    (organization_id, slug) [unique]
    (organization_id, id) [unique]
  }
}

///////////////////////////////////////////////////////
// AGENTS
///////////////////////////////////////////////////////

Table agents.agents {
  id uuid [pk]
  workspace_id uuid [not null]
  name text [not null]
  slug text [not null]
  status text [not null, note: 'CHECK active | disabled']
  persona text
  prompt_template_key text [note: 'template from the installed pack']
  prompt_version integer [not null]
  stt_provider text
  stt_model text
  stt_language text
  llm_provider text
  llm_model text
  tts_provider text
  tts_model text
  tts_language text
  greeting_message text [not null]
  emergency_message text [not null]
  fallback_message text [not null]
  transfer_enabled boolean [not null, default: false]
  transfer_policy jsonb
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, slug) [unique]
    (workspace_id, id) [unique]
  }
}

Table agents.phone_numbers {
  id uuid [pk]
  workspace_id uuid [not null]
  agent_id uuid [not null]
  phone_number text [not null, note: "CHECK E.164: '^\\+[1-9][0-9]{7,14}$'"]
  provider text [not null]
  trusted_trunk_id text
  direction text [not null, note: 'CHECK inbound | outbound']
  status text [not null, note: 'CHECK active | inactive | released']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]

  indexes {
    phone_number [unique, note: "partial: WHERE status = 'active'"]
    (workspace_id, id) [unique]
    agent_id
  }
}

Table agents.agent_tools {
  id uuid [pk]
  workspace_id uuid [not null]
  agent_id uuid [not null]
  tool_key text [not null, note: 'must exist in the code tool registry and the pack']
  enabled boolean [not null]
  config jsonb [note: 'per-agent settings; argument schemas live in code']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  updated_by uuid

  indexes {
    (agent_id, tool_key) [unique]
  }
}

///////////////////////////////////////////////////////
// RELEASES
///////////////////////////////////////////////////////

Table releases.agent_releases {
  id uuid [pk]
  workspace_id uuid [not null]
  agent_id uuid [not null]
  version_no integer [not null]
  schema_version integer [not null]
  pack_key text [not null]
  pack_version text [not null]
  prompt_version integer [not null]
  status text [not null, note: 'CHECK draft | published | superseded | archived']
  snapshot jsonb [not null, note: 'immutable once published (trigger)']
  digest text [not null, note: 'sha-256 of canonical snapshot']
  source_release_id uuid [note: 'set when this release is a rollback']
  created_by uuid
  published_by uuid
  published_at timestamptz
  created_at timestamptz [not null]

  indexes {
    (agent_id, version_no) [unique]
    agent_id [unique, note: "partial: WHERE status = 'published' (one live release per agent)"]
    (workspace_id, id) [unique]
  }
}

///////////////////////////////////////////////////////
// CATALOG
///////////////////////////////////////////////////////

Table catalog.entity_types {
  id uuid [pk]
  workspace_id uuid [not null, note: 'always tenant-owned; installed from the pack']
  key text [not null, note: 'doctor | service | property | ...']
  name text [not null]
  description text
  schema_version integer [not null]
  attributes_schema jsonb [not null, note: 'JSON Schema']
  searchable_fields text[]
  display_template jsonb
  source text [not null, note: 'CHECK pack | custom']
  source_pack_key text
  source_pack_version text
  status text [not null, note: 'CHECK active | deprecated']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, key, schema_version) [unique]
    (workspace_id, id) [unique]
  }
}

Table catalog.entities {
  id uuid [pk]
  workspace_id uuid [not null]
  entity_type_id uuid [not null]
  key text [not null]
  name text [not null]
  aliases text[]
  attributes jsonb [not null, note: 'validated against entity_types.attributes_schema']
  publication_status text [not null, note: 'CHECK draft | published | archived']
  valid_during tstzrange
  search_vector tsvector
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, key) [unique, note: 'partial: WHERE deleted_at IS NULL']
    (workspace_id, id) [unique]
    (workspace_id, entity_type_id, publication_status)
  }
}

Table catalog.entity_relations {
  id uuid [pk]
  workspace_id uuid [not null]
  from_entity_id uuid [not null]
  to_entity_id uuid [not null]
  relation_type text [not null, note: 'declared by the pack']
  attributes jsonb
  publication_status text [not null, note: 'CHECK draft | published | archived']
  valid_during tstzrange
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, from_entity_id, relation_type)
    (workspace_id, to_entity_id)
  }
  Note: 'EXCLUDE overlapping valid_during for same (workspace, from, to, relation_type)'
}

Table catalog.availability_rules {
  id uuid [pk]
  workspace_id uuid [not null]
  entity_id uuid
  location_entity_id uuid
  timezone text [not null]
  rrule text [not null, note: 'RFC 5545 recurrence (days)']
  start_time time [not null]
  end_time time [not null, note: 'CHECK end_time > start_time']
  valid_during tstzrange
  publication_status text [not null, note: 'CHECK draft | published | archived']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  Note: 'CHECK entity_id IS NOT NULL OR location_entity_id IS NOT NULL'
}

Table catalog.availability_exceptions {
  id uuid [pk]
  workspace_id uuid [not null]
  availability_rule_id uuid
  entity_id uuid
  location_entity_id uuid
  timezone text [not null]
  exception_date date [not null]
  is_available boolean [not null, note: 'false = closed; true = special hours']
  start_time time
  end_time time
  public_message text
  publication_status text [not null, note: 'CHECK draft | published | archived']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  Note: 'CHECK at least one of availability_rule_id, entity_id, location_entity_id'
}

///////////////////////////////////////////////////////
// KNOWLEDGE
///////////////////////////////////////////////////////

Table knowledge.sources {
  id uuid [pk]
  workspace_id uuid [not null]
  source_type text [not null, note: 'CHECK upload | url | connector']
  name text
  uri text
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  deleted_at timestamptz
}

Table knowledge.documents {
  id uuid [pk]
  workspace_id uuid [not null]
  source_id uuid
  title text [not null]
  category text [note: 'categories declared by the pack']
  status text [not null, note: 'CHECK active | archived']
  published_version_id uuid [note: 'the version callers hear; null = not live']
  effective_during tstzrange
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, id) [unique]
  }
}

Table knowledge.document_versions {
  id uuid [pk]
  workspace_id uuid [not null]
  document_id uuid [not null]
  version_no integer [not null]
  status text [not null, note: 'CHECK processing | needs_review | published | superseded | rejected | archived']
  title text [not null]
  original_filename text [not null]
  mime_type text [not null]
  checksum text [not null, note: 'sha-256 of the upload']
  storage_uri text [note: 'nullable: uploads are not retained by default']
  extraction_warnings text[]
  uploaded_by uuid
  reviewed_by uuid
  reviewed_at timestamptz
  published_by uuid
  published_at timestamptz
  created_at timestamptz [not null]

  indexes {
    (document_id, version_no) [unique]
    (workspace_id, checksum) [unique, note: 'duplicate upload detection']
    (workspace_id, id) [unique]
  }
}

Table knowledge.document_sections {
  id uuid [pk]
  workspace_id uuid [not null]
  document_version_id uuid [not null]
  position integer [not null]
  heading text [note: 'CHECK length <= 200']
  text text [not null, note: 'staff-reviewed wording; CHECK length 1..4000']
  entity_id uuid [note: 'optional link, e.g. a doctor bio -> doctor']
  keywords text[]
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  updated_by uuid

  indexes {
    (document_version_id, position) [unique]
    (workspace_id, id) [unique]
  }
}

Table knowledge.chunks {
  id uuid [pk]
  workspace_id uuid [not null]
  document_version_id uuid [not null]
  section_id uuid [not null]
  chunk_index integer [not null]
  heading text
  text text [not null]
  embedding "vector(384)" [note: 'dimension fixed per model; see open decisions']
  embedding_model text
  search_vector tsvector
  entity_id uuid
  created_at timestamptz [not null]

  indexes {
    (document_version_id, chunk_index) [unique]
  }
  Note: 'Derived index data, rebuilt from sections. HNSW on embedding, GIN on search_vector.'
}

Table knowledge.faqs {
  id uuid [pk]
  workspace_id uuid [not null]
  category text
  canonical_question text [not null]
  alternative_phrasings text[]
  approved_answer text [not null]
  entity_id uuid
  publication_status text [not null, note: 'CHECK draft | published | archived']
  valid_during tstzrange
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]
}

Table knowledge.announcements {
  id uuid [pk]
  workspace_id uuid [not null]
  kind text [not null, note: 'information | closure | ...; kinds declared by the pack']
  public_message text [not null, note: 'CHECK length 1..2000']
  internal_note text
  entity_id uuid [note: 'optional scope, e.g. one doctor or one project']
  location_entity_id uuid
  priority integer [not null, default: 100]
  valid_during tstzrange [not null]
  publication_status text [not null, note: 'CHECK draft | published | archived']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, publication_status, valid_during)
  }
}

///////////////////////////////////////////////////////
// ENGAGEMENT
///////////////////////////////////////////////////////

Table engagement.contacts {
  id uuid [pk]
  workspace_id uuid [not null]
  display_name_ciphertext bytea
  phone_ciphertext bytea
  phone_lookup_hmac text [note: 'HMAC keyed per workspace']
  pii_key_version integer
  preferred_language text
  consent_status text [note: 'CHECK unknown | granted | withdrawn']
  consented_at timestamptz
  last_confirmed_at timestamptz
  retention_until timestamptz
  pii_erased_at timestamptz
  created_at timestamptz [not null]
  updated_at timestamptz [not null]

  indexes {
    (workspace_id, phone_lookup_hmac) [unique]
    (workspace_id, id) [unique]
  }
}

Table engagement.consents {
  id uuid [pk]
  workspace_id uuid [not null]
  contact_id uuid
  conversation_id uuid
  consent_type text [not null, note: 'e.g. profile_reuse | callback | recording']
  action text [not null, note: 'CHECK granted | withdrawn']
  notice_version text [not null, note: 'which notice wording the caller heard']
  source text [note: 'voice | dashboard | api']
  recorded_at timestamptz [not null]
  metadata jsonb [note: 'no PII']

  Note: 'Append-only.'
}

Table engagement.conversations {
  id uuid [pk]
  workspace_id uuid [not null]
  agent_id uuid [not null]
  agent_release_id uuid [note: 'null only if the call failed before a release loaded']
  phone_number_id uuid
  contact_id uuid
  channel text [not null, note: 'CHECK voice (future: chat, whatsapp)']
  provider text [not null]
  provider_call_id text [not null]
  provider_room_id text
  caller_number_ciphertext bytea
  pii_key_version integer
  status text [not null, note: 'CHECK active | completed | failed | abandoned']
  started_at timestamptz [not null]
  answered_at timestamptz
  ended_at timestamptz
  deadline_at timestamptz [note: 'max call end; used by stale-session cleanup']
  heartbeat_at timestamptz [note: 'last runtime liveness signal']
  duration_seconds integer [note: 'generated from started_at/ended_at']
  language text
  detected_languages text[]
  primary_intent text
  disposition text
  transfer_attempted boolean [not null, default: false]
  transfer_succeeded boolean [not null, default: false]
  safety_flag boolean [not null, default: false]
  failure_code text
  summary text [note: 'sanitized administrative summary; no PII']
  estimated_cost numeric(14,4)
  currency text
  contact_reuse_consented boolean [not null, default: false]
  is_test boolean [not null]
  retention_until timestamptz
  created_at timestamptz [not null]

  indexes {
    (provider, provider_call_id) [unique]
    (workspace_id, id) [unique]
    (workspace_id, started_at)
    (workspace_id, started_at) [note: 'partial: WHERE failure_code IS NOT NULL AND NOT is_test']
    heartbeat_at [note: "partial: WHERE status = 'active'"]
  }
  Note: 'NOT partitioned (see §7).'
}

Table engagement.call_events {
  id uuid [not null]
  occurred_at timestamptz [not null]
  workspace_id uuid [not null]
  conversation_id uuid [not null]
  event_key text [not null, note: 'idempotency within the conversation']
  event_type text [not null, note: 'CHECK started | ended | tool_failed | transfer_failed | safety_routed | timeout | ...']
  sanitized_payload jsonb [note: 'no PII, prompts or transcripts']

  indexes {
    (id, occurred_at) [pk]
    (conversation_id, event_key, occurred_at) [unique]
  }
  Note: 'Partitioned monthly by occurred_at. Append-only.'
}

Table engagement.work_item_kinds {
  id uuid [pk]
  workspace_id uuid [not null, note: 'installed from the pack']
  key text [not null, note: 'appointment_request | lead_inquiry | callback_request | ...']
  name text [not null]
  schema_version integer [not null]
  payload_schema jsonb [not null, note: 'JSON Schema for the non-PII payload']
  stages text[] [not null]
  initial_stage text [not null]
  terminal_stages text[] [not null]
  source_pack_key text
  source_pack_version text
  status text [not null, note: 'CHECK active | deprecated']
  created_at timestamptz [not null]
  updated_at timestamptz [not null]

  indexes {
    (workspace_id, key, schema_version) [unique]
    (workspace_id, id) [unique]
  }
}

Table engagement.work_items {
  id uuid [pk]
  workspace_id uuid [not null]
  kind_id uuid [not null]
  conversation_id uuid
  contact_id uuid
  entity_id uuid [note: 'primary subject, e.g. doctor or property']
  stage text [not null, note: 'must be in work_item_kinds.stages']
  payload jsonb [not null, note: 'non-PII fields, validated by kind']
  subject_name_ciphertext bytea [note: 'person the request is for (may differ from caller)']
  callback_number_ciphertext bytea
  staff_note_ciphertext bytea
  pii_key_version integer
  pii_erased_at timestamptz
  assignee_user_id uuid
  idempotency_key text [not null]
  sla_due_at timestamptz
  retention_until timestamptz
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  updated_by uuid
  row_version integer [not null, default: 1]

  indexes {
    (workspace_id, idempotency_key) [unique]
    (workspace_id, id) [unique]
    (workspace_id, stage, created_at)
    (workspace_id, entity_id)
  }
}

Table engagement.work_item_events {
  id uuid [pk]
  workspace_id uuid [not null]
  work_item_id uuid [not null]
  from_stage text
  to_stage text
  actor_type text [not null, note: 'CHECK user | runtime | system']
  actor_user_id uuid
  occurred_at timestamptz [not null]
  metadata jsonb [note: 'no PII']

  Note: 'Append-only.'
}

Table engagement.tasks {
  id uuid [pk]
  workspace_id uuid [not null]
  work_item_id uuid
  assignee_user_id uuid
  title text [not null]
  description text
  status text [not null, note: 'CHECK open | done | cancelled']
  due_at timestamptz
  completed_at timestamptz
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  created_by uuid
  updated_by uuid
  deleted_at timestamptz
  row_version integer [not null, default: 1]
}

///////////////////////////////////////////////////////
// BILLING
///////////////////////////////////////////////////////

Table billing.rate_cards {
  id uuid [pk]
  version_no integer [not null, unique]
  currency text [not null]
  stt_rate numeric(14,6)
  llm_input_rate numeric(14,6)
  llm_output_rate numeric(14,6)
  tts_rate numeric(14,6)
  telephony_rate numeric(14,6)
  valid_during tstzrange [not null]
  created_at timestamptz [not null]

  Note: 'Platform defaults (no tenant). EXCLUDE overlapping valid_during per currency.'
}

Table billing.workspace_rate_overrides {
  id uuid [pk]
  workspace_id uuid [not null]
  currency text [not null]
  stt_rate numeric(14,6)
  llm_input_rate numeric(14,6)
  llm_output_rate numeric(14,6)
  tts_rate numeric(14,6)
  telephony_rate numeric(14,6)
  valid_during tstzrange [not null]
  created_by uuid
  created_at timestamptz [not null]
}

Table billing.usage_events {
  id uuid [not null]
  occurred_at timestamptz [not null]
  workspace_id uuid [not null]
  agent_id uuid
  conversation_id uuid
  event_key text [not null]
  stt_seconds integer
  llm_input_tokens bigint
  llm_output_tokens bigint
  tts_characters integer
  telephony_seconds integer
  stt_cost numeric(14,6)
  llm_cost numeric(14,6)
  tts_cost numeric(14,6)
  telephony_cost numeric(14,6)
  currency text
  rate_card_id uuid
  rate_override_id uuid
  metadata jsonb [note: 'no PII']

  indexes {
    (id, occurred_at) [pk]
    (workspace_id, event_key, occurred_at) [unique]
  }
  Note: 'Partitioned monthly by occurred_at. Append-only.'
}

Table billing.usage_rollups {
  id uuid [pk]
  workspace_id uuid [not null]
  agent_id uuid [not null]
  usage_date date [not null]
  currency text [not null]
  stt_seconds bigint [not null]
  llm_input_tokens bigint [not null]
  llm_output_tokens bigint [not null]
  tts_characters bigint [not null]
  telephony_seconds bigint [not null]
  total_cost numeric(18,6) [not null]
  created_at timestamptz [not null]
  updated_at timestamptz [not null]

  indexes {
    (workspace_id, agent_id, usage_date, currency) [unique]
  }
}

///////////////////////////////////////////////////////
// AUDIT
///////////////////////////////////////////////////////

Table audit.audit_log {
  id uuid [not null]
  occurred_at timestamptz [not null]
  organization_id uuid [not null]
  workspace_id uuid
  actor_type text [not null, note: 'CHECK user | api_key | system | runtime']
  actor_id uuid
  action text [not null, note: 'e.g. release.publish | pii.reveal | membership.update']
  resource_type text [not null]
  resource_id uuid
  outcome text [not null, note: 'CHECK success | denied | failed']
  change_diff jsonb [note: 'allow-listed fields only; never PII']
  correlation_id text
  hash_chain text [note: 'optional tamper evidence']

  indexes {
    (id, occurred_at) [pk]
    (organization_id, occurred_at)
    (workspace_id, resource_type, resource_id)
  }
  Note: 'Partitioned monthly by occurred_at. Append-only.'
}

///////////////////////////////////////////////////////
// OPS
///////////////////////////////////////////////////////

Table ops.outbox_events {
  id uuid [pk]
  workspace_id uuid
  event_type text [not null]
  aggregate_type text
  aggregate_id uuid
  payload jsonb [not null]
  status text [not null, note: 'CHECK pending | published | failed']
  attempts integer [not null, default: 0]
  next_attempt_at timestamptz
  locked_until timestamptz
  last_error_code text
  published_at timestamptz
  created_at timestamptz [not null]

  indexes {
    (status, next_attempt_at)
  }
}

Table ops.idempotency_keys {
  id uuid [pk]
  workspace_id uuid
  scope text [not null, note: 'endpoint or operation name']
  key text [not null]
  request_hash text [not null]
  response_status integer
  response jsonb
  created_at timestamptz [not null]
  expires_at timestamptz [not null]

  indexes {
    (workspace_id, scope, key) [unique, note: 'NULLS NOT DISTINCT']
  }
}

Table ops.jobs {
  id uuid [pk]
  job_type text [not null]
  status text [not null, note: 'CHECK queued | running | succeeded | failed | cancelled']
  payload jsonb [note: 'no PII']
  attempts integer [not null, default: 0]
  max_attempts integer [not null, default: 5]
  scheduled_at timestamptz [not null]
  next_attempt_at timestamptz
  locked_by text
  locked_until timestamptz [note: 'lease for SELECT ... FOR UPDATE SKIP LOCKED workers']
  started_at timestamptz
  completed_at timestamptz
  error_code text
  created_at timestamptz [not null]

  indexes {
    (status, next_attempt_at)
  }
}

Table ops.schema_migrations {
  version text [pk]
  checksum text [not null]
  applied_at timestamptz [not null]
}

///////////////////////////////////////////////////////
// RELATIONSHIPS
///////////////////////////////////////////////////////

Ref: iam.identities.user_id > iam.users.id
Ref: iam.memberships.user_id > iam.users.id
Ref: iam.memberships.organization_id > tenancy.organizations.id
Ref: iam.memberships.workspace_id > tenancy.workspaces.id
Ref: iam.api_keys.organization_id > tenancy.organizations.id
Ref: iam.api_keys.workspace_id > tenancy.workspaces.id
Ref: iam.platform_admins.user_id > iam.users.id

Ref: tenancy.workspaces.organization_id > tenancy.organizations.id
Ref: tenancy.workspaces.(pack_key, pack_version) > tenancy.pack_versions.(pack_key, version)

Ref: agents.agents.workspace_id > tenancy.workspaces.id
Ref: agents.phone_numbers.agent_id > agents.agents.id
Ref: agents.agent_tools.agent_id > agents.agents.id

Ref: releases.agent_releases.agent_id > agents.agents.id
Ref: releases.agent_releases.source_release_id > releases.agent_releases.id
Ref: releases.agent_releases.published_by > iam.users.id

Ref: catalog.entity_types.workspace_id > tenancy.workspaces.id
Ref: catalog.entities.entity_type_id > catalog.entity_types.id
Ref: catalog.entity_relations.from_entity_id > catalog.entities.id
Ref: catalog.entity_relations.to_entity_id > catalog.entities.id
Ref: catalog.availability_rules.entity_id > catalog.entities.id
Ref: catalog.availability_rules.location_entity_id > catalog.entities.id
Ref: catalog.availability_exceptions.availability_rule_id > catalog.availability_rules.id
Ref: catalog.availability_exceptions.entity_id > catalog.entities.id
Ref: catalog.availability_exceptions.location_entity_id > catalog.entities.id

Ref: knowledge.sources.workspace_id > tenancy.workspaces.id
Ref: knowledge.documents.source_id > knowledge.sources.id
Ref: knowledge.documents.published_version_id - knowledge.document_versions.id
Ref: knowledge.document_versions.document_id > knowledge.documents.id
Ref: knowledge.document_sections.document_version_id > knowledge.document_versions.id
Ref: knowledge.document_sections.entity_id > catalog.entities.id
Ref: knowledge.chunks.document_version_id > knowledge.document_versions.id
Ref: knowledge.chunks.section_id > knowledge.document_sections.id
Ref: knowledge.chunks.entity_id > catalog.entities.id
Ref: knowledge.faqs.entity_id > catalog.entities.id
Ref: knowledge.announcements.entity_id > catalog.entities.id
Ref: knowledge.announcements.location_entity_id > catalog.entities.id

Ref: engagement.contacts.workspace_id > tenancy.workspaces.id
Ref: engagement.consents.contact_id > engagement.contacts.id
Ref: engagement.consents.conversation_id > engagement.conversations.id
Ref: engagement.conversations.agent_id > agents.agents.id
Ref: engagement.conversations.agent_release_id > releases.agent_releases.id
Ref: engagement.conversations.phone_number_id > agents.phone_numbers.id
Ref: engagement.conversations.contact_id > engagement.contacts.id
Ref: engagement.call_events.conversation_id > engagement.conversations.id
Ref: engagement.work_item_kinds.workspace_id > tenancy.workspaces.id
Ref: engagement.work_items.kind_id > engagement.work_item_kinds.id
Ref: engagement.work_items.conversation_id > engagement.conversations.id
Ref: engagement.work_items.contact_id > engagement.contacts.id
Ref: engagement.work_items.entity_id > catalog.entities.id
Ref: engagement.work_items.assignee_user_id > iam.users.id
Ref: engagement.work_item_events.work_item_id > engagement.work_items.id
Ref: engagement.work_item_events.actor_user_id > iam.users.id
Ref: engagement.tasks.work_item_id > engagement.work_items.id
Ref: engagement.tasks.assignee_user_id > iam.users.id

Ref: billing.workspace_rate_overrides.workspace_id > tenancy.workspaces.id
Ref: billing.usage_events.conversation_id > engagement.conversations.id
Ref: billing.usage_events.agent_id > agents.agents.id
Ref: billing.usage_events.rate_card_id > billing.rate_cards.id
Ref: billing.usage_events.rate_override_id > billing.workspace_rate_overrides.id
Ref: billing.usage_rollups.agent_id > agents.agents.id

Ref: audit.audit_log.organization_id > tenancy.organizations.id
Ref: audit.audit_log.workspace_id > tenancy.workspaces.id
```

Every tenant table also references `tenancy.workspaces` through `workspace_id`. Those
references are omitted above for readability wherever a composite FK through the parent
already implies them.

Cross-module FKs (for example `engagement.work_items.entity_id → catalog.entities`) are
allowed at the database level for integrity. Application code still reads the other
module's data only through that module's interface.

## 9. Domain examples

### 9.1 Clinic pack

Installed entity types are `doctor`, `service` and `location`. Work item kinds are
`appointment_request` and `callback_request`. There are no `doctors`, `services` or
`doctor_services` tables.

```json
{ "type": "doctor", "key": "dr-sharma", "name": "Dr. Sharma",
  "attributes": { "specialization": "Cardiology", "qualification": "MBBS, MD", "experience_years": 15 } }
```

```json
{ "relation_type": "doctor_offers_service", "from": "dr-sharma", "to": "cardiology-consultation",
  "attributes": { "fee": 1200, "currency": "INR" } }
```

An `appointment_request` work item stores the preferred date and time window in `payload`,
the patient's name in `subject_name_ciphertext`, the callback number in
`callback_number_ciphertext`, and the doctor in `entity_id`. Its stages are `new → contacted →
confirmed_externally → closed | cancelled`.

### 9.2 Real-estate pack

Installed entity types are `property`, `project`, `developer`, `sales_agent` and `location`.
Work item kinds are `lead_inquiry`, `site_visit_request` and `callback_request`.

```json
{ "type": "property", "key": "flat-a-1203", "name": "Flat A-1203",
  "attributes": { "bhk": 3, "area_sqft": 1850, "price": 12500000, "possession_date": "2027-06-30", "parking": 2 } }
```

```json
{ "relation_type": "property_in_project", "from": "flat-a-1203", "to": "palm-residency" }
```

Frequent attribute filters (price, BHK) get generated columns plus btree indexes in the
migrations. There are no domain columns in core tables.

## 10. Release snapshot

The release is the runtime boundary between editable authoring data and the live agent. It
contains everything the runtime needs for a call, so nothing else is read mid-call.

```json
{
  "schema_version": 4,
  "release": { "id": "…", "version_no": 12, "digest": "sha256:…", "published_at": "…" },
  "pack": { "key": "clinic", "version": "1.2.0" },
  "workspace": { "name": "Acme Healthcare", "timezone": "Asia/Kolkata",
                 "default_language": "hi-IN", "supported_languages": ["hi-IN", "en-IN"] },
  "agent": { "name": "Receptionist", "prompt_template_key": "receptionist", "prompt_version": 3,
             "greeting": "Namaste, how may I help you?",
             "emergency_message": "…", "fallback_message": "…",
             "transfer": { "enabled": false } },
  "policy": { "prohibited_topics": ["diagnosis", "prescription"],
              "emergency_triggers": ["…"], "refusal_message": "…" },
  "tools": [ { "key": "find_entities" }, { "key": "search_knowledge" },
             { "key": "create_work_item", "config": { "kinds": ["appointment_request", "callback_request"] } } ],
  "entity_types": [ { "key": "doctor", "schema_version": 1, "searchable_fields": ["name", "specialization"] } ],
  "entities": [ { "type": "doctor", "key": "dr-sharma", "name": "Dr. Sharma", "attributes": {} } ],
  "relations": [],
  "availability": { "rules": [], "exceptions": [] },
  "work_item_kinds": [ { "key": "appointment_request", "schema_version": 1, "payload_schema": {} } ],
  "faqs": [],
  "knowledge_sections": [ { "document_title": "About us", "heading": "…", "text": "…" } ],
  "announcements": [ { "kind": "closure", "message": "…", "valid_during": ["…", "…"], "priority": 100 } ]
}
```

- Built only from `publication_status = 'published'`, non-deleted rows valid at build time,
  plus the `published_version_id` sections of active documents. Announcements and availability
  keep their validity windows, and the runtime applies them against the call's clock.
- Validated in SQL (build function) **and** in Pydantic. The runtime refuses any
  `schema_version` it doesn't support.
- Publish is digest-checked. It publishes only the previewed digest, and only if the live
  release hasn't changed since preview (optimistic concurrency). Rollback publishes a new
  release with `source_release_id` set.

Runtime flow:

```text
Incoming call → phone number (active) → agent → published release (one indexed read)
→ snapshot held in memory for the whole call → retrieval + tools in memory
→ conversation / work items / events written through SECURITY DEFINER functions
```

## 11. Implementation constraints (migrations)

The migrations must additionally enforce:

1. Composite tenant FKs `(workspace_id, x_id) → parent(workspace_id, id)` on every tenant
   table, with `UNIQUE (workspace_id, id)` on each parent.
2. RLS enabled **and forced** on every tenant table (by `app.workspace_id`) and every
   organization-scoped table (by `app.organization_id`).
3. `SET LOCAL app.workspace_id` / `app.organization_id` only in trusted backend code, per
   transaction.
4. One published release per agent (partial unique index). Published snapshots are
   immutable (trigger).
5. `tstzrange` validity with exclusion constraints (`btree_gist`) where overlaps are invalid.
6. Append-only tables: revoke `UPDATE`/`DELETE` and block them by trigger.
7. PII only as ciphertext plus key version. HMAC lookup keyed per workspace. No PII in `jsonb`
   payloads, metadata or events.
8. Monthly partitions for `call_events`, `usage_events` and `audit_log`, with
   `PRIMARY KEY (id, time)`, created ahead of time by a job, and no inbound FKs.
9. UUIDv7 identifiers.
10. JSON Schema validation of entity attributes and work item payloads in the application
    (Pydantic plus the stored schema). Add a DB-side check with `pg_jsonschema` only if it's
    available.
11. Work item `stage` validated against its kind's `stages` (trigger or definer function).
12. `CHECK` constraints for every status and kind column. E.164 check on phone numbers.
13. Database roles:

| Role | Purpose | Access |
| --- | --- | --- |
| `db_owner` | migrations only | owns schemas; never used by apps |
| `app_api` | dashboard and management API | RLS-scoped DML on authoring tables; publish via definer functions |
| `app_agent_runtime` | voice runtime | `SELECT` on published releases and active phone numbers only; writes conversations, events, work items and usage via `SECURITY DEFINER` functions |
| `app_worker` | background jobs | ops tables, retention, partitions, rollups, outbox |
| `app_readonly` | analytics / replica | read-only, RLS-scoped |

No role has `BYPASSRLS`. Definer functions pin `search_path = ''`, revoke `PUBLIC` execute and
use explicit grants. Each role gets its own `statement_timeout`, `lock_timeout`,
`idle_in_transaction_session_timeout` and connection limit.

## 12. Source mapping from the current schema

| Current (`supabase/migrations`) | Target |
| --- | --- |
| `clinics` | `tenancy.workspaces` (+ `agents.agents` for greeting/emergency/fallback messages) |
| `clinic_users` | `iam.memberships` |
| `clinic_private.platform_admins` | `iam.platform_admins` |
| `phone_numbers` | `agents.phone_numbers` |
| `configuration_versions` (+ `source_version_id`) | `releases.agent_releases` (+ `source_release_id`) |
| `doctors`, `services`, `locations` | `catalog.entities` (types `doctor`, `service`, `location`) |
| `doctor_services` (fees) | `catalog.entity_relations` (`doctor_offers_service`, fee in `attributes`) |
| `weekly_schedules` | `catalog.availability_rules` |
| `special_date_schedules`, `schedule_exceptions` | `catalog.availability_exceptions` |
| `temporary_notices` | `knowledge.announcements` (`notice_type` → `kind`, `starts_at/expires_at` → `valid_during`) |
| `approved_faqs` | `knowledge.faqs` |
| `knowledge_documents` | `knowledge.documents` + `knowledge.document_versions` |
| `knowledge_documents.sections` (jsonb) | `knowledge.document_sections` (+ derived `knowledge.chunks`) |
| `caller_profiles` | `engagement.contacts` |
| `consent_events` | `engagement.consents` |
| `call_sessions` | `engagement.conversations` |
| `call_events` | `engagement.call_events` |
| `appointment_requests`, `callback_requests` | `engagement.work_items` (kinds `appointment_request`, `callback_request`) |
| `usage_records` | `billing.usage_events` |
| `audit_logs` | `audit.audit_log` |

The existing data is fictional "Clinic A" test data, so the plan is a fresh schema plus
re-seeding from `packs/clinic/seeds/`, not a data migration.

## 13. Repository layout for the database

```text
docs/database/
├── database-schema.md          # this document (agreed schema)
└── erd.dbml                    # optional: §8 extracted for dbdiagram.io
db/
├── migrations/                 # 0001_foundation.sql, 0002_iam_tenancy.sql, 0003_catalog.sql,
│                               # 0004_knowledge.sql, 0005_agents_releases.sql,
│                               # 0006_engagement.sql, 0007_billing_audit_ops.sql, ...
└── seeds/platform.sql          # platform data only (roles, rate cards, pack registry)
src/praxima/packs/<pack>/seeds/ # fictional domain demo data, kept with its pack
supabase/migrations/            # current schema, frozen during the transition
tests/db/                       # isolation, constraints, partitions, append-only
```

## 14. Open decisions

| Decision | Default in this document | Alternative |
| --- | --- | --- |
| Vector store | pgvector, `vector(384)` (fits today's `BAAI/bge-small-en-v1.5`) | keep Qdrant; drop `embedding` from `chunks` |
| Embedding models per workspace | one model platform-wide | per-model chunk tables if models must differ |
| Staff identity | own `iam` tables; Supabase Auth or OIDC linked via `identities` | Supabase Auth only |
| Dashboard session store | Redis (shared, TTL) | `iam.sessions` table |
| UUIDv7 source | app-generated (PG16) | native `uuidv7()` on PG18 |
| JSON Schema in DB | application-side only | `pg_jsonschema` extension |
| Default retention | set per workspace (not fixed here) | platform default, e.g. 90 days for conversations |

## 15. Changes from the first draft

**Must-fix**
1. **Partitioning:** `conversations` is no longer partitioned (its FKs and unique key could not
   work). Partitioned tables use `(id, time)` primary keys, and `usage_events` de-duplicates on
   `(workspace_id, event_key, occurred_at)`.
2. **Conversations:** added the fields the runtime and dashboard use today:
   `deadline_at`, `heartbeat_at`, `failure_code`, `safety_flag`, `phone_number_id`,
   `answered_at`, `duration_seconds`, `transfer_attempted` / `transfer_succeeded`,
   `detected_languages`, `primary_intent`, `provider_room_id`, the encrypted caller number and
   `channel`. `provider_call_id` is now unique per provider.
3. **Work items:** added their own encrypted PII (subject name, callback number, staff note),
   `pii_erased_at`, `retention_until`, `entity_id`, and a `kind_id` FK. The idempotency key is
   now unique per workspace.
4. **Publication status:** added to entities, relations, availability, FAQs and
   announcements. Announcements also gained `internal_note`, `created_by` and FK-based entity
   scope.
5. **Documents:** status moved to versions, and `documents.published_version_id` was added.
   `storage_uri` is now nullable. Added a new `document_sections` table (reviewed wording),
   separate from the derived `chunks`, plus the missing upload metadata and a fixed embedding
   dimension.
6. **Shared platform rows:** packs are installed into each workspace. Added
   `tenancy.pack_versions` and `workspaces.pack_key` / `pack_version`. `entity_types` and the
   new `work_item_kinds` are always tenant-owned. Rate cards were split into platform
   `rate_cards` and tenant `workspace_rate_overrides`.
7. **Release snapshot:** now includes pack, policy, tools, messages, work item kinds, FAQs and
   release metadata. Added `source_release_id` and `prompt_version`, and publishing is
   digest-checked.
8. **FAQs:** added the `knowledge.faqs` table (was missing).

**Should-fix**
- One Postgres schema per module, named after it (`agents`, `releases`, `engagement`).
  `agent_releases` now lives in `releases`.
- Authoring columns on every editable table: `created_by`, `updated_by`, `deleted_at`,
  `row_version`.
- Consents: added `action`, `notice_version` and `conversation_id`. Contacts: added
  `preferred_language`, `last_confirmed_at`, `consented_at` and the encrypted display name.
- Billing: usage events carry costs, currency, rate card, override and `agent_id`. Rollups
  are per agent and currency.
- Jobs and outbox: added `attempts`, `next_attempt_at`, leases (`locked_until`) and
  `error_code` instead of free text.
- Phone numbers: partial unique index on active numbers, plus an E.164 check.
- Memberships: unique key with `NULLS NOT DISTINCT`. The staff role `agent` was renamed to
  `staff`.
- `agent_tools`: argument schemas removed (they live in code). Only `enabled` and `config`
  remain.
- Types: `text` plus `CHECK` instead of `varchar(n)`, `text[]` for lists, and `citext` email.
- Audit log: added `organization_id`, `actor_type` and `outcome`.
