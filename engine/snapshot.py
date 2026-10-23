from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from research.par_sources.defillama import DefiLlamaClient

from .evidence_inputs import CHAIN_NAMESPACES, EvidenceLoader
from .ingest import EngineHttpClient, ingest_all
from .midnight import MIDNIGHT_VENUES
from .universe import build_universe, refine_unknown_denominations, refine_with_onchain_asset


ENGINE_DATA = Path(__file__).resolve().parent / "data"

# The append-only history is the one thing here that cannot be rebuilt, so it is kept out of the
# cache tree and given its own location. In the container that is a separate volume: mixing it
# with `engine/data`, which holds disposable HTTP archives, makes "what do I back up" ambiguous
# at exactly the moment it matters.
SNAPSHOT_DIR = Path(os.environ.get("PAR_SNAPSHOT_DIR") or (ENGINE_DATA / "snapshots"))

# The trend the rubric asks for is 30 days. A window slightly wider than that lets a missed run
# or a weekend gap still produce a usable comparison instead of a hole.
TREND_WINDOW_DAYS = 30
TREND_TOLERANCE_DAYS = 5
MIN_TREND_SPAN_DAYS = 25


@dataclass(frozen=True)
class SnapshotRow:
    observed_on: str
    observed_at: str
    chain_id: int
    address: str
    denomination: str
    symbol: str | None
    issuer_name: str | None
    protocol_tvl_usd: float | None
    redemption_ratio: float | None
    market_ratio: float | None
    peg_last: float | None
    source: str

    def key(self) -> tuple[int, str]:
        return (self.chain_id, self.address)


def _month_file(day: date) -> Path:
    return SNAPSHOT_DIR / f"evidence_{day.strftime('%Y-%m')}.jsonl"


def read_history(chain_id: int, address: str) -> list[tuple[datetime, float]]:
    """Our own observations of one issuer's size, oldest first.

    This exists because historical on-chain state is not readable on free infrastructure:
    measured 2026-08-19, `eth_call` at the latest block answers while the same call 216,000
    blocks back returns HTTP 403 on publicnode, and the other public endpoints are gated or
    down. A protocol TVL trend therefore cannot be reconstructed backwards — it can only be
    accumulated forwards, one day at a time, starting whenever we start.
    """
    target = address.lower()
    points: dict[str, float] = {}
    stamps: dict[str, str] = {}
    if not SNAPSHOT_DIR.exists():
        return []
    for path in sorted(SNAPSHOT_DIR.glob("evidence_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("chain_id") != chain_id or str(row.get("address", "")).lower() != target:
                continue
            value = row.get("protocol_tvl_usd")
            day = row.get("observed_on")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                continue
            if not isinstance(day, str):
                continue
            # One row per day wins: a re-run replaces the earlier reading rather than doubling it.
            points[day] = float(value)
            stamps[day] = str(row.get("observed_at") or f"{day}T00:00:00+00:00")
    out = []
    for day in sorted(points):
        try:
            out.append((datetime.fromisoformat(stamps[day]), points[day]))
        except ValueError:
            continue
    return out


def trend_pair(history: list[tuple[datetime, float]]) -> tuple[tuple[datetime, float], tuple[datetime, float]] | None:
    """The (t-30d, now) pair the rubric compares, or None while the history is too short."""
    if len(history) < 2:
        return None
    latest = history[-1]
    target = latest[0].timestamp() - TREND_WINDOW_DAYS * 86_400
    prior = min(history[:-1], key=lambda point: abs(point[0].timestamp() - target))
    if abs(prior[0].timestamp() - target) > TREND_TOLERANCE_DAYS * 86_400:
        return None
    if (latest[0] - prior[0]).total_seconds() < MIN_TREND_SPAN_DAYS * 86_400:
        return None
    return prior, latest


def capture(*, offline: bool = False, as_of: datetime | None = None) -> list[SnapshotRow]:
    moment = as_of or datetime.now(UTC)
    # Deliberately does NOT import research.validate. That module pulls the backtest stack —
    # pandas and matplotlib — which a daily collector has no use for, and the collector runs in a
    # 768M container beside a live trading engine. The two lists it needs come straight from the
    # DefiLlama client that everything else already uses.
    # Honours `offline` rather than pinning it. The report deliberately reads its universe from
    # cache while fetching evidence live, and copying that here meant a first run on a machine
    # with no cache could never populate one.
    catalog = DefiLlamaClient(EngineHttpClient(offline=offline))
    yields_pools = catalog.yields_pools()
    protocols = catalog.protocols()
    markets, _errors = ingest_all(offline=offline, as_of=moment)
    loader = EvidenceLoader.for_research_cache(
        offline=offline, yields_pools=yields_pools, protocols=protocols,
    )
    universe = build_universe(markets, yields_pools)
    try:
        universe = refine_unknown_denominations(
            universe, loader.llama.stablecoins(), loader.llama.stablecoin_detail,
        )
        universe = refine_with_onchain_asset(universe, loader.vault, CHAIN_NAMESPACES)
    except Exception:  # noqa: BLE001 - refinement is a third source, never a hard dependency
        pass

    seen: set[tuple[int, str, str]] = set()
    rows: list[SnapshotRow] = []
    for row in universe:
        key = row.evidence_key
        if key is None or key in seen:
            continue
        seen.add(key)
        try:
            bundle = loader.load(
                chain_id=row.chain_id, chain=row.chain,
                underlying_symbol=row.underlying_symbol or "",
                underlying_address=row.underlying_address or "",
                as_of=moment, numeraire=row.denomination,
                numeraire_coin=row.numeraire_coin,
                venue_oracle_context=row.oracle_context,
            )
        except Exception:  # noqa: BLE001 - a source failure is this row's gap, not the run's
            continue
        tvl = bundle.protocol_tvl_usd[-1].value if bundle.protocol_tvl_usd else None
        rows.append(SnapshotRow(
            observed_on=moment.date().isoformat(),
            observed_at=moment.isoformat(),
            chain_id=row.chain_id,
            address=(row.underlying_address or "").lower(),
            denomination=row.denomination,
            symbol=row.underlying_symbol,
            issuer_name=bundle.issuer_name,
            protocol_tvl_usd=tvl,
            redemption_ratio=bundle.redemption_ratio,
            market_ratio=bundle.market_ratio,
            peg_last=bundle.peg_prices[-1].value if bundle.peg_prices else None,
            source=bundle.protocol_tvl_usd[-1].source if bundle.protocol_tvl_usd else "COULD NOT VERIFY",
        ))
    collateral_tokens = {
        (row.chain_id, str(collateral.get("token") or "").lower())
        for row in universe if row.venue in MIDNIGHT_VENUES
        for collateral in (row.collaterals or ())
        if collateral.get("token")
    }
    for chain_id, address in sorted(collateral_tokens):
        try:
            loader.collateral_usd_prices(chain_id, address, moment)
        except Exception:  # noqa: BLE001 - each token's missing cache is independent
            continue
    warmed_checks: set[tuple[int, str, str, str, str]] = set()
    check_collateral = getattr(loader, "collateral_checks", None)
    if callable(check_collateral):
        for row in universe:
            if row.venue not in MIDNIGHT_VENUES:
                continue
            loan = str(getattr(row, "underlying_address", None) or "")
            if not loan:
                continue
            units = getattr(row, "total_units", None)
            for collateral in row.collaterals or ():
                token = str(collateral.get("token") or "")
                oracle = str(collateral.get("oracle") or "")
                if not token or token.lower() == loan.lower():
                    continue
                key = (row.chain_id, token.lower(), oracle.lower(), loan.lower(), str(units))
                if key in warmed_checks:
                    continue
                warmed_checks.add(key)
                try:
                    # Warming writes observations made during the run, not evidence for an
                    # earlier verdict. Explicit historical/offline captures retain their cutoff.
                    check_time = moment if offline or as_of is not None else datetime.now(UTC)
                    check_collateral(row.chain_id, token, oracle, loan, units, check_time)
                except Exception:  # noqa: BLE001 - one oracle or quote cannot stop the nightly run
                    continue
    return rows


def append(rows: Iterable[SnapshotRow], *, day: date | None = None) -> Path:
    """Append today's readings, replacing any earlier reading taken the same day."""
    rows = list(rows)
    when = day or (date.fromisoformat(rows[0].observed_on) if rows else datetime.now(UTC).date())
    path = _month_file(when)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = when.isoformat()
    kept: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if existing.get("observed_on") == stamp:
                continue
            kept.append(json.dumps(existing, sort_keys=True))
    for row in rows:
        kept.append(json.dumps(row.__dict__, sort_keys=True))
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append one day of issuer-size readings, so a 30-day trend can exist later",
    )
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    rows = capture(offline=args.offline)
    path = append(rows)
    priced = sum(row.protocol_tvl_usd is not None for row in rows)
    ratios = sum(row.redemption_ratio is not None for row in rows)
    ready = 0
    for row in rows:
        if trend_pair(read_history(row.chain_id, row.address)) is not None:
            ready += 1
    print(f"snapshot {rows[0].observed_on if rows else 'n/a'}: {len(rows)} underlyings, "
          f"{priced} with a size, {ratios} with a redemption ratio")
    print(f"underlyings whose own history now spans a 30-day trend: {ready}")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
