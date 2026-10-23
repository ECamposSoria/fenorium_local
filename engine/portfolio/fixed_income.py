"""Verified-cashflow fixed-income analytics with no APY-only fabrication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
from typing import Any, Iterable

from .contracts import (
    BPS,
    MetricResult,
    MetricStatus,
    PortfolioContractError,
    ReasonCode,
    computed_metric,
    require_utc,
    unavailable_metric,
    utc_iso,
)


ACT_365_SECONDS = 365 * 24 * 60 * 60
MIN_FIXED_INCOME_COVERAGE_BPS = 9_000


class DayCountConvention(str, Enum):
    ACT_365 = "ACT/365"


class CompoundingConvention(str, Enum):
    ANNUAL_EFFECTIVE = "ANNUAL_EFFECTIVE"


@dataclass(frozen=True, slots=True)
class Cashflow:
    payment_at: datetime
    amount_usd_micros: int

    def __post_init__(self) -> None:
        require_utc(self.payment_at, field_name="Cashflow.payment_at")
        if isinstance(self.amount_usd_micros, bool) or not isinstance(self.amount_usd_micros, int):
            raise PortfolioContractError("cashflow amounts must be integer USD micros")
        if self.amount_usd_micros <= 0:
            raise PortfolioContractError("cashflow amount must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_at": utc_iso(self.payment_at, field_name="Cashflow.payment_at"),
            "amount_usd_micros": self.amount_usd_micros,
        }


@dataclass(frozen=True, slots=True)
class CashflowSchedule:
    """A schedule whose evidence, dates, convention, and cashflows were verified."""

    instrument_id: str
    cashflows: tuple[Cashflow, ...]
    day_count: DayCountConvention = DayCountConvention.ACT_365
    compounding: CompoundingConvention = CompoundingConvention.ANNUAL_EFFECTIVE
    verified: bool = False
    evidence_refs: tuple[str, ...] = ()
    method_version: str = "fenorium/fixed-income-schedule/v1"

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.method_version:
            raise PortfolioContractError("cashflow schedule requires instrument_id and method_version")
        if not self.cashflows:
            raise PortfolioContractError("cashflow schedule cannot be empty")
        dates = [require_utc(cashflow.payment_at) for cashflow in self.cashflows]
        if dates != sorted(dates):
            raise PortfolioContractError("cashflow schedule must be ordered by payment date")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "cashflows": [cashflow.to_dict() for cashflow in self.cashflows],
            "day_count": self.day_count.value,
            "compounding": self.compounding.value,
            "verified": self.verified,
            "evidence_refs": sorted(self.evidence_refs),
            "method_version": self.method_version,
        }


@dataclass(frozen=True, slots=True)
class FixedIncomeInput:
    """All prerequisites required before duration-family outputs become valid."""

    instrument_id: str
    value_usd_micros: int
    settlement_at: datetime
    method_version: str
    market_price_usd_micros: int | None = None
    redemption_value_usd_micros: int | None = None
    schedule: CashflowSchedule | None = None
    verified_price: bool = False
    verified_redemption: bool = False
    apy_bps: int | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.method_version:
            raise PortfolioContractError("fixed-income input requires identity and method_version")
        require_utc(self.settlement_at, field_name="FixedIncomeInput.settlement_at")
        if self.value_usd_micros < 0:
            raise PortfolioContractError("fixed-income holding value cannot be negative")
        for name, value in (
            ("market_price_usd_micros", self.market_price_usd_micros),
            ("redemption_value_usd_micros", self.redemption_value_usd_micros),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise PortfolioContractError(f"{name} must be a positive integer USD-micro amount")
        if self.schedule is not None and self.schedule.instrument_id != self.instrument_id:
            raise PortfolioContractError("cashflow schedule identity must match fixed-income input")


@dataclass(frozen=True, slots=True)
class FixedIncomeAnalytics:
    ytm: MetricResult
    macaulay_duration: MetricResult
    modified_duration: MetricResult
    dv01: MetricResult
    pv01: MetricResult
    convexity: MetricResult
    pull_to_par: MetricResult
    analytical: bool
    as_of: datetime
    method_version: str
    exclusions: tuple[str, ...] = ()
    schema_version: str = "fenorium/fixed-income-analytics/v1"

    def __post_init__(self) -> None:
        require_utc(self.as_of, field_name="FixedIncomeAnalytics.as_of")
        if not self.method_version:
            raise PortfolioContractError("FixedIncomeAnalytics.method_version is required")
        if self.schema_version != "fenorium/fixed-income-analytics/v1":
            raise PortfolioContractError(
                "FixedIncomeAnalytics must use fenorium/fixed-income-analytics/v1"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="FixedIncomeAnalytics.as_of"),
            "ytm": self.ytm.to_dict(),
            "macaulay_duration": self.macaulay_duration.to_dict(),
            "modified_duration": self.modified_duration.to_dict(),
            "dv01": self.dv01.to_dict(),
            "pv01": self.pv01.to_dict(),
            "convexity": self.convexity.to_dict(),
            "pull_to_par": self.pull_to_par.to_dict(),
            "analytical": self.analytical,
            "exclusions": sorted(self.exclusions),
        }


def _failure(
    reason: ReasonCode, *, input: FixedIncomeInput, limitation: str
) -> FixedIncomeAnalytics:
    common = {
        "method_version": input.method_version,
        "as_of": input.settlement_at,
        "limitations": (limitation,),
        "excluded_positions": (input.instrument_id,),
    }
    return FixedIncomeAnalytics(
        ytm=unavailable_metric(reason, unit="BPS_ANNUAL_EFFECTIVE_YTM", **common),
        macaulay_duration=unavailable_metric(reason, unit="MICRO_YEARS_MACAULAY", **common),
        modified_duration=unavailable_metric(reason, unit="MICRO_YEARS_MODIFIED", **common),
        dv01=unavailable_metric(reason, unit="USD_MICROS_DV01", **common),
        pv01=unavailable_metric(reason, unit="USD_MICROS_PV01", **common),
        convexity=unavailable_metric(reason, unit="MICRO_YEARS_SQUARED_CONVEXITY", **common),
        pull_to_par=unavailable_metric(reason, unit="BPS_PULL_TO_PAR", **common),
        analytical=False,
        as_of=input.settlement_at,
        method_version=input.method_version,
        exclusions=(input.instrument_id,),
    )


def _act_365_years(start: datetime, end: datetime) -> Decimal:
    """Exact decimal counterpart to the existing engine's ACT/365 convention."""

    # engine.pricing defines the same ACT/365 convention in floats for its
    # market-model surface.  Portfolio evidence is dependency-free and
    # integer/Decimal-only, so it keeps its exact counterpart here rather than
    # importing the pricing module's optional HTTP dependency graph.
    if require_utc(end) <= require_utc(start):
        raise PortfolioContractError("cashflows must occur after settlement")
    delta = require_utc(end) - require_utc(start)
    seconds = (
        Decimal(delta.days * 86_400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )
    return seconds / Decimal(ACT_365_SECONDS)


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _present_value(cashflows: tuple[tuple[Decimal, Decimal], ...], rate: Decimal) -> Decimal:
    return sum(
        amount / ((Decimal(1) + rate) ** years)
        for years, amount in cashflows
    )


def _solve_ytm(cashflows: tuple[tuple[Decimal, Decimal], ...], price: Decimal) -> Decimal | None:
    low = Decimal("-0.999999")
    high = Decimal("100")
    if _present_value(cashflows, low) < price or _present_value(cashflows, high) > price:
        return None
    for _ in range(220):
        middle = (low + high) / 2
        present = _present_value(cashflows, middle)
        if present > price:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def analyze_fixed_income(input: FixedIncomeInput) -> FixedIncomeAnalytics:
    """Calculate only facts justified by a verified schedule and convention."""

    schedule = input.schedule
    if schedule is None:
        reason = (
            ReasonCode.METHODOLOGY_NOT_APPLICABLE
            if input.apy_bps is not None
            else ReasonCode.UNRESOLVED_POSITION
        )
        return _failure(
            reason,
            input=input,
            limitation="APY alone is not a verified redemption/cashflow schedule",
        )
    if not schedule.verified:
        return _failure(
            ReasonCode.UNRESOLVED_POSITION,
            input=input,
            limitation="cashflow schedule evidence is not verified",
        )
    if not input.verified_price or input.market_price_usd_micros is None:
        return _failure(
            ReasonCode.MISSING_PRICE,
            input=input,
            limitation="verified current price is required for fixed-income analytics",
        )
    if not input.verified_redemption or input.redemption_value_usd_micros is None:
        return _failure(
            ReasonCode.UNRESOLVED_POSITION,
            input=input,
            limitation="verified redemption value is required for fixed-income analytics",
        )
    if (
        schedule.day_count is not DayCountConvention.ACT_365
        or schedule.compounding is not CompoundingConvention.ANNUAL_EFFECTIVE
    ):
        return _failure(
            ReasonCode.METHODOLOGY_NOT_APPLICABLE,
            input=input,
            limitation="v1 supports verified ACT/365 annual-effective schedules only",
        )
    if input.redemption_value_usd_micros > schedule.cashflows[-1].amount_usd_micros:
        return _failure(
            ReasonCode.UNRESOLVED_POSITION,
            input=input,
            limitation="verified redemption is not represented in the terminal verified cashflow",
        )

    try:
        with localcontext() as context:
            context.prec = 64
            cashflows = tuple(
                (
                    _act_365_years(input.settlement_at, flow.payment_at),
                    Decimal(flow.amount_usd_micros),
                )
                for flow in schedule.cashflows
            )
            price = Decimal(input.market_price_usd_micros)
            ytm = _solve_ytm(cashflows, price)
            if ytm is None:
                return _failure(
                    ReasonCode.METHODOLOGY_NOT_APPLICABLE,
                    input=input,
                    limitation="cashflow present value did not bracket an annual-effective YTM",
                )
            present_values = tuple(
                amount / ((Decimal(1) + ytm) ** years) for years, amount in cashflows
            )
            macaulay = sum(
                years * present for (years, _), present in zip(cashflows, present_values)
            ) / price
            modified = macaulay / (Decimal(1) + ytm)
            one_bp = Decimal(1) / Decimal(BPS)
            dv01 = max(Decimal(0), price - _present_value(cashflows, ytm + one_bp))
            convexity = sum(
                amount * years * (years + Decimal(1))
                / ((Decimal(1) + ytm) ** (years + Decimal(2)))
                for years, amount in cashflows
            ) / price
            pull_to_par = (
                (Decimal(input.redemption_value_usd_micros) / price - Decimal(1)) * Decimal(BPS)
            )
    except (ArithmeticError, ValueError) as exc:
        return _failure(
            ReasonCode.METHODOLOGY_NOT_APPLICABLE,
            input=input,
            limitation=f"fixed-income calculation could not be verified: {type(exc).__name__}",
        )

    common = {
        "method_version": input.method_version,
        "as_of": input.settlement_at,
        "frequency": "ACT/365",
        "assumptions": (CompoundingConvention.ANNUAL_EFFECTIVE.value,),
        "sources": tuple(sorted(schedule.evidence_refs)),
    }
    return FixedIncomeAnalytics(
        ytm=computed_metric(
            _round(ytm * Decimal(BPS)), unit="BPS_ANNUAL_EFFECTIVE_YTM", **common
        ),
        macaulay_duration=computed_metric(
            _round(macaulay * Decimal(1_000_000)), unit="MICRO_YEARS_MACAULAY", **common
        ),
        modified_duration=computed_metric(
            _round(modified * Decimal(1_000_000)), unit="MICRO_YEARS_MODIFIED", **common
        ),
        dv01=computed_metric(_round(dv01), unit="USD_MICROS_DV01", **common),
        pv01=computed_metric(_round(dv01), unit="USD_MICROS_PV01", **common),
        convexity=computed_metric(
            _round(convexity * Decimal(1_000_000)),
            unit="MICRO_YEARS_SQUARED_CONVEXITY",
            **common,
        ),
        pull_to_par=computed_metric(_round(pull_to_par), unit="BPS_PULL_TO_PAR", **common),
        analytical=True,
        as_of=input.settlement_at,
        method_version=input.method_version,
    )


@dataclass(frozen=True, slots=True)
class FixedIncomePortfolioEntry:
    instrument_id: str
    analytical_nav_usd_micros: int
    analytics: FixedIncomeAnalytics

    def __post_init__(self) -> None:
        if self.analytical_nav_usd_micros < 0:
            raise PortfolioContractError("fixed-income analytical NAV cannot be negative")


@dataclass(frozen=True, slots=True)
class FixedIncomeAggregate:
    weighted_ytm: MetricResult
    weighted_macaulay_duration: MetricResult
    weighted_modified_duration: MetricResult
    total_dv01: MetricResult
    total_pv01: MetricResult
    weighted_convexity: MetricResult
    weighted_pull_to_par: MetricResult
    analytical_coverage_bps: int
    exclusions: tuple[str, ...]
    as_of: datetime
    method_version: str
    schema_version: str = "fenorium/fixed-income-aggregate/v1"

    def __post_init__(self) -> None:
        require_utc(self.as_of, field_name="FixedIncomeAggregate.as_of")
        if not self.method_version:
            raise PortfolioContractError("FixedIncomeAggregate.method_version is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="FixedIncomeAggregate.as_of"),
            "weighted_ytm": self.weighted_ytm.to_dict(),
            "weighted_macaulay_duration": self.weighted_macaulay_duration.to_dict(),
            "weighted_modified_duration": self.weighted_modified_duration.to_dict(),
            "total_dv01": self.total_dv01.to_dict(),
            "total_pv01": self.total_pv01.to_dict(),
            "weighted_convexity": self.weighted_convexity.to_dict(),
            "weighted_pull_to_par": self.weighted_pull_to_par.to_dict(),
            "analytical_coverage_bps": self.analytical_coverage_bps,
            "exclusions": sorted(self.exclusions),
        }


def aggregate_fixed_income(
    entries: Iterable[FixedIncomePortfolioEntry],
    *,
    fixed_income_nav_usd_micros: int,
    as_of: datetime,
    method_version: str,
    min_coverage_bps: int = MIN_FIXED_INCOME_COVERAGE_BPS,
) -> FixedIncomeAggregate:
    """Aggregate only analytically complete fixed-income holdings with visible exclusions."""

    require_utc(as_of, field_name="aggregate_fixed_income.as_of")
    if fixed_income_nav_usd_micros <= 0:
        raise PortfolioContractError("fixed_income_nav_usd_micros must be positive")
    rows = tuple(sorted(entries, key=lambda row: row.instrument_id))
    usable: list[FixedIncomePortfolioEntry] = []
    excluded: list[str] = []
    for row in rows:
        metrics = (
            row.analytics.ytm,
            row.analytics.macaulay_duration,
            row.analytics.modified_duration,
            row.analytics.dv01,
            row.analytics.pv01,
            row.analytics.convexity,
            row.analytics.pull_to_par,
        )
        if row.analytics.analytical and all(metric.status is MetricStatus.COMPUTED for metric in metrics):
            usable.append(row)
        else:
            excluded.append(row.instrument_id)
    covered = sum(row.analytical_nav_usd_micros for row in usable)
    if covered > fixed_income_nav_usd_micros:
        raise PortfolioContractError(
            "analytical fixed-income NAV cannot exceed the supplied fixed-income NAV denominator"
        )
    coverage = (covered * BPS) // fixed_income_nav_usd_micros

    if coverage < min_coverage_bps or not usable:
        reason = ReasonCode.INSUFFICIENT_COVERAGE
        common = {
            "method_version": method_version,
            "as_of": as_of,
            "coverage_bps": coverage,
            "excluded_positions": tuple(sorted(excluded)),
        }
        return FixedIncomeAggregate(
            weighted_ytm=unavailable_metric(reason, unit="BPS_ANNUAL_EFFECTIVE_YTM", **common),
            weighted_macaulay_duration=unavailable_metric(
                reason, unit="MICRO_YEARS_MACAULAY", **common
            ),
            weighted_modified_duration=unavailable_metric(
                reason, unit="MICRO_YEARS_MODIFIED", **common
            ),
            total_dv01=unavailable_metric(reason, unit="USD_MICROS_DV01", **common),
            total_pv01=unavailable_metric(reason, unit="USD_MICROS_PV01", **common),
            weighted_convexity=unavailable_metric(
                reason, unit="MICRO_YEARS_SQUARED_CONVEXITY", **common
            ),
            weighted_pull_to_par=unavailable_metric(reason, unit="BPS_PULL_TO_PAR", **common),
            analytical_coverage_bps=coverage,
            exclusions=tuple(sorted(excluded)),
            as_of=as_of,
            method_version=method_version,
        )

    def weighted(attribute: str) -> int:
        numerator = sum(
            row.analytical_nav_usd_micros * int(getattr(row.analytics, attribute).value)
            for row in usable
        )
        return numerator // covered

    common = {
        "method_version": method_version,
        "as_of": as_of,
        "coverage_bps": coverage,
        "excluded_positions": tuple(sorted(excluded)),
    }
    return FixedIncomeAggregate(
        weighted_ytm=computed_metric(
            weighted("ytm"), unit="BPS_ANNUAL_EFFECTIVE_YTM", **common
        ),
        weighted_macaulay_duration=computed_metric(
            weighted("macaulay_duration"), unit="MICRO_YEARS_MACAULAY", **common
        ),
        weighted_modified_duration=computed_metric(
            weighted("modified_duration"), unit="MICRO_YEARS_MODIFIED", **common
        ),
        total_dv01=computed_metric(
            sum(int(row.analytics.dv01.value) for row in usable), unit="USD_MICROS_DV01", **common
        ),
        total_pv01=computed_metric(
            sum(int(row.analytics.pv01.value) for row in usable), unit="USD_MICROS_PV01", **common
        ),
        weighted_convexity=computed_metric(
            weighted("convexity"), unit="MICRO_YEARS_SQUARED_CONVEXITY", **common
        ),
        weighted_pull_to_par=computed_metric(
            weighted("pull_to_par"), unit="BPS_PULL_TO_PAR", **common
        ),
        analytical_coverage_bps=coverage,
        exclusions=tuple(sorted(excluded)),
        as_of=as_of,
        method_version=method_version,
    )
