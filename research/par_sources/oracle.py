from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .http import HttpClient, SourceError, require_mapping


ETHEREUM_RPC = "https://ethereum-rpc.publicnode.com"
LATEST_ROUND_DATA = "0xfeaf968c"
DESCRIPTION = "0x7284e416"


@dataclass(frozen=True)
class OracleEvidence:
    feed_address: str
    answer: int
    updated_at: datetime
    heartbeat_seconds: float
    heartbeat_source: str
    endpoint: str


@dataclass(frozen=True)
class OracleRound:
    feed_address: str
    answer: int
    updated_at: datetime | None
    endpoint: str


def decode_latest_round_data(payload: Any, endpoint: str) -> tuple[int, datetime | None]:
    mapping = require_mapping(payload, endpoint)
    result = mapping.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise SourceError(f"{endpoint}: COULD NOT VERIFY latestRoundData; missing hex result")
    encoded = result[2:]
    if len(encoded) != 5 * 64:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY latestRoundData; expected five ABI words, got {len(encoded) // 64}"
        )
    try:
        words = [int(encoded[offset:offset + 64], 16) for offset in range(0, len(encoded), 64)]
    except ValueError as exc:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY latestRoundData; malformed ABI hex") from exc
    answer = words[1] - 2**256 if words[1] >= 2**255 else words[1]
    updated_at = words[3]
    if answer <= 0 or updated_at < 0:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY latestRoundData; non-positive answer or negative updatedAt")
    if updated_at == 0:
        return answer, None
    try:
        timestamp = datetime.fromtimestamp(updated_at, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY latestRoundData; invalid updatedAt") from exc
    return answer, timestamp


def decode_abi_string(payload: Any, endpoint: str) -> str:
    mapping = require_mapping(payload, endpoint)
    result = mapping.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise SourceError(f"{endpoint}: COULD NOT VERIFY description; missing hex result")
    encoded = result[2:]
    try:
        raw = bytes.fromhex(encoded)
        if len(raw) < 64:
            raise ValueError("short ABI result")
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 > len(raw):
            raise ValueError("invalid ABI offset")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        start = offset + 32
        if start + length > len(raw):
            raise ValueError("invalid ABI length")
        value = raw[start:start + length].decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY description; malformed ABI string") from exc
    if not value:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY description; empty string")
    return value


def decode_block_timestamp(payload: Any, endpoint: str) -> datetime:
    mapping = require_mapping(payload, endpoint)
    result = mapping.get("result")
    timestamp = result.get("timestamp") if isinstance(result, dict) else None
    if not isinstance(timestamp, str) or not timestamp.startswith("0x"):
        raise SourceError(f"{endpoint}: COULD NOT VERIFY block timestamp")
    try:
        return datetime.fromtimestamp(int(timestamp, 16), UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY block timestamp") from exc


class OracleClient:
    def __init__(self, http: HttpClient, rpc_url: str = ETHEREUM_RPC) -> None:
        self.http = http
        self.rpc_url = rpc_url

    def probe_latest_round_data(
        self,
        feed_address: str,
        *,
        cache_name: str | None = None,
    ) -> OracleRound:
        address = feed_address.lower()
        endpoint = f"POST {self.rpc_url} eth_call {address} latestRoundData()"

        def has_round(payload: Any) -> bool:
            try:
                decode_latest_round_data(payload, endpoint)
            except SourceError:
                return False
            return True

        payload = self.http.request_json(
            "ethereum_rpc",
            cache_name or f"oracle_latest_round_{address}",
            "POST",
            self.rpc_url,
            json_body={
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": feed_address, "data": LATEST_ROUND_DATA}, "latest"],
                "id": 1,
            },
            validate=has_round,
        )
        answer, updated_at = decode_latest_round_data(payload, endpoint)
        return OracleRound(feed_address, answer, updated_at, endpoint)

    def latest_round_data(
        self,
        feed_address: str,
        *,
        heartbeat_seconds: float,
        heartbeat_source: str,
        cache_name: str | None = None,
    ) -> OracleEvidence:
        if heartbeat_seconds <= 0 or not heartbeat_source:
            raise ValueError("oracle heartbeat and its provenance are required")
        round_data = self.probe_latest_round_data(feed_address, cache_name=cache_name)
        if round_data.updated_at is None:
            raise SourceError(
                f"{round_data.endpoint}: COULD NOT VERIFY oracle_model_risk; updatedAt is zero"
            )
        return OracleEvidence(
            feed_address=feed_address,
            answer=round_data.answer,
            updated_at=round_data.updated_at,
            heartbeat_seconds=float(heartbeat_seconds),
            heartbeat_source=heartbeat_source,
            endpoint=round_data.endpoint,
        )

    def description(self, feed_address: str, *, cache_name: str | None = None) -> str:
        address = feed_address.lower()
        endpoint = f"POST {self.rpc_url} eth_call {address} description()"

        def has_description(payload: Any) -> bool:
            try:
                decode_abi_string(payload, endpoint)
            except SourceError:
                return False
            return True

        payload = self.http.request_json(
            "ethereum_rpc", cache_name or f"oracle_description_{address}", "POST", self.rpc_url,
            json_body={
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": feed_address, "data": DESCRIPTION}, "latest"], "id": 1,
            },
            validate=has_description,
        )
        return decode_abi_string(payload, endpoint)

    def latest_block_timestamp(self, *, cache_name: str = "latest_block") -> datetime:
        endpoint = f"POST {self.rpc_url} eth_getBlockByNumber latest"

        def has_timestamp(payload: Any) -> bool:
            try:
                decode_block_timestamp(payload, endpoint)
            except SourceError:
                return False
            return True

        payload = self.http.request_json(
            "ethereum_rpc", cache_name, "POST", self.rpc_url,
            json_body={
                "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                "params": ["latest", False], "id": 1,
            },
            validate=has_timestamp,
            prefer_cache_minutes=1,
        )
        return decode_block_timestamp(payload, endpoint)
