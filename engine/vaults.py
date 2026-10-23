"""Measured vault exit capacity, kept separate from headline assets and trailing yield.

An ERC-4626 vault's ``totalAssets`` describes claims, not cash available for immediate exit.
MetaMorpho can satisfy a withdrawal only from idle underlying plus the portion of each queued
market that both belongs to the vault and is presently liquid. This module preserves that
distinction structurally: an unwalked queue has unknown capacity and refuses every requested size.

Rates carry a second boundary. A realized APY is a backward-looking observation over its own
window, not a rate offered for the caller's next holding period. Comparison with a quote is thus an
explicit opt-in rather than an accidental consequence of storing both as numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .attestation import MEASURED
from .chain import ChainError, _address, _uint, eth_call, rpc_call, selector

_VIRTUAL_ASSETS = 1
_VIRTUAL_SHARES = 1_000_000
_WAD = 10**18


class VaultError(RuntimeError):
    """Raised when vault evidence is malformed or cannot be read."""


class RateKind(str, Enum):
    QUOTED = "QUOTED"
    INDICATIVE = "INDICATIVE"
    REALIZED = "REALIZED"


@dataclass(frozen=True, slots=True)
class VaultLiquidity:
    vault: str
    total_assets: int
    idle_assets: int
    queue_length: int
    queue_available: int | None
    decimals: int
    observed_at: datetime
    evidence_class: str = MEASURED

    def __post_init__(self) -> None:
        for field, value in (
            ("total_assets", self.total_assets),
            ("idle_assets", self.idle_assets),
            ("queue_length", self.queue_length),
            ("decimals", self.decimals),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VaultError(f"{field} must be a non-negative integer")
        if self.queue_available is not None and (
            isinstance(self.queue_available, bool)
            or not isinstance(self.queue_available, int)
            or self.queue_available < 0
        ):
            raise VaultError("queue_available must be a non-negative integer or None")

    @property
    def withdrawable_now(self) -> int | None:
        if self.queue_available is None:
            return None
        return self.idle_assets + self.queue_available


def withdrawable_at_size(liq: VaultLiquidity, size: int) -> tuple[bool, str]:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise VaultError("size must be a non-negative integer in asset base units")
    available = liq.withdrawable_now
    if available is None:
        return False, "withdraw queue not walked: cannot show this size can be exited at all"
    if available < size:
        return False, f"withdrawable now {available} is below requested size {size}"
    return True, f"withdrawable now {available} covers requested size {size}"


def compare_rate(
    kind: RateKind,
    *,
    allow_realized_vs_quoted: bool = False,
) -> tuple[bool, str]:
    if kind is RateKind.REALIZED and not allow_realized_vs_quoted:
        return False, (
            "realized APY is a trailing observation, not an offered rate, and its duration "
            "is unmatched to the quote"
        )
    if kind is RateKind.REALIZED:
        return True, "realized-versus-quoted comparison explicitly allowed by the caller"
    if kind is RateKind.INDICATIVE:
        return True, "indicative rate remains labelled as non-executable"
    return True, "quoted rate is an offer-class observation"


def _read_word(rpc: str, contract: str, signature: str, arguments: str = "") -> bytes:
    try:
        raw = eth_call(rpc, contract, selector(signature) + arguments)
    except ChainError as exc:
        raise VaultError(f"could not read {signature} from {contract}: {exc}") from exc
    if len(raw) < 32:
        raise VaultError(f"{signature} returned fewer than 32 bytes")
    return raw


def _address_word(address: str) -> str:
    try:
        value = int(address, 16)
    except (TypeError, ValueError) as exc:
        raise VaultError(f"invalid address {address!r}") from exc
    if not 0 <= value < 1 << 160:
        raise VaultError(f"invalid address {address!r}")
    return f"{value:064x}"


def _shares_to_assets_down(shares: int, total_assets: int, total_shares: int) -> int:
    return shares * (total_assets + _VIRTUAL_ASSETS) // (total_shares + _VIRTUAL_SHARES)


def _assets_to_shares_down(assets: int, total_assets: int, total_shares: int) -> int:
    return assets * (total_shares + _VIRTUAL_SHARES) // (total_assets + _VIRTUAL_ASSETS)


def _expected_market_balances(
    market: tuple[int, int, int, int, int, int],
    *,
    borrow_rate: int,
    timestamp: int,
) -> tuple[int, int, int, int]:
    (
        total_supply_assets,
        total_supply_shares,
        total_borrow_assets,
        total_borrow_shares,
        last_update,
        fee,
    ) = market
    if timestamp < last_update:
        raise VaultError("latest block timestamp precedes the market's last update")
    elapsed = timestamp - last_update
    if elapsed == 0 or total_borrow_assets == 0 or borrow_rate == 0:
        return (
            total_supply_assets,
            total_supply_shares,
            total_borrow_assets,
            total_borrow_shares,
        )
    first_term = borrow_rate * elapsed
    second_term = first_term * first_term // (2 * _WAD)
    third_term = second_term * first_term // (3 * _WAD)
    compounded = first_term + second_term + third_term
    interest = total_borrow_assets * compounded // _WAD
    total_supply_assets += interest
    total_borrow_assets += interest
    if fee:
        fee_amount = interest * fee // _WAD
        fee_shares = _assets_to_shares_down(
            fee_amount,
            total_supply_assets - fee_amount,
            total_supply_shares,
        )
        total_supply_shares += fee_shares
    return (
        total_supply_assets,
        total_supply_shares,
        total_borrow_assets,
        total_borrow_shares,
    )


def _latest_timestamp(rpc: str) -> int:
    try:
        block = rpc_call(rpc, "eth_getBlockByNumber", ["latest", False])
    except ChainError as exc:
        raise VaultError(f"could not read latest block timestamp: {exc}") from exc
    if not isinstance(block, dict) or not isinstance(block.get("timestamp"), str):
        raise VaultError(f"latest block returned an unusable timestamp: {block!r}")
    try:
        return int(block["timestamp"], 16)
    except ValueError as exc:
        raise VaultError(f"latest block timestamp is not hex: {block['timestamp']!r}") from exc


def walk_withdraw_queue(rpc: str, vault: str) -> int:
    """Walk a MetaMorpho V1 queue using current on-chain market and position state."""
    morpho = _address(_read_word(rpc, vault, "MORPHO()"), 0)
    asset = _address(_read_word(rpc, vault, "asset()"), 0)
    length = _uint(_read_word(rpc, vault, "withdrawQueueLength()"), 0)
    timestamp = _latest_timestamp(rpc)
    morpho_balance = _uint(
        _read_word(rpc, asset, "balanceOf(address)", _address_word(morpho)), 0
    )
    available = 0
    for index in range(length):
        market_id_raw = _read_word(
            rpc, vault, "withdrawQueue(uint256)", f"{index:064x}"
        )
        market_id = market_id_raw[:32].hex()
        market = _read_word(rpc, morpho, "market(bytes32)", market_id)
        if len(market) < 6 * 32:
            raise VaultError("market(bytes32) returned fewer than six ABI words")
        market_values = tuple(_uint(market, word) for word in range(6))
        params = _read_word(rpc, morpho, "idToMarketParams(bytes32)", market_id)
        if len(params) < 5 * 32:
            raise VaultError("idToMarketParams(bytes32) returned fewer than five ABI words")
        irm = _address(params, 3)
        borrow_rate = 0
        if int(irm, 16) != 0 and market_values[2] != 0 and timestamp != market_values[4]:
            borrow_rate = _uint(
                _read_word(
                    rpc,
                    irm,
                    "borrowRateView((address,address,address,address,uint256),"
                    "(uint128,uint128,uint128,uint128,uint128,uint128))",
                    params[:5 * 32].hex() + market[:6 * 32].hex(),
                ),
                0,
            )
        (
            total_supply_assets,
            total_supply_shares,
            total_borrow_assets,
            _,
        ) = _expected_market_balances(
            market_values,
            borrow_rate=borrow_rate,
            timestamp=timestamp,
        )
        position = _read_word(
            rpc,
            morpho,
            "position(bytes32,address)",
            market_id + _address_word(vault),
        )
        if len(position) < 3 * 32:
            raise VaultError("position(bytes32,address) returned fewer than three ABI words")
        supply_shares = _uint(position, 0)
        supplied = _shares_to_assets_down(
            supply_shares, total_supply_assets, total_supply_shares
        )
        market_liquidity = max(total_supply_assets - total_borrow_assets, 0)
        available += min(supplied, market_liquidity)
    return min(available, morpho_balance)


def read_vault_liquidity(rpc: str, vault: str) -> VaultLiquidity:
    total_assets = _uint(_read_word(rpc, vault, "totalAssets()"), 0)
    asset = _address(_read_word(rpc, vault, "asset()"), 0)
    decimals = _uint(_read_word(rpc, asset, "decimals()"), 0)
    if decimals > 36:
        raise VaultError(f"asset decimals out of range: {decimals}")
    idle_assets = _uint(
        _read_word(rpc, asset, "balanceOf(address)", _address_word(vault)), 0
    )
    queue_length = _uint(_read_word(rpc, vault, "withdrawQueueLength()"), 0)
    queue_available = walk_withdraw_queue(rpc, vault)
    return VaultLiquidity(
        vault=vault,
        total_assets=total_assets,
        idle_assets=idle_assets,
        queue_length=queue_length,
        queue_available=queue_available,
        decimals=decimals,
        observed_at=datetime.now(timezone.utc),
    )
