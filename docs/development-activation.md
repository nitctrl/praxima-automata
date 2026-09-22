# Fictional development activation

## Scope and current state

The operator selected **fictional clinic A only**. The existing voice POC, its
`inbound-agent` dispatch and real phone routing remain untouched. No real number
is assigned to a clinic in this development database. Do not use real patient data.

Prepared and verified:

- Six applied migrations checked by SHA-256; existing runtime credential retained.
- Private `.env.clinic-dev` generated with owner-only `0600` permissions; rerunning
  setup validates it without overwriting or rotating keys.
- English/Hindi test outage WAVs generated through Sarvam streaming, cached in
  ignored `.clinic-dev-audio/`; playback uses local PCM frames, not a network call.
- Guarded owner bootstrap and v2 publication helpers tested with rollback-only
  confirmed Auth identities. No persistent identity is seeded by those tests.
- Separate console-only voice adapter; source tests cover actual SDK hook contracts,
  confirmation ordering, interruption/error paths, language switching and usage.

**Activated on 2026-09-18:** the operator-created, verified Auth account was
assigned active owner access to fictional Clinic A. Its v2 configuration was
published and successfully read through the restricted runtime role. The operator
reported dashboard sign-in; authenticated browser workflows have not yet been
independently verified. No real phone routing was changed.

**Still pending:** authenticated dashboard workflow checks, listening review of
test audio, and an actual microphone session. Tests and audio rendering alone are
not evidence of successful end-to-end voice operation.

## Private account setup

From the project root, substitute the exact dedicated DEV project reference and
the chosen owner email (neither is a password):

```sh
uv run python scripts/activate.py register --confirm-development-project <dev-ref> --email <owner-email>
```

The password prompt is hidden and requires a terminal. Enter a **new password of
at least 12 characters directly in that terminal**, never in chat, command-line
arguments, source or browser automation. The command sends it only to the selected
project's official Supabase Auth signup endpoint. It never prints response tokens.
Confirm the email from Supabase. Existing users are not reset or silently confirmed.

After verification:

```sh
uv run python scripts/activate.py bootstrap --confirm-development-project <dev-ref> --email <owner-email>
uv run python scripts/activate.py publish --confirm-development-project <dev-ref> --email <owner-email>
uv run python scripts/dashboard.py
```

Bootstrap accepts only fixture A, test-provider routes and a confirmed, nondeleted,
nonbanned Auth account. Conflicting existing membership requires manual review.
It records an audit entry; it is not public self-enrollment. Publication switches
to the verified user's authenticated DB role, validates a typed public snapshot
and uses the normal publication RPC. Neither command changes a phone or dispatch.
Publication of an already valid v2 snapshot is idempotent.

Open http://127.0.0.1:8080 and enter credentials privately. Restarting the dashboard
loads the development encryption key and invalidates previous in-memory sessions.
The dashboard receives no migration/runtime credential in its environment.

## Console voice test

Review the cached English/Hindi outage audio locally before testing. To generate
missing files without overwriting existing files:

```sh
uv run python scripts/render_dev_audio.py --confirm-development-project <dev-ref>
uv run python scripts/clinic_voice.py console
```

The wrapper accepts **only** the bare `console` argument or `--help`. Although SDK
help lists its general commands, `start`, `dev`, `connect`, `--record`, and extra
arguments are rejected by this wrapper. Console runs an unregistered fake job;
the adapter does not connect a cloud room or accept SIP participants. Its exact
fixture route is selected by trusted local code, never by caller metadata.

Recording is explicitly disabled. SDK diagnostic logging is suppressed because
upstream logs may contain recognized speech or raw provider errors. Console may
display speech transiently; use only fictional inputs. Audio/text still goes to
the configured STT/TTS service: this is not an offline speech-recognition system.
No application transcript, caller profile, model prompt or recording is persisted.

The interface is intentionally narrow, **not a full natural-language agent**:

- Say `English` or `Hindi` to switch the voice and readback language.
- Say `hours` / `समय`, or `doctors` / `डॉक्टर` for published facts.
- Say `callback` / `वापस कॉल`; supply a fictional name and an E.164 number.
- Say `appointment request` / `अपॉइंटमेंट अनुरोध`; select a doctor by numeric
  index, give `today`, `tomorrow`, `आज` or an ISO date, then name and number.
- The minimum appointment flow proposes **new patient**, without a time preference;
  this is included in the readback. It does not infer availability or reserve slots.
- Listen to the entire exact readback, then affirm. Interruptions, corrections,
  language changes, unsafe speech or provider errors invalidate confirmation.
  A nonconfirmation discards the pending request; begin again to correct it.
- Success means a request was stored, **never a confirmed appointment**.

STT may not reliably render numeric indexes, ISO dates or E.164 numbers. This
limited interface exists to verify orchestration before adding richer intent and
field parsing. It uses no LLM at all and makes no natural-language quality claim.
Fees/service/location/FAQ functions remain available in the structured backend
and dashboard test; they are not fully exposed through this small voice menu.

The call pins one published snapshot, runs deadline/inactivity/heartbeat checks,
encrypts confirmed names/numbers before SQL, ingests bounded provider usage and
finalizes on shutdown. Test sessions are marked `is_test`; usage is unpriced raw
units, not billing. TTS outage fallback is cached independently of the failed
provider. Startup without required assets/publication fails closed, before speech.

## Remaining production gates

- A separate approved real clinic/configuration and explicit phone assignment.
- Trusted called-destination/trunk adapter and supervised inbound Plivo call tests.
- Broader language/safety evaluations, approved clinic emergency scripts and audio.
- Verified staffed transfer answer/failure/recovery semantics; currently disabled.
- Scheduled abandoned-call reconciliation using a separate maintenance credential;
  the SQL function exists but no scheduler has been installed.
- Retention erasure, production key management/rotation, HTTPS/session scaling,
  provider privacy settings and observability without sensitive content.

No production readiness, clinical certification, PSTN health, real-user login or
successful microphone-session claim follows from the automated tests.