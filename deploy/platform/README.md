# Fenorium local platform service

This package serves the portfolio-first static terminal and the deterministic `/v1` API on
`127.0.0.1:8516`. It does not hold keys, sign transactions, broadcast routes, charge fees, or retain
raw portfolio snapshots. The only writable container path is the explicit append-only evidence
volume; temporary request bodies remain in memory.

The image is `fenorium-platform:0.1.0` from `python:3.12.11-slim`, running as UID/GID `10001` with
all Linux capabilities dropped, `no-new-privileges`, a read-only root filesystem, and bounded `/tmp`.
The optional assistant remains a separate loopback process on `127.0.0.1:8787`.

## Local process

From the repository root:

```bash
./.venv/bin/python -m deploy.platform.service
```

Open `http://127.0.0.1:8516/` or check `http://127.0.0.1:8516/health`. POST operations require
`Content-Type: application/json` and a bounded visible `Idempotency-Key`. The service supports:

- `GET /v1/universe` for the unchanged shipped-universe response;
- `GET /v1/universe?size_usd=<integer>` to re-rate every shipped row at a whole-dollar size from
  `1` through `1,000,000,000`, without network calls;

- `fenorium/platform-wallet-snapshot-request/1` at `POST /v1/wallet-snapshots`;
- `fenorium/platform-analytics-request/1` at `POST /v1/portfolio-analytics`;
- `fenorium/platform-scenario-request/1` at `POST /v1/scenarios`;
- `fenorium/platform-opportunity-request/1` at `POST /v1/opportunities`;
- `fenorium/platform-execution-quote-request/1` at `POST /v1/execution-quotes`;
- `fenorium/platform-decision-bundle-request/1` at `POST /v1/decision-bundles`.

Representative exact request bodies live in `deploy/platform/tests/test_api.py`. The service rejects
floats and unknown fields before an engine adapter sees them. Execution quotes keep the gated
`transaction_request` null and cannot broadcast. An optional `router_quote` (recorded LI.FI response)
and `requested_from` (the address used to request it) expose checked evidence under
`unsigned_transaction_request`. The router response is bound using the analytical quote's
observation/expiry times and must reproduce its `route_hash`. Missing or invalid transaction
evidence returns null. The evidence uses LI.FI field names with decimal-string value/gas quantities;
it is not an instruction or authorization to execute. Literal examples and refusal tests live in
`engine/tests/test_unsigned_transaction_request.py`.

When `FENORIUM_EVIDENCE_DIR` is configured, each validated quote request
is also retained there as a content-addressed, write-once `*.quote.json` observation beside the existing
`*.decision.json` artifacts. The artifact reuses the request schema and keeps notional, available depth,
and every cost component as integer USD micros; `source`, `observed_at`, and `expires_at` travel with it.
Identical retries resolve to the same bytes and file rather than replacing history. `GET
/v1/executions/{id}` returns `EXECUTION_DISABLED`.

The static client derives registered chain/address identities from the served universe, obtains the
wallet's observations through its injected provider, and posts `{request, observations}`. The platform
validates each supplied registered-asset identity against the engine's chain/kind/address identity,
then classifies and values the recorded observations. It never contacts an RPC itself. Empty
`price_observations` remain missing evidence rather than a stablecoin peg assumption.

The sized universe response preserves the standard disabled-execution envelope, freshness fields, source
hash, and each row's independent `depth_source` / `tvl_source` fields. It also echoes `size_usd`, the
90-day open-ended holding period, realized-rate opt-in, the 30 bp stated base fee, the 10 bp routing
fee assumption, and the exact fallback size-model assumptions. Each rerated row carries
`rating.cost_model`, `rating.size_cost_bps` and its measured or modelled basis. The routing fee applies
to LI.FI token purchases; position deposits, lending orders, declared demos and stablecoins are exempt.
Net yield deducts entry costs only; risk remains in the Score and verdict. Neither a measured ladder
increment nor a modelled size cost is a live executable quote. Invalid syntax returns `INVALID_SIZE_USD`; zero or values above the maximum return
`SIZE_USD_OUT_OF_RANGE` in the platform error envelope's `reason_code`.

## Routing (`/v1/execution-routes`)

`/v1/execution-quotes` analyses a route the **caller** supplies, which is why it can never
authorise anything: every input, including the hash tying them together, arrives in the request
body, so the hash shows the caller's own objects agree with each other and nothing more.

`/v1/execution-routes` is the other half. The caller names a wallet, what it holds, and an
instrument by chain and address; this server resolves that instrument against the universe file
it serves, fetches the route from LI.FI itself, and holds the payload. Authorisation is then the
execution gate's decision rather than structurally impossible.

Outbound requests are **off by default**. A deployment opts in:

| Variable | Effect |
| --- | --- |
| `FENORIUM_ROUTER=lifi` | Enables the LI.FI router. Anything else, including unset, leaves the endpoint refusing with `ROUTER_UNAVAILABLE`. |
| `FENORIUM_LIFI_API_KEY` | Optional. Without it LI.FI answers on its anonymous rate limit. |
| `FENORIUM_LIFI_INTEGRATOR` | The integration's name exactly as the LI.FI Partner Portal fixed it. Without it LI.FI files every route under its own default, `lifi-api`, even with our key on the request. |
| `FENORIUM_LIFI_FEE` | The routing fee as a plain decimal fraction, above 0 and at most `0.01`; requires the integrator. Unset charges nothing. |

### Paying from another chain

`fenorium/platform-execution-route-request/2` adds `pay_with.chain_id`: the chain the payment starts
on. v1 is unchanged and means the instrument's own chain. Instruments live on the five
`INSTRUMENT_CHAINS` (1, 10, 56, 8453, 42161); a buyer may also pay from the 18 `PAY_FROM_CHAINS`
(Gnosis, Unichain, Polygon, Monad, Sonic, X Layer, zkSync Era, World Chain, HyperEVM, Pharos, Morph,
Mantle, Arc, Celo, Avalanche, Ink, Linea, Scroll). LI.FI pays Fenorium's fee to the Safe's address on
the chain the buyer pays from, so a chain is only added after two checks, both done 2026-09-29 for
these: the Safe factory there simulates deploying the Fenorium Safe at exactly `0xC84E…71aE`, and a
USDC quote from it credits the `par` fee. Thirteen EVM chains LI.FI routes fail the first check
(Etherlink, Blast, Mode, Soneium, Metis, Plume and others) and must never be added: fees paid there
could not be moved. The server
asks LI.FI for that exact pair of chains and refuses an answer describing any other pair. A route
that crosses chains must name the paying wallet as its destination (`DESTINATION_NOT_WALLET`
otherwise), and it is authorised only when `DESTINATION_FAILURE_EXPERIMENT` is cleared (see
*Execution scope*). Responses carry `pay_chain_id`, fees carry `token_chain_id`, and the route
carries LI.FI's `execution_duration_s`.

`GET /v1/executions/{source_tx}?from_chain=<id>&to_chain=<id>` reports where a crossing stands, from
LI.FI's status API: `status`/`substatus` as LI.FI states them, `state` (`SETTLED`,
`FAILED_DESTINATION` for a partial delivery, `REFUNDED`, or a pending state), `terminal`, and
`received` -- the token that actually arrived, in its own units, which after a failed destination
step is not the instrument. Terminal answers are written to the evidence directory. It answers only
while same-chain execution is open, and shares the router's one-request-per-1.2-seconds pacing.

### Fees

Every route LI.FI quotes carries its own fixed fee — 0.25% when measured on 2026-09-24 — taken
from what the user pays. `/v1/execution-routes` now states it: each fee named, split between LI.FI
and the integrator, with dollar figures rounded up. A route that does not state its fees is not
fully costed (`MISSING_COST`), exactly like one that will not state its gas.

Charging our own fee is `FENORIUM_LIFI_FEE`. LI.FI collects it in its own contract — Fenorium never
sits in the fund path — and refuses the request outright (HTTP 400, code 1011) until the
integration's **fee wallet** is set in the Partner Portal. Every route is checked against the
setting: an integrator share nobody configured, a missing one, one above the configured rate, or
one taken in a different token is `FEE_MISMATCH`, and the route is not offered.

LI.FI reports its fixed fee and ours as **one** entry (`LIFI Fixed Fee`, at the combined rate) and
splits it in `feeSplit`. The integrator's share is credited to the integrator this server names, so
the response restates it as `fenorium_fee_usd_micros`: Fenorium's part of the fees listed, not an
amount added to them. A deployment that names no integrator reports 0 there.

A deployment that charges a fee answers with `fenorium/platform-response/2`, in which
`fees_enabled` can be true. The v1 envelope pins it to false and is never edited, so a client built
for v1 refuses a fee-charging deployment instead of misreading it. Turning a fee on therefore also
needs the client's `web/config.json` to agree — deliberately two places, as with execution.

The router paces the whole process to one outbound request every 1.2 seconds and refuses with
`ROUTER_BUSY` rather than queueing, so an unauthenticated POST cannot turn this service into an
amplifier against a rate-limited vendor. A purchase this server has already refused -- an
instrument outside the universe, a transfer gate that was never established, an engine `NO BID`
-- costs no outbound call at all.

### Execution scope

`FENORIUM_EXECUTION_CLEARED` declares the clearances a deployment has, comma-separated:
`LEGAL_REVIEW`, `SECURITY_AUDIT`, `DESTINATION_FAILURE_EXPERIMENT`. Nothing declared is nothing
cleared, and a misspelled name is a startup error. Which ones an instruction needs depends on
whether it crosses chains:

| Instruction | Needs |
| --- | --- |
| Same-chain route (`/v1/execution-routes`), direct deposit (`/v1/position-deposits`) | `LEGAL_REVIEW`, `SECURITY_AUDIT` |
| Cross-chain route | all three |

A same-chain route settles in one transaction: it delivers at least the quoted minimum or reverts,
and LI.FI documents each chain's phase as atomic within itself ("Source tokens remain in the user's
wallet (for same-chain)"). The destination-failure experiment asks what happens when a bridged
destination leg fails, which a single transaction cannot do. Whether a route crosses chains is read
from the route LI.FI returned, not from the request. The envelope's and `/health`'s
`execution_enabled` report same-chain execution, because that is everything this deployment issues.
`/v1/execution-quotes` still names all three: it analyses caller-supplied routes, which may cross
chains, and never authorises.

## Owner-run container gates

The repository policy treats image builds and resident services as owner-run commands:

```bash
docker compose -p fenorium-platform -f deploy/docker-compose.platform.yml config --quiet
docker compose -p fenorium-platform -f deploy/docker-compose.platform.yml build
docker compose -p fenorium-platform -f deploy/docker-compose.platform.yml up -d
docker logs --tail 200 fenorium-platform
```

Stop it without deleting the evidence volume:

```bash
docker compose -p fenorium-platform -f deploy/docker-compose.platform.yml down
```

No public port, reverse proxy, DNS change, Vela stack, or mainnet integration is part of these local
commands.
