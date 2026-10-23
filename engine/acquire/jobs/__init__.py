"""The acquisition jobs this deployment runs, by name."""

from __future__ import annotations

from typing import Callable

from .current_prices import CurrentPricesJob
from .fee_balances import FeeBalancesJob
from .lifi_tokens import LifiTokensJob


JOBS: dict[str, Callable[[], object]] = {
    "current-prices": CurrentPricesJob,
    "fee-balances": FeeBalancesJob,
    "lifi-tokens": LifiTokensJob,
}
