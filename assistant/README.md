# Fenorium local assistant contract

The assistant accepts only a versioned, deterministic `AssistantEnvelope`:

```json
{
  "schema_version": "fenorium/assistant-envelope/1",
  "method_version": "engine-method-1",
  "as_of": "2026-09-01T12:00:00Z",
  "facts": [
    {
      "field_ref": "asset/example/status",
      "status": "BID",
      "reason_code": "MEASURED",
      "value": 125000,
      "unit": "USD",
      "source_ref": "source:engine/example"
    }
  ]
}
```

`tools.mjs` is pure and can be imported without QVAC. It validates stable field references,
precomputed scalar values, statuses, reason codes, and source references; it only selects facts and
checks explanations. It performs no portfolio arithmetic.

`serve.mjs` listens on `127.0.0.1:8787`, uses an explicit origin allowlist, and requires
`X-Fenorium-Pairing-Token` for `/ask`. The token is random per process unless
`FENORIUM_ASSISTANT_PAIRING_TOKEN` is supplied. QVAC is loaded lazily; without it, `/ask` returns
`assistant-unavailable` plus the deterministic selected facts. `/health` contains no pairing token,
origin list, envelope, or model diagnostics.

For deterministic local checks, `FENORIUM_ASSISTANT_DISABLE_QVAC=1` forces the same unavailable path
for the service or CLI without attempting to load the optional runtime.

The CLI reads an envelope file:

```bash
node assistant/ask.mjs assistant-envelope.json "Explain the recorded status"
```
