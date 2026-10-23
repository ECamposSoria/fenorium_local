from __future__ import annotations

from typing import Any

from .http import HttpClient, SourceError, require_key, require_list, require_mapping
from .normalize import Instrument, number, parse_iso, strip_chain_prefix


class PendleClient:
    BASE = "https://api-v2.pendle.finance/core"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def markets_active(self, chain_id: int) -> list[dict[str, Any]]:
        url = f"{self.BASE}/v1/{chain_id}/markets/active"
        payload = require_mapping(self.http.request_json("pendle", f"markets_active_{chain_id}", "GET", url), url)
        return [require_mapping(item, url) for item in require_list(require_key(payload, "markets", url), url)]

    def assets_all(self, chain_id: int) -> list[dict[str, Any]]:
        url = f"{self.BASE}/v1/{chain_id}/assets/all"
        payload = self.http.request_json("pendle", f"assets_all_{chain_id}", "GET", url)
        return [require_mapping(item, url) for item in require_list(payload, url)]

    def ohlcv(self, chain_id: int, token_address: str, time_frame: str, start: str, end: str) -> dict[str, Any]:
        if time_frame not in {"hour", "day"}:
            raise ValueError("time_frame must be 'hour' or 'day'")
        url = f"{self.BASE}/v4/{chain_id}/prices/{token_address}/ohlcv"
        name = f"ohlcv_{chain_id}_{token_address}_{time_frame}_{start[:10]}_{end[:10]}"
        return require_mapping(self.http.request_json("pendle", name, "GET", url, params={
            "time_frame": time_frame, "timestamp_start": start, "timestamp_end": end,
        }), url)

    def convert_quote(self, chain_id: int, token_in: str, amount: str, token_out: str, name: str) -> dict[str, Any]:
        url = f"{self.BASE}/v3/sdk/{chain_id}/convert"
        payload = self.http.request_json("pendle", f"convert_{name}", "POST", url, json_body={
            "inputs": [{"token": token_in, "amount": amount}],
            "outputs": [token_out], "slippage": 0.01, "enableAggregator": True,
        }, expected_statuses=(201,))
        return require_mapping(payload, url)

    def limit_order_book(self, chain_id: int, market: str, precision_decimal: int = 3) -> dict[str, Any]:
        url = f"{self.BASE}/v2/limit-orders/book/{chain_id}"
        return require_mapping(self.http.request_json("pendle", f"limit_order_book_{chain_id}_{market}", "GET", url,
            params={"market": market, "precisionDecimal": precision_decimal}), url)

    @staticmethod
    def normalize_market(chain_id: int, chain: str, market: dict[str, Any]) -> Instrument:
        endpoint = f"GET {PendleClient.BASE}/v1/{chain_id}/markets/active"
        details = require_mapping(require_key(market, "details", endpoint), endpoint)
        name = require_key(market, "name", endpoint)
        address = require_key(market, "address", endpoint)
        if not isinstance(name, str) or not isinstance(address, str):
            raise SourceError(f"{endpoint}: `name` and `address` must be strings")
        pt = strip_chain_prefix(require_key(market, "pt", endpoint), chain_id, endpoint, "pt")
        underlying = strip_chain_prefix(require_key(market, "underlyingAsset", endpoint), chain_id, endpoint, "underlyingAsset")
        liquidity = details.get("liquidity")
        return Instrument(
            venue="Pendle", chain_id=chain_id, chain=chain, name=name,
            symbol=name if name.upper().startswith("PT-") else f"PT-{name}", address=address,
            maturity=parse_iso(require_key(market, "expiry", endpoint), endpoint, "expiry"),
            implied_apy=number(require_key(details, "impliedApy", endpoint), endpoint, "details.impliedApy"),
            liquidity_usd=None if liquidity is None else number(liquidity, endpoint, "details.liquidity"),
            pt_address=pt, underlying_address=underlying, underlying_symbol=None,
            protocol=None, source_endpoint=endpoint, raw=market,
        )
