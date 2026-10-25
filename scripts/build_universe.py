#!/usr/bin/env python3
"""Assemble every instrument Par knows about onto one page, without flattening the differences.

Three categories reach this table by three different routes, and the temptation is to render them
as one comparable list because they all end in a percentage. They are not comparable, and the
column definitions here are the whole point:

* **rate_kind is never dropped.** A Pendle PT quotes a rate you can lock. A Morpho vault reports
  what it happened to return over a trailing window. A tokenized treasury's issuer publishes a
  target. Sorting them into one column and calling it "APY" is the error this project exists to
  correct, so the kind travels with the number and the UI shows it.

* **depth_usd means "shown to be exitable or acquirable at this size", and nothing else.** For a
  vault that is Morpho's liquidity figure, which is genuinely less than TVL — measured 2026-08-22,
  the largest vault held $432.7M and could honour $188.2M. For an RWA fund, TVL is the *size of the
  fund*, which says nothing about what a buyer can acquire or redeem today. RWA depth is filled
  only from a measured LI.FI route ladder; it stays None when no route quotes or measurement
  fails. Filling it in with TVL would be the single most misleading thing this file could do.

* **the engine's refusal travels with its output.** A sparse risk-adjusted-yield column is more
  truthful than filling evidence gaps with favourable defaults, so native curve points are rated
  here from their full source records and the browser may change arithmetic assumptions only for
  results the engine already found computable.

* **a venue position is not silently treated as a token.** Exactly, Term Finance, Tenor and
  Morpho Midnight expose
  balances inside their contracts rather than transferable instrument ERC-20s. Their missing token
  address therefore means no transfer gate applies; it does not mean the gate went unmeasured.

Writes web/universe.json. Network sources are optional: each one that fails leaves its category
empty and says so, rather than substituting a plausible number.
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # the engine's own classifier decides gate status, not a copy of it
# The scheduled wrapper points this at a same-filesystem candidate. The normal command and
# isolated venue command keep the canonical path, and no secret is accepted through this setting.
OUT = Path(os.environ.get("FENORIUM_UNIVERSE_OUTPUT", ROOT / "web" / "universe.json")).resolve()
CURVE = Path(
    os.environ.get("FENORIUM_CURVE_INPUT", ROOT / "engine" / "data" / "curve.json")
).resolve()

from engine.rating import NET_AFTER_COSTS, RATING_METHOD_VERSION, Assumptions, Rating, rate_instrument
from engine.purchase_paths import routed_purchase
from engine.chain import is_transport_failure
from engine.registry import registry
from engine.midnight import MIDNIGHT_VENUES
from engine.rpc_endpoints import rpc_endpoints
from engine.portfolio.contracts import redact_endpoints
from engine.variable_yield import (
    MulticallChunkError,
    MulticallRead,
    VenueResult,
    _RpcSession,
    aave_v3_rows,
    compound_v3_rows,
    execute_multicall_chunks,
    retain_previous_rows,
    validate_yield_payload,
)

# The defaults the page opens with. `holding_period_days` and `accept_realized` exist so that
# open-ended instruments — every vault and tokenized fund — can be compared at all: a vault has no
# maturity to amortise an entry cost across, and its rate is a trailing observation rather than a
# quote. Both are the reader's assumptions, both are editable on the page, and the Rating records
# which basis each number came from so a trailing return is never silently ranked against a locked
# one. Neither relaxes the depth test.
SHIPPED_ASSUMPTIONS = Assumptions(
    size_usd=250_000,
    execution_cost_bps=30,
    routing_fee_bps=10,
    holding_period_days=90,
    accept_realized=True,
)


def rating_json(rating: Rating) -> dict:
    """Serialize one reproducible rating without turning UNKNOWN components into zero."""
    def optional_int(value: int | None) -> int | None:
        return None if value is None else int(value)

    return {
        "computable": bool(rating.computable),
        "refusal": rating.refusal,
        "headline_bps": int(rating.headline_bps),
        "execution_bps": int(rating.execution_bps),
        "base_fee_bps": int(rating.base_fee_bps),
        "size_cost_bps": optional_int(rating.size_cost_bps),
        "size_cost_basis": rating.size_cost_basis,
        "size_cost_rung_usd": rating.size_cost_rung_usd,
        "size_cost_measured_at": rating.size_cost_measured_at,
        "routing_fee_bps": int(rating.routing_fee_bps),
        "base_fee_annualized_bps": optional_int(rating.base_fee_annualized_bps),
        "size_cost_annualized_bps": optional_int(rating.size_cost_annualized_bps),
        "routing_fee_annualized_bps": optional_int(rating.routing_fee_annualized_bps),
        "net_bps": int(rating.net_bps),
        "rate_basis": rating.rate_basis,
        "size_usd": rating.size_usd,
        "reported_depth_usd": rating.reported_depth_usd_exact,
        "method_version": rating.method_version,
        "cost_model": rating.cost_model,
        "impact_coefficient_bps": int(rating.impact_coefficient_bps),
        "impact_exponent": rating.impact_exponent,
        "impact_rounding": rating.impact_rounding,
        "annualization_rounding": rating.annualization_rounding,
        "components": [[label, int(bps)] for label, bps in rating.components],
    }

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) par-universe/1.0",
      "Content-Type": "application/json"}

# Issuers whose products are tokenized fixed income rather than a DeFi money market.
# Contract addresses for issuers whose gate Par can probe directly. DefiLlama's pool records
# carry no token address, and eligibility is only answerable against a contract. Verified
# on-chain 2026-08-22; the probe reads whatever the contract actually does, so a wrong address
# would surface as a changed classification rather than a silent default.
# Verified against li.quest on 2026-08-23 by quoting $250,000 from USDC on Ethereum. Each one
# returned a real route. Deliberately absent: USTB and bIB01, which transfer freely on chain and
# have no DEX route at any size — being allowed to hold a token and being able to buy one are
# independent facts, and only the second is what a route measures.
RWA_ADDRESSES = {
    # Only the gated instruments live here now. The buyable ones are first-class rows in
    # VERIFIED_RWA; matching them through DefiLlama's pool symbols as well produced a duplicate
    # USDY, because the two paths describe the same token by different keys.
    ("blackrock-buidl", "BUIDL"): (1, "0x7712c34205737192402172409a8F7ccef8aA2AEc", 6),
    ("ondo-yield-assets", "OUSG"): (1, "0x1B19C19393e2d034D8Ff31ff34c81252FcBbee92", 18),
}

# Par-deployed demo instruments. Base Sepolia only, labelled as mocks wherever they appear:
# the permissioned funds above cannot be acquired by anyone who is not a qualified purchaser,
# so the interactive leg is simulated and the simulation says so.
# `symbol` is each contract's own symbol(), read from Base Sepolia. Without it every consumer
# falls back to the row's name, and a terminal that wants a ticker prints thirty characters of
# prose into a column sized for four.
#
# `category` decides which asset class a holding lands in. With only the two treasury mocks the
# demo book had exactly one class and nothing to group, so the exposure chart could not be
# exercised at all. The later rows give it a native fixed-maturity note, a vault, and the demo's
# own unit of account. Every one of them is a mock on chain 84532 and says so in its name.
TESTNET = [
    {
        "name": "mGT — Mock Gated Treasury",
        "symbol": "mGT",
        "address": "0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54",
        "deployed_block": 45828366,
        "rate_bps": 525,
        "tier": "TIER_B",
        "category": "rwa",
    },
    {
        "name": "mPT — Mock Permissionless Treasury",
        "symbol": "mPT",
        "address": "0x5C8Ea45363e98e8647eE3759ac888E12817E86a3",
        "deployed_block": 45828367,
        "rate_bps": 475,
        "tier": "TIER_A",
        "category": "rwa",
    },
    {
        "name": "mFT — Mock Fixed-Term Note",
        "fenorium_score": 78,
        "symbol": "mFT",
        "address": "0x3E3CB7D30181e1dF2dB97FCe867aDCc5A0c08F1E",
        "deployed_block": 46597172,
        "rate_bps": 440,
        "tier": "TIER_B",
        "category": "native",
    },
    {
        "name": "mVY — Mock Variable Yield Vault",
        "symbol": "mVY",
        "address": "0xFd9C6631D34d23Df4A3e27f9C760cEA4CdeFc906",
        "deployed_block": 46597173,
        "rate_bps": 310,
        "tier": "TIER_C",
        "category": "vault",
        # A vault share has no redemption date. Carrying one would put it in a maturity bucket
        # it does not belong to and let a ladder metric count it.
        "maturity": None,
        "maturity_timestamp": None,
    },
    {
        # The demo's unit of account, not something the demo offers to sell. It is priced at
        # exactly one dollar because that is what the sale contracts define a dollar to be here,
        # and it accretes nothing.
        "name": "mUSDC — Mock USDC",
        "symbol": "mUSDC",
        "address": "0xfECb2EfF5603b44daD440561cA3cD889F29239ac",
        "deployed_block": 46270271,
        "rate_bps": 0,
        "tier": None,
        "category": "stablecoin",
        "maturity": None,
        "maturity_timestamp": None,
        "unit_of_account": True,
    },
]

# Each sale contract stores its own `pricePerWholeInstrument`, and both were deployed declaring
# one whole instrument to cost 1.000000 mUSDC. Those two separately deployed declarations are
# what fixes the demo's dollar; they are not a market, and if they ever diverged the pricing
# engine's alignment rule would refuse the price rather than pick one.
# Fixed-point at DEMO_PRICE_SCALE, the same scale the instrument prices use: this is $1.00.
DEMO_UNIT_PRICE_FIXED = 100_000_000
DEMO_UNIT_SALES = (
    ("gated", "0x4c4b088C374363e70e3Fd4daDC6C71b3970f91Db"),
    ("permissionless", "0xfeCD071FCBcDfc27A2eA189E527A1E7EB90299E5"),
)
DEMO_UNIT_PRICE_SOURCE = (
    "DECLARED TESTNET DEMO UNIT OF ACCOUNT · both demo sale contracts declare one whole "
    "instrument to cost 1.000000 of this token · not a market observation and not a peg check"
)

DEMO_MATURITY = "2030-12-31"
DEMO_RATE_SOURCE = "DECLARED TESTNET DEMO RATE · synthetic · not a market observation"
DEMO_MATURITY_SOURCE = "DECLARED TESTNET DEMO MATURITY · synthetic · not a market observation"
DEMO_DAYS_SOURCE = (
    "DERIVED FROM DECLARED TESTNET DEMO MATURITY · synthetic · not a market observation"
)
DEMO_TIER_SOURCE = (
    "DECLARED TESTNET DEMO TIER · synthetic · not a market observation or observed credit assessment"
)
DEMO_SCORE_SOURCE = "DECLARED TESTNET DEMO SCORE · synthetic · not an evidence assessment"
DEMO_DEPTH_SOURCE = (
    "DECLARED TESTNET DEMO DEPTH · synthetic withdrawable-liquidity scenario · "
    "not a market observation"
)
# The demo price accretes from this date at each instrument's own declared rate. It used to be
# a flat $1.00 forever, which made every derived figure -- P&L, return, the whole performance
# series -- exactly zero, and correctly so: a constant price produces no gain. A tokenised
# treasury declaring 525bp does not hold a constant price, it accretes, so publishing a flat one
# contradicted the instrument's own stated economics. This is still a declared synthetic price
# and carries that provenance; it simply now follows the rate it already declares.
# The instruments were minted to their first holder on 2026-08-23T12:25:24Z (Base Sepolia
# block 45859818). Accruing from an arbitrary later date understated the declared position for
# the days the token demonstrably existed; accruing from before it existed would assert value
# that could not have accrued. Issuance is the defensible boundary.
DEMO_ACCRUAL_START = "2026-08-23T12:25:24+00:00"
CATALOG_ACCRUAL_START = "2026-10-21T00:00:00+00:00"
DEMO_SALE_BASE_FIXED = 100_000_000
DEMO_REDEMPTION_BASE_FIXED = 100_500_000
DEMO_PRICE_SCALE = "100000000"
DEMO_MATURITY_DATETIME = f"{DEMO_MATURITY}T00:00:00+00:00"
DEMO_MATURITY_TIMESTAMP = int(datetime.fromisoformat(DEMO_MATURITY_DATETIME).timestamp())
DEMO_PRICE_SOURCE = (
    "DECLARED TESTNET DEMO PRICE · synthetic · accretes from the sale price at the declared "
    "rate between 2026-09-01 and maturity · not a market observation"
)
DEMO_PRICE_VALID_UNTIL = "2030-12-31T23:59:59Z"
DEMO_PRICE_VALIDITY_SOURCE = (
    "DECLARED TESTNET DEMO PRICE VALIDITY · synthetic · valid through declared maturity · "
    "not a market observation"
)
DEMO_REASON = (
    "DECLARED TESTNET DEMO ECONOMICS · synthetic rate, maturity, tier, withdrawable-depth "
    "scenario and prices; not market observations"
)

RWA_PROJECTS = ("ondo-yield-assets", "blackrock-buidl", "usual-usd0", "midas-rwa",
                "superstate", "openeden", "matrixdock", "hashnote", "spiko",
                "franklin-templeton", "backed-finance")

def chain_id_for(name: object) -> int | None:
    """The chain DefiLlama or the curve means by `name`, from the chain registry; None for a chain
    the registry does not know, which includes every non-EVM chain."""
    return registry().resolve(name)


def _get(url: str, payload: dict | None = None, timeout: int = 40):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data, UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _yield_pools() -> list[dict]:
    """Fetch one shared rate feed and reject empty/malformed HTTP 200 responses."""
    return validate_yield_payload(_get("https://yields.llama.fi/pools"))


def native_rows() -> list[dict]:
    """The 204 fixed-maturity instruments the engine already adjudicates."""
    curve = json.loads(CURVE.read_text())
    rows = []
    for p in curve["points"]:
        identity = str(p.get("identity") or "")
        tokenised = re.fullmatch(r"0x[0-9a-fA-F]{40}", identity.split(":")[-1]) is not None
        rate = p.get("executable_apy")
        kind = "executable"
        if rate is None:
            rate = p.get("indicative_apy")
            kind = "indicative"
        row = {
            "category": "native",
            "name": p.get("instrument"),
            "venue": p.get("venue"),
            "chain": p.get("chain"),
            "chain_id": chain_id_for(p.get("chain")),
            "denomination": p.get("denomination"),
            # Pendle, Spectra and TermMax name a real contract in their identity; carrying it
            # here is what lets the gate probe answer for them too. Exactly, Term Finance and
            # Tenor name a synthetic key because their instrument is a position rather than a
            # token, and those rows keep address None and say so.
            "address": identity.split(":")[-1] if tokenised else None,
            # A position's deposit asset and the contract it goes into, when the venue's record
            # named them. Absent rather than guessed: a symbol cannot tell native USDC from USDC.e.
            "underlying_address": None if tokenised else p.get("underlying_address"),
            "accounting_asset_address": p.get("accounting_asset_address"),
            "market_address": None if tokenised else p.get("market_address"),
            "rate_bps": None if rate is None else round(float(rate) * 10_000),
            "rate_kind": (None if rate is None else
                          p.get("rate_kind") if p.get("venue") in MIDNIGHT_VENUES
                          and p.get("rate_kind") == "indicative" else kind),
            # The engine measured this against the venue's own book.
            "depth_usd": (None if p.get("liquidity_usd") is None
                          else round(float(p["liquidity_usd"]))),
            "tvl_usd": None,
            "status": p.get("status"),
            "verdict": p.get("status"),
            "rejection": p.get("rejection_basis"),
            "tier": p.get("tier"),
            "fenorium_score": p.get("fenorium_score"),
            "score_cap": p.get("score_cap"),
            "tokenised": tokenised,
            "maturity": p.get("maturity"),
            "maturity_timestamp": p.get("maturity_timestamp"),
            "days_to_maturity": p.get("days_to_maturity"),
            "obtainable": p.get("obtainable"),
            "reason": p.get("reason"),
        }
        loan_address = p.get("underlying_address")
        market_id = p.get("market_id")
        if (p.get("venue") in MIDNIGHT_VENUES and isinstance(market_id, str) and market_id
                and isinstance(loan_address, str) and loan_address
                and type(p.get("loan_decimals")) is int):
            row["market_id"] = market_id
            row["loan_token"] = p.get("loan_token") or {
                "address": loan_address.lower(), "decimals": p["loan_decimals"],
            }
        if p.get("venue") in MIDNIGHT_VENUES:
            row["collaterals"] = p.get("collaterals")
            row["listed"] = p.get("listed")
        if not tokenised:
            row["gate"] = {
                "status": None,
                "reason": "not a transferable token: a position in the venue's contract, "
                          "so no transfer gate applies",
                "measured_at": None,
            }
        point = {**p, "obtainable": row.get("obtainable"), "routed_purchase": routed_purchase(row)}
        row["rating"] = rating_json(rate_instrument(point, SHIPPED_ASSUMPTIONS))
        row["_rating_point"] = point
        rows.append(row)
    return rows


MORPHO_RATE_LOOKBACK = "THIRTY_DAYS"
MORPHO_RATE_SOURCE = "Morpho avgNetApyExcludingRewards 30d trailing observation"


def vault_rows() -> tuple[list[dict], str | None]:
    """Curated lending vaults. Morpho publishes withdrawable liquidity separately from TVL."""
    # `netApy` is Morpho's instantaneous rate *including reward emissions*, which is neither
    # trailing nor interest. A market pinned at 100% utilisation drives it to six figures while
    # nobody can withdraw. The realised figure is derived from share-price growth, which is what
    # the `realized` label below has always claimed to carry.
    query = ("{ vaults(first:400, where:{chainId_in:[1,8453]}) { items { address name "
             "chain { id } state { totalAssetsUsd "
             f"realized: avgNetApyExcludingRewards(lookback: {MORPHO_RATE_LOOKBACK}) }} "
             "liquidity { usd } asset { symbol } } } }")
    try:
        d = _get("https://blue-api.morpho.org/graphql", {"query": query})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [], f"morpho unreachable: {exc}"
    if not isinstance(d, dict):
        return [], "morpho HTTP 200 payload is not an object"
    if "errors" in d:
        return [], f"morpho returned errors: {str(d['errors'])[:120]}"

    try:
        items = d["data"]["vaults"]["items"]
    except (KeyError, TypeError):
        return [], "morpho HTTP 200 payload has no vault items"
    if not isinstance(items, list) or not items:
        return [], "morpho HTTP 200 payload has no vault items"
    if not all(isinstance(item, dict) for item in items):
        return [], "morpho HTTP 200 vault list contains malformed rows"

    rows = []
    omitted = 0
    for v in items:
        state = v.get("state") or {}
        tvl = state.get("totalAssetsUsd")
        if not tvl or tvl < 250_000:
            continue
        apy = state.get("realized")
        liq = (v.get("liquidity") or {}).get("usd")
        # A vault with no protocol-reported withdrawable liquidity would be a guaranteed rating
        # refusal. Omit it instead of inflating the variable-yield population with null depth.
        # Tested on the figure that is published, not the raw one: $0.008 withdrawable out of $2M
        # passed `liq <= 0` and was published as `depth_usd: 0`, a stuck vault presented as one
        # with an unmeasured book.
        if liq is None or round(float(liq)) <= 0:
            omitted += 1
            continue
        rows.append({
            "category": "vault",
            "name": v.get("name"),
            "venue": "Morpho",
            "chain": "Ethereum" if (v.get("chain") or {}).get("id") == 1 else "Base",
            "chain_id": (v.get("chain") or {}).get("id"),
            "address": v.get("address"),
            "rate_bps": None if apy is None else round(float(apy) * 10_000),
            # Trailing observation, not an offer. Never silently comparable to a locked rate.
            "rate_kind": None if apy is None else "realized",
            "rate_source": None if apy is None else MORPHO_RATE_SOURCE,
            # Morpho's own liquidity figure: what the withdraw queue can honour now.
            "depth_usd": None if liq is None else round(float(liq)),
            "tvl_usd": round(float(tvl)),
            "status": None,
            "tier": None,
            "maturity": None,
            "maturity_timestamp": None,
            "days_to_maturity": None,
            "reason": None,
        })
    error = None if not omitted else f"morpho: {omitted} vaults omitted without positive liquidity"
    return rows, error


def rwa_rows(pools: list[dict] | None = None) -> tuple[list[dict], str | None]:
    """Tokenized fixed income. TVL is fund size, never depth — the size gate must refuse."""
    if pools is None:
        try:
            pools = _yield_pools()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return [], f"defillama unreachable: {exc}"

    seen, rows = set(), []
    for p in pools:
        project = (p.get("project") or "").lower()
        if not any(k in project for k in RWA_PROJECTS):
            continue
        tvl = p.get("tvlUsd") or 0
        if tvl < 1_000_000:
            continue
        key = (project, p.get("symbol"), p.get("chain"))
        if key in seen:
            continue
        seen.add(key)
        apy = p.get("apy")
        # The address map is Ethereum-only. The same fund deployed on another chain is a
        # different contract with its own gate, and stamping the mainnet address onto it would
        # report a compliance registry that instrument does not use.
        known = (RWA_ADDRESSES.get((project, (p.get("symbol") or "").upper()))
                 if p.get("chain") == "Ethereum" else None)
        rows.append({
            "category": "rwa",
            "name": f"{p.get('symbol')} — {p.get('project')}",
            "venue": p.get("project"),
            "chain": p.get("chain"),
            "chain_id": known[0] if known else chain_id_for(p.get("chain")),
            "address": known[1] if known else None,
            "decimals": known[2] if known else None,
            "rate_bps": None if apy is None else round(float(apy) * 100),
            # The issuer's published figure. Par has not shown it obtainable at any size.
            "rate_kind": None if apy is None else "issuer_quoted",
            # Deliberately None. See the module docstring: fund size is not obtainable depth.
            "depth_usd": None,
            "tvl_usd": round(float(tvl)),
            "status": None,
            "tier": None,
            "maturity": None,
            "maturity_timestamp": None,
            "days_to_maturity": None,
            "reason": "fund size is published; obtainable depth at a stated size is not",
        })
    return rows, None


# Tokens Par verified directly: each returned a live LI.FI route at $250,000 from USDC on
# Ethereum, 2026-08-23. They are first-class rows rather than joins onto DefiLlama, because a
# DefiLlama RWA record is a *pool* whose `symbol` is the deposit asset — matching those to token
# addresses by symbol joins two different things and produces confident nonsense.
#
# `issuer` is the project slug whose published rate legitimately belongs to the token. A pool run
# by anyone else (a Curve LP holding the token, a Morpho market lending against it) reports the
# yield of that pool, not of the instrument, and attaching it would restate someone else's number
# as this instrument's rate.
# Each entry: symbol, chain, address, decimals, issuer, DefiLlama slug, category, rate pool.
#
# `rate_pool` names the exact pool whose published rate belongs to this instrument. Matching by
# underlying is not enough and matching by symbol is worse: Sky runs three Ethereum pools on the
# same USDS underlying -- sUSDS at 352bp, stUSDS at 554bp and USDS at 578bp -- so a join that
# takes the largest hands a different product's yield to this one the moment that pool is absent
# or overtaken. The pool id is exact, and where it cannot be found the rate stays unknown, which
# is the whole doctrine here: a pool run by anyone but the issuer reports that pool's yield, and
# so does a different pool run by the same issuer.
VERIFIED_RWA = (
    ("mTBILL", 1, "0xDD629E5241CbC5919847783e6C96B2De4754e438", 18, "Midas", "midas-rwa", "rwa", None),
    ("wUSDM", 1, "0x57F5E098CaD7A3D1Eed53991D4d66C45C9AF7812", 18, "Mountain", "mountain-protocol", "rwa", None),
    ("USDY", 1, "0x96F6eF951840721AdBF46Ac996b59E0235CB985C", 18, "Ondo", "ondo-yield-assets", "rwa", None),
    ("USDtb", 1, "0xC139190F447e929f090Edeb554D95AbB8b18aC1C", 18, "Ethena", "ethena-usdtb", "rwa", None),
    ("syrupUSDC", 1, "0x80ac24aA929eaF5013f6436cdA2a7ba190f5Cc0b", 6, "Maple", "maple", "rwa", None),
    ("USDM", 1, "0x59D9356E565Ab3A36dD77763Fc0d87fEaf85508C", 18, "Mountain", "mountain-protocol", "rwa", None),
    ("USD0++", 1, "0x35D8949372D46B7a3D5A56006AE77B215fc69bC0", 18, "Usual", "usual-usd0", "rwa", None),
    # Added 2026-09-21. Every address below was read on its own chain before it was written here:
    # symbol() and decimals() match what is claimed, and sUSDS's asset() returns exactly the USDS
    # its pool names, which is what establishes the pairing rather than assuming it.
    ("sUSDS", 1, "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD", 18, "Sky", "sky-lending", "vault",
     "d8c4eff5-c8a9-46fc-a888-057c4c668e72"),
    ("USYC", 56, "0x8D0fA28f221eB5735BC71d3a0Da67EE5bC821311", 6, "Circle", "circle-usyc", "rwa",
     "7c0a89c7-70cf-460c-b62e-cb278bf97e8f"),
    ("USYC", 1, "0x136471a34f6ef19fE571EFFC1CA711fdb8E49f2b", 6, "Circle", "circle-usyc", "rwa",
     "448a64ff-06fd-4e56-b63c-03662ac39010"),
    ("USTB", 1, "0x43415eB6ff9DB7E26A15b704e7A3eDCe97d31C4e", 6, "Superstate", "invesco-ustb", "rwa",
     "1910847a-f8b5-40ce-a1ab-1dafdded5fbb"),
    # Added 2026-09-21. Read on chain before being written here, and each one by the route that
    # can prove it: the five SparkLend receipts came from the market's own ProtocolDataProvider
    # via getReserveTokensAddresses(), so each spToken's UNDERLYING_ASSET_ADDRESS() returns the
    # reserve it was derived from; Fluid's and Spark Savings' vaults are ERC-4626, so asset()
    # returns the token they are a claim on. An invented address fails that read instead of
    # quietly registering somebody else's token.
    #
    # SparkLend and Spark Savings are separate issuers here because they are separate products
    # that collide: both mint a spUSDC and a spUSDT on Ethereum, at different addresses and
    # different rates (3.31% lending against 3.60% savings). Anything keying on the symbol alone
    # would merge them, so the row carries the venue that distinguishes them.
    ("spUSDS", 1, "0xC02aB1A5eaA8d1B114EF786D9bde108cD4364359", 18, "SparkLend", "sparklend", "vault",
     "0ed981dc-b49d-426d-ade5-6014728b1ef9"),
    ("spDAI", 1, "0x4DEDf26112B3Ec8eC46e7E31EA5e123490B05B8B", 18, "SparkLend", "sparklend", "vault",
     "e26ce7d9-db75-4aa4-b1db-cc21ae17bdfb"),
    ("spUSDT", 1, "0xe7dF13b8e3d6740fe17CBE928C7334243d86c92f", 6, "SparkLend", "sparklend", "vault",
     "8fbe28b8-140d-4e37-8804-5d2aba4daded"),
    ("spUSDC", 1, "0x377C3bd93f2a2984E1E7bE6A5C22c525eD4A4815", 6, "SparkLend", "sparklend", "vault",
     "65ce8276-b4d9-41ba-9f6f-21fc374cf9bc"),
    ("spPYUSD", 1, "0x779224df1c756b4EDD899854F32a53E8c2B2ce5d", 6, "SparkLend", "sparklend", "vault",
     "995b269a-8409-4c55-b97e-868f443b432e"),
    ("spUSDT", 1, "0xe2e7a17dFf93280dec073C995595155283e3C372", 6, "Spark Savings", "spark-savings", "vault",
     "a5d67f7e-5b51-4a9d-969d-caf051a7f5a4"),
    ("spUSDC", 1, "0x28B3a8fb53B741A8Fd78c0fb9A6B2393d896a43d", 6, "Spark Savings", "spark-savings", "vault",
     "c5c74dd1-995c-4445-9d84-3e710bad7d52"),
    ("fUSDC", 1, "0x9Fb7b4477576Fe5B32be4C1843aFB1e55F251B33", 6, "Fluid", "fluid-lending", "vault",
     "4438dabc-7f0c-430b-8136-2722711ae663"),
    ("fUSDT", 1, "0x5C20B550819128074FD538Edf79791733ccEdd18", 6, "Fluid", "fluid-lending", "vault",
     "4e8cc592-c8d5-4824-8155-128ba521e903"),
    ("fUSDC", 42161, "0x1A996cb54bb95462040408C06122D45D6Cdb6096", 6, "Fluid", "fluid-lending", "vault",
     "4c45cc9e-e1a4-43c9-8a3d-687d96abb07c"),
    ("fUSDC", 8453, "0xf42f5795D9ac7e9D757dB633D693cD548Cfd9169", 6, "Fluid", "fluid-lending", "vault",
     "7372edda-f07f-4598-83e5-4edec48c4039"),
    # Centrifuge's pool for this one is keyed on JTRSY itself rather than on a deposit asset,
    # so the pin and the holding name the same token. Its sibling pools deposit USDC and mint a
    # share whose address the feed never gives, and USAT -- the largest of them -- is Tether's
    # token listed here, not Centrifuge's issue. Neither is registered on a guess.
    ("JTRSY", 1, "0x8c213ee79581Ff4984583C6a801e5263418C4b86", 6, "Centrifuge", "centrifuge-protocol", "rwa",
     "99181d82-5589-4ae6-b737-594aa11ded9f"),
)

def verified_rwa_rows(pools: list[dict] | None) -> list[dict]:
    """The tokens Par can actually price, with a rate only where the issuer published one."""
    by_token: dict[tuple[int, str], dict] = {}
    for pool in pools or []:
        pool_chain = chain_id_for(str(pool.get("chain") or ""))
        if pool_chain is None:
            continue
        for underlying in (pool.get("underlyingTokens") or []):
            key = (pool_chain, str(underlying).lower())
            best = by_token.get(key)
            if best is None or (pool.get("tvlUsd") or 0) > (best.get("tvlUsd") or 0):
                by_token[key] = pool

    by_id = {str(pool.get("pool")): pool for pool in pools or [] if pool.get("pool")}

    rows = []
    for symbol, chain_id, address, decimals, issuer, slug, category, rate_pool in VERIFIED_RWA:
        chain = registry().require(chain_id).label
        pool = by_id.get(rate_pool) if rate_pool else by_token.get((chain_id, address.lower()))
        own = pool if (pool and (pool.get("project") or "").lower() == slug
                       and chain_id_for(pool.get("chain")) == chain_id) else None
        # A fixed-income rate is interest paid on the deposit, which is `apyBase`. The `apy`
        # field folds in `apyReward` -- an incentive emission in somebody's governance token.
        # SparkLend's largest Ethereum USDS pool is an "SPK Farming Pool" whose entire 4.32%
        # is reward and whose apyBase is null, while the market's actual supply rate is 2.56%
        # in a pool holding less. Reading the total would have published a token emission as a
        # yield, and picked the wrong pool of the two to do it with.
        apy = own.get("apyBase") if own else None
        rows.append({
            "category": category,
            "name": f"{symbol} — {issuer}",
            "venue": issuer,
            "chain": chain,
            "chain_id": chain_id,
            "address": address,
            "decimals": decimals,
            "rate_bps": None if apy is None else round(float(apy) * 100),
            "rate_kind": None if apy is None else "issuer_quoted",
            # Filled by the obtainability pass from a live route, not from fund size.
            "depth_usd": None,
            "tvl_usd": (None if (own or {}).get("tvlUsd") is None
                        else round(float(own["tvlUsd"]))),
            "status": None,
            "tier": None,
            "maturity": None,
            "maturity_timestamp": None,
            "days_to_maturity": None,
            "reason": None if apy is not None else (
                "this pool publishes no base rate; what it pays is incentive emission, not interest"
                if own is not None else
                "no rate published by the issuer; only third-party pools reference this token"),
        })
    return rows


def declared_accrued_price(base_fixed: int, rate_bps: int, as_of: datetime, *,
                           start=DEMO_ACCRUAL_START, end=None) -> int:
    """Accrete a declared demo price at the instrument's own stated rate.

    Integer in, integer out, through Decimal rather than float: this feeds price_usd_fixed, which
    is money evidence. Accrual is clamped to the declared window, so a valuation before issuance
    is the base price and one after maturity stops accreting rather than compounding forever.
    """
    start = datetime.fromisoformat(start) if isinstance(start, str) else start
    end = end or DEMO_MATURITY_DATETIME
    end = datetime.fromisoformat(end) if isinstance(end, str) else end
    moment = min(max(as_of.astimezone(timezone.utc), start), end)
    years = Decimal((moment - start).total_seconds()) / Decimal(365 * 24 * 60 * 60)
    factor = (Decimal(1) + Decimal(rate_bps) / Decimal(10_000)) ** years
    return int((Decimal(base_fixed) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def declared_price_accrual_metadata(base_fixed: int, rate_bps: int, *,
                                    accrual_start=DEMO_ACCRUAL_START,
                                    maturity=DEMO_MATURITY_DATETIME) -> dict:
    """Describe the declared compound formula used by one demo price observation."""
    return {
        "model": "declared_compound_interest_v1",
        "price_scale": DEMO_PRICE_SCALE,
        "base_price_usd_fixed": str(base_fixed),
        "rate_bps": rate_bps,
        "accrual_start": accrual_start,
        "maturity": maturity,
    }


def demo_decision_hash(row: dict) -> str:
    """Preview hash of a demo row's declared inputs, reproducible from the served row.

    The platform serves `price_usd_fixed` as a decimal string (money is never a JSON number on
    the wire) while the artifact stores an integer, so the observations are hashed in the served
    form; recomputing from the artifact means applying the same str().
    """
    observations = [{**observation, "price_usd_fixed": str(observation["price_usd_fixed"])}
                    for observation in row["price_observations"]]
    return "sha256:" + hashlib.sha256(json.dumps({
        "identity": f"eip155:84532/erc20:{row['address'].lower()}",
        "rate_bps": row["rate_bps"], "tier": row["tier"], "maturity": row["maturity"],
        "fenorium_score": row.get("fenorium_score"), "price_observations": observations,
        "evidence_as_of": row["evidence_as_of"],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stamp_demo_evidence(row: dict, at: datetime) -> None:
    row["evidence_as_of"] = at.isoformat()
    row["decision_hash"] = demo_decision_hash(row)


def restate_demo_rows(rows: list[dict], generated_at: datetime) -> None:
    """Move the demo rows to the moment the artifact is actually published.

    testnet_rows() prices the demos when it is called, which is before the network fetches that
    dominate a build. Stamping the later generation time onto those figures published an
    observation whose price was never computed at the moment it claimed, so anyone recomputing
    from the declared terms at the stated timestamp got a different number -- which defeats the
    point of publishing the terms. Recompute, do not relabel.
    """
    observed_at = generated_at.isoformat().replace("+00:00", "Z")
    for row in rows:
        # Only a row that declares a redemption date has a countdown to restate. Writing one
        # onto a vault or onto the unit of account contradicts the maturity the row itself
        # publishes as absent.
        if row.get("maturity"):
            row["days_to_maturity"] = (
                datetime.fromisoformat(row["maturity"]).date() - generated_at.date()
            ).days
        for observation in row["price_observations"]:
            observation["observed_at"] = observed_at
            # The unit of account is a dollar at every moment, so there is nothing to recompute.
            # Running it through the accrual worked only because its rate is zero and neither of
            # its legs is named for the redemption terms -- an accident, not a rule.
            if row.get("price_accrual") is None:
                observation["price_usd_fixed"] = DEMO_UNIT_PRICE_FIXED
                continue
            base = (DEMO_REDEMPTION_BASE_FIXED
                    if observation["independence_key"] == "declared-testnet-demo-redemption-terms"
                    else int(row["price_accrual"]["base_price_usd_fixed"]))
            observation["price_usd_fixed"] = declared_accrued_price(
                base, row["rate_bps"], generated_at,
                start=row["price_accrual"]["accrual_start"], end=row["price_accrual"]["maturity"])
        stamp_demo_evidence(row, generated_at)


# A registered asset's identity carries its kind, and the browser derives that kind from the
# row's category -- see registeredKind() in web/portfolio.js. Stamping "erc20" on every
# observation left the note and the vault with observations whose identity did not match
# their own row, so the price engine discarded them and both assets went unpriced.
def registered_kind(category: str) -> str:
    return {"vault": "vault", "native": "pt"}.get(category, "erc20")


SETTLEMENT_CATEGORY = "settlement"

# The assets a wallet actually settles in, and the reason they are here: nothing can read a
# balance for a contract the universe never names, so a real wallet's USDC and ETH were invisible
# to the scan and therefore unpriceable -- which is why the live price sources had nothing to
# value. They are registered so they can be read and priced, and marked so the market population
# keeps leaving them out: cash is what you buy an instrument with, not an instrument.
#
# Every address here is one the live price registry already carries a verified feed for, so an
# asset that can be priced is an asset that can be seen. `test_settlement_rows` holds the two
# sides together.
# The list itself lives in the chain registry (`settlement` per chain).
SETTLEMENT_ASSETS: tuple[tuple[int, str, str, str, int, bool], ...] = tuple(
    (chain.chain_id, chain.label, token.symbol, token.address, token.decimals, token.stablecoin)
    for chain in registry().chains for token in chain.settlement
)

NOT_AN_INSTRUMENT = (
    "settlement asset: registered so a wallet's cash can be read and priced, "
    "never offered as an instrument"
)


def is_settlement(row: dict) -> bool:
    return row.get("category") == SETTLEMENT_CATEGORY


def settlement_rows() -> list[dict]:
    """Cash, named so it can be seen. Carries no rate, no depth and no verdict, because it has
    none: every field an instrument earns by being adjudicated stays absent rather than zero."""
    rows = []
    for chain_id, chain, symbol, address, decimals, stablecoin in SETTLEMENT_ASSETS:
        rows.append({
            "category": SETTLEMENT_CATEGORY,
            "name": f"{symbol} — settlement asset",
            "symbol": symbol,
            "venue": "—",
            "chain": chain,
            "chain_id": chain_id,
            "address": address,
            "decimals": decimals,
            "is_stablecoin": stablecoin,
            "is_variable_income": False,
            "rate_bps": None,
            "rate_kind": None,
            "rate_source": NOT_AN_INSTRUMENT,
            "depth_usd": None,
            "depth_source": None,
            "tvl_usd": None,
            "status": None,
            "verdict": None,
            "rejection": None,
            "reason": NOT_AN_INSTRUMENT,
            "tier": None,
            "tier_source": None,
            "maturity": None,
            "maturity_timestamp": None,
            "maturity_source": None,
            "days_to_maturity": None,
            "days_to_maturity_source": None,
            "tokenised": True,
        })
    return rows


def checksum_address(address: str) -> str:
    """Keep the existing EIP-55 output spelling when reading lowercase manifests."""
    from engine.keccak import keccak256

    raw = address.removeprefix("0x").lower()
    if len(raw) != 40 or any(char not in "0123456789abcdef" for char in raw):
        raise ValueError(f"invalid deployment address: {address!r}")
    digest = keccak256(raw.encode("ascii")).hex()
    return "0x" + "".join(char.upper() if int(digest[i], 16) >= 8 else char
                          for i, char in enumerate(raw))


def testnet_rows(declared_at: datetime | None = None, *, mocks_manifest_path=None) -> list[dict]:
    manifest_path = mocks_manifest_path if mocks_manifest_path is not None else os.environ.get(
        "FENORIUM_MOCKS_MANIFEST", ROOT / "contracts/deployments/base-sepolia-mocks.json")
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest["chain_id"] != 84532:
        raise ValueError("mock deployment must be on Base Sepolia")
    deployments = {**manifest["instruments"], "mUSDC": manifest["payment_token"]}
    unit_sales = tuple((leg, checksum_address(manifest["instruments"][symbol]["desk"]))
                       for leg, symbol in (("gated", "mGT"), ("permissionless", "mPT")))
    declaration_time = declared_at or datetime.now(timezone.utc)
    if declaration_time.tzinfo is None or declaration_time.utcoffset() is None:
        raise ValueError("declared demo timestamp must be timezone-aware")
    observed_at = declaration_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    days_to_maturity = (
        datetime.fromisoformat(DEMO_MATURITY).date() - declaration_time.date()
    ).days

    def price_observations(address: str, rate_bps: int, category: str) -> list[dict]:
        identity = f"eip155:84532/{registered_kind(category)}:{address.lower()}"
        # Both legs accrete at the same rate, so the 50bp they are declared apart stays 50bp and
        # the two-independent-sources alignment rule keeps holding at every point in time.
        return [
            {
                "identity": identity,
                "source_id": "DECLARED_TESTNET_DEMO_SALE_TERMS_NOT_MARKET_OBSERVATION",
                "independence_key": "declared-testnet-demo-sale-terms",
                "price_usd_fixed": declared_accrued_price(
                    DEMO_SALE_BASE_FIXED, rate_bps, declaration_time),
                "observed_at": observed_at,
                "method_version": "declared-testnet-demo-price/sale-terms-accrual-v1",
            },
            {
                "identity": identity,
                "source_id": "DECLARED_TESTNET_DEMO_REDEMPTION_TERMS_NOT_MARKET_OBSERVATION",
                "independence_key": "declared-testnet-demo-redemption-terms",
                "price_usd_fixed": declared_accrued_price(
                    DEMO_REDEMPTION_BASE_FIXED, rate_bps, declaration_time),
                "observed_at": observed_at,
                "method_version": "declared-testnet-demo-price/redemption-terms-accrual-v1",
            },
        ]

    def unit_observations(address: str, category: str) -> list[dict]:
        identity = f"eip155:84532/{registered_kind(category)}:{address.lower()}"
        return [
            {
                "identity": identity,
                "source_id": f"DECLARED_TESTNET_DEMO_UNIT_OF_ACCOUNT_{leg.upper()}_SALE_NOT_MARKET_OBSERVATION",
                "independence_key": f"declared-testnet-demo-unit-{leg}-sale",
                "price_usd_fixed": DEMO_UNIT_PRICE_FIXED,
                "observed_at": observed_at,
                "method_version": f"declared-testnet-demo-price/unit-of-account-{leg}-sale-v1",
                "declared_by_contract": sale,
            }
            for leg, sale in unit_sales
        ]

    rows = []
    for t in TESTNET:
        deployment = deployments[t["symbol"]]
        t = {**t, "address": checksum_address(deployment["address"]),
             "deployed_block": deployment["deployed_block"]}
        unit = t.get("unit_of_account") is True
        # "maturity" is absent on the treasury rows and explicitly None on the ones that have no
        # redemption date, so the default has to be the shared demo maturity, not None.
        maturity = t["maturity"] if "maturity" in t else DEMO_MATURITY
        rows.append({
            "category": t["category"],
            "name": t["name"],
            "symbol": t["symbol"],
            "venue": "Par (testnet mock)",
            "chain": "Base Sepolia",
            "chain_id": 84532,
            "address": t["address"],
            # A token cannot have a transfer before it existed, so a scan that starts earlier is
            # asking the chain about blocks it can only answer "none" for. Each of these is the
            # creation block from contracts/broadcast/, which is the record of the transaction
            # that deployed it.
            "deployed_block": t["deployed_block"],
            "decimals": 6,
            "testnet": True,
            "is_stablecoin": t["category"] == "stablecoin",
            "is_variable_income": t["category"] == "vault",
            "rate_bps": t["rate_bps"],
            "rate_kind": "declared_demo",
            "rate_source": DEMO_RATE_SOURCE,
            "depth_usd": 10_000_000,
            "depth_source": DEMO_DEPTH_SOURCE,
            "tvl_usd": None,
            "status": None,
            "tier": t["tier"],
            "tier_source": DEMO_TIER_SOURCE if t["tier"] else None,
            "maturity": maturity,
            "maturity_timestamp": (DEMO_MATURITY_TIMESTAMP if maturity == DEMO_MATURITY
                                   else t.get("maturity_timestamp")),
            "maturity_source": DEMO_MATURITY_SOURCE if maturity else None,
            "days_to_maturity": days_to_maturity if maturity else None,
            "days_to_maturity_source": DEMO_DAYS_SOURCE if maturity else None,
            "price_valid_until": DEMO_PRICE_VALID_UNTIL,
            "price_validity_source": DEMO_PRICE_VALIDITY_SOURCE,
            # Historical demo valuation uses the declared sale terms; observation contracts stay
            # unchanged. The schedule's own `maturity` is the date the declared accrual stops,
            # which every accreting row needs and which is not the same fact as the instrument's
            # redemption date -- the vault has the first and not the second. Only the unit of
            # account has neither, because a dollar that accreted would silently revalue every
            # instrument priced in it.
            "price_accrual": None if unit else declared_price_accrual_metadata(DEMO_SALE_BASE_FIXED, t["rate_bps"]),
            "price_observations": unit_observations(t["address"], t["category"]) if unit
            else price_observations(t["address"], t["rate_bps"], t["category"]),
            "price_source": DEMO_UNIT_PRICE_SOURCE if unit else DEMO_PRICE_SOURCE,
            "reason": DEMO_REASON,
        })
        if "fenorium_score" in t:
            rows[-1].update({
                "fenorium_score": t["fenorium_score"],
                "verdict": "BID" if t["fenorium_score"] >= 60 else "NO BID",
                "score_source": DEMO_SCORE_SOURCE,
            })
        stamp_demo_evidence(rows[-1], declaration_time)
    return rows


def catalog_price_fields(row: dict, item: dict, declared_at: datetime) -> dict:
    if declared_at.tzinfo is None or declared_at.utcoffset() is None:
        raise ValueError("declared demo timestamp must be timezone-aware")
    observed_at = declared_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end = f"{row['maturity']}T00:00:00+00:00" if row["maturity"] else DEMO_MATURITY_DATETIME
    base = int(item["price_micros"]) * 100
    rate = row["rate_bps"]
    return {
        "price_valid_until": DEMO_PRICE_VALID_UNTIL,
        "price_validity_source": DEMO_PRICE_VALIDITY_SOURCE,
        "price_accrual": declared_price_accrual_metadata(
            base, rate, accrual_start=CATALOG_ACCRUAL_START, maturity=end),
        "price_observations": [{
            "identity": row["identity"],
            "source_id": "DECLARED_TESTNET_CATALOG_DESK_TERMS_NOT_MARKET_OBSERVATION",
            "independence_key": "declared-testnet-catalog-desk-terms",
            "method_version": "declared-testnet-catalog-price/desk-terms-accrual-v1",
            "observed_at": observed_at,
            "price_usd_fixed": declared_accrued_price(
                base, rate, declared_at, start=CATALOG_ACCRUAL_START, end=end),
            "declared_by_contract": row["desk"],
        }],
        "price_source": "DECLARED TESTNET CATALOG PRICE · the desk's price accreting at the declared rate · not a market observation",
    }


def testnet_catalog_rows(declared_at: datetime | None = None, *,
                         manifest_path=None) -> list[dict]:
    from scripts.build_testnet_scenarios import DEFAULT_AS_OF, _deployed, _market

    declaration_time = declared_at or datetime.now(timezone.utc)
    if declaration_time.tzinfo is None or declaration_time.utcoffset() is None:
        raise ValueError("declared demo timestamp must be timezone-aware")
    manifest_path = manifest_path if manifest_path is not None else os.environ.get(
        "FENORIUM_CATALOG_MANIFEST", ROOT / "contracts/deployments/base-sepolia-catalog.json")
    manifest = json.loads(Path(manifest_path).read_text())
    market = _deployed(_market(DEFAULT_AS_OF), manifest, declaration_time)
    instruments = {item["index"]: item for item in manifest["instruments"]}
    rows = []
    for gen in market:
        item = instruments[gen["_index"]]
        maturity = gen["maturity"]
        rate = gen["rate_bps"]
        row = {
            "category": gen["category"], "name": gen["name"], "symbol": gen["symbol"],
            "venue": gen["venue"], "modelled_on": gen["modelled_on"],
            "chain": "Base Sepolia", "chain_id": 84532,
            "address": gen["address"], "desk": gen["desk"],
            "deployed_block": item["deployed_block"], "decimals": 6,
            "testnet": True, "catalog": True,
            "is_stablecoin": gen["is_stablecoin"], "is_variable_income": gen["is_variable_income"],
            "rate_bps": rate, "rate_kind": "declared_demo",
            "rate_source": "DECLARED TESTNET CATALOG RATE · synthetic · not a market observation",
            "depth_usd": gen["depth_usd"],
            "depth_source": "DECLARED TESTNET CATALOG DEPTH · synthetic withdrawable-liquidity scenario · not a market observation",
            "tvl_usd": None, "status": None, "tier": gen["tier"],
            "tier_source": "DECLARED TESTNET CATALOG TIER · synthetic · not a credit assessment",
            "maturity": maturity, "maturity_timestamp": gen["maturity_timestamp"],
            "maturity_source": "DECLARED TESTNET CATALOG MATURITY · synthetic · not a market observation" if maturity else None,
            "days_to_maturity": (datetime.fromisoformat(maturity).date() - declaration_time.date()).days if maturity else None,
            "days_to_maturity_source": "DECLARED TESTNET CATALOG DAYS TO MATURITY · synthetic · not a market observation" if maturity else None,
            **catalog_price_fields(gen, item, declaration_time),
            "instrument_risk": gen["instrument_risk"], "rejection": None,
            "reason": "DECLARED TESTNET CATALOG ECONOMICS · synthetic rate, maturity, tier, depth and score on a real Base Sepolia token; not market observations",
        }
        if gen["category"] == "native":
            score = gen["fenorium_score"]
            row.update({
                "fenorium_score": score, "verdict": "BID" if score >= 60 else "NO BID",
                "score_source": "DECLARED TESTNET CATALOG SCORE · synthetic · not an evidence assessment",
            })
            if score < 60:
                row["rejection"] = "credit"
                row["reason"] = f"NO BID (credit) — declared demo score {score} is below 60. " + row["reason"]
        stamp_demo_evidence(row, declaration_time)
        rows.append(row)
    return rows


def measure_demo_supply(
    rows: list[dict],
    *,
    clock=lambda: datetime.now(timezone.utc),
    call=None,
) -> None:
    """Value measured demo token supply at its latest declared synthetic price."""
    from engine.chain import rpc_call

    rpc = call if call is not None else rpc_call

    def read_uint(address: str, selector: str) -> int:
        endpoints = rpc_endpoints(84532)
        if not endpoints:
            raise RuntimeError("no RPC endpoints configured for chain 84532")
        for endpoint in endpoints:
            try:
                result = rpc(endpoint, "eth_call", [{"to": address, "data": selector}, "latest"])
            except Exception as exc:
                if not is_transport_failure(exc) or endpoint == endpoints[-1]:
                    raise
                continue
            if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
                raise ValueError(f"invalid ERC-20 result for {selector}")
            return int(result, 16)
        raise RuntimeError("no usable RPC endpoint")

    for row in rows:
        if row.get("chain_id") != 84532 or row.get("category") not in {"rwa", "vault"}:
            continue
        try:
            supply = read_uint(row["address"], "0x18160ddd")
            decimals = read_uint(row["address"], "0x313ce567")
            if decimals > 255:
                raise ValueError("invalid ERC-20 decimals")
            price = Decimal(row["price_observations"][0]["price_usd_fixed"]) / Decimal(DEMO_PRICE_SCALE)
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid declared demo price")
            measured_at = clock().isoformat()
            row["tvl_usd"] = round(Decimal(supply) / (10 ** decimals) * price)
            row["tvl_source"] = (
                f"MEASURED totalSupply × DECLARED TESTNET DEMO PRICE · read {measured_at} · "
                "not a market observation"
            )
        except Exception as exc:
            row["tvl_usd"] = None
            # The reason is published; an RPC address can carry a provider key.
            row["tvl_source"] = f"not measured: {redact_endpoints(str(exc))}"[:160]


@dataclass(frozen=True, slots=True)
class EligibilityMeasurement:
    measured: int
    failed: int
    rpc_round_trips: int
    legacy_sequential_round_trips: int
    rpc_mode: str
    source_errors: tuple[str, ...]


TRANSFER_PROBE = "transfer probe"
ZERO_TRANSFER_PROBE = "zero-amount transfer probe"


def measure_eligibility(
    rows: list[dict],
    *,
    clock=lambda: datetime.now(timezone.utc),
) -> EligibilityMeasurement:
    """Probe every instrument that has a contract, here rather than in the browser.

    Doing this client-side meant 40-odd probes against public endpoints on every page load, which
    earned sustained rate limiting; worse, a throttled reply is indistinguishable from a clean one
    unless you look, and an early version cached "not available" onto ten healthy vaults because
    of it. Measuring once, slowly, with the engine's own classifier removes the whole failure
    mode — and the reading carries the timestamp it was taken at, so nobody mistakes it for live.
    """
    from engine.eligibility import (
        GATING_SYSTEMS,
        REGISTRY_SIGNATURES,
        GateProbe,
        GateStatus,
        assess_gate,
        classify_multicall_transfer,
        decode_registry_results,
        registry_probe_calldata,
        transfer_probe_calldata,
    )

    def record(probe: GateProbe) -> dict:
        return {
            "status": probe.status.value if isinstance(probe.status, GateStatus) else str(probe.status),
            "reason": probe.revert_reason,
            "measured_at": probe.probed_at.isoformat(),
        }

    def failure(detail: str) -> dict:
        # Published with the row: an RPC address in the detail can carry a provider key.
        return {"status": None, "reason": f"not measured: {redact_endpoints(detail)}"[:120], "measured_at": None}

    by_chain: dict[int, dict[str, list[dict]]] = {}
    for row in rows:
        address, chain_id = row.get("address"), row.get("chain_id")
        endpoints = rpc_endpoints(chain_id) if isinstance(chain_id, int) else ()
        if not isinstance(address, str) or not endpoints or is_settlement(row):
            continue
        by_chain.setdefault(chain_id, {}).setdefault(address.lower(), []).append(row)

    session = _RpcSession(None)
    measured = failed = legacy_round_trips = sequential_round_trips = 0
    modes: list[str] = []
    source_errors: list[str] = []

    for chain_id, target_rows in sorted(by_chain.items()):
        endpoints = rpc_endpoints(chain_id)
        preferred = endpoints[0]

        def fail_chain(detail: str) -> None:
            nonlocal failed
            source_errors.append(f"chain {chain_id}: {redact_endpoints(detail)}")
            for owned_rows in target_rows.values():
                for row in owned_rows:
                    row["gate"] = failure(detail)
                    failed += 1

        try:
            multicall_available = session.multicall_available(chain_id, preferred)
        except Exception as exc:  # transport failure is not proof that Multicall3 is absent
            fail_chain(f"Multicall3 availability source failed: {exc}")
            modes.append(f"chain {chain_id}: source failed")
            continue

        if not multicall_available:
            modes.append(f"chain {chain_id}: sequential fallback (Multicall3 absent)")
            for address, owned_rows in target_rows.items():
                try:
                    transport_errors: list[str] = []
                    for endpoint in endpoints:
                        try:
                            probe = assess_gate(endpoint, chain_id, address)
                            break
                        except Exception as exc:
                            if not is_transport_failure(exc):
                                raise
                            transport_errors.append(str(exc))
                    else:
                        raise RuntimeError(
                            f"all {len(endpoints)} RPC endpoints failed for chain {chain_id}: {transport_errors[-1]}"
                        )
                    gate = record(probe)
                    decimals = int.from_bytes(
                        session.call(chain_id, preferred, address, "0x313ce567"), "big"
                    )
                    if not 0 <= decimals <= 255:
                        raise ValueError("ERC-20 decimals is outside the supported 0..255 range")
                    # The fallback preserves the old registry-first semantics. Its steady-state
                    # upper bound is four registry calls, one transfer call, and one metadata read.
                    sequential_round_trips += 6
                    legacy_round_trips += 6
                    for row in owned_rows:
                        row["decimals"] = decimals
                        row["gate"] = dict(gate)
                        measured += 1
                except Exception as exc:  # noqa: BLE001 - failed evidence stays unavailable
                    for row in owned_rows:
                        row["gate"] = failure(str(exc))
                        failed += 1
            continue

        modes.append(f"chain {chain_id}: Multicall3 aggregate3")
        registry_calls = tuple(
            call
            for address in target_rows
            for call in (
                *(
                    MulticallRead(
                        address,
                        registry_probe_calldata(signature),
                        address,
                        f"registry:{signature}",
                    )
                    for signature in REGISTRY_SIGNATURES
                ),
                MulticallRead(address, "0x313ce567", address, "metadata:decimals"),
            )
        )
        reader = lambda rpc, to, data, cid=chain_id: session.call(cid, rpc, to, data)
        try:
            registry_results = execute_multicall_chunks(reader, preferred, registry_calls)
        except MulticallChunkError as exc:
            fail_chain(str(exc))
            continue

        registry_by_address: dict[str, dict[str, bytes]] = {address: {} for address in target_rows}
        failed_subcalls: dict[str, list[str]] = {}
        for call, (success, return_data) in zip(registry_calls, registry_results, strict=True):
            if not success:
                failed_subcalls.setdefault(call.owner, []).append(call.field)
            elif call.field.startswith("registry:"):
                registry_by_address[call.owner][call.field.removeprefix("registry:")] = return_data
            elif call.field == "metadata:decimals" and len(return_data) == 32:
                decimals = int.from_bytes(return_data, "big")
                if 0 <= decimals <= 255:
                    for row in target_rows[call.owner]:
                        row["decimals"] = decimals
                else:
                    failed_subcalls.setdefault(call.owner, []).append(call.field)
            elif call.field == "metadata:decimals":
                failed_subcalls.setdefault(call.owner, []).append(call.field)

        probes: dict[str, GateProbe] = {}
        transfer_addresses: list[str] = []
        registry_fields = {f"registry:{signature}" for signature in REGISTRY_SIGNATURES}
        for address, results in registry_by_address.items():
            unread = registry_fields.intersection(failed_subcalls.get(address, ()))
            # Distinguish a contract that answers none of these getters from one that answers some.
            # A plain ERC-20 has no identityRegistry() at all, so every signature reverts, and that
            # uniform absence is exactly how the sequential probe learned the token defers to no
            # identity registry before classifying it by transfer. Treating those reverts as failed
            # reads suppressed the transfer probe entirely: the 2026-09-05 build measured 0 of 176
            # gates. A partial answer is the dangerous case and still refuses -- a contract that
            # returns three registry getters is registry-aware, so an unreadable fourth could be
            # the gate, and a zero-balance transfer can revert on the balance check before ever
            # reaching it. aggregate3 reports per-call reverts individually while a transport
            # failure raises out of execute_multicall_chunks above, so an unsuccessful sub-call
            # here always means the chain answered.
            registry = decode_registry_results(results)
            if unread and len(unread) < len(registry_fields) and registry.get("system") not in GATING_SYSTEMS:
                # A getter that answered and named a gating registry decides on its own (Securitize
                # DS has getDSService() and no identityRegistry(); Ondo has kycRegistry()), so it is
                # classified below like any other gated token. Short of that, the one partial answer
                # that is safe to probe is a sanctions oracle beside three reverted identity getters:
                # the identity getters are then uniformly absent, exactly the ordinary ERC-20 case,
                # and the doctrine above already declares the sanctions screen non-gating. Measured
                # 2026-10-11: BUIDL, OUSG and USDY had sat "not measured" for a month on this line.
                identity_fields = {f"registry:{signature}" for signature in REGISTRY_SIGNATURES
                                   if signature != "sanctionsList()"}
                if not identity_fields.issubset(unread):
                    continue
            if registry.get("system") in GATING_SYSTEMS:
                probes[address] = GateProbe(
                    chain_id,
                    address,
                    GateStatus.GATED,
                    f"defers to a {registry['system']} identity registry at {registry['registry']}",
                    clock(),
                )
                # The old reader stopped at the first gating registry it found.
                for index, signature in enumerate(REGISTRY_SIGNATURES, start=1):
                    partial = {key: value for key, value in results.items() if key in REGISTRY_SIGNATURES[:index]}
                    if decode_registry_results(partial).get("system") in GATING_SYSTEMS:
                        legacy_round_trips += index * len(target_rows[address])
                        break
            else:
                transfer_addresses.append(address)
                legacy_round_trips += 5 * len(target_rows[address])

        # Two transfers per token, one unit and zero, issued side by side so they read the same
        # block. See classify_transfer_pair for why the second is what makes the first mean
        # anything.
        transfer_calls = tuple(
            MulticallRead(address, transfer_probe_calldata(amount=amount), address, field)
            for address in transfer_addresses
            for amount, field in ((1, TRANSFER_PROBE), (0, ZERO_TRANSFER_PROBE))
        )
        if transfer_calls:
            try:
                transfer_results = execute_multicall_chunks(reader, preferred, transfer_calls)
            except MulticallChunkError as exc:
                fail_chain(str(exc))
                continue
            paired = zip(transfer_calls[0::2], transfer_results[0::2], transfer_results[1::2], strict=True)
            for call, one, zero in paired:
                probe = classify_multicall_transfer(
                    chain_id, call.owner, one=one, zero=zero, probed_at=clock(),
                )
                if probe is not None:
                    probes[call.owner] = probe

        for address, owned_rows in target_rows.items():
            probe = probes.get(address)
            if probe is None:
                fields = failed_subcalls.get(address)
                detail = (
                    f"Multicall3 sub-call failed: {', '.join(fields)}"
                    if fields
                    else "Multicall3 sub-call failed without classifiable return data"
                )
                gate = failure(detail)
                for row in owned_rows:
                    row["gate"] = dict(gate)
                    failed += 1
            else:
                gate = record(probe)
                for row in owned_rows:
                    row["gate"] = dict(gate)
                    measured += 1

    return EligibilityMeasurement(
        measured=measured,
        failed=failed,
        rpc_round_trips=session.round_trips + sequential_round_trips,
        legacy_sequential_round_trips=legacy_round_trips,
        rpc_mode="; ".join(modes) or "not applicable",
        source_errors=tuple(source_errors),
    )


def _previous_attempts() -> dict[str, str]:
    """When the market last answered about each row, whatever it answered.

    Kept apart from `_previous_measurements`, which retains only readings that produced a depth
    because a depth is what it carries forward. Scheduling needs the other question: a genuine
    "no route at any size" IS an answer, and a scheduler that cannot see it treats the row as
    never measured and asks again every night. Enough of those and they eat the budget, and the
    rows that did measure go stale forever while the ladder re-confirms the same refusals.

    A transport failure is not an answer and is deliberately absent: it never reached a market,
    so it should be retried rather than counted as recently asked.
    """
    try:
        prior = json.loads(OUT.read_text())
    except (OSError, TypeError, ValueError):
        # A prior artifact that cannot be read is no history, never a failed build: the run that
        # would fix it is the one being prevented.
        return {}
    out = {}
    for row in prior.get("instruments", []):
        obs, addr = row.get("obtainable"), row.get("address")
        if not obs or not addr:
            continue
        # `last_answered_at` survives runs that could not ask; `measured_at` is the fallback for
        # artifacts published before it existed, and only where a market actually answered.
        answered = obs.get("last_answered_at")
        if not answered and obs.get("measured_at") and (
                obs.get("max_quoted_usd") is not None or obs.get("measured") is True):
            answered = obs["measured_at"]
        if answered:
            out[f"{row.get('chain_id')}:{addr.lower()}"] = str(answered)
    return out


def _previous_measurements() -> dict[str, dict]:
    """Whatever the last successful run measured, keyed by chain and address."""
    try:
        prior = json.loads(OUT.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for row in prior.get("instruments", []):
        obs = row.get("obtainable")
        addr = row.get("address")
        if obs and addr and obs.get("max_quoted_usd") is not None:
            out[f"{row.get('chain_id')}:{addr.lower()}"] = obs
    return out


# What a measured depth actually is, said in the same terms the other depth sources use: the
# method, and what it is not.
MEASURED_DEPTH_SOURCE = "LI.FI executable route quote · largest size still quoted"


def measured_depth_source(observation) -> str:
    tool = (observation.tool_at or {}).get(observation.max_quoted_usd)
    named = f" via {tool}" if tool else ""
    return (f"{MEASURED_DEPTH_SOURCE}{named} · not protocol withdrawable liquidity "
            f"· measured {observation.measured_at.isoformat()}")


# The source behind every executable-quote price observation. One router, one source.
LIFI_PRICE_INDEPENDENCE = "lifi-executable-route-quote"


def executable_price_observations(row: dict, observation) -> list[dict]:
    """Nominal USDC spent per guaranteed unit, rounded down at the demo price scale."""
    from research.par_sources.lifi_quotes import USDC_BY_CHAIN, USDC_DECIMALS_BY_CHAIN

    binding = getattr(observation, "price_binding", None)
    chain = row.get("chain_id")
    if not observation.measured or observation.max_quoted_usd is None or binding is None:
        return []
    if (binding.from_chain != chain or binding.to_chain != chain
            or binding.from_token.lower() != USDC_BY_CHAIN.get(chain, "").lower()
            or binding.to_token.lower() != row["address"].lower()
            or binding.from_decimals != USDC_DECIMALS_BY_CHAIN.get(chain)
            or binding.to_decimals != row.get("decimals")
            or binding.from_amount <= 0 or binding.to_amount_min <= 0):
        return []
    price = (binding.from_amount * 10 ** binding.to_decimals * int(DEMO_PRICE_SCALE)
             // (10 ** binding.from_decimals * binding.to_amount_min))
    if price <= 0:
        return []
    observed_at = datetime.fromtimestamp(binding.observed_at, timezone.utc).isoformat().replace("+00:00", "Z")
    provenance = (f"LI.FI executable route quote tool={binding.tool}; "
                  f"size={binding.from_amount} raw USDC/{10 ** binding.from_decimals}; "
                  f"observed_at={observed_at}; guaranteed output; "
                  "USDC nominal USD unit, not a market mid")
    # One quote is one source, however many times it is taken, so the independence key names
    # the source and nothing else. Folding the size, the timestamp or the route hash into it
    # made every reading its own "independent" source: two quotes of the same token a second
    # apart would satisfy the two-source quorum between them, which is the exact check that
    # stops a single router from pricing an asset alone. The observation-specific detail lives
    # in `source_id`, where it is provenance rather than identity. An asset with only this one
    # source stays unpriced, which is the correct outcome and not a gap to work around.
    return [{
        "identity": f"eip155:{chain}/{registered_kind(row.get('category'))}:{row['address'].lower()}",
        "source_id": provenance,
        "independence_key": LIFI_PRICE_INDEPENDENCE,
        "price_usd_fixed": str(price),
        "observed_at": observed_at,
        "method_version": "lifi-executable-quote/nominal-usdc-guaranteed-output-v1",
    }]


# What the router finding no route says about a depth that came from a route.
NO_ROUTE_DEPTH_SOURCE = "no route quoted at any size"


def no_route_depth_source(observation) -> str:
    return (f"{NO_ROUTE_DEPTH_SOURCE} · the earlier route measurement no longer stands "
            f"· measured {observation.measured_at.isoformat()}")


def carried_forward_depth_source(kept: dict) -> str:
    return (
        f"{kept.get('depth_source') or MEASURED_DEPTH_SOURCE} · carried forward "
        f"from {kept.get('measured_at') or 'an earlier run'}; not re-measured"
    )


def rerate_measured_rows(rows: list[dict]) -> None:
    """Consume private curve inputs, changing only depth where measurement replaced it.

    A rating is arithmetic over a depth, so once measurement replaces the depth the rating
    computed from the old one is no longer about this instrument. Native rows are re-rated here
    from the very curve point they were rated with. A row retained from a previous run because
    its venue source failed arrives with a rating already attached, and that attachment is
    exactly what makes `rate_open_ended` skip it; dropping it is what lets that pass recompute
    the row against the depth it now carries.
    """
    for row in rows:
        point = row.pop("_rating_point", None)
        observation = row.get("obtainable") or {}
        if observation.get("max_quoted_usd") is None:
            # Retained venue rows may carry a previous method's arithmetic even when
            # obtainability did not change. Let rate_open_ended upgrade that contract.
            previous_rating = row.get("rating")
            if (point is None and isinstance(previous_rating, dict)
                    and previous_rating.get("method_version") != RATING_METHOD_VERSION):
                row.pop("rating", None)
            continue
        if point is not None:
            row["rating"] = rating_json(rate_instrument(
                {**point, "liquidity_usd": row["depth_usd"],
                 "obtainable": row.get("obtainable"), "routed_purchase": routed_purchase(row)},
                SHIPPED_ASSUMPTIONS,
            ))
        else:
            row.pop("rating", None)


def measure_obtainability(
    rows: list[dict], *, budget_seconds: float = 1200,
) -> tuple[int, int, int, int, int]:
    """Price acquisition across categories within a shared wall-clock budget.

    A gated result is sufficient reason not to spend routing quota for an arbitrary buyer. Every
    other eligible contract is measured with BID rows first, rotating within each group.
    An incomplete observation keeps a prior reading stale, or leaves reported depth intact when
    no prior reading exists.
    """
    from research.par_sources.lifi_quotes import BUDGET_REFUSAL, USDC_BY_CHAIN, measure

    if (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float))
            or not 0 <= budget_seconds < float("inf")):
        raise ValueError("budget_seconds must be a finite non-negative number")
    deadline = time.monotonic() + budget_seconds

    api_key = os.environ.get("LIFI_API_KEY") or None
    previous = _previous_measurements()
    attempts = _previous_attempts()
    measured = failed = gated = stale = unmeasured = 0

    def priority(row: dict) -> tuple:
        """BID rows first, then longest unmeasured, deepest first among equals.

        Ordering by depth alone is deterministic, and that was the problem: the ladder's budget
        reaches roughly 80 of the 170 eligible rows, so the same head was measured every night
        and the tail was never measured at all -- not once, not slowly, never. Rows already
        carry the time they were last measured, and a row nobody has ever measured sorts before
        every row that has been, so coverage accumulates across nights instead of standing still.
        Each night's unreached rows keep the previous reading, marked stale and dated, so what
        rotation costs is the age of a figure rather than its presence.

        Still deterministic: the same universe and the same previous artifact give the same
        order.
        """
        try:
            depth = Decimal(str(row.get("depth_usd")))
        except (ValueError, ArithmeticError):
            depth = Decimal("-Infinity")
        if not depth.is_finite():
            depth = Decimal("-Infinity")
        address = str(row.get("address") or "").lower()
        # Empty string sorts before every ISO-8601 stamp, which is where the never-measured go.
        last_measured = attempts.get(f"{row.get('chain_id')}:{address}", "")
        return (0 if row.get("verdict") == "BID" else 1,
                last_measured, depth.copy_negate(), row.get("chain_id") or 0, address)

    def skip(row: dict, reason: str) -> None:
        nonlocal stale, unmeasured
        key = f"{row.get('chain_id')}:{row['address'].lower()}"
        kept = previous.get(key)
        # When a market last answered, carried through a run that could not ask. Without it a
        # refused row is published with no scheduling history at all, so the next run reads it as
        # never measured and asks again -- and a row skipped for budget every night would be
        # asked first every night, forever, while the rows that answered went stale.
        answered_before = attempts.get(key)
        if kept:
            row["obtainable"] = {**kept, "stale": True, "stale_reason": reason,
                                 "last_answered_at": answered_before or kept.get("measured_at")}
            row["depth_usd"] = kept["max_quoted_usd"]
            row["depth_source"] = carried_forward_depth_source(kept)
            stale += 1
        else:
            row["obtainable"] = {
                "measured": False, "max_quoted_usd": None, "impact_bps_at": {},
                "tool_at": {}, "refusal": f"not measured: {reason}", "measured_at": None,
                "last_answered_at": answered_before,
            }
            unmeasured += 1

    # Sort a copy before overwriting depth, preserving the published row order.
    for row in sorted(rows, key=priority):
        address, chain_id = row.get("address"), row.get("chain_id")
        if chain_id != 84532:
            row.pop("price_observations", None)
        if not address:
            continue
        # Cash is the thing you pay with. Quoting it against itself measures nothing, and the
        # ladder's budget is the scarcest thing this pass spends.
        if is_settlement(row):
            continue

        gate_status = str((row.get("gate") or {}).get("status") or "").upper()
        if gate_status == "GATED":
            row["obtainable"] = {
                "status": "gated",
                "max_quoted_usd": None,
                "impact_bps_at": {},
                "tool_at": {},
                "measured": False,
                "refusal": "gate status is GATED; route not measured for an arbitrary buyer",
                "measured_at": (row.get("gate") or {}).get("measured_at"),
            }
            gated += 1
            continue

        if chain_id not in USDC_BY_CHAIN:
            skip(row, f"LI.FI USDC quote asset is not configured for chain {chain_id}")
            continue
        decimals = row.get("decimals")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
            skip(row, "decimals must be a non-negative integer")
            continue
        if time.monotonic() >= deadline:
            skip(row, BUDGET_REFUSAL)
            continue

        try:
            observation = measure(
                chain_id, address, api_key=api_key, decimals=decimals, deadline=deadline,
            )
            # A row retained from a previous run arrives carrying that run's measurement, and
            # the depth it produced. Note it before this run's reading replaces it: only a depth
            # that came from a route can be contradicted by the router finding no route.
            route_depth_before = (row.get("obtainable") or {}).get("max_quoted_usd") is not None
            # A market answering and a capacity being measured are different facts. A lower rung
            # can return a route while a higher one is throttled: nothing is published as a
            # maximum, `measured` is false because the surviving error is a transport one, and a
            # market answered all the same. Scheduling asks the first question.
            answered_before = attempts.get(f"{chain_id}:{address.lower()}")
            answered_now = bool(getattr(
                observation, "market_answered",
                observation.max_quoted_usd is not None or observation.measured,
            ) or observation.max_quoted_usd is not None or observation.measured)
            row["obtainable"] = {
                # `measured` separates "the market declined to quote" from "we never got an
                # answer". Only the first is a fact about the instrument.
                "measured": observation.measured,
                "max_quoted_usd": observation.max_quoted_usd,
                "impact_bps_at": observation.impact_bps_at,
                "tool_at": observation.tool_at,
                "refusal": observation.refusal,
                "measured_at": observation.measured_at.isoformat(),
            }
            if observation.max_quoted_usd is not None:
                prices = executable_price_observations(row, observation)
                if prices:
                    row["price_observations"] = prices
                row["depth_usd"] = observation.max_quoted_usd
                # The number changed, so the sentence describing where it came from has to change
                # with it. Aave and Compound rows carry a protocol balance read here, and the
                # terminal prints that provenance beside the figure -- leaving it in place would
                # present route capacity as protocol withdrawable liquidity, which it is not.
                row["depth_source"] = measured_depth_source(observation)
                measured += 1
            elif not observation.measured:
                # The router never answered. Overwriting a real prior measurement with that would
                # let a two-hour quota limit delete evidence we already hold, and publish six
                # buyable instruments as unbuyable. Keep the reading and mark it stale.
                kept = previous.get(f"{chain_id}:{address.lower()}")
                if kept:
                    row["obtainable"] = {**kept, "stale": True,
                                         "stale_reason": observation.refusal}
                    row["depth_usd"] = kept["max_quoted_usd"]
                    row["depth_source"] = carried_forward_depth_source(kept)
                    stale += 1
                else:
                    unmeasured += 1
            else:
                # The router answered, and the answer is that no route exists at any size. That
                # is current evidence about this instrument, and it supersedes a depth an earlier
                # route quote produced: publishing $5,000,000 of route capacity beside "no route
                # quoted" asserts both at once. The depth becomes UNKNOWN rather than zero, and
                # the rating computed from it cannot outlive it. A protocol balance read is not
                # contradicted by a missing route, so a depth from one is left alone.
                if route_depth_before and observation.market_refused_every_size:
                    row["depth_usd"] = None
                    row["depth_source"] = no_route_depth_source(observation)
                    row.pop("rating", None)
                    measured += 1
                elif route_depth_before:
                    # Only the first rung's error survives into `refusal`, so "measured" here can
                    # rest on a refusal at $10,000 while $1,000,000 was rate-limited and never
                    # asked about. That is not a complete answer, and an incomplete answer is not
                    # grounds to delete a reading we hold. Keep it, and say it is not re-measured.
                    kept = previous.get(f"{chain_id}:{address.lower()}")
                    if kept:
                        row["obtainable"] = {**kept, "stale": True,
                                             "stale_reason": observation.refusal}
                        row["depth_usd"] = kept["max_quoted_usd"]
                        row["depth_source"] = carried_forward_depth_source(kept)
                        stale += 1
                    else:
                        measured += 1
                else:
                    measured += 1

            # Every branch above may replace this block, and three of them restore a previous
            # reading wholesale -- which is how three review rounds each found another carrying
            # the old scheduling stamp along with the old depth. One place decides it instead.
            # `measured_at` belongs to whichever reading supplies the depth and is left exactly
            # as that branch wrote it; this says when a market last spoke, which is a fact about
            # tonight whatever depth ends up published.
            block = row.get("obtainable")
            if isinstance(block, dict):
                block["last_answered_at"] = (
                    observation.measured_at.isoformat() if answered_now else answered_before)
        except Exception as exc:  # noqa: BLE001 - transport failure is not a liquidity finding
            row["obtainable"] = {
                "measured": False,
                "max_quoted_usd": None,
                "impact_bps_at": {},
                "tool_at": {},
                "refusal": f"not measured: {exc}"[:240],
                "measured_at": None,
                "last_answered_at": attempts.get(f"{chain_id}:{address.lower()}"),
            }
            failed += 1
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    return measured, failed, gated, stale, unmeasured


# Venue marks, embedded rather than hotlinked. A demo that depends on a third-party CDN at page
# load fails in the room it is being shown in; twenty small images inlined once cost ~60KB and
# cannot fail later. Fetched from DefiLlama's protocol icons, which is also where the venue data
# comes from.
LOGO_SLUGS = {
    "Pendle": "pendle", "Spectra": "spectra", "Exactly": "exactly", "Term Finance": "term-finance",
    "TermMax": "termmax", "Tenor": "tenor", "Morpho": "morpho",
    "Morpho Midnight": "morpho-midnight", "Aave v3": "aave-v3",
    "Compound v3": "compound-v3", "Ondo": "ondo-finance",
    "Midas": "midas-rwa", "Mountain": "mountain-protocol", "Ethena": "ethena",
    "Maple": "maple", "Usual": "usual", "ondo-yield-assets": "ondo-finance",
    "blackrock-buidl": "blackrock-buidl", "usual-usd0": "usual", "midas-rwa": "midas-rwa",
    "openeden-tbill": "openeden", "openeden-usdo": "openeden", "matrixdock-stbt": "matrixdock",
}


def venue_logos(venues: set[str]) -> dict[str, str]:
    """Fetch each venue's mark once and inline it as a data URI."""
    import base64

    out: dict[str, str] = {}
    for venue in sorted(venues):
        slug = LOGO_SLUGS.get(venue)
        if not slug:
            continue
        url = f"https://icons.llamao.fi/icons/protocols/{slug}?w=48&h=48"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=20) as r:
                blob = r.read()
                mime = r.headers.get("Content-Type", "image/png").split(";")[0]
            if len(blob) > 40_000:      # a mark that large is not a mark
                continue
            out[venue] = f"data:{mime};base64," + base64.b64encode(blob).decode()
        except Exception:  # noqa: BLE001 - a missing logo is cosmetic, never fatal
            continue
        time.sleep(0.1)
    return out


def row_rating_point(row: dict) -> dict:
    """Reconstruct arithmetic inputs from a published row without refreshing evidence."""
    rate = row.get("rate_bps")
    apy = None if rate is None else rate / 10_000
    indicative = row.get("rate_kind") == "indicative"
    return {
        "instrument": row.get("name"), "venue": row.get("venue"),
        "status": row.get("status"), "rejection_basis": row.get("rejection"),
        "tier": row.get("tier"), "rate_kind": row.get("rate_kind"),
        "executable_apy": None if indicative else apy,
        "indicative_apy": apy if indicative else None,
        "days_to_maturity": row.get("days_to_maturity"),
        "liquidity_usd": row.get("depth_usd"),
        "obtainable": row.get("obtainable"),
        "routed_purchase": routed_purchase(row),
    }


def rerate_snapshot(payload: dict) -> None:
    """Upgrade only rating arithmetic, preserving snapshot timestamps and market evidence."""
    for row in payload["instruments"]:
        if (not row.get("rating")
                and (row.get("is_stablecoin") is True or row.get("category") == "stablecoin")):
            continue
        rating = rate_instrument(row_rating_point(row), SHIPPED_ASSUMPTIONS)
        days = row.get("days_to_maturity")
        if (row.get("rate_kind") == "declared_demo" and isinstance(row.get("maturity"), str)
                and type(days) is int and days <= 0):
            rating = replace(rating, computable=False,
                             refusal="MATURED: the redemption date has passed, so there is no horizon left to rate")
        row["rating"] = rating_json(rating)
    payload["assumptions"] = SHIPPED_ASSUMPTIONS.to_json()
    payload["rating_method"] = {"method_version": RATING_METHOD_VERSION, "model": NET_AFTER_COSTS}


def rate_open_ended(rows: list[dict]) -> int:
    """Attach a rating to vaults and tokenized funds, which have no maturity of their own.

    Only native rows were rated, so every vault and fund showed an empty net-yield cell — not
    because the evidence was missing but because the arithmetic had no tenor to amortise an entry
    cost across. The shipped horizon supplies one, and `rate_basis` on the result records that the
    number came from a trailing or issuer-published rate rather than a quote.
    """
    rated = 0
    for row in rows:
        if row.get("category") == "native" or row.get("rating") or row.get("rate_bps") is None:
            continue
        # A stablecoin is cash, not a position with a yield. Rating one gave the demo's own unit
        # of account a headline rate, and everything else is priced in it.
        if row.get("is_stablecoin") is True or row.get("category") == "stablecoin":
            continue
        point = row_rating_point(row)
        rating = rate_instrument(point, SHIPPED_ASSUMPTIONS)
        row["rating"] = rating_json(rating)
        if rating.computable:
            rated += 1
    return rated


def _previous_instruments() -> list[dict]:
    try:
        payload = json.loads(OUT.read_text())
    except (OSError, ValueError):
        return []
    rows = payload.get("instruments")
    return rows if isinstance(rows, list) else []


def _status_with_provenance(result: VenueResult, status: dict) -> dict:
    return {
        **status,
        "omitted": result.omitted,
        "endpoint": result.endpoint,
        "depth_source": result.depth_source,
        "rpc_round_trips": result.rpc_round_trips,
        "rpc_mode": result.rpc_mode,
        "rpc_endpoints": list(result.rpc_endpoints),
        "omission_reasons": list(result.omission_reasons),
    }


def _collect_variable_venue(
    venue: str, result: VenueResult, previous: list[dict]
) -> tuple[list[dict], dict]:
    rows, status = retain_previous_rows(
        venue=venue,
        rows=result.rows,
        error=result.error,
        previous_rows=previous,
    )
    return rows, _status_with_provenance(result, status)


def _isolated_venue(venue: str) -> int:
    """Print one variable-yield source without writing or probing unrelated sources."""
    previous = _previous_instruments()
    if venue == "morpho":
        rows, error = vault_rows()
        result = VenueResult(
            tuple(rows),
            error,
            0,
            "POST https://blue-api.morpho.org/graphql vaults",
            "vault.liquidity.usd",
        )
        display_name = "Morpho"
    else:
        try:
            pools = _yield_pools()
        except Exception as exc:  # noqa: BLE001 - malformed 200 is a source failure too
            pools = []
            result = VenueResult(
                (),
                f"DefiLlama trailing-rate source failed: {exc}",
                0,
                "GET https://yields.llama.fi/pools",
                "not measured because the rate source failed",
            )
        else:
            result = aave_v3_rows(pools) if venue == "aave-v3" else compound_v3_rows(pools)
        display_name = "Aave v3" if venue == "aave-v3" else "Compound v3"

    rows, status = _collect_variable_venue(display_name, result, previous)
    rate_open_ended(rows)
    print(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "venue": display_name,
        "source": status,
        "instruments": rows,
    }, indent=2))
    return 1 if status["error"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rerate-only", action="store_true",
                      help="update existing snapshot ratings only; no network, new timestamps or market data")
    mode.add_argument(
        "--venue",
        choices=("morpho", "aave-v3", "compound-v3"),
        help="print one live variable-yield adapter as JSON; never write web/universe.json",
    )
    args = parser.parse_args(argv)
    if args.rerate_only:
        payload = json.loads(OUT.read_text())
        rerate_snapshot(payload)
        OUT.write_text(json.dumps(payload, indent=2) + "\n")
        return 0
    if args.venue:
        return _isolated_venue(args.venue)

    previous = _previous_instruments()
    native = native_rows()
    morpho_live, morpho_err = vault_rows()
    morpho_result = VenueResult(
        tuple(morpho_live),
        morpho_err,
        0,
        "POST https://blue-api.morpho.org/graphql vaults",
        "vault.liquidity.usd",
    )

    try:
        pools = _yield_pools()
        pools_error = None
    except Exception as exc:  # noqa: BLE001 - malformed 200 is a source failure too
        pools = []
        pools_error = f"DefiLlama trailing-rate source failed: {exc}"

    if pools_error:
        aave_result = VenueResult(
            (), pools_error, 0, "GET https://yields.llama.fi/pools",
            "not measured because the rate source failed",
        )
        compound_result = VenueResult(
            (), pools_error, 0, "GET https://yields.llama.fi/pools",
            "not measured because the rate source failed",
        )
    else:
        aave_result = aave_v3_rows(pools)
        compound_result = compound_v3_rows(pools)

    morpho, morpho_status = _collect_variable_venue("Morpho", morpho_result, previous)
    aave, aave_status = _collect_variable_venue("Aave v3", aave_result, previous)
    compound, compound_status = _collect_variable_venue(
        "Compound v3", compound_result, previous
    )
    vaults = morpho + aave + compound
    vault_errors = [
        status["error"] for status in (morpho_status, aave_status, compound_status)
        if status["error"]
    ]
    vault_err = "; ".join(vault_errors) or None

    rwa, rwa_err = rwa_rows(pools)
    if pools_error:
        rwa_err = pools_error
    demo_rows = testnet_rows()
    everything = (native + vaults + rwa + verified_rwa_rows(pools)
                  + settlement_rows() + demo_rows)
    for row in everything:
        row.setdefault("maturity_timestamp", None)
    eligibility = measure_eligibility(everything)
    (obtainable, unobtainable, gated,
     kept_stale, never_measured) = measure_obtainability(everything)
    # after obtainability, so a measured route can satisfy the depth test
    rerate_measured_rows(everything)
    open_ended_rated = rate_open_ended(everything)
    generated_at = datetime.now(timezone.utc)
    restate_demo_rows(demo_rows, generated_at)

    payload = {
        "generated_at": generated_at.isoformat(),
        "input_freshness": json.loads(CURVE.read_text()).get("input_freshness"),
        "assumptions": SHIPPED_ASSUMPTIONS.to_json(),
        "rating_method": {
            "method_version": RATING_METHOD_VERSION,
            "model": NET_AFTER_COSTS,
        },
        "notes": {
            "rate_kind": {
                "executable": "quoted by the venue and shown obtainable at a size",
                "indicative": "published by the venue, not shown obtainable at any size",
                "realized": "trailing observation, not an offer — duration is unmatched",
                "issuer_quoted": "the issuer's published figure; Par has not verified obtainability",
                "declared_demo": "synthetic Base Sepolia testnet declaration; not a market observation",
            },
            "depth_usd": "only set where a figure measures what can be exited or acquired now; "
                         "fund TVL is never used for this",
        },
        "sources": {
            "native": {"rows": len(native), "error": None},
            "vault": {
                "rows": len(vaults),
                "error": vault_err,
                "venues": {
                    "morpho": morpho_status,
                    "aave_v3": aave_status,
                    "compound_v3": compound_status,
                },
            },
            "rwa": {"rows": len(rwa), "error": rwa_err},
        },
        # Whatever corroboration rule produced the curve is a property of these numbers and
        # travels with them; the page states it rather than leaving a reader to assume the
        # strongest one was used.
        "peg_corroboration": json.loads(
            CURVE.read_text()).get("peg_corroboration"),
        "eligibility": {
            "measured": eligibility.measured,
            "not_measured": eligibility.failed,
            "rpc_round_trips": eligibility.rpc_round_trips,
            "legacy_sequential_round_trips": eligibility.legacy_sequential_round_trips,
            "rpc_mode": eligibility.rpc_mode,
            "error": "; ".join(eligibility.source_errors) or None,
        },
        "obtainability": {"measured": obtainable, "not_measured": never_measured,
                          "gated": gated, "kept_from_previous_run": kept_stale},
        "instruments": everything,
    }
    payload["logos"] = venue_logos({str(r.get("venue")) for r in everything if r.get("venue")})
    OUT.write_text(json.dumps(payload, indent=1) + "\n")

    for cat, info in payload["sources"].items():
        state = info["error"] or f"{info['rows']} rows"
        print(f"  {cat:<8} {state}")
    print(f"  rated    {open_ended_rated} open-ended instruments now carry a net yield")
    print(
        f"  gates    {eligibility.measured} measured, {eligibility.failed} not measured; "
        f"{eligibility.rpc_round_trips} RPC round trips "
        f"({eligibility.legacy_sequential_round_trips} under the prior sequential probe plan)"
    )
    print(f"  routes   {obtainable} measured, {kept_stale} kept from a previous run, "
          f"{never_measured} never measured, {gated} gated")
    # OUT is only inside ROOT for the ordinary in-repo build. The scheduled refresh points
    # FENORIUM_UNIVERSE_OUTPUT at a candidate outside the repo, and relative_to raises there --
    # which crashed a completed build on its closing log line.
    try:
        destination = OUT.relative_to(ROOT)
    except ValueError:
        destination = OUT
    print(f"  total    {len(payload['instruments'])} instruments -> {destination}")
    return 1 if (vault_err and rwa_err) else 0


if __name__ == "__main__":
    sys.exit(main())
