"""Registered-asset wallet discovery over an injected read-only adapter.

This module intentionally has no RPC URL, HTTP client, or token enumeration logic.
The adapter is supplied by the caller and discovery only reads native balances and
the explicitly registered contracts in a request.  That makes the scope auditable
and prevents a partial scan from being presented as a complete wallet inventory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
from typing import Any, Protocol, runtime_checkable

from ..registry import registry
from .contracts import (
    CapabilityStatus,
    DiscoveryScope,
    PortfolioContractError,
    ReasonCode,
    safe_detail,
    normalize_address,
    require_utc,
    stable_identity,
    utc_iso,
)


# The chains a wallet can be read on: every chain the registry reads portfolios on or lets a buyer
# pay from, since a balance there is money the wallet can spend. Its one testnet is the demo's.
SUPPORTED_CHAIN_IDS = registry().ids_with_role("portfolio") | registry().ids_with_role("pay")
MAINNET_CHAIN_IDS = frozenset(
    chain_id for chain_id in SUPPORTED_CHAIN_IDS if not registry().require(chain_id).testnet
)
(DEMO_CHAIN_ID,) = sorted(registry().ids_with_role("demo"))


class AssetKind(str, Enum):
    NATIVE = "NATIVE"
    ERC20 = "ERC20"
    VAULT = "VAULT"
    PT = "PT"
    POSITION = "POSITION"


@dataclass(frozen=True, slots=True)
class BlockReference:
    """An explicit state reference with finality supplied by the platform boundary."""

    chain_id: int
    block_number: int
    block_hash: str | None = None
    finalized: bool = True
    finality: str = "finalized"

    def __post_init__(self) -> None:
        if self.chain_id not in SUPPORTED_CHAIN_IDS:
            raise PortfolioContractError(f"unsupported discovery chain {self.chain_id}")
        if self.block_number < 0:
            raise PortfolioContractError("block_number cannot be negative")
        if not isinstance(self.finality, str) or re.fullmatch(r"finalized|safe|depth-[0-9]+", self.finality) is None:
            raise PortfolioContractError("finality must be finalized, safe, or depth-<n>")
        if self.finalized is not (self.finality == "finalized"):
            raise PortfolioContractError("finalized must agree with the explicit block finality")
        if self.block_hash is not None:
            if not isinstance(self.block_hash, str) or not self.block_hash.startswith("0x"):
                raise PortfolioContractError("block_hash must be a 0x-prefixed hash when supplied")

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash.lower() if self.block_hash else None,
            "finalized": self.finalized,
            "finality": self.finality,
        }

    @property
    def number(self) -> int:
        """Compatibility alias for callers that call an EVM block number simply number."""

        return self.block_number


@dataclass(frozen=True, slots=True)
class RegisteredAsset:
    """A known balance target.  Discovery cannot touch anything outside this registry."""

    chain_id: int
    kind: AssetKind
    decimals: int
    address: str | None = None
    symbol: str = ""
    category: str = "UNKNOWN"
    protocol: str = "UNKNOWN"
    is_stablecoin: bool = False
    is_variable_income: bool = False
    valuation_support: CapabilityStatus = CapabilityStatus.SUPPORTED
    risk_support: CapabilityStatus = CapabilityStatus.UNRESOLVED
    fixed_income_support: CapabilityStatus = CapabilityStatus.UNSUPPORTED
    permission_support: CapabilityStatus = CapabilityStatus.UNRESOLVED
    execution_support: CapabilityStatus = CapabilityStatus.UNRESOLVED
    instrument_id: str | None = None

    def __post_init__(self) -> None:
        if self.chain_id not in SUPPORTED_CHAIN_IDS:
            raise PortfolioContractError(f"registered asset uses unsupported chain {self.chain_id}")
        if self.decimals < 0 or self.decimals > 255:
            raise PortfolioContractError("registered asset decimals must be in [0, 255]")
        if self.kind is AssetKind.POSITION:
            if self.address is not None:
                raise PortfolioContractError("a position instrument cannot have a contract address")
            if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
                raise PortfolioContractError("a position instrument requires a non-empty instrument_id")
        elif self.instrument_id is not None:
            raise PortfolioContractError("instrument_id is only valid for position instruments")
        elif self.kind is AssetKind.NATIVE:
            if self.address is not None:
                raise PortfolioContractError("a native registered asset cannot have a contract address")
        elif self.address is None:
            raise PortfolioContractError("ERC20, vault, and PT assets require an exact contract address")
        elif self.address is not None:
            object.__setattr__(self, "address", normalize_address(self.address))

    @property
    def identity(self) -> str:
        if self.kind is AssetKind.POSITION:
            return self.instrument_id or ""
        return stable_identity(self.chain_id, self.address, kind=self.kind.value)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "identity": self.identity,
            "chain_id": self.chain_id,
            "kind": self.kind.value,
            "address": self.address,
            "decimals": self.decimals,
            "symbol": self.symbol,
            "category": self.category,
            "protocol": self.protocol,
            "is_stablecoin": self.is_stablecoin,
            "is_variable_income": self.is_variable_income,
        }
        if self.kind is AssetKind.POSITION:
            payload["instrument_id"] = self.instrument_id
        return payload


@dataclass(frozen=True, slots=True)
class WalletScanRequest:
    """A read-only, versioned request with a finality anchor for every scanned chain."""

    wallet: str
    registered_assets: tuple[RegisteredAsset, ...]
    block_references: tuple[BlockReference, ...]
    as_of: datetime
    method_version: str
    schema_version: str = "fenorium/wallet-scan-request/v1"
    chain_ids: tuple[int, ...] = ()
    include_base_sepolia_demo: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != "fenorium/wallet-scan-request/v1":
            raise PortfolioContractError("WalletScanRequest must use fenorium/wallet-scan-request/v1")
        object.__setattr__(self, "wallet", normalize_address(self.wallet))
        require_utc(self.as_of, field_name="WalletScanRequest.as_of")
        if not self.method_version:
            raise PortfolioContractError("WalletScanRequest.method_version is required")
        identities = [asset.identity for asset in self.registered_assets]
        if len(identities) != len(set(identities)):
            raise PortfolioContractError("registered assets must have unique stable identities")
        references = {reference.chain_id: reference for reference in self.block_references}
        if len(references) != len(self.block_references):
            raise PortfolioContractError("only one finalized block reference is allowed per chain")
        target_chains = self.scanned_chain_ids
        missing = sorted(set(target_chains) - set(references))
        if missing:
            raise PortfolioContractError(
                f"missing explicit finalized block reference(s) for chain(s): {', '.join(map(str, missing))}"
            )

    @property
    def scanned_chain_ids(self) -> tuple[int, ...]:
        requested = self.chain_ids or tuple(asset.chain_id for asset in self.registered_assets)
        return tuple(sorted(set(int(chain_id) for chain_id in requested)))

    def block_for(self, chain_id: int) -> BlockReference:
        for reference in self.block_references:
            if reference.chain_id == chain_id:
                return reference
        raise PortfolioContractError(f"no block reference exists for chain {chain_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="WalletScanRequest.as_of"),
            "wallet": self.wallet,
            "registered_assets": [
                asset.to_dict() for asset in sorted(self.registered_assets, key=lambda asset: asset.identity)
            ],
            "block_references": [
                reference.to_dict()
                for reference in sorted(self.block_references, key=lambda reference: reference.chain_id)
            ],
            "chain_ids": list(self.scanned_chain_ids),
            "include_base_sepolia_demo": self.include_base_sepolia_demo,
        }


@runtime_checkable
class ReadOnlyRPCAdapter(Protocol):
    """The narrow I/O boundary used by scanner implementations.

    A conforming production adapter may have transport retries internally, but must
    perform a read at the supplied finalized block and must never sign or broadcast.
    Tests can use a tiny in-memory fake.
    """

    def native_balance(self, chain_id: int, wallet: str, block: BlockReference) -> int: ...

    def token_balance(
        self, chain_id: int, token: str, wallet: str, block: BlockReference
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class EnumerationResult:
    """What a source named, and whether it can stand behind the list being the whole list.

    `establishes_coverage` is the source's own claim about the requested block, and it defaults
    to nothing: a source says it, or the scan is incomplete. Asserting completeness by default
    is how an index that quietly dropped a row, or that answered about a different moment,
    turns into 100% coverage over a denominator nobody verified.
    """

    addresses: tuple[str, ...]
    establishes_coverage: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.addresses, tuple):
            raise PortfolioContractError("EnumerationResult.addresses must be a tuple")
        if not self.establishes_coverage and not self.detail:
            raise PortfolioContractError(
                "a source that cannot establish coverage must say why"
            )


@runtime_checkable
class TokenEnumerationSource(Protocol):
    """Names token contracts a wallet may hold. It never supplies an amount.

    An index is a hint, not an authority: it can be stale, incomplete, or seeded on purpose by
    anyone willing to pay gas to airdrop a token. Returning addresses only means there is no
    number here to distrust -- every enumerated amount is still read by
    ``ReadOnlyRPCAdapter.token_balance`` at the scan's own finalized block, exactly as a
    registered balance is. Swapping one index for another, or for a self-hosted one, therefore
    changes how complete a scan is and can never change what a balance says.
    """

    def tokens_held(
        self, chain_id: int, wallet: str, block: BlockReference
    ) -> EnumerationResult: ...


@dataclass(frozen=True, slots=True)
class AdapterError:
    chain_id: int
    identity: str | None
    reason_code: ReasonCode
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "identity": self.identity,
            "reason_code": self.reason_code.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RawPosition:
    """A discovered registered balance, including failures and zeros."""

    asset: RegisteredAsset
    raw_amount: int | None
    block: BlockReference
    adapter_error: AdapterError | None = None

    def __post_init__(self) -> None:
        if self.raw_amount is not None:
            if isinstance(self.raw_amount, bool) or not isinstance(self.raw_amount, int):
                raise PortfolioContractError("raw token amount must be an integer or unavailable")
            if self.raw_amount < 0:
                raise PortfolioContractError("raw token amount cannot be negative")
        if self.adapter_error is not None and self.raw_amount is not None:
            raise PortfolioContractError("a position cannot contain both an amount and an adapter error")

    @property
    def identity(self) -> str:
        return self.asset.identity

    @property
    def is_zero(self) -> bool:
        return self.raw_amount == 0

    @property
    def unresolved(self) -> bool:
        return self.raw_amount is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "asset": self.asset.to_dict(),
            "raw_amount": self.raw_amount,
            "block": self.block.to_dict(),
            "zero_balance": self.is_zero,
            "unresolved": self.unresolved,
            "adapter_error": None if self.adapter_error is None else self.adapter_error.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EnumeratedPosition:
    """A token an index named that the registry does not carry.

    It is deliberately not a ``RawPosition``: that type holds a ``RegisteredAsset``, and the
    valuation path walks registered positions. Keeping these in their own type means an
    unregistered token cannot reach a total by being forgotten about -- there is no field for
    it to arrive in. It is evidence that something is held, and nothing more.
    """

    chain_id: int
    address: str
    raw_amount: int
    block: BlockReference
    reason_code: ReasonCode = ReasonCode.UNREGISTERED_ASSET

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        if isinstance(self.raw_amount, bool) or not isinstance(self.raw_amount, int):
            raise PortfolioContractError("an enumerated amount must be an integer")
        if self.raw_amount <= 0:
            raise PortfolioContractError(
                "an enumerated position must carry a positive balance; a zero is not a holding"
            )
        if self.reason_code is not ReasonCode.UNREGISTERED_ASSET:
            raise PortfolioContractError(
                "an enumerated position is unregistered by construction"
            )

    @property
    def identity(self) -> str:
        return f"{self.chain_id}:{self.address}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "address": self.address,
            "raw_amount": self.raw_amount,
            "block": self.block.to_dict(),
            "reason_code": self.reason_code.value,
            # Said in the payload rather than left to a reader's assumption.
            "valued": False,
        }


@dataclass(frozen=True, slots=True)
class ScannedChain:
    chain_id: int
    block: BlockReference
    complete: bool
    position_count: int
    error_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "block": self.block.to_dict(),
            "complete": self.complete,
            "position_count": self.position_count,
            "error_count": self.error_count,
        }


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """All registry positions remain visible even when one or more reads fail."""

    wallet: str
    as_of: datetime
    method_version: str
    positions: tuple[RawPosition, ...]
    scanned_chains: tuple[ScannedChain, ...]
    adapter_errors: tuple[AdapterError, ...]
    schema_version: str = "fenorium/wallet-snapshot/v1"
    discovery_scope: DiscoveryScope = DiscoveryScope.REGISTERED_ASSETS_ONLY
    enumerated: tuple[EnumeratedPosition, ...] = ()
    # Whether the widened scan ran to the end on every chain it was asked about. False means
    # something was not looked at -- a truncated list, an index outage, a balance that would
    # not read -- so what was found is a floor and not an inventory.
    enumeration_complete: bool = True

    def __post_init__(self) -> None:
        # The scope and the version move together. A v1 document cannot describe an enumerated
        # holding -- it has no field for one -- so widening the scope is a new document shape
        # rather than a new value inside the old one, and a v1 reader keeps working untouched.
        if self.schema_version not in ("fenorium/wallet-snapshot/v1", "fenorium/wallet-snapshot/v2"):
            raise PortfolioContractError("WalletSnapshot must use wallet-snapshot v1 or v2")
        registered_only = self.discovery_scope is DiscoveryScope.REGISTERED_ASSETS_ONLY
        if registered_only and self.schema_version != "fenorium/wallet-snapshot/v1":
            raise PortfolioContractError("a REGISTERED_ASSETS_ONLY snapshot is v1")
        if not registered_only and self.schema_version != "fenorium/wallet-snapshot/v2":
            raise PortfolioContractError("an enumerated snapshot must declare v2")
        if registered_only and not self.enumeration_complete:
            raise PortfolioContractError(
                "a REGISTERED_ASSETS_ONLY snapshot enumerated nothing and cannot be incomplete at it"
            )
        if registered_only and self.enumerated:
            raise PortfolioContractError(
                "a REGISTERED_ASSETS_ONLY snapshot cannot carry enumerated positions"
            )
        identities = [item.identity for item in self.enumerated]
        if identities != sorted(identities):
            raise PortfolioContractError("enumerated positions must be sorted by stable identity")
        if len(identities) != len(set(identities)):
            raise PortfolioContractError("an enumerated token cannot appear twice")
        registered_keys = {
            f"{position.asset.chain_id}:{position.asset.address}"
            for position in self.positions
            if position.asset.address is not None
        }
        overlap = registered_keys.intersection(identities)
        if overlap:
            raise PortfolioContractError(
                f"a registered asset cannot also be enumerated: {sorted(overlap)[0]}"
            )
        object.__setattr__(self, "wallet", normalize_address(self.wallet))
        require_utc(self.as_of, field_name="WalletSnapshot.as_of")
        if not self.method_version:
            raise PortfolioContractError("WalletSnapshot.method_version is required")
        identities = [position.identity for position in self.positions]
        if identities != sorted(identities):
            raise PortfolioContractError("WalletSnapshot positions must be sorted by stable identity")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="WalletSnapshot.as_of"),
            "wallet": self.wallet,
            "discovery_scope": self.discovery_scope.value,
            "positions": [position.to_dict() for position in self.positions],
            "scanned_chains": [chain.to_dict() for chain in self.scanned_chains],
            "adapter_errors": [error.to_dict() for error in self.adapter_errors],
        }
        # A v1 payload keeps the exact keys it always had, so its schema and its readers are
        # untouched by any of this.
        if self.discovery_scope is not DiscoveryScope.REGISTERED_ASSETS_ONLY:
            payload["enumerated"] = [item.to_dict() for item in self.enumerated]
            payload["enumeration_complete"] = self.enumeration_complete
        return payload


def _adapter_call(
    adapter: object,
    names: tuple[str, ...],
    *args: Any,
) -> int:
    """Call a documented adapter method or a compatibility spelling used by fakes."""

    last_error: Exception | None = None
    for name in names:
        method = getattr(adapter, name, None)
        if method is None:
            continue
        try:
            value = method(*args)
        except TypeError as exc:
            # A fake may use keyword-only parameters; retry it with the conventional
            # names before treating its TypeError as a transport failure.
            try:
                if len(args) == 3:
                    value = method(chain_id=args[0], wallet=args[1], block=args[2])
                else:
                    value = method(
                        chain_id=args[0], token=args[1], wallet=args[2], block=args[3]
                    )
            except Exception as retry_exc:  # noqa: BLE001 - adapter failures are evidence
                last_error = retry_exc if isinstance(retry_exc, Exception) else exc
                continue
        except Exception as exc:  # noqa: BLE001 - preserve partial-discovery evidence
            last_error = exc
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PortfolioContractError(
                f"adapter {name} returned a non-negative integer requirement violation: {value!r}"
            )
        return value
    if last_error is not None:
        raise last_error
    raise PortfolioContractError(f"adapter exposes none of the read-only methods {names!r}")


def _read_registered_balance(
    adapter: ReadOnlyRPCAdapter,
    *,
    asset: RegisteredAsset,
    wallet: str,
    block: BlockReference,
) -> int:
    if asset.kind is AssetKind.NATIVE:
        return _adapter_call(
            adapter,
            ("native_balance", "get_native_balance", "balance_native"),
            asset.chain_id,
            wallet,
            block,
        )
    if asset.address is None:
        raise PortfolioContractError(
            "a registered contract-backed asset must carry an address"
        )
    return _adapter_call(
        adapter,
        ("token_balance", "erc20_balance", "get_token_balance", "balance_of"),
        asset.chain_id,
        asset.address,
        wallet,
        block,
    )


DEFAULT_ENUMERATION_BUDGET = 400


def _reconcile_enumerated(
    enumeration: TokenEnumerationSource,
    adapter: ReadOnlyRPCAdapter,
    *,
    chain_id: int,
    wallet: str,
    block: BlockReference,
    registered_here: set[str],
    budget: int,
    enumerated: list[EnumeratedPosition],
    errors: list[AdapterError],
) -> int:
    """Confirm every named token on chain. Returns how many failures to charge the chain.

    Nothing an index says about an amount is used. It contributes addresses; the balance comes
    from the same adapter and the same finalized block that a registered asset's does.
    """

    try:
        result = enumeration.tokens_held(chain_id, wallet, block)
    except Exception as exc:  # noqa: BLE001 - an index outage is partial discovery, not an empty wallet
        errors.append(AdapterError(
            chain_id=chain_id,
            identity=None,
            reason_code=ReasonCode.ENUMERATION_UNAVAILABLE,
            detail=safe_detail(exc),
        ))
        return 1
    if not isinstance(result, EnumerationResult):
        raise PortfolioContractError("tokens_held must return an EnumerationResult")
    named = result.addresses

    candidates: list[str] = []
    seen: set[str] = set()
    failures = 0
    if not result.establishes_coverage:
        # The source itself says the list may not be the whole list. Believing it anyway is the
        # difference between "we found nothing else" and "nothing else is there".
        errors.append(AdapterError(
            chain_id=chain_id,
            identity=None,
            reason_code=ReasonCode.ENUMERATION_TRUNCATED,
            detail=f"the index did not establish coverage at this block: {result.detail}",
        ))
        failures += 1
    for raw in named:
        try:
            address = normalize_address(raw)
        except Exception as exc:  # noqa: BLE001 - one bad entry is not a failed wallet
            # An index is untrusted input. A single unusable address it emits must cost that
            # address, not the registered balances already read on this chain.
            errors.append(AdapterError(
                chain_id=chain_id,
                identity=None,
                reason_code=ReasonCode.ENUMERATION_UNAVAILABLE,
                detail=f"unusable address from the index: {safe_detail(exc)}",
            ))
            failures += 1
            continue
        if address in registered_here or address in seen:
            # Already read as a registered position; it belongs there, not here.
            continue
        seen.add(address)
        candidates.append(address)
    candidates.sort()

    if len(candidates) > budget:
        # Saying so is the point. A truncated scan that reported itself complete would put a
        # coverage figure on a denominator it never actually looked at.
        errors.append(AdapterError(
            chain_id=chain_id,
            identity=None,
            reason_code=ReasonCode.ENUMERATION_TRUNCATED,
            detail=(
                f"{len(candidates)} tokens named, {budget} reconciled; "
                "this chain was not enumerated to the end"
            ),
        ))
        failures += 1
        candidates = candidates[:budget]

    for address in candidates:
        try:
            amount = _adapter_call(
                adapter,
                ("token_balance", "erc20_balance", "get_token_balance", "balance_of"),
                chain_id,
                address,
                wallet,
                block,
            )
        except Exception as exc:  # noqa: BLE001 - one bad token must not lose the rest
            errors.append(AdapterError(
                chain_id=chain_id,
                identity=f"{chain_id}:{address}",
                reason_code=ReasonCode.ADAPTER_ERROR,
                detail=safe_detail(exc),
            ))
            failures += 1
            continue
        # An index lists tokens a wallet once touched. A zero now is not a holding, and a
        # balance the chain does not confirm never becomes one.
        if amount > 0:
            enumerated.append(EnumeratedPosition(
                chain_id=chain_id, address=address, raw_amount=amount, block=block,
            ))
    return failures


def discover_wallet(
    request: WalletScanRequest,
    adapter: ReadOnlyRPCAdapter,
    *,
    enumeration: TokenEnumerationSource | None = None,
    enumeration_budget: int = DEFAULT_ENUMERATION_BUDGET,
) -> WalletSnapshot:
    """Read registered balances, and optionally reconcile tokens an index named as well.

    The function catches each individual adapter failure.  A broken endpoint therefore
    leaves an explicit unresolved position and a chain error in the result instead of
    dropping that asset or returning an empty wallet.

    With no ``enumeration`` the result is exactly what it always was: a v1 snapshot scoped
    REGISTERED_ASSETS_ONLY, with the same keys.  With one, every address it names is read
    through the same adapter at the same finalized block before it is believed, and lands in
    ``enumerated`` where nothing values it.
    """

    if enumeration is not None and enumeration_budget <= 0:
        raise PortfolioContractError("an enumeration budget must be positive")
    positions: list[RawPosition] = []
    errors: list[AdapterError] = []
    chains: list[ScannedChain] = []
    enumerated: list[EnumeratedPosition] = []
    enumeration_failures = 0
    assets_by_chain: dict[int, list[RegisteredAsset]] = {
        chain_id: [] for chain_id in request.scanned_chain_ids
    }
    for asset in request.registered_assets:
        if asset.chain_id in assets_by_chain:
            assets_by_chain[asset.chain_id].append(asset)

    for chain_id in request.scanned_chain_ids:
        block = request.block_for(chain_id)
        chain_assets = sorted(assets_by_chain.get(chain_id, []), key=lambda asset: asset.identity)
        chain_error_count = 0
        for asset in chain_assets:
            if chain_id == DEMO_CHAIN_ID and not request.include_base_sepolia_demo:
                error = AdapterError(
                    chain_id=chain_id,
                    identity=asset.identity,
                    reason_code=ReasonCode.DEMO_CHAIN_NOT_ENABLED,
                    detail="Base Sepolia is demo-only and was not explicitly enabled",
                )
                positions.append(RawPosition(asset=asset, raw_amount=None, block=block, adapter_error=error))
                errors.append(error)
                chain_error_count += 1
                continue
            try:
                amount = _read_registered_balance(
                    adapter, asset=asset, wallet=request.wallet, block=block
                )
            except Exception as exc:  # noqa: BLE001 - a partial scan must retain failure evidence
                error = AdapterError(
                    chain_id=chain_id,
                    identity=asset.identity,
                    reason_code=ReasonCode.ADAPTER_ERROR,
                    detail=safe_detail(exc),
                )
                positions.append(RawPosition(asset=asset, raw_amount=None, block=block, adapter_error=error))
                errors.append(error)
                chain_error_count += 1
            else:
                positions.append(RawPosition(asset=asset, raw_amount=amount, block=block))
        # The demo opt-in is a property of the chain, not of the registry. Enumerating a chain
        # the caller declined to scan would return testnet holdings through a new door.
        demo_withheld = chain_id == DEMO_CHAIN_ID and not request.include_base_sepolia_demo
        if enumeration is not None and not demo_withheld:
            previous_failures = enumeration_failures
            registered_here = {
                asset.address for asset in chain_assets if asset.address is not None
            }
            enumeration_failures += _reconcile_enumerated(
                enumeration,
                adapter,
                chain_id=chain_id,
                wallet=request.wallet,
                block=block,
                registered_here=registered_here,
                budget=enumeration_budget,
                enumerated=enumerated,
                errors=errors,
            )
            chain_error_count = chain_error_count + enumeration_failures - previous_failures
        chains.append(
            ScannedChain(
                chain_id=chain_id,
                block=block,
                complete=chain_error_count == 0,
                position_count=len(chain_assets),
                error_count=chain_error_count,
            )
        )

    enumerated.sort(key=lambda item: item.identity)
    return WalletSnapshot(
        wallet=request.wallet,
        as_of=request.as_of,
        method_version=request.method_version,
        positions=tuple(sorted(positions, key=lambda position: position.identity)),
        scanned_chains=tuple(sorted(chains, key=lambda chain: chain.chain_id)),
        adapter_errors=tuple(
            sorted(errors, key=lambda error: (error.chain_id, error.identity or "", error.detail))
        ),
        schema_version=(
            "fenorium/wallet-snapshot/v1" if enumeration is None
            else "fenorium/wallet-snapshot/v2"
        ),
        discovery_scope=(
            DiscoveryScope.REGISTERED_ASSETS_ONLY if enumeration is None
            else DiscoveryScope.REGISTERED_PLUS_ENUMERATED
        ),
        enumerated=tuple(enumerated),
        enumeration_complete=enumeration_failures == 0,
    )


# Clear verb aliases make the small public API easy to discover without adding I/O.
scan_wallet = discover_wallet
