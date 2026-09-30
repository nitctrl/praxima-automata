# AI Clinic Receptionist documentation

This documentation describes the voice-agent runtime and the work required to operate
it as a safe multi-clinic administrative reception product.

## Start here

- [Market readiness](market-readiness.md) — deployment, platform integration,
  third-party integrations, onboarding and launch blockers.
- [Voice-agent architecture](product-architecture.md) — current runtime design and
  system boundaries.
- [Platform contract](schema-contract.md) — database, snapshot, encryption, cache and
  SIP interface that the fullstack platform must implement.

> The product is an administrative assistant. It does not diagnose, triage, prescribe,
> interpret medical results or give treatment advice.

## Documentation preview

Run the Zensical preview server on a browser-safe local port:

```sh
uvx --from zensical zensical serve --dev-addr 127.0.0.1:8002
```

Then open <http://127.0.0.1:8002/>.
