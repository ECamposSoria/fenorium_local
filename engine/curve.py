from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .demo import _current_credit_rows, _offline_evidence_loader
from .evidence_inputs import CHAIN_NAMESPACES, DOCUMENTED_FEEDS, RESEARCH_DATA
from .ingest import EngineHttpClient, ingest_all
from .midnight import MIDNIGHT_VENUES
from .universe import (build_universe, refine_unknown_denominations,
                       refine_with_onchain_asset, record_identity)
from research.par_sources.http import HttpClient
from research.par_sources.oracle import OracleClient


ENGINE_DATA = Path(__file__).resolve().parent / "data"
INPUT_FRESHNESS_MAX_AGE_SECONDS = 36 * 60 * 60

# Buckets match research/validate.py so a point on this curve and a dispersion figure in the
# research notes refer to the same tenor.
BUCKETS = (("<=30d", 30.0), ("30-90d", 90.0), ("90-180d", 180.0), (">180d", None))


def bucket_for(days: float) -> str | None:
    if days < 0:
        return None
    for name, limit in BUCKETS:
        if limit is None or days <= limit:
            return name
    return None


@dataclass(frozen=True)
class CurvePoint:
    identity: str
    venue: str
    chain: str
    instrument: str
    underlying: str | None
    denomination: str
    maturity: str
    maturity_timestamp: int
    days_to_maturity: float
    bucket: str
    executable_apy: float | None
    indicative_apy: float | None
    tier: str | None
    status: str
    rejection_basis: str | None
    score: float | None
    reason: str | None
    rate_kind: str
    liquidity_usd: float | None
    source_endpoint: str
    denomination_source: str
    # A position has no token of its own. What a buyer deposits, and the contract it is deposited
    # into, are carried from the record that produced the rate, so a purchase goes to the market
    # that was priced rather than to one inferred from a symbol.
    underlying_address: str | None = None
    market_address: str | None = None
    accounting_asset_address: str | None = None
    market_id: str | None = None
    loan_decimals: int | None = None
    loan_token: dict[str, Any] | None = None
    collaterals: list[dict[str, Any]] | None = None
    listed: bool | None = None
    fenorium_score: int | None = None
    score_cap: str | None = None


def _indicative(record_metadata: dict[str, Any]) -> float | None:
    """A resting lender ask is where the market says it would lend, not where we can lend.

    Term Finance's book is uncrossed on 47 of 53 live repos: lenders queued with no borrower on
    the other side. Dropping those points would understate the market; pricing them as executable
    would overstate what the book can actually fill, so they ride alongside as indicative.
    """
    value = record_metadata.get("lender_ask_rate")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _oldest_consumed_source(markets: list[Any]) -> datetime:
    """The staleness bound for a curve built from several independently cached venues.

    Pendle, TermMax, Tenor, Term Finance and Exactly each cache on their own schedule; one venue
    refreshing on time never proves the others did. Taking the newest of their timestamps would
    report the curve as fresh whenever a single venue happened to refetch, even while the rest of
    the book was built from a stale cache. The bound that actually protects the fail-closed
    threshold is the *oldest* consumed timestamp: the one venue still driving the age check no
    matter how current the others are.
    """
    return min(row.source_timestamp.astimezone(UTC) for row in markets)


def build_curve(as_of: datetime | None = None) -> dict[str, Any]:
    moment = as_of or datetime.now(UTC)
    context_http = EngineHttpClient(offline=True)
    from research.validate import _fetch_context

    context = _fetch_context(context_http)
    markets, ingest_errors = ingest_all(offline=True, as_of=moment)
    if not markets:
        raise RuntimeError("curve integrity failure: no consumed market cache timestamps")
    oldest_input = _oldest_consumed_source(markets)
    input_age_seconds = max(0, int((moment - oldest_input).total_seconds()))
    loader = _offline_evidence_loader()
    universe = build_universe(markets, context.yields)
    try:
        universe = refine_unknown_denominations(
            universe, loader.llama.stablecoins(), loader.llama.stablecoin_detail,
        )
        universe = refine_with_onchain_asset(universe, loader.vault, CHAIN_NAMESPACES)
    except Exception:  # noqa: BLE001 - a third source, never a hard dependency
        pass

    _rows, decisions, _unquoted = _current_credit_rows(moment, evidence_loader=loader)
    indicative = {}
    accounting_assets = {}
    market_ids = {}
    loan_decimals = {}
    record_metadata: dict[str, dict[str, Any]] = {}
    contracts: dict[str, tuple[str | None, str | None]] = {}
    for record in markets:
        rate = _indicative(record.metadata)
        identity = record_identity(record)
        if identity is not None:
            record_metadata[identity] = record.metadata
            accounting_assets[identity] = record.metadata.get("accounting_asset_address")
            if record.venue in MIDNIGHT_VENUES:
                market_ids[identity] = (record.metadata.get("market_id")
                                        or record.metadata.get("fixed_market_identifier"))
                loan_decimals[identity] = record.metadata.get("loan_decimals")
        if rate is not None and identity is not None:
            indicative[identity] = rate
        if identity is not None and (record.venue == "Exactly" or record.venue in MIDNIGHT_VENUES):
            contracts[identity] = (
                record.metadata.get("underlying_address"),
                (record.metadata.get("midnight_address") if record.venue in MIDNIGHT_VENUES
                 else record.metadata.get("oracle_market_address")),
            )

    points: list[CurvePoint] = []
    for row in universe:
        if row.maturity is None:
            continue
        days = (row.maturity - moment).total_seconds() / 86400.0
        bucket = bucket_for(days)
        if bucket is None:
            continue
        decision = decisions.get(row.identity)
        tier = None if decision is None else decision.tier
        if decision is None:
            if row.implied_apy is not None:
                raise RuntimeError(
                    f"curve integrity failure: quoted identity {row.identity} has no credit decision"
                )
            status = "NO QUOTE"
            basis = "market_data"
            reason = (
                "NO QUOTE — no executable borrower bid; the visible lender ask is indicative only"
                if indicative.get(row.identity) is not None
                else "NO QUOTE — the venue publishes neither an executable nor an indicative rate"
            )
        elif decision.bid:
            status = "BID"
            basis = None
            reason = decision.reason
        else:
            status = "NO BID"
            basis = decision.rejection_basis
            reason = decision.reason
        indicative_apy = indicative.get(row.identity)
        metadata = record_metadata.get(row.identity, {})
        rate_kind = (metadata.get("rate_kind") if row.venue in MIDNIGHT_VENUES
                     and metadata.get("rate_kind") in {"indicative", "executable", "none"} else
                     "executable" if row.implied_apy is not None else (
            "indicative" if indicative_apy is not None else "none"
        ))
        executable_apy = row.implied_apy
        if row.venue in MIDNIGHT_VENUES and rate_kind == "indicative":
            if row.implied_apy is not None:
                indicative_apy = row.implied_apy
            executable_apy = None
        points.append(CurvePoint(
            identity=row.identity, venue=row.venue, chain=row.chain, instrument=row.symbol,
            underlying=row.underlying_symbol, denomination=row.denomination,
            maturity=row.maturity.date().isoformat(), maturity_timestamp=int(row.maturity.timestamp()),
            days_to_maturity=round(days, 2),
            bucket=bucket,
            executable_apy=executable_apy,
            indicative_apy=indicative_apy,
            tier=None if tier is None else tier.value,
            status=status, rejection_basis=basis,
            score=None if decision is None else decision.score,
            fenorium_score=None if decision is None else decision.fenorium_score,
            score_cap=None if decision is None else decision.score_cap,
            reason=reason, rate_kind=rate_kind, liquidity_usd=row.liquidity_usd,
            source_endpoint=row.source_endpoint,
            denomination_source=row.denomination_source,
            underlying_address=contracts.get(row.identity, (None, None))[0],
            market_address=contracts.get(row.identity, (None, None))[1],
            accounting_asset_address=accounting_assets.get(row.identity),
            market_id=market_ids.get(row.identity),
            loan_decimals=loan_decimals.get(row.identity),
            loan_token=metadata.get("loan_token"),
            collaterals=metadata.get("collaterals"),
            listed=metadata.get("listed"),
        ))

    identities = [point.identity for point in points]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        raise RuntimeError(
            "curve integrity failure: duplicate contract identities: " + ", ".join(duplicates)
        )

    grouped: dict[tuple[str, str, str], list[CurvePoint]] = {}
    for point in points:
        if point.executable_apy is None or point.tier is None:
            continue
        grouped.setdefault((point.denomination, point.tier, point.bucket), []).append(point)
    segments = []
    for (denomination, tier, bucket), group in sorted(grouped.items()):
        rates = sorted(point.executable_apy for point in group)
        segments.append({
            "denomination": denomination, "tier": tier, "bucket": bucket,
            "instruments": len(group),
            "median_apy": statistics.median(rates),
            "low_apy": rates[0], "high_apy": rates[-1],
            "dispersion_bp": (rates[-1] - rates[0]) * 10_000,
            "venues": sorted({point.venue for point in group}),
            "bids": sum(point.status == "BID" for point in group),
        })

    executable_count = sum(point.rate_kind == "executable" for point in points)
    indicative_count = sum(point.rate_kind == "indicative" for point in points)
    no_rate_count = sum(point.rate_kind == "none" for point in points)
    if executable_count + indicative_count + no_rate_count != len(points):
        raise RuntimeError("curve integrity failure: rate coverage does not partition the universe")
    status_counts = {
        status: sum(point.status == status for point in points)
        for status in ("BID", "NO BID", "NO QUOTE")
    }
    return {
        "generated_at": moment.isoformat(),
        "input_freshness": {
            "method": "OLDEST_CONSUMED_MARKET_SOURCE_TIMESTAMP",
            "oldest_cache_entry_at": oldest_input.isoformat(),
            "age_seconds": input_age_seconds,
            "maximum_age_seconds": INPUT_FRESHNESS_MAX_AGE_SECONDS,
        },
        "peg_corroboration": (
            "DefiLlama 90-day daily closes, at least 60 points; CoinGecko consulted where listed"
        ),
        "universe": len(points),
        "represented_instruments": len(points),
        "omitted_instruments": 0,
        "venues": sorted({point.venue for point in points}),
        "with_executable_rate": executable_count,
        "with_indicative_rate_only": indicative_count,
        "no_rate_at_all": no_rate_count,
        "bids": status_counts["BID"],
        "status_counts": status_counts,
        "unquoted_instruments": indicative_count + no_rate_count,
        "unquoted_instrument_identities": sorted(
            point.identity for point in points if point.rate_kind != "executable"
        ),
        "ingest_errors": ingest_errors,
        "segments": segments,
        "points": [asdict(point) for point in points],
    }


def render(curve: dict[str, Any]) -> str:
    lines = [
        "PAR CREDIT CURVE",
        f"as_of={curve['generated_at']} instruments={curve['universe']} "
        f"venues={', '.join(curve['venues'])}",
        f"executable={curve['with_executable_rate']} "
        f"indicative_only={curve['with_indicative_rate_only']} "
        f"no_rate={curve['no_rate_at_all']} bids={curve['bids']}",
        "",
        f"{'denomination':13s} {'tier':8s} {'bucket':9s} {'n':>3} {'median':>8} {'low':>8} "
        f"{'high':>8} {'disp bp':>8} {'bids':>5}  venues",
    ]
    for segment in curve["segments"]:
        lines.append(
            f"{segment['denomination']:13s} {segment['tier']:8s} {segment['bucket']:9s} "
            f"{segment['instruments']:>3} {segment['median_apy'] * 100:>7.2f}% "
            f"{segment['low_apy'] * 100:>7.2f}% {segment['high_apy'] * 100:>7.2f}% "
            f"{segment['dispersion_bp']:>8.0f} {segment['bids']:>5}  {', '.join(segment['venues'])}"
        )
    for error in curve["ingest_errors"]:
        lines.append(f"UNAVAILABLE — {error}")
    return "\n".join(lines)


def refresh_documented_oracles(http: HttpClient | None = None) -> list[str]:
    """Read each documented feed live, into the cache entry the offline build then reads.

    The curve judges a feed's age against the moment it is built, but builds from the snapshot's
    caches: an afternoon build measured a feed read at 02:25 against 13:49 and refused 63 markets
    for a USDC/USD feed that had updated at 04:43 (2026-10-19). A failed read keeps the cached one,
    whose verdict errs on the stale side.
    """
    http = http or HttpClient(RESEARCH_DATA, delay_seconds=0, retries=2)
    notes = []
    for (_chain_id, symbol), feed in sorted(DOCUMENTED_FEEDS.items()):
        try:
            read = OracleClient(http).probe_latest_round_data(feed.address)
        except Exception as exc:  # noqa: BLE001 - the cached read still stands
            notes.append(f"{symbol} feed {feed.address}: live read failed, the cached read stands ({exc})")
            continue
        # The client answers a failed request from its cache; say which one this was.
        record = http.records[-1] if http.records else None
        source = ("the cached read stands" + (f" ({record.fallback_reason})" if record.fallback_reason else "")
                  if record is None or record.from_cache else "read live")
        updated = f"updated {read.updated_at:%Y-%m-%dT%H:%M}Z" if read.updated_at else "no update time"
        notes.append(f"{symbol} feed {feed.address}: {source}, {updated}")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Par's cross-venue fixed-income credit curve")
    parser.add_argument("--output", type=Path, default=ENGINE_DATA / "curve.json")
    parser.add_argument("--refresh-oracles", action="store_true",
                        help="read documented oracle feeds live before the offline build")
    args = parser.parse_args()
    if args.refresh_oracles:
        for note in refresh_documented_oracles():
            print(f"oracle: {note}")
    curve = build_curve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(curve, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render(curve))
    print(f"\nwritten: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
