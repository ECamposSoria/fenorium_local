from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


BASE = "https://api.etherscan.io/v2/api"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_api_key() -> str | None:
    """`ETHERSCAN_API_KEY` from the environment, else from the repo's gitignored `.env`."""
    key = os.environ.get("ETHERSCAN_API_KEY")
    if key:
        return key.strip()
    if not _ENV_PATH.exists():
        return None
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == "ETHERSCAN_API_KEY":
            return value.strip().strip('"').strip("'") or None
    return None


@dataclass(frozen=True)
class EtherscanHolderEvidence:
    token_address: str
    top_holder_share: float
    holder_count: int
    endpoint: str


def parse_holder_share(holders_payload: Any, supply_payload: Any,
                       endpoint: str) -> tuple[float, int]:
    holders_mapping = require_mapping(holders_payload, endpoint)
    supply_mapping = require_mapping(supply_payload, endpoint)
    holders = require_list(require_key(holders_mapping, "result", endpoint), endpoint)
    try:
        supply = int(require_key(supply_mapping, "result", endpoint))
    except (TypeError, ValueError) as exc:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY holder concentration; invalid token supply"
        ) from exc
    quantities: list[int] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        try:
            quantity = int(holder.get("TokenHolderQuantity"))
        except (TypeError, ValueError):
            continue
        if quantity >= 0:
            quantities.append(quantity)
    if supply <= 0 or not quantities:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY holder concentration; no usable supply/holders"
        )
    share = max(quantities) / supply
    if share > 1.0 + 1e-9:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY holder concentration; holder exceeds total supply"
        )
    return min(share, 1.0), len(quantities)


class EtherscanClient:
    def __init__(self, http: HttpClient, api_key: str | None = None,
                 cache_ttl_minutes: float = 1_440.0) -> None:
        self.http = http
        self.api_key = api_key if api_key is not None else load_api_key()
        self.cache_ttl_minutes = cache_ttl_minutes

    def top_token_holders(self, chain_id: int, token_address: str) -> EtherscanHolderEvidence:
        if not self.api_key:
            raise SourceError(
                "COULD NOT VERIFY authenticated explorer: ETHERSCAN_API_KEY is absent "
                "from the environment and repo .env"
            )
        address = token_address.lower()
        endpoint = f"GET {BASE} tokenholderlist/tokensupply chain {chain_id} {address}"
        common = {"chainid": chain_id, "contractaddress": address, "apikey": self.api_key}

        def has_holders(payload: Any) -> bool:
            result = payload.get("result") if isinstance(payload, dict) else None
            return payload.get("status") == "1" and isinstance(result, list) and bool(result)

        holders = self.http.request_json(
            "etherscan", f"top_token_holders_{chain_id}_{address}", "GET", BASE,
            params={**common, "module": "token", "action": "tokenholderlist",
                    "page": 1, "offset": 10},
            validate=has_holders, prefer_cache_minutes=self.cache_ttl_minutes,
        )

        def has_supply(payload: Any) -> bool:
            result = payload.get("result") if isinstance(payload, dict) else None
            try:
                return payload.get("status") == "1" and int(result) > 0
            except (AttributeError, TypeError, ValueError):
                return False

        supply = self.http.request_json(
            "etherscan", f"token_supply_{chain_id}_{address}", "GET", BASE,
            params={**common, "module": "stats", "action": "tokensupply"},
            validate=has_supply, prefer_cache_minutes=self.cache_ttl_minutes,
        )
        share, count = parse_holder_share(holders, supply, endpoint)
        return EtherscanHolderEvidence(address, share, count, endpoint)
