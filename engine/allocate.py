from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from .credit import CreditDecision, CreditInputs, score_credit
from .gas import GasMarket, Operation, annual_fixed_cost_usd, effective_hurdle
from .models import Job


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    instrument: str
    underlying: str
    venue: str
    job: Job
    fixed_rate: float
    floating_reference: float
    credit_penalty: float
    expected_net_return: float
    cvar95: float
    illiquidity_score: float
    executable_capacity_usd: float
    weight: float = 0.0
    evidence: tuple[str, ...] = ()
    chain: str = "Ethereum"
    cvar_kind: str = "measured"
    cvar_detail: str = "CVaR95 supplied by caller"

    def __post_init__(self) -> None:
        values = (self.fixed_rate, self.floating_reference, self.credit_penalty,
                  self.expected_net_return, self.cvar95, self.illiquidity_score,
                  self.executable_capacity_usd, self.weight)
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"{self.candidate_id}: candidate values must be finite")
        if self.executable_capacity_usd < 0 or not 0 <= self.illiquidity_score <= 1 or self.weight < 0:
            raise ValueError(f"{self.candidate_id}: invalid capacity, illiquidity, or weight")
        if self.cvar_kind not in {"measured", "fallback"}:
            raise ValueError(f"{self.candidate_id}: cvar_kind must be measured or fallback")


@dataclass(frozen=True)
class Veto:
    candidate_id: str
    reason: str
    sensor: str


class Sensor(Protocol):
    def veto(self, candidate: Candidate) -> Veto | None: ...


@dataclass(frozen=True)
class ReserveVenue:
    candidate_id: str
    instrument: str
    underlying: str
    protocol: str
    chain: str
    rate_total: float
    rate_base: float | None
    rate_reward: float | None
    tvl_usd: float | None
    redeem_steps: str
    instant_exit: bool
    source_timestamp: datetime
    source_endpoint: str
    credit_inputs: CreditInputs | None

    def __post_init__(self) -> None:
        rates = tuple(value for value in (self.rate_total, self.rate_base, self.rate_reward) if value is not None)
        if not all(math.isfinite(value) for value in rates):
            raise ValueError(f"{self.candidate_id}: reserve rates must be finite")
        if self.tvl_usd is not None and (not math.isfinite(self.tvl_usd) or self.tvl_usd < 0):
            raise ValueError(f"{self.candidate_id}: reserve TVL is invalid")


@dataclass(frozen=True)
class ReserveEvaluation:
    venue: ReserveVenue
    decision: CreditDecision | None
    reason: str


@dataclass(frozen=True)
class ReserveSelection:
    venue: ReserveVenue
    aave_floor_rate: float
    aave_total_rate: float
    evaluations: tuple[ReserveEvaluation, ...]
    fallback_reason: str | None
    trace: str
    aave_tvl_usd: float | None
    aave_source_timestamp: datetime

    @property
    def rate(self) -> float:
        return self.venue.rate_total


@dataclass(frozen=True)
class ReserveAllocation:
    candidate_id: str
    instrument: str
    protocol: str
    chain: str
    weight: float
    amount_usd: float
    rate_total: float
    exit_type: str
    tvl_usd: float | None
    trace: str


def _aave_venue(total_rate: float, *, source_timestamp: datetime, tvl_usd: float | None = None) -> ReserveVenue:
    return ReserveVenue(
        candidate_id="aave-v3:ethereum:usdc",
        instrument="Aave v3 USDC",
        underlying="USDC",
        protocol="aave-v3",
        chain="Ethereum",
        rate_total=total_rate,
        rate_base=None,
        rate_reward=None,
        tvl_usd=tvl_usd,
        redeem_steps="instant",
        instant_exit=True,
        source_timestamp=source_timestamp,
        source_endpoint="DefiLlama yields / Aave v3 USDC",
        credit_inputs=None,
    )


def select_reserve_venue(
    venues: Sequence[ReserveVenue], *, as_of: datetime, aave_floor_rate: float,
    aave_total_rate: float, aave_tvl_usd: float | None = None,
    aave_source_timestamp: datetime | None = None,
    min_tvl_usd: float = 10_000_000.0,
    aave_by_chain: dict[str, float] | None = None,
) -> ReserveSelection:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if not all(math.isfinite(value) and value >= 0 for value in (aave_floor_rate, aave_total_rate)):
        raise ValueError("Aave rates must be finite and non-negative")
    evaluations: list[ReserveEvaluation] = []
    approved: list[ReserveVenue] = []
    for venue in venues:
        if venue.underlying != "USDC":
            evaluations.append(ReserveEvaluation(venue, None, "not USDC"))
            continue
        if not venue.instant_exit or venue.redeem_steps != "instant":
            evaluations.append(ReserveEvaluation(
                venue, None, f"staged exit ({venue.redeem_steps}); not a cash equivalent"
            ))
            continue
        if venue.tvl_usd is None or venue.tvl_usd < min_tvl_usd:
            observed = "COULD NOT VERIFY" if venue.tvl_usd is None else f"${venue.tvl_usd:,.0f}"
            evaluations.append(ReserveEvaluation(
                venue, None, f"TVL {observed} is below the measured ${min_tvl_usd:,.0f} reserve floor"
            ))
            continue
        if venue.credit_inputs is None:
            evaluations.append(ReserveEvaluation(venue, None, "COULD NOT VERIFY credit inputs"))
            continue
        decision = score_credit(venue.credit_inputs, as_of=as_of)
        evaluations.append(ReserveEvaluation(venue, decision, decision.reason))
        if decision.bid:
            approved.append(venue)

    aave_pulled_at = aave_source_timestamp or as_of
    aave = _aave_venue(aave_total_rate, source_timestamp=aave_pulled_at, tvl_usd=aave_tvl_usd)
    if not approved:
        reason = "no instant-exit LI.FI USDC venue passed engine.credit.score_credit"
        trace = (
            f"RESERVE FALLBACK — Aave v3 USDC at {aave_total_rate:.2%} total / "
            f"{aave_floor_rate:.2%} base floor, instant exit, TVL "
            f"{'COULD NOT VERIFY' if aave_tvl_usd is None else f'${aave_tvl_usd:,.0f}'}; {reason}"
        )
        return ReserveSelection(
            aave, aave_floor_rate, aave_total_rate, tuple(evaluations), reason, trace,
            aave_tvl_usd, aave_pulled_at,
        )

    # Clear the Aave floor on rate, then take the deepest — deliberately NOT max-yield.
    #
    # Two rules, both load-bearing. First, a curated vault carries curator and collateral risk that
    # Aave does not, so accepting *less* than Aave to hold it is paying for risk instead of being
    # paid for it; anything below the Aave total is excluded. Second, LI.FI exposes no per-vault
    # curator, collateral or concentration evidence, so every vault on one protocol scores on that
    # protocol's TVL and comes back identically TIER_A — ranking indistinguishable candidates by
    # yield is not credit judgement, it is reaching. The reserve's job is to be money, so among
    # those that clear the floor it is ranked by the depth it can actually be recalled from.
    # The floor is chain-specific. Aave USDC pays 3.289% on Ethereum and 3.520% on Base today, so
    # admitting a Base venue on Ethereum's lower floor would accept curated-vault risk for less
    # than the same chain's passive alternative.
    by_chain = aave_by_chain or {}
    floor_for = lambda venue: by_chain.get(venue.chain, aave_total_rate)  # noqa: E731
    clears_floor = [venue for venue in approved if venue.rate_total >= floor_for(venue)]
    below_floor = [venue for venue in approved if venue.rate_total < floor_for(venue)]
    if not clears_floor:
        reason = (
            f"no credit-approved instant-exit venue cleared the Aave total of {aave_total_rate:.2%}; "
            f"{len(below_floor)} passed credit but paid less than Aave"
        )
        trace = (
            f"RESERVE FALLBACK — Aave v3 USDC at {aave_total_rate:.2%} total / "
            f"{aave_floor_rate:.2%} base floor, instant exit, TVL "
            f"{'COULD NOT VERIFY' if aave_tvl_usd is None else f'${aave_tvl_usd:,.0f}'}; {reason}"
        )
        return ReserveSelection(
            aave, aave_floor_rate, aave_total_rate, tuple(evaluations), reason, trace,
            aave_tvl_usd, aave_pulled_at,
        )

    chosen = max(clears_floor, key=lambda venue: (venue.tvl_usd or 0.0, venue.rate_total, venue.candidate_id))
    ranked_others = sorted(
        (venue for venue in clears_floor if venue.candidate_id != chosen.candidate_id),
        key=lambda venue: (-(venue.tvl_usd or 0.0), venue.candidate_id),
    )
    beaten = [
        f"{venue.instrument} ({venue.rate_total:.2%} on "
        f"{'COULD NOT VERIFY' if venue.tvl_usd is None else f'${venue.tvl_usd:,.0f}'})"
        for venue in ranked_others[:3]
    ]
    if len(ranked_others) > 3:
        beaten.append(f"{len(ranked_others) - 3} other venues clearing the floor")
    if below_floor:
        beaten.append(f"{len(below_floor)} credit-approved venues excluded for paying under Aave")
    if not beaten:
        beaten = ["no other venue cleared the floor"]
    trace = (
        f"RESERVE CHOICE — {chosen.instrument} ({chosen.protocol}, {chosen.chain}) at "
        f"{chosen.rate_total:.2%} total, base "
        f"{'COULD NOT VERIFY' if chosen.rate_base is None else f'{chosen.rate_base:.2%}'}, "
        f"reward {'COULD NOT VERIFY' if chosen.rate_reward is None else f'{chosen.rate_reward:.2%}'}, "
        f"{chosen.redeem_steps} exit, TVL "
        f"{'COULD NOT VERIFY' if chosen.tvl_usd is None else f'${chosen.tvl_usd:,.0f}'}; "
        f"deepest of those clearing the Aave floor, ahead of {', '.join(beaten)}; "
        f"Aave floor on {chosen.chain} {floor_for(chosen):.2%} "
        f"(Ethereum reference {aave_floor_rate:.2%} base / {aave_total_rate:.2%} total); "
        f"ranked on depth, not yield — LI.FI exposes no per-vault curator or collateral evidence, "
        f"so credit scores here cannot separate one curated vault from another"
    )
    return ReserveSelection(
        chosen, aave_floor_rate, aave_total_rate, tuple(evaluations), None, trace,
        aave_tvl_usd, aave_pulled_at,
    )


def apply_sensor_vetoes(candidates: Sequence[Candidate], sensors: Sequence[Sensor]) -> tuple[tuple[Candidate, ...], tuple[Veto, ...]]:
    """The only sensor gateway: sensors can remove risk, never create or increase it."""
    before = tuple(candidates)
    before_ids = {candidate.candidate_id for candidate in before}
    if len(before_ids) != len(before):
        raise AssertionError("pre-sensor candidate ids must be unique")
    before_weights = {candidate.candidate_id: candidate.weight for candidate in before}
    vetoes: list[Veto] = []
    removed: set[str] = set()
    for candidate in before:
        for sensor in sensors:
            result = sensor.veto(candidate)
            if result is not None and not isinstance(result, Veto):
                raise TypeError(f"sensor {type(sensor).__name__} returned {type(result).__name__}; only Veto or None is permitted")
            if result is not None:
                if result.candidate_id != candidate.candidate_id:
                    raise AssertionError("sensor veto targeted a candidate other than the one inspected")
                removed.add(candidate.candidate_id)
                vetoes.append(result)
                break
    after = tuple(candidate for candidate in before if candidate.candidate_id not in removed)
    after_ids = {candidate.candidate_id for candidate in after}
    # Raised, not asserted: `python -O` strips assert, and this is the guarantee that a safety
    # sensor can never originate or enlarge a position. It must hold in every build.
    if not after_ids.issubset(before_ids):
        raise AssertionError("sensor candidate set expanded")
    if any(candidate.weight > before_weights[candidate.candidate_id] for candidate in after):
        raise AssertionError("sensor increased a candidate weight")
    return after, tuple(vetoes)


@dataclass(frozen=True)
class AllocationConfig:
    lambda_cvar: float = 1.0  # one-for-one tail-loss charge keeps credit loss visible
    mu_illiquidity: float = 0.01  # up to 100bp charge for a fully illiquid position
    nu_concentration: float = 0.02  # convex charge discourages filling one asset first
    min_reserve_weight: float = 0.50  # waiting is the default when fixed edge is absent
    per_asset_cap: float = 0.20  # one underlying cannot dominate the book
    reserve_venue_cap: float = 0.25  # callable reserve venues remain concentration-limited
    job_caps: dict[Job, float] = field(default_factory=lambda: {
        Job.LEND_FIXED: 0.25, Job.DISPERSION: 0.20, Job.CARRY: 0.15,
    })
    carry_edge_threshold: float = 0.005  # require 50bp after floating and credit
    step_weight: float = 0.005  # 50bp grid gives deterministic convergence

    def __post_init__(self) -> None:
        if not 0 <= self.min_reserve_weight <= 1 or self.step_weight <= 0:
            raise ValueError("reserve and step weights are invalid")
        if not 0 < self.per_asset_cap <= 1 or not 0 < self.reserve_venue_cap <= 1:
            raise ValueError("asset and reserve venue caps must be in (0,1]")


@dataclass(frozen=True)
class Allocation:
    candidate_id: str
    instrument: str
    job: Job
    weight: float
    amount_usd: float
    objective_contribution: float
    trace: str


@dataclass(frozen=True)
class AllocationResult:
    allocations: tuple[Allocation, ...]
    reserve_weight: float
    reserve_amount_usd: float
    objective: float
    rejected: tuple[str, ...]
    iterations: int
    convergence: str
    reserve_selection: ReserveSelection | None = None
    reserve_allocations: tuple[ReserveAllocation, ...] = ()


_JOB_RANK = {Job.LEND_FIXED: 0, Job.DISPERSION: 1, Job.CARRY: 2}


def _base_utility(candidate: Candidate, config: AllocationConfig) -> float:
    return (candidate.expected_net_return - config.lambda_cvar * candidate.cvar95
            - config.mu_illiquidity * candidate.illiquidity_score)


def _reserve_sleeves(selection: ReserveSelection, reserve_weight: float, portfolio_usd: float,
                     cap: float) -> tuple[ReserveAllocation, ...]:
    remaining = reserve_weight
    sleeves: list[ReserveAllocation] = []
    # Deepest first, not highest-yielding first — the same rule select_reserve_venue() uses to pick
    # the hurdle, so the money goes where the stated policy says it goes. LI.FI exposes no per-vault
    # curator or collateral evidence, so credit cannot separate one curated vault from another;
    # reaching for the top rate under that blindness is buying an unexplained premium, which is the
    # exact behaviour this engine exists to refuse. Depth is the one risk proxy actually measured.
    # Must also clear the Aave total, exactly as select_reserve_venue() requires of the hurdle.
    # Without this the depth ranking happily deploys a 3.08% vault while Aave pays 4.75%, and the
    # sleeve trace would claim it "beat Aave" when it did not.
    approved = sorted(
        (evaluation.venue for evaluation in selection.evaluations
         if evaluation.decision is not None and evaluation.decision.bid
         and evaluation.venue.instant_exit
         and evaluation.venue.rate_total > selection.aave_total_rate),
        key=lambda venue: (-(venue.tvl_usd or 0.0), -venue.rate_total, venue.candidate_id),
    )
    for venue in approved:
        weight = min(cap, remaining)
        if weight <= 1e-12:
            break
        trace = (
            f"RESERVE {venue.instrument}: weight {weight:.3f}, rate {venue.rate_total:.2%}, "
            f"exit {venue.redeem_steps}, TVL "
            f"{'COULD NOT VERIFY' if venue.tvl_usd is None else f'${venue.tvl_usd:,.0f}'}; "
            f"beat Aave total {selection.aave_total_rate:.2%}; per-venue cap {cap:.3f}"
        )
        sleeves.append(ReserveAllocation(
            venue.candidate_id, venue.instrument, venue.protocol, venue.chain, weight,
            weight * portfolio_usd, venue.rate_total, venue.redeem_steps, venue.tvl_usd, trace,
        ))
        remaining -= weight

    aave_weight = min(cap, remaining)
    if aave_weight > 1e-12:
        aave = _aave_venue(
            selection.aave_total_rate, source_timestamp=selection.aave_source_timestamp,
            tvl_usd=selection.aave_tvl_usd,
        )
        role = "named conservative fallback" if selection.fallback_reason else "fallback below the selected LI.FI hurdle"
        sleeves.append(ReserveAllocation(
            aave.candidate_id, aave.instrument, aave.protocol, aave.chain, aave_weight,
            aave_weight * portfolio_usd, aave.rate_total, aave.redeem_steps, aave.tvl_usd,
            f"RESERVE Aave v3 USDC: weight {aave_weight:.3f}, total {selection.aave_total_rate:.2%}, "
            f"base floor {selection.aave_floor_rate:.2%}, instant exit, TVL "
            f"{'COULD NOT VERIFY' if aave.tvl_usd is None else f'${aave.tvl_usd:,.0f}'}; "
            f"{role}; per-venue cap {cap:.3f}",
        ))
        remaining -= aave_weight
    if remaining > 1e-12:
        sleeves.append(ReserveAllocation(
            "idle-usdc", "Idle USDC", "cash", "Ethereum", remaining,
            remaining * portfolio_usd, 0.0, "wallet", None,
            f"RESERVE Idle USDC: weight {remaining:.3f}; no additional underwritten venue was available "
            f"inside the {cap:.3f} per-venue cap",
        ))
    return tuple(sleeves)


def allocate(candidates: Sequence[Candidate], *, portfolio_usd: float, reserve_rate: float | None = None,
             reserve_selection: ReserveSelection | None = None,
             gas_markets: Mapping[str, GasMarket] | None = None,
             ops_per_year: Mapping[Operation | str, int] | None = None,
             config: AllocationConfig = AllocationConfig()) -> AllocationResult:
    if reserve_selection is not None:
        if reserve_rate is not None and not math.isclose(reserve_rate, reserve_selection.rate):
            raise ValueError("reserve_rate conflicts with selected reserve venue")
        reserve_rate = reserve_selection.rate
    if reserve_rate is None:
        raise ValueError("reserve_rate or reserve_selection is required")
    if portfolio_usd <= 0 or not math.isfinite(portfolio_usd) or not math.isfinite(reserve_rate):
        raise ValueError("portfolio_usd must be positive and rates finite")
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate ids must be unique")
    weights = {candidate.candidate_id: 0.0 for candidate in candidates}
    asset_weights: dict[str, float] = {}
    job_weights: dict[Job, float] = {job: 0.0 for job in config.job_caps}
    reserve = 1.0
    rejected: list[str] = []
    eligible: list[Candidate] = []
    hurdles: dict[str, float] = {}
    annual_gas_costs: dict[str, float] = {}
    for candidate in candidates:
        if gas_markets is None:
            hurdle = reserve_rate
            annual_gas_cost = 0.0
        elif ops_per_year is None:
            rejected.append(
                f"{candidate.candidate_id}: COULD NOT VERIFY effective hurdle — missing ops_per_year"
            )
            continue
        else:
            try:
                annual_gas_cost = annual_fixed_cost_usd(
                    candidate.chain, ops_per_year, markets=gas_markets,
                )
                hurdle = effective_hurdle(
                    candidate.chain, portfolio_usd, ops_per_year,
                    reserve_apy=reserve_rate, markets=gas_markets,
                )
            except ValueError as exc:
                rejected.append(f"{candidate.candidate_id}: {exc}")
                continue
        hurdles[candidate.candidate_id] = hurdle
        annual_gas_costs[candidate.candidate_id] = annual_gas_cost
        carry_edge = candidate.fixed_rate - hurdle - candidate.credit_penalty
        if candidate.executable_capacity_usd <= 0:
            rejected.append(f"{candidate.candidate_id}: executable capacity unavailable or zero")
        elif candidate.job is Job.CARRY and carry_edge <= config.carry_edge_threshold:
            hurdle_text = (
                f"selected reserve {reserve_rate * 100:.2f}% wins" if gas_markets is None else
                f"selected reserve {reserve_rate * 100:.2f}% / {candidate.chain} effective hurdle "
                f"{hurdle * 100:.2f}% wins"
            )
            rejected.append(
                f"{candidate.candidate_id}: CARRY edge {carry_edge * 100:.2f}% <= "
                f"{config.carry_edge_threshold * 100:.2f}% threshold; {hurdle_text}"
            )
        else:
            eligible.append(candidate)

    iterations = 0
    last_marginals: dict[str, float] = {}
    while reserve - config.step_weight >= config.min_reserve_weight - 1e-12:
        choices: list[tuple[float, int, str, Candidate, float]] = []
        for candidate in eligible:
            current = weights[candidate.candidate_id]
            capacity_weight = candidate.executable_capacity_usd / portfolio_usd
            room = min(capacity_weight - current,
                       config.per_asset_cap - asset_weights.get(candidate.underlying, 0.0),
                       config.job_caps[candidate.job] - job_weights[candidate.job],
                       reserve - config.min_reserve_weight)
            increment = min(config.step_weight, max(0.0, room))
            if increment <= 1e-12:
                continue
            concentration_before = asset_weights.get(candidate.underlying, 0.0) ** 2
            concentration_after = (asset_weights.get(candidate.underlying, 0.0) + increment) ** 2
            marginal = (_base_utility(candidate, config) - hurdles[candidate.candidate_id]) * increment \
                - config.nu_concentration * (concentration_after - concentration_before)
            last_marginals[candidate.candidate_id] = marginal
            choices.append((marginal, -_JOB_RANK[candidate.job], candidate.candidate_id, candidate, increment))
        if not choices:
            break
        marginal, _rank, _identifier, best, increment = max(choices, key=lambda row: (row[0], row[1], row[2]))
        if marginal <= 0:
            break
        weights[best.candidate_id] += increment
        asset_weights[best.underlying] = asset_weights.get(best.underlying, 0.0) + increment
        job_weights[best.job] += increment
        reserve -= increment
        iterations += 1
        if iterations > math.ceil(1.0 / config.step_weight) + len(candidates):
            raise AssertionError("allocator failed its deterministic iteration bound")

    allocations: list[Allocation] = []
    for candidate in eligible:
        weight = weights[candidate.candidate_id]
        if weight <= 1e-12:
            rejected.append(
                f"{candidate.candidate_id}: objective did not beat selected reserve; "
                f"net {candidate.expected_net_return * 100:.2f}%, CVaR95 {candidate.cvar95 * 100:.2f}% "
                f"({candidate.cvar_kind}), {candidate.chain} effective hurdle "
                f"{hurdles[candidate.candidate_id] * 100:.2f}%"
            )
            continue
        capacity_weight = candidate.executable_capacity_usd / portfolio_usd
        bounds = {
            "executable capacity": capacity_weight,
            "per-asset cap": config.per_asset_cap,
            f"{candidate.job.value} cap": config.job_caps[candidate.job],
            "reserve floor": 1.0 - config.min_reserve_weight,
        }
        bound = min(bounds, key=lambda name: abs(bounds[name] - weight))
        contribution = weight * _base_utility(candidate, config) \
            - config.nu_concentration * weight * weight
        alternatives = [row for row in rejected if not row.startswith(candidate.candidate_id)]
        trace = (
            f"{candidate.job.value} {candidate.instrument}: weight {weight:.3f}, fixed {candidate.fixed_rate * 100:.2f}%, "
            f"reserve {reserve_rate * 100:.2f}% + {candidate.chain} annual gas "
            f"${annual_gas_costs[candidate.candidate_id]:,.2f}/{portfolio_usd:,.0f} = effective hurdle "
            f"{hurdles[candidate.candidate_id] * 100:.2f}%, credit penalty {candidate.credit_penalty * 100:.2f}%, "
            f"CVaR95 {candidate.cvar95 * 100:.2f}% ({candidate.cvar_kind}: {candidate.cvar_detail}), "
            f"capacity ${candidate.executable_capacity_usd:,.0f}; "
            f"nearest binding constraint: {bound}={bounds[bound]:.3f}; "
            f"alternatives rejected: {' | '.join(alternatives) if alternatives else 'none at decision time'}"
        )
        allocations.append(Allocation(candidate.candidate_id, candidate.instrument, candidate.job,
                                      round(weight, 12), weight * portfolio_usd, contribution, trace))
    if reserve_selection is None:
        pulled_at = datetime.now().astimezone()
        reserve_selection = ReserveSelection(
            _aave_venue(reserve_rate, source_timestamp=pulled_at),
            reserve_rate, reserve_rate, (), "legacy explicit reserve rate", "Aave reserve rate supplied directly",
            None, pulled_at,
        )
    reserve_allocations = _reserve_sleeves(reserve_selection, reserve, portfolio_usd, config.reserve_venue_cap)
    objective = sum(sleeve.weight * sleeve.rate_total for sleeve in reserve_allocations) \
        + sum(allocation.objective_contribution for allocation in allocations)
    return AllocationResult(
        tuple(allocations), round(reserve, 12), reserve * portfolio_usd, objective,
        tuple(rejected), iterations,
        f"deterministic {config.step_weight:.3%} marginal grid; stopped when no feasible positive marginal remained",
        reserve_selection, reserve_allocations,
    )
