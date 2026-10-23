from __future__ import annotations

from typing import Any

from .http import HttpClient, require_key, require_list, require_mapping


class TermMaxClient:
    BASE = "https://api.termmax.ts.finance"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _get_data(self, route: str, name: str, params: dict[str, Any]) -> Any:
        url = f"{self.BASE}{route}"
        payload = require_mapping(self.http.request_json(
            "termmax",
            name,
            "GET",
            url,
            params=params,
            validate=lambda candidate: isinstance(candidate, dict) and candidate.get("data") is not None,
        ), url)
        return require_key(payload, "data", url)

    def market_config_list(self, chain_id: int = 1) -> list[dict[str, Any]]:
        data = require_mapping(self._get_data("/market/config/list", f"market_config_list_{chain_id}", {"chainId": chain_id}), "/market/config/list")
        return [require_mapping(item, "/market/config/list") for item in require_list(require_key(data, "markets", "/market/config/list"), "/market/config/list")]

    def asset_configs(self, chain_id: int = 1) -> list[dict[str, Any]]:
        """Token metadata (decimals, oracle price) from the same cached config payload."""
        data = require_mapping(self._get_data("/market/config/list", f"market_config_list_{chain_id}", {"chainId": chain_id}), "/market/config/list")
        return [require_mapping(item, "/market/config/list") for item in require_list(require_key(data, "assetConfigs", "/market/config/list"), "/market/config/list")]

    def gt_configs(self, chain_id: int = 1) -> list[dict[str, Any]]:
        data = require_mapping(self._get_data("/market/config/list", f"market_config_list_{chain_id}", {"chainId": chain_id}), "/market/config/list")
        return [require_mapping(item, "/market/config/list") for item in require_list(require_key(data, "gtConfigs", "/market/config/list"), "/market/config/list")]

    def global_config(self, chain_id: int = 1) -> dict[str, Any]:
        data = require_mapping(self._get_data("/market/config/list", f"market_config_list_{chain_id}", {"chainId": chain_id}), "/market/config/list")
        return require_mapping(require_key(data, "globalConfig", "/market/config/list"), "/market/config/list")

    def market_list_with_collection(self, chain_id: int = 1) -> list[dict[str, Any]]:
        data = require_mapping(self._get_data("/v2/market/list-with-collection", f"market_list_with_collection_{chain_id}", {"chainId": chain_id}), "/v2/market/list-with-collection")
        return [require_mapping(item, "/v2/market/list-with-collection") for item in require_list(require_key(data, "collections", "/v2/market/list-with-collection"), "/v2/market/list-with-collection")]

    def market_info_orders(self, chain_id: int, market_address: str, version: str) -> list[dict[str, Any]]:
        data = self._get_data("/market/info/orders", f"market_info_orders_{chain_id}_{market_address}", {
            "chainId": chain_id, "marketAddress": market_address, "version": version,
        })
        return [require_mapping(item, "/market/info/orders") for item in require_list(data, "/market/info/orders")]

    def taker_quote(self, side: str, chain_id: int, market_address: str, in_amount: str) -> list[dict[str, Any]]:
        if side not in {"buy-ft", "sell-ft", "sell-xt"}:
            raise ValueError("unsupported TermMax taker side")
        route = f"/taker/order/aggregator/{side}"
        data = self._get_data(route, f"taker_{side}_{chain_id}_{market_address}_{in_amount}", {
            "chainId": chain_id, "marketAddress": market_address, "inAmount": in_amount,
        })
        return [require_mapping(item, route) for item in require_list(data, route)]
