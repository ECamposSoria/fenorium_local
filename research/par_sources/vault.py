from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from engine.registry import registry

from .http import HttpClient, SourceError, require_mapping


# The chains the collector reads vaults and oracles on, from the chain registry. The first public
# endpoint is the primary; the rest are fallbacks.
_COLLECTED = registry().with_role("collect")
CHAIN_RPCS = {chain.chain_id: chain.rpc_public[0] for chain in _COLLECTED if chain.rpc_public}

CHAIN_RPC_FALLBACKS: dict[int, tuple[str, ...]] = {
    chain.chain_id: chain.rpc_public[1:] for chain in _COLLECTED
}

CHAIN_RPC_ENVIRONMENT = {chain.chain_id: chain.rpc_env for chain in _COLLECTED}


def endpoints_for(chain_id: int) -> tuple[str, ...]:
    """Configured RPC endpoints in deterministic environment-override-first order."""
    primary = CHAIN_RPCS.get(chain_id)
    if primary is None:
        return ()
    environment = CHAIN_RPC_ENVIRONMENT.get(chain_id)
    override = os.environ.get(environment, "").strip() if environment else ""
    configured = (override, primary, *CHAIN_RPC_FALLBACKS.get(chain_id, ()))
    return tuple(dict.fromkeys(endpoint for endpoint in configured if endpoint))


class RpcTransportError(SourceError):
    """Every configured endpoint failed for transport/capacity reasons."""


_TRANSPORT_RPC_CODES = frozenset({-32005, -32029})
_TRANSPORT_RPC_MESSAGE = re.compile(
    r"rate limit|too many requests?|limit exceeded|capacity|throttl", re.IGNORECASE,
)
_HTTP_STATUS = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


def _json_rpc_error(payload: Any) -> dict[str, Any] | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else None


def _transport_rpc_error(error: dict[str, Any]) -> bool:
    return error.get("code") in _TRANSPORT_RPC_CODES or bool(
        _TRANSPORT_RPC_MESSAGE.search(str(error.get("message") or ""))
    )


def _transport_source_error(error: SourceError) -> bool:
    message = str(error)
    normalized = message.casefold()
    if "network error" in normalized or "timed out" in normalized or "timeout" in normalized:
        return True
    match = _HTTP_STATUS.search(message)
    return bool(match and (int(match.group(1)) == 429 or int(match.group(1)) >= 500))


def _rpc_error_reason(error: dict[str, Any]) -> str:
    code = error.get("code")
    message = str(error.get("message") or "JSON-RPC error")
    return f"JSON-RPC error {code}: {message}" if code is not None else f"JSON-RPC error: {message}"


def request_rpc_json(
    http: HttpClient,
    chain_id: int,
    rpc_endpoints: tuple[str, ...],
    *,
    source: str = "ethereum_rpc",
    cache_name: str,
    method: str,
    params: list[Any],
    operation: str,
    validate: Callable[[Any, str], bool],
    backoff_seconds: float = 0.25,
    prefer_cache_minutes: float | None = None,
) -> tuple[Any, str]:
    """Return the first clean RPC answer, rotating only on transport failures.

    The validator remembers JSON-RPC error bodies rejected by ``HttpClient``. This matters because
    a public RPC can report a throttle under HTTP 200; accepting or losing that body would turn a
    capacity failure into apparent on-chain evidence.
    """
    if not rpc_endpoints:
        raise SourceError(f"no RPC configured for chain {chain_id}")

    last_reason = "unknown transport failure"
    for index, rpc_url in enumerate(rpc_endpoints):
        endpoint = f"POST {rpc_url} {operation}"
        rejected_rpc_error: dict[str, Any] | None = None
        records = getattr(http, "records", ())
        records_before = len(records) if isinstance(records, list) else 0

        def clean_payload(payload: Any) -> bool:
            nonlocal rejected_rpc_error
            rejected_rpc_error = _json_rpc_error(payload)
            if rejected_rpc_error is not None:
                return False
            return validate(payload, endpoint)

        try:
            payload = http.request_json(
                source, cache_name, "POST", rpc_url,
                json_body={
                    "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
                },
                validate=clean_payload,
                prefer_cache_minutes=prefer_cache_minutes,
            )
        except SourceError as exc:
            if rejected_rpc_error is not None:
                reason = _rpc_error_reason(rejected_rpc_error)
                if not _transport_rpc_error(rejected_rpc_error):
                    raise SourceError(f"{endpoint}: {reason}") from exc
            elif _transport_source_error(exc):
                reason = str(exc)
            else:
                raise
        else:
            records = getattr(http, "records", ())
            recent_record = (
                records[-1] if isinstance(records, list) and len(records) > records_before else None
            )
            fallback_reason = getattr(recent_record, "fallback_reason", None)
            # A live failure followed by HttpClient's cache fallback is not a clean answer from
            # this endpoint. Rotate transport failures; surface semantic failures. Offline and
            # prefer-cache reads have no fallback_reason and remain valid cache operations.
            if rejected_rpc_error is not None:
                reason = _rpc_error_reason(rejected_rpc_error)
                if not _transport_rpc_error(rejected_rpc_error):
                    raise SourceError(f"{endpoint}: {reason}")
            elif fallback_reason:
                reason = str(fallback_reason)
                if not _transport_source_error(SourceError(reason)):
                    raise SourceError(f"{endpoint}: {reason}")

            if rejected_rpc_error is not None or fallback_reason:
                last_reason = reason
                if index + 1 < len(rpc_endpoints) and backoff_seconds > 0:
                    time.sleep(min(2.0, backoff_seconds * (2**index)))
                continue

            rpc_error = _json_rpc_error(payload)
            if rpc_error is None:
                if not validate(payload, endpoint):
                    raise SourceError(f"{endpoint}: COULD NOT VERIFY; unusable contract answer")
                return payload, endpoint
            reason = _rpc_error_reason(rpc_error)
            if not _transport_rpc_error(rpc_error):
                raise SourceError(f"{endpoint}: {reason}")

        last_reason = reason
        if index + 1 < len(rpc_endpoints) and backoff_seconds > 0:
            time.sleep(min(2.0, backoff_seconds * (2**index)))

    raise RpcTransportError(
        f"all {len(rpc_endpoints)} endpoints failed for chain {chain_id}: {last_reason}"
    )

TOTAL_ASSETS = "0x01e1d114"
ASSET = "0x38d52e0f"
TOTAL_SUPPLY = "0x18160ddd"
DECIMALS = "0x313ce567"
CONVERT_TO_ASSETS = "0x07a2d13a"
STETH_PER_TOKEN = "0x035faf82"
SYMBOL = "0x95d89b41"


@dataclass(frozen=True)
class VaultBacking:
    """Backing read straight off the vault, with the token its quantity is denominated in.

    The caller prices `unit_token`; this deliberately returns a quantity rather than a dollar
    figure so that the price and its provenance stay the caller's evidence, not ours.
    """

    vault_address: str
    kind: str
    units: float
    unit_token: str
    unit_decimals: int
    endpoint: str


def decode_uint(payload: Any, endpoint: str, field: str) -> int:
    mapping = require_mapping(payload, endpoint)
    result = mapping.get("result")
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY {field}; missing hex result")
    try:
        return int(result, 16)
    except ValueError as exc:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY {field}; malformed hex") from exc


def decode_address(payload: Any, endpoint: str, field: str) -> str:
    mapping = require_mapping(payload, endpoint)
    result = mapping.get("result")
    if not isinstance(result, str) or len(result) != 66:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY {field}; not a single ABI word")
    address = "0x" + result[-40:]
    if int(address, 16) == 0:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY {field}; zero address")
    return address.lower()


def decode_decimals(payload: Any, endpoint: str) -> int:
    value = decode_uint(payload, endpoint, "decimals")
    if not 0 <= value <= 36:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY decimals; {value} is out of range")
    return value


class VaultClient:
    """Reads a vault token's own backing, for assets no stablecoin issuer registry covers.

    Strategy and vault tokens (mHYPER, mixUSDC, yoUSD, eEARN, tmvUSDC...) have no issuer in
    DefiLlama's stablecoin registry, and matching them by name against its protocol list is
    ambiguous in exactly the wrong direction: `PRIME` resolves to both DeltaPrime and Echelon
    Prime, and the on-chain vault turned out to be neither. The contract is unambiguous.
    """

    def __init__(
        self,
        http: HttpClient,
        rpc_urls: dict[int, str] | None = None,
        rpc_fallbacks: dict[int, tuple[str, ...]] | None = None,
        *,
        backoff_seconds: float = 0.25,
    ) -> None:
        self.http = http
        self.rpc_urls = dict(CHAIN_RPCS if rpc_urls is None else rpc_urls)
        self.rpc_fallbacks = dict(
            CHAIN_RPC_FALLBACKS if rpc_fallbacks is None and rpc_urls is None
            else (rpc_fallbacks or {})
        )
        self.backoff_seconds = backoff_seconds

    def _endpoints(self, chain_id: int) -> tuple[str, ...]:
        primary = self.rpc_urls.get(chain_id)
        if primary is None:
            raise SourceError(f"COULD NOT VERIFY vault backing: no RPC configured for chain {chain_id}")
        return (primary, *self.rpc_fallbacks.get(chain_id, ()))

    def _call(self, chain_id: int, address: str, selector: str, field: str) -> tuple[Any, str]:
        target = address.lower()
        return request_rpc_json(
            self.http, chain_id, self._endpoints(chain_id),
            source="vault_rpc",
            cache_name=f"vault_{chain_id}_{target}_{field}",
            method="eth_call",
            params=[{"to": address, "data": selector}, "latest"],
            operation=f"eth_call {target} {field}",
            validate=lambda candidate, _endpoint: isinstance(candidate, dict)
            and isinstance(candidate.get("result"), str),
            backoff_seconds=self.backoff_seconds,
        )

    def _decimals(self, chain_id: int, address: str) -> int:
        payload, endpoint = self._call(chain_id, address, DECIMALS, "decimals()")
        return decode_decimals(payload, endpoint)

    def backing(self, chain_id: int, vault_address: str) -> VaultBacking:
        try:
            asset_payload, asset_endpoint = self._call(chain_id, vault_address, ASSET, "asset()")
            asset_address = decode_address(asset_payload, asset_endpoint, "asset()")
        except RpcTransportError:
            raise
        except SourceError:
            return self._supply_backing(chain_id, vault_address)

        payload, endpoint = self._call(chain_id, vault_address, TOTAL_ASSETS, "totalAssets()")
        raw = decode_uint(payload, endpoint, "totalAssets()")

        # totalAssets() is denominated in the underlying asset, not in vault shares. mixUSDC
        # issues 18-decimal shares against 6-decimal USDC: scaling by the vault's own decimals
        # reports $0.00 against 7.6M shares outstanding, which reads as a measurement rather
        # than as the misread it is.
        asset_decimals = self._decimals(chain_id, asset_address)
        units = raw / 10**asset_decimals
        if units <= 0:
            raise SourceError(
                f"{endpoint}: COULD NOT VERIFY protocol_tvl; totalAssets() is zero for a vault "
                f"with shares outstanding, which is a failed read rather than an empty vault"
            )
        return VaultBacking(
            vault_address.lower(), "erc4626_total_assets", units,
            asset_address, asset_decimals, endpoint,
        )

    def _supply_backing(self, chain_id: int, vault_address: str) -> VaultBacking:
        decimals = self._decimals(chain_id, vault_address)
        payload, endpoint = self._call(chain_id, vault_address, TOTAL_SUPPLY, "totalSupply()")
        raw = decode_uint(payload, endpoint, "totalSupply()")
        units = raw / 10**decimals
        if units <= 0:
            raise SourceError(
                f"{endpoint}: COULD NOT VERIFY protocol_tvl; totalSupply() is zero"
            )
        return VaultBacking(
            vault_address.lower(), "erc20_total_supply", units,
            vault_address.lower(), decimals, endpoint,
        )

    def underlying_asset(self, chain_id: int, token_address: str) -> tuple[str, int]:
        """The contract an ERC-4626 share is a claim on, and its decimals.

        This is the token declaring its own unit. `sENA` answers ENA and `sKAITO` answers KAITO,
        which is a denomination read off the chain rather than guessed from a ticker.
        """
        payload, endpoint = self._call(chain_id, token_address, ASSET, "asset()")
        asset_address = decode_address(payload, endpoint, "asset()")
        return asset_address, self._decimals(chain_id, asset_address)

    def redemption_ratio(self, chain_id: int, token_address: str) -> tuple[float, str]:
        """Units of the underlying one share redeems for, read from the token.

        This is the reference a discount is measured against. Without it a non-dollar asset has
        no par to fall from, and the 90-day ratio drawdown picks up ordinary drift: wstETH reads
        177bp of drawdown against ETH while trading 77bp below what it actually redeems for.
        """
        for selector, name, argument in (
            (CONVERT_TO_ASSETS, "convertToAssets(1e18)", f"{10**18:064x}"),
            (STETH_PER_TOKEN, "stEthPerToken()", ""),
        ):
            try:
                payload, endpoint = self._call(
                    chain_id, token_address, selector + argument, name,
                )
                raw = decode_uint(payload, endpoint, name)
            except RpcTransportError:
                raise
            except SourceError:
                continue
            if raw > 0:
                return raw / 10**18, endpoint
        raise SourceError(
            f"COULD NOT VERIFY redemption_ratio: {token_address} on chain {chain_id} exposes "
            f"neither convertToAssets nor stEthPerToken"
        )

    def symbol(self, chain_id: int, token_address: str) -> str:
        payload, endpoint = self._call(chain_id, token_address, SYMBOL, "symbol()")
        mapping = require_mapping(payload, endpoint)
        result = mapping.get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            raise SourceError(f"{endpoint}: COULD NOT VERIFY symbol")
        try:
            raw = bytes.fromhex(result[2:])
            offset = int.from_bytes(raw[:32], "big")
            length = int.from_bytes(raw[offset:offset + 32], "big")
            value = raw[offset + 32:offset + 32 + length].decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError(f"{endpoint}: COULD NOT VERIFY symbol; malformed ABI string") from exc
        if not value:
            raise SourceError(f"{endpoint}: COULD NOT VERIFY symbol; empty")
        return value
