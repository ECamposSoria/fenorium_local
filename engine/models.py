from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Job(StrEnum):
    LEND_FIXED = "LEND_FIXED"
    DISPERSION = "DISPERSION"
    CARRY = "CARRY"
    RESERVE = "RESERVE"


@dataclass(frozen=True)
class SlippagePoint:
    size_usd: float
    price_impact: float
    timestamp: datetime
    source: str


@dataclass(frozen=True)
class MarketRecord:
    venue: str
    chain: str
    instrument: str
    underlying: str | None
    maturity: datetime | None
    implied_apy: float | None
    liquidity_usd: float | None
    executable_capacity_usd: float | None
    slippage_curve: tuple[SlippagePoint, ...]
    source_timestamp: datetime
    source_endpoint: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "chain": self.chain,
            "instrument": self.instrument,
            "underlying": self.underlying,
            "maturity": self.maturity.isoformat() if self.maturity is not None else None,
            "implied_apy": self.implied_apy,
            "liquidity_usd": self.liquidity_usd,
            "executable_capacity_usd": self.executable_capacity_usd,
            "slippage_curve": [
                {
                    "size_usd": point.size_usd,
                    "price_impact": point.price_impact,
                    "timestamp": point.timestamp.isoformat(),
                    "source": point.source,
                }
                for point in self.slippage_curve
            ],
            "source_timestamp": self.source_timestamp.isoformat(),
            "source_endpoint": self.source_endpoint,
            "metadata": self.metadata,
        }
