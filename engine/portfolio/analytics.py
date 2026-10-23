"""Coverage-gated whole-portfolio point-in-time and historical analytics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
from fractions import Fraction
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from engine.risk import conservative_cvar_fallback

from .contracts import (
    BPS,
    CapabilityStatus,
    MetricResult,
    MetricStatus,
    PortfolioContractError,
    ReasonCode,
    computed_metric,
    require_utc,
    unavailable_metric,
    utc_iso,
)
from .valuation import PortfolioValuation, PricedPosition


MIN_ES_OBSERVATIONS = 250
MIN_RISK_COVERAGE_BPS = 9_000


DAILY_RETURN_TEXT = re.compile(r"^-?\d+\.\d{6}$")
DAILY_RETURN_PLACES = Decimal("0.000001")


class MarkingFrequency(str, Enum):
    UTC_DAILY = "UTC_DAILY"


class AnalyticsScope(str, Enum):
    TOTAL = "TOTAL"
    VALUED_HOLDINGS = "VALUED_HOLDINGS"


@dataclass(frozen=True, slots=True)
class DailyReturn:
    """A signed one-day return in integer basis points at UTC midnight."""

    observed_at: datetime
    return_bps: int

    def __post_init__(self) -> None:
        timestamp = require_utc(self.observed_at, field_name="DailyReturn.observed_at")
        if timestamp.time() != datetime.min.time():
            raise PortfolioContractError("daily return observations must be normalized to UTC midnight")
        if isinstance(self.return_bps, bool) or not isinstance(self.return_bps, int):
            raise PortfolioContractError("daily return must be an integer basis-point value")
        if self.return_bps <= -BPS:
            raise PortfolioContractError("daily return cannot erase or invert NAV")

    @property
    def day(self) -> date:
        return require_utc(self.observed_at).date()

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": utc_iso(self.observed_at, field_name="DailyReturn.observed_at"),
            "return_bps": self.return_bps,
        }


@dataclass(frozen=True, slots=True)
class ReturnSeries:
    """Daily risk history for one stable position identity, with explicit provenance."""

    position_id: str
    frequency: MarkingFrequency
    annualization_factor: int
    returns: tuple[DailyReturn, ...]
    method_version: str
    provenance: str = "observed"

    def __post_init__(self) -> None:
        if not self.position_id or not self.method_version:
            raise PortfolioContractError("return series requires a position identity and method version")
        if self.annualization_factor not in (252, 365):
            raise PortfolioContractError("v1 annualization factor must be 252 or 365")
        days = [item.day for item in self.returns]
        if len(days) != len(set(days)):
            raise PortfolioContractError("return series cannot contain duplicate UTC dates")
        if days != sorted(days):
            raise PortfolioContractError("return series must be sorted by UTC date")
        if self.provenance not in ("observed", "modelled", "spliced"):
            raise PortfolioContractError("return series provenance must be observed, modelled, or spliced")

    def by_day(self) -> dict[date, int]:
        return {item.day: item.return_bps for item in self.returns}


@dataclass(frozen=True, slots=True)
class AllocationBucket:
    category: str
    value_usd_micros: int | None
    weight_bps: int | None
    position_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "value_usd_micros": self.value_usd_micros,
            "weight_bps": self.weight_bps,
            "position_ids": sorted(self.position_ids),
        }


@dataclass(frozen=True, slots=True)
class AllocationAnalysis:
    buckets: tuple[AllocationBucket, ...]
    concentration_hhi: MetricResult
    as_of: datetime
    method_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": utc_iso(self.as_of, field_name="AllocationAnalysis.as_of"),
            "method_version": self.method_version,
            "buckets": [bucket.to_dict() for bucket in self.buckets],
            "concentration_hhi": self.concentration_hhi.to_dict(),
        }


def allocation_and_concentration(
    valuation: PortfolioValuation, *, method_version: str | None = None
) -> AllocationAnalysis:
    """Calculate allocation independently from historical-risk eligibility.

    Unknown values are never silently discarded: they become an explicit UNKNOWN
    bucket.  We only expose allocation weights and HHI when the total NAV is
    authoritative.
    """

    method = method_version or valuation.method_version
    known: dict[str, list[PricedPosition]] = {}
    unknown: list[PricedPosition] = []
    for position in valuation.positions:
        if position.value_usd_micros is None:
            unknown.append(position)
        else:
            known.setdefault(position.category or "UNKNOWN", []).append(position)

    buckets: list[AllocationBucket] = []
    denominator = valuation.total_nav_usd_micros
    for category, rows in known.items():
        value = sum(row.value_usd_micros or 0 for row in rows)
        weight = None if denominator is None or denominator == 0 else (value * BPS) // denominator
        buckets.append(
            AllocationBucket(
                category=category,
                value_usd_micros=value,
                weight_bps=weight,
                position_ids=tuple(sorted(row.identity for row in rows)),
            )
        )
    # Holdings outside the registry belong here too. They are the reason the total is not
    # authoritative, and a bucket that omits them names an incompleteness without its cause.
    unpriced = tuple(sorted(
        [row.identity for row in unknown] + list(getattr(valuation, "unvalued_identities", ()))
    ))
    if unpriced:
        buckets.append(
            AllocationBucket(
                category="UNKNOWN",
                value_usd_micros=None,
                weight_bps=None,
                position_ids=unpriced,
            )
        )
    buckets.sort(key=lambda bucket: bucket.category)

    if denominator is None:
        concentration = unavailable_metric(
            ReasonCode.VALUE_COVERAGE_NOT_COMPUTABLE,
            unit="HHI_BPS_SQUARED",
            method_version=method,
            as_of=valuation.as_of,
            excluded_positions=unpriced,
        )
    elif denominator == 0:
        concentration = unavailable_metric(
            ReasonCode.METHODOLOGY_NOT_APPLICABLE,
            unit="HHI_BPS_SQUARED",
            method_version=method,
            as_of=valuation.as_of,
        )
    else:
        weights = [
            (bucket.value_usd_micros or 0) * BPS // denominator
            for bucket in buckets
            if bucket.value_usd_micros is not None
        ]
        concentration = computed_metric(
            sum(weight * weight for weight in weights),
            unit="HHI_BPS_SQUARED",
            method_version=method,
            as_of=valuation.as_of,
            coverage_bps=BPS,
        )
    return AllocationAnalysis(
        buckets=tuple(buckets),
        concentration_hhi=concentration,
        as_of=valuation.as_of,
        method_version=method,
    )


@dataclass(frozen=True, slots=True)
class PortfolioAnalytics:
    """All series-dependent metrics share one synchronized risk cohort."""

    expected_shortfall_95: MetricResult
    volatility: MetricResult
    drawdown: MetricResult
    correlation: MetricResult
    component_expected_shortfall: MetricResult
    risk_coverage_bps: int | None
    aligned_observations: int
    included_positions: tuple[str, ...]
    excluded_positions: tuple[str, ...]
    as_of: datetime
    method_version: str
    nav_scope: AnalyticsScope = AnalyticsScope.TOTAL
    included_provenance: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = "fenorium/portfolio-analytics/v1"
    # The aligned daily portfolio returns the metrics are computed from, (UTC day ISO, bps as a
    # decimal string with six places), oldest first — the dashboard draws NAV, drawdown and the
    # return distribution from them. Weighted returns are fractional; rounded to whole basis
    # points a 40/60 book losing 0.4 bp a day exported zeros and a flat chart beside a 100 bp
    # drawdown card (review dd).
    daily_returns: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_utc(self.as_of, field_name="PortfolioAnalytics.as_of")
        days = [day for day, _ in self.daily_returns]
        if any(not isinstance(day, str) or not isinstance(value, str) or not DAILY_RETURN_TEXT.fullmatch(value)
               for day, value in self.daily_returns):
            raise PortfolioContractError("daily returns must be (ISO day, decimal bps text) pairs")
        if days != sorted(days) or len(set(days)) != len(days):
            raise PortfolioContractError("daily returns must be sorted by UTC day without duplicates")
        if self.daily_returns and len(self.daily_returns) != self.aligned_observations:
            raise PortfolioContractError("daily returns must cover exactly the aligned observations")
        if not self.method_version:
            raise PortfolioContractError("PortfolioAnalytics.method_version is required")
        if self.schema_version != "fenorium/portfolio-analytics/v1":
            raise PortfolioContractError("PortfolioAnalytics must use fenorium/portfolio-analytics/v1")
        if not isinstance(self.nav_scope, AnalyticsScope):
            raise PortfolioContractError("PortfolioAnalytics.nav_scope must be an AnalyticsScope")
        provenance = dict(self.included_provenance or {})
        if set(provenance) - set(self.included_positions):
            raise PortfolioContractError("included provenance must belong to included positions")
        if any(value not in ("observed", "modelled", "spliced") for value in provenance.values()):
            raise PortfolioContractError("invalid included position provenance")
        object.__setattr__(self, "included_provenance", provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="PortfolioAnalytics.as_of"),
            "expected_shortfall_95": self.expected_shortfall_95.to_dict(),
            "volatility": self.volatility.to_dict(),
            "drawdown": self.drawdown.to_dict(),
            "correlation": self.correlation.to_dict(),
            "component_expected_shortfall": self.component_expected_shortfall.to_dict(),
            "risk_coverage_bps": self.risk_coverage_bps,
            "aligned_observations": self.aligned_observations,
            "included_positions": sorted(self.included_positions),
            "excluded_positions": sorted(self.excluded_positions),
            "nav_scope": self.nav_scope.value,
            "included_provenance": dict(sorted(self.included_provenance.items())),
            "daily_returns": [{"day": day, "return_bps": value} for day, value in self.daily_returns],
        }


def _failure_analytics(
    reason: ReasonCode,
    *,
    as_of: datetime,
    method_version: str,
    coverage_bps: int | None,
    observations: int | None,
    excluded: tuple[str, ...],
    frequency: str | None = None,
    nav_scope: AnalyticsScope = AnalyticsScope.TOTAL,
    limitations: tuple[str, ...] = (),
) -> PortfolioAnalytics:
    common = dict(
        method_version=method_version,
        as_of=as_of,
        observations=observations,
        coverage_bps=coverage_bps,
        excluded_positions=excluded,
        frequency=frequency,
        limitations=limitations,
    )
    return PortfolioAnalytics(
        expected_shortfall_95=unavailable_metric(
            reason, unit="BPS_ONE_DAY_ES95", window="TRAILING_DAILY_UTC", **common
        ),
        volatility=unavailable_metric(
            reason, unit="BPS_ANNUALIZED_VOLATILITY", window="TRAILING_DAILY_UTC", **common
        ),
        drawdown=unavailable_metric(
            reason, unit="BPS_MAX_DRAWDOWN", window="TRAILING_DAILY_UTC", **common
        ),
        correlation=unavailable_metric(
            reason, unit="CORRELATION_BPS", window="TRAILING_DAILY_UTC", **common
        ),
        component_expected_shortfall=unavailable_metric(
            reason, unit="BPS_ONE_DAY_COMPONENT_ES95", window="TRAILING_DAILY_UTC", **common
        ),
        risk_coverage_bps=coverage_bps,
        aligned_observations=observations or 0,
        included_positions=(),
        excluded_positions=excluded,
        as_of=as_of,
        method_version=method_version,
        nav_scope=nav_scope,
    )


def _round_decimal(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_fraction(value: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return Decimal(value.numerator) / Decimal(value.denominator)


def _portfolio_returns(
    series: tuple[ReturnSeries, ...],
    values: Mapping[str, int],
    denominator: int,
    *,
    last_day: date,
    trailing_days: int,
) -> tuple[tuple[date, Fraction], dict[str, dict[date, int]]]:
    if trailing_days < 1:
        raise PortfolioContractError("trailing_days must be positive")
    first_day = last_day - timedelta(days=trailing_days - 1)
    by_position = {
        row.position_id: {
            day: value
            for day, value in row.by_day().items()
            if first_day <= day <= last_day
        }
        for row in series
    }
    common_days = set.intersection(*(set(rows) for rows in by_position.values()))
    ordered_days = tuple(sorted(common_days))
    returns: list[tuple[date, Fraction]] = []
    for day in ordered_days:
        numerator = sum(values[position_id] * by_position[position_id][day] for position_id in values)
        returns.append((day, Fraction(numerator, denominator)))
    return tuple(returns), by_position


def _correlation_bps(first: tuple[int, ...], second: tuple[int, ...]) -> int:
    if len(first) != len(second) or len(first) < 2:
        raise PortfolioContractError("correlation requires two equally aligned series")
    n = len(first)
    first_mean = Fraction(sum(first), n)
    second_mean = Fraction(sum(second), n)
    covariance = sum(
        (Fraction(left) - first_mean) * (Fraction(right) - second_mean)
        for left, right in zip(first, second)
    )
    first_variance = sum((Fraction(value) - first_mean) ** 2 for value in first)
    second_variance = sum((Fraction(value) - second_mean) ** 2 for value in second)
    if first_variance == 0 or second_variance == 0:
        return 0
    with localcontext() as context:
        context.prec = 50
        value = (
            _decimal_fraction(covariance)
            / (_decimal_fraction(first_variance).sqrt() * _decimal_fraction(second_variance).sqrt())
        )
        return max(-BPS, min(BPS, _round_decimal(value * Decimal(BPS))))


def compute_portfolio_analytics(
    valuation: PortfolioValuation,
    return_series: Iterable[ReturnSeries],
    *,
    as_of: datetime,
    method_version: str,
    min_observations: int = MIN_ES_OBSERVATIONS,
    min_coverage_bps: int = MIN_RISK_COVERAGE_BPS,
    max_staleness_days: int = 2,
    trailing_days: int = 365,
    scope: AnalyticsScope = AnalyticsScope.TOTAL,
) -> PortfolioAnalytics:
    """Compute one-day ES95 and companion metrics over the same aligned UTC series.

    No missing days are filled. Every valued position without a compatible series
    remains in the declared scope denominator and can prevent risk computation.
    Modelled histories carry explicit provenance; they do not change the method version.
    """

    require_utc(as_of, field_name="compute_portfolio_analytics.as_of")
    if min_observations < 1:
        raise PortfolioContractError("min_observations must be positive")
    if not 0 <= min_coverage_bps <= BPS:
        raise PortfolioContractError("min_coverage_bps must be in [0, 10000]")
    if not isinstance(scope, AnalyticsScope):
        raise PortfolioContractError("scope must be an AnalyticsScope")
    if scope is AnalyticsScope.TOTAL and valuation.total_nav_usd_micros is None:
        return _failure_analytics(
            ReasonCode.UNRESOLVED_POSITION,
            as_of=as_of,
            method_version=method_version,
            coverage_bps=None,
            observations=None,
            # Holdings outside the registry belong here too: they are why the total was
            # withheld, and an exclusion list without them refuses a metric while naming
            # nothing responsible for the refusal.
            excluded=tuple(
                sorted(
                    [
                        position.identity
                        for position in valuation.positions
                        if position.value_usd_micros is None
                    ]
                    + list(getattr(valuation, "unvalued_identities", ()))
                )
            ),
            nav_scope=scope,
        )
    denominator = (
        valuation.priced_nav_lower_bound_usd_micros
        if scope is AnalyticsScope.VALUED_HOLDINGS
        else valuation.total_nav_usd_micros
    )
    assert denominator is not None
    scope_excluded = set(
        [position.identity for position in valuation.positions if position.value_usd_micros is None]
        + list(getattr(valuation, "unvalued_identities", ()))
    ) if scope is AnalyticsScope.VALUED_HOLDINGS else set()
    scope_limitations = (
        (f"over valued holdings; {len(scope_excluded)} holding{'' if len(scope_excluded) == 1 else 's'} excluded",)
        if scope is AnalyticsScope.VALUED_HOLDINGS else ()
    )
    if denominator <= 0:
        return _failure_analytics(
            ReasonCode.METHODOLOGY_NOT_APPLICABLE,
            as_of=as_of,
            method_version=method_version,
            coverage_bps=0,
            observations=0,
            excluded=tuple(sorted(scope_excluded)),
            nav_scope=scope,
            limitations=scope_limitations,
        )

    value_by_id = {
        position.identity: position.value_usd_micros
        for position in valuation.positions
        if position.value_usd_micros is not None and position.value_usd_micros > 0
    }
    series_by_id: dict[str, ReturnSeries] = {}
    for row in return_series:
        if row.position_id not in value_by_id:
            raise PortfolioContractError(
                f"return history belongs to unknown or unpriced position {row.position_id!r}"
            )
        if row.position_id in series_by_id:
            raise PortfolioContractError(f"duplicate return series for {row.position_id}")
        series_by_id[row.position_id] = row

    potential: list[ReturnSeries] = []
    excluded: set[str] = set(scope_excluded)
    incompatible: set[str] = set()
    method_incompatible: set[str] = set()
    for position in valuation.positions:
        if position.identity not in value_by_id:
            continue
        series = series_by_id.get(position.identity)
        if position.position.risk.status is not CapabilityStatus.SUPPORTED or series is None:
            excluded.add(position.identity)
            continue
        if series.frequency is not MarkingFrequency.UTC_DAILY:
            excluded.add(position.identity)
            incompatible.add(position.identity)
            continue
        if series.method_version != method_version:
            excluded.add(position.identity)
            method_incompatible.add(position.identity)
            continue
        potential.append(series)

    if not potential:
        coverage = 0
        reason = (
            ReasonCode.INCOMPATIBLE_MARKING_FREQUENCY
            if incompatible
            else ReasonCode.METHODOLOGY_NOT_APPLICABLE
            if method_incompatible
            else ReasonCode.INSUFFICIENT_COVERAGE
        )
        return _failure_analytics(
            reason,
            as_of=as_of,
            method_version=method_version,
            coverage_bps=coverage,
            observations=0,
            excluded=tuple(sorted(excluded)),
            nav_scope=scope,
            limitations=scope_limitations,
        )

    # 252- and 365-factor histories are separate marking cohorts.  Retain the
    # largest-supported cohort deterministically and only proceed if it covers 90%.
    groups: dict[int, list[ReturnSeries]] = {}
    for row in potential:
        groups.setdefault(row.annualization_factor, []).append(row)
    selected_factor, selected_series = max(
        groups.items(),
        key=lambda item: (
            sum(value_by_id[row.position_id] for row in item[1]),
            -item[0],
        ),
    )
    for factor, rows in groups.items():
        if factor != selected_factor:
            incompatible.update(row.position_id for row in rows)
            excluded.update(row.position_id for row in rows)
    selected = tuple(sorted(selected_series, key=lambda row: row.position_id))
    selected_values = {row.position_id: value_by_id[row.position_id] for row in selected}
    coverage = (sum(selected_values.values()) * BPS) // denominator
    if coverage < min_coverage_bps:
        reason = (
            ReasonCode.INCOMPATIBLE_MARKING_FREQUENCY
            if incompatible
            else ReasonCode.METHODOLOGY_NOT_APPLICABLE
            if method_incompatible
            else ReasonCode.INSUFFICIENT_COVERAGE
        )
        return _failure_analytics(
            reason,
            as_of=as_of,
            method_version=method_version,
            coverage_bps=coverage,
            observations=0,
            excluded=tuple(sorted(excluded)),
            frequency=MarkingFrequency.UTC_DAILY.value,
            nav_scope=scope,
            limitations=scope_limitations,
        )

    portfolio_returns, values_by_position = _portfolio_returns(
        selected,
        selected_values,
        denominator,
        last_day=require_utc(as_of).date(),
        trailing_days=trailing_days,
    )
    observations = len(portfolio_returns)
    if observations < min_observations:
        return _failure_analytics(
            ReasonCode.INSUFFICIENT_HISTORY,
            as_of=as_of,
            method_version=method_version,
            coverage_bps=coverage,
            observations=observations,
            excluded=tuple(sorted(excluded)),
            frequency=MarkingFrequency.UTC_DAILY.value,
            nav_scope=scope,
            limitations=scope_limitations,
        )
    latest_day = portfolio_returns[-1][0]
    if (require_utc(as_of).date() - latest_day) > timedelta(days=max_staleness_days):
        return _failure_analytics(
            ReasonCode.STALE_INPUT,
            as_of=as_of,
            method_version=method_version,
            coverage_bps=coverage,
            observations=observations,
            excluded=tuple(sorted(excluded)),
            frequency=MarkingFrequency.UTC_DAILY.value,
            nav_scope=scope,
            limitations=scope_limitations,
        )

    tail_count = math.ceil(observations * 0.05)
    ordered_tail = sorted(
        enumerate(portfolio_returns),
        key=lambda indexed: (indexed[1][1], indexed[1][0]),
    )[:tail_count]
    statistics = return_statistics(portfolio_returns, selected_factor)

    days = tuple(day for day, _ in portfolio_returns)
    correlation_values: dict[str, int] = {}
    component_values: dict[str, int] = {}
    for position_id in selected_values:
        rows = tuple(values_by_position[position_id][day] for day in days)
        tail_contributions = [
            Fraction(-selected_values[position_id] * values_by_position[position_id][day], denominator)
            for _, (day, _) in ordered_tail
        ]
        component_values[position_id] = _round_decimal(
            _decimal_fraction(sum(tail_contributions, Fraction(0)) / tail_count)
        )
        for other_id in selected_values:
            if position_id >= other_id:
                continue
            other_rows = tuple(values_by_position[other_id][day] for day in days)
            correlation_values[f"{position_id}|{other_id}"] = _correlation_bps(rows, other_rows)

    modelled_count = sum(row.provenance in ("modelled", "spliced") for row in selected)
    metric_limitations = scope_limitations + (
        (f"modelled daily prices for {modelled_count} position{'' if modelled_count == 1 else 's'} "
         "(rate moves only; excludes default risk)",)
        if modelled_count else ()
    )
    metric_sources = ("price-history historical-duration-v1",) if modelled_count else ()
    common = dict(
        method_version=method_version,
        as_of=as_of,
        window="TRAILING_DAILY_UTC",
        frequency=MarkingFrequency.UTC_DAILY.value,
        observations=observations,
        coverage_bps=coverage,
        excluded_positions=tuple(sorted(excluded)),
        limitations=metric_limitations,
        sources=metric_sources,
    )
    provenance = {row.position_id: row.provenance for row in selected}
    return PortfolioAnalytics(
        expected_shortfall_95=computed_metric(
            statistics["expected_shortfall_95_bps"],
            unit="BPS_ONE_DAY_ES95",
            assumptions=("HISTORICAL_ONE_DAY_ES95", f"TAIL_K={tail_count}"),
            **common,
        ),
        volatility=computed_metric(
            statistics["annualized_volatility_bps"],
            unit="BPS_ANNUALIZED_VOLATILITY",
            assumptions=(f"ANNUALIZATION_FACTOR={selected_factor}",),
            **common,
        ),
        drawdown=computed_metric(statistics["max_drawdown_bps"], unit="BPS_MAX_DRAWDOWN", **common),
        correlation=computed_metric(
            correlation_values, unit="CORRELATION_BPS", **common
        ),
        component_expected_shortfall=computed_metric(
            component_values, unit="BPS_ONE_DAY_COMPONENT_ES95", **common
        ),
        risk_coverage_bps=coverage,
        aligned_observations=observations,
        included_positions=tuple(sorted(selected_values)),
        excluded_positions=tuple(sorted(excluded)),
        as_of=as_of,
        method_version=method_version,
        nav_scope=scope,
        included_provenance=provenance,
        daily_returns=tuple((day.isoformat(), str(_decimal_fraction(value).quantize(DAILY_RETURN_PLACES, rounding=ROUND_HALF_UP)))
                            for day, value in portfolio_returns),
    )


def return_statistics(
    returns: Sequence[tuple[date, Fraction]], annualization_factor: int,
) -> dict[str, int]:
    """Historical risk in basis points; callers gate the observation count and freshness."""
    observations = len(returns)
    tail_count = math.ceil(observations * 0.05)
    ordered_tail = sorted(returns, key=lambda row: (row[1], row[0]))[:tail_count]
    tail_losses = [max(Fraction(0), -value) for _, value in ordered_tail]
    expected_shortfall = _round_decimal(_decimal_fraction(sum(tail_losses, Fraction(0)) / tail_count))

    values = tuple(value for _, value in returns)
    mean = sum(values, Fraction(0)) / observations
    variance = sum((value - mean) ** 2 for value in values) / observations
    with localcontext() as context:
        context.prec = 50
        annualized_volatility = _round_decimal(
            _decimal_fraction(variance).sqrt() * Decimal(annualization_factor).sqrt()
        )

    wealth = Fraction(BPS)
    peak = wealth
    maximum_drawdown = Fraction(0)
    for _, daily_return in returns:
        wealth *= (Fraction(BPS) + daily_return) / BPS
        peak = max(peak, wealth)
        maximum_drawdown = max(maximum_drawdown, Fraction((peak - wealth) * BPS, peak))
    return {
        "expected_shortfall_95_bps": expected_shortfall,
        "annualized_volatility_bps": annualized_volatility,
        "max_drawdown_bps": _round_decimal(_decimal_fraction(maximum_drawdown)),
    }


def internal_policy_charge(
    *,
    source: str,
    observations: int,
    as_of: datetime,
    method_version: str,
    missing: str,
) -> MetricResult:
    """Expose the legacy conservative charge only under its non-measurement name.

    This adapter deliberately calls the existing risk contract so the old policy is
    not duplicated, then serializes its known 2% policy constant as 200 bps.  Its
    status is not COMPUTED and callers must not substitute it for measured ES.
    """

    estimate = conservative_cvar_fallback(
        source=source, observations=observations, missing=missing
    )
    return MetricResult(
        status=MetricStatus.INTERNAL_POLICY_ONLY,
        value=200,
        unit="BPS_INTERNAL_POLICY_CHARGE",
        method_version=method_version,
        as_of=as_of,
        observations=estimate.observations,
        reason_code=ReasonCode.INSUFFICIENT_HISTORY,
        assumptions=("LEGACY_RISK_FALLBACK", "NOT_A_MEASURED_EXPECTED_SHORTFALL"),
        limitations=(estimate.detail,),
    )


portfolio_analytics = compute_portfolio_analytics
