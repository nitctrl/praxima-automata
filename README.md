# AI Clinic Receptionist: voice agent

This is the LiveKit voice agent that answers inbound calls for small clinics. It is an
**administrative assistant**, not a medical device, a clinical decision-support
system or a triage service.

Call path: caller → Plivo → SIP trunk → LiveKit → this worker (`inbound-agent`).

The platform team owns the database migrations, dashboard, publication workflow,
document upload and message sending. [docs/schema-contract.md](docs/schema-contract.md)
defines the interface the agent relies on. It also lists the SQL changes the platform
must make; see [Before the first real call](#before-the-first-real-call).

## What the agent does

- It finds the clinic from the **called number and SIP trunk**. It never uses caller
  ID or anything the caller says to choose a clinic. For each call it loads that
  clinic's single published configuration and keeps using it until the call ends.
- It answers clinic questions only from published facts and reviewed documents, using
  hybrid lexical and semantic retrieval.
- It lists free appointment slots and books one after reading it back to the caller.
  The platform sends the confirmation message.
- It takes callback requests, for example when the caller wants a person. Call
  transfer is not implemented.
- It follows the caller's language (Hindi, English, mixed and other languages Sarvam
  supports), limited to the clinic's supported languages.
- Names and phone numbers are encrypted before they are written to the database. The
  agent stores no transcripts or audio.

## What it refuses

Emergency phrases, including Hindi and romanized Hindi, get the clinic's approved
emergency message. This message is spoken **without calling the model**. For medical
questions (diagnosis, medicine, dosage, test results, treatment) the model receives a
guard instruction and gives no medical content. Attempts to change the agent's
instructions or to get another clinic's data are handled the same way. The agent does
not invent doctors, fees, schedules or slots.

## Failure behaviour

A caller never waits in silence:

| Situation | What the caller hears |
| --- | --- |
| Unknown or inactive number, or bad SIP data | Fixed "not available" message, then hang-up |
| Clinic at its concurrent-call or monthly-minute limit | Fixed "lines busy" message, then hang-up |
| Database or configuration unavailable | Fixed "technical problem" message, then hang-up |
| Configuration still loading after 1.5 s | Generic greeting; clinic tools are attached once loaded |
| Language-model error | "Could you say that again?" |
| Session closes with an error | The room is deleted, which ends the call |
| Caller silent for 20 s | "Are you still there?"; still silent 30 s later: goodbye and hang-up |
| Maximum call duration | Closing message and hang-up (`CLINIC_MAX_CALL_SECONDS` or the platform deadline) |
| Call record unavailable | The conversation continues; callback requests report unavailable |

## Run

Python 3.10+ and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --locked                 # add --extra semantic for Qdrant semantic search
cp .env.example .env             # then fill in values; never commit them
uv run src/agent.py download-files
uv run src/agent.py start        # production worker
uv run src/agent.py console      # local microphone; needs CONSOLE_CLINIC_ID
```

In deployment, provide every setting through secret management. Settings are read
from the process environment first. `DATABASE_URL` must be the restricted
`clinic_runtime` role behind the Supabase pooler, never an owner or migration login.
Use port 6543 (transaction pooler) when many calls run at once. LiveKit runs one
process per call, and each process holds up to two connections.

## Deploy

- Run the worker under a supervisor that restarts it, for example LiveKit Cloud
  agents or a container with a restart policy. Emit logs as JSON (`LOG_FORMAT=json`);
  logs mask phone numbers.
- The SIP dispatch rule ([sip/dispatch-rule.json](sip/dispatch-rule.json)) must target
  `agent_name: inbound-agent`.
- [sip/inbound-trunk.json](sip/inbound-trunk.json) contains placeholders only. Fill in
  the clinic numbers, **restrict `allowed_addresses` to Plivo's signalling ranges**
  and set trunk authentication before going live.
- **Rollback:** redeploy the previous image or commit. Configuration rollback belongs
  to the platform and affects new calls only.

## Before the first real call

These gates are tracked in [docs/schema-contract.md](docs/schema-contract.md):

1. The platform's `start_call` must accept snapshot schema version 3. Until it does,
   every call runs in degraded mode: no call record, callbacks or usage.
2. Add the `calendar_bookings` overlap exclusion constraint.
3. Make the concurrency limit configurable, and keep `CLINIC_MAX_CONCURRENT_CALLS`
   equal to it.
4. Restrict and authenticate the SIP trunk (see Deploy).
5. Run a pilot of at least 50 calls covering Hindi, English, mixed speech, noise,
   silence, interruptions, concurrency, disconnects, database outage and long calls.

## Verify

```sh
uv run ruff check
uv run mypy
uv run pytest -q
```

The tests run offline and use fictional data. They do not prove that real calls work
end to end.

Further reading: [architecture](docs/product-architecture.md) and
[schema contract](docs/schema-contract.md).
