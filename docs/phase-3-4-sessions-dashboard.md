# Phase 3 backend and Phase 4 minimum dashboard

## Deployment status — important

These changes implement a **separate, tested session/request backend and minimum
staff dashboard**. They do not activate a clinic receptionist on the telephone.
The existing [voice POC](../src/agent.py), provider versions, Plivo resources and
SIP dispatch are unchanged. Its generic prompt/KB is not a clinic-mode fallback.

Phase 3's caller-facing integration remains gated: no trusted SIP destination
adapter or carrier handoff has been activated. A separate console-only development
adapter now supplies speech/readback event wiring and provider usage subscriptions;
it speaks deterministic replies, not LLM output. See [development setup](development-activation.md).
Do not equate the
backend tests with completion of live Phase 3 acceptance testing. Phase 4 can be
used independently for authorized staff authoring/review.

## Implemented components

| Component | Implemented behavior |
|---|---|
| [sessions](../src/clinic/sessions.py) | Idempotent provider/account/call identity; pinned published version; deadline and 20-second heartbeat checks; 60-second inactivity bound; shutdown persistence; stop signal on dependency failure |
| [requests](../src/clinic/requests.py) | Minimal typed request fields; exact uninterrupted readback followed by an explicit caller affirmation; correction invalidation; no model-supplied confirmation flag |
| [privacy](../src/clinic/privacy.py) | AES-256-GCM with random nonce and tenant/resource/field AAD; external versioned keys; protected names/numbers and redacted repr |
| [safety](../src/clinic/safety.py) | Conservative emergency/medical/injection/admin/unknown routing, fixed refusal responses, exact configured emergency wording, supplemental forbidden-claim filter |
| [session tools](../src/clinic/session_tools.py) | Per-call gated knowledge tools, prepare appointment/callback, save only confirmed request, supported-language selection, honest unavailable-transfer result |
| [prompt](../src/clinic/prompt.py) | Versioned administrative policy; JSON-delimited clinic data; request-only and nonclinical rules |
| SQL 005 | Restricted lifecycle/request/usage functions, clinic concurrency and duration reservation limits, maintenance-only abandoned-session reconciliation, staff status and owner membership operations |
| SQL 006 | Audited sensitive request retrieval, column-level PII read restrictions, settings allowlist and separately provisioned platform-admin overview |
| [dashboard](../src/clinic/dashboard.py) | FastAPI/Jinja, Supabase Auth verification, tenant RLS, role checks, secure sessions, draft authoring and explicit publication |

Runtime cannot perform generic table writes or set a request to
`confirmed_externally`. Only authorized staff can record that external human
outcome. Database functions are trusted backend interfaces, not exposed model
capabilities. As before, `app.clinic_id` is not protection against an attacker
who already has arbitrary SQL access with the shared runtime credential.

### Session/adapter contract

1. Resolve the **called destination and expected trunk** from a verified carrier/
   LiveKit ingress adapter. Never substitute `sip.phoneNumber` (caller number),
   arbitrary participant attributes, room names or model arguments.
2. `CallSessionService.start()` checks active routing and a v2 published snapshot.
   Duplicate live call identities return the original pinned version. A different
   room/tenant or ended/expired call is not resumable.
3. Load `CallOrchestrator` and construct `SessionTools` **once per call**. Start
   its watchdog and have the media host close the call when `stop_event` is set.
   Per-call host events/tool writes must be serialized; do not share these objects.
4. Deliver actual caller turns to `user_turn()` before allowing model/tool work.
   Unsafe/unknown turns clear pending confirmation. Do not log or store raw text.
5. Preparing a request produces a revision and exact readback. Only a trusted host
   may report `readback_completed(revision, exact_text, interrupted=False)` after
   speech really finishes. This method is deliberately **not a function tool**.
   Then deliver the next actual caller turn. Interruptions, corrections, a replaced
   revision or an affirmation without completed playback cannot authorize a write.
6. `persist_confirmed()` encrypts PII before SQL. Its request UUID remains stable
   for a retry. A successful result means a request was stored, not a booking.
7. Report trusted playback/caller activity through `activity()` to maintain the
   inactivity timer. Close in a host shutdown/finally handler. Stop the media path
   independently if the database or provider fails; a database heartbeat is not
   proof that media is alive.

The database currently caps concurrent calls at four per clinic and reserves the
configured maximum duration against the monthly minute allowance. Test calls are
excluded from that monthly production accounting. SQL `record_usage()` deduplicates
caller-supplied event UUIDs and stores provider units under `unpriced-units-v1`.
There are **no configured tariffs or invoice estimates**. The console-only test
subscribes to SDK metrics using stable event UUIDs and a bounded queue; this has
not been activated on the real telephone worker.

`clinic_private.reconcile_calls()` is maintenance-only. It closes abandoned
sessions after a deadline or a heartbeat older than two minutes. It is provided
but **not scheduled automatically**. An operator must schedule it with a separate
maintenance credential, never give that credential to the voice or web process.
Historical fixture rows without a lifecycle deadline are intentionally not treated
as live managed sessions. New runtime sessions always receive a deadline.

### Safety limitations

The deterministic classifier is a conservative English/Hindi starter, not a
complete multilingual medical classifier. It neither diagnoses nor assesses
severity. Unknown speech is not an authorization signal. Names also undergo
basic clinical/injection rejection, but input filtering is not a guarantee that
free text contains no sensitive information.

The output filter is **supplemental**, not a safe-to-speak certification. It is
not wired to the legacy LLM. Reviewed emergency wording and per-language scripts
must be approved by a real clinic. English/Hindi request readbacks and separate
per-language Sarvam TTS instances are implemented in the local test adapter;
actual microphone/end-to-end acceptance remains pending.

Transfer deliberately returns `unavailable`, `transferred=false`, and offers
callback. No transfer number is invented. SIP REFER acceptance is not proof that
a receptionist answered. Local English/Hindi development fallback WAVs were rendered
successfully and bypass TTS during an outage. Listening review and real-clinic
approval are still required; these are not production-approved recordings.

## Dashboard startup and access

From the project root, after the normal locked dependency sync:

```sh
uv run python scripts/dashboard.py
```

Open **http://127.0.0.1:8080**. The launcher imports only Supabase URL, publishable
key and optional dashboard origin from the ignored local environment file. It
does not load migration, runtime, voice-provider or PII credentials from that file.
An optional private development key artifact is loaded separately with owner-only
permission/project checks. No fake development login or public demo-data bypass is included.

Initial access requires:

1. Create/invite a staff account in the dedicated Supabase project's Auth console.
   Enter passwords in the local UI, never in chat or committed configuration.
2. A privileged setup operator assigns that Auth user UUID an active `owner`
   membership for the intended clinic in `clinic_users`. This bootstrap is not
   public self-enrollment. No persistent Auth users were created by these tests.
3. Sign in. An owner can then assign existing Auth user UUIDs to clinic roles in
   **Team**; managers cannot change memberships and nobody can change their own
   membership through this endpoint.
4. Existing fictional v1 snapshots intentionally make Today/Test unavailable.
   An owner/manager must first review and publish a valid v2 configuration.
   This does not assign any real number to a fictional clinic.

Production must use an HTTPS reverse proxy and matching `CLINIC_DASHBOARD_ORIGIN`.
The launcher binds loopback, disables request access logs and does not trust
forwarded headers. Configure the edge explicitly; do not expose plain HTTP.

### Authentication and authorization

- Supabase Auth's server-side `/auth/v1/user` verifies each protected request's
  access token. PostgREST independently validates the forwarded bearer JWT and
  applies membership RLS. No unverified decoded JWT, user metadata, service-role
  key or migration connection establishes dashboard authority.
- The browser receives only an opaque session ID in an HttpOnly, SameSite=Strict
  cookie, with Secure required except loopback HTTP. JWTs are not in HTML, URLs,
  browser storage or cookies. Sessions expire after at most 15 minutes; re-login
  is required. Restarting the single worker signs users out.
- Session state is bounded in process memory: **one worker only**. Shared session
  storage and distributed/edge rate limits are required before scaling.
- Mutations require exact Origin, JSON content type and the session CSRF header.
  Login requires Origin and is rate-limited. Bodies are bounded at 16 KiB.
- Every tenant endpoint rechecks active membership. UUIDs from the browser select
  among authorized clinics; they do not grant authority. All data requests have
  explicit clinic predicates as well as database RLS.
- Roles: owner/manager author/publish; receptionist handles requests; viewer reads
  safe data only. Sensitive contact retrieval is owner/manager/receptionist only.
- Platform overview requires a separate active row in the private
  `platform_admins` table, provisioned by an operator. A clinic owner is not a
  platform administrator. Platform phone reassignment and clinic onboarding remain
  setup operations, not dashboard mutations in this minimum release.

### Pages and workflow

- **Today:** published open/closed status, effective doctor hours and leave/notices,
  new requests and unresolved non-test calls; recent queues bounded to 50 rows.
- **Doctors, Services, Locations, Schedules, Exceptions, Notices, FAQs:** scoped
  draft editors; UUID references are visible in related lists. Save does not publish.
  Public messages and internal notes remain separate. Records marked published in
  authoring are eligible for the next snapshot, not immediately visible to calls.
- **Fees:** append new effective intervals; overlapping active intervals are denied.
  Editing existing fee history is intentionally unavailable. Ending an existing
  open-ended interval currently requires an audited operator workflow.
- **Requests/Callbacks:** safe lists omit names/numbers entirely. Explicit contact
  reveal is audited; details can be hidden again. Human status changes are audited.
- **Calls:** sanitized outcomes, pinned version and test marker, no audio/transcripts.
- **Knowledge & review:** FAQ review and draft publication status. Storage/document
  ingestion and vector search are Phase 5, not a simulated upload button.
- **Publish & history:** reviewed preview/digest, transactional publication, optimistic
  stale-preview rejection, and reviewed rollback into a new immutable version.
- **Agent test:** deterministic published-knowledge/safety testing, explicitly not an
  actual model or phone call. It creates no call/session/usage rows.
- **Settings/Team/Audit/Usage/Platform:** approved language/message drafts, role
  administration, append-only audits, raw units and restricted platform overview.

Pagination is bounded. This first UI favors explicit generic field editors over
a complex schedule calendar. Empty values clear existing optional fields; required
fields and invalid enum/date/reference values are rejected by validation/database.
No endpoint accepts arbitrary table names, tenant changes or unallowlisted columns.

### PII keys and retention

Supply `CLINIC_PII_KEYS` as a JSON object mapping key version to a base64-encoded
random **32-byte** AES key, and `CLINIC_PII_KEY_VERSION` naming the active version.
Provision these externally via a secret manager for the request backend and
authorized-contact dashboard process. Do not reuse database/provider passwords.
A private development-only key has now been generated in an ignored, owner-only
artifact; it was not printed or rotated. Tests use disposable in-memory keys and
fictional numbers only. Missing/malformed keys fail closed.

Retain old versions until their records are re-encrypted or erased; dropping a key
loses access to its data. Do not rotate keys by replacing the only key in place.
Direct authenticated reads of protected columns/profiles are denied, even though
the columns are encrypted; the audited detail RPC is the only staff retrieval path.

Profile reuse and recording remain disabled. New managed session rows currently
have a provisional 30-day retention timestamp. Scheduled PII erasure, configurable
retention, consent/profile reuse and key-rotation maintenance are Phase 6/pilot gates.
Do not use this development project for real patient data.

## Verification performed

- **330 tests passed:** 267 offline + 63 real-development-database tests. Mutating
  database fixtures, memberships, publications and calls were rolled back.
- Tests cover confirmation ordering/interruption/correction, AAD isolation, scope
  denial, duplicate sessions/requests/usage, lifecycle/reconciliation, staff roles,
  direct PII denial, platform authority, Auth revocation handling, CSRF/origin,
  secure opaque cookies, body limits and protected-field rejection.
- Strict mypy: 27 source/script modules; Ruff lint and formatting pass.
- Wheel/sdist build passes; all three HTML/JS/CSS assets verified in the wheel.
- Existing dependency version sets preserved; agent and SIP dispatch diff empty.
- Browser: real unauthenticated shell and mobile layout; synthetic browser-only
  authenticated responses tested navigation, draft modal, text escaping and mobile
  table containment. Synthetic responses were removed afterwards. This is **not**
  proof of real staff-login or live end-to-end voice acceptance.
- One upstream Starlette TestClient/httpx deprecation warning remains; no test
  failures. No unrelated HTTP client migration was introduced.

## Remaining activation checklist

Before enabling clinic voice: verified destination adapter; serialized actual
speech/readback/turn hooks; safe grounded output interception; provider usage event
wiring; multilingual readback and TTS switching; independent fallback audio;
approved clinic wording; configured PII keys; scheduled reconciliation/retention;
real staffed transfer outcome/recovery tests; credential and Auth bootstrap; and
an explicitly assigned real clinic/phone with a supervised inbound-call test.

The dashboard and session-service tests do not certify clinical safety, billing
accuracy, production readiness or current PSTN call health.