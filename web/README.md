# Fenorium portfolio terminal

This directory contains the static, portfolio-first Fenorium terminal. It is an analytical v1: Market
is the default, wallet-optional landing and starts with the 253-instrument fixed-income population
(`native` plus fixed-income `rwa`). The 42 variable-yield vaults remain available from the category
filter. The market remains read-only; the only transaction-capable surface is the explicitly
labelled Base Sepolia demo card for the two exact demo instruments.

## Scope and boundaries

- Discovery is explicitly `REGISTERED_ASSETS_ONLY`: known addressed universe rows plus explicit native
  assets across Ethereum (1), Optimism (10), BNB Smart Chain (56), Base (8453), and Arbitrum (42161).
- Each wallet scan records a finalized block reference and keeps every RPC/chain error visible. It does
  not enumerate arbitrary tokens, LPs, NFTs, debt, or non-EVM accounts.
- Prices, raw units, USD micros, and basis points are represented as integer-authoritative values in
  `portfolio.js`. Stablecoins require observed evidence; no `$1` peg is assumed.
- Unknown or unpriced positions produce a `PRICED_NAV_LOWER_BOUND`, never a misleading complete NAV.
- Expected Shortfall is fail-closed. The browser accepts only a backend `MetricResult` with at least 250
  observations and 9000 bps compatible coverage; it does not fabricate history or show the old 2%
  policy fallback as ES.
- Opportunity scenarios require an explicit source. Variable-income sources require opt-in and a max
  amount. `NO BID` and `NO QUOTE` stay hard gates; missing route-cost fields are `ANALYTICAL_ONLY`.
- Evidence previews are structured and `UNANCHORED`. Execute is visibly disabled with legal, quote, and
  destination-failure blockers; `execution_enabled` and `fees_enabled` remain false.
- Market, inspector, asset-summary and comparison cost/net figures identify measured LI.FI ladder
  size costs or the modelled size/depth fallback; CSV records `rating.cost_model`. Entry costs also
  include the stated base fee and applicable Fenorium routing fee. These are not live executable
  quotes. The independent `depth_source` is
  displayed beside those figures; depth and TVL figures disclose `depth_source` and `tvl_source`
  separately, with missing provenance shown as `UNKNOWN · Not measured`. No color is required to read
  the distinction.
- The wallet state machine uses only an injected `window.ethereum`: `eth_accounts` is read silently on
  load, `eth_requestAccounts` runs only after an explicit Connect click, and account/chain events are
  applied without reloading the page. Wrong-chain actions target Base Sepolia (`0x14a34`).
- On a connected chain, exact addressed universe rows with recorded decimals are read at the provider's
  `finalized` block through Multicall3 `aggregate3` in chunks of 75. Every sub-call allows failure; a
  failed balance stays visible as `UNRESOLVED` with its adapter error and is never decoded as zero.
  The browser posts only the resulting observations to same-origin `/v1/portfolio-analytics`; the
  service never receives an RPC key or gains wallet/signing authority. Results are scoped by account,
  chain, and request generation, so a late read cannot cross an account switch.
- The browser sends no price observation unless one actually exists. The current universe is not a spot
  price feed, so a non-zero balance without independent price evidence is `UNKNOWN`, lowers coverage,
  and prevents authoritative NAV. Stablecoin names never create a one-dollar price.
- `web/config.json` is the deployment source of truth for demo payment-token, sale, and faucet
  addresses. The current payment-token and sale addresses are intentionally null because those
  deployments are not recorded. No address is inferred. When an owner records verified addresses, the
  card reads balance, allowance, exact quote, instrument balance, and gated eligibility before a
  distinct approval and purchase transaction; receipts are confirmed before balances are refreshed.
  The optional faucet can call only the configured public MockUSDC `mint(address,uint256)` method.
- Mainnet instruments always show `Mainnet execution disabled`. Demo fills are labelled `Base Sepolia ·
  testnet · demo instrument` and are not mainnet price, liquidity, or verdict evidence.
- Market comparison keeps a visible checkbox column outside the column chooser, retains 2–5 selections
  across pagination, and shows side-by-side deterministic fields without ranking or selecting a winner.
- The local assistant receives only an `AssistantEnvelope` over the loopback URL after a user supplies
  `X-Fenorium-Pairing-Token`; the token is not persisted. It has no portfolio array, execution,
  quoting, signing, routing, or calldata capability. The deterministic browser UI remains complete
  when it is absent.

The existing Base Sepolia issuer and its tests remain isolated in their original files. This terminal
does not transact on mainnet and does not use the assistant to compute or transact.

## Run locally

Serve `web/` with any static HTTP server; no packages or CDN assets are required. For example:

```bash
python3 -m http.server 8516 --directory web
```

Open `http://127.0.0.1:8516/`. A disconnected Overview renders no portfolio values. The fixture is
loaded from `data/sample-portfolio.json` only after **View a sample book** is selected, remains labelled
`SAMPLE BOOK · LOCAL_DEMO_FIXTURE`, and can be dismissed in one click. The fixed-
income view requests `/v1/universe`, and demo deployment fields are read from `config.json`.

## Focused checks

From the repository root:

```bash
node --check web/app.js
node --check web/ui.js
node --check web/portfolio.js
python3 -m unittest discover -s web/tests -v
git diff --check
```

The focused checks do not call an RPC, fetch a remote endpoint, run a browser, or prove a real wallet
balance. They cover the deterministic kernel, browser contracts, ABI helpers, wallet transitions,
bounded receipt flow, comparison rendering, assistant fallback, and the preserved mainnet blockers.
