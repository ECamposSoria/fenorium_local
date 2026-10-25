"""Rating a universe row again at a size the caller names.

The universe is rated at one reference size. `/v1/universe?size_usd=` re-rates every row at
another, and a route is re-rated at its own notional before it is built: depth that covers the
reference size may not cover this purchase, and a verdict earned at one size is not one at another.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from fractions import Fraction
from math import ceil
from typing import Any

from engine.rating import Assumptions, Rating, rate_instrument
from engine.purchase_paths import routed_purchase


EXECUTION_COST_BPS = 30
IMPACT_COEFFICIENT_BPS = 100
IMPACT_EXPONENT = "1/2"
HOLDING_PERIOD_DAYS = 90
EXECUTION_COST_MODEL = "STATED_BASE_FEE_PLUS_SIZE_COST_PLUS_ROUTING_FEE"
MATURED_REFUSAL = "MATURED: the redemption date has passed, so there is no horizon left to rate"


def restate_declared_days(rows: list[dict[str, Any]], today: date) -> None:
    """Count each declared row's days to maturity on the day it is served.

    A declared row is not rebuilt nightly, so the count written when its universe was built goes
    stale, and every rating amortizes its entry cost over this count. Restating it here, before
    any rating, keeps the days a row shows and the yield computed from them on the same date.
    """
    for row in rows:
        maturity = row.get("maturity")
        if row.get("rate_kind") != "declared_demo" or not isinstance(maturity, str):
            continue
        try:
            row["days_to_maturity"] = (date.fromisoformat(maturity) - today).days
        except ValueError:
            continue


def _declared_matured(row: dict[str, Any]) -> bool:
    days = row.get("days_to_maturity")
    return (row.get("rate_kind") == "declared_demo" and isinstance(row.get("maturity"), str)
            and type(days) is int and days <= 0)


def _routing_fee_bps(configured_fee: Fraction | None) -> int:
    """Convert the configured fee fraction to integer bps, rounding up; unset means zero."""
    return 0 if configured_fee is None else ceil(configured_fee * 10_000)


def _rating_assumptions(size_usd: int, routing_fee_bps: int) -> Assumptions:
    return Assumptions(
        size_usd=size_usd,
        execution_cost_bps=EXECUTION_COST_BPS,
        routing_fee_bps=routing_fee_bps,
        impact_coefficient_bps=IMPACT_COEFFICIENT_BPS,
        impact_exponent=IMPACT_EXPONENT,
        holding_period_days=HOLDING_PERIOD_DAYS,
        accept_realized=True,
    )


def _rating_point(row: dict[str, Any]) -> dict[str, Any]:
    rate_bps = row.get("rate_bps")
    apy = rate_bps / 10_000 if rate_bps is not None else None
    indicative = row.get("rate_kind") == "indicative"
    return {
        "instrument": row.get("name"),
        "venue": row.get("venue"),
        "status": row.get("status"),
        "rejection_basis": row.get("rejection"),
        "tier": row.get("tier"),
        "rate_kind": row.get("rate_kind"),
        "executable_apy": None if indicative else apy,
        "indicative_apy": apy if indicative else None,
        "days_to_maturity": row.get("days_to_maturity"),
        "liquidity_usd": row.get("depth_usd"),
        "obtainable": row.get("obtainable"),
        "routed_purchase": routed_purchase(row),
    }


def _rating_json(rating: Rating) -> dict[str, Any]:
    def optional_int(value: int | None) -> int | None:
        return None if value is None else int(value)

    return {
        "computable": bool(rating.computable),
        "refusal": rating.refusal,
        "headline_bps": int(rating.headline_bps),
        "execution_bps": int(rating.execution_bps),
        "base_fee_bps": int(rating.base_fee_bps),
        "size_cost_bps": optional_int(rating.size_cost_bps),
        "size_cost_basis": rating.size_cost_basis,
        "size_cost_rung_usd": rating.size_cost_rung_usd,
        "size_cost_measured_at": rating.size_cost_measured_at,
        "routing_fee_bps": int(rating.routing_fee_bps),
        "base_fee_annualized_bps": optional_int(rating.base_fee_annualized_bps),
        "size_cost_annualized_bps": optional_int(rating.size_cost_annualized_bps),
        "routing_fee_annualized_bps": optional_int(rating.routing_fee_annualized_bps),
        "net_bps": int(rating.net_bps),
        "rate_basis": rating.rate_basis,
        "size_usd": rating.size_usd,
        "reported_depth_usd": rating.reported_depth_usd_exact,
        "method_version": rating.method_version,
        "cost_model": rating.cost_model,
        "impact_coefficient_bps": int(rating.impact_coefficient_bps),
        "impact_exponent": rating.impact_exponent,
        "impact_rounding": rating.impact_rounding,
        "annualization_rounding": rating.annualization_rounding,
        "components": [[label, int(bps)] for label, bps in rating.components],
    }


def _is_size_depth_refusal(refusal: Any) -> bool:
    return isinstance(refusal, str) and refusal.startswith("reported depth $") and " below the intended $" in refusal


def _rerated_rows(rows: list[dict[str, Any]], size_usd: int, routing_fee_bps: int) -> tuple[list[dict[str, Any]], int]:
    assumptions = _rating_assumptions(size_usd, routing_fee_bps)
    rerated = []
    priceable_count = 0
    for source_row in rows:
        row = dict(source_row)
        rating = rate_instrument(_rating_point(source_row), assumptions)
        if _declared_matured(source_row):
            # The open-ended fallback would rate it over a holding period it no longer has.
            rating = replace(rating, computable=False, refusal=MATURED_REFUSAL)
        row["rating"] = _rating_json(rating)
        row["source_verdict"] = source_row.get("verdict")
        row["source_reason"] = source_row.get("reason")
        if str(source_row.get("verdict") or "").upper() == "UNRATED":
            row["fenorium_score"] = None
            row["score_cap"] = None
            # A preview decision hash covers the score; one that no longer matches is withdrawn.
            row.pop("decision_hash", None)
        if rating.computable:
            priceable_count += 1
            # Size may only make the engine's verdict stricter, never looser. Every NO BID in
            # the universe is refused on credit or evidence grounds -- none on size alone -- so
            # a smaller notional can never earn a BID the engine withheld. Lifting one here
            # would retune a threshold to green the curve.
        elif _is_size_depth_refusal(rating.refusal) and source_row.get("verdict"):
            # Only an instrument the engine actually verdicts can be demoted by size. Vault and
            # RWA rows carry no verdict at all -- it is not applicable to them -- so manufacturing
            # a NO BID here would have the platform speak where the engine was deliberately
            # silent. Their rating still refuses for depth, which is where that belongs.
            row["verdict"] = "NO BID"
            if source_row.get("verdict") == "BID":
                # A score that passed at the reference size must not remain green after this
                # size-dependent refusal. Old universes without scores stay numberless.
                score = source_row.get("fenorium_score")
                if type(score) is int:
                    row["fenorium_score"] = min(score, 59)
                    if row["fenorium_score"] != score:
                        row.pop("decision_hash", None)
                row["score_cap"] = rating.refusal
                row["reason"] = rating.refusal
            else:
                source_reason = source_row.get("reason")
                row["reason"] = (
                    f"{source_reason} · Size constraint: {rating.refusal}"
                    if source_reason
                    else rating.refusal
                )
        rerated.append(row)
    return rerated, priceable_count
