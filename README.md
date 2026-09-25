# AI Clinic Receptionist — development foundation

The original LiveKit/Sarvam/Gemini voice pipeline is preserved. The current step
adds **read-only published knowledge for fictional Clinic A** to the original LLM
agent. No custom turn handler, custom TTS, call-session orchestration, usage writes,
request collection or transfer is attached to this path.

## Current phone and console agent

The existing Plivo/India LiveKit route targets `inbound-agent`. Run exactly one phone
worker with `uv run src/agent.py start`; local microphone testing uses
`uv run src/agent.py console`. Both use the same knowledge tools and original speech
settings. Do not run the previous clinic test worker for this step.

Each session reads Clinic A's single published version using the restricted runtime
credentials, then closes the database connection. All conversational lookups use that
in-memory version. Publish approved dashboard changes before starting a new session.
Drafts, patient details, requests and other clinics are not loaded. The SIP path exposes
Clinic A only for the explicitly approved called-number/trunk/rule combination.

The LLM handles natural phrasing and follow-ups through one hybrid-RRF knowledge tool.
Published doctors, schedules, fees, locations, FAQs, notices and reviewed uploads are
rendered into a single version-scoped retrieval corpus. Schedules are working hours,
not bookable slots.
If loading fails, tools report unavailable without stopping the voice pipeline or
substituting the generic business demo's facts. Provider charges still apply.

The separate clinic SIP experiment was rolled back and its real-number database
mapping remains disabled. **New calls do not appear in the dashboard Calls tab.**
Existing dashboard data and experimental code are retained, not reactivated. The
original worker's logging/recording behavior has not been redesigned in this step;
use fictional inputs only. This is not a production medical service.

## Session backend and dashboard

The new backend supports pinned call sessions, confirmed request collection,
encrypted PII, safety routing, usage units and scoped staff workflows. The
FastAPI/Jinja dashboard supports doctor/service/schedule/notice authoring, requests,
calls and explicit publish/rollback. A separate **console-only fictional test**
now binds actual speech/turn events, bilingual readbacks and provider metrics.
The separate fictional SIP pilot is inactive; real-clinic rollout and verified
transfers remain gated, not production-ready.
See [development activation](docs/development-activation.md) for private account
setup, publication requirements and the local voice test's limitations.

Start the local dashboard with `uv run python scripts/dashboard.py`, then open
http://127.0.0.1:8080. Sign-in requires a Supabase Auth account with a clinic
membership; there is no seeded login. See [setup, permissions, verification and
remaining activation gates](docs/phase-3-4-sessions-dashboard.md).

## Phase 2 status

Versioned public snapshots, preview/publish/rollback and typed doctor/service/fee/
schedule/location/FAQ tools are implemented and tested offline and against the
development database. See [Phase 2 workflow, tool behavior and limits](docs/phase-2-knowledge.md).
Phase 1's legacy seed snapshots are preserved: new tools require an explicit
authorized version-2 publication and fail closed on version 1. The controlled phone
test uses Clinic A's published version. This is not a production-ready medical service.

## Clinic documents

Managers upload `.docx` or `.md` background prose (clinic story, vision,
achievements, doctor biographies) on the dashboard's **Clinic documents** page.
Extraction is local and bounded: 5 MiB per upload, 200 sections, 100 000 characters,
no macros, embedded objects, external entities or archive bombs. An upload never
reaches a caller. Staff review and edit every section, assign an optional doctor,
approve the document, and only the next **Preview → Publish** makes it part of the
snapshot (`schema_version` 3, `document_sections`), for new calls only.

Publication converts both structured form data and reviewed uploaded prose into
bounded knowledge chunks. Dashboard Agent test and the phone worker call the same
`search_clinic_knowledge` path. It combines in-process lexical ranking with Qdrant
semantic ranking using reciprocal-rank fusion (RRF). Qdrant receives only public facts
from a reviewed published snapshot, never drafts or raw uploads. The phone system
prompt is rendered from `agent_system_prompt.j2`; active **Quick daily info** notices
are included in that prompt as well as the retrieval corpus:

```sh
uv sync --extra semantic
docker compose up -d qdrant
# .env: QDRANT_URL=http://127.0.0.1:6333
```

After changing a form or approving a document, use **Preview current drafts → Publish
reviewed version**, then start a new call. Publication indexes the complete public
corpus. Without `QDRANT_URL`, the same endpoint remains available with an explicit
lexical fallback.

## Setup

Python 3.10+, `uv`, and a **dedicated Supabase Cloud development project** are
required. No implicit localhost database is supported. From this project root:

```sh
uv sync --locked
```

Keep existing provider settings in your ignored `.env`. Add missing keys using
[.env.example](.env.example) as a reference; do not overwrite working secrets or
put them in the template. Obtain the Supabase project ref, URL and publishable
key from its dashboard. Put the direct/session-pooler **migration** URI in
`MIGRATION_DATABASE_URL`, with `/postgres`, port 5432, encoded password and
`?sslmode=require`. No other URI query parameters are accepted. Do not use
transaction pooling (6543), a service key in a browser, or the migration login
in the runtime.

`SUPABASE_PUBLISHABLE_KEY` is used by dashboard Auth/PostgREST. It is not a
database password and is not consumed by Phase 1 repositories. Hosted Supabase
already supplies the `auth` schema and Auth roles; this is not a generic bare
PostgreSQL bootstrap.

## Migrations and fictional fixtures

Review [the migration files](supabase/migrations/202609170001_tenancy.sql) and
[Phase 1 scope](docs/phase-1-database.md) before proceeding. Replace the placeholder
below with the dedicated **development** project reference, not a secret:

```sh
uv run python scripts/database.py migrate --confirm-development-project <development-project-ref>
uv run python scripts/database.py seed --confirm-development-project <development-project-ref>
uv run python scripts/database.py provision-runtime --confirm-development-project <development-project-ref>
uv run python scripts/database.py status --confirm-development-project <development-project-ref>
```

Initial migration refuses a nonempty public application schema. All pending
migrations execute in one transaction under a migration lock. History records
SHA-256 checksums; unknown, changed and out-of-order migrations are refused.
Always add a forward migration rather than editing an applied file. Seed and
provisioning require the exact current migration history. Project confirmation
is an operator assertion of development use, **not automatic detection of a
production project**.

Seed is idempotent per fictional clinic and does not overwrite fixture edits.
It creates two explicitly fictional clinics, each with two Sharma doctors,
three services, different fees and Monday hours, leave, an expiring closure,
FAQ, a published sample administrative document record, and anonymized test
call/request history. Their reserved fictional numbers use `provider=test`
and a non-live trunk. **No real phone assignment is seeded.** No Auth users are
persistently seeded and no actual Storage upload occurs.

Initial runtime provisioning creates a random password in ignored `.env.runtime`
with permissions 0600. It refuses an existing artifact or an already-login-enabled
role: it is not a password rotation command. Never print or share that file.
If provisioning is interrupted, check the DB role state and artifact locally;
do not repeatedly retry or assume an artifact proves a successful commit.
The original `.env` and its `DATABASE_URL` are left unchanged. Offline tests
explicitly load `.env.runtime`; the current voice agent does not. A later
supervised clinic deployment must inject this restricted `DATABASE_URL` from
secret management, never the migration URL.

## Verification

```sh
uv run ruff format --check src/clinic scripts tests
uv run ruff check src/clinic scripts tests
uv run mypy
uv run pytest -q
uv run pytest -q --development-project=<development-project-ref>
uv run python -m build
```

Default pytest skips DB integration tests; skips are not evidence of isolation.
The explicit integration run requires migrated, seeded DEV data and restricted
runtime credentials. It uses real `authenticated` and `clinic_runtime` roles,
transactional temporary Auth identities, and rolls back mutation fixtures.
Use no real personal data in this project. Build produces a typed `clinic`
library wheel and a source distribution with migrations/scripts/tests; it is
not a production worker deployment. The existing agent is still run through
its existing source entrypoint [src/agent.py](src/agent.py).

See [architecture](docs/product-architecture.md) and
[Phase 1](docs/phase-1-database.md) / [Phase 2](docs/phase-2-knowledge.md) /
[Phases 3–4](docs/phase-3-4-sessions-dashboard.md) verification.
Database roles,
RLS and offline fixtures do not certify clinical safety or end-to-end call health.
