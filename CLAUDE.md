# CLAUDE.md: praxima-automata backend

Guidance for Claude Code (and humans) working in this repository. Read it before changing
code. It defines what the platform is, the rules that must never break, the target
architecture and folder structure, and how to get there from today's code.

- References: `PROJECT_UNDERSTANDING.md` (how today's code works, with `path:line` refs),
  **`docs/database/database-schema.md` (target data model; source of truth for tables)**,
  `docs/database-design-plan.md` (planning history), `docs/product-architecture.md`,
  `docs/phase-*.md`, `.github/copilot-instructions.md` (original clinic product spec).
- The staff web UI is a separate Next.js app in `../frontend` (its own git repo). This repo
  is **backend only**.

---

## 1. What this is

A **multi-tenant, multi-domain voice-agent platform**. An organization configures one or
more **agents** that answer phone calls in the organization's languages (Hindi-first today).
They answer questions from **published** information, capture requests or leads for staff,
and hand off to humans. The same platform serves a **clinic**, a **real-estate developer**,
or any other business **by configuration (a "domain pack"), not by new code or new tables**.

| Concept (platform) | Clinic pack | Real-estate pack |
| --- | --- | --- |
| Workspace | a clinic / branch | a developer / project office |
| Entity types | doctor, service, location | property, project, developer, sales agent |
| Relations | doctor offers service (fee) | property in project |
| Availability | doctor weekly hours | site-visit windows |
| Work item kinds | appointment request, callback | lead inquiry, site-visit request, callback |
| Knowledge | doctor bios, policies | brochures, RERA/possession FAQs, price sheets |
| Prohibited advice | medical diagnosis or advice | legal/financial guarantees, price commitments |

**Where the code is today:** it is **clinic-specific** (package `clinic`, Supabase tables
`clinics`, `doctors`, `appointment_requests`, …) and runs a fictional "Clinic A" for
development. The live call path must keep working while we generalize:

```
Caller → telephony provider (Plivo) → SIP trunk → LiveKit → voice worker (src/agent.py, "inbound-agent")
```

**The target data model** is `docs/database/database-schema.md` (revised draft, awaiting
sign-off; its §14 lists open decisions). Nothing is migrated to it yet. Code against the
**concepts** through repositories and ports, so table and column names can still change
without touching business logic. When the data model changes, update that document first.

---

## 2. Non-negotiable rules

Breaking any of these is a bug, even if the tests pass.

**Product safety (every domain)**
- Agents are **informational and administrative**. They never make commitments on the
  business's behalf (no confirmed bookings, prices, availability guarantees, or legal,
  medical or financial advice). They create **work items** that a human confirms.
- Each domain pack declares its own prohibited topics, emergency handling and fallback
  wording. **Safety routing is deterministic** (policy engine plus pack rules), never left to
  the LLM.
- Agents answer only from the **published, immutable release snapshot**. Drafts, raw
  uploads and unreviewed data never reach callers.
- A conversation is **pinned** to the release it loaded at the start. Publishing affects new
  conversations only.
- Publication is a staff-only action (preview → publish, digest-checked). No model-visible
  tool can publish, confirm a work item or mark a readback as heard.

**Tenancy**
- Hierarchy: **Organization → Workspace → Agent**. A phone number binds to exactly one
  agent. The workspace is the isolation boundary for data.
- Resolve the tenant from **trusted ingress** only (the called number or trusted SIP
  metadata → agent → workspace). Never from caller speech, caller ID or request input.
- Tenancy model: **shared database, shared schema, row-level isolation**. Every
  tenant-owned row carries `workspace_id NOT NULL`, and every query is tenant-scoped.
  Target: composite `(workspace_id, id)` foreign keys plus RLS (enabled **and forced**) keyed
  on `app.workspace_id`, set with `SET LOCAL` by trusted backend code. Organization-scoped
  tables (memberships, API keys, audit) use `app.organization_id`.
- A table that other tenant tables reference by workspace never has a nullable
  `workspace_id`. Shared templates (pack entity types, work item kinds) are **installed
  (copied) into each workspace**, never referenced across tenants.
- Never let a client choose the tenant, a table, a column or an entity type's schema.
  Allow-list everything.

**Secrets and privacy**
- **Never read `.env`, `.env.runtime` or any real secrets file.** Use `.env.example` and the
  loading code. Never print, log or commit credentials, tokens, phone numbers or keys.
- Never log exception messages, URLs, headers, bodies, prompts, transcripts or user input.
  Log `type(exc).__name__` and correlation ids only.
- Contact PII is encrypted per field (envelope encryption with a key version, today
  `PiiCipher`), bound to tenant, resource and field, with an HMAC lookup column if lookup
  is needed. Decryption happens only in an explicit, **audited** staff action (a POST, never
  a GET). Erasure nulls the ciphertext and keeps the audit record.
- Recording is off by default. Audit every publish, membership change and PII access.

**Least privilege**
- Separate credentials per process. **Today:** migration URL (scripts only), restricted
  runtime DB role (voice worker), and Supabase publishable key + user JWT (dashboard, via
  PostgREST/RLS). **Target:** `db_owner` (migrations), `app_api`, `app_agent_runtime`
  (read-only releases; writes only via `SECURITY DEFINER` functions), `app_worker`,
  `app_readonly`. No `BYPASSRLS` or superuser at runtime.
- The API never uses owner or service-role credentials.

**Dashboard API security** (keep all of it through every refactor)
- An opaque HttpOnly SameSite=Strict session cookie. Identity-provider tokens stay
  server-side. CSRF token on every non-GET request.
- Exact `Origin` match with the configured dashboard origin, content-type allow-list, body
  size limits, trusted hosts, `Cache-Control: no-store`, and CSP `default-src 'none'`.

**Dev vs production**
- Fictional development code (`src/praxima/dev/`) is never imported by production code.
  The only exceptions are the two existing activation guards listed in
  `tests/test_architecture.py`.
- Never assign, dispatch or reconfigure a real phone number from code.

---

## 3. Commands

Package manager: **uv**. The Python target is 3.10 (the venv runs 3.12).

```bash
uv sync                                   # install (add --extra semantic for fastembed)
uv run src/agent.py console               # local microphone test
uv run src/agent.py start                 # LiveKit worker
uv run python scripts/dashboard.py        # dashboard API on http://127.0.0.1:8080 (JSON only)
docker compose up -d qdrant               # optional semantic search (today)

uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run mypy                               # strict
uv run pytest -q                          # unit and API tests; DB tests are skipped
uv run pytest -q --development-project=<dev-project-ref>   # opt-in real DB tests

uv run python scripts/database.py {migrate|seed|provision-runtime|status} \
    --confirm-development-project <dev-project-ref>
```

Frontend (in `../frontend`): `pnpm dev` on http://127.0.0.1:3000. It proxies `/api/*` to
this API, so the dashboard origin must be `http://127.0.0.1:3000` locally.

Known issue: `mypy` stops on a numpy stub (`type` statement vs `python_version = "3.10"`)
inside `.venv`. That is unrelated to project code; don't weaken strictness to hide it.

---

## 4. Architecture

### 4.1 Style: a modular monolith

One deployable codebase with several entrypoints, split into **modules (bounded
contexts)**. Each module owns its data and exposes a small public interface. This keeps
local development simple (one repo, one test run) while letting any module be extracted
into a service later.

### 4.2 Runtime processes (entrypoints)

| Process | Entrypoint (target) | Responsibility |
| --- | --- | --- |
| Voice runtime | `entrypoints/voice_worker.py` (today `src/agent.py`) | LiveKit worker: resolve agent, load pinned release, run the conversation, tools, safety |
| HTTP API | `entrypoints/api.py` (today `scripts/dashboard.py`) | Staff dashboard and management API, `/api/v1` |
| Background jobs | `entrypoints/jobs.py` | Retention/erasure, partition maintenance, usage rollups, outbox dispatch, re-indexing |
| CLI / ops | `scripts/*.py` → `entrypoints/cli.py` | Migrations, seeding packs, provisioning |
| Staff UI | `../frontend` | Talks only to the HTTP API |

### 4.3 Conversation flow (domain-neutral)

```
inbound call → resolve phone number → agent → workspace (trusted ingress)
→ create conversation (idempotent on provider call id)
→ load ONE published agent release (pinned) + the domain pack's policies and tools
→ conversation loop: deterministic safety policy → typed tools over the release
     (find/get entities, availability, knowledge search, announcements)
→ create work items (idempotent per conversation) → close → usage + audit events
```

The hot path does **one indexed read** of the published release per call and keeps it in
memory. Retrieval is hybrid lexical plus semantic (RRF) over reviewed knowledge chunks and
active announcements.

### 4.4 Layers inside every module

```
 api/  (HTTP adapter: routers, request/response schemas)       ─┐
 runtime hooks (voice tools exposed by the module)              ├─► application/
                                                                ─┘   use cases, ports (Protocols)
                                                                            │
                                                                        domain/
                                                              pure models, rules, state machines
                                                                            ▲
 infrastructure/  repositories and provider adapters implement the ports ───┘
```

Dependency rules (enforce in review; add `import-linter` contracts when the move starts):
1. `domain/` imports only `shared.kernel` and the stdlib/pydantic. No I/O, env or clock
   reads (inject time).
2. `application/` depends on `domain/` and **ports**, never on concrete clients.
3. `infrastructure/` is the only layer that imports `psycopg`, `httpx`, provider SDKs,
   `qdrant_client`, `google.genai`, `livekit` or `fastembed`.
4. **Modules talk to each other only through their `application` public interface** (the
   names exported in `module/__init__.py`) or through domain events. Never import another
   module's `infrastructure/` or `domain/` internals, and never query another module's
   tables.
5. `api/` never imports `livekit`; the voice runtime never imports `fastapi`.
6. **Core code never branches on the industry.** No `if industry == "clinic"`. Domain
   differences come from pack data (schemas, rules, templates) and registered extension
   points.
7. Nothing in production imports `dev/`.

### 4.5 Modules

Each module owns **one Postgres schema of the same name** (full table list in
`docs/database/database-schema.md` §5). A module's code queries only its own schema.

| Module = schema | Owns |
| --- | --- |
| `iam` | users, external identities (OIDC/Supabase subject), memberships and roles (owner/admin/manager/staff/viewer), API keys, platform admins |
| `tenancy` | organizations, workspaces (industry, installed pack and version, timezone, languages, limits), pack version registry |
| `agents` | agent definitions (persona, prompt template, voice/STT/LLM/TTS config, messages, transfer policy), phone numbers, per-agent tool enablement |
| `catalog` | entity types (versioned JSON Schema), entities, relations, availability rules and exceptions |
| `knowledge` | sources, documents and versions (review lifecycle), reviewed sections, derived chunks and embeddings, FAQs, announcements (live updates), retrieval |
| `releases` | build snapshot → preview → publish → rollback; release snapshot model with `schema_version` and digest |
| `engagement` | contacts (encrypted), consents, conversations and call events, work item kinds, work items (stage, payload validated per kind, own encrypted PII) and their history, tasks |
| `billing` | platform rate cards, workspace rate overrides, usage events, rollups, per-workspace limits |
| `audit` | append-only audit log |
| `ops` (shared infra) | transactional outbox, idempotency keys, jobs, schema migrations |

### 4.6 Domain packs

A **pack** is versioned configuration that specializes the platform for an industry. It is
data first, with code only through narrow extension points:

```
packs/<pack_id>/
├── manifest.yaml            # id, version, supported languages, entity types, work item kinds, tools
├── entity_types/*.json      # JSON Schema per entity type (attributes, searchable fields, display)
├── work_items/*.json        # JSON Schema per work item kind (payload, stages)
├── policy.yaml              # prohibited topics, emergency triggers, refusal/fallback wording
├── prompts/*.j2             # persona and system prompt templates (rendered with release data)
├── tools.yaml               # which generic tools are enabled, with pack-specific descriptions
├── labels.yaml              # UI/voice vocabulary ("Doctor", "Property", …)
├── seeds/                   # fictional demo data for development and tests
└── extensions.py            # optional: registered hooks (e.g. speech normalizer), kept tiny
```

- Packs ship with the platform: `clinic` first, then `real_estate`, and `_template` for new
  ones. Each released pack version is registered in `tenancy.pack_versions`.
- **Installing a pack** into a workspace sets `workspaces.pack_key` / `pack_version` and
  copies the pack's entity types and work item kinds into that workspace's tables. Upgrading
  a pack is an explicit, versioned operation. Release snapshots record the pack key and
  version they were built with.
- Generic voice tools work for every pack: `find_entities(type, filters)`,
  `get_entity(key)`, `get_availability(entity)`, `search_knowledge(query)`,
  `get_announcements()`, `create_work_item(kind, payload)`, `request_callback()`,
  `transfer_to_human()`. Packs rename and describe them for the LLM but never change their
  safety semantics.
- Adding a new industry means **adding a pack and tests, with no core changes and no
  migrations**. If a core change seems necessary, write an ADR first.

---

## 5. Folder structure

### 5.1 Today (after step 1)

The code lives in `src/praxima/` in the target layout. **Step 1 moved whole files with no
behaviour change**, so the content is still clinic-specific:
- `entrypoints/api.py` is still the single ~800-line `create_app()` (the old `dashboard.py`).
- `entrypoints/voice_worker.py` is the old `src/agent.py`. It is excluded from ruff and mypy,
  as before. `src/agent.py` is now a thin wrapper that calls its `main()`, and stays as the
  LiveKit deploy path.
- The clinic prompt template is in `packs/clinic/prompts/`.
- File placement deviates slightly from §5.3 where no split has happened yet:
  - `settings.py` → `shared/db/settings.py`, `db.py` → `shared/db/pool.py`
  - `speech.py` → `runtime/speech/normalize.py`, `rag.py` →
    `modules/knowledge/application/retrieval.py`
  - `voice_errors.py` and `usage.py` → `runtime/`
  - `answers.py` → `integrations/llm/gemini.py`
  - dev files keep their names under `dev/`

Migrations are still in `supabase/migrations/`, and tests are still flat in `tests/`.

`tests/test_architecture.py` enforces the dependency rules. It lists today's four known
violations **exactly**, fails on any new one, and fails if a listed one is fixed but not
removed. Rule 4 (modules use only each other's public interface) is **not enforced yet**:
most code still imports `modules.releases.domain.snapshot` and other internals directly.
Step 3 fixes this.

### 5.2 Target

The package is renamed from `clinic` to the domain-neutral **`praxima`**.

```
praxima-automata/
├── CLAUDE.md  README.md  PROJECT_UNDERSTANDING.md
├── pyproject.toml  uv.lock  compose.yaml  .env.example
├── docs/
│   ├── architecture/              # ADRs: NNNN-title.md (decisions, trade-offs)
│   ├── database/                  # database-schema.md (target model + DBML ERD)
│   ├── packs/                     # how to author a domain pack
│   └── runbooks/                  # deploy, rollback, backup/restore, key rotation, incidents
├── db/                            # target: vendor-neutral PostgreSQL
│   ├── migrations/                # NNNN_description.sql, forward-only, checksummed
│   └── seeds/                     # platform seeds; pack demo data lives in packs/*/seeds
├── supabase/migrations/           # CURRENT schema, frozen once db/ takes over
├── sip/                           # LiveKit SIP dispatch rules
├── scripts/                       # thin wrappers around entrypoints/cli.py
├── src/
│   ├── agent.py                   # KEEP as a shim → praxima.entrypoints.voice_worker (deploy path)
│   └── praxima/
│       ├── shared/                # shared kernel, no business rules
│       │   ├── kernel/            # ids (UUIDv7), time/clock, money, errors, result types
│       │   ├── config.py          # typed settings, validated at startup
│       │   ├── logging.py         # structured, PII-free logging + correlation ids
│       │   ├── security/          # PII envelope encryption, HMAC lookup, hashing
│       │   ├── db/                # connection pool, tenant scope (SET LOCAL), transactions, outbox
│       │   └── events.py          # domain event base + in-process bus
│       ├── modules/
│       │   ├── iam/
│       │   │   ├── __init__.py    # PUBLIC interface: the only thing other modules import
│       │   │   ├── domain/        # models, rules (pure)
│       │   │   ├── application/   # services/use cases, ports.py
│       │   │   ├── infrastructure/# repositories (Postgres), identity-provider adapters
│       │   │   └── api/           # router.py, schemas.py
│       │   ├── tenancy/  agents/  catalog/  knowledge/
│       │   ├── releases/  engagement/  billing/  audit/
│       ├── runtime/               # voice conversation runtime (domain-neutral)
│       │   ├── session.py         # conversation orchestration, pinning, cleanup
│       │   ├── tools/             # generic tool implementations + registry
│       │   ├── policy/            # deterministic safety engine that executes pack policies
│       │   ├── prompting.py       # renders pack templates with release data
│       │   ├── speech/            # normalization (digits, dates), language handling
│       │   └── fallbacks.py       # deterministic outage/timeout audio and prompts
│       ├── integrations/          # third-party provider adapters shared by modules/runtime
│       │   ├── livekit/  telephony/ (plivo)  speech/ (sarvam)
│       │   ├── llm/ (gemini)  vectors/ (qdrant | pgvector)  identity/ (supabase | oidc)
│       ├── packs/
│       │   ├── _template/  clinic/  real_estate/
│       │   └── loader.py          # load and validate manifests and schemas, version registry
│       ├── entrypoints/
│       │   ├── api.py             # create_app(): middleware, /api/v1 module routers, lifespan
│       │   ├── http/              # middleware (origin/CSRF/size/headers), deps (session, roles)
│       │   ├── voice_worker.py    # LiveKit entrypoint/prewarm
│       │   ├── jobs.py            # scheduled/background jobs
│       │   └── cli.py             # migrate, seed <pack>, provision, status
│       └── dev/                   # fictional development only; never imported in prod
└── tests/
    ├── conftest.py
    ├── unit/<module>/             # domain + application with fakes; no network
    ├── api/                       # HTTP contract, auth, role, CSRF/Origin, tenant isolation
    ├── runtime/                   # tools, policy engine, pinning, fallbacks
    ├── packs/                     # every pack: schemas valid, seeds load, same flows pass
    ├── db/                        # isolation, constraints, partitions (opt-in, real Postgres)
    └── architecture/              # import rules (import-linter / pytest)
```

### 5.3 Migration map (original `src/clinic` → target)

| Current (`src/clinic/…`) | Target (`src/praxima/…`) |
| --- | --- |
| `src/agent.py` | `entrypoints/voice_worker.py` + `runtime/session.py` (keep the `src/agent.py` shim) |
| `settings.py`, `WebSettings` | `shared/config.py` |
| `db.py` | `shared/db/` |
| `privacy.py` | `shared/security/` |
| `snapshot.py` | `modules/releases/domain/` (generalized snapshot, new `schema_version`) |
| `publication.py` | `modules/releases/application/` |
| `knowledge.py`, `questions.py` | `modules/catalog/` (entity queries) + `runtime/tools/` |
| `documents.py` | `modules/knowledge/domain/` (extraction, ranking) + `application/` (upload/review) |
| `rag.py`, `vectors.py` | `modules/knowledge/application/retrieval.py` + `integrations/vectors/` |
| `answers.py` | `modules/knowledge/application/` + `integrations/llm/gemini.py` |
| `requests.py` | `modules/engagement/domain/work_items.py` (generic kinds, pack schemas) |
| `sessions.py`, `usage.py` | `modules/engagement/` + `modules/billing/` + `runtime/session.py` |
| `resolver.py` | `modules/agents/application/` (number → agent → workspace) |
| `staff.py` | `modules/iam/` |
| `safety.py` | `runtime/policy/` (engine) + `packs/clinic/policy.yaml` (clinic rules) |
| `speech.py` | `runtime/speech/` |
| `tools.py`, `session_tools.py`, `agent_knowledge.py` | `runtime/tools/` (generic) |
| `prompt.py`, `templates/agent_system_prompt.j2` | `runtime/prompting.py` + `packs/clinic/prompts/` |
| `voice_errors.py`, `fallback_audio.py` (prod parts) | `runtime/fallbacks.py`, `shared/logging.py` |
| `dashboard.py` | `entrypoints/api.py`, `entrypoints/http/`, `modules/*/api/`, `integrations/identity/` |
| `development.py`, `dev_*.py`, `activation.py`, `sip_test.py` | `dev/` |
| clinic strings, fixtures, seeds | `packs/clinic/` |

### 5.4 How to migrate (incremental, never big-bang)

1. ✅ **Skeleton first (done):** `src/praxima/` skeleton; every file moved with no
   behaviour change, and all imports updated (no `clinic` package or shims left).
2. **API next:** split `dashboard.py` into `entrypoints/api.py` + `http/` + module routers
   (best test coverage). Then move to the REST `/api/v1` contract (§6) together with
   `../frontend`.
3. **Generalize behind ports:** introduce repositories and ports so application code stops
   knowing table names. Clinic tables keep working underneath.
4. **Extract the clinic pack:** move clinic vocabulary, policy, prompts and fixtures into
   `packs/clinic/`. Core tests must pass with no clinic words in core code.
5. **New data model:** only once `docs/database/database-schema.md` is signed off (§14 open
   decisions resolved). Add `db/` migrations, swap repository implementations, and bump the
   release `schema_version` to 4. Existing data is fictional, so re-seed from
   `packs/clinic/seeds/` rather than migrating data. Keep the old schema until the voice path
   is verified on the new one.
6. **Second pack (`real_estate`)** is the proof that the design is generic: it must need no
   core code changes.

Keep refactors and behaviour changes in separate commits. The voice call path must work
after every step.

---

## 6. HTTP API conventions (REST, `/api/v1`)

Target contract. Today's endpoints (`/api/login`, `/api/clinics/{id}/rows/{table}`,
POST-for-everything) migrate to this. Change the backend and
`../frontend/src/features/*/api.ts` **together**.

| Method | Path (under `/api/v1`) | Notes |
| --- | --- | --- |
| POST / GET / DELETE | `/auth/session` | sign in / current session + CSRF + memberships / sign out |
| GET | `/organizations/{orgId}/workspaces` | |
| GET, PATCH | `/workspaces/{wsId}` | settings: languages, timezone, limits |
| GET | `/workspaces/{wsId}/overview` | today's summary (was `/today`) |
| GET, POST / GET, PATCH | `/workspaces/{wsId}/agents[/{agentId}]` | persona, messages, voice config |
| GET | `/workspaces/{wsId}/entity-types` | installed from the pack, plus custom types |
| GET | `/workspaces/{wsId}/work-item-kinds` | installed from the pack: payload schema and stages |
| GET, POST / GET, PATCH, DELETE | `/workspaces/{wsId}/entities[/{id}]` | `?type=doctor&q=…`; attributes validated by type schema |
| GET, POST / GET, PATCH | `/workspaces/{wsId}/announcements[/{id}]` | live updates |
| GET, POST / GET, PATCH, DELETE | `/workspaces/{wsId}/faqs[/{id}]` | approved question/answer pairs |
| GET, POST / GET, PATCH | `/workspaces/{wsId}/documents[/{id}]` | upload is octet-stream; `status` is PATCHable |
| POST | `/workspaces/{wsId}/agents/{agentId}/releases/preview` | snapshot + digest |
| GET, POST | `/workspaces/{wsId}/agents/{agentId}/releases` | list / publish previewed digest; rollback = publish `{source}` |
| GET / GET, PATCH | `/workspaces/{wsId}/work-items[/{id}]` | `?kind=&stage=`; PATCH `{stage}` |
| POST | `/workspaces/{wsId}/work-items/{id}/contact-reveal` | audited; stays POST |
| GET / GET | `/workspaces/{wsId}/conversations[/{id}]` | sanitized outcomes only |
| POST | `/workspaces/{wsId}/agents/{agentId}/test-queries` | same retrieval path as calls; `is_test` |
| GET, PUT, DELETE | `/workspaces/{wsId}/memberships[/{userId}]` | owner/admin only |
| GET | `/health` (liveness), `/ready` (DB + critical deps) | unauthenticated, no data |

Rules:
- Plural kebab-case resources; the tenant is in the path and **re-authorized** on every
  request against memberships. Never put table names in URLs.
- Pydantic request and response models (`extra="forbid"` on input) with `response_model`
  declared. OpenAPI and `/docs` are served in **dev only**; the frontend may generate types
  from it.
- Auth and roles are FastAPI dependencies (`Depends(require_role(Role.MANAGER))`).
  Permissions are named actions (`entities:write`, `pii:reveal`), mapped to roles in one
  place.
- Errors are always `{"detail": "<safe message>"}`. Status codes: 400 validation, 401
  session, 403 role/origin/CSRF, 404 not in this workspace, 409 conflict/preview
  required/stale digest, 413, 415, 422 schema-invalid attributes, 429, 503 upstream.
- `POST` creates (201 + `Location`), `PATCH` partially updates, `PUT` replaces, `DELETE`
  removes (soft delete). The security middleware accepts these methods, and a bodiless
  DELETE must not fail the content-type check.
- Pagination: `?limit=` (max 100) and `&cursor=` (keyset) for new endpoints; keep
  `limit/offset` only where it already exists. Keep sort order stable.
- Mutations that can repeat (publish, work item creation, uploads) accept an
  `Idempotency-Key` header.

---

## 7. Code conventions

- **Style:** ruff (`E,F,I,UP`, line length 100); `mypy --strict`, with no new `type: ignore`
  unless it has a reason comment. Target py3.10 syntax.
- **Naming:** domain-neutral names in core (`workspace`, `entity`, `work_item`,
  `announcement`, `release`, `conversation`). Industry words (`doctor`, `clinic`,
  `property`) appear only in `packs/`, seeds and pack tests.
- **Module docstring:** one line stating the module's invariant (see existing modules).
  Comments explain *why*, sparingly.
- **Models:** Pydantic v2, `extra="forbid"` for untrusted input. Validate once at the
  boundary, then pass typed objects. Validate entity and work item payloads against their
  pack JSON Schema.
- **Persistence:** no ORM; psycopg with **parameterized SQL only**, inside repositories.
  Table names live only in `infrastructure/`. Privileged writes go through `SECURITY
  DEFINER` functions with `search_path = ''`.
- **IDs and time:** UUIDv7 (time-ordered), `timestamptz`, validity as ranges. Inject a clock
  instead of calling `datetime.now()` in domain code.
- **External calls:** every call has a timeout. Retry only idempotent operations, with
  backoff. Map failures to safe fallbacks: the caller never hears silence, and staff see a
  generic 503.
- **Events:** cross-module side effects use domain events; external side effects
  (webhooks, CRM) go through the transactional outbox.
- **Async:** no blocking I/O on the event loop. Offload CPU-heavy work (docx parsing,
  embeddings).
- **State:** in-process session and rate-limit stores are single-worker only. Moving to
  multiple workers requires a shared store (e.g. Redis); that is a production gate.
- **Dependencies:** pinned in `uv.lock`. Don't upgrade unrelated packages, and don't add a
  second web framework or an ORM without an ADR.

---

## 8. Data and migrations

- **Current:** `supabase/migrations/YYYYMMDDNNNN_*.sql`, run with `scripts/database.py`.
  **Target:** vendor-neutral PostgreSQL 16+ in `db/migrations/NNNN_*.sql`, designed in
  `docs/database/database-schema.md`. Schema changes go into that document first, then into
  a migration.
- Migrations are forward-only and **never edited once applied**. Use expand/contract for
  breaking changes. Avoid unsafe locks: `CREATE INDEX CONCURRENTLY`, `NOT VALID` then
  `VALIDATE`.
- There are three table classes: **tenant** (`workspace_id NOT NULL`, composite tenant FKs,
  RLS enabled and forced), **organization-scoped** (RLS by organization) and **platform** (no
  tenant; read-only to app roles). Every new table needs isolation tests.
- Each module owns its schema. No cross-module joins in application code; use a public
  interface, an event or the release snapshot. Cross-module FKs are allowed in the database
  for integrity.
- Standard columns: `created_at`, `updated_at`, `created_by`, `updated_by`, `deleted_at`
  (soft delete) and `row_version` (optimistic concurrency, HTTP 409) on editable tables.
  Rows that feed a release also have `publication_status` (draft, published or archived).
  Use `text` + `CHECK` for statuses, `text[]` for lists, and `error_code` rather than free-text
  errors.
- PII is stored only as `*_ciphertext` + `pii_key_version` (+ an HMAC lookup where needed),
  never in `jsonb` payloads, metadata or events. Work items keep their own encrypted PII
  (the subject can differ from the caller).
- The release snapshot shape is validated both in SQL and in Pydantic. Change both
  together and bump `schema_version`. The voice runtime must refuse snapshot versions it
  doesn't understand.
- Partition only `call_events`, `usage_events` and `audit_log` (monthly, `PRIMARY KEY (id,
  time)`, no inbound FKs). **`conversations` stays unpartitioned** so other tables can
  reference it. Retention runs as jobs.

---

## 9. Testing

- Every behaviour change needs a test. Bug fixes need a regression test.
- `unit/` uses fakes for ports and no network. `api/` uses `TestClient` +
  `httpx.MockTransport` (pattern in today's `tests/test_dashboard.py`).
- Required for every new endpoint: tenant isolation (another workspace gets 404/403), role
  denial, CSRF/Origin, size limits.
- **Pack parity:** the same runtime and API test flows run against every pack's fictional
  seeds (clinic and real estate), which proves the core is domain-neutral.
- DB tests (`integration` marker) are opt-in against a dedicated dev database, with
  fictional data only. A skipped test is not evidence.
- Before finishing: ruff format check, ruff check, mypy, pytest. If the API contract
  changed, also run `pnpm test` in `../frontend`.

---

## 10. Git

- Commit messages: an imperative subject of 72 characters or fewer, and a body explaining *why*.
- **Do not add `Co-Authored-By: Claude…` or "Generated with Claude Code" lines** to commits
  or PRs. Commits are authored by the repo owner.
- Commit or push only when asked. Never force-push shared branches without explicit
  approval.
- Never commit `.env*` (except `.env.example`), `.qdrant_storage/`, audio renders or
  credentials.
