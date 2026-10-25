#!/usr/bin/env python3
"""Build deterministic, fully synthetic Fenorium testnet scenario fixtures.

The artifact is local UI data only. It does not read RPCs, wallets, keys, or live market data.
Run from the repository root with:
  python3 scripts/build_testnet_scenarios.py --as-of 2026-10-20 --output web/data/testnet-scenarios.json

Without --as-of the fixed documented date 2026-10-20 is used, keeping checked-in output stable.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.platform.rerating import _rating_assumptions, _rating_point, _rating_json
from engine.rate_conventions import rate_convention
from engine.rating import rate_instrument
from engine.portfolio import (
    AnalyticsScope, AssetKind, BlockReference, CapabilityStatus, DailyReturn,
    MarkingFrequency, PriceObservation, RawPosition, RegisteredAsset, ReturnSeries,
    classify_position, compute_portfolio_analytics, value_portfolio,
)


DEFAULT_AS_OF = date(2026, 10, 20)
SCHEMA_VERSION = "fenorium/testnet-scenarios/1"
METHOD_VERSION = "fenorium-synthetic-scenarios-v1"
# Real supported chain IDs identify the network represented in the mock. Every row, address,
# balance, and observation is synthetic; the fixture never asserts these values exist on-chain.
MOCK_CHAINS = ((1, "Ethereum · simulated"), (10, "OP Mainnet · simulated"),
               (56, "BNB Smart Chain · simulated"), (137, "Polygon · simulated"),
               (42161, "Arbitrum One · simulated"), (8453, "Base · simulated"))


def _iso_day(day: date) -> str:
    return day.isoformat()


def _timestamp(day: date, hour: int = 12) -> str:
    return datetime.combine(day, time(hour=hour), tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _market(as_of: date) -> list[dict[str, Any]]:
    # 33 of each: 99 market rows (native, RWA, vault) and 33 stablecoins for the portfolios
    # (owner 2026-10-20: about a hundred instruments on the Market).
    categories = (["native", "rwa", "vault", "stablecoin"] * 33)
    # Maturity dates are the deployed tokens' declared terms, fixed at the catalog's declaration
    # date; `as_of` only moves the remaining tenor (review ed: a regeneration at another date moved
    # them away from the page catalog's schedules).
    maturities = [DEFAULT_AS_OF + timedelta(days=days) for days in (14, 95, 182, 365, 730)]
    rows: list[dict[str, Any]] = []
    for i, category in enumerate(categories):
        chain_id, chain = MOCK_CHAINS[i % len(MOCK_CHAINS)]
        maturity = maturities[(i * 3) % len(maturities)] if category in ("native", "rwa") else None
        score = 59 if i == 0 else 60 if i == 1 else 42 + (i * 17) % 57
        liquidity = 1_250_000 + (i * 97_331) % 18_000_000
        quote_status = "AVAILABLE"
        price_status = "PRICED"
        venue = ("Exactly" if i == 0 else "Pendle" if category == "native" else "Tokenized Treasury" if category == "rwa"
                 else ("Aave v3" if i % 2 else "Morpho") if category == "vault" else "Stablecoin issuer")
        symbol = (f"PT-SYN{i + 1:02d}" if category == "native" else f"RWA{i + 1:02d}" if category == "rwa"
                  else f"vUSD{i + 1:02d}" if category == "vault" else f"sUSD{i + 1:02d}")
        address = None if i == 0 else f"0x{i + 1:040x}"
        name = f"{symbol} Synthetic {category.upper()} Instrument"
        headline_bps = 0 if category == "stablecoin" else 250 + (i * 31) % 701
        # The Fenorium Score verdicts fixed-maturity rows only; a fund, vault or stablecoin carries
        # none, as in the published universe (deploy/platform/rerating.py).
        verdict = ("BID" if score >= 60 else "NO BID") if category == "native" else None
        row = {
            "_index": i, "id": f"mock-instrument-{i + 1:02d}",
            "identity": f"eip155:{chain_id}/erc20:{address.lower()}" if address else None,
            "chain_id": chain_id, "chain": chain, "address": address,
            "name": name, "symbol": symbol,
            "category": category, "asset_type": ("variable_income" if category == "vault" else
                "stablecoin" if category == "stablecoin" else "fixed_income"),
            "venue": venue, "issuer": f"Synthetic Issuer {(i % 8) + 1}",
            "maturity": _iso_day(maturity) if maturity else None,
            "maturity_timestamp": int(datetime.combine(maturity, time(), tzinfo=timezone.utc).timestamp()) if maturity else None,
            "days_to_maturity": (maturity - as_of).days if maturity else None,
            "rate_bps": headline_bps, "ytm_bps": headline_bps if category in ("native", "rwa") else None,
            "apy_bps": headline_bps if category in ("vault", "stablecoin") else None,
            "implied_apy_bps": headline_bps, "rate_kind": "declared_demo" if headline_bps is not None else None,
            "fenorium_score": score, "verdict": verdict, "status": verdict,
            "tier": "TIER_A" if score >= 85 else "TIER_B" if score >= 70 else "TIER_C",
            "tier_source": "synthetic tier scenario · not a credit assessment",
            "depth_usd": liquidity, "liquidity_usd": liquidity,
            "tvl_usd": (2_000_000 + i * 125_000) if category in ("rwa", "vault") else None,
            "tvl_source": "synthetic fund-size scenario · not a market observation",
            "depth_source": "synthetic executable-depth scenario",
            "rejection": "credit" if verdict == "NO BID" else None,
            "gate": {"status": "GATED" if category == "rwa" and (i // 4) % 4 == 0 else "PERMISSIONLESS",
                     "reason": "synthetic transfer-gate scenario; not measured on any chain",
                     "measured_at": _timestamp(as_of)},
            "tokenised": category in ("native", "rwa", "vault"),
            "quote_status": quote_status, "price_status": price_status,
            "price_usd": 0.985 + (i % 25) / 1000,
            "is_stablecoin": category == "stablecoin", "is_variable_income": category == "vault",
            "simulation": True, "simulated_network": True, "is_demo": False,
            "data_status": "SYNTHETIC", "evidence_as_of": _timestamp(as_of),
            "reason": ((f"NO BID (credit) — synthetic Fenorium Score {score} is below 60. "
                        if verdict == "NO BID" else "") +
                       "Deterministic synthetic scenario data; not a market quote or offer."),
        }
        row["rate_convention"] = rate_convention(row)
        if i == 0:
            # Addressless Exactly position with renderer-compatible instrument identity.
            row["position_type"] = "position"
            row["identity"] = (f"eip155:{chain_id}/position:{venue.lower().replace(' ', '-')}:"
                               f"{quote(name, safe='_.-~@')}")
        # Declared desk purchases have no LI.FI ladder or routing fee; the shared
        # rating-point classifier keeps this catalog on the modelled size-cost basis.
        row["rating"] = _rating_json(rate_instrument(_rating_point(row), _rating_assumptions(250_000, 10)))
        row["ratings_by_size"] = {
            str(size): _rating_json(rate_instrument(_rating_point(row), _rating_assumptions(size, 10)))
            for size in (10_000, 50_000, 250_000, 1_000_000)
        }
        for size, rating in [("250000", row["rating"]), *row["ratings_by_size"].items()]:
            rating["size_usd"] = size
            if not rating["computable"]:
                raise RuntimeError(f"Synthetic rating unavailable for {row['id']} at ${size}: {rating['refusal']}")
        row["decision_hashes_by_size"] = {size: "sha256:" + hashlib.sha256(json.dumps({
            "identity": row["identity"], "rating": rating, "verdict": row["verdict"],
            "fenorium_score": row["fenorium_score"], "evidence_as_of": row["evidence_as_of"],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            for size, rating in row["ratings_by_size"].items()}
        row["decision_hash"] = row["decision_hashes_by_size"]["250000"]
        rows.append(row)
    for i, row in enumerate(rows):
        positions = _portfolio_rows(rows, [i], as_of)
        valuation = _engine_valuation(positions, as_of)
        profile = ("conservative" if row["category"] in ("stablecoin", "rwa") else
                   "balanced" if row["category"] == "vault" else "concentrated")
        returns = _daily_returns(as_of, 400, profile, seed=i)
        analytics = _engine_analytics(valuation, returns, as_of, history=400).to_dict()
        metrics = {"expected_shortfall_95_bps": "expected_shortfall_95",
                   "volatility_bps": "volatility", "max_drawdown_bps": "drawdown"}
        for metric in metrics.values():
            if analytics[metric]["status"] != "COMPUTED":
                raise RuntimeError(f"Synthetic {metric} unavailable for {row['id']}")
        row["instrument_risk"] = {
            **{field: analytics[metric]["value"] for field, metric in metrics.items()},
            "observations": 400, "window": "TRAILING_DAILY_UTC", "method_version": METHOD_VERSION,
            "source": "synthetic daily return scenario · not a market observation",
        }
    return rows


def _deployed(rows: list[dict[str, Any]], manifest: dict[str, Any], declared_at: datetime) -> list[dict[str, Any]]:
    from scripts.build_universe import catalog_price_fields, registered_kind

    instruments = manifest["instruments"]
    by_index = {item["index"]: item for item in instruments}
    indices = {row["_index"] for row in rows}
    if (manifest["chain_id"] != 84532 or len(by_index) != len(instruments)
            or len(indices) != len(rows) or indices != set(by_index)):
        raise ValueError("catalog deployment must join one-to-one on index on Base Sepolia")
    for row in rows:
        item = by_index[row["_index"]]
        if row["symbol"] != item["symbol"]:
            raise ValueError(f"catalog symbol differs at index {row['_index']}")
    deployed = []
    for row in rows:
        item = by_index[row["_index"]]
        row = {**row,
               "modelled_on": f"{row['venue']} · {row['chain'].removesuffix(' · simulated')}",
               "chain_id": 84532, "chain": "Base Sepolia", "address": item["instrument"], "decimals": 6,
               "identity": f"eip155:84532/{registered_kind(row['category'])}:{item['instrument']}",
               "venue": "Fenorium testnet catalog", "desk": item["desk"],
               "simulated_network": False, "simulation": False, "deployed": True}
        row.pop("position_type", None)
        row.update(catalog_price_fields(row, item, declared_at))
        row["decision_hashes_by_size"] = {size: "sha256:" + hashlib.sha256(json.dumps({
            "identity": row["identity"], "rating": rating, "verdict": row["verdict"],
            "fenorium_score": row["fenorium_score"], "evidence_as_of": row["evidence_as_of"],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            for size, rating in row["ratings_by_size"].items()}
        row["decision_hash"] = row["decision_hashes_by_size"]["250000"]
        deployed.append(row)
    return deployed


def _daily_returns(as_of: date, count: int, profile: str, *, seed: int | None = None) -> list[dict[str, str]]:
    rng = random.Random(391_000 + count + sum(ord(c) for c in profile) if seed is None else seed)
    result = []
    for offset in range(count - 1, -1, -1):
        n = count - 1 - offset
        if profile == "stressed":
            bps = (-38 if n in (count // 3, count // 2) else rng.randint(-27, 19))
        elif profile == "conservative":
            bps = rng.randint(-5, 7)
        elif profile == "concentrated":
            bps = rng.randint(-24, 20)
        else:
            bps = rng.randint(-11, 12)
        result.append({"day": _iso_day(as_of - timedelta(days=offset)), "return_bps": f"{bps:.6f}"})
    return result


def _portfolio_rows(market: list[dict[str, Any]], indices: list[int], as_of: date,
                    *, unpriced: bool = False) -> list[dict[str, Any]]:
    positions = []
    for order, index in enumerate(indices):
        row = market[index]
        target_value_micros = (80_000 + (index * 7_919) % 150_000) * 1_000_000
        price_micros = 1_000_000 if row["category"] == "stablecoin" else 975_000 + (index % 17) * 1_500
        units = (target_value_micros * 1_000_000 + price_micros // 2) // price_micros
        if unpriced and order == len(indices) - 1:
            price_micros = None
        value = units * price_micros // 1_000_000 if price_micros is not None else None
        positions.append({
            "id": f"scenario-position-{index + 1:02d}",
            "position_type": row.get("position_type", "erc20"), "address": row.get("address"),
            "identity": row["identity"], "chain_id": row["chain_id"], "symbol": row["symbol"],
            "name": row["name"], "asset_type": row["asset_type"], "category": row["category"],
            "is_stablecoin": row["is_stablecoin"], "is_variable_income": row["is_variable_income"],
            "issuer": row["issuer"], "venue": row["venue"],
            "raw_units": str(units), "decimals": 6, "price_micros": str(price_micros) if price_micros is not None else None,
            "value_usd_micros": str(value) if value is not None else None,
            "status": "SUPPORTED" if price_micros is not None else "UNPRICED",
            "unresolved": False, "deployable": False,
            "price_source": "synthetic scenario mark", "source_timestamp": _timestamp(as_of),
            "yield_bps": row["rate_bps"], "rate_bps": row["rate_bps"], "rate_kind": row["rate_kind"],
            "maturity": row["maturity"], "risk_supported": True,
        })
    return positions


def _engine_valuation(positions: list[dict[str, Any]], as_of: date):
    as_of_ts = datetime.combine(as_of, time(), tzinfo=timezone.utc)
    classified = []
    observations = []
    direct_values = {}
    for row in positions:
        category = row["category"]
        kind = (AssetKind.POSITION if row["position_type"] == "position" else
                AssetKind.PT if category == "native" else
                AssetKind.VAULT if category == "vault" else AssetKind.ERC20)
        asset = RegisteredAsset(
            chain_id=row["chain_id"], kind=kind,
            address=row["address"] if kind is not AssetKind.POSITION else None,
            decimals=row["decimals"], symbol=row["symbol"],
            category="CASH" if category == "stablecoin" else "VARIABLE_INCOME" if category == "vault" else "FIXED_INCOME",
            protocol=row["venue"], is_stablecoin=row["is_stablecoin"],
            is_variable_income=row["is_variable_income"],
            valuation_support=CapabilityStatus.SUPPORTED,
            risk_support=CapabilityStatus.SUPPORTED,
            fixed_income_support=CapabilityStatus.SUPPORTED if category in ("native", "rwa") else CapabilityStatus.UNSUPPORTED,
            permission_support=CapabilityStatus.SUPPORTED,
            execution_support=CapabilityStatus.SUPPORTED,
            instrument_id=row["identity"] if kind is AssetKind.POSITION else None,
        )
        classified_row = classify_position(RawPosition(
            asset, int(row["raw_units"]), BlockReference(row["chain_id"], 1_000_000 + row["chain_id"])))
        classified.append(classified_row)
        if kind is AssetKind.POSITION:
            direct_values[classified_row.identity] = int(row["value_usd_micros"])
        elif row["price_micros"] is not None:
            observations.append(PriceObservation(
                identity=classified_row.identity, source_id=f"synthetic-mark:{row['id']}",
                independence_key=f"synthetic-mark:{row['id']}",
                price_usd_fixed=int(row["price_micros"]) * 100,
                observed_at=as_of_ts, method_version=METHOD_VERSION,
            ))
    return value_portfolio(
        classified, observations, as_of=as_of_ts, method_version=METHOD_VERSION,
        max_price_age_seconds=1, position_values_usd_micros=direct_values,
    )


def _engine_analytics(valuation, returns: list[dict[str, str]], as_of: date, *, history: int):
    as_of_ts = datetime.combine(as_of, time(), tzinfo=timezone.utc)
    series = []
    if history:
        points = [DailyReturn(datetime.combine(date.fromisoformat(row["day"]), time(), tzinfo=timezone.utc),
                              round(float(row["return_bps"]))) for row in returns]
        for priced in valuation.positions:
            if priced.value_usd_micros is None:
                continue
            series.append(ReturnSeries(
                position_id=priced.identity, frequency=MarkingFrequency.UTC_DAILY,
                annualization_factor=365, returns=tuple(points), method_version=METHOD_VERSION,
                provenance="modelled",
            ))
    scope = (AnalyticsScope.VALUED_HOLDINGS if valuation.positions
             else AnalyticsScope.TOTAL)
    return compute_portfolio_analytics(
        valuation, series, as_of=as_of_ts, method_version=METHOD_VERSION,
        trailing_days=400, scope=scope,
    )


def _snapshot(name: str, market: list[dict[str, Any]], indices: list[int], as_of: date,
              *, history: int = 400, profile: str = "balanced", unpriced: bool = False) -> dict[str, Any]:
    positions = _portfolio_rows(market, indices, as_of, unpriced=unpriced)
    returns = _daily_returns(as_of, history, profile)
    valuation_result = _engine_valuation(positions, as_of)
    analytics_result = _engine_analytics(valuation_result, returns, as_of, history=history)
    valuation = valuation_result.to_dict()
    analytics = analytics_result.to_dict()
    valued = valuation_result.priced_nav_lower_bound_usd_micros
    excluded = [position.identity for position in valuation_result.positions
                if position.value_usd_micros is None]
    nav_points = []
    if valued and history:
        nav = valued
        points_reversed = [(as_of, nav)]
        for entry in reversed(returns):
            nav = max(1, round(nav / (1 + float(entry["return_bps"]) / 10_000)))
            points_reversed.append((date.fromisoformat(entry["day"]) - timedelta(days=1), nav))
        nav_points = [{"as_of": _iso_day(day), "nav_usd_micros": str(value)}
                      for day, value in reversed(points_reversed)]
    return {
        "schema_version": "PortfolioSnapshot/v1", "method_version": METHOD_VERSION,
        "provenance": {"kind": "LOCAL_DEMO_FIXTURE", "scenario_kind": "SYNTHETIC_TESTNET_SCENARIO",
                       "label": "SIMULATED · synthetic scenario data",
                       "as_of": _timestamp(as_of), "source": "deterministic local fixture; no wallet, RPC, or market reads"},
        "account": "0x0000000000000000000000000000000000005e71", "scope": "REGISTERED_ASSETS_ONLY",
        "chains": [{"chain_id": chain, "rpc_status": "SIMULATED",
                    "source_timestamp": _timestamp(as_of)}
                   for chain in sorted({market[i]["chain_id"] for i in indices})],
        "positions": positions, "risk_scope": {"nav_scope": analytics["nav_scope"], "valued_usd_micros": str(valued),
            "excluded": [{"identity": identity, "symbol": next(p["symbol"] for p in positions if p["identity"] == identity),
                          "reason": "MISSING_PRICE"} for identity in excluded],
            "modelled_positions": sum(position.value_usd_micros is not None
                                      for position in valuation_result.positions)},
        "valuation": valuation,
        "analytics": analytics,
        "nav_series": {"method_version": METHOD_VERSION, "source": "synthetic risk return path; no cash flows",
                       "points": nav_points, "external_flows": "none"},
    }


def _activity(market: list[dict[str, Any]], indices: list[int], as_of: date) -> list[dict[str, Any]]:
    return [{"id": f"synthetic-activity-{index + 1:02d}",
             "day": _iso_day(as_of - timedelta(days=7 + offset * 11)),
             "kind": "distribution" if offset % 2 == 0 else "allocation",
             "symbol": market[index]["symbol"], "amount": f"{25 + offset * 37}.00",
             "chain_id": market[index]["chain_id"], "status": "Simulated"}
            for offset, index in enumerate(indices[:3])]


def build(as_of: date = DEFAULT_AS_OF) -> dict[str, Any]:
    market = _market(as_of)
    manifest_path = Path(os.environ.get(
        "FENORIUM_CATALOG_MANIFEST", ROOT / "contracts/deployments/base-sepolia-catalog.json"))
    if manifest_path.exists() or "FENORIUM_CATALOG_MANIFEST" in os.environ:
        market = _deployed(market, json.loads(manifest_path.read_text()),
                           datetime.combine(as_of, time(hour=12), tzinfo=timezone.utc))
    specs = [
        ("balanced", "Balanced", "Diversified, fully priced synthetic book.", list(range(0, 8)), "balanced", 400, False),
        ("conservative", "Conservative", "Stablecoins and short dated fixed income.", [1, 3, 5, 7, 9, 11, 15], "conservative", 400, False),
        ("concentrated", "Concentrated", "Large exposure to one synthetic issuer across several asset classes.", [0, 8, 16, 24, 32], "concentrated", 400, False),
        # Every catalog token is deployed on Base Sepolia (owner 2026-10-21), so no book of them can
        # spread across networks; this one spreads across the asset classes instead.
        ("multichain", "All asset classes", "Fixed maturity, tokenized funds, vaults and stablecoins from the catalog, modelled on six networks and deployed on Base Sepolia.", [0, 1, 2, 3, 4, 5], "balanced", 400, False),
        ("stressed", "Stressed", "Synthetic return shocks and volatile marks.", [1, 2, 5, 8, 14, 17], "stressed", 400, False),
        ("incomplete", "Incomplete valuation", "One holding has no price; total NAV is unavailable.", [1, 8, 11], "balanced", 400, True),
        ("short-history", "Short history", "Only 120 aligned observations; risk metrics are unavailable.", [2, 9, 19], "balanced", 120, False),
        ("empty", "Empty", "No registered holdings or daily observations.", [], "balanced", 0, False),
    ]
    return {"schema_version": SCHEMA_VERSION, "as_of": _iso_day(as_of),
            "universe": {"generated_at": _timestamp(as_of), "instrument_count": len(market),
                         "source": "deterministic synthetic scenario universe", "simulation_only": True,
                         "metadata": {"generated_at": _timestamp(as_of),
                                      "source": "deterministic synthetic scenario universe",
                                      "simulation_only": True,
                                      "categories": ["native", "rwa", "vault", "stablecoin"],
                                      "mock_chain_ids": [chain[0] for chain in MOCK_CHAINS],
                                      "score_thresholds": {"watch_below": 60, "eligible_from": 60},
                                      "quote_states": ["AVAILABLE"],
                                      "price_states": ["PRICED"]}, "instruments": market},
            "scenarios": [{"id": scenario_id, "label": label, "description": description,
                           "snapshot": _snapshot(scenario_id, market, indices, as_of, history=history,
                                                 profile=profile, unpriced=unpriced),
                           "activity": _activity(market, indices, as_of)}
                          for scenario_id, label, description, indices, profile, history, unpriced in specs]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF.isoformat(), help="fixed scenario date (YYYY-MM-DD); default 2026-10-20")
    parser.add_argument("--output", type=Path, default=ROOT / "web/data/testnet-scenarios.json", help="output JSON path")
    args = parser.parse_args()
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError as exc:
        parser.error(f"--as-of must be YYYY-MM-DD: {exc}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build(as_of), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
