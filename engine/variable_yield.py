"""Protocol-native variable-yield venue adapters.

DefiLlama is used only for a trailing rate observation.  Instrument identity, fund size, and
withdrawable liquidity come from protocol contracts.  A row without a positive protocol-native
depth is omitted: TVL is never substituted for liquidity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence

from .chain import ChainError, RpcTransportError, rpc_call, selector
from .rpc_endpoints import PUBLIC_RPC_ENDPOINTS, RPC_ENVIRONMENT, rpc_endpoints


MIN_TVL_USD = 250_000
COMET_PRICE_SCALE = 100_000_000
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
# Seventy-five small view calls keep both calldata and return payloads comfortably below common
# public-provider response limits while reducing each reserve phase to only a few round-trips.
MULTICALL_CHUNK_SIZE = 75


class SourcePayloadError(ValueError):
    """A successful transport response carried no usable source data."""


class MulticallChunkError(SourcePayloadError):
    """A whole aggregate3 chunk failed, so none of that chunk is source evidence."""


@dataclass(frozen=True, slots=True)
class VenueResult:
    rows: tuple[dict[str, Any], ...]
    error: str | None
    omitted: int
    endpoint: str
    depth_source: str
    rpc_round_trips: int = 0
    rpc_mode: str = "not applicable"
    rpc_endpoints: tuple[str, ...] = ()
    omission_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MulticallRead:
    target: str
    call_data: str
    owner: str
    field: str


@dataclass(frozen=True, slots=True)
class AaveMarket:
    chain_id: int
    chain: str
    rpc: str
    addresses_provider: str


@dataclass(frozen=True, slots=True)
class CompoundMarket:
    chain_id: int
    chain: str
    rpc: str
    deployment: str
    comet_address: str
    manifest: str


AAVE_MARKETS = (
    AaveMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e",
    ),
    AaveMarket(
        8453,
        "Base",
        "https://base-rpc.publicnode.com",
        "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D",
    ),
)


# The proxies are pinned to Compound's official deployment roots.  The adapter verifies each
# market's base token on chain; the manifest is provenance, never a source of liquidity.
COMPOUND_MARKETS = (
    CompoundMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "mainnet/usdc",
        "0xc3d688B66703497DAA19211EEdff47f25384cdc3",
        "https://github.com/compound-finance/comet/blob/main/deployments/mainnet/usdc/roots.json",
    ),
    CompoundMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "mainnet/weth",
        "0xA17581A9E3356d9A858b789D68B4d866e593aE94",
        "https://github.com/compound-finance/comet/blob/main/deployments/mainnet/weth/roots.json",
    ),
    CompoundMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "mainnet/usdt",
        "0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840",
        "https://github.com/compound-finance/comet/blob/main/deployments/mainnet/usdt/roots.json",
    ),
    CompoundMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "mainnet/wsteth",
        "0x3D0bb1ccaB520A66e607822fC55BC921738fAFE3",
        "https://github.com/compound-finance/comet/blob/main/deployments/mainnet/wsteth/roots.json",
    ),
    CompoundMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "mainnet/usds",
        "0x5D409e56D886231aDAf00c8775665AD0f9897b56",
        "https://github.com/compound-finance/comet/blob/main/deployments/mainnet/usds/roots.json",
    ),
    CompoundMarket(
        1,
        "Ethereum",
        "https://ethereum-rpc.publicnode.com",
        "mainnet/wbtc",
        "0xe85Dc543813B8c2CFEaAc371517b925a166a9293",
        "https://github.com/compound-finance/comet/blob/main/deployments/mainnet/wbtc/roots.json",
    ),
    CompoundMarket(
        8453,
        "Base",
        "https://base-rpc.publicnode.com",
        "base/usdbc",
        "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf",
        "https://github.com/compound-finance/comet/blob/main/deployments/base/usdbc/roots.json",
    ),
    CompoundMarket(
        8453,
        "Base",
        "https://base-rpc.publicnode.com",
        "base/usdc",
        "0xb125E6687d4313864e53df431d5425969c15Eb2F",
        "https://github.com/compound-finance/comet/blob/main/deployments/base/usdc/roots.json",
    ),
    CompoundMarket(
        8453,
        "Base",
        "https://base-rpc.publicnode.com",
        "base/weth",
        "0x46e6b214b524310239732D51387075E0e70970bf",
        "https://github.com/compound-finance/comet/blob/main/deployments/base/weth/roots.json",
    ),
    CompoundMarket(
        8453,
        "Base",
        "https://base-rpc.publicnode.com",
        "base/aero",
        "0x784efeB622244d2348d4F2522f8860B96fbEcE89",
        "https://github.com/compound-finance/comet/blob/main/deployments/base/aero/roots.json",
    ),
    CompoundMarket(
        8453,
        "Base",
        "https://base-rpc.publicnode.com",
        "base/usds",
        "0x2c776041CCFe903071AF44aa147368a9c8EEA518",
        "https://github.com/compound-finance/comet/blob/main/deployments/base/usds/roots.json",
    ),
)


RpcReader = Callable[[str, str, str], bytes]


class _RpcSession:
    """One collector run with endpoint rotation and exact request-attempt accounting."""

    def __init__(self, reader: RpcReader | None) -> None:
        self.reader = reader
        self.round_trips = 0
        self.used_endpoints: list[str] = []

    def _record(self, endpoint: str, chain_id: int) -> None:
        self.round_trips += 1
        public = endpoint in PUBLIC_RPC_ENDPOINTS.get(chain_id, ())
        label = endpoint if public else f"${RPC_ENVIRONMENT.get(chain_id, 'RPC_URL')}"
        if label not in self.used_endpoints:
            self.used_endpoints.append(label)

    def call(self, chain_id: int, preferred_rpc: str, to: str, data: str) -> bytes:
        if self.reader is not None:
            self._record(preferred_rpc, chain_id)
            return self.reader(preferred_rpc, to, data)

        endpoints = rpc_endpoints(chain_id)
        if not endpoints:
            raise RpcTransportError(f"no RPC endpoints configured for chain {chain_id}")
        failures: list[str] = []
        for endpoint in endpoints:
            self._record(endpoint, chain_id)
            try:
                result = rpc_call(
                    endpoint,
                    "eth_call",
                    [{"to": to, "data": data}, "latest"],
                )
            except RpcTransportError as exc:
                failures.append(str(exc))
                continue
            if not isinstance(result, str) or not result.startswith("0x"):
                raise ChainError(f"eth_call returned an unusable result against {endpoint}: {result!r}")
            try:
                raw = bytes.fromhex(result[2:])
            except ValueError as exc:
                raise ChainError(f"eth_call returned malformed hex against {endpoint}") from exc
            if not raw:
                raise ChainError(
                    f"eth_call returned empty data against {endpoint}: call reverted or no contract"
                )
            return raw
        raise RpcTransportError(
            f"all {len(endpoints)} RPC endpoints failed for chain {chain_id}: {failures[-1]}"
        )

    def multicall_available(self, chain_id: int, preferred_rpc: str) -> bool:
        # Injected fixture readers model Multicall3 directly. Only the live reader can prove
        # absence with eth_getCode; a transport failure here propagates and never authorizes fallback.
        if self.reader is not None:
            return True
        endpoints = rpc_endpoints(chain_id)
        if not endpoints:
            raise RpcTransportError(f"no RPC endpoints configured for chain {chain_id}")
        failures: list[str] = []
        for endpoint in endpoints:
            self._record(endpoint, chain_id)
            try:
                result = rpc_call(endpoint, "eth_getCode", [MULTICALL3_ADDRESS, "latest"])
            except RpcTransportError as exc:
                failures.append(str(exc))
                continue
            if not isinstance(result, str) or not result.startswith("0x"):
                raise ChainError(f"eth_getCode returned unusable data against {endpoint}: {result!r}")
            try:
                code = bytes.fromhex(result[2:])
            except ValueError as exc:
                raise ChainError(f"eth_getCode returned malformed hex against {endpoint}") from exc
            return bool(code)
        raise RpcTransportError(
            f"all {len(endpoints)} RPC endpoints failed Multicall3 availability check for "
            f"chain {chain_id}: {failures[-1]}"
        )


def _abi_uint(value: int) -> bytes:
    if not 0 <= value < 1 << 256:
        raise ValueError("ABI uint256 value is out of range")
    return value.to_bytes(32, "big")


def _calldata_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise ValueError("call data must be even-length 0x-prefixed hex")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ValueError("call data contains non-hex characters") from exc


def encode_aggregate3(calls: Sequence[MulticallRead]) -> str:
    """Encode aggregate3((address,bool,bytes)[]) with allowFailure=true for every read."""
    tuples: list[bytes] = []
    for call in calls:
        target = bytes.fromhex(_valid_address(call.target, "Multicall3 target")[2:])
        calldata = _calldata_bytes(call.call_data)
        padded = calldata + b"\0" * ((-len(calldata)) % 32)
        tuples.append(
            b"\0" * 12 + target + _abi_uint(1) + _abi_uint(96)
            + _abi_uint(len(calldata)) + padded
        )
    offsets: list[bytes] = []
    cursor = 32 * len(tuples)
    for encoded_tuple in tuples:
        offsets.append(_abi_uint(cursor))
        cursor += len(encoded_tuple)
    array = _abi_uint(len(tuples)) + b"".join(offsets) + b"".join(tuples)
    payload = _abi_uint(32) + array
    return selector("aggregate3((address,bool,bytes)[])") + payload.hex()


def _bounded_offset(raw: bytes, base: int, offset: int, field: str) -> int:
    position = base + offset
    if offset % 32 or position < 0 or position + 32 > len(raw):
        raise SourcePayloadError(f"Multicall3 returned malformed {field} offset")
    return position


def decode_aggregate3(
    raw: bytes, *, expected_count: int,
) -> tuple[tuple[bool, bytes], ...]:
    """Strictly decode aggregate3's dynamic Result[] return value."""
    if len(raw) < 64 or len(raw) % 32:
        raise SourcePayloadError("Multicall3 returned malformed aggregate3 data")
    array_start = _bounded_offset(raw, 0, int.from_bytes(raw[:32], "big"), "result array")
    count = int.from_bytes(raw[array_start:array_start + 32], "big")
    if count != expected_count:
        raise SourcePayloadError(
            f"Multicall3 returned {count} results for {expected_count} calls"
        )
    offsets_start = array_start + 32
    if offsets_start + count * 32 > len(raw):
        raise SourcePayloadError("Multicall3 returned truncated result offsets")
    decoded: list[tuple[bool, bytes]] = []
    for index in range(count):
        offset = int.from_bytes(raw[offsets_start + index * 32:offsets_start + (index + 1) * 32], "big")
        tuple_start = _bounded_offset(raw, offsets_start, offset, f"result {index}")
        if tuple_start + 64 > len(raw):
            raise SourcePayloadError(f"Multicall3 returned truncated result {index}")
        success_word = int.from_bytes(raw[tuple_start:tuple_start + 32], "big")
        if success_word not in (0, 1):
            raise SourcePayloadError(f"Multicall3 returned malformed success flag for result {index}")
        data_offset = int.from_bytes(raw[tuple_start + 32:tuple_start + 64], "big")
        data_start = _bounded_offset(raw, tuple_start, data_offset, f"result {index} data")
        length = int.from_bytes(raw[data_start:data_start + 32], "big")
        body_start = data_start + 32
        body_end = body_start + length
        if body_end > len(raw):
            raise SourcePayloadError(f"Multicall3 returned truncated data for result {index}")
        decoded.append((bool(success_word), raw[body_start:body_end]))
    return tuple(decoded)


def execute_multicall_chunks(
    reader: RpcReader,
    rpc: str,
    calls: Sequence[MulticallRead],
    *,
    chunk_size: int = MULTICALL_CHUNK_SIZE,
) -> tuple[tuple[bool, bytes], ...]:
    """Execute complete chunks; any outer-call failure invalidates the source batch."""
    if chunk_size <= 0:
        raise ValueError("Multicall3 chunk size must be positive")
    results: list[tuple[bool, bytes]] = []
    for start in range(0, len(calls), chunk_size):
        chunk = calls[start:start + chunk_size]
        chunk_number = start // chunk_size + 1
        try:
            raw = reader(rpc, MULTICALL3_ADDRESS, encode_aggregate3(chunk))
            results.extend(decode_aggregate3(raw, expected_count=len(chunk)))
        except Exception as exc:
            raise MulticallChunkError(
                f"Multicall3 chunk {chunk_number} failed as a source read: {exc}"
            ) from exc
    return tuple(results)


def complete_multicall_groups(
    calls: Sequence[MulticallRead],
    results: Sequence[tuple[bool, bytes]],
) -> tuple[dict[str, dict[str, bytes]], list[str]]:
    """Return only owners whose every sub-call succeeded, with explicit failure reasons."""
    if len(calls) != len(results):
        raise SourcePayloadError(
            f"Multicall3 call/result mismatch: {len(calls)} calls, {len(results)} results"
        )
    grouped: dict[str, dict[str, bytes]] = {}
    failed: dict[str, list[str]] = {}
    for call, (success, return_data) in zip(calls, results, strict=True):
        if not success:
            failed.setdefault(call.owner, []).append(call.field)
            continue
        grouped.setdefault(call.owner, {})[call.field] = return_data
    for owner in failed:
        grouped.pop(owner, None)
    errors = [
        f"{owner}: Multicall3 sub-call failed for {', '.join(fields)}"
        for owner, fields in failed.items()
    ]
    return grouped, errors


def validate_yield_payload(payload: Any) -> list[dict[str, Any]]:
    """Require a non-empty DefiLlama pool list even when HTTP returned 200."""
    if not isinstance(payload, dict):
        raise SourcePayloadError("DefiLlama HTTP 200 payload is not an object")
    pools = payload.get("data")
    if not isinstance(pools, list) or not pools:
        raise SourcePayloadError("DefiLlama HTTP 200 payload has no pools")
    if not all(isinstance(pool, dict) for pool in pools):
        raise SourcePayloadError("DefiLlama HTTP 200 pool list contains malformed rows")
    return pools


def retain_previous_rows(
    *,
    venue: str,
    rows: Iterable[dict[str, Any]],
    error: str | None,
    previous_rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep last-known venue rows when a failed source would otherwise erase them."""
    current = list(rows)
    kept = 0
    # Only a venue-wide failure may preserve last-known rows. A non-empty current result plus an
    # error means individual markets were omitted; resurrecting every missing prior identity in
    # that case keeps delisted or drained instruments looking current under a new timestamp.
    if error and not current:
        identities = {
            (row.get("chain_id"), str(row.get("address") or "").lower()) for row in current
        }
        for row in previous_rows:
            identity = (row.get("chain_id"), str(row.get("address") or "").lower())
            if row.get("venue") == venue and identity not in identities:
                current.append(row)
                identities.add(identity)
                kept += 1
    return current, {
        "rows": len(current),
        "error": error,
        "kept_from_previous_run": kept,
    }


def _integer(value: Any, field: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} is missing")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not an integer") from exc
    if str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise ValueError(f"{field} is not an exact integer")
    return parsed


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} is missing")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} is not finite")
    return parsed


def _bps_from_percent(value: Any) -> int:
    return int((_decimal(value, "trailing APY") * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def _reward_fields(pool: dict[str, Any], base_bps: int | None) -> dict[str, Any]:
    reward_apy = pool.get("apyReward")
    # Optional emissions metadata must not make an otherwise verified base-rate row disappear.
    try:
        reward_bps = None if reward_apy is None else _bps_from_percent(reward_apy) or None
    except (ValueError, InvalidOperation, OverflowError):
        reward_bps = None
    fields = {
        "reward_apy_bps": reward_bps,
        "reward_tokens": pool.get("rewardTokens") or None,
    }
    if reward_apy is not None:
        try:
            # Judged on the base rate the row publishes, whichever observation it came from.
            fields["rewards_only"] = _decimal(reward_apy, "reward APY") > 0 and not base_bps
        except (ValueError, InvalidOperation, OverflowError):
            fields["rewards_only"] = False
    return fields


def _usd(raw_units: int, token_scale: int, price: int, price_scale: int) -> int:
    if raw_units < 0 or token_scale <= 0 or price <= 0 or price_scale <= 0:
        raise ValueError("USD conversion inputs must be positive")
    numerator = raw_units * price
    denominator = token_scale * price_scale
    return (numerator + denominator // 2) // denominator


def _trailing_rate(pool: dict[str, Any]) -> tuple[int | None, str | None]:
    try:
        has_rewards = _decimal(pool.get("apyReward") or 0, "reward APY") > 0
    except ValueError:
        has_rewards = False  # Optional malformed emissions metadata is not a measured reward.
    if has_rewards:
        # apyMean30d includes incentive tokens. Prefer the trailing base observation,
        # otherwise retain the current base with its observation basis explicitly labelled.
        if pool.get("apyBase7d") is not None:
            return _bps_from_percent(pool["apyBase7d"]), "DefiLlama apyBase7d trailing observation"
        if pool.get("apyBase") is not None:
            return _bps_from_percent(pool["apyBase"]), "DefiLlama apyBase current observation"
        return None, None
    for field, label in (
        ("apyMean30d", "DefiLlama apyMean30d trailing observation"),
        ("apyBase7d", "DefiLlama apyBase7d trailing observation"),
    ):
        if pool.get(field) is not None:
            return _bps_from_percent(pool[field]), label
    raise ValueError("no trailing DefiLlama rate (apyMean30d or apyBase7d)")


def _require_rate_identity(
    pool: dict[str, Any], *, project: str, chain: str, underlying_address: str
) -> None:
    underlying = {str(value).lower() for value in pool.get("underlyingTokens") or []}
    if (
        pool.get("project") != project
        or pool.get("chain") != chain
        or underlying_address.lower() not in underlying
    ):
        raise ValueError("trailing rate does not match chain and underlying contract identity")


def map_aave_reserve(
    reserve: dict[str, Any], rate_pool: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Map one fully read Aave reserve; never infer depth from total supply or TVL."""
    try:
        _require_rate_identity(
            rate_pool,
            project="aave-v3",
            chain=str(reserve.get("chain") or ""),
            underlying_address=str(reserve.get("underlying_address") or ""),
        )
        liquidity = _integer(reserve.get("available_liquidity"), "withdrawable liquidity")
        if liquidity <= 0:
            raise ValueError("withdrawable liquidity is not positive")
        total_supply = _integer(reserve.get("total_supply"), "total supply")
        decimals = _integer(reserve.get("decimals"), "decimals")
        if not 0 <= decimals <= 36:
            raise ValueError("decimals is outside the supported 0..36 range")
        price = _integer(reserve.get("price"), "oracle price")
        price_unit = _integer(reserve.get("price_unit"), "oracle price unit")
        depth_usd = _usd(liquidity, 10 ** decimals, price, price_unit)
        tvl_usd = _usd(total_supply, 10 ** decimals, price, price_unit)
        rate_bps, rate_source = _trailing_rate(rate_pool)
        if depth_usd <= 0:
            raise ValueError("withdrawable liquidity rounds to zero USD")
        if tvl_usd < MIN_TVL_USD:
            raise ValueError(f"TVL is below ${MIN_TVL_USD:,}")
        address = _valid_address(reserve.get("a_token_address"), "aToken address")
        underlying = _valid_address(reserve.get("underlying_address"), "underlying address")
        symbol = str(reserve.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("underlying symbol is missing")
        chain_id = _integer(reserve.get("chain_id"), "chain id")
    except ValueError as exc:
        return None, str(exc)

    return {
        "category": "vault",
        "name": f"a{symbol} — Aave v3",
        "venue": "Aave v3",
        "chain": str(reserve.get("chain") or chain_id),
        "chain_id": chain_id,
        "address": address,
        "decimals": decimals,
        "underlying_address": underlying,
        "rate_bps": rate_bps,
        **_reward_fields(rate_pool, rate_bps),
        "rate_kind": "realized" if rate_bps is not None else None,
        "rate_source": rate_source,
        "depth_usd": depth_usd,
        "depth_source": "eth_call ERC20(underlying).balanceOf(aToken)",
        "tvl_usd": tvl_usd,
        "tvl_source": "eth_call aToken.totalSupply()",
        "status": None,
        "tier": None,
        "maturity": None,
        "days_to_maturity": None,
        "reason": None,
    }, None


def map_compound_market(
    market: dict[str, Any], rate_pool: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Map one Comet market using supplier claims less outstanding base borrows as depth."""
    try:
        _require_rate_identity(
            rate_pool,
            project="compound-v3",
            chain=str(market.get("chain") or ""),
            underlying_address=str(market.get("base_token_address") or ""),
        )
        if rate_pool.get("poolMeta") is not None:
            raise ValueError("trailing rate is a Compound collateral row, not the base market")
        total_supply = _integer(market.get("total_supply"), "total supply")
        total_borrow = _integer(market.get("total_borrow"), "total borrow")
        if total_supply <= total_borrow:
            raise ValueError("withdrawable liquidity (totalSupply - totalBorrow) is not positive")
        base_scale = _integer(market.get("base_scale"), "base scale")
        if base_scale not in {10 ** exponent for exponent in range(37)}:
            raise ValueError("base scale is not a supported power of ten")
        decimals = len(str(base_scale)) - 1
        price = _integer(market.get("price"), "base token price")
        price_scale = _integer(market.get("price_scale"), "price scale")
        depth_usd = _usd(total_supply - total_borrow, base_scale, price, price_scale)
        tvl_usd = _usd(total_supply, base_scale, price, price_scale)
        rate_bps, rate_source = _trailing_rate(rate_pool)
        if depth_usd <= 0:
            raise ValueError("withdrawable liquidity rounds to zero USD")
        if tvl_usd < MIN_TVL_USD:
            raise ValueError(f"TVL is below ${MIN_TVL_USD:,}")
        address = _valid_address(market.get("comet_address"), "Comet address")
        underlying = _valid_address(market.get("base_token_address"), "base token address")
        symbol = str(market.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("base token symbol is missing")
        chain_id = _integer(market.get("chain_id"), "chain id")
    except ValueError as exc:
        return None, str(exc)

    return {
        "category": "vault",
        "name": f"{symbol} — Compound v3",
        "venue": "Compound v3",
        "chain": str(market.get("chain") or chain_id),
        "chain_id": chain_id,
        "address": address,
        "decimals": decimals,
        "underlying_address": underlying,
        "rate_bps": rate_bps,
        **_reward_fields(rate_pool, rate_bps),
        "rate_kind": "realized" if rate_bps is not None else None,
        "rate_source": rate_source,
        "depth_usd": depth_usd,
        "depth_source": "eth_call Comet.totalSupply() - Comet.totalBorrow()",
        "tvl_usd": tvl_usd,
        "tvl_source": "eth_call Comet.totalSupply()",
        "status": None,
        "tier": None,
        "maturity": None,
        "days_to_maturity": None,
        "reason": None,
    }, None


def _valid_address(value: Any, field: str) -> str:
    address = str(value or "")
    if len(address) != 42 or not address.startswith("0x"):
        raise ValueError(f"{field} is malformed")
    try:
        int(address[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{field} is malformed") from exc
    if int(address[2:], 16) == 0:
        raise ValueError(f"{field} is zero")
    return address


def _word(raw: bytes, index: int, field: str) -> bytes:
    start = index * 32
    value = raw[start:start + 32]
    if len(value) != 32:
        raise SourcePayloadError(f"eth_call returned no {field}")
    return value


def _uint(raw: bytes, field: str, index: int = 0) -> int:
    return int.from_bytes(_word(raw, index, field), "big")


def _address(raw: bytes, field: str, index: int = 0) -> str:
    word = _word(raw, index, field)
    if any(word[:12]):
        raise SourcePayloadError(f"eth_call returned malformed {field}")
    return _valid_address("0x" + word[12:].hex(), field)


def _address_argument(address: str) -> str:
    return _valid_address(address, "call argument")[2:].lower().rjust(64, "0")


def _address_array(raw: bytes, field: str) -> tuple[str, ...]:
    offset = _uint(raw, f"{field} offset")
    if offset % 32 or offset + 32 > len(raw):
        raise SourcePayloadError(f"eth_call returned malformed {field} offset")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    if length > 512 or offset + 32 + length * 32 > len(raw):
        raise SourcePayloadError(f"eth_call returned malformed {field} length")
    return tuple(
        _address(raw[offset + 32:offset + 32 + length * 32], field, index)
        for index in range(length)
    )


def _string(raw: bytes, field: str) -> str:
    if len(raw) == 32:
        return raw.rstrip(b"\0").decode("utf-8")
    offset = _uint(raw, f"{field} offset")
    if offset % 32 or offset + 32 > len(raw):
        raise SourcePayloadError(f"eth_call returned malformed {field} offset")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    end = offset + 32 + length
    if length > 256 or end > len(raw):
        raise SourcePayloadError(f"eth_call returned malformed {field} length")
    try:
        return raw[offset + 32:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourcePayloadError(f"eth_call returned non-UTF-8 {field}") from exc


def _call(reader: RpcReader, rpc: str, to: str, signature: str, argument: str = "") -> bytes:
    return reader(rpc, to, selector(signature) + argument)


def _pool_for_asset(
    pools: Iterable[dict[str, Any]],
    *,
    project: str,
    chain: str,
    asset: str,
    require_base_market: bool = False,
    require_unambiguous: bool = False,
) -> dict[str, Any] | None:
    candidates = []
    for pool in pools:
        if pool.get("project") != project or pool.get("chain") != chain:
            continue
        # DefiLlama emits Compound collateral rows with the same underlying address but a
        # ``<base>-pool`` marker. Only the marker-free row is the supplied Comet base market.
        if require_base_market and pool.get("poolMeta") is not None:
            continue
        underlying = {str(value).lower() for value in pool.get("underlyingTokens") or []}
        if asset.lower() not in underlying:
            continue
        candidates.append(pool)
    if require_unambiguous and len(candidates) != 1:
        return None

    matches = []
    for pool in candidates:
        try:
            _trailing_rate(pool)
            tvl = _decimal(pool.get("tvlUsd") or 0, "pool TVL")
        except ValueError:
            continue
        matches.append((tvl, str(pool.get("pool") or ""), pool))
    return max(matches, key=lambda item: (item[0], item[1]))[2] if matches else None


def _aave_row(
    pools: Iterable[dict[str, Any]],
    market: AaveMarket,
    asset: str,
    a_token: str,
    symbol: str,
    decimals: int,
    available_liquidity: int,
    total_supply: int,
    price: int,
    price_unit: int,
) -> dict[str, Any]:
    reserve = {
        "chain_id": market.chain_id,
        "chain": market.chain,
        "underlying_address": asset,
        "a_token_address": a_token,
        "symbol": symbol,
        "decimals": decimals,
        "available_liquidity": available_liquidity,
        "total_supply": total_supply,
        "price": price,
        "price_unit": price_unit,
    }
    rate_pool = _pool_for_asset(
        pools,
        project="aave-v3",
        chain=market.chain,
        asset=asset,
        # DefiLlama's pool UUID is not an on-chain deployment identifier that can be joined to
        # addresses_provider. Any repeated chain/underlying identity is therefore ambiguous,
        # regardless of TVL or poolMeta, and must be refused instead of blended across instances.
        require_unambiguous=True,
    )
    if rate_pool is None:
        raise ValueError("no unique Aave-instance underlying-address trailing-rate match")
    row, error = map_aave_reserve(reserve, rate_pool)
    if row is None:
        raise ValueError(error or "row was not priceable")
    return row


def _aave_sequential_rows(
    pools: Iterable[dict[str, Any]],
    market: AaveMarket,
    reader: RpcReader,
    data_provider: str,
    oracle: str,
    price_unit: int,
    assets: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for asset in assets:
        try:
            argument = _address_argument(asset)
            tokens = _call(
                reader, market.rpc, data_provider,
                "getReserveTokensAddresses(address)", argument,
            )
            a_token = _address(tokens, "aToken address")
            rows.append(_aave_row(
                pools,
                market,
                asset,
                a_token,
                _string(_call(reader, market.rpc, asset, "symbol()"), "symbol"),
                _uint(_call(reader, market.rpc, asset, "decimals()"), "decimals"),
                _uint(
                    _call(
                        reader, market.rpc, asset, "balanceOf(address)",
                        _address_argument(a_token),
                    ),
                    "available liquidity",
                ),
                _uint(_call(reader, market.rpc, a_token, "totalSupply()"), "aToken total supply"),
                _uint(
                    _call(
                        reader, market.rpc, oracle, "getAssetPrice(address)", argument,
                    ),
                    "Aave asset price",
                ),
                price_unit,
            ))
        except Exception as exc:
            errors.append(f"{market.chain} {asset}: {exc}")
    return rows, errors


def _aave_multicall_rows(
    pools: Iterable[dict[str, Any]],
    market: AaveMarket,
    reader: RpcReader,
    data_provider: str,
    oracle: str,
    price_unit: int,
    assets: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    phase_one: list[MulticallRead] = []
    for asset in assets:
        owner = f"{market.chain} {asset}"
        argument = _address_argument(asset)
        phase_one.extend((
            MulticallRead(
                data_provider,
                selector("getReserveTokensAddresses(address)") + argument,
                owner,
                "aToken address",
            ),
            MulticallRead(asset, selector("symbol()"), owner, "symbol"),
            MulticallRead(asset, selector("decimals()"), owner, "decimals"),
        ))
    first_results = execute_multicall_chunks(reader, market.rpc, phase_one)
    first_groups, errors = complete_multicall_groups(phase_one, first_results)

    identified: dict[str, tuple[str, str, str, int]] = {}
    for asset in assets:
        owner = f"{market.chain} {asset}"
        values = first_groups.get(owner)
        if values is None:
            continue
        try:
            identified[owner] = (
                asset,
                _address(values["aToken address"], "aToken address"),
                _string(values["symbol"], "symbol"),
                _uint(values["decimals"], "decimals"),
            )
        except Exception as exc:
            errors.append(f"{owner}: {exc}")

    phase_two: list[MulticallRead] = []
    for owner, (asset, a_token, _symbol_value, _decimals_value) in identified.items():
        argument = _address_argument(asset)
        phase_two.extend((
            # This is deliberately underlying.balanceOf(aToken), never aToken.totalSupply().
            MulticallRead(
                asset,
                selector("balanceOf(address)") + _address_argument(a_token),
                owner,
                "available liquidity",
            ),
            MulticallRead(a_token, selector("totalSupply()"), owner, "aToken total supply"),
            MulticallRead(
                oracle,
                selector("getAssetPrice(address)") + argument,
                owner,
                "Aave asset price",
            ),
        ))
    second_results = execute_multicall_chunks(reader, market.rpc, phase_two)
    second_groups, second_errors = complete_multicall_groups(phase_two, second_results)
    errors.extend(second_errors)

    rows: list[dict[str, Any]] = []
    for owner, (asset, a_token, symbol_value, decimals_value) in identified.items():
        values = second_groups.get(owner)
        if values is None:
            continue
        try:
            rows.append(_aave_row(
                pools,
                market,
                asset,
                a_token,
                symbol_value,
                decimals_value,
                _uint(values["available liquidity"], "available liquidity"),
                _uint(values["aToken total supply"], "aToken total supply"),
                _uint(values["Aave asset price"], "Aave asset price"),
                price_unit,
            ))
        except Exception as exc:
            errors.append(f"{owner}: {exc}")
    return rows, errors


def aave_v3_rows(
    pools: Iterable[dict[str, Any]], *, reader: RpcReader | None = None
) -> VenueResult:
    pools = tuple(pools)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    modes: list[str] = []
    session = _RpcSession(reader)
    for market in AAVE_MARKETS:
        chain_reader = lambda rpc, to, data, chain_id=market.chain_id: session.call(
            chain_id, rpc, to, data
        )
        try:
            provider = market.addresses_provider
            multicall_available = session.multicall_available(market.chain_id, market.rpc)
            pool = _address(_call(chain_reader, market.rpc, provider, "getPool()"), "Aave Pool")
            oracle = _address(
                _call(chain_reader, market.rpc, provider, "getPriceOracle()"), "Aave oracle"
            )
            data_provider = _address(
                _call(chain_reader, market.rpc, provider, "getPoolDataProvider()"),
                "Aave PoolDataProvider",
            )
            price_unit = _uint(
                _call(chain_reader, market.rpc, oracle, "BASE_CURRENCY_UNIT()"), "Aave price unit"
            )
            assets = _address_array(
                _call(chain_reader, market.rpc, pool, "getReservesList()"), "Aave reserve list"
            )
        except Exception as exc:  # one chain failure must not erase another chain's rows
            errors.append(f"{market.chain} bootstrap failed: {exc}")
            continue

        try:
            if multicall_available:
                modes.append(f"{market.chain}: Multicall3 aggregate3")
                chain_rows, chain_errors = _aave_multicall_rows(
                    pools, market, chain_reader, data_provider, oracle, price_unit, assets
                )
            else:
                modes.append(f"{market.chain}: sequential fallback (Multicall3 unavailable)")
                chain_rows, chain_errors = _aave_sequential_rows(
                    pools, market, chain_reader, data_provider, oracle, price_unit, assets
                )
        except MulticallChunkError as exc:
            errors.append(f"{market.chain} batch failed: {exc}")
            continue
        rows.extend(chain_rows)
        errors.extend(chain_errors)

    return VenueResult(
        tuple(rows),
        _error_summary("Aave v3", errors, rows),
        len(errors),
        "Aave V3 Pool/PoolDataProvider/Oracle via eth_call",
        "ERC20(underlying).balanceOf(aToken)",
        session.round_trips,
        "; ".join(modes) or "unavailable",
        tuple(session.used_endpoints),
        tuple(errors),
    )


def _compound_row(
    pools: Iterable[dict[str, Any]],
    deployment: CompoundMarket,
    base_token: str,
    symbol_value: str,
    base_scale: int,
    total_supply: int,
    total_borrow: int,
    price: int,
) -> dict[str, Any]:
    market = {
        "chain_id": deployment.chain_id,
        "chain": deployment.chain,
        "comet_address": deployment.comet_address,
        "base_token_address": base_token,
        "symbol": symbol_value,
        "base_scale": base_scale,
        "total_supply": total_supply,
        "total_borrow": total_borrow,
        "price": price,
        "price_scale": COMET_PRICE_SCALE,
    }
    rate_pool = _pool_for_asset(
        pools,
        project="compound-v3",
        chain=deployment.chain,
        asset=base_token,
        require_base_market=True,
    )
    if rate_pool is None:
        raise ValueError("no exact base-token-address trailing-rate match")
    row, error = map_compound_market(market, rate_pool)
    if row is None:
        raise ValueError(error or "row was not priceable")
    row["deployment_source"] = deployment.manifest
    return row


def _compound_sequential_rows(
    pools: Iterable[dict[str, Any]],
    deployments: Sequence[CompoundMarket],
    reader: RpcReader,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for deployment in deployments:
        try:
            comet = deployment.comet_address
            base_token = _address(
                _call(reader, deployment.rpc, comet, "baseToken()"), "Comet base token"
            )
            price_feed = _address(
                _call(reader, deployment.rpc, comet, "baseTokenPriceFeed()"),
                "Comet base token price feed",
            )
            rows.append(_compound_row(
                pools,
                deployment,
                base_token,
                _string(_call(reader, deployment.rpc, base_token, "symbol()"), "symbol"),
                _uint(_call(reader, deployment.rpc, comet, "baseScale()"), "Comet base scale"),
                _uint(
                    _call(reader, deployment.rpc, comet, "totalSupply()"),
                    "Comet total supply",
                ),
                _uint(
                    _call(reader, deployment.rpc, comet, "totalBorrow()"),
                    "Comet total borrow",
                ),
                _uint(
                    _call(
                        reader, deployment.rpc, comet, "getPrice(address)",
                        _address_argument(price_feed),
                    ),
                    "Comet base token price",
                ),
            ))
        except Exception as exc:
            errors.append(f"{deployment.deployment}: {exc}")
    return rows, errors


def _compound_multicall_rows(
    pools: Iterable[dict[str, Any]],
    deployments: Sequence[CompoundMarket],
    reader: RpcReader,
) -> tuple[list[dict[str, Any]], list[str]]:
    phase_one: list[MulticallRead] = []
    for deployment in deployments:
        owner = deployment.deployment
        comet = deployment.comet_address
        phase_one.extend((
            MulticallRead(comet, selector("baseToken()"), owner, "Comet base token"),
            MulticallRead(
                comet,
                selector("baseTokenPriceFeed()"),
                owner,
                "Comet base token price feed",
            ),
        ))
    first_results = execute_multicall_chunks(reader, deployments[0].rpc, phase_one)
    first_groups, errors = complete_multicall_groups(phase_one, first_results)

    identified: dict[str, tuple[CompoundMarket, str, str]] = {}
    for deployment in deployments:
        values = first_groups.get(deployment.deployment)
        if values is None:
            continue
        try:
            identified[deployment.deployment] = (
                deployment,
                _address(values["Comet base token"], "Comet base token"),
                _address(
                    values["Comet base token price feed"],
                    "Comet base token price feed",
                ),
            )
        except Exception as exc:
            errors.append(f"{deployment.deployment}: {exc}")

    phase_two: list[MulticallRead] = []
    for owner, (deployment, base_token, price_feed) in identified.items():
        comet = deployment.comet_address
        phase_two.extend((
            MulticallRead(base_token, selector("symbol()"), owner, "symbol"),
            MulticallRead(comet, selector("baseScale()"), owner, "Comet base scale"),
            MulticallRead(comet, selector("totalSupply()"), owner, "Comet total supply"),
            # Withdrawable liquidity is supply minus borrow; both reads remain independent.
            MulticallRead(comet, selector("totalBorrow()"), owner, "Comet total borrow"),
            MulticallRead(
                comet,
                selector("getPrice(address)") + _address_argument(price_feed),
                owner,
                "Comet base token price",
            ),
        ))
    second_results = execute_multicall_chunks(reader, deployments[0].rpc, phase_two)
    second_groups, second_errors = complete_multicall_groups(phase_two, second_results)
    errors.extend(second_errors)

    rows: list[dict[str, Any]] = []
    for owner, (deployment, base_token, _price_feed) in identified.items():
        values = second_groups.get(owner)
        if values is None:
            continue
        try:
            rows.append(_compound_row(
                pools,
                deployment,
                base_token,
                _string(values["symbol"], "symbol"),
                _uint(values["Comet base scale"], "Comet base scale"),
                _uint(values["Comet total supply"], "Comet total supply"),
                _uint(values["Comet total borrow"], "Comet total borrow"),
                _uint(values["Comet base token price"], "Comet base token price"),
            ))
        except Exception as exc:
            errors.append(f"{owner}: {exc}")
    return rows, errors


def compound_v3_rows(
    pools: Iterable[dict[str, Any]], *, reader: RpcReader | None = None
) -> VenueResult:
    pools = tuple(pools)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    modes: list[str] = []
    session = _RpcSession(reader)
    deployments_by_chain: dict[int, list[CompoundMarket]] = {}
    for deployment in COMPOUND_MARKETS:
        deployments_by_chain.setdefault(deployment.chain_id, []).append(deployment)

    for chain_id, deployments in deployments_by_chain.items():
        first = deployments[0]
        chain_reader = lambda rpc, to, data, selected_chain=chain_id: session.call(
            selected_chain, rpc, to, data
        )
        try:
            if session.multicall_available(chain_id, first.rpc):
                modes.append(f"{first.chain}: Multicall3 aggregate3")
                chain_rows, chain_errors = _compound_multicall_rows(
                    pools, deployments, chain_reader
                )
            else:
                modes.append(f"{first.chain}: sequential fallback (Multicall3 unavailable)")
                chain_rows, chain_errors = _compound_sequential_rows(
                    pools, deployments, chain_reader
                )
        except MulticallChunkError as exc:
            errors.append(f"{first.chain} batch failed: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{first.chain} source failed: {exc}")
            continue
        rows.extend(chain_rows)
        errors.extend(chain_errors)

    return VenueResult(
        tuple(rows),
        _error_summary("Compound v3", errors, rows),
        len(errors),
        "Compound official deployment roots plus Comet eth_call",
        "Comet.totalSupply() - Comet.totalBorrow()",
        session.round_trips,
        "; ".join(modes) or "unavailable",
        tuple(session.used_endpoints),
        tuple(errors),
    )


def _error_summary(venue: str, errors: list[str], rows: list[dict[str, Any]]) -> str | None:
    if not errors and rows:
        return None
    if not rows and not errors:
        return f"{venue} returned no priceable rows"
    detail = "; ".join(errors[:3])
    suffix = "" if len(errors) <= 3 else f"; and {len(errors) - 3} more"
    return f"{venue}: {len(errors)} market reads omitted: {detail}{suffix}"
