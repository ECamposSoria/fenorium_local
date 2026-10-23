"""Mandates, single transformations, complete costs, and deterministic ranking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Iterable

from engine.composer import RouteBinding

from .contracts import (
    BPS,
    CapabilityResult,
    CapabilityStatus,
    MetricResult,
    MetricStatus,
    PortfolioContractError,
    ReasonCode,
    require_utc,
    utc_iso,
)


class OpportunityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    ANALYTICAL_ONLY = "ANALYTICAL_ONLY"
    NOT_RANKABLE = "NOT_RANKABLE"


class Objective(str, Enum):
    DEPLOY_IDLE_STABLECOINS = "DEPLOY_IDLE_STABLECOINS"
    INCREASE_FIXED_INCOME = "INCREASE_FIXED_INCOME"
    REBALANCE_SELECTED_RISK_ASSET = "REBALANCE_SELECTED_RISK_ASSET"
    REDUCE_RISK = "REDUCE_RISK"


class CapitalKind(str, Enum):
    STABLECOIN = "STABLECOIN"
    CASH = "CASH"
    VARIABLE_INCOME = "VARIABLE_INCOME"


class MandateStatus(str, Enum):
    PASS = "PASS"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"


COST_COMPONENTS = (
    "source_swap_usd_micros",
    "price_impact_usd_micros",
    "bridge_usd_micros",
    "integrator_fee_usd_micros",
    "fenorium_fee_usd_micros",
    "destination_venue_usd_micros",
    "gas_usd_micros",
    "entry_usd_micros",
)


@dataclass(frozen=True, slots=True)
class CostLedger:
    """All route economics in integer USD micros; no missing cost becomes zero."""

    source_swap_usd_micros: int | None
    price_impact_usd_micros: int | None
    bridge_usd_micros: int | None
    integrator_fee_usd_micros: int | None
    fenorium_fee_usd_micros: int | None
    destination_venue_usd_micros: int | None
    gas_usd_micros: int | None
    entry_usd_micros: int | None

    def __post_init__(self) -> None:
        for name in COST_COMPONENTS:
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise PortfolioContractError(f"{name} must be a non-negative integer USD-micro amount")
        for name in ("integrator_fee_usd_micros", "fenorium_fee_usd_micros"):
            value = getattr(self, name)
            if value not in (None, 0):
                raise PortfolioContractError(f"{name} must remain exactly zero in v1")

    @property
    def missing_components(self) -> tuple[str, ...]:
        return tuple(name for name in COST_COMPONENTS if getattr(self, name) is None)

    @property
    def complete(self) -> bool:
        return not self.missing_components

    @property
    def total_usd_micros(self) -> int | None:
        if not self.complete:
            return None
        return sum(int(getattr(self, name)) for name in COST_COMPONENTS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_swap_usd_micros": self.source_swap_usd_micros,
            "price_impact_usd_micros": self.price_impact_usd_micros,
            "bridge_usd_micros": self.bridge_usd_micros,
            "integrator_fee_usd_micros": self.integrator_fee_usd_micros,
            "fenorium_fee_usd_micros": self.fenorium_fee_usd_micros,
            "destination_venue_usd_micros": self.destination_venue_usd_micros,
            "gas_usd_micros": self.gas_usd_micros,
            "entry_usd_micros": self.entry_usd_micros,
            "total_usd_micros": self.total_usd_micros,
            "complete": self.complete,
            "missing_components": list(self.missing_components),
        }


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """A read-only route reference; no transaction request or signing capability is held."""

    verdict: str
    route_hash: str | None
    observed_at: datetime
    expires_at: datetime
    available_depth_usd_micros: int | None
    costs: CostLedger
    source: str = "LI.FI"

    def __post_init__(self) -> None:
        if self.verdict not in ("BID", "NO BID", "NO QUOTE"):
            raise PortfolioContractError("execution quote verdict must be BID, NO BID, or NO QUOTE")
        if self.verdict == "BID" and not self.route_hash:
            raise PortfolioContractError("a BID execution quote requires a bound route hash")
        if self.route_hash is not None and (
            not isinstance(self.route_hash, str)
            or not self.route_hash.startswith("0x")
            or len(self.route_hash) != 66
        ):
            raise PortfolioContractError("route_hash must be a 32-byte 0x-prefixed hash or null")
        require_utc(self.observed_at, field_name="ExecutionQuote.observed_at")
        require_utc(self.expires_at, field_name="ExecutionQuote.expires_at")
        if self.expires_at <= self.observed_at:
            raise PortfolioContractError("execution quote expiry must follow observation")
        if self.available_depth_usd_micros is not None and self.available_depth_usd_micros < 0:
            raise PortfolioContractError("available route depth cannot be negative")

    def is_live_at(self, as_of: datetime) -> bool:
        return require_utc(as_of) < require_utc(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "route_hash": self.route_hash,
            "observed_at": utc_iso(self.observed_at, field_name="ExecutionQuote.observed_at"),
            "expires_at": utc_iso(self.expires_at, field_name="ExecutionQuote.expires_at"),
            "available_depth_usd_micros": self.available_depth_usd_micros,
            "costs": self.costs.to_dict(),
            "source": self.source,
        }


def quote_from_route_binding(
    binding: RouteBinding,
    *,
    costs: CostLedger,
    available_depth_usd_micros: int | None,
    verdict: str = "BID",
    source: str = "LI.FI",
) -> ExecutionQuote:
    """Adapt the existing size-bound composer contract into this read-only contract."""

    return ExecutionQuote(
        verdict=verdict,
        route_hash=binding.route_hash,
        observed_at=datetime.fromtimestamp(binding.observed_at, tz=UTC),
        expires_at=datetime.fromtimestamp(binding.expires_at, tz=UTC),
        available_depth_usd_micros=available_depth_usd_micros,
        costs=costs,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class CapitalSource:
    identity: str
    kind: CapitalKind
    available_usd_micros: int
    selected_usd_micros: int
    verdict: str = "BID"
    explicit_variable_income_opt_in: bool = False
    variable_income_limit_usd_micros: int | None = None

    def __post_init__(self) -> None:
        if not self.identity:
            raise PortfolioContractError("capital source requires a stable identity")
        for name in ("available_usd_micros", "selected_usd_micros"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PortfolioContractError(f"{name} must be a non-negative integer USD-micro amount")
        if self.selected_usd_micros > self.available_usd_micros:
            raise PortfolioContractError("selected capital cannot exceed the source's known value")
        if self.verdict not in ("BID", "NO BID", "NO QUOTE"):
            raise PortfolioContractError("capital source verdict must be BID, NO BID, or NO QUOTE")
        if self.variable_income_limit_usd_micros is not None and (
            isinstance(self.variable_income_limit_usd_micros, bool)
            or not isinstance(self.variable_income_limit_usd_micros, int)
            or self.variable_income_limit_usd_micros < 0
        ):
            raise PortfolioContractError("variable-income limit must be a non-negative integer or null")


@dataclass(frozen=True, slots=True)
class Mandate:
    """Explicit constraints; an absent fact is never treated as permission."""

    allowed_issuers: frozenset[str] = frozenset()
    allowed_protocols: frozenset[str] = frozenset()
    allowed_chain_ids: frozenset[int] = frozenset()
    protocol_max_bps: int | None = None
    min_liquidity_usd_micros: int | None = None
    max_maturity_days: int | None = None
    required_credentials: frozenset[str] = frozenset()
    require_supported_permission: bool = True

    def __post_init__(self) -> None:
        for name, values in (
            ("allowed_issuers", self.allowed_issuers),
            ("allowed_protocols", self.allowed_protocols),
            ("required_credentials", self.required_credentials),
        ):
            if not isinstance(values, frozenset) or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise PortfolioContractError(f"{name} must contain non-empty strings")
        if not isinstance(self.allowed_chain_ids, frozenset) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.allowed_chain_ids
        ):
            raise PortfolioContractError("allowed_chain_ids must contain positive integers")
        if self.protocol_max_bps is not None and (
            isinstance(self.protocol_max_bps, bool)
            or not isinstance(self.protocol_max_bps, int)
            or not 0 <= self.protocol_max_bps <= BPS
        ):
            raise PortfolioContractError("protocol_max_bps must be in [0, 10000]")
        for name in ("min_liquidity_usd_micros", "max_maturity_days"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise PortfolioContractError(f"{name} must be a non-negative integer or null")
        if not isinstance(self.require_supported_permission, bool):
            raise PortfolioContractError("require_supported_permission must be boolean")


@dataclass(frozen=True, slots=True)
class MandateResult:
    status: MandateStatus
    reason_codes: tuple[ReasonCode, ...]
    protocol_concentration_bps: int | None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status is MandateStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_codes": [reason.value for reason in sorted(self.reason_codes, key=lambda item: item.value)],
            "protocol_concentration_bps": self.protocol_concentration_bps,
            "detail": self.detail,
        }


def evaluate_mandate(
    mandate: Mandate,
    *,
    protocol: str | None,
    chain_id: int | None,
    current_protocol_value_usd_micros: int | None,
    added_value_usd_micros: int,
    total_nav_usd_micros: int | None,
    permission: CapabilityResult | None,
    issuer: str | None = None,
    available_depth_usd_micros: int | None = None,
    maturity_days: int | None = None,
    credentials: frozenset[str] | None = None,
) -> MandateResult:
    """Evaluate identity, permission, concentration, liquidity and term constraints.

    A fact is required only when its corresponding constraint is active.  When it is
    required, an unknown value is an ``UNRESOLVED`` result and never a pass.
    """

    if (
        protocol in (None, "", "UNKNOWN")
        or chain_id is None
        or current_protocol_value_usd_micros is None
        or total_nav_usd_micros is None
        or total_nav_usd_micros <= 0
    ):
        return MandateResult(
            MandateStatus.UNRESOLVED,
            (ReasonCode.UNRESOLVED_POSITION,),
            None,
            "protocol, chain, concentration input, or total NAV is unknown",
        )
    if added_value_usd_micros < 0 or current_protocol_value_usd_micros < 0:
        raise PortfolioContractError("mandate values must be non-negative")
    if mandate.require_supported_permission and (
        permission is None or permission.status is not CapabilityStatus.SUPPORTED
    ):
        return MandateResult(
            MandateStatus.UNRESOLVED,
            (
                permission.reason_code
                if permission is not None
                else ReasonCode.PERMISSION_UNKNOWN,
            ),
            None,
            "permission evidence is not supported; unknown never passes",
        )
    if mandate.allowed_protocols and protocol not in mandate.allowed_protocols:
        return MandateResult(
            MandateStatus.REJECTED,
            (ReasonCode.PROTOCOL_NOT_ALLOWED,),
            None,
            "candidate protocol is not permitted by the mandate",
        )
    if mandate.allowed_chain_ids and chain_id not in mandate.allowed_chain_ids:
        return MandateResult(
            MandateStatus.REJECTED,
            (ReasonCode.CHAIN_NOT_ALLOWED,),
            None,
            "candidate chain is not permitted by the mandate",
        )
    if mandate.allowed_issuers:
        if issuer in (None, "", "UNKNOWN"):
            return MandateResult(
                MandateStatus.UNRESOLVED,
                (ReasonCode.UNRESOLVED_POSITION,),
                None,
                "issuer evidence is required by the mandate",
            )
        if issuer not in mandate.allowed_issuers:
            return MandateResult(
                MandateStatus.REJECTED,
                (ReasonCode.ISSUER_NOT_ALLOWED,),
                None,
                "candidate issuer is not permitted by the mandate",
            )
    if mandate.min_liquidity_usd_micros is not None:
        if available_depth_usd_micros is None:
            return MandateResult(
                MandateStatus.UNRESOLVED,
                (ReasonCode.MISSING_LIQUIDITY,),
                None,
                "depth at the requested size is required by the mandate",
            )
        if available_depth_usd_micros < mandate.min_liquidity_usd_micros:
            return MandateResult(
                MandateStatus.REJECTED,
                (ReasonCode.LIQUIDITY_FLOOR_FAIL,),
                None,
                "candidate depth is below the mandate liquidity floor",
            )
    if mandate.max_maturity_days is not None:
        if maturity_days is None:
            return MandateResult(
                MandateStatus.UNRESOLVED,
                (ReasonCode.METHODOLOGY_NOT_APPLICABLE,),
                None,
                "verified maturity is required by the mandate",
            )
        if maturity_days > mandate.max_maturity_days:
            return MandateResult(
                MandateStatus.REJECTED,
                (ReasonCode.MATURITY_LIMIT_FAIL,),
                None,
                "candidate maturity exceeds the mandate limit",
            )
    if mandate.required_credentials:
        if credentials is None:
            return MandateResult(
                MandateStatus.UNRESOLVED,
                (ReasonCode.PERMISSION_UNKNOWN,),
                None,
                "credential evidence is required by the mandate",
            )
        missing_credentials = mandate.required_credentials - credentials
        if missing_credentials:
            return MandateResult(
                MandateStatus.REJECTED,
                (ReasonCode.CREDENTIAL_REQUIREMENT_FAIL,),
                None,
                "candidate is missing one or more mandate credentials",
            )
    concentration = (
        (current_protocol_value_usd_micros + added_value_usd_micros) * BPS
    ) // total_nav_usd_micros
    if mandate.protocol_max_bps is not None and concentration > mandate.protocol_max_bps:
        return MandateResult(
            MandateStatus.REJECTED,
            (ReasonCode.PROTOCOL_CONCENTRATION_FAIL,),
            concentration,
            "candidate would exceed the protocol concentration limit",
        )
    return MandateResult(
        MandateStatus.PASS,
        (ReasonCode.MANDATE_PASS,),
        concentration,
    )


def validate_capital_source(source: CapitalSource) -> tuple[bool, ReasonCode | None]:
    """Stablecoin/cash capital is deployable; variable income needs a stated limit."""

    if source.verdict == "NO BID":
        return False, ReasonCode.NO_BID
    if source.verdict == "NO QUOTE":
        return False, ReasonCode.NO_QUOTE
    if source.selected_usd_micros <= 0:
        return False, ReasonCode.METHODOLOGY_NOT_APPLICABLE
    if source.kind in (CapitalKind.STABLECOIN, CapitalKind.CASH):
        return True, None
    if not source.explicit_variable_income_opt_in:
        return False, ReasonCode.VARIABLE_INCOME_OPT_IN_REQUIRED
    if (
        source.variable_income_limit_usd_micros is None
        or source.selected_usd_micros > source.variable_income_limit_usd_micros
    ):
        return False, ReasonCode.VARIABLE_INCOME_LIMIT_EXCEEDED
    return True, None


@dataclass(frozen=True, slots=True)
class PortfolioTransformation:
    """A single source-to-destination scenario, never an automatic sell instruction."""

    identity: str
    source: CapitalSource
    destination_identity: str
    destination_verdict: str
    quote: ExecutionQuote | None
    before_nav_usd_micros: int | None
    after_nav_usd_micros: int | None
    as_of: datetime

    def __post_init__(self) -> None:
        if not self.identity or not self.destination_identity:
            raise PortfolioContractError("transformation identities are required")
        if self.destination_verdict not in ("BID", "NO BID", "NO QUOTE"):
            raise PortfolioContractError("destination verdict must be BID, NO BID, or NO QUOTE")
        require_utc(self.as_of, field_name="PortfolioTransformation.as_of")
        for name in ("before_nav_usd_micros", "after_nav_usd_micros"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise PortfolioContractError(f"{name} must be a non-negative integer USD-micro amount or null")


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    transformation: PortfolioTransformation
    status: OpportunityStatus
    reason_codes: tuple[ReasonCode, ...]
    cost_ledger: CostLedger | None

    def to_dict(self) -> dict[str, Any]:
        transformation = self.transformation
        return {
            "identity": transformation.identity,
            "source_identity": transformation.source.identity,
            "destination_identity": transformation.destination_identity,
            "status": self.status.value,
            "reason_codes": [reason.value for reason in sorted(self.reason_codes, key=lambda item: item.value)],
            "before_nav_usd_micros": transformation.before_nav_usd_micros,
            "after_nav_usd_micros": transformation.after_nav_usd_micros,
            "cost_ledger": None if self.cost_ledger is None else self.cost_ledger.to_dict(),
        }


def evaluate_scenario(transformation: PortfolioTransformation) -> ScenarioResult:
    """Apply hard source/destination gates before calling a scenario executable."""

    source_ok, source_reason = validate_capital_source(transformation.source)
    if not source_ok:
        return ScenarioResult(
            transformation,
            OpportunityStatus.REJECTED,
            (source_reason or ReasonCode.UNRESOLVED_POSITION,),
            transformation.quote.costs if transformation.quote else None,
        )
    if transformation.destination_verdict == "NO BID":
        return ScenarioResult(
            transformation,
            OpportunityStatus.REJECTED,
            (ReasonCode.NO_BID,),
            transformation.quote.costs if transformation.quote else None,
        )
    if transformation.destination_verdict == "NO QUOTE":
        return ScenarioResult(
            transformation,
            OpportunityStatus.REJECTED,
            (ReasonCode.NO_QUOTE,),
            transformation.quote.costs if transformation.quote else None,
        )
    if transformation.quote is None:
        return ScenarioResult(
            transformation,
            OpportunityStatus.ANALYTICAL_ONLY,
            (ReasonCode.MISSING_EXECUTION_QUOTE,),
            None,
        )
    if transformation.quote.verdict == "NO BID":
        return ScenarioResult(
            transformation,
            OpportunityStatus.REJECTED,
            (ReasonCode.NO_BID,),
            transformation.quote.costs,
        )
    if transformation.quote.verdict == "NO QUOTE":
        return ScenarioResult(
            transformation,
            OpportunityStatus.REJECTED,
            (ReasonCode.NO_QUOTE,),
            transformation.quote.costs,
        )
    if not transformation.quote.is_live_at(transformation.as_of):
        return ScenarioResult(
            transformation,
            OpportunityStatus.ANALYTICAL_ONLY,
            (ReasonCode.QUOTE_EXPIRED,),
            transformation.quote.costs,
        )
    if transformation.quote.available_depth_usd_micros is None:
        return ScenarioResult(
            transformation,
            OpportunityStatus.ANALYTICAL_ONLY,
            (ReasonCode.MISSING_LIQUIDITY,),
            transformation.quote.costs,
        )
    if transformation.quote.available_depth_usd_micros < transformation.source.selected_usd_micros:
        return ScenarioResult(
            transformation,
            OpportunityStatus.REJECTED,
            (ReasonCode.MISSING_LIQUIDITY,),
            transformation.quote.costs,
        )
    if not transformation.quote.costs.complete:
        return ScenarioResult(
            transformation,
            OpportunityStatus.ANALYTICAL_ONLY,
            (ReasonCode.MISSING_COST,),
            transformation.quote.costs,
        )
    return ScenarioResult(
        transformation,
        OpportunityStatus.ELIGIBLE,
        (ReasonCode.SUFFICIENT_DEPTH,),
        transformation.quote.costs,
    )


def metrics_comparable(before: MetricResult | None, after: MetricResult | None) -> bool:
    """Require the same measured methodology and coverage basis for an ES delta."""

    if before is None or after is None:
        return False
    if before.status is not MetricStatus.COMPUTED or after.status is not MetricStatus.COMPUTED:
        return False
    if not isinstance(before.value, int) or not isinstance(after.value, int):
        return False
    return (
        before.unit == after.unit
        and before.method_version == after.method_version
        and before.window == after.window
        and before.frequency == after.frequency
        and before.observations == after.observations
        and before.coverage_bps == after.coverage_bps
    )


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    """Facts required to rank a scenario under a user-selected objective."""

    identity: str
    objective: Objective
    scenario: ScenarioResult
    mandate: MandateResult
    primary_objective_delta: int | None
    net_executable_income_usd_micros: int | None
    liquidity_bps: int | None
    evidence_age_seconds: int | None
    before_expected_shortfall: MetricResult | None = None
    after_expected_shortfall: MetricResult | None = None

    def __post_init__(self) -> None:
        if not self.identity:
            raise PortfolioContractError("opportunity candidate requires a stable identity")
        if self.liquidity_bps is not None and not 0 <= self.liquidity_bps <= BPS:
            raise PortfolioContractError("liquidity_bps must be in [0, 10000]")
        if self.evidence_age_seconds is not None and self.evidence_age_seconds < 0:
            raise PortfolioContractError("evidence_age_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class OpportunityEvaluation:
    candidate: OpportunityCandidate
    status: OpportunityStatus
    reason_codes: tuple[ReasonCode, ...]
    rank_tuple: tuple[Any, ...] | None

    @property
    def identity(self) -> str:
        return self.candidate.identity

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "objective": self.candidate.objective.value,
            "status": self.status.value,
            "reason_codes": [reason.value for reason in sorted(self.reason_codes, key=lambda item: item.value)],
            "rank_tuple": None if self.rank_tuple is None else list(self.rank_tuple),
        }


def evaluate_opportunity(candidate: OpportunityCandidate) -> OpportunityEvaluation:
    """Apply hard gates and construct the transparent lexicographic rank tuple."""

    reasons = set(candidate.scenario.reason_codes) | set(candidate.mandate.reason_codes)
    if candidate.scenario.status is OpportunityStatus.REJECTED:
        return OpportunityEvaluation(
            candidate, OpportunityStatus.REJECTED, tuple(sorted(reasons, key=lambda item: item.value)), None
        )
    if not candidate.mandate.passed:
        return OpportunityEvaluation(
            candidate, OpportunityStatus.REJECTED, tuple(sorted(reasons, key=lambda item: item.value)), None
        )

    primary = candidate.primary_objective_delta
    if candidate.objective is Objective.REDUCE_RISK:
        before_es = candidate.before_expected_shortfall
        after_es = candidate.after_expected_shortfall
        if not metrics_comparable(before_es, after_es):
            reasons.add(ReasonCode.INSUFFICIENT_HISTORY)
            return OpportunityEvaluation(
                candidate,
                OpportunityStatus.NOT_RANKABLE,
                tuple(sorted(reasons, key=lambda item: item.value)),
                None,
            )
        if before_es is None or after_es is None or before_es.value is None or after_es.value is None:
            reasons.add(ReasonCode.INSUFFICIENT_HISTORY)
            return OpportunityEvaluation(
                candidate,
                OpportunityStatus.NOT_RANKABLE,
                tuple(sorted(reasons, key=lambda item: item.value)),
                None,
            )
        primary = int(before_es.value) - int(after_es.value)
        reasons.add(ReasonCode.ES_REDUCTION)
    if primary is None:
        return OpportunityEvaluation(
            candidate,
            OpportunityStatus.NOT_RANKABLE,
            tuple(sorted(reasons, key=lambda item: item.value)),
            None,
        )
    if candidate.net_executable_income_usd_micros is None:
        return OpportunityEvaluation(
            candidate,
            OpportunityStatus.NOT_RANKABLE,
            tuple(sorted(reasons, key=lambda item: item.value)),
            None,
        )
    if candidate.objective is not Objective.REDUCE_RISK:
        reasons.add(ReasonCode.INCOME_INCREASE)

    status = candidate.scenario.status
    # Rank only reproducible numerical facts.  Higher objective delta, income, and
    # liquidity win; lower evidence age wins; stable identity resolves every tie.
    rank_tuple = (
        0 if status is OpportunityStatus.ELIGIBLE else 1,
        -primary,
        -candidate.net_executable_income_usd_micros,
        -(candidate.liquidity_bps if candidate.liquidity_bps is not None else -1),
        candidate.evidence_age_seconds if candidate.evidence_age_seconds is not None else 2**63 - 1,
        candidate.identity,
    )
    return OpportunityEvaluation(
        candidate,
        status,
        tuple(sorted(reasons, key=lambda item: item.value)),
        rank_tuple,
    )


@dataclass(frozen=True, slots=True)
class OpportunitySet:
    objective: Objective
    opportunities: tuple[OpportunityEvaluation, ...]
    method_version: str
    as_of: datetime
    schema_version: str = "fenorium/opportunity-set/v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="OpportunitySet.as_of"),
            "objective": self.objective.value,
            "opportunities": [row.to_dict() for row in self.opportunities],
        }


def rank_opportunities(
    candidates: Iterable[OpportunityCandidate],
    *,
    objective: Objective,
    method_version: str,
    as_of: datetime,
) -> OpportunitySet:
    """Return a byte-stable ordering with the stable identity as final tie-breaker."""

    require_utc(as_of, field_name="rank_opportunities.as_of")
    candidate_rows = tuple(candidates)
    identities = [row.identity for row in candidate_rows]
    if len(identities) != len(set(identities)):
        raise PortfolioContractError("opportunity candidate identities must be unique")
    wrong_objective = [row.identity for row in candidate_rows if row.objective is not objective]
    if wrong_objective:
        raise PortfolioContractError(
            f"cannot mix objectives in one rank: {', '.join(sorted(wrong_objective))}"
        )
    evaluated = [evaluate_opportunity(candidate) for candidate in candidate_rows]
    evaluated.sort(
        key=lambda row: row.rank_tuple
        if row.rank_tuple is not None
        else (2, 0, 0, 0, 0, row.identity)
    )
    return OpportunitySet(
        objective=objective,
        opportunities=tuple(evaluated),
        method_version=method_version,
        as_of=as_of,
    )


scenario = evaluate_scenario
opportunity_ranking = rank_opportunities
