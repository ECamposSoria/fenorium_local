from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "research"
if str(RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(RESEARCH_DIR))

from research.validate import _fetch_context, quality_and_dispersion
from research.par_sources.termmax import TermMaxClient

from .demo import _current_credit_rows, _evidence_as_of
from .credit import CreditInputs, TimedValue, score_credit
from .evidence_inputs import CHAIN_NAMESPACES, DOCUMENTED_FEEDS, EvidenceLoader
from .ingest import EngineHttpClient, ingest_all
from .universe import (build_universe, record_identity, refine_unknown_denominations,
                        refine_with_onchain_asset)


def _summary(rows: list[tuple[str, Any, Any]], decisions: dict, markets: list[Any],
             unquoted: list[Any] | None = None) -> dict[str, Any]:
    rejected = [(name, decision) for name, _tier, decision in rows if not decision.bid]
    credit_only = [name for name, decision in rejected
                   if decision.credit_blockers and not decision.evidence_blockers]
    evidence_only = [name for name, decision in rejected
                     if decision.evidence_blockers and not decision.credit_blockers]
    mixed = [name for name, decision in rejected
             if decision.credit_blockers and decision.evidence_blockers]
    missing = Counter(
        field for _name, decision in rejected if decision.evidence_blockers
        for field in decision.missing_fields
    )
    capacities = {
        identity: market.executable_capacity_usd
        for market in markets
        if (identity := record_identity(market)) is not None
    }
    evaluable_capacity = 0.0
    capacity_missing: list[str] = []
    for name, _tier, decision in rows:
        if decision.evidence_blockers:
            continue
        key = next((key for key, value in decisions.items() if value is decision), None)
        capacity = capacities.get(key) if key is not None else None
        if capacity is None:
            capacity_missing.append(name)
        else:
            evaluable_capacity += float(capacity)
    unquoted = unquoted or []
    return {
        # An instrument nobody is quoting is still in the universe. Counting only the scorable
        # rows would report a smaller market than the one we actually see.
        "universe": len(rows) + len(unquoted),
        "unquoted_no_lend_book": len(unquoted),
        "unquoted_instruments": sorted(row.name for row in unquoted),
        "total": len(rows),
        "bid": sum(decision.bid for _name, _tier, decision in rows),
        "rejected": len(rejected),
        "credit_only": len(credit_only),
        "evidence_only": len(evidence_only),
        "mixed": len(mixed),
        "credit_rejections": len(credit_only) + len(mixed),
        "evidence_rejections": len(evidence_only) + len(mixed),
        "assessed": len(rows) - len(evidence_only) - len(mixed),
        "missing_field_counts": sorted(missing.items(), key=lambda item: (-item[1], item[0])),
        "verified_evaluable_capacity_usd": evaluable_capacity,
        "evaluable_capacity_missing": capacity_missing,
    }


def _termmax_ynrwax(loader: EvidenceLoader, markets: list[Any], as_of: datetime) -> dict[str, Any]:
    matches = [market for market in markets
               if market.venue == "TermMax" and market.instrument == "USDC/ynRWAx@16OCT2026"]
    if len(matches) != 1:
        return {"status": f"COULD NOT VERIFY: expected one TermMax ynRWAx market, found {len(matches)}"}
    market = matches[0]
    collateral = str(market.metadata.get("collateral_address") or "")
    bundle = loader.load(
        chain_id=1,
        chain="Ethereum",
        underlying_symbol="ynRWAx",
        underlying_address=collateral,
        as_of=as_of,
    )
    oracle_status: str
    try:
        termmax = TermMaxClient(loader.http)
        gt_address = str(market.metadata.get("gt_address") or "").lower()
        gt_matches = [row for row in termmax.gt_configs(1)
                      if str(row.get("contractAddress") or "").lower() == gt_address]
        if len(gt_matches) != 1:
            raise RuntimeError(f"expected one gtConfig for {gt_address}, found {len(gt_matches)}")
        oracle_address = str(gt_matches[0].get("oracleAddress") or "")
        global_config = termmax.global_config(1)
        global_addresses = {
            str(global_config.get("oracleAggregator") or "").lower(),
            str(global_config.get("oracleAggregatorV2") or "").lower(),
        }
        if oracle_address.lower() not in global_addresses:
            raise RuntimeError(
                f"gtConfig oracle {oracle_address} is not either declared global oracle aggregator"
            )
        round_data = loader.oracle.probe_latest_round_data(
            oracle_address,
            cache_name=f"termmax_oracle_latest_round_{oracle_address.lower()}",
        )
        oracle_status = (
            f"COULD NOT VERIFY: TermMax oracle {oracle_address} returned updatedAt "
            f"{round_data.updated_at.isoformat()} but exposes no verified heartbeat"
        )
    except Exception as exc:  # noqa: BLE001
        oracle_status = f"COULD NOT VERIFY: TermMax venue oracle latestRoundData unreadable: {exc}"

    decision = score_credit(CreditInputs(
        instrument=market.instrument,
        underlying="ynRWAx",
        maturity=market.maturity,
        implied_apy=TimedValue(market.source_timestamp, float(market.implied_apy), market.source_endpoint),
        peg_prices=bundle.peg_prices,
        protocol_tvl_usd=bundle.protocol_tvl_usd,
        oracle_updated_at=None,
        oracle_heartbeat_seconds=None,
        market_liquidity_usd=TimedValue(
            market.source_timestamp,
            float(market.executable_capacity_usd),
            "TermMax lendCapacityUsdValue from the same best-rate order",
        ),
        holder_top10_share=bundle.holder_top10_share,
        peg_mechanism=bundle.peg_mechanism,
        oracle_kind=bundle.oracle_kind,
        oracle_behavior=bundle.oracle_behavior,
        oracle_reason=bundle.oracle_reason,
        price_disagreement_bp=bundle.price_disagreement_bp,
    ), as_of=as_of)
    return {
        "status": "BID" if decision.bid else "NO BID",
        "score": decision.score,
        "rejection_basis": decision.rejection_basis,
        "credit_blockers": decision.credit_blockers,
        "evidence_blockers": decision.evidence_blockers,
        "missing_fields": decision.missing_fields,
        "capacity_usd": market.executable_capacity_usd,
        "protocol_slug": bundle.protocol_slug,
        "issuer_name": bundle.issuer_name,
        "peg_mechanism": bundle.peg_mechanism,
        "peg_type": bundle.peg_type,
        "tvl_points": len(bundle.protocol_tvl_usd),
        "peg_points": len(bundle.peg_prices),
        "holder_share": None if bundle.holder_top10_share is None else bundle.holder_top10_share.value,
        "oracle": oracle_status,
        "underlying_oracle_kind": bundle.oracle_kind,
        "underlying_oracle_behavior": bundle.oracle_behavior,
        "underlying_oracle_reason": bundle.oracle_reason,
        "reason": decision.reason,
    }


def _chainlink_usdc_control(loader: EvidenceLoader, as_of: datetime) -> dict[str, Any]:
    definition = DOCUMENTED_FEEDS[(1, "USDC")]
    try:
        evidence = loader.oracle.latest_round_data(
            definition.address,
            heartbeat_seconds=definition.heartbeat_seconds,
            heartbeat_source=definition.heartbeat_source,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": f"COULD NOT VERIFY: {exc}"}
    age_hours = (as_of - evidence.updated_at).total_seconds() / 3600.0
    ratio = age_hours / (evidence.heartbeat_seconds / 3600.0)
    points = 10 if ratio < 0.25 else 7 if ratio <= 1.0 else 0
    return {
        "status": "fresh" if ratio <= 1.0 else "stale",
        "feed_address": evidence.feed_address,
        "updated_at": evidence.updated_at.isoformat(),
        "age_hours": age_hours,
        "heartbeat_hours": evidence.heartbeat_seconds / 3600.0,
        "heartbeat_source": evidence.heartbeat_source,
        "age_heartbeat_ratio": ratio,
        "rubric_points": points,
    }
def build_report(*, offline: bool) -> dict[str, Any]:
    initial_as_of = max(_evidence_as_of(), datetime.now(UTC))
    context = _fetch_context(EngineHttpClient(offline=True))
    tiers, _within, _cross = quality_and_dispersion(context, initial_as_of)
    loader = EvidenceLoader.for_research_cache(
        offline=offline,
        yields_pools=context.yields,
        protocols=context.protocols,
    )
    universe_markets, _universe_errors = ingest_all(offline=True)
    universe = build_universe(universe_markets, context.yields)
    try:
        universe = refine_unknown_denominations(
            universe, loader.llama.stablecoins(), loader.llama.stablecoin_detail,
        )
        universe = refine_with_onchain_asset(universe, loader.vault, CHAIN_NAMESPACES)
    except Exception:  # noqa: BLE001 - a third source, never a hard dependency
        pass
    instruments_by_underlying: dict[tuple[int, str, str], list[str]] = {}
    instruments_by_evidence: dict[tuple[int, str, str, str], list[str]] = {}
    for row in universe:
        key = row.evidence_key
        if key is None:
            continue
        try:
            loader.load(
                chain_id=row.chain_id, chain=row.chain,
                underlying_symbol=row.underlying_symbol or "",
                underlying_address=row.underlying_address or "",
                as_of=initial_as_of, numeraire=row.denomination,
                numeraire_coin=row.numeraire_coin,
                venue_oracle_context=row.oracle_context,
            )
        except Exception:  # noqa: BLE001 - a source failure is this row's gap, not the run's
            continue
        instruments_by_underlying.setdefault(key, []).append(row.name)
        instruments_by_evidence.setdefault((*key, row.identity), []).append(row.name)

    as_of = datetime.now(UTC)
    before_rows, before_decisions, before_unquoted = _current_credit_rows(as_of, evidence_loader=None)
    after_rows, after_decisions, after_unquoted = _current_credit_rows(as_of, evidence_loader=loader)
    markets, ingest_errors = ingest_all(offline=True)
    before = _summary(before_rows, before_decisions, markets, before_unquoted)
    after = _summary(after_rows, after_decisions, markets, after_unquoted)
    before_unassessed = {name for name, _tier, decision in before_rows if decision.evidence_blockers}
    after_unassessed = {name for name, _tier, decision in after_rows if decision.evidence_blockers}
    unresolved = []
    for key, bundle in sorted(loader._cache.items()):
        instruments = instruments_by_evidence.get(key, instruments_by_underlying.get(key[:3], []))
        for field, reason in bundle.unresolved:
            unresolved.append({
                "chain_id": key[0],
                "underlying_address": key[1],
                "instruments": instruments,
                "field": field,
                "reason": reason,
            })
    unresolved_by_field: dict[str, set[str]] = {}
    for row in unresolved:
        unresolved_by_field.setdefault(str(row["field"]), set()).update(row["instruments"])
    oracle_classes: Counter[str] = Counter()
    evidence_trace = []
    for key, bundle in sorted(loader._cache.items()):
        instruments = instruments_by_evidence.get(key, instruments_by_underlying.get(key[:3], []))
        oracle_class = (
            f"{bundle.oracle_kind}:{bundle.oracle_behavior}"
            if bundle.oracle_kind and bundle.oracle_behavior else "unclassified_or_no_base_feed"
        )
        oracle_classes[oracle_class] += len(instruments)
        evidence_trace.append({
            "chain_id": key[0],
            "underlying_address": key[1],
            "instruments": instruments,
            "issuer_name": bundle.issuer_name,
            "stablecoin_id": bundle.protocol_slug,
            "peg_mechanism": bundle.peg_mechanism,
            "peg_type": bundle.peg_type,
            "oracle_kind": bundle.oracle_kind,
            "oracle_behavior": bundle.oracle_behavior,
            "oracle_description": bundle.oracle_description,
            "oracle_reason": bundle.oracle_reason,
            "price_disagreement_bp": bundle.price_disagreement_bp,
        })
    return {
        "generated_at": as_of.isoformat(),
        "mode": "offline" if offline else "live",
        "before": before,
        "after": after,
        "change": {
            "cannot_assess_to_assessed": len(before_unassessed - after_unassessed),
            "cannot_assess_to_assessed_instruments": sorted(before_unassessed - after_unassessed),
            "assessed_to_cannot_assess": len(after_unassessed - before_unassessed),
            "assessed_to_cannot_assess_instruments": sorted(after_unassessed - before_unassessed),
            "net_assessed": after["assessed"] - before["assessed"],
            "evidence_rejections": after["evidence_rejections"] - before["evidence_rejections"],
            "credit_rejections": after["credit_rejections"] - before["credit_rejections"],
            "verified_evaluable_capacity_usd": (
                after["verified_evaluable_capacity_usd"] - before["verified_evaluable_capacity_usd"]
            ),
        },
        "unresolved": unresolved,
        "unresolved_instruments_by_field": {
            field: sorted(instruments) for field, instruments in sorted(unresolved_by_field.items())
        },
        "unresolved_instrument_counts": {
            field: len(instruments) for field, instruments in sorted(unresolved_by_field.items())
        },
        "oracle_class_split_instruments": dict(sorted(oracle_classes.items())),
        "evidence_trace": evidence_trace,
        "termmax_ynrwax": _termmax_ynrwax(loader, markets, as_of),
        "chainlink_usdc_control": _chainlink_usdc_control(loader, as_of),
        "ingest_errors": ingest_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Par's evidence-gap closure")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(offline=args.offline)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        after = report["after"]
        print(f"written: {args.output}")
        print(
            f"universe={after['universe']} executable_rate={after['total']} "
            f"assessed={after['assessed']} bid={after['bid']} rejected={after['rejected']}"
        )
        print(
            "missing=" + ", ".join(
                f"{field}:{count}" for field, count in after["missing_field_counts"]
            )
        )
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
