from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .http import SourceError


class QualityTier(StrEnum):
    TIER_A = "TIER_A"
    TIER_B = "TIER_B"
    TIER_C = "TIER_C"


@dataclass(frozen=True)
class Instrument:
    venue: str
    chain_id: int
    chain: str
    name: str
    symbol: str
    address: str
    maturity: datetime
    implied_apy: float
    liquidity_usd: float | None
    pt_address: str
    underlying_address: str | None
    underlying_symbol: str | None
    protocol: str | None
    source_endpoint: str
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["maturity"] = self.maturity.isoformat()
        return result


def strip_chain_prefix(value: Any, chain_id: int, endpoint: str, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise SourceError(f"{endpoint}: `{key}` must be a non-empty string")
    prefix = f"{chain_id}-"
    return value[len(prefix) :] if value.startswith(prefix) else value


def parse_iso(value: Any, endpoint: str, key: str) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"{endpoint}: `{key}` must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"{endpoint}: invalid `{key}` ISO-8601 value {value!r}") from exc


def number(value: Any, endpoint: str, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceError(f"{endpoint}: `{key}` must be numeric")
    return float(value)
