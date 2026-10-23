from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


BASE = "https://api.ethplorer.io"


@dataclass(frozen=True)
class HolderEvidence:
    token_address: str
    top_holder_share: float
    holder_count: int
    endpoint: str


def parse_top_holders(payload: Any, endpoint: str) -> tuple[float, int]:
    mapping = require_mapping(payload, endpoint)
    holders = require_list(require_key(mapping, "holders", endpoint), endpoint)
    shares: list[float] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        share = holder.get("share")
        if isinstance(share, bool) or not isinstance(share, (int, float)):
            continue
        value = float(share)
        if math.isfinite(value) and 0 <= value <= 100:
            shares.append(value)
    if not shares:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY holder concentration; no numeric holder shares")
    return max(shares) / 100.0, len(shares)


class EthplorerClient:
    def __init__(self, http: HttpClient, api_key: str = "freekey", cache_ttl_minutes: float = 1_440.0) -> None:
        self.http = http
        self.api_key = api_key
        self.cache_ttl_minutes = cache_ttl_minutes

    def top_token_holders(self, token_address: str, limit: int = 10) -> HolderEvidence:
        if limit != 10:
            raise ValueError("Par's holder rubric requires exactly the top 10 holders")
        address = token_address.lower()
        url = f"{BASE}/getTopTokenHolders/{address}"
        endpoint = f"GET {url}"

        def has_holders(payload: Any) -> bool:
            try:
                parse_top_holders(payload, endpoint)
            except SourceError:
                return False
            return True

        payload = self.http.request_json(
            "ethplorer",
            f"top_token_holders_{address}",
            "GET",
            url,
            params={"apiKey": self.api_key, "limit": limit},
            validate=has_holders,
            retry_on_429=False,
            prefer_cache_minutes=self.cache_ttl_minutes,
        )
        top_holder_share, count = parse_top_holders(payload, endpoint)
        return HolderEvidence(address, top_holder_share, count, endpoint)
