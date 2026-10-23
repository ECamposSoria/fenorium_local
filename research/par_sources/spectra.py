from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from engine.registry import registry

from .http import HttpClient, SourceError, require_key, require_list, require_mapping
from .normalize import Instrument, number


# Spectra names each network it serves; the registry says which chain that is.
NETWORK_CHAINS = {chain.spectra: (chain.chain_id, chain.label) for chain in registry().chains if chain.spectra}


class SpectraClient:
    BASE = "https://app.spectra.finance/api/v1"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def pools(self, network: str) -> list[dict[str, Any]]:
        url = f"{self.BASE}/{network}/pools"
        payload = self.http.request_json("spectra", f"pools_{network}", "GET", url)
        return [require_mapping(item, url) for item in require_list(payload, url)]

    @staticmethod
    def normalize_pools(network: str, items: list[dict[str, Any]]) -> list[Instrument]:
        endpoint = f"GET {SpectraClient.BASE}/{network}/pools"
        chain_id, chain = NETWORK_CHAINS.get(network, (0, network))
        result: list[Instrument] = []
        for item in items:
            pools = require_list(require_key(item, "pools", endpoint), endpoint)
            symbol = require_key(item, "symbol", endpoint)
            address = require_key(item, "address", endpoint)
            maturity = require_key(item, "maturity", endpoint)
            if not isinstance(symbol, str) or not isinstance(address, str) or not isinstance(maturity, (int, float)):
                raise SourceError(f"{endpoint}: pool `symbol`, `address`, or `maturity` has wrong type")
            for pool_raw in pools:
                pool = require_mapping(pool_raw, endpoint)
                liquidity = require_mapping(require_key(pool, "liquidity", endpoint), endpoint).get("usd")
                underlying = item.get("underlying")
                underlying_map = underlying if isinstance(underlying, dict) else {}
                result.append(Instrument(
                    venue="Spectra", chain_id=int(item.get("chainId", chain_id)), chain=chain,
                    name=symbol, symbol=symbol, address=str(pool.get("address", address)),
                    maturity=datetime.fromtimestamp(float(maturity), UTC),
                    implied_apy=number(require_key(pool, "impliedApy", endpoint), endpoint, "pools[].impliedApy") / 100.0,
                    liquidity_usd=None if liquidity is None else number(liquidity, endpoint, "pools[].liquidity.usd"),
                    pt_address=address, underlying_address=underlying_map.get("address"),
                    underlying_symbol=underlying_map.get("symbol"), protocol=None,
                    source_endpoint=endpoint, raw={"pt": item, "pool": pool},
                ))
        return result
