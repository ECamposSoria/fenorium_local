"""Strict two-source valuation and coverage-aware NAV construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .classification import ClassifiedPosition, classify_position
from .discovery import WalletSnapshot
from .contracts import (
    BPS,
    PRICE_SCALE,
    USD_MICROS,
    CapabilityStatus,
    PortfolioContractError,
    ReasonCode,
    require_utc,
    utc_iso,
)


# Which source values a holding when several are fresh: the first available in this order, then any
# other source by name. One fresh observation is enough (owner rule, 2026-10-01); the others are
# there as fallback for when the preferred one is missing or stale, not to agree with it.
# The testnet demo publishes its declared sale price before its redemption price (0.5% higher);
# the sale price is what the demo contract sells at, so it marks the holding.
# A catalog token declares one leg: its desk's price.
# The platform's own current-price feed (engine/acquire/jobs/current_prices.py), refreshed every
# few minutes from DefiLlama. Ranked after the on-chain readings a browser takes live.
DEFILLAMA_CURRENT = "defillama-current"
SOURCE_PREFERENCE = (
    "chainlink-aggregator", "uniswap-v3-twap", DEFILLAMA_CURRENT,
    "declared-testnet-demo-sale-terms", "declared-testnet-demo-redemption-terms",
    "declared-testnet-catalog-desk-terms",
)
# Not dollar prices, so they never value a holding. A LI.FI quote states USDC spent per token
# (scripts/build_universe.executable_price_observations); while two sources had to agree it could
# only confirm a dollar price, but alone it would turn a USDC depeg into an overstated NAV. Until
# its USDC leg is converted with a measured USDC/USD price it stays evidence, not a price.
NOT_DOLLAR_DENOMINATED = frozenset({"lifi-executable-route-quote"})


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One independently attributable integer fixed-price observation."""

    identity: str
    source_id: str
    independence_key: str
    price_usd_fixed: int
    observed_at: datetime
    method_version: str
    # Sources this observation is built on top of. An independence key names where a price came
    # from; this names what it leans on to get there, which is not the same thing. A pool quotes
    # in its own token, so stating that ratio in dollars borrows a dollar price from somewhere --
    # and an observation that borrows from Chainlink is not independent evidence against
    # Chainlink, however different the measurement in front of it was.
    depends_on: tuple[str, ...] = ()
    schema_version: str = "fenorium/price-observation/v1"

    def __post_init__(self) -> None:
        if not self.identity or not self.source_id or not self.independence_key:
            raise PortfolioContractError("price identity, source_id, and independence_key are required")
        if isinstance(self.price_usd_fixed, bool) or not isinstance(self.price_usd_fixed, int):
            raise PortfolioContractError("price_usd_fixed must be an integer at PRICE_SCALE precision")
        if self.price_usd_fixed <= 0:
            raise PortfolioContractError("price_usd_fixed must be positive; zero is not an observed price")
        require_utc(self.observed_at, field_name="PriceObservation.observed_at")
        if not self.method_version:
            raise PortfolioContractError("PriceObservation.method_version is required")
        if self.schema_version != "fenorium/price-observation/v1":
            raise PortfolioContractError("PriceObservation must use fenorium/price-observation/v1")
        if not isinstance(self.depends_on, tuple) or any(
            not isinstance(item, str) or not item for item in self.depends_on
        ):
            raise PortfolioContractError("depends_on must be a tuple of non-empty source names")
        if self.independence_key in self.depends_on:
            raise PortfolioContractError("an observation cannot depend on itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "source_id": self.source_id,
            "independence_key": self.independence_key,
            "price_usd_fixed": self.price_usd_fixed,
            "price_scale": PRICE_SCALE,
            "observed_at": utc_iso(self.observed_at, field_name="PriceObservation.observed_at"),
            "method_version": self.method_version,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class PriceResult:
    identity: str
    price_usd_fixed: int | None
    selected_sources: tuple[PriceObservation, ...]
    reason_code: ReasonCode
    as_of: datetime
    method_version: str
    alignment_bps: int | None = None
    schema_version: str = "fenorium/price-result/v1"

    def __post_init__(self) -> None:
        require_utc(self.as_of, field_name="PriceResult.as_of")
        if not self.method_version:
            raise PortfolioContractError("PriceResult.method_version is required")
        if self.schema_version != "fenorium/price-result/v1":
            raise PortfolioContractError("PriceResult must use fenorium/price-result/v1")
        if self.price_usd_fixed is not None and self.price_usd_fixed <= 0:
            raise PortfolioContractError("PriceResult.price_usd_fixed must be positive or null")

    @property
    def available(self) -> bool:
        return self.price_usd_fixed is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "price_usd_fixed": self.price_usd_fixed,
            "price_scale": PRICE_SCALE,
            "selected_sources": [source.to_dict() for source in self.selected_sources],
            "reason_code": self.reason_code.value,
            "as_of": utc_iso(self.as_of, field_name="PriceResult.as_of"),
            "method_version": self.method_version,
            "alignment_bps": self.alignment_bps,
        }


def strict_price(
    identity: str,
    observations: Iterable[PriceObservation],
    *,
    as_of: datetime,
    method_version: str,
    max_age_seconds: int,
    source_max_age_seconds: Mapping[str, int] | None = None,
) -> PriceResult:
    """Price a holding from its most preferred fresh source.

    `max_age_seconds` bounds every source except those `source_max_age_seconds` names, which keep
    their own bound: a feed that publishes every few minutes is fresh on a different clock from a
    chain read taken a moment ago.

    One fresh observation is enough. Of several, the source earliest in SOURCE_PREFERENCE wins and
    the rest are fallback: a stale or missing preferred source hands over to the next fresh one.
    Disagreement between sources does not block a value. Repeated reads from one source count once
    (its newest), and the chosen observation is the only selected source in the output.
    """

    require_utc(as_of, field_name="strict_price.as_of")
    bounds = dict(source_max_age_seconds or {})
    if max_age_seconds < 0 or any(type(value) is not int or value < 0 for value in bounds.values()):
        raise PortfolioContractError("price age limits cannot be negative")
    observed = [item for item in observations
                if item.identity == identity and item.independence_key not in NOT_DOLLAR_DENOMINATED]
    fresh = [
        item
        for item in observed
        if 0 <= int((as_of - require_utc(item.observed_at)).total_seconds())
        <= bounds.get(item.independence_key, max_age_seconds)
    ]
    if not fresh:
        reason = ReasonCode.STALE_INPUT if observed else ReasonCode.MISSING_PRICE
        return PriceResult(
            identity=identity,
            price_usd_fixed=None,
            selected_sources=(),
            reason_code=reason,
            as_of=as_of,
            method_version=method_version,
        )

    # The newest observation per source; lower source id and price break exact ties.
    newest: dict[str, PriceObservation] = {}
    for item in sorted(
        fresh,
        key=lambda row: (
            row.independence_key,
            -int(require_utc(row.observed_at).timestamp()),
            row.source_id,
            row.price_usd_fixed,
        ),
    ):
        newest.setdefault(item.independence_key, item)

    def preference(row: PriceObservation) -> tuple[int, str, str]:
        key = row.independence_key
        rank = SOURCE_PREFERENCE.index(key) if key in SOURCE_PREFERENCE else len(SOURCE_PREFERENCE)
        return rank, key, row.source_id

    chosen = min(newest.values(), key=preference)
    return PriceResult(
        identity=identity,
        price_usd_fixed=chosen.price_usd_fixed,
        selected_sources=(chosen,),
        reason_code=ReasonCode.COMPUTED,
        as_of=as_of,
        method_version=method_version,
    )


@dataclass(frozen=True, slots=True)
class PricedPosition:
    position: ClassifiedPosition
    price: PriceResult | None
    value_usd_micros: int | None
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        if self.value_usd_micros is not None and self.value_usd_micros < 0:
            raise PortfolioContractError("position USD value cannot be negative")
        if self.value_usd_micros is not None and self.position.raw_amount is None:
            raise PortfolioContractError("unresolved raw amount cannot have a derived value")

    @property
    def identity(self) -> str:
        return self.position.identity

    @property
    def category(self) -> str:
        return self.position.asset.category or "UNKNOWN"

    @property
    def protocol(self) -> str:
        return self.position.asset.protocol or "UNKNOWN"

    @property
    def is_unresolved_nonzero(self) -> bool:
        return self.position.raw_amount is None or (
            self.position.raw_amount > 0 and self.value_usd_micros is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "position": self.position.to_dict(),
            "price": None if self.price is None else self.price.to_dict(),
            "value_usd_micros": self.value_usd_micros,
            "reason_code": self.reason_code.value,
        }


def _raw_to_usd_micros(raw_amount: int, *, decimals: int, price_usd_fixed: int) -> int:
    denominator = (10**decimals) * PRICE_SCALE
    return (raw_amount * price_usd_fixed * USD_MICROS) // denominator


# The one conversion from token units at a fixed price to USD micros, rounded down.
raw_to_usd_micros = _raw_to_usd_micros


def value_position(
    position: ClassifiedPosition,
    observations: Iterable[PriceObservation],
    *,
    as_of: datetime,
    method_version: str,
    max_price_age_seconds: int,
    source_max_age_seconds: Mapping[str, int] | None = None,
) -> PricedPosition:
    """Value one position without a stablecoin peg assumption or a price fallback."""

    raw_amount = position.raw_amount
    if raw_amount is None:
        return PricedPosition(
            position=position,
            price=None,
            value_usd_micros=None,
            reason_code=ReasonCode.UNRESOLVED_POSITION,
        )
    if raw_amount == 0:
        return PricedPosition(
            position=position,
            price=None,
            value_usd_micros=0,
            reason_code=ReasonCode.ZERO_BALANCE,
        )
    if position.valuation.status is not CapabilityStatus.SUPPORTED:
        return PricedPosition(
            position=position,
            price=None,
            value_usd_micros=None,
            reason_code=position.valuation.reason_code,
        )
    price = strict_price(
        position.identity,
        observations,
        as_of=as_of,
        method_version=method_version,
        max_age_seconds=max_price_age_seconds,
        source_max_age_seconds=source_max_age_seconds,
    )
    if not price.available:
        return PricedPosition(
            position=position,
            price=price,
            value_usd_micros=None,
            reason_code=price.reason_code,
        )
    if price.price_usd_fixed is None:
        raise PortfolioContractError(
            "an available strict price must carry its fixed-point USD value"
        )
    return PricedPosition(
        position=position,
        price=price,
        value_usd_micros=_raw_to_usd_micros(
            raw_amount,
            decimals=position.asset.decimals,
            price_usd_fixed=price.price_usd_fixed,
        ),
        reason_code=ReasonCode.COMPUTED,
    )


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    """An authoritative NAV only when every nonzero registered position has a value."""

    positions: tuple[PricedPosition, ...]
    as_of: datetime
    method_version: str
    priced_nav_lower_bound_usd_micros: int
    total_nav_usd_micros: int | None
    unresolved_position_count: int
    position_count_coverage_bps: int | None
    value_coverage_bps: int | None
    reason_codes: tuple[ReasonCode, ...]
    # Confirmed holdings this registry cannot price, by identity. The count alone says coverage
    # is incomplete; these say which holdings made it so, which is what a reader needs to act.
    unvalued_identities: tuple[str, ...] = ()
    schema_version: str = "fenorium/portfolio-valuation/v1"
    # v1 has no field for a holding it cannot price, so a valuation carrying one is a different
    # document and says so. Editing v1 in place would have given two incompatible shapes one
    # version: old payloads failing the new schema, new payloads failing the old one.

    def __post_init__(self) -> None:
        require_utc(self.as_of, field_name="PortfolioValuation.as_of")
        if self.schema_version not in (
            "fenorium/portfolio-valuation/v1", "fenorium/portfolio-valuation/v2"
        ):
            raise PortfolioContractError("PortfolioValuation must use valuation v1 or v2")
        carries_unvalued = bool(self.unvalued_identities)
        if carries_unvalued and self.schema_version != "fenorium/portfolio-valuation/v2":
            raise PortfolioContractError("a valuation naming unpriced holdings is v2")
        if not carries_unvalued and self.schema_version != "fenorium/portfolio-valuation/v1":
            raise PortfolioContractError("a valuation with no unpriced holdings is v1")
        if not self.method_version:
            raise PortfolioContractError("PortfolioValuation.method_version is required")
        identities = [position.identity for position in self.positions]
        if identities != sorted(identities):
            raise PortfolioContractError("priced positions must be sorted by stable identity")
        if self.total_nav_usd_micros is not None and self.total_nav_usd_micros < 0:
            raise PortfolioContractError("total NAV cannot be negative")
        if self.total_nav_usd_micros is None and self.value_coverage_bps is not None:
            raise PortfolioContractError("value coverage cannot be asserted when authoritative NAV is unavailable")

    @property
    def authoritative(self) -> bool:
        return self.total_nav_usd_micros is not None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="PortfolioValuation.as_of"),
            "positions": [position.to_dict() for position in self.positions],
            "priced_nav_lower_bound_usd_micros": self.priced_nav_lower_bound_usd_micros,
            "total_nav_usd_micros": self.total_nav_usd_micros,
            "authoritative_total_nav": self.authoritative,
            "unresolved_position_count": self.unresolved_position_count,
            "position_count_coverage_bps": self.position_count_coverage_bps,
            "value_coverage_bps": self.value_coverage_bps,
            "reason_codes": [reason.value for reason in self.reason_codes],
        }
        # A v1 payload keeps the exact keys it always had, so its schema and its readers are
        # untouched by any of this.
        if self.unvalued_identities:
            payload["unvalued_identities"] = list(self.unvalued_identities)
        return payload


def value_portfolio(
    positions: Iterable[ClassifiedPosition],
    observations: Iterable[PriceObservation],
    *,
    as_of: datetime,
    method_version: str,
    max_price_age_seconds: int,
    unvalued_holdings: tuple[str, ...] = (),
    discovery_complete: bool = True,
    source_max_age_seconds: Mapping[str, int] | None = None,
    position_values_usd_micros: Mapping[str, int | None] | None = None,
) -> PortfolioValuation:
    """Value all classified positions and preserve lower-bound/coverage semantics.

    `unvalued_holdings` names balances confirmed on chain that this registry cannot price. They
    carry no value and never enter the lower bound, but they do enter the denominator: a total
    that excludes a holding we have already read is not the total. `discovery_complete` says
    whether the search for them finished, because finding none and not having looked are the
    same figure and different facts.
    """

    classified = tuple(sorted(positions, key=lambda position: position.identity))
    observation_rows = tuple(observations)
    overrides = dict(position_values_usd_micros or {})
    classified_by_id = {position.identity: position for position in classified}
    unknown_overrides = set(overrides) - set(classified_by_id)
    if unknown_overrides:
        raise PortfolioContractError("position value override names an unknown position")
    for identity, value in overrides.items():
        position = classified_by_id[identity]
        if position.asset.kind.value != "POSITION":
            raise PortfolioContractError("direct position values are only valid for POSITION assets")
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise PortfolioContractError("position values must be non-negative integer micros or null")
    priced = tuple(
        (
            PricedPosition(
                position=position,
                price=None,
                value_usd_micros=overrides[position.identity],
                reason_code=(ReasonCode.COMPUTED if overrides[position.identity] is not None
                             else ReasonCode.MISSING_PRICE),
            )
            if position.identity in overrides and position.raw_amount is not None
            else value_position(
                position,
                observation_rows,
                as_of=as_of,
                method_version=method_version,
                max_price_age_seconds=max_price_age_seconds,
                source_max_age_seconds=source_max_age_seconds,
            )
        )
        for position in classified
    )
    lower_bound = sum(
        position.value_usd_micros for position in priced if position.value_usd_micros is not None
    )
    nonzero = [position for position in priced if position.position.raw_amount != 0]
    unresolved = [position for position in nonzero if position.is_unresolved_nonzero]
    outside = tuple(sorted(set(unvalued_holdings)))
    known_count = len(nonzero) - len(unresolved)
    denominator = len(nonzero) + len(outside)
    # Coverage of what was examined is not coverage of what was asked for. With the search
    # unfinished the denominator is the positions that came back, so a failed enumeration
    # reported 100% of a scan that never ran. Withholding NAV alone left that figure standing.
    count_coverage = (
        None if not discovery_complete
        else (known_count * BPS) // denominator if denominator else BPS
    )
    authoritative = not unresolved and not outside and discovery_complete
    reasons = {position.reason_code for position in unresolved}
    if outside:
        reasons.add(ReasonCode.UNREGISTERED_ASSET)
    if not discovery_complete:
        # Nothing was found outside the registry, and nobody finished looking. Publishing an
        # authoritative total here asserts that the part of the search that was cut short would
        # have come back empty, which is exactly what is unknown.
        reasons.add(ReasonCode.ENUMERATION_TRUNCATED)
    if not authoritative:
        reasons.add(ReasonCode.VALUE_COVERAGE_NOT_COMPUTABLE)
    return PortfolioValuation(
        positions=priced,
        as_of=as_of,
        method_version=method_version,
        priced_nav_lower_bound_usd_micros=lower_bound,
        total_nav_usd_micros=lower_bound if authoritative else None,
        unresolved_position_count=len(unresolved) + len(outside),
        unvalued_identities=outside,
        schema_version=(
            "fenorium/portfolio-valuation/v2" if outside
            else "fenorium/portfolio-valuation/v1"
        ),
        position_count_coverage_bps=count_coverage,
        value_coverage_bps=BPS if authoritative else None,
        reason_codes=tuple(sorted(reasons, key=lambda reason: reason.value)),
    )


# Intentional aliases for callers reading the broad-plan names.
price_position = value_position
portfolio_valuation = value_portfolio


def value_wallet(
    snapshot: WalletSnapshot,
    observations: Iterable[PriceObservation],
    *,
    as_of: datetime,
    method_version: str,
    max_price_age_seconds: int,
    permission_gates: dict[str, Any] | None = None,
    source_max_age_seconds: Mapping[str, int] | None = None,
    position_values_usd_micros: Mapping[str, int | None] | None = None,
) -> PortfolioValuation:
    """Value a wallet snapshot, including what it found and could not price.

    The one entry point that reads a snapshot end to end. `classify_snapshot` sees only
    `positions`, so valuing through it drops any confirmed holding outside the registry and
    then reports full coverage over a denominator that excludes a balance already read on
    chain. Here the snapshot supplies its own enumerated holdings and its own completeness,
    which is what makes them impossible to leave out.
    """

    gates = permission_gates or {}
    classified = tuple(
        classify_position(position, permission_gate=gates.get(position.identity))
        for position in snapshot.positions
    )
    return value_portfolio(
        classified,
        observations,
        as_of=as_of,
        method_version=method_version,
        max_price_age_seconds=max_price_age_seconds,
        unvalued_holdings=tuple(item.identity for item in getattr(snapshot, "enumerated", ())),
        discovery_complete=getattr(snapshot, "enumeration_complete", True),
        source_max_age_seconds=source_max_age_seconds,
        position_values_usd_micros=position_values_usd_micros,
    )
