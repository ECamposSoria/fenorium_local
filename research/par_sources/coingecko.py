from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, SourceError, require_mapping


BASE = "https://api.coingecko.com/api/v3"


@dataclass(frozen=True)
class CoinGeckoPricePoint:
    timestamp: int
    price: float


def parse_market_chart(payload: Any, endpoint: str, *, minimum_points: int) -> tuple[CoinGeckoPricePoint, ...]:
    mapping = require_mapping(payload, endpoint)
    rows = mapping.get("prices")
    if not isinstance(rows, list):
        raise SourceError(f"{endpoint}: COULD NOT VERIFY price history; response lost prices")
    points: dict[int, CoinGeckoPricePoint] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        stamp, price = row[0], row[1]
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            continue
        value = float(price)
        if not math.isfinite(value) or value <= 0:
            continue
        seconds = int(float(stamp) / 1000.0)
        points[seconds] = CoinGeckoPricePoint(seconds, value)
    ordered = tuple(points[stamp] for stamp in sorted(points))
    if len(ordered) < minimum_points:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY price history; {len(ordered)} usable observations, "
            f"need {minimum_points}"
        )
    return ordered


class CoinGeckoClient:
    """Keyless public price client; suitable only for paced, cached research pulls."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _market_chart(self, path: str, cache_name: str, start: int, end: int,
                      minimum_points: int) -> tuple[CoinGeckoPricePoint, ...]:
        url = f"{BASE}{path}"
        endpoint = f"GET {url}"

        def has_history(payload: Any) -> bool:
            try:
                parse_market_chart(payload, endpoint, minimum_points=minimum_points)
            except SourceError:
                return False
            return True

        payload = self.http.request_json(
            "coingecko", cache_name, "GET", url,
            params={"vs_currency": "usd", "from": start, "to": end, "precision": "full"},
            validate=has_history, prefer_cache_minutes=1_440,
        )
        return parse_market_chart(payload, endpoint, minimum_points=minimum_points)

    # The cache name ignores the requested range, so a caller asking for a different span passes
    # its own tag; otherwise a cached 90-day series answers a longer request.
    def contract_market_chart(self, platform: str, address: str, start: int, end: int,
                              *, minimum_points: int = 60, cache_tag: str = "90d") -> tuple[CoinGeckoPricePoint, ...]:
        normalized = address.lower()
        return self._market_chart(
            f"/coins/{platform}/contract/{normalized}/market_chart/range",
            f"contract_market_chart_{cache_tag}_{platform}_{normalized}", start, end, minimum_points,
        )

    def coin_market_chart(self, coin_id: str, start: int, end: int,
                          *, minimum_points: int = 60, cache_tag: str = "90d") -> tuple[CoinGeckoPricePoint, ...]:
        return self._market_chart(
            f"/coins/{coin_id}/market_chart/range",
            f"coin_market_chart_{cache_tag}_{coin_id}", start, end, minimum_points,
        )

    def contract_spot(self, platform: str, address: str) -> tuple[float, str]:
        normalized = address.lower()
        url = f"{BASE}/simple/token_price/{platform}"
        endpoint = f"GET {url}"

        def has_price(payload: Any) -> bool:
            entry = payload.get(normalized) if isinstance(payload, dict) else None
            price = entry.get("usd") if isinstance(entry, dict) else None
            return (isinstance(price, (int, float)) and not isinstance(price, bool)
                    and math.isfinite(float(price)) and float(price) > 0)

        payload = require_mapping(self.http.request_json(
            "coingecko", f"spot_{platform}_{normalized}", "GET", url,
            params={"contract_addresses": normalized, "vs_currencies": "usd",
                    "include_last_updated_at": "true", "precision": "full"},
            validate=has_price, prefer_cache_minutes=30,
        ), endpoint)
        return float(payload[normalized]["usd"]), endpoint
