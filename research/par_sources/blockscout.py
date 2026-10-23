from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.registry import registry

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


# `blockscout_api` in the chain registry. Verified serving holder lists 2026-08-23. BSC and
# HyperEVM have none: bsc.blockscout.com returns 404 and no Blockscout instance exists for HyperEVM,
# so holder concentration on those chains has no free source and stays unresolved rather than guessed.
CHAIN_APIS = {chain.chain_id: chain.blockscout_api for chain in registry().chains if chain.blockscout_api}


@dataclass(frozen=True)
class BlockscoutHolderEvidence:
    token_address: str
    top_holder_share: float
    holder_count: int
    endpoint: str


def parse_holder_share(holders_payload: Any, token_payload: Any, endpoint: str) -> tuple[float, int]:
    holders_mapping = require_mapping(holders_payload, endpoint)
    token_mapping = require_mapping(token_payload, endpoint)
    holders = require_list(require_key(holders_mapping, "result", endpoint), endpoint)
    token = require_mapping(require_key(token_mapping, "result", endpoint), endpoint)
    total_supply = token.get("totalSupply")
    try:
        supply = int(total_supply)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY holder concentration; invalid totalSupply") from exc
    values: list[int] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        try:
            value = int(holder.get("value"))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            values.append(value)
    if supply <= 0 or not values:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY holder concentration; no usable supply/holders")
    share = max(values) / supply
    if share > 1.0 + 1e-9:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY holder concentration; holder exceeds total supply")
    return min(share, 1.0), len(values)


class BlockscoutClient:
    def __init__(self, http: HttpClient, cache_ttl_minutes: float = 1_440.0) -> None:
        self.http = http
        self.cache_ttl_minutes = cache_ttl_minutes

    def top_token_holders(self, chain_id: int, token_address: str) -> BlockscoutHolderEvidence:
        base = CHAIN_APIS.get(chain_id)
        if base is None:
            raise SourceError(f"COULD NOT VERIFY: no verified Blockscout holder API for chain {chain_id}")
        address = token_address.lower()
        endpoint = f"GET {base} token.getTokenHolders/getToken {address}"

        def has_holders(payload: Any) -> bool:
            if not isinstance(payload, dict):
                return False
            result = payload.get("result")
            return payload.get("status") == "1" and isinstance(result, list) and bool(result)

        holders = self.http.request_json(
            "blockscout",
            f"top_token_holders_{chain_id}_{address}",
            "GET",
            base,
            params={
                "module": "token",
                "action": "getTokenHolders",
                "contractaddress": address,
                "page": 1,
                "offset": 10,
            },
            validate=has_holders,
            prefer_cache_minutes=self.cache_ttl_minutes,
        )

        def has_supply(payload: Any) -> bool:
            if not isinstance(payload, dict):
                return False
            result = payload.get("result")
            return payload.get("status") == "1" and isinstance(result, dict) and bool(result.get("totalSupply"))

        token = self.http.request_json(
            "blockscout",
            f"token_info_{chain_id}_{address}",
            "GET",
            base,
            params={"module": "token", "action": "getToken", "contractaddress": address},
            validate=has_supply,
            prefer_cache_minutes=self.cache_ttl_minutes,
        )
        share, count = parse_holder_share(holders, token, endpoint)
        return BlockscoutHolderEvidence(address, share, count, endpoint)
