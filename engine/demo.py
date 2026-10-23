from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
import sys

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "research"
if str(RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(RESEARCH_DIR))

from research.par_sources.normalize import QualityTier
from research.par_sources.http import cache_fetched_at
from research.validate import _fetch_context, quality_and_dispersion

from .universe import (build_universe, refine_unknown_denominations,
                        refine_with_onchain_asset, record_identity)

from .allocate import (AllocationConfig, Candidate, ReserveSelection, ReserveVenue,
                       allocate, select_reserve_venue)
from .credit import (CollateralEvidence, CreditInputs, TimedValue, false_positive_report,
                     score_credit, xusd_pre_event_backtest)
from .evidence_inputs import CHAIN_NAMESPACES, EvidenceLoader
from .ingest import EngineHttpClient, ingest_all
from .midnight import MIDNIGHT_VENUES
from .gas import DEFAULT_OPS_PER_YEAR, GasMarket
from .models import Job
from .pricing import render_quote_table, validate_cached_convert_quotes
from .risk import CvarEstimate, conservative_cvar_fallback


def _evidence_as_of() -> datetime:
    cache = Path(__file__).resolve().parents[1] / "research" / "data" / "latest"
    names = (
        "pendle_markets_active_*.json", "defillama_yields_pools.json",
        "defillama_protocols.json", "defillama_stablecoins.json",
    )
    paths = [path for pattern in names for path in cache.glob(pattern)]
    if not paths:
        raise RuntimeError("research cache has no timestamped market evidence")
    return max(cache_fetched_at(path) for path in paths)


def _peer_group(row) -> str:
    return "midnight" if row.venue in MIDNIGHT_VENUES else "market"


def _current_credit_rows(as_of: datetime, *, evidence_loader: EvidenceLoader | None):
    """Score every fixed-maturity instrument we ingest, from every venue.

    `evidence_loader` is keyword-only and REQUIRED — pass None explicitly to opt into the
    degraded path. It used to default to None, which meant a caller who simply forgot it
    silently got worse evidence and a different verdict. That is not a defaulting decision,
    it is a correctness one.

    The universe used to be Pendle's stablecoin markets alone, which made "cross-venue credit
    curve" a claim rather than a fact: Spectra, TermMax and Tenor were ingested, displayed as
    rates, and never underwritten.
    """
    context = _fetch_context(EngineHttpClient(offline=True))
    markets, _ingest_errors = ingest_all(offline=True, as_of=as_of)
    rows = build_universe(markets, context.yields)
    if evidence_loader is not None:
        try:
            rows = refine_unknown_denominations(
                rows, evidence_loader.llama.stablecoins(), evidence_loader.llama.stablecoin_detail,
            )
            rows = refine_with_onchain_asset(
                rows, evidence_loader.vault, CHAIN_NAMESPACES,
            )
        except Exception:  # noqa: BLE001 - the registry is a third source, never a hard dependency
            pass
    quoted = [row for row in rows if row.implied_apy is not None]
    unquoted = [row for row in rows if row.implied_apy is None]

    def score(row, peer_median: float | None):
        bundle = None
        if evidence_loader is not None and row.evidence_key is not None:
            bundle = evidence_loader.load(
                chain_id=row.chain_id, chain=row.chain,
                underlying_symbol=row.underlying_symbol or "",
                underlying_address=row.underlying_address or "",
                as_of=as_of, numeraire=row.denomination,
                numeraire_coin=row.numeraire_coin,
                venue_oracle_context=row.oracle_context,
            )
        collaterals = None
        if row.venue in MIDNIGHT_VENUES:
            if evidence_loader is None:
                collaterals = ()
            else:
                loan_address = str(row.underlying_address or "")
                collateral_evidence = []
                for collateral in row.collaterals or ():
                    token = str(collateral.get("token") or "")
                    prices = ()
                    if token and loan_address and token.lower() != loan_address.lower():
                        prices = evidence_loader.collateral_prices(
                            row.chain_id, token, loan_address, as_of,
                        ) or ()
                    checks = {}
                    check_collateral = getattr(evidence_loader, "collateral_checks", None)
                    same_as_loan = bool(token and loan_address and token.lower() == loan_address.lower())
                    if token and loan_address and not same_as_loan and callable(check_collateral):
                        try:
                            checks = check_collateral(
                                row.chain_id, token, str(collateral.get("oracle") or ""),
                                loan_address, getattr(row, "total_units", None), as_of,
                            ) or {}
                        except AssertionError:
                            raise
                        except Exception:  # noqa: BLE001 - a collateral check is independent evidence
                            checks = {}
                    checks.setdefault("loan_symbol", row.underlying_symbol or "")
                    collateral_evidence.append(CollateralEvidence(
                        symbol=str(collateral.get("symbol") or token or "unknown"),
                        token=token,
                        lltv_wad=collateral.get("lltv", ""),
                        same_as_loan=same_as_loan,
                        prices=tuple(prices),
                        **checks,
                    ))
                collaterals = tuple(collateral_evidence)
        return score_credit(CreditInputs(
            instrument=row.symbol, underlying=row.underlying_symbol or "unavailable",
            maturity=row.maturity,
            implied_apy=TimedValue(as_of, row.implied_apy, row.source_endpoint),
            peg_prices=() if bundle is None else bundle.peg_prices,
            protocol_tvl_usd=() if bundle is None else bundle.protocol_tvl_usd,
            oracle_updated_at=None if bundle is None else bundle.oracle_updated_at,
            oracle_heartbeat_seconds=None if bundle is None else bundle.oracle_heartbeat_seconds,
            market_liquidity_usd=None if row.liquidity_usd is None else
                TimedValue(as_of, row.liquidity_usd, row.source_endpoint),
            holder_top10_share=None if bundle is None else bundle.holder_top10_share,
            peg_mechanism=None if bundle is None else bundle.peg_mechanism,
            denomination=row.denomination,
            redemption_discount_bp=None if bundle is None else bundle.redemption_discount_bp,
            redemption_source=None if bundle is None else bundle.redemption_source,
            oracle_kind=None if bundle is None else bundle.oracle_kind,
            oracle_behavior=None if bundle is None else bundle.oracle_behavior,
            oracle_reason=None if bundle is None else bundle.oracle_reason,
            price_disagreement_bp=None if bundle is None else bundle.price_disagreement_bp,
            tier_median_apy=None if peer_median is None else TimedValue(
                as_of, peer_median,
                f"median implied APY across fetched {row.denomination}-denominated "
                f"{'Midnight lending ' if _peer_group(row) == 'midnight' else ''}markets "
                f"in the same quality tier",
            ),
            collaterals=collaterals,
        ), as_of=as_of)

    # Two passes. The peer median that powers the excess-yield blocker needs each row's tier, and
    # the tier comes out of scoring — so score once to learn the tiers, then re-score against the
    # medians. Medians are per (denomination, tier): ETH paper medians 2.23% against USD's 8.24%,
    # so one pooled median would read every ETH instrument as cheap and blunt the blocker on
    # every dollar one. Midnight lending markets are their own peer group for the same reason: a
    # loan against collateral is not a principal token, and on 2026-10-14 their 257 lender bids
    # lifted the USD TIER_A median from 4.19% to 6.17%, loosening every other venue's ceiling.
    provisional = {row.identity: score(row, None) for row in quoted}
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for row in quoted:
        grouped.setdefault(
            (row.denomination, provisional[row.identity].tier.value, _peer_group(row)), [],
        ).append(row.implied_apy)
    medians = {key: statistics.median(values) for key, values in grouped.items() if values}

    output = []
    decisions = {}
    for row in quoted:
        peer = medians.get((row.denomination, provisional[row.identity].tier.value, _peer_group(row)))
        decision = score(row, peer)
        output.append((row.name, decision.tier, decision))
        decisions[row.identity] = decision
    return output, decisions, unquoted


def _offline_evidence_loader() -> EvidenceLoader:
    context = _fetch_context(EngineHttpClient(offline=True))
    return EvidenceLoader.for_research_cache(
        offline=True,
        yields_pools=context.yields,
        protocols=context.protocols,
    )


def _aave_usdc_rates(chain: str = "Ethereum") -> tuple[float, float, float, float | None]:
    """Aave v3 USDC on `chain`, taken from the CORE market, not merely the largest pool.

    DefiLlama lists several aave-v3/Ethereum/USDC pools. Picking by TVL alone selected the
    "Umbrella" pool (4.749% = 3.289% base + 1.460% reward), whose extra 146bp pays for
    slashing risk — a credit spread, not a risk-free floor. The core market carries no
    `poolMeta`; prefer it, and only fall back to the largest pool if no core market is listed.
    """
    context = _fetch_context(EngineHttpClient(offline=True))
    matches = [row for row in context.yields if row.get("project") == "aave-v3"
               and row.get("chain") == chain and row.get("symbol") == "USDC"]
    if not matches:
        raise RuntimeError(f"DefiLlama cache has no aave-v3 / {chain} / USDC pool")
    core = [row for row in matches if not row.get("poolMeta")]
    row = max(core or matches, key=lambda value: float(value.get("tvlUsd") or -1))
    total = float(row["apy"]) / 100.0
    base = float(row["apyBase"]) / 100.0
    reward = float(row.get("apyReward") or 0.0) / 100.0
    tvl = None if row.get("tvlUsd") is None else float(row["tvlUsd"])
    return base, total, reward, tvl


def _aave_usdc_rate() -> float:
    return _aave_usdc_rates()[1]


def _aave_usdc_pulled_at() -> datetime:
    path = Path(__file__).resolve().parents[1] / "research" / "data" / "latest" / "defillama_yields_pools.json"
    if not path.exists():
        raise RuntimeError("DefiLlama cache has no timestamped yields pool evidence")
    return cache_fetched_at(path)


def _protocol_tvl(protocol: str) -> tuple[float | None, datetime | None]:
    context = _fetch_context(EngineHttpClient(offline=True))
    slugs = {"morpho": "morpho-blue", "aave": "aave-v3", "aave-v3": "aave-v3"}
    slug = slugs.get(protocol.lower(), protocol.lower())
    matches = [row for row in context.protocols if str(row.get("slug", "")).lower() == slug]
    values = [float(row["tvl"]) for row in matches if row.get("tvl") is not None]
    if not values:
        return None, None
    path = Path(__file__).resolve().parents[1] / "research" / "data" / "latest" / "defillama_protocols.json"
    pulled_at = cache_fetched_at(path) if path.exists() else None
    return max(values), pulled_at


def _reserve_candidates(markets: list, *, as_of: datetime) -> tuple[ReserveVenue, ...]:
    result: list[ReserveVenue] = []
    protocol_tvls: dict[str, tuple[float | None, datetime | None]] = {}
    for market in markets:
        if market.venue != "LI.FI Earn" or market.implied_apy is None:
            continue
        protocol = str(market.metadata.get("protocol") or "COULD NOT VERIFY")
        if protocol not in protocol_tvls:
            protocol_tvls[protocol] = _protocol_tvl(protocol)
        protocol_tvl, protocol_tvl_at = protocol_tvls[protocol]
        price = market.metadata.get("underlying_price_usd")
        peg = () if price is None else (
            TimedValue(market.source_timestamp, float(price), market.source_endpoint),
        )
        tvl = () if protocol_tvl is None or protocol_tvl_at is None else (
            TimedValue(protocol_tvl_at, protocol_tvl, f"DefiLlama {protocol} protocol TVL"),
        )
        liquidity = None if market.liquidity_usd is None else TimedValue(
            market.source_timestamp, market.liquidity_usd,
            "LI.FI Earn vault TVL; this is not executable exit depth",
        )
        instant_exit = bool(market.metadata.get("instant_exit"))
        redeem_steps = str(market.metadata.get("redeem_steps") or "COULD NOT VERIFY")
        credit_inputs = CreditInputs(
            instrument=market.instrument,
            underlying=market.underlying or "COULD NOT VERIFY",
            maturity=None,
            implied_apy=TimedValue(market.source_timestamp, market.implied_apy, market.source_endpoint),
            peg_prices=peg,
            protocol_tvl_usd=tvl,
            oracle_updated_at=None,
            market_liquidity_usd=liquidity,
            holder_top10_share=None,
            transparent_yield_source_apy=None if market.metadata.get("apy_base") is None else TimedValue(
                market.source_timestamp, float(market.metadata["apy_base"]), market.source_endpoint
            ),
            callable_exit=instant_exit,
        )
        result.append(ReserveVenue(
            candidate_id=f"{market.chain}:{protocol}:{market.instrument}",
            instrument=market.instrument,
            underlying=market.underlying or "COULD NOT VERIFY",
            protocol=protocol,
            chain=market.chain,
            rate_total=market.implied_apy,
            rate_base=market.metadata.get("apy_base"),
            rate_reward=market.metadata.get("apy_reward"),
            tvl_usd=market.liquidity_usd,
            redeem_steps=redeem_steps,
            instant_exit=instant_exit,
            source_timestamp=market.source_timestamp,
            source_endpoint=market.source_endpoint,
            credit_inputs=credit_inputs,
        ))
    return tuple(result)


def _aave_by_chain() -> dict[str, float]:
    """Aave USDC total per chain. The floor is chain-specific; Base is not Ethereum."""
    rates: dict[str, float] = {}
    for chain in ("Ethereum", "Base"):
        try:
            rates[chain] = _aave_usdc_rates(chain)[1]
        except RuntimeError:
            continue
    return rates


def _reserve_selection(markets: list, *, as_of: datetime) -> ReserveSelection:
    base, total, _reward, tvl = _aave_usdc_rates()
    return select_reserve_venue(
        _reserve_candidates(markets, as_of=as_of), as_of=as_of,
        aave_floor_rate=base, aave_total_rate=total, aave_tvl_usd=tvl,
        aave_source_timestamp=_aave_usdc_pulled_at(),
        aave_by_chain=_aave_by_chain(),
    )


def _round2_analysis() -> dict | None:
    path = Path(__file__).resolve().parents[1] / "research" / "data" / "latest" / "round2_analysis.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _gas_markets(payload: dict | None) -> dict[str, GasMarket]:
    rows = payload.get("gas_markets") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    result: dict[str, GasMarket] = {}
    for chain, row in rows.items():
        if not isinstance(row, dict):
            continue
        try:
            result[chain] = GasMarket(
                chain=chain, gas_price_wei=int(row["gas_price_wei"]), eth_usd=float(row["eth_usd"]),
                gas_pulled_at=datetime.fromisoformat(str(row["gas_pulled_at"])),
                eth_usd_pulled_at=datetime.fromisoformat(str(row["eth_usd_pulled_at"])),
                gas_source=str(row["gas_source"]), eth_usd_source=str(row["eth_usd_source"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _market_cvar(market, payload: dict | None) -> CvarEstimate:
    maturity = "callable" if market.maturity is None else market.maturity.date().isoformat()
    address = market.metadata.get("pt_address") or market.metadata.get("address") \
        or market.metadata.get("market_address") or market.instrument
    key = f"{market.venue}|{market.chain}|{address}|{maturity}"
    rows = payload.get("coverage") if isinstance(payload, dict) else None
    match = next((row for row in rows if isinstance(row, dict) and row.get("key") == key), None) \
        if isinstance(rows, list) else None
    if match is None:
        return conservative_cvar_fallback(
            source="research/data/latest/round2_analysis.json", observations=0,
            missing=f"daily_return_history for {key}",
        )
    return CvarEstimate(
        float(match["cvar95"]), str(match["cvar_kind"]), int(match["observations"]),
        str(match["source"]), str(match["cvar_detail"]),
    )


def main() -> int:
    markets, ingest_errors = ingest_all(offline=True)
    as_of = max((datetime.now(UTC), _evidence_as_of(), *(row.source_timestamp for row in markets)))
    print("PAR OFFLINE EVIDENCE REPORT")
    print(f"as_of={as_of.isoformat()} markets={len(markets)} ingest_errors={len(ingest_errors)}")
    print("venues=" + ", ".join(f"{venue}:{sum(row.venue == venue for row in markets)}"
                                for venue in sorted({row.venue for row in markets})))
    for error in ingest_errors:
        print(f"UNAVAILABLE — {error}")
    empty_lend = [row.instrument for row in markets if row.venue == "TermMax" and row.metadata.get("literal_empty_book")]
    print("literal_empty_lend_books=" + (", ".join(empty_lend) if empty_lend else "none in cache"))

    print("\nPENDLE MODEL VS CACHED CONVERT QUOTES")
    quote_rows = validate_cached_convert_quotes()
    print(render_quote_table(quote_rows))
    print(f"max_abs_residual_bp={max(abs(row.residual_bp) for row in quote_rows):.2f}")

    print("\nPT-xUSD PRE-EVENT BACKTEST")
    xusd = xusd_pre_event_backtest()
    print(xusd.reason)
    print("signals=" + ", ".join(f"{key}:{value}" for key, value in xusd.metrics.items()))

    print("\nCURRENT-LIVE REJECTION COST")
    rows, decisions, unquoted = _current_credit_rows(as_of, evidence_loader=_offline_evidence_loader())
    report = false_positive_report(rows)
    print(f"false_positive_rate_on_currently_live={report.rejected_count}/{report.live_count} ({report.false_positive_rate:.1%})")
    print(f"  rejected_on_credit={len(report.rejected_on_credit)}/{report.live_count} ({report.credit_rejection_rate:.1%}) — measured defect")
    print(f"  rejected_for_evidence={len(report.rejected_for_evidence)}/{report.live_count} — not underwritable on available data")
    print("rejected_on_credit=" + (", ".join(report.rejected_on_credit) if report.rejected_on_credit else "none"))
    print(f"tier_a_rejected={len(report.tier_a_rejected)}/{report.tier_a_count}")
    if report.live_count and report.rejected_count / report.live_count > 0.5:
        print("WARNING — the filter rejects most currently-live paper; it is a narrow quality-universe filter, not a broad carry selector")
    if report.live_count and len(report.rejected_for_evidence) > len(report.rejected_on_credit):
        print("WARNING — most rejections are for MISSING EVIDENCE, not measured credit defects. On this cache the")
        print("          filter is gated by data coverage, so it has not yet demonstrated it can separate a bargain")
        print("          discount from a default premium on paper it CAN see. Widen coverage before claiming that.")
    if report.tier_a_count and len(report.tier_a_rejected) / report.tier_a_count > 0.5:
        print("WARNING — the filter rejects most TIER_A paper and is not useful in its current form")

    print("\nFIXED VS FLOATING AND ALLOCATION")
    round2 = _round2_analysis()
    gas_markets = _gas_markets(round2)
    if gas_markets:
        for chain, gas in sorted(gas_markets.items()):
            print(
                f"GAS {chain}: {gas.gas_price_wei} wei, ETH/USD ${gas.eth_usd:.6f}, "
                f"gas UTC {gas.gas_pulled_at.isoformat()}, price UTC {gas.eth_usd_pulled_at.isoformat()}"
            )
    else:
        print("COULD NOT VERIFY gas_price_wei and ETH/USD — chain-specific candidates will not be allocated")
    reserve = _reserve_selection(markets, as_of=as_of)
    reserve_rate = reserve.rate
    print(f"Aave v3 USDC floor={reserve.aave_floor_rate:.4%} base; total={reserve.aave_total_rate:.4%}")
    print(reserve.trace)
    for evaluation in sorted(reserve.evaluations, key=lambda row: -row.venue.rate_total):
        venue = evaluation.venue
        verdict = "BID" if evaluation.decision is not None and evaluation.decision.bid else "NO BID"
        print(
            f"RESERVE LADDER {verdict} — {venue.instrument} | {venue.protocol}/{venue.chain} | "
            f"total {venue.rate_total:.4%} | base "
            f"{'COULD NOT VERIFY' if venue.rate_base is None else f'{venue.rate_base:.4%}'} | "
            f"reward {'COULD NOT VERIFY' if venue.rate_reward is None else f'{venue.rate_reward:.4%}'} | "
            f"TVL {'COULD NOT VERIFY' if venue.tvl_usd is None else f'${venue.tvl_usd:,.0f}'} | "
            f"exit {venue.redeem_steps} | {evaluation.reason}"
        )
    candidates = []
    comparisons = []
    for market in markets:
        if market.venue != "Pendle" or market.implied_apy is None:
            continue
        identity = record_identity(market)
        decision = decisions.get(identity) if identity is not None else None
        if decision is None or decision.tier.value != QualityTier.TIER_A.value or not decision.bid:
            continue
        penalty = (100.0 - decision.score) / 100.0 * 0.02
        edge = market.implied_apy - reserve_rate - penalty
        cvar = _market_cvar(market, round2)
        comparisons.append((market.instrument, market.implied_apy, reserve_rate, penalty, edge))
        candidates.append(Candidate(
            candidate_id=f"{market.chain}:{market.instrument}:{market.maturity.date()}",
            instrument=market.instrument, underlying=market.underlying or "unavailable", venue=market.venue,
            job=Job.CARRY, fixed_rate=market.implied_apy, floating_reference=reserve_rate,
            credit_penalty=penalty, expected_net_return=market.implied_apy - penalty,
            cvar95=cvar.value, illiquidity_score=0.25,
            executable_capacity_usd=market.executable_capacity_usd or 0.0,
            evidence=(decision.reason, cvar.detail), chain=market.chain,
            cvar_kind=cvar.kind, cvar_detail=cvar.detail,
        ))
    print("instrument | fixed credit yield | selected callable reserve | credit penalty | carry edge")
    for instrument, fixed, floating, penalty, edge in comparisons:
        print(f"{instrument} | {fixed:.4%} | {floating:.4%} | {penalty:.4%} | {edge:+.4%}")
    print("Carry edge is a credit-adjusted term comparison; venue dispersion is reported separately above.")
    allocation_kwargs = {
        "portfolio_usd": 1_000_000, "reserve_selection": reserve,
        "gas_markets": gas_markets, "ops_per_year": DEFAULT_OPS_PER_YEAR,
    } if gas_markets else {"portfolio_usd": 1_000_000, "reserve_selection": reserve}
    before = allocate(candidates, config=AllocationConfig(lambda_cvar=0.0), **allocation_kwargs)
    result = allocate(candidates, config=AllocationConfig(), **allocation_kwargs)
    print(f"CVaR before (lambda=0, objective={before.objective:.6%}): " + (
        ", ".join(f"{row.instrument} {row.weight:.1%}" for row in before.allocations) or "no fixed allocation"
    ))
    print(f"CVaR live/fallback (lambda=1, objective={result.objective:.6%}): " + (
        ", ".join(f"{row.instrument} {row.weight:.1%}" for row in result.allocations) or "no fixed allocation"
    ))
    print(f"reserve={result.reserve_weight:.1%} (${result.reserve_amount_usd:,.0f})")
    for sleeve in result.reserve_allocations:
        print(sleeve.trace)
    for allocation in result.allocations:
        print(allocation.trace)
    for rejection in result.rejected:
        print("WAIT — " + rejection)
    measured = sum(candidate.cvar_kind == "measured" for candidate in candidates)
    fallback = sum(candidate.cvar_kind == "fallback" for candidate in candidates)
    print(f"CVaR coverage for allocation candidates: measured={measured}, fallback={fallback}; fallback is an explicit 2.00% policy charge, never zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
