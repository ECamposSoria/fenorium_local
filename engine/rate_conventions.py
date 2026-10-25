"""Quote conventions for converting fixed-maturity rates to term returns."""

from typing import Any, Mapping


EFFECTIVE_ANNUAL = "effective_annual"
SIMPLE_ACT365 = "simple_act365"

VENUE_RATE_CONVENTIONS = {
    # research/par_sources/pendle.py: impliedApy; engine/pricing.py: pt_price compounds annually.
    "Pendle": EFFECTIVE_ANNUAL,
    # research/par_sources/spectra.py: pools[].impliedApy / 100 is the PT's effective annual yield.
    "Spectra": EFFECTIVE_ANNUAL,
    # engine/ingest.py: apyBase from the yields index does not establish compounding; use simple fallback.
    "Exactly": SIMPLE_ACT365,
    # research/par_sources/termfinance.py: order offerRate / RATE_SCALE does not establish compounding; use simple fallback.
    "Term Finance": SIMPLE_ACT365,
    # engine/ingest.py: sortedOrderStats.lendApy does not establish compounding; use simple fallback.
    "TermMax": SIMPLE_ACT365,
    # engine/ingest.py: (1 / price - 1) * 365 * 86400 / seconds_to_maturity.
    "Tenor": SIMPLE_ACT365,
    # engine/midnight.py: lender_apr is explicitly simple annual lending return.
    "Morpho Midnight": SIMPLE_ACT365,
}


def rate_convention(row: Mapping[str, Any]) -> str:
    """Classify from the rate's source, including demos labelled with real venue names."""
    if row.get("rate_kind") == "declared_demo":
        # scripts/build_universe.py: declared_accrued_price uses (1 + rate_bps / 10000) ** years.
        return EFFECTIVE_ANNUAL
    # Unestablished venues retain simple ACT/365; an APY label alone cannot prove compounding.
    return VENUE_RATE_CONVENTIONS.get(row.get("venue"), SIMPLE_ACT365)
