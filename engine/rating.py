"""Net yield after entry costs, computed on the caller's stated assumptions.

This is a calculator, not a rating. The distinction is load-bearing and it is structural, not
cosmetic:

* every deduction comes from an assumption the caller supplies, and the assumptions travel with
  the result, so a number can never be quoted without the inputs that produced it;
* the result decomposes — each component is returned separately and they sum back to the net, so
  a reader can always ask "where did that basis point come from" and get an answer;
* **it refuses to compute what the evidence cannot support.** An instrument the engine could not
  evaluate returns a refusal, not a number with a wide error bar. Producing a confident-looking
  figure on absent evidence is precisely the failure this project exists to catch, and it would be
  worse coming from us than from a yield table, because ours would look considered.

Nothing here asserts what an instrument is worth. It answers: *given your costs and your size,
what does the arithmetic say?*
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import isqrt
from typing import Any

BPS = 10_000
RATING_METHOD_VERSION = "par-engine/2026.10.25-net-after-costs-v1"
NET_AFTER_COSTS = "NET_AFTER_COSTS"
MEASURED_BASE_RUNG_USD = 10_000  # Smallest research.par_sources.lifi_quotes.DEFAULT_SIZES_USD rung.
MEASURED_LIFI_LADDER = "MEASURED_LIFI_LADDER"
MODELLED_SIZE_TO_REPORTED_DEPTH = "MODELLED_SIZE_TO_REPORTED_DEPTH"
IMPACT_ROUNDING = "CEILING_TO_INTEGER_BPS"
ANNUALIZATION_ROUNDING = "NEAREST_INTEGER_BPS_HALF_UP"


class RatingError(ValueError):
    """Raised when the caller's assumptions are not usable."""


@dataclass(frozen=True, slots=True)
class Assumptions:
    """Everything the caller decides. Versioned so a result can be reproduced.

    ``execution_cost_bps`` is the stated base fee paid at every size. Without a usable measured
    ladder, additional size cost is MODELLED: ``impact_coefficient_bps`` is the
    assumed cost at size/depth == 1 and ``impact_exponent`` selects the exact rational curve.
    The square-root default is encoded as ``"1/2"`` rather than a float so the assumptions can
    travel through canonical JSON without weakening the integer-only evidence boundary.
    """

    size_usd: int | float | str
    execution_cost_bps: int
    routing_fee_bps: int = 10
    impact_coefficient_bps: int = 100
    impact_exponent: str = "1/2"
    require_full_evidence: bool = True
    label: str = "caller-supplied"
    # An open-ended instrument has no maturity to amortise an entry cost across, so the caller
    # states how long they intend to hold it. Without this a vault and a tokenized fund cannot be
    # compared to a dated instrument at all — not because the arithmetic is unavailable, but
    # because the question "what does this cost me per year" is meaningless until someone says
    # for how long. It is the caller's assumption, exactly like the costs, and it travels with
    # the result.
    holding_period_days: int | None = None
    # A vault reports what it returned over a trailing window. That is neither a quote you can
    # lock nor an indicative price nobody stands behind — it is a third thing, and refusing it
    # outright excludes every curated vault from comparison. Accepting it is opt-in, and the
    # Rating carries `rate_basis` so a trailing number can never be sorted against a locked one
    # as though the two answered the same question.
    accept_realized: bool = False

    def __post_init__(self) -> None:
        if _fraction(self.size_usd, "size_usd") <= 0:
            raise RatingError("size_usd must be positive: a rate without a size is not a rate")
        if not isinstance(self.execution_cost_bps, int) or isinstance(
            self.execution_cost_bps, bool
        ) or self.execution_cost_bps < 0:
            raise RatingError("execution_cost_bps must be a non-negative integer")
        if not isinstance(self.impact_coefficient_bps, int) or isinstance(
            self.impact_coefficient_bps, bool
        ) or self.impact_coefficient_bps < 0:
            raise RatingError("impact_coefficient_bps must be a non-negative integer")
        if self.impact_exponent not in {"1/2", "1"}:
            raise RatingError('impact_exponent must be the exact rational string "1/2" or "1"')
        if self.holding_period_days is not None and self.holding_period_days <= 0:
            raise RatingError("holding_period_days must be positive when supplied")
        if type(self.routing_fee_bps) is not int or self.routing_fee_bps < 0:
            raise RatingError("routing_fee_bps must be a non-negative integer")

    def to_json(self) -> dict[str, Any]:
        return {
            "size_usd": _fraction_text(self.size_usd, "size_usd"),
            "execution_cost_bps": int(self.execution_cost_bps),
            "routing_fee_bps": self.routing_fee_bps,
            "impact_coefficient_bps": int(self.impact_coefficient_bps),
            "impact_exponent": self.impact_exponent,
            "cost_model": NET_AFTER_COSTS,
            "impact_rounding": IMPACT_ROUNDING,
            "annualization_rounding": ANNUALIZATION_ROUNDING,
            "require_full_evidence": bool(self.require_full_evidence),
            "holding_period_days": self.holding_period_days,
            "accept_realized": bool(self.accept_realized),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class Rating:
    instrument: str
    venue: str
    computable: bool
    refusal: str | None = None
    headline_bps: int = 0
    execution_bps: int = 0
    base_fee_bps: int = 0
    size_cost_bps: int | None = None
    size_cost_basis: str | None = None
    size_cost_rung_usd: int | None = None
    size_cost_measured_at: str | None = None
    routing_fee_bps: int = 0
    base_fee_annualized_bps: int | None = None
    size_cost_annualized_bps: int | None = None
    routing_fee_annualized_bps: int | None = None
    net_bps: int = 0
    days_to_maturity: float | None = None
    tier: str | None = None
    rate_basis: str = "quoted"
    reported_depth_usd: float | None = None
    size_usd: str = ""
    reported_depth_usd_exact: str | None = None
    method_version: str = RATING_METHOD_VERSION
    cost_model: str = NET_AFTER_COSTS
    impact_coefficient_bps: int = 100
    impact_exponent: str = "1/2"
    impact_rounding: str = IMPACT_ROUNDING
    annualization_rounding: str = ANNUALIZATION_ROUNDING
    components: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def _fraction(value: int | float | str, name: str) -> Fraction:
    """Convert a numeric input through its decimal text, never through binary float arithmetic."""
    if isinstance(value, bool):
        raise RatingError(f"{name} must be numeric")
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise RatingError(f"{name} must be a finite number") from exc
    return parsed


def _fraction_text(value: int | float | str, name: str) -> str:
    """Canonical exact text for an assumption that may have arrived as a decimal."""
    parsed = _fraction(value, name)
    return (
        str(parsed.numerator)
        if parsed.denominator == 1
        else f"{parsed.numerator}/{parsed.denominator}"
    )


def _round_fraction_nearest(value: Fraction) -> int:
    """Round a non-negative fraction to nearest integer; exact half-bps round upward."""
    if value < 0:
        raise RatingError("basis-point cost cannot be negative")
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + int(remainder * 2 >= value.denominator)


def modelled_size_cost_bps(
    *,
    size_usd: int | float | str,
    depth_usd: int | float | str,
    coefficient_bps: int,
    exponent: str,
) -> int:
    """Return stated size impact in integer bps, conservatively rounded upward.

    This is a caller-parameterized model, not observed slippage and not an executable quote.
    Square-root impact is evaluated with exact integer inequalities, so no float can influence the
    published bps. Ceiling is deliberate: a positive modelled component never disappears as 0 bp.
    """
    size = _fraction(size_usd, "size_usd")
    depth = _fraction(depth_usd, "depth_usd")
    if size <= 0 or depth <= 0:
        raise RatingError("size_usd and depth_usd must be positive to model impact")
    if (
        not isinstance(coefficient_bps, int)
        or isinstance(coefficient_bps, bool)
        or coefficient_bps < 0
    ):
        raise RatingError("coefficient_bps must be a non-negative integer")

    numerator = coefficient_bps * size.numerator * depth.denominator
    denominator = size.denominator * depth.numerator
    if exponent == "1":
        return (numerator + denominator - 1) // denominator
    if exponent != "1/2":
        raise RatingError('impact exponent must be "1/2" or "1"')

    squared_numerator = coefficient_bps * numerator
    root = isqrt(squared_numerator // denominator)
    return root + int(root * root * denominator < squared_numerator)


def annualized_execution_bps(cost_bps: int, days_to_maturity: float) -> int:
    """Amortize a one-off entry cost across the holding period.

    Thirty basis points to get in is half a percent a year on a 220-day position and over three
    percent on a 34-day one. Yield tables quote the same headline for both, which is how a short
    instrument that cannot cover its own execution ends up looking like the best row on the page.
    """
    if not isinstance(cost_bps, int) or isinstance(cost_bps, bool) or cost_bps < 0:
        raise RatingError("cost_bps must be a non-negative integer")
    if days_to_maturity <= 0:
        raise RatingError("days_to_maturity must be positive to amortize an entry cost")
    return _round_fraction_nearest(
        Fraction(cost_bps * 365, 1) / _fraction(days_to_maturity, "days_to_maturity")
    )


def _measured_size_cost(ladder: Any, size: Fraction) -> tuple[int, int, str | None] | None:
    """Select the next quoted rung; malformed or missing evidence uses the stated model."""
    if not isinstance(ladder, dict) or ladder.get("measured") is not True:
        return None
    impacts = ladder.get("impact_bps_at")
    maximum = ladder.get("max_quoted_usd")
    if not isinstance(impacts, dict) or maximum is None:
        return None
    try:
        maximum = _fraction(maximum, "max_quoted_usd")
    except RatingError:
        return None
    rungs = {}
    for key, value in impacts.items():
        if type(key) not in (int, str):
            continue
        try:
            rung = int(key)
        except ValueError:
            continue
        if rung >= MEASURED_BASE_RUNG_USD and value is not None:
            rungs[rung] = value
    base = rungs.get(MEASURED_BASE_RUNG_USD)
    eligible = [rung for rung in rungs if max(size, MEASURED_BASE_RUNG_USD) <= rung <= maximum]
    if type(base) is not int or not eligible:
        return None
    chosen = min(eligible)
    if type(rungs[chosen]) is not int:
        return None
    # The absolute ladder level depends on LI.FI's USD valuation of the destination token,
    # which is biased for principal tokens. Two rungs share that valuation and the same moment,
    # so their difference isolates the measured extra cost of size.
    return max(0, rungs[chosen] - base), chosen, ladder.get("measured_at")


def rate_instrument(point: dict[str, Any], assumptions: Assumptions) -> Rating:
    """Rate one curve point. Returns a refusal rather than a guess when evidence is absent."""
    instrument = str(point.get("instrument") or point.get("identity") or "unknown")
    venue = str(point.get("venue") or "unknown")
    tier = point.get("tier")
    days = point.get("days_to_maturity")
    depth = point.get("liquidity_usd")

    def refuse(reason: str) -> Rating:
        return Rating(
            instrument=instrument, venue=venue, computable=False, refusal=reason,
            days_to_maturity=days, tier=tier, reported_depth_usd=depth,
            size_usd=_fraction_text(assumptions.size_usd, "size_usd"),
            reported_depth_usd_exact=(
                None if depth is None else _fraction_text(depth, "depth_usd")
            ),
            base_fee_bps=assumptions.execution_cost_bps,
            routing_fee_bps=(assumptions.routing_fee_bps
                             if point.get("routed_purchase") is True else 0),
            impact_coefficient_bps=assumptions.impact_coefficient_bps,
            impact_exponent=assumptions.impact_exponent,
        )

    # The same doctrine the engine applies to a verdict applies to a number derived from one.
    if assumptions.require_full_evidence and point.get("status") == "NO QUOTE":
        return refuse("NO QUOTE: the engine could not price this instrument, so neither can this")
    if point.get("rejection_basis") == "evidence" and assumptions.require_full_evidence:
        return refuse("insufficient evidence: rating one would dress a data gap as a risk view")

    rate = point.get("executable_apy")
    kind = "executable"
    if rate is None:
        rate = point.get("indicative_apy")
        kind = "indicative"
    if rate is None:
        return refuse("no published rate")
    if days is None or days <= 0:
        # Open-ended: fall back to the horizon the caller stated. Refusing outright would exclude
        # every vault and tokenized fund from comparison over a fact about their shape rather than
        # about the evidence for them.
        days = assumptions.holding_period_days
        if days is None:
            return refuse(
                "open-ended instrument and no holding period stated: an entry cost cannot be "
                "annualised without one"
            )
        horizon_is_assumed = True
    else:
        horizon_is_assumed = False
    declared = str(point.get("rate_kind") or "")
    if declared == "realized":
        if not assumptions.accept_realized:
            return refuse(
                "rate is a trailing observation, not an offer: enable accept_realized to compare "
                "it, and never against a locked rate without saying so"
            )
        kind = "realized"
    elif declared == "issuer_quoted":
        kind = "issuer_quoted"
    elif declared == "declared_demo":
        # Preserve provenance through otherwise unchanged rating arithmetic. A declared demo
        # rate is neither a venue quote nor a market observation.
        kind = "declared_demo"
    elif kind == "indicative" and assumptions.require_full_evidence:
        return refuse("rate is indicative only, never shown to be executable at any size")

    if depth is None:
        # Unknown depth is not permission and the model cannot evaluate size/depth. This refusal
        # applies even when other evidence requirements are relaxed: missing impact is UNKNOWN,
        # never silently represented by the flat base fee or a zero component.
        return refuse(
            "no reported depth: size cost is UNKNOWN, so execution cost and net yield "
            "cannot be computed at this size"
        )
    elif _fraction(depth, "depth_usd") < _fraction(assumptions.size_usd, "size_usd"):
        return refuse(
            f"reported depth ${float(depth):,.0f} is below the intended "
            f"${float(assumptions.size_usd):,.0f}"
        )

    ladder = point.get("obtainable")
    if isinstance(ladder, dict) and ladder.get("measured") is True:
        try:
            maximum = _fraction(ladder.get("max_quoted_usd"), "max_quoted_usd")
        except RatingError:
            maximum = None
        if maximum is not None and maximum < _fraction(assumptions.size_usd, "size_usd"):
            return refuse(
                f"reported depth ${float(maximum):,.0f} is below the intended "
                f"${float(assumptions.size_usd):,.0f}"
            )

    headline = _round_fraction_nearest(_fraction(rate, "rate") * BPS)
    measured = _measured_size_cost(ladder, _fraction(assumptions.size_usd, "size_usd"))
    if measured is not None:
        size_cost, rung, measured_at = measured
        basis = MEASURED_LIFI_LADDER
    else:
        size_cost = modelled_size_cost_bps(
            size_usd=assumptions.size_usd, depth_usd=depth,
            coefficient_bps=assumptions.impact_coefficient_bps,
            exponent=assumptions.impact_exponent,
        )
        rung, measured_at = None, None
        basis = MODELLED_SIZE_TO_REPORTED_DEPTH
    routing_fee = assumptions.routing_fee_bps if point.get("routed_purchase") is True else 0
    base_annualized = annualized_execution_bps(assumptions.execution_cost_bps, float(days))
    size_annualized = annualized_execution_bps(size_cost, float(days))
    routing_annualized = annualized_execution_bps(routing_fee, float(days))
    execution = base_annualized + size_annualized + routing_annualized
    net = headline - execution
    suffix = " (your stated horizon)" if horizon_is_assumed else ""
    size_label = (
        f"size cost {size_cost}bp at ${float(assumptions.size_usd):,.0f} "
        f"(measured LI.FI quotes $10k→${rung:,})"
        if measured is not None else
        f"MODELLED size cost {size_cost}bp at ${float(assumptions.size_usd):,.0f}"
    )
    components = [
        ("headline", headline),
        (f"entry fee {assumptions.execution_cost_bps}bp over {float(days):.0f}d" + suffix,
         -base_annualized),
        (size_label + suffix, -size_annualized),
    ]
    if routing_fee:
        components.append((f"Fenorium routing fee {routing_fee}bp over {float(days):.0f}d" + suffix,
                           -routing_annualized))
    components.append(("net", net))

    return Rating(
        instrument=instrument, venue=venue, computable=True,
        headline_bps=headline, execution_bps=execution,
        base_fee_bps=assumptions.execution_cost_bps,
        size_cost_bps=size_cost, size_cost_basis=basis,
        size_cost_rung_usd=rung, size_cost_measured_at=measured_at,
        routing_fee_bps=routing_fee,
        base_fee_annualized_bps=base_annualized,
        size_cost_annualized_bps=size_annualized,
        routing_fee_annualized_bps=routing_annualized,
        net_bps=net, days_to_maturity=float(days), tier=tier, reported_depth_usd=depth,
        size_usd=_fraction_text(assumptions.size_usd, "size_usd"),
        reported_depth_usd_exact=_fraction_text(depth, "depth_usd"),
        rate_basis=kind, impact_coefficient_bps=assumptions.impact_coefficient_bps,
        impact_exponent=assumptions.impact_exponent,
        components=tuple(components),
    )


def rate_curve(points: list[dict[str, Any]], assumptions: Assumptions) -> list[Rating]:
    return [rate_instrument(point, assumptions) for point in points]
