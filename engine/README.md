# Par engine

Par is a deterministic risk-curation engine for fixed-rate DeFi paper. Its default action is to
wait in an explicit, underwritten reserve book. It does not use an LLM or AI to originate, size,
rank, or increase risk.
Optional sensors implement only `veto(candidate) -> Veto | None`, and `apply_sensor_vetoes` checks
at runtime that the surviving set is a subset and no weight increased.

## Modules

- `portfolio/` is the deterministic whole-portfolio layer. It defines integer-only canonical
  contracts; registered-asset/finalized-block discovery; per-capability classification; strict
  two-source valuation; allocation, concentration, volatility, drawdown, correlation, historical
  ES95 and component ES; verified-cashflow fixed-income analytics; explicit capital sources,
  mandates, transformations, route costs and lexicographic opportunities; confidential-policy
  scaffolding; LI.FI status parsing; and Decision/Outcome bundle reconciliation. It opens no network
  connection and exposes no signing or transaction builder.

- `ingest.py` normalizes Pendle, Spectra, TermMax, Tenor, Term Finance, Exactly, and LI.FI Earn into
  `MarketRecord`. New HTTP cache writes stay under `engine/data`; the verified
  `research/data/latest` cache is a read-only fallback.
  Pendle and Spectra APYs become fractions at this boundary. Executable capacity is populated only
  when a successful size quote or explicit lender capacity exists; otherwise it is `None`. Callable
  LI.FI venues have no fabricated maturity and carry their measured exit type in metadata.
- `pricing.py` prices zero-coupon PTs with ACT/365 and annual effective compounding. Its Pendle V2
  approximation implements the protocol logit curve and `rateScalar = scalarRoot / years`. It
  prints model-versus-Convert residuals instead of treating an approximation as an executable quote.
- `credit.py` produces a numerical score, BID/NO BID, and slide-ready quantitative reason. Every
  evidence value is timestamped and anti-lookahead is asserted before scoring. The TIER_A/B/C
  thresholds and enum match the rule in `research/validate.py`. Oracle points measure model risk:
  documented-feed heartbeat status, undocumented genuine feeds, variable per-block adapters, and
  hardcoded-par adapters are distinct outcomes. Missing holder, oracle-model, or TVL data is
  reported as unavailable.
- `allocate.py` uses a deterministic 0.5%-weight marginal grid. It ranks the job families
  LEND_FIXED, DISPERSION, CARRY, charges CVaR/illiquidity/concentration, enforces every capacity and
  cap, and leaves the residual in a concentration-limited reserve book. Instant-exit LI.FI venues
  pass through `score_credit` before they can set the hurdle or receive weight; Aave remains the
  named floor/fallback. `gas.py` raises that selected reserve hurdle by the candidate chain's live
  annual fixed gas cost divided by book size. Every funded position carries both components in its
  decision trace.
- `risk.py` computes empirical historical CVaR95 only with at least 100 daily returns (five 5%-tail
  observations). Shorter or non-NAV histories receive an explicit 2.00% conservative policy
  fallback labelled `FALLBACK, NOT A MEASUREMENT`; missing history is never replaced by zero.
  That fallback remains an internal allocator policy charge. The portfolio layer has a separate
  measured ES contract requiring 250 synchronized observations and at least 90% compatible
  authoritative NAV coverage; it never displays the 2% charge as portfolio ES.
- `evidence.py` keeps measured credit defects, insufficient evidence, and mixed rejections
  separately countable; exact missing field identifiers remain attached to each decision.
- `ledger.py` writes publishable JSON Lines with `O_APPEND`, immutable decision IDs, one later
  realization per decision, forecast-realized deltas, and a SHA-256 hash chain that detects edits.
- `curve.py` emits one point for every active fixed-maturity contract identity across the six fixed
  venues. Executable, indicative-only, and no-rate points partition the universe; `NO QUOTE` points
  remain visible and duplicate or omitted identities fail the build.
- `variable_yield.py` reads Aave V3 and Compound V3 through Multicall3 `aggregate3` in chunks of
  75 sub-calls. Every sub-call allows failure, but a failed result omits its reserve and records its
  full reason in `omission_reasons`;
  an outer chunk failure fails the source so the universe collector retains prior rows. Sequential
  reads are used only when `eth_getCode` proves Multicall3 is absent, and that mode is reported.
  Aave depth remains underlying `balanceOf(aToken)` while TVL remains aToken `totalSupply`;
  Compound depth remains `totalSupply - totalBorrow` while TVL remains `totalSupply`.
- `rpc_endpoints.py` provides deterministic public rotation for engine collectors. Set
  `ETHEREUM_RPC_URL`, `BASE_RPC_URL`, or the corresponding chain environment variable to place a
  configured endpoint first without committing a paid URL or key. Only transport/capacity failures
  rotate; malformed or semantic contract responses fail closed.
- `demo.py` runs entirely from cache and prints ingestion coverage, quote residuals, the pre-event
  xUSD veto, current-live rejection cost, fixed-versus-selected-reserve comparisons, the Aave
  base/total floor, and allocation traces.

## Run

From the repository root:

```bash
./.venv/bin/python -m unittest discover -s engine/tests -v
./.venv/bin/python -m engine.curve
./.venv/bin/python -m engine.ingest --offline
./.venv/bin/python -m engine.pricing
./.venv/bin/python -m research.round2 --offline
./.venv/bin/python -m engine.demo --offline
./.venv/bin/python scripts/build_universe.py --venue aave-v3
./.venv/bin/python scripts/build_universe.py --venue compound-v3
```

Create `.venv` with Python 3.12 and install `requirements.txt` on a new machine. `matplotlib` is used
only by the research case-study code and is configured there with the headless `Agg` backend.

## Allocation defaults

| Knob | Default | Why |
|---|---:|---|
| `lambda_cvar` | 1.00 | charge one unit of expected return per unit of modeled tail loss |
| `mu_illiquidity` | 0.01 | charge up to 100bp for a fully illiquid asset |
| `nu_concentration` | 0.02 | convex cost prevents filling one underlying first |
| `min_reserve_weight` | 50% | waiting is the base case when fixed edge is absent |
| `per_asset_cap` | 20% | no underlying dominates the portfolio |
| `reserve_venue_cap` | 25% | no callable reserve venue dominates the portfolio |
| LEND_FIXED cap | 25% | highest edge, still capacity- and asset-capped |
| DISPERSION cap | 20% | venue convergence is execution-dependent |
| CARRY cap | 15% | lowest-edge job receives the smallest risk budget |
| `carry_edge_threshold` | 50bp | fixed minus the selected callable reserve minus credit must clear a real hurdle |
| `step_weight` | 0.5% | at most 200 deterministic iterations for a full portfolio |

The demo maps score shortfall to a disclosed credit penalty of up to 200bp. That mapping is a policy
default, not market evidence. Its disclosed 100-operation gas scenario is 40 lends, 40 redeems and
20 rebalances; a rebalance is derived as divest plus invest. Live gas and ETH/USD are collected by
`research.round2`, and all raw pulls or failures are timestamped under `research/data`. CVaR uses
measured PT close returns when the minimum history exists and the labelled 2.00% fallback otherwise.

LI.FI reserve eligibility uses the measured section-7 universe: USDC, at least $10M reported TVL,
and a one-transaction exit. Smaller or staged venues remain visible evidence but cannot become the
reserve. If the API key or usable evidence is absent, ingestion reports `COULD NOT VERIFY`, other
venues continue to run, and reserve selection falls back to capped Aave plus explicit idle USDC.

Credit prices are dual-sourced through DefiLlama and CoinGecko using exact contract identities (or
CoinGecko IDs for numeraires). Every aligned day must remain within the explicit 100bp policy
tolerance; when it does, the lower observation is retained. One out-of-tolerance day, too few
aligned days, or an unavailable second source leaves the field `COULD NOT VERIFY`. Keyless
CoinGecko is dynamically rate-limited, so all pulls are paced, shape-validated, timestamped, and
cached.

Holder evidence prefers authenticated Etherscan V2 when `ETHERSCAN_API_KEY` is present in the
environment or repo `.env`. Without it, the engine declares `explorer_authentication` unavailable
and continues through the existing Ethplorer/Blockscout free path. No API key is written to cache.

## Known limits

The active Pendle endpoint does not expose pool reserves, `scalarRoot`, the SY index, or all fees.
The pricing validation therefore uses reported liquidity, a disclosed 50/50 value split, and
`scalarRoot=50`; the residual table quantifies the resulting model risk. A Convert quote remains the
execution authority. Spectra has no executable quote endpoint in the supplied contract, and Tenor's
`withdrawable` is not USD-denominated, so their executable capacity remains unavailable rather than
being inferred. Historical xUSD protocol TVL, oracle updates, and holder concentration are absent
from the supplied cache; the pre-event veto is driven by the observed 30-day peg deviation, while
those fields are explicitly unavailable.
