from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class Operation(StrEnum):
    LEND = "lend"
    REDEEM = "redeem"
    REBALANCE = "rebalance"


@dataclass(frozen=True)
class GasMarket:
    chain: str
    gas_price_wei: int
    eth_usd: float
    gas_pulled_at: datetime
    eth_usd_pulled_at: datetime
    gas_source: str
    eth_usd_source: str

    def __post_init__(self) -> None:
        if self.gas_price_wei <= 0:
            raise ValueError(f"{self.chain}: gas_price_wei must be measured and positive")
        if not math.isfinite(self.eth_usd) or self.eth_usd <= 0:
            raise ValueError("ETH/USD must be measured, finite, and positive")
        if self.gas_pulled_at.tzinfo is None or self.eth_usd_pulled_at.tzinfo is None:
            raise ValueError("gas observations must be timezone-aware")


# Explicit conservative engineering estimates, not measured transaction receipts.
#
# PendleAdapter.invest is the longest lend path: onlyVault + ABI decode + one external
# swapExactTokenForPt call. TermMaxAdapter.invest has the same shape with takeOrder. The token
# approval is constructor-only, so it is not charged per lend. The longest matured redemption is
# PendleAdapter.redeemMatured -> redeemPyToToken(vault), one external state-changing call. An early
# divest has the same one-call adapter shape. A rebalance is therefore derived as one divest plus
# one invest, not assigned an unrelated flat number.
ADAPTER_GAS_UNITS: Mapping[Operation, int] = {
    Operation.LEND: 286_000,
    Operation.REDEEM: 186_000,
    Operation.REBALANCE: 472_000,
}

ADAPTER_GAS_DERIVATION: Mapping[Operation, str] = {
    Operation.LEND: (
        "286,000 conservative units: 21,000 transaction envelope + 15,000 onlyVault/ABI/return "
        "+ 250,000 for the longest one-call adapter path, Pendle swapExactTokenForPt; constructor "
        "forceApprove is excluded because it is not per lend"
    ),
    Operation.REDEEM: (
        "186,000 conservative units: 21,000 transaction envelope + 15,000 guard/return + 150,000 "
        "for the longest one-call matured path, Pendle redeemPyToToken(vault)"
    ),
    Operation.REBALANCE: (
        "472,000 conservative units: one early divest with the redeem-shaped 186,000-unit path "
        "+ one 286,000-unit invest path"
    ),
}

# Disclosed operating scenario used by the demo/report: 100 total actions per year, with a
# rebalance counted as its own divest+invest operation class rather than hidden inside lend/redeem.
DEFAULT_OPS_PER_YEAR: Mapping[Operation, int] = {
    Operation.LEND: 40,
    Operation.REDEEM: 40,
    Operation.REBALANCE: 20,
}


def annual_fixed_cost_usd(
    chain: str,
    ops_per_year: Mapping[Operation | str, int],
    *,
    markets: Mapping[str, GasMarket],
    gas_units: Mapping[Operation, int] = ADAPTER_GAS_UNITS,
) -> float:
    market = markets.get(chain)
    if market is None:
        raise ValueError(f"COULD NOT VERIFY — missing gas_price_wei for chain {chain}")
    total_units = 0
    for raw_operation, raw_count in ops_per_year.items():
        operation = Operation(raw_operation)
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise ValueError(f"{operation.value}: ops_per_year must be a non-negative integer")
        if operation not in gas_units or gas_units[operation] <= 0:
            raise ValueError(f"COULD NOT VERIFY — missing positive gas_units for {operation.value}")
        total_units += gas_units[operation] * raw_count
    return total_units * market.gas_price_wei / 1e18 * market.eth_usd


def effective_hurdle(
    chain: str,
    book_size: float,
    ops_per_year: Mapping[Operation | str, int],
    *,
    reserve_apy: float,
    markets: Mapping[str, GasMarket],
    gas_units: Mapping[Operation, int] = ADAPTER_GAS_UNITS,
) -> float:
    """Reserve APY plus annual fixed gas cost as a fraction of the book."""
    if not math.isfinite(book_size) or book_size <= 0:
        raise ValueError("book_size must be finite and positive")
    if not math.isfinite(reserve_apy) or reserve_apy < 0:
        raise ValueError("reserve_apy must be finite and non-negative")
    return reserve_apy + annual_fixed_cost_usd(
        chain, ops_per_year, markets=markets, gas_units=gas_units,
    ) / book_size
