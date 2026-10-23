"""Measured route capacity, kept independent from permission to hold the token.

A permissionless transfer says nothing about whether a buyer can source the token, while a gated
token can still have a technically valid market route that an arbitrary buyer cannot use.  This
module records only what LI.FI actually quoted at stated sizes.  It folds the existing Composer
ladder rather than interpreting transport failures as liquidity or re-parsing route payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .attestation import MEASURED
from .composer import RouteBinding, SizeQuote, capacity_cliff


class ObtainabilityError(ValueError):
    """Raised when route-capacity evidence cannot be summarized honestly."""


@dataclass(frozen=True, slots=True)
class Obtainability:
    chain_id: int
    token: str
    quote_asset: str
    max_quoted_usd: int | None
    impact_bps_at: dict[int, int]
    tool_at: dict[int, str]
    refusal: str | None
    measured_at: datetime
    evidence_class: str = MEASURED
    price_binding: RouteBinding | None = None
    # Whether every rung reached a market and every market declined. Deleting evidence already
    # held needs a complete answer, not a first answer: a size nobody could ask about is
    # UNKNOWN, not refused. Defaults to False so a caller that never measured a ladder -- the
    # budget path, a transport failure -- can never be read as a complete market refusal.
    market_refused_every_size: bool = False
    # Whether any rung reached a market at all, which is a different question from whether the
    # ladder established a capacity. A lower rung can return a route while a higher one is
    # throttled: no maximum is published, `measured` is false because the error that survives is
    # a transport one -- and yet a market answered, which is what scheduling needs to know.
    market_answered: bool = False

    @property
    def obtainable(self) -> bool:
        return self.max_quoted_usd is not None

    @property
    def measured(self) -> bool:
        """Whether the market actually answered, as opposed to the request never landing.

        "No route exists" and "we could not ask" are different facts, and only the first is a
        statement about the instrument. Collapsing them into one boolean would put a rate limit
        on screen as though the venue had refused — the same shape as the throttle that was once
        cached as a gate verdict for ten healthy vaults.

        This asks whether a market refused, never merely whether the failure looked unlike a
        known transport marker. An expired API key answers every request with HTTP 401, which
        matches no such marker: read as an absence of transport failure, it would claim the
        market had spoken. Anything not positively identified as a refusal is not an answer.
        """
        if self.max_quoted_usd is not None:
            return True
        return _is_market_refusal(self.refusal)

    def obtainable_at(self, size_usd: int) -> tuple[bool, str]:
        if isinstance(size_usd, bool) or not isinstance(size_usd, int) or size_usd <= 0:
            raise ObtainabilityError(
                "size_usd must be a positive whole-dollar integer"
            )
        if self.max_quoted_usd is None:
            reason = self.refusal or "no usable route evidence"
            if not self.measured:
                return False, f"not measured: {reason}"
            return False, f"no route quoted at any size: {reason}"
        if size_usd > self.max_quoted_usd:
            return False, (
                f"largest quoted route was ${self.max_quoted_usd:,.0f}, "
                f"below the intended ${size_usd:,.0f}"
            )
        impact = self.impact_bps_at.get(size_usd)
        if impact is None:
            return False, (
                f"route capacity reached ${self.max_quoted_usd:,.0f}, but the intended "
                f"${size_usd:,.0f} has no measured price impact"
            )
        return True, f"quoted at ${size_usd:,.0f} with {impact:,d} bps of price impact"


# The one phrase that identifies a market declining to quote. The adapter that knows a refusal
# when it sees one — LI.FI answers "no route" with an HTTP 404 and everything else with its own
# status code — stamps this on it, and nothing else ever produces it.
#
# It is deliberately not a description of the failure. Matching descriptive phrases is how a
# network outage became market evidence: an unreachable host raises "[Errno 113] No route to
# host", and a rule that looked for "no route" anywhere in the text read that as the market
# having answered — then deleted the depth and rating an earlier run had measured, during an
# outage, which is the one moment the evidence is least replaceable.
MARKET_REFUSAL_PREFIX = "the market declined to quote"


def _is_market_refusal(reason: str | None) -> bool:
    """Whether a market answered and declined, as opposed to any other way of getting nothing.

    Deliberately an allowlist of one. Its predecessor asked whether the text failed to match a
    list of transport markers, which made every unanticipated failure — an authentication
    rejection, a request the router refused as invalid, a response that did not parse —
    indistinguishable from "there is no route", and that conclusion deletes evidence.
    """
    if not reason:
        return False
    return MARKET_REFUSAL_PREFIX in reason.casefold()


def market_answered(ladder: tuple[SizeQuote, ...]) -> bool:
    """Whether any rung reached a market, by being quoted or by being refused.

    Separate from whether a capacity was established: a ladder can be answered at one size and
    never reach a market at the next, which measures nothing and is still an answer.
    """
    return any(rung.executable or _is_market_refusal(rung.error) for rung in ladder)


def summarise_ladder(
    ladder: tuple[SizeQuote, ...],
    *,
    chain_id: int,
    token: str,
    quote_asset: str,
    sizes_usd: tuple[int, ...],
    measured_at: datetime,
) -> Obtainability:
    """Fold Composer's raw-unit ladder into whole-USD measured capacity."""
    if len(ladder) != len(sizes_usd):
        raise ObtainabilityError(
            f"ladder has {len(ladder)} rungs but sizes_usd has {len(sizes_usd)} entries"
        )
    if any(
        isinstance(size, bool) or not isinstance(size, int) or size <= 0
        for size in sizes_usd
    ):
        raise ObtainabilityError("sizes_usd entries must all be positive whole-dollar integers")

    raw_cliff = capacity_cliff(ladder)
    # A maximum is a claim about where capacity stops, so every size above the one that quoted
    # has to have been asked and answered by a market. A rung that never reached one leaves the
    # sizes above the cliff unknown, and publishing the cliff regardless states a limit nobody
    # measured: with $10,000 quoted and $100,000 upward rate-limited, the builder would replace
    # a $5,000,000 depth with $10,000 and then refuse the rating for a shortfall that is an
    # artefact of the throttle. A truncated ladder is not a capacity cliff.
    truncated = next(
        (rung.error for rung in ladder
         if raw_cliff is not None and rung.size > raw_cliff
         and not rung.executable and not _is_market_refusal(rung.error)),
        None,
    )
    answered = market_answered(ladder)
    if truncated is not None:
        return Obtainability(
            chain_id=int(chain_id), token=token, quote_asset=quote_asset,
            max_quoted_usd=None, impact_bps_at={}, tool_at={},
            refusal=truncated, measured_at=measured_at, market_answered=answered,
        )

    max_quoted_usd = None
    impact_bps_at: dict[int, int] = {}
    tool_at: dict[int, str] = {}

    for rung, size_usd in zip(ladder, sizes_usd, strict=True):
        if rung.binding is None:
            continue
        if rung.size == raw_cliff:
            max_quoted_usd = size_usd
        impact = rung.binding.price_impact_bps
        if impact is not None:
            impact_bps_at[size_usd] = int(impact)
        tool_at[size_usd] = rung.binding.tool

    refusal = None
    market_refused_every_size = False
    if raw_cliff is None:
        # `refusal` is the first error only, so it cannot say what happened further up the
        # ladder. A rate limit at $1,000,000 leaves that size unmeasured while a refusal at
        # $10,000 still reads as "measured", and the two together are not "no route at any size".
        refusal = next((rung.error for rung in ladder if rung.error), None)
        market_refused_every_size = all(_is_market_refusal(rung.error) for rung in ladder)

    return Obtainability(
        chain_id=int(chain_id),
        token=token,
        quote_asset=quote_asset,
        max_quoted_usd=max_quoted_usd,
        impact_bps_at=impact_bps_at,
        tool_at=tool_at,
        refusal=refusal,
        measured_at=measured_at,
        market_refused_every_size=market_refused_every_size,
        market_answered=answered,
        # Keep one actual quote, not synthetic independent sources from ladder sizes.
        price_binding=next((rung.binding for rung in ladder if rung.executable), None),
    )
