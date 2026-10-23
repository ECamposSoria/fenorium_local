from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Generic, Iterable, TypeVar

from research.par_sources.normalize import QualityTier

from .pricing import implied_rate, year_fraction


T = TypeVar("T")
ROOT = Path(__file__).resolve().parents[1]
XUSD_MATURITY = datetime(2025, 12, 18, tzinfo=UTC)
XUSD_DISCLOSURE = datetime(2025, 11, 3, tzinfo=UTC)

# The rubric's peg factor is defined over 90 days; two thirds of that window is the minimum
# coverage at which the statistic is meaningful rather than an artefact of a short life.
PEG_WINDOW_DAYS = 90

# A drawdown limit only means something against a promise. Par is a promise a dollar stablecoin
# makes, so 100bp off it is a break. An accruing asset measured against its own unit promises
# monotone accrual, not a level, and no limit for those classes has been established from data —
# so they carry no limit rather than borrowing the dollar one.
DOLLAR_PEG_LIMIT_BP = 100.0

PEG_DRAWDOWN_LIMIT_BP: dict[str, float | None] = {
    "USD": DOLLAR_PEG_LIMIT_BP,
    "ETH": None,
    "BTC": None,
    "HYPE": None,
    "BNB": None,
    "UNKNOWN": None,
}
MIN_PEG_COVERAGE_DAYS = 60

# Policy weight, not an observation: daily closes understate intraday moves and 90 days
# rarely contains a stress day.
COLLATERAL_STRESS_MULTIPLE = 2.0
# Policy tolerance: the market series contains daily closes, while the oracle is live.
COLLATERAL_ORACLE_DEVIATION_BP = 500
# Fixed adapters may hide impairment; accept them only with a stable market ratio history.
COLLATERAL_FIXED_ADAPTER_DRAWDOWN_BP = 100

# Oracle model-risk rubric (10 points total). A documented periodic feed retains the historical
# freshness score exactly. Other classes receive less because they expose different, less directly
# measurable failure modes; these are policy weights, not observations about any individual feed.
ORACLE_UNDOCUMENTED_FEED_POINTS = 5.0
ORACLE_VARIABLE_ADAPTER_POINTS = 3.0


@dataclass(frozen=True)
class TimedValue(Generic[T]):
    timestamp: datetime
    value: T
    source: str


@dataclass(frozen=True)
class CollateralEvidence:
    symbol: str
    token: str
    lltv_wad: str | int
    same_as_loan: bool
    prices: tuple[TimedValue[float], ...]
    oracle_price_raw: int | None = None
    oracle_market_deviation_bp: float | None = None
    feed_model: str | None = None
    liquidation_size_usd: float | None = None
    liquidation_impact_bp: float | None = None
    collateral_decimals: int | None = None
    loan_decimals: int | None = None
    loan_symbol: str = "unknown"


@dataclass(frozen=True)
class CreditInputs:
    instrument: str
    underlying: str
    maturity: datetime | None
    implied_apy: TimedValue[float]
    peg_prices: tuple[TimedValue[float], ...]
    protocol_tvl_usd: tuple[TimedValue[float], ...]
    oracle_updated_at: TimedValue[datetime] | None
    market_liquidity_usd: TimedValue[float] | None
    holder_top10_share: TimedValue[float] | None
    oracle_heartbeat_seconds: TimedValue[float] | None = None
    tier_median_apy: TimedValue[float] | None = None
    transparent_yield_source_apy: TimedValue[float] | None = None
    callable_exit: bool = False
    # Price provenance is a first-class credit input.  `accruing` wrappers must also expose the
    # reference asset's market/NAV series; a rising share conversion rate cannot launder a weak
    # reference asset into a perfect peg score.
    # Existing callers supply direct market-price observations; retain that contract as the
    # default. New non-market assets must classify explicitly (`unknown` is scored at zero).
    price_source: str | None = "market"
    reference_price_source: str | None = None
    reference_peg_prices: tuple[TimedValue[float], ...] = ()
    wrapper_integrity_verified: bool = False
    peg_mechanism: str | None = None
    denomination: str = "USD"
    redemption_discount_bp: float | None = None
    redemption_source: str | None = None
    oracle_kind: str | None = None
    oracle_behavior: str | None = None
    oracle_reason: str | None = None
    price_disagreement_bp: float | None = None
    collaterals: tuple[CollateralEvidence, ...] | None = None


@dataclass(frozen=True)
class CreditDecision:
    score: float
    bid: bool
    reason: str
    tier: QualityTier
    metrics: dict[str, float | str | None]
    # "credit" — measured evidence says the paper is bad.
    # "evidence" — the paper may be fine; we cannot see enough to underwrite it.
    # Collapsing these two into one NO BID hides whether the filter discriminates at all.
    rejection_basis: str | None = None
    credit_blockers: tuple[str, ...] = ()
    evidence_blockers: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    fenorium_score: int | None = None
    score_cap: str | None = None


def _max_drawdown_bp(values: list[float]) -> float | None:
    """Worst fall from the running high, in bp.

    Deliberately NOT |price − $1|. Yield-bearing wrappers accrue by design: sUSDS trades near
    $1.1067 and a distance-from-a-dollar test reads that as a 1,067bp depeg, rejecting the safest
    paper in the set. Drawdown ignores accrual and catches the thing that actually matters — a
    token that stops holding a level it previously held. On xUSD this reads ~9,600bp.
    """
    peak = None
    worst = 0.0
    for value in values:
        if value <= 0:
            continue
        peak = value if peak is None else max(peak, value)
        worst = max(worst, (peak - value) / peak * 10_000)
    return worst if peak is not None else None


def _assert_as_of(as_of: datetime, points: Iterable[TimedValue[object]]) -> None:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    for point in points:
        if point.timestamp.tzinfo is None:
            raise ValueError(f"{point.source}: datapoint timestamp must be timezone-aware")
        if point.timestamp > as_of:
            raise AssertionError(
                f"anti-lookahead: {point.source} datapoint {point.timestamp.isoformat()} is after as_of {as_of.isoformat()}"
            )


def classify_quality(protocol_tvl_usd: float | None, max_peg_deviation_bp: float | None,
                     *, as_of: datetime, evidence: Iterable[TimedValue[object]] = ()) -> QualityTier:
    """The exact TIER_A/B/C rule from research/validate.py."""
    _assert_as_of(as_of, evidence)
    if protocol_tvl_usd is not None and protocol_tvl_usd >= 1_000_000_000 and max_peg_deviation_bp is not None and max_peg_deviation_bp <= 50:
        return QualityTier.TIER_A
    if protocol_tvl_usd is not None and protocol_tvl_usd >= 100_000_000 and max_peg_deviation_bp is not None and max_peg_deviation_bp <= 100:
        return QualityTier.TIER_B
    return QualityTier.TIER_C


def score_credit(inputs: CreditInputs, *, as_of: datetime) -> CreditDecision:
    points: list[TimedValue[object]] = [inputs.implied_apy, *inputs.peg_prices,
                                       *inputs.reference_peg_prices, *inputs.protocol_tvl_usd]
    points.extend(point for point in (inputs.oracle_updated_at, inputs.oracle_heartbeat_seconds,
                                     inputs.market_liquidity_usd,
                                     inputs.holder_top10_share, inputs.tier_median_apy,
                                     inputs.transparent_yield_source_apy) if point is not None)
    points.extend(point for collateral in inputs.collaterals or () for point in collateral.prices)
    _assert_as_of(as_of, points)
    if inputs.oracle_updated_at is not None and inputs.oracle_updated_at.value > as_of:
        raise AssertionError(
            f"anti-lookahead: oracle update {inputs.oracle_updated_at.value.isoformat()} is after as_of {as_of.isoformat()}"
        )
    if inputs.maturity is not None and inputs.maturity.tzinfo is None:
        raise ValueError("maturity must be timezone-aware")

    peg_values = [point.value for point in inputs.peg_prices]
    max_peg_bp = _max_drawdown_bp(peg_values)
    reference_peg_bp = _max_drawdown_bp([point.value for point in inputs.reference_peg_prices])
    peg_vol_bp = statistics.pstdev(peg_values) * 10_000 if len(peg_values) >= 2 else None
    tvl_points = sorted(inputs.protocol_tvl_usd, key=lambda point: point.timestamp)
    tvl_latest = tvl_points[-1].value if tvl_points else None
    tvl_change = None
    if len(tvl_points) >= 2 and tvl_points[0].value > 0:
        tvl_change = tvl_latest / tvl_points[0].value - 1.0
    tier = classify_quality(tvl_latest, max_peg_bp, as_of=as_of, evidence=points)
    days = None if inputs.maturity is None else (inputs.maturity - as_of).total_seconds() / 86400.0
    oracle_age_hours = None
    oracle_heartbeat_hours = None
    oracle_age_heartbeat_ratio = None
    if inputs.oracle_updated_at is not None:
        oracle_age_hours = (as_of - inputs.oracle_updated_at.value).total_seconds() / 3600.0
    if inputs.oracle_heartbeat_seconds is not None:
        if inputs.oracle_heartbeat_seconds.value <= 0:
            raise ValueError("oracle heartbeat must be positive")
        oracle_heartbeat_hours = inputs.oracle_heartbeat_seconds.value / 3600.0
    if oracle_age_hours is not None and oracle_heartbeat_hours is not None:
        oracle_age_heartbeat_ratio = oracle_age_hours / oracle_heartbeat_hours
    oracle_kind = inputs.oracle_kind
    oracle_behavior = inputs.oracle_behavior
    # Backwards-compatible inference for the documented-feed control and existing callers. Merely
    # having a timestamp is not enough; both timestamp and documented heartbeat must be present.
    if oracle_kind is None and oracle_age_heartbeat_ratio is not None:
        oracle_kind = "genuine_feed"
        oracle_behavior = "documented_heartbeat"
    oracle_model_score = 0.0
    oracle_model_verified = False
    if oracle_kind == "genuine_feed" and oracle_behavior == "documented_heartbeat":
        if oracle_age_heartbeat_ratio is not None:
            oracle_model_verified = True
            oracle_model_score = 10.0 if oracle_age_heartbeat_ratio < 0.25 else \
                7.0 if oracle_age_heartbeat_ratio <= 1.0 else 0.0
    elif oracle_kind == "genuine_feed" and oracle_behavior == "heartbeat_undocumented":
        oracle_model_verified = True
        oracle_model_score = ORACLE_UNDOCUMENTED_FEED_POINTS
    elif oracle_kind == "onchain_adapter" and oracle_behavior == "variable_price":
        oracle_model_verified = True
        oracle_model_score = ORACLE_VARIABLE_ADAPTER_POINTS
    elif oracle_kind == "onchain_adapter" and oracle_behavior == "hardcoded_1to1":
        oracle_model_verified = True
    liquidity = inputs.market_liquidity_usd.value if inputs.market_liquidity_usd else None
    concentration = inputs.holder_top10_share.value if inputs.holder_top10_share else None
    excess_pp = None
    if inputs.tier_median_apy is not None:
        excess_pp = (inputs.implied_apy.value - inputs.tier_median_apy.value) * 100.0
    unexplained_pp = None
    if inputs.transparent_yield_source_apy is not None:
        unexplained_pp = (inputs.implied_apy.value - inputs.transparent_yield_source_apy.value) * 100.0

    # The rubric defines peg stability as the max drawdown over a 90-day window. A short series
    # cannot evidence that statistic: a token that has never been observed through stress has not
    # demonstrated stability, it has merely not been tested. Measured 2026-08-18: strUSD had 22
    # daily points and scored a full 30/30, which is the same mistake as reading a private-credit
    # NAV that only marks up as proof of safety. Insufficient coverage scores 0 and is reported as
    # missing evidence, never as a credit defect.
    # Distinguish a genuinely SHORT SERIES from a SCALAR SUMMARY. A caller that hands over one
    # synthesised point (a drawdown already computed upstream) is not evidencing 1 day of history —
    # it simply is not passing a series, and penalising that would reject the entire universe for a
    # pipeline shape rather than for anything about the asset. Only a real multi-point series is
    # held to the rubric's 90-day window.
    peg_coverage = len(peg_values)
    peg_is_series = peg_coverage > 1
    peg_coverage_ok = (not peg_is_series) or peg_coverage >= MIN_PEG_COVERAGE_DAYS

    def peg_points(drawdown_bp: float | None) -> float:
        if not peg_coverage_ok:
            return 0.0
        return 30.0 if drawdown_bp is not None and drawdown_bp < 25 else \
            20.0 if drawdown_bp is not None and drawdown_bp <= 50 else \
            10.0 if drawdown_bp is not None and drawdown_bp <= 100 else 0.0

    raw_peg_score = peg_points(max_peg_bp)
    price_caps = {"market": 30.0, "accruing": 30.0, "oracle_nav": 20.0, "self_nav": 10.0}
    price_cap = price_caps.get(inputs.price_source, 0.0)
    if inputs.price_source == "accruing":
        reference_cap = price_caps.get(inputs.reference_price_source, 0.0)
        reference_score = peg_points(reference_peg_bp)
        integrity_score = 30.0 if inputs.wrapper_integrity_verified else 0.0
        final_peg_score = min(raw_peg_score, reference_score, reference_cap, price_cap, integrity_score)
    else:
        reference_score = None
        integrity_score = None
        final_peg_score = min(raw_peg_score, price_cap)

    score = final_peg_score
    score += 20.0 if tvl_latest is not None and tvl_latest > 1_000_000_000 else \
        15.0 if tvl_latest is not None and tvl_latest >= 500_000_000 else \
        10.0 if tvl_latest is not None and tvl_latest >= 100_000_000 else \
        5.0 if tvl_latest is not None and tvl_latest >= 20_000_000 else 0.0
    score += 10.0 if tvl_change is not None and tvl_change > 0 else \
        7.0 if tvl_change is not None and tvl_change >= -0.10 else \
        3.0 if tvl_change is not None and tvl_change >= -0.25 else 0.0
    score += oracle_model_score
    score += 15.0 if liquidity is not None and liquidity >= 1_000_000 else 8.0 if liquidity is not None and liquidity >= 100_000 else 0.0
    score += 5.0 if concentration is not None and concentration < 0.10 else \
        3.0 if concentration is not None and concentration <= 0.25 else 0.0
    score += 10.0 if inputs.callable_exit else 10.0 if days is not None and 7 <= days <= 365 \
        else 5.0 if days is not None and 2 < days <= 730 else 0.0

    # Split deliberately: a measured defect is a different statement from an unmeasurable asset,
    # and a filter that cannot tell them apart has not demonstrated any credit judgement.
    credit_blockers: list[str] = []
    evidence_blockers: list[str] = []
    missing_fields: list[str] = []
    collateral_text: list[str] = []
    collateral_multiples: list[float] = []
    if inputs.collaterals == ():
        evidence_blockers.append("collateral list unavailable")
        missing_fields.append("collateral_list")
    collateral_start = (as_of.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                        - timedelta(days=PEG_WINDOW_DAYS))
    for collateral in inputs.collaterals or ():
        symbol = collateral.symbol
        wad = collateral.lltv_wad
        try:
            wad_value = int(wad) if (type(wad) in (str, int)
                                    and re.fullmatch(r"[0-9]+", str(wad))) else 0
        except ValueError:
            wad_value = 0
        if not 0 < wad_value < 10**18:
            evidence_blockers.append(f"collateral {symbol} LLTV unreadable")
            if "collateral_lltv" not in missing_fields:
                missing_fields.append("collateral_lltv")
            continue
        lltv = wad_value / 10**18
        cushion = (10**18 - wad_value) / 10**18
        if collateral.same_as_loan:
            collateral_text.append(f"{symbol} same as the loan")
            continue
        daily = {
            point.timestamp.astimezone(UTC).date(): point.value
            for point in sorted(collateral.prices, key=lambda point: point.timestamp)
            if collateral_start <= point.timestamp <= as_of
            and math.isfinite(point.value) and point.value > 0
        }
        if type(collateral.oracle_price_raw) is not int or collateral.oracle_price_raw <= 0:
            evidence_blockers.append(f"collateral {symbol} oracle unreadable")
            if "collateral_oracle" not in missing_fields:
                missing_fields.append("collateral_oracle")
        deviation = collateral.oracle_market_deviation_bp
        if (deviation is not None and math.isfinite(deviation)
                and deviation > COLLATERAL_ORACLE_DEVIATION_BP
                and not math.isclose(deviation, COLLATERAL_ORACLE_DEVIATION_BP, rel_tol=1e-14)):
            credit_blockers.append(
                f"collateral {symbol} oracle price is {deviation:.0f} bp from its market price"
            )
        feed_model = collateral.feed_model
        feed_models = set((feed_model or "unverified").split("+"))
        if feed_models & {"unverified", "updated_at_zero"}:
            evidence_blockers.append(f"collateral {symbol} oracle feed unverified")
            if "collateral_oracle_feed" not in missing_fields:
                missing_fields.append("collateral_oracle_feed")
        if "hardcoded_1to1" in feed_models:
            drawdown = _max_drawdown_bp(list(daily.values()))
            if drawdown is not None and drawdown > COLLATERAL_FIXED_ADAPTER_DRAWDOWN_BP and not math.isclose(
                drawdown, COLLATERAL_FIXED_ADAPTER_DRAWDOWN_BP, rel_tol=1e-14
            ):
                credit_blockers.append(f"collateral {symbol} is priced by a fixed 1:1 adapter")
        impact = collateral.liquidation_impact_bp
        size = collateral.liquidation_size_usd
        if impact is None or not math.isfinite(impact) or size is None or not math.isfinite(size) or size <= 0:
            evidence_blockers.append(
                f"collateral {symbol} has no route to {collateral.loan_symbol} for liquidators"
            )
            if "collateral_liquidation_depth" not in missing_fields:
                missing_fields.append("collateral_liquidation_depth")
        else:
            stressed_impact = impact / 10_000 * COLLATERAL_STRESS_MULTIPLE
            if stressed_impact >= cushion or math.isclose(stressed_impact, cushion, rel_tol=1e-14):
                credit_blockers.append(
                    f"selling ${size:,.0f} of {symbol} costs {impact / 100:.1f}%, "
                    f"more than half its {cushion:.1%} cushion"
                )
        if len(daily) < MIN_PEG_COVERAGE_DAYS:
            evidence_blockers.append(
                f"collateral {symbol} has {len(daily)} days of price history against the 60 needed"
                if daily else f"collateral {symbol} price history unavailable"
            )
            if "collateral_price_history" not in missing_fields:
                missing_fields.append("collateral_price_history")
            continue
        days_and_prices = sorted(daily.items())
        worst = max([0.0] + [
            (prior - price) / prior
            for (prior_day, prior), (day, price) in zip(days_and_prices, days_and_prices[1:])
            if (day - prior_day).days == 1
        ])
        if worst > 0:
            collateral_multiples.append(cushion / worst)
        stressed_fall = worst * COLLATERAL_STRESS_MULTIPLE
        # Keep the inclusive boundary when division rounds an equal ratio down by an ULP.
        if stressed_fall >= cushion or math.isclose(stressed_fall, cushion, rel_tol=1e-14):
            credit_blockers.append(
                f"collateral {symbol} fell {worst:.1%} in one day within 90 days; "
                f"at {lltv:.1%} LLTV its {cushion:.1%} cushion is under twice that"
            )
        collateral_text.append(
            f"{symbol} worst day {worst:.1%} vs {cushion:.1%} cushion at {lltv:.1%} LLTV"
            + (f"; oracle within {deviation:.0f} bp" if deviation is not None else "")
            + f"; {feed_model}"
            + (f"; liquidation cost {impact / 100:.1f}% at ${size:,.0f}"
               if impact is not None and size is not None else "")
        )
    if inputs.price_source not in price_caps:
        evidence_blockers.append("price-source classification unavailable")
    elif inputs.price_source == "accruing" and inputs.reference_price_source not in price_caps:
        evidence_blockers.append("reference price-source classification unavailable")
    elif inputs.price_source == "accruing" and not inputs.reference_peg_prices:
        evidence_blockers.append("reference peg history unavailable")
    elif inputs.price_source == "accruing" and not inputs.wrapper_integrity_verified:
        evidence_blockers.append("wrapper integrity unavailable")
    if max_peg_bp is not None and peg_is_series and not peg_coverage_ok:
        evidence_blockers.append(
            f"peg history covers {peg_coverage} of the {PEG_WINDOW_DAYS}d rubric window "
            f"(minimum {MIN_PEG_COVERAGE_DAYS}); stability is untested, not demonstrated"
        )
    if max_peg_bp is None:
        evidence_blockers.append("peg history unavailable")
    elif inputs.redemption_discount_bp is not None:
        # With a redemption reference the question has an answer in any denomination: how far
        # below redeemable value is it trading. 100bp means the same thing here as it does off
        # par, so no separate limit is invented. A premium is clamped to zero, not scored as
        # strength — sENA sits 36bp above redemption and that is not a credit fact in our favour.
        if inputs.redemption_discount_bp > DOLLAR_PEG_LIMIT_BP:
            credit_blockers.append(
                f"trades {inputs.redemption_discount_bp:.0f}bp below on-chain redeemable value"
            )
    elif (limit := PEG_DRAWDOWN_LIMIT_BP.get(inputs.denomination)) is None:
        # The 100bp line means something for a dollar peg, where par is a fixed promise. Against
        # its own unit an accruing asset has no par, and no calibrated limit was established for
        # these classes: measured 2026-08-19, stETH/ETH drew down 173bp over a calm year while
        # DefiLlama's 2022 series is unusable for an anchor (it prints stETH 14% ABOVE ETH).
        # Reusing the dollar line would manufacture a defect out of ordinary ratio drift, so the
        # figure is reported and the gap is declared instead of a defect being claimed.
        evidence_blockers.append(
            f"{inputs.denomination}-denominated drawdown measured at {max_peg_bp:.0f}bp, but no "
            f"calibrated drawdown limit exists for this denomination"
        )
        missing_fields.append("peg_drawdown_limit")
    elif max_peg_bp > limit:
        credit_blockers.append(f"underlying peg drawdown {max_peg_bp:.0f}bp")
    if tvl_change is not None and tvl_change < -0.25:
        credit_blockers.append(f"underlying TVL {tvl_change * 100:+.1f}% over observed window")
    if oracle_age_heartbeat_ratio is not None and oracle_age_heartbeat_ratio > 1.0:
        credit_blockers.append(
            f"oracle age {oracle_age_hours:.1f}h exceeds its {oracle_heartbeat_hours:.1f}h heartbeat"
        )
    oracle_impairment_watch = None
    impairment_peg_bp = None
    impairment_coverage_ok = False
    impairment_history = "the 90-day market peg history"
    if oracle_kind == "onchain_adapter" and oracle_behavior == "hardcoded_1to1":
        if inputs.price_source == "market":
            impairment_peg_bp = max_peg_bp
            impairment_coverage_ok = peg_coverage_ok
        elif (inputs.price_source == "accruing" and inputs.reference_price_source == "market"
              and inputs.reference_peg_prices):
            impairment_peg_bp = reference_peg_bp
            reference_coverage = len(inputs.reference_peg_prices)
            impairment_coverage_ok = reference_coverage == 1 or reference_coverage >= MIN_PEG_COVERAGE_DAYS
            impairment_history = "the underlying's 90-day market peg history"
        if impairment_coverage_ok and impairment_peg_bp is not None and impairment_peg_bp <= 100:
            oracle_impairment_watch = "market_peg_history"
        else:
            credit_blockers.append(
                "oracle model is hardcoded 1:1 and cannot express impairment"
            )
    if liquidity is not None and liquidity < 100_000:
        credit_blockers.append(f"market liquidity only ${liquidity:,.0f}")
    if days is not None and days <= 2:
        credit_blockers.append(f"only {max(days, 0):.1f} days to maturity")
    excess_threshold_apy = None if inputs.tier_median_apy is None else inputs.tier_median_apy.value * 1.5
    if excess_threshold_apy is not None and inputs.implied_apy.value > excess_threshold_apy:
        credit_blockers.append(
            f"implied {inputs.implied_apy.value * 100:.1f}% exceeds 1.5x the {tier.value} median "
            f"({excess_threshold_apy * 100:.1f}%)"
        )
    unavailable = []
    if not inputs.peg_prices:
        missing_fields.append("peg_history")
        unavailable.append("peg history")
    if inputs.price_source not in price_caps:
        missing_fields.append("price_source")
        unavailable.append("price-source classification")
    if inputs.price_source == "accruing" and not inputs.reference_peg_prices:
        missing_fields.append("reference_peg_history")
        unavailable.append("reference peg history")
    if inputs.price_source == "accruing" and not inputs.wrapper_integrity_verified:
        missing_fields.append("wrapper_integrity")
        unavailable.append("wrapper integrity")
    if not inputs.protocol_tvl_usd:
        missing_fields.extend(("protocol_tvl", "protocol_tvl_trend"))
        unavailable.append("protocol TVL/trend")
    elif tvl_change is None:
        missing_fields.append("protocol_tvl_trend")
        unavailable.append("30d TVL trend")
    if not oracle_model_verified:
        missing_fields.append("oracle_model_risk")
        unavailable.append("oracle model risk")
    if inputs.market_liquidity_usd is None:
        missing_fields.append("market_liquidity_usd")
        unavailable.append("USD market depth")
    if inputs.holder_top10_share is None:
        missing_fields.append("holder_concentration")
        unavailable.append("holder concentration")
    if score < 60:
        if missing_fields:
            evidence_blockers.extend(f"missing field: {field}" for field in missing_fields)
            evidence_blockers.append(f"evidence score {score:.0f}/100 is below 60")
        else:
            credit_blockers.append(f"measured evidence score {score:.0f}/100 is below 60")
    blockers = credit_blockers + evidence_blockers

    decision = not blockers
    # Primary basis remains credit whenever a measured defect exists. Secondary missing-evidence
    # blockers remain explicit on the same decision, so reporting can expose a mixed rejection
    # without diluting a proven defect such as xUSD's drawdown.
    basis = None if decision else ("credit" if credit_blockers else "evidence")
    prefix = "BID" if decision else (
        "NO BID (credit + insufficient evidence)" if credit_blockers and evidence_blockers else
        "NO BID (credit)" if basis == "credit" else "NO BID (insufficient evidence)"
    )
    # A field can be absent on a passing score, since the rubric lets overwhelming evidence
    # elsewhere clear the floor. The figure then has to read as unverified rather than crash the
    # string or, worse, print a zero — venues outside Pendle quote no USD depth at all.
    def _usd(value: float | None) -> str:
        return "COULD NOT VERIFY" if value is None else f"${value:,.0f}"

    evidence_text = (f"PT implies {inputs.implied_apy.value * 100:.1f}%; " + "; ".join(blockers)) if blockers else (
        f"{tier.value}, max peg drawdown "
        f"{'COULD NOT VERIFY' if max_peg_bp is None else f'{max_peg_bp:.0f}bp'}, "
        f"TVL {_usd(tvl_latest)}, liquidity {_usd(liquidity)}, "
        + ("one-transaction exit" if inputs.callable_exit else f"{days:.0f}d to maturity")
    )
    if unavailable:
        evidence_text += "; unavailable: " + ", ".join(unavailable)
    if inputs.peg_mechanism is not None:
        evidence_text += f"; issuer peg mechanism: {inputs.peg_mechanism}"
    if oracle_impairment_watch is not None:
        evidence_text += (
            f"; oracle model is hardcoded 1:1; impairment watched through {impairment_history} "
            f"(max drawdown {impairment_peg_bp:.0f}bp)"
        )
    if inputs.oracle_reason is not None:
        evidence_text += f"; oracle: {inputs.oracle_reason}"
    if decision and collateral_text:
        evidence_text += "; collateral: " + ", ".join(collateral_text)
    fenorium_score = round(score) if decision else min(round(score), 59)
    score_cap = None
    if not decision and round(score) >= 60:
        score_cap = next(iter(credit_blockers or evidence_blockers), None)

    return CreditDecision(round(score, 2), decision, f"{prefix} — {evidence_text}", tier, {
        "collateral_worst_cushion_multiple": min(collateral_multiples) if collateral_multiples else None,
        "max_peg_deviation_bp": max_peg_bp,
        "reference_max_peg_deviation_bp": reference_peg_bp,
        "raw_peg_score": raw_peg_score,
        "price_source": inputs.price_source,
        "price_source_cap": price_cap,
        "reference_price_source": inputs.reference_price_source,
        "reference_peg_score": reference_score,
        "wrapper_integrity_score": integrity_score,
        "final_peg_score": final_peg_score,
        "implied_apy": inputs.implied_apy.value,
        "peg_volatility_bp": peg_vol_bp,
        "protocol_tvl_usd": tvl_latest,
        "tvl_change": tvl_change,
        "peg_mechanism": inputs.peg_mechanism,
        "redemption_discount_bp": inputs.redemption_discount_bp,
        "oracle_kind": oracle_kind,
        "oracle_behavior": oracle_behavior,
        "oracle_model_risk_score": oracle_model_score,
        "oracle_impairment_watch": oracle_impairment_watch,
        "oracle_age_hours": oracle_age_hours,
        "oracle_heartbeat_hours": oracle_heartbeat_hours,
        "oracle_age_heartbeat_ratio": oracle_age_heartbeat_ratio,
        "price_disagreement_bp": inputs.price_disagreement_bp,
        "liquidity_usd": liquidity,
        "holder_top10_share": concentration,
        "days_to_maturity": days,
        "callable_exit": "instant" if inputs.callable_exit else None,
        "excess_yield_pp": excess_pp,
        "excess_yield_threshold_apy": excess_threshold_apy,
        "unexplained_yield_pp": unexplained_pp,
    }, basis, tuple(credit_blockers), tuple(evidence_blockers), tuple(missing_fields),
        fenorium_score, score_cap)


def xusd_pre_event_backtest(cache_dir: Path = ROOT / "research" / "data" / "latest",
                            *, as_of: datetime = datetime(2025, 11, 2, 23, 59, 59, tzinfo=UTC)) -> CreditDecision:
    coin_path = cache_dir / "defillama_xusd_history_2025-10-01_2025-12-18.json"
    pt_path = cache_dir / "pendle_ohlcv_1_0xba1edf4a4c7eb8cb0dae0594326179f9c7d909d2_day_2025-10-01_2025-12-18.json"
    coin_payload = json.loads(coin_path.read_text(encoding="utf-8"))
    coins = coin_payload.get("coins")
    if not isinstance(coins, dict) or not coins:
        raise ValueError(f"{coin_path}: response lost non-empty `coins`")
    coin = next(iter(coins.values()))
    prices = coin.get("prices") if isinstance(coin, dict) else None
    if not isinstance(prices, list):
        raise ValueError(f"{coin_path}: response lost `coins.*.prices`")
    peg = tuple(TimedValue(datetime.fromtimestamp(float(row["timestamp"]), UTC), float(row["price"]), str(coin_path))
                for row in prices if datetime.fromtimestamp(float(row["timestamp"]), UTC) <= as_of)
    pt_payload = json.loads(pt_path.read_text(encoding="utf-8"))
    csv_text = pt_payload.get("results")
    if not isinstance(csv_text, str):
        raise ValueError(f"{pt_path}: response lost CSV `results`")
    rows = [row for row in csv.DictReader(io.StringIO(csv_text))
            if datetime.fromtimestamp(float(row["time"]), UTC) <= as_of]
    if not rows or not peg:
        raise ValueError("xUSD cache contains no pre-event PT or underlying observations")
    last = rows[-1]
    observed_at = datetime.fromtimestamp(float(last["time"]), UTC)
    apy = implied_rate(1.0, float(last["close"]), year_fraction(observed_at, XUSD_MATURITY))
    return score_credit(CreditInputs(
        instrument="PT-xUSD-18DEC2025", underlying="xUSD", maturity=XUSD_MATURITY,
        implied_apy=TimedValue(observed_at, apy, str(pt_path)), peg_prices=peg,
        protocol_tvl_usd=(), oracle_updated_at=None, market_liquidity_usd=None,
        holder_top10_share=None,
    ), as_of=as_of)


@dataclass(frozen=True)
class FalsePositiveReport:
    live_count: int
    rejected_count: int
    rejected: tuple[str, ...]
    tier_a_count: int
    tier_a_rejected: tuple[str, ...]
    rejected_on_credit: tuple[str, ...] = ()
    rejected_for_evidence: tuple[str, ...] = ()

    @property
    def false_positive_rate(self) -> float:
        return self.rejected_count / self.live_count if self.live_count else math.nan

    @property
    def credit_rejection_rate(self) -> float:
        """Share of live paper turned down on measured defects rather than on missing data.

        This, not the headline rejection rate, is the number that says whether the filter
        exercises credit judgement.
        """
        return len(self.rejected_on_credit) / self.live_count if self.live_count else math.nan


def false_positive_report(rows: Iterable[tuple[str, QualityTier, CreditDecision]]) -> FalsePositiveReport:
    materialized = list(rows)
    rejected = tuple(name for name, _tier, decision in materialized if not decision.bid)
    on_credit = tuple(name for name, _tier, d in materialized if not d.bid and d.credit_blockers)
    for_evidence = tuple(name for name, _tier, d in materialized if not d.bid and d.evidence_blockers)
    tier_a = [(name, decision) for name, tier, decision in materialized if tier.value == QualityTier.TIER_A.value]
    tier_a_rejected = tuple(name for name, decision in tier_a if not decision.bid)
    return FalsePositiveReport(len(materialized), len(rejected), rejected, len(tier_a), tier_a_rejected,
                               on_credit, for_evidence)
