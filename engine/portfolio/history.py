"""What a wallet did before it ever met us, as evidence rather than as a performance claim.

Two things this module deliberately cannot do.

It carries no value. A `TransferEvent` has no USD field -- not a nullable one, none -- because
what a past transfer was worth needs a price at that timestamp held to the same two-source
standard a live price is, and that evidence is a separate problem with a much worse supply.
Showing what happened is cheap; showing what it earned is not, and a field left open would be
filled eventually by whoever needed a number that day.

It does not assume a source saw everything. An index paginates, rate limits, starts at a block
and covers the chains it covers. `HistoryResult` therefore carries the source's own claim about
coverage, and a source that cannot establish it has to say why. Absence never announces itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .contracts import (
    PortfolioContractError,
    ReasonCode,
    safe_detail,
    normalize_address,
    require_utc,
    utc_iso,
)
from .discovery import AdapterError


HISTORY_SCHEMA = "fenorium/wallet-history/v1"
DEFAULT_VERIFICATION_BUDGET = 60


class Direction(str, Enum):
    IN = "IN"
    OUT = "OUT"
    SELF = "SELF"


@dataclass(frozen=True, slots=True)
class TransferEvent:
    """One token movement the wallet was a party to.

    `verified` means the chain was asked and confirmed this transfer, not that an index said so.
    An unverified event is a claim; it is kept, labelled, and never quietly promoted.
    """

    chain_id: int
    token: str
    counterparty: str
    direction: Direction
    raw_amount: int
    block_number: int
    transaction_hash: str
    occurred_at: datetime
    log_index: int
    decimals: int | None = None
    symbol: str | None = None
    registered_identity: str | None = None
    verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "token", normalize_address(self.token))
        object.__setattr__(self, "counterparty", normalize_address(self.counterparty))
        if isinstance(self.raw_amount, bool) or not isinstance(self.raw_amount, int):
            raise PortfolioContractError("a transfer amount must be an integer")
        if self.raw_amount < 0:
            raise PortfolioContractError("a transfer amount cannot be negative")
        if self.block_number < 0:
            raise PortfolioContractError("block_number cannot be negative")
        if self.log_index < 0:
            raise PortfolioContractError("log_index cannot be negative")
        digest = self.transaction_hash
        if not isinstance(digest, str) or len(digest) != 66 or not digest.startswith("0x"):
            raise PortfolioContractError("transaction_hash must be a 32-byte hash")
        try:
            int(digest[2:], 16)
        except ValueError as exc:
            raise PortfolioContractError("transaction_hash contains non-hex characters") from exc
        object.__setattr__(self, "transaction_hash", digest.lower())
        require_utc(self.occurred_at, field_name="TransferEvent.occurred_at")
        if self.decimals is not None and not 0 <= self.decimals <= 255:
            raise PortfolioContractError("decimals must be in [0, 255]")

    @property
    def identity(self) -> str:
        """Stable and unique: one log in one transaction on one chain."""

        return f"{self.chain_id}:{self.transaction_hash}:{self.log_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "chain_id": self.chain_id,
            "token": self.token,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "counterparty": self.counterparty,
            "direction": self.direction.value,
            "raw_amount": str(self.raw_amount),
            "block_number": self.block_number,
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "occurred_at": utc_iso(self.occurred_at, field_name="TransferEvent.occurred_at"),
            "registered_identity": self.registered_identity,
            "verified": self.verified,
            # Named so that nothing downstream has to infer it from a missing key.
            "valued": False,
        }


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """What a source found, and whether it can stand behind having found all of it."""

    transfers: tuple[TransferEvent, ...]
    establishes_coverage: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.transfers, tuple):
            raise PortfolioContractError("HistoryResult.transfers must be a tuple")
        if not self.establishes_coverage and not self.detail:
            raise PortfolioContractError("a source that cannot establish coverage must say why")


@runtime_checkable
class WalletHistorySource(Protocol):
    """Reports transfers a wallet was party to. It never reports a value."""

    def transfers(self, chain_id: int, wallet: str) -> HistoryResult: ...


@runtime_checkable
class TransferVerifier(Protocol):
    """Asks the chain whether a claimed transfer actually happened.

    An index can report a transfer that did not occur as easily as it can omit one that did.
    Verification answers the first; only the source's coverage claim can speak to the second.

    The wallet is a parameter rather than a field on the event. An event names its counterparty
    and a direction, so on its own it cannot say who the other side was meant to be -- and a
    verifier without that will confirm a transfer the wallet was never part of.
    """

    def confirms(self, event: TransferEvent, wallet: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ScannedHistoryChain:
    chain_id: int
    event_count: int
    verified_count: int
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "event_count": self.event_count,
            "verified_count": self.verified_count,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class WalletHistory:
    wallet: str
    as_of: datetime
    method_version: str
    events: tuple[TransferEvent, ...]
    scanned_chains: tuple[ScannedHistoryChain, ...]
    errors: tuple[AdapterError, ...]
    complete: bool
    schema_version: str = HISTORY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != HISTORY_SCHEMA:
            raise PortfolioContractError(f"WalletHistory must use {HISTORY_SCHEMA}")
        object.__setattr__(self, "wallet", normalize_address(self.wallet))
        require_utc(self.as_of, field_name="WalletHistory.as_of")
        if not self.method_version:
            raise PortfolioContractError("WalletHistory.method_version is required")
        identities = [event.identity for event in self.events]
        if len(identities) != len(set(identities)):
            raise PortfolioContractError("one log cannot appear twice in a history")
        ordering = [
            (event.chain_id, event.block_number, event.log_index) for event in self.events
        ]
        if ordering != sorted(ordering):
            raise PortfolioContractError("history must be ordered by chain, block, then log index")

    @property
    def registered_events(self) -> tuple[TransferEvent, ...]:
        return tuple(event for event in self.events if event.registered_identity is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="WalletHistory.as_of"),
            "wallet": self.wallet,
            "complete": self.complete,
            "events": [event.to_dict() for event in self.events],
            "scanned_chains": [chain.to_dict() for chain in self.scanned_chains],
            "errors": [error.to_dict() for error in self.errors],
        }


def load_wallet_history(
    wallet: str,
    *,
    chain_ids: tuple[int, ...],
    source: WalletHistorySource,
    as_of: datetime,
    method_version: str,
    registered_by_key: dict[tuple[int, str], str] | None = None,
    verifier: TransferVerifier | None = None,
    verification_budget: int = DEFAULT_VERIFICATION_BUDGET,
    priority_chain_ids: tuple[int, ...] = (),
) -> WalletHistory:
    """Collect a wallet's transfers, label the ones the registry knows, value nothing.

    A chain whose source could not establish coverage, or whose verification was cut short,
    is reported incomplete. `complete` is the conjunction across chains, so a history is only
    whole when every chain it covers was whole.
    """

    if verification_budget < 0:
        raise PortfolioContractError("a verification budget cannot be negative")
    require_utc(as_of, field_name="load_wallet_history.as_of")
    registry = registered_by_key or {}
    wallet_address = normalize_address(wallet)

    events: list[TransferEvent] = []
    chains: list[ScannedHistoryChain] = []
    errors: list[AdapterError] = []
    remaining_budget = verification_budget

    requested_chains = set(chain_ids)
    priority_chains = list(dict.fromkeys(chain for chain in priority_chain_ids if chain in requested_chains))
    verification_order = priority_chains + sorted(requested_chains.difference(priority_chains))
    for chain_id in verification_order:
        try:
            result = source.transfers(chain_id, wallet_address)
        except Exception as exc:  # noqa: BLE001 - a source outage is a gap, not an empty past
            errors.append(AdapterError(
                chain_id=chain_id,
                identity=None,
                reason_code=ReasonCode.HISTORY_UNAVAILABLE,
                detail=safe_detail(exc),
            ))
            chains.append(ScannedHistoryChain(chain_id, 0, 0, complete=False))
            continue
        if not isinstance(result, HistoryResult):
            raise PortfolioContractError("a history source must return a HistoryResult")

        chain_complete = result.establishes_coverage
        if not result.establishes_coverage:
            errors.append(AdapterError(
                chain_id=chain_id,
                identity=None,
                reason_code=ReasonCode.HISTORY_TRUNCATED,
                detail=f"the source did not establish coverage: {result.detail}",
            ))

        chain_events: list[TransferEvent] = []
        verified_here = 0
        for event in result.transfers:
            if event.chain_id != chain_id:
                raise PortfolioContractError("a source returned an event for another chain")
            identity = registry.get((event.chain_id, event.token))
            # A source does not get to certify its own claim. `verified` means the chain was
            # asked and agreed, so it is decided here and an incoming flag is ignored.
            confirmed = False
            if verifier is not None:
                if remaining_budget > 0:
                    remaining_budget -= 1
                    try:
                        confirmed = bool(verifier.confirms(event, wallet_address))
                    except Exception as exc:  # noqa: BLE001 - one unverifiable log is not a lost history
                        errors.append(AdapterError(
                            chain_id=chain_id,
                            identity=event.identity,
                            reason_code=ReasonCode.ADAPTER_ERROR,
                            detail=safe_detail(exc),
                        ))
                        confirmed = False
                        chain_complete = False
                    if not confirmed:
                        # The chain was asked and did not agree. Keeping the event while saying
                        # so is the point: dropping it would hide a source that is wrong.
                        chain_complete = False
                else:
                    chain_complete = False
            chain_events.append(
                TransferEvent(
                    chain_id=event.chain_id,
                    token=event.token,
                    counterparty=event.counterparty,
                    direction=event.direction,
                    raw_amount=event.raw_amount,
                    block_number=event.block_number,
                    transaction_hash=event.transaction_hash,
                    occurred_at=event.occurred_at,
                    log_index=event.log_index,
                    decimals=event.decimals,
                    symbol=event.symbol,
                    registered_identity=identity,
                    verified=confirmed,
                )
            )
            if confirmed:
                verified_here += 1

        if verifier is not None and verified_here < len(chain_events):
            errors.append(AdapterError(
                chain_id=chain_id,
                identity=None,
                reason_code=ReasonCode.HISTORY_TRUNCATED,
                detail=(
                    f"{len(chain_events) - verified_here} of {len(chain_events)} transfers were "
                    "not confirmed against the chain"
                ),
            ))
        events.extend(chain_events)
        chains.append(ScannedHistoryChain(
            chain_id=chain_id,
            event_count=len(chain_events),
            verified_count=verified_here,
            complete=chain_complete,
        ))

    events.sort(key=lambda event: (event.chain_id, event.block_number, event.log_index))
    return WalletHistory(
        wallet=wallet_address,
        as_of=as_of,
        method_version=method_version,
        events=tuple(events),
        scanned_chains=tuple(sorted(chains, key=lambda chain: chain.chain_id)),
        errors=tuple(sorted(errors, key=lambda e: (e.chain_id, e.identity or "", e.detail))),
        complete=bool(chains) and all(chain.complete for chain in chains),
    )
