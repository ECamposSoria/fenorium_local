"""Pure request adapters for the local Fenorium read-only platform API.

The HTTP layer never performs wallet RPC, pricing, routing, signing, or inference.
Callers provide recorded observations and the adapters below bind them to the
deterministic ``engine.portfolio`` contracts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from fractions import Fraction
import re
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Mapping

from .execution_gate import CLOSED, ExecutionGate
from .gas_check import apply_gas_check
from .instruments import InstrumentError, PositionRow
from .midnight_orders import (
    OrderError, VerifiedMarket, to_market_id, build_order, cancel_transaction, publish_transaction,
    read_orders_facts, verify_signature,
)
from .ratelimit import Throttled
from engine.onchain import ChainReader, OnchainError
from engine.keccak import keccak256
from engine.chain import selector
from engine.midnight import ADDRESSES, MIDNIGHT_VENUES, MAX_TICK
from engine.registry import DEPOSIT_VENUES, asset_id, assets as registry_assets, purchase_refusals, registry
from .current_prices import MAX_AGE_SECONDS as CURRENT_PRICE_MAX_AGE_SECONDS
from .performance import wallet_performance
from .rerating import _is_size_depth_refusal, _rating_assumptions, _rating_point, _routing_fee_bps
from .order_store import OrderStoreError
from .position_exits import ExitError, MorphoExits, MorphoPublicBook, build_exit, parse_market, calldata, _words
from .route_store import RouteStateError
from engine.rating import rate_instrument
from .price_history import select_cohort
from engine.portfolio.analytics import AnalyticsScope
from .router import RouteRequest, RouterError
from engine.composer import (
    NATIVE_TOKEN_SENTINELS,
    RouteError,
    parse_quote,
    parse_unsigned_transaction_request,
)
from engine.portfolio.lifi import parse_lifi_status
from engine.portfolio.contracts import PRICE_SCALE, USD_MICROS, safe_detail
from engine.portfolio.valuation import raw_to_usd_micros
from engine.portfolio import (
    DEFILLAMA_CURRENT,
    AssetKind,
    EnumerationResult,
    value_wallet,
    PortfolioContractError,
    Direction,
    HistoryResult,
    TransferEvent,
    load_wallet_history,
    BlockReference,
    CapabilityResult,
    CapabilityStatus,
    CapitalKind,
    CapitalSource,
    CostLedger,
    DailyReturn,
    DecisionBundle,
    ExecutionQuote,
    Mandate,
    MarkingFrequency,
    MetricResult,
    MetricStatus,
    Objective,
    OpportunityCandidate,
    PortfolioTransformation,
    PriceObservation,
    ReasonCode,
    RegisteredAsset,
    ReturnSeries,
    WalletScanRequest,
    allocation_and_concentration,
    canonical_data,
    classify_snapshot,
    compute_portfolio_analytics,
    discover_wallet,
    evaluate_mandate,
    evaluate_scenario,
    rank_opportunities,
    value_portfolio,
)


PLATFORM_RESPONSE_SCHEMA = "fenorium/platform-response/1"
# v1 pins `fees_enabled` to false and is never edited in place. A deployment that charges a routing
# fee says so in a second version, so a reader built for v1 refuses it instead of misreading it.
PLATFORM_RESPONSE_SCHEMA_V2 = "fenorium/platform-response/2"
WALLET_OPERATION_SCHEMA = "fenorium/platform-wallet-snapshot-request/1"
ANALYTICS_OPERATION_SCHEMA = "fenorium/platform-analytics-request/1"
SCENARIO_OPERATION_SCHEMA = "fenorium/platform-scenario-request/1"
OPPORTUNITY_OPERATION_SCHEMA = "fenorium/platform-opportunity-request/1"
QUOTE_OPERATION_SCHEMA = "fenorium/platform-execution-quote-request/1"
ROUTE_OPERATION_SCHEMA = "fenorium/platform-execution-route-request/1"
# v2 names the chain the payment starts on. v1 is never edited: it keeps meaning "pay on the
# instrument's own chain".
ROUTE_OPERATION_SCHEMA_V2 = "fenorium/platform-execution-route-request/2"
SALE_OPERATION_SCHEMA = "fenorium/platform-sale-route-request/1"
EXECUTION_STATUS_SCHEMA = "fenorium/platform-execution-status/1"
# The mainnets this deployment routes between, from the chain registry: those an instrument may be
# bought on (`buy`) and those a buyer may pay from (`pay`). A paying chain is where LI.FI pays
# Fenorium's fee -- straight to the Safe's address -- so each was verified (on 2026-09-29 for the
# current set) to (a) deploy the Fenorium Safe at exactly 0xC84E…71aE when its factory is asked
# (simulated createProxyWithNonce), and (b) route USDC with the `par` fee credited. A chain failing
# (a) would receive fees nobody could ever move; never give one the pay role without repeating both.
INSTRUMENT_CHAINS = registry().ids_with_role("buy")
ROUTE_CHAINS = registry().ids_with_role("pay")
HISTORY_INDEX_CHAIN_IDS = frozenset(chain.chain_id for chain in registry().chains if chain.blockscout_index)
PAY_FROM_CHAINS = ROUTE_CHAINS - INSTRUMENT_CHAINS
POSITION_OPERATION_SCHEMA = "fenorium/platform-position-deposit-request/1"
POSITION_EXIT_OPERATION_SCHEMA = "fenorium/platform-position-exit-request/1"
LENDING_ORDER_OPERATION_SCHEMA = "fenorium/platform-lending-order-request/1"
DECISION_OPERATION_SCHEMA = "fenorium/platform-decision-bundle-request/1"
HISTORY_OPERATION_SCHEMA = "fenorium/platform-wallet-history-request/1"
# v2 answers with the history and, beside it, what each transfer was worth at the daily price
# nearest its moment. v1 is never edited: it keeps answering with the history alone.
HISTORY_OPERATION_SCHEMA_V2 = "fenorium/platform-wallet-history-request/2"
TRANSFER_VALUES_SCHEMA = "fenorium/transfer-values/1"
PREVIEW_OPERATION_SCHEMA = "fenorium/platform-route-preview-request/1"
SUBMISSION_OPERATION_SCHEMA = "fenorium/platform-route-submission/1"
MAX_PREVIEW_OPTIONS = 3
PREVIEW_RESPONSE_SECONDS = 15
PREVIEW_MIN_ROUTE_SECONDS = 2
MAX_CONCURRENT_HISTORY_VERIFICATIONS = 2
HISTORY_VERIFICATION_SECONDS = 30
HISTORY_RESPONSE_SECONDS = 45


class _DeadlineVerifier:
    def __init__(self, verifier: Any, *, deadline_at: float | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        self.verifier = verifier
        self.monotonic = monotonic or (lambda: time.monotonic())
        self.deadline = self.monotonic() + HISTORY_VERIFICATION_SECONDS
        if deadline_at is not None:
            self.deadline = min(self.deadline, deadline_at)

    def confirms(self, event: TransferEvent, wallet: str) -> bool:
        if self.monotonic() >= self.deadline:
            raise TimeoutError("verification deadline passed")
        return self.verifier.confirms(event, wallet, deadline_at=self.deadline)


class APIError(ValueError):
    """A public, non-sensitive request error."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise APIError("INVALID_REQUEST", f"{name} must be an object")
    return value


def _only(payload: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise APIError("UNKNOWN_FIELD", f"{name} contains unsupported field(s): {', '.join(unknown)}")


def _required(payload: Mapping[str, Any], key: str, name: str) -> Any:
    if key not in payload:
        raise APIError("MISSING_FIELD", f"{name}.{key} is required")
    return payload[key]


def _text(value: Any, name: str, *, maximum: int = 512, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise APIError("INVALID_FIELD", f"{name} must be a bounded string")
    if any(ord(character) < 32 for character in value):
        raise APIError("INVALID_FIELD", f"{name} contains control characters")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise APIError("INVALID_INTEGER", f"{name} must be an integer")
    if isinstance(value, str):
        if not value or (value[0] == "-" and not value[1:].isdigit()) or (
            value[0] != "-" and not value.isdigit()
        ):
            raise APIError("INVALID_INTEGER", f"{name} must be an integer")
        value = int(value)
    if not isinstance(value, int):
        raise APIError("INVALID_INTEGER", f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise APIError("INVALID_INTEGER", f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise APIError("INVALID_INTEGER", f"{name} must be at most {maximum}")
    return value


def _optional_integer(value: Any, name: str, *, minimum: int | None = None) -> int | None:
    return None if value is None else _integer(value, name, minimum=minimum)


def _timestamp(value: Any, name: str) -> datetime:
    text = _text(value, name, maximum=64)
    if not text.endswith("Z"):
        raise APIError("INVALID_TIMESTAMP", f"{name} must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise APIError("INVALID_TIMESTAMP", f"{name} is not an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise APIError("INVALID_TIMESTAMP", f"{name} must be UTC")
    return parsed.astimezone(UTC)


def _enum(enum_type, value: Any, name: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise APIError("INVALID_ENUM", f"{name} is not a supported value") from exc


def _strings(value: Any, name: str, *, maximum: int = 256) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise APIError("INVALID_FIELD", f"{name} must be a bounded array")
    return tuple(_text(item, f"{name}[]") for item in value)


def _capability(value: Any, name: str) -> CapabilityStatus:
    return _enum(CapabilityStatus, value, name)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise APIError("INVALID_BOOLEAN", f"{name} must be a boolean")
    return value


def _evm_hash(value: Any, name: str) -> str:
    text = _text(value, name, maximum=66)
    if len(text) != 66 or not text.startswith("0x"):
        raise APIError("INVALID_HASH", f"{name} must be a 32-byte 0x-prefixed hash")
    try:
        int(text[2:], 16)
    except ValueError as exc:
        raise APIError("INVALID_HASH", f"{name} must contain hexadecimal bytes") from exc
    return text.lower()


def _evm_address(value: Any, name: str) -> str:
    text = _text(value, name, maximum=42)
    if len(text) != 42 or not text.startswith("0x"):
        raise APIError("INVALID_ADDRESS", f"{name} must be a 20-byte 0x-prefixed address")
    try:
        int(text[2:], 16)
    except ValueError as exc:
        raise APIError("INVALID_ADDRESS", f"{name} must contain hexadecimal bytes") from exc
    return text.lower()


def _raw_amount(value: Any, name: str) -> int:
    """A token quantity in its own units, as digits.

    Never a JSON number: 10**18 of an 18-decimal token is past the point where a double keeps
    whole units, and a spend that silently rounds is a spend of the wrong amount. Bounded by
    uint256's width before conversion, so an arbitrarily long digit string is refused rather
    than turned into an arbitrarily large integer.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,78}", value):
        raise APIError("INVALID_AMOUNT", f"{name} must be a decimal integer string of token units")
    amount = int(value)
    if amount <= 0:
        raise APIError("INVALID_AMOUNT", f"{name} must be positive")
    if amount >= 2 ** 256:
        raise APIError("INVALID_AMOUNT", f"{name} must fit in a uint256")
    return amount


def _pay_asset(payload: Any, *, with_chain: bool = False, optional_chain: bool = False,
               name: str = "operation.pay_with") -> tuple[str, int, str, int | None]:
    row = _object(payload, name)
    _only(row, {"address", "decimals", "symbol"} | ({"chain_id"} if with_chain else set()), name)
    return (
        _evm_address(_required(row, "address", name), f"{name}.address"),
        _integer(
            _required(row, "decimals", name),
            f"{name}.decimals",
            minimum=0,
            maximum=36,
        ),
        _text(row.get("symbol", ""), f"{name}.symbol", maximum=32, allow_empty=True),
        _integer(_required(row, "chain_id", name), f"{name}.chain_id", minimum=1)
        if with_chain and (not optional_chain or "chain_id" in row) else None,
    )


def _registered_asset(payload: Any, index: int, history: Callable[[int, str | None], bool] | None = None) -> RegisteredAsset:
    row = _object(payload, f"registered_assets[{index}]")
    _only(
        row,
        {
            "identity", "chain_id", "kind", "decimals", "address", "symbol", "category", "protocol",
            "is_stablecoin", "is_variable_income", "valuation_support", "risk_support",
            "fixed_income_support", "permission_support", "execution_support",
        },
        f"registered_assets[{index}]",
    )
    chain_id = _integer(_required(row, "chain_id", "registered_asset"), "registered_asset.chain_id", minimum=1)
    address = None if row.get("address") is None else _text(row["address"], "registered_asset.address", maximum=42)
    asset = RegisteredAsset(
        chain_id=chain_id,
        kind=_enum(AssetKind, _required(row, "kind", "registered_asset"), "registered_asset.kind"),
        decimals=_integer(_required(row, "decimals", "registered_asset"), "registered_asset.decimals",
                          minimum=0, maximum=36),
        address=address,
        symbol=_text(row.get("symbol", ""), "registered_asset.symbol", maximum=64, allow_empty=True),
        category=_text(row.get("category", "UNKNOWN"), "registered_asset.category", maximum=128),
        protocol=_text(row.get("protocol", "UNKNOWN"), "registered_asset.protocol", maximum=128),
        is_stablecoin=_boolean(row.get("is_stablecoin", False), "registered_asset.is_stablecoin"),
        is_variable_income=_boolean(row.get("is_variable_income", False), "registered_asset.is_variable_income"),
        # Valuation defaults open where permission and execution stay closed, because it is not
        # the gate that protects a number: value_position() still requires strict_price() to find
        # a fresh price observation, and returns MISSING_PRICE with a null value when it cannot. Defaulting this closed made the flag itself the gate, so the browser -- which
        # declares no capabilities by design, see the client contract test -- had every position
        # refused as UNRESOLVED before its price observations were ever examined. This value also
        # matches RegisteredAsset's own declared default; the two disagreeing was the defect.
        valuation_support=_capability(row.get("valuation_support", "SUPPORTED"), "valuation_support"),
        # The browser declares no capabilities, and risk is only as supported as the history
        # behind it -- which this server holds and the browser never does. A row that says nothing
        # is risk-supported exactly when a current daily history exists for it here.
        risk_support=_capability(row.get("risk_support", _risk_default(chain_id, address, history)), "risk_support"),
        fixed_income_support=_capability(row.get("fixed_income_support", "UNSUPPORTED"), "fixed_income_support"),
        permission_support=_capability(row.get("permission_support", "UNRESOLVED"), "permission_support"),
        execution_support=_capability(row.get("execution_support", "UNRESOLVED"), "execution_support"),
    )
    supplied_identity = _text(
        _required(row, "identity", "registered_asset"),
        "registered_asset.identity",
    )
    if supplied_identity != asset.identity:
        raise APIError(
            "IDENTITY_MISMATCH",
            "registered_asset.identity does not match its chain, kind, and address",
        )
    return asset


def _block_reference(payload: Any, index: int) -> BlockReference:
    row = _object(payload, f"block_references[{index}]")
    _only(row, {"chain_id", "block_number", "block_hash", "finalized", "finality"}, f"block_references[{index}]")
    if row.get("finalized") is not True:
        raise APIError("FINALIZED_BLOCK_REQUIRED", "every block reference must explicitly be finalized")
    return BlockReference(
        chain_id=_integer(_required(row, "chain_id", "block_reference"), "block_reference.chain_id", minimum=1),
        block_number=_integer(_required(row, "block_number", "block_reference"), "block_reference.block_number", minimum=0),
        block_hash=None if row.get("block_hash") is None else _evm_hash(row["block_hash"], "block_reference.block_hash"),
        finalized=True,
        finality=row.get("finality", "finalized"),
    )


def _risk_default(chain_id: int, address: str | None, history: Callable[[int, str | None], bool] | None) -> str:
    if history is None:
        return "UNRESOLVED"
    return "SUPPORTED" if history(chain_id, address) else "UNRESOLVED"


def _wallet_scan_request(payload: Any, history: Callable[[int, str | None], bool] | None = None) -> WalletScanRequest:
    row = _object(payload, "request")
    _only(
        row,
        {
            "schema_version", "method_version", "as_of", "wallet", "registered_assets",
            "block_references", "chain_ids", "include_base_sepolia_demo",
        },
        "request",
    )
    assets = _required(row, "registered_assets", "request")
    blocks = _required(row, "block_references", "request")
    chains = row.get("chain_ids", [])
    if not isinstance(assets, list) or len(assets) > 2_000:
        raise APIError("INVALID_FIELD", "request.registered_assets must be a bounded array")
    if not isinstance(blocks, list) or len(blocks) > 6:
        raise APIError("INVALID_FIELD", "request.block_references must be a bounded array")
    if not isinstance(chains, list) or len(chains) > 6:
        raise APIError("INVALID_FIELD", "request.chain_ids must be a bounded array")
    return WalletScanRequest(
        schema_version=_text(_required(row, "schema_version", "request"), "request.schema_version"),
        method_version=_text(_required(row, "method_version", "request"), "request.method_version", maximum=128),
        as_of=_timestamp(_required(row, "as_of", "request"), "request.as_of"),
        wallet=_text(_required(row, "wallet", "request"), "request.wallet", maximum=42),
        registered_assets=tuple(_registered_asset(asset, index, history) for index, asset in enumerate(assets)),
        block_references=tuple(_block_reference(block, index) for index, block in enumerate(blocks)),
        chain_ids=tuple(_integer(chain, "request.chain_ids[]", minimum=1) for chain in chains),
        include_base_sepolia_demo=_boolean(row.get("include_base_sepolia_demo", False), "request.include_base_sepolia_demo"),
    )


class _RecordedAdapter:
    def __init__(self, observations: Any) -> None:
        if not isinstance(observations, list) or len(observations) > 2_000:
            raise APIError("INVALID_FIELD", "observations must be a bounded array")
        self._native: dict[int, int | Exception] = {}
        self._tokens: dict[tuple[int, str], int | Exception] = {}
        for index, payload in enumerate(observations):
            row = _object(payload, f"observations[{index}]")
            _only(row, {"chain_id", "address", "raw_amount", "error"}, f"observations[{index}]")
            chain_id = _integer(_required(row, "chain_id", "observation"), "observation.chain_id", minimum=1)
            address = row.get("address")
            has_amount = row.get("raw_amount") is not None
            has_error = row.get("error") is not None
            if has_amount == has_error:
                raise APIError("INVALID_OBSERVATION", "an observation needs exactly one of raw_amount or error")
            value: int | Exception = (
                _integer(row["raw_amount"], "observation.raw_amount", minimum=0)
                if has_amount
                else RuntimeError(_text(row["error"], "observation.error", maximum=256))
            )
            if address is None:
                if chain_id in self._native:
                    raise APIError("DUPLICATE_OBSERVATION", "native observations must be unique per chain")
                self._native[chain_id] = value
            else:
                normalized = _text(address, "observation.address", maximum=42).lower()
                if len(normalized) != 42 or not normalized.startswith("0x"):
                    raise APIError("INVALID_ADDRESS", "observation.address must be a 20-byte EVM address")
                try:
                    int(normalized[2:], 16)
                except ValueError as exc:
                    raise APIError("INVALID_ADDRESS", "observation.address must contain hexadecimal bytes") from exc
                key = (chain_id, normalized)
                if key in self._tokens:
                    raise APIError("DUPLICATE_OBSERVATION", "token observations must be unique")
                self._tokens[key] = value

    @staticmethod
    def _resolve(value: int | Exception | None) -> int:
        if value is None:
            raise RuntimeError("recorded adapter observation is missing")
        if isinstance(value, Exception):
            raise value
        return value

    def native_balance(self, chain_id: int, _wallet: str, _block: BlockReference) -> int:
        return self._resolve(self._native.get(chain_id))

    def token_balance(self, chain_id: int, token: str, _wallet: str, _block: BlockReference) -> int:
        return self._resolve(self._tokens.get((chain_id, token.lower())))


def _discover(operation: Mapping[str, Any], history: Callable[[int, str | None], bool] | None = None):
    request = _wallet_scan_request(_required(operation, "request", "operation"), history)
    adapter = _RecordedAdapter(_required(operation, "observations", "operation"))
    enumerated = operation.get("enumerated")
    # Absent means registered-only, which is what every existing caller sends and what it has
    # always meant. Supplying it widens the scan and, with it, the denominator.
    source = None if enumerated is None else _RecordedEnumeration(enumerated)
    return request, discover_wallet(request, adapter, enumeration=source)




class _RecordedEnumeration:
    """Token addresses the caller says an index named, per chain.

    Addresses only, exactly as the engine's port demands: an amount supplied here would be a
    number nobody read from a chain. Balances come from the same `observations` that answer for
    registered assets, so an enumerated token with no observation is an adapter error rather
    than a silent zero. Coverage is never established -- a caller listing tokens has not shown
    there are no others.
    """

    def __init__(self, payload: Any) -> None:
        rows = payload if isinstance(payload, list) else []
        if len(rows) > 6:
            raise APIError("INVALID_FIELD", "enumerated must name at most six chains")
        self._by_chain: dict[int, tuple[str, ...]] = {}
        for index, entry in enumerate(rows):
            row = _object(entry, f"enumerated[{index}]")
            _only(row, {"chain_id", "tokens"}, f"enumerated[{index}]")
            chain_id = _integer(_required(row, "chain_id", "enumerated"), "enumerated.chain_id", minimum=1)
            tokens = _required(row, "tokens", "enumerated")
            if not isinstance(tokens, list) or len(tokens) > 500:
                raise APIError("INVALID_FIELD", "enumerated.tokens must be a bounded array")
            if chain_id in self._by_chain:
                raise APIError("DUPLICATE_OBSERVATION", "enumerated chains must be unique")
            self._by_chain[chain_id] = tuple(
                _text(token, "enumerated.tokens[]", maximum=42) for token in tokens
            )

    def tokens_held(self, chain_id: int, wallet: str, block: Any) -> EnumerationResult:
        return EnumerationResult(
            addresses=self._by_chain.get(chain_id, ()),
            establishes_coverage=False,
            detail="supplied with the request; this service did not enumerate the chain itself",
        )


class _RecordedHistory:
    """Transfers supplied with the request, grouped by chain.

    The same shape every other endpoint uses: this service replays evidence rather than
    fetching it, so a response is a function of its request and nothing else. Coverage is the
    caller's claim to make, and a caller that does not make it gets an incomplete history --
    which is the honest reading of "here are some transfers I found".
    """

    def __init__(self, payload: Any) -> None:
        rows = payload if isinstance(payload, list) else None
        if rows is None or len(rows) > 5_000:
            raise APIError("INVALID_FIELD", "transfers must be a bounded array")
        self._by_chain: dict[int, list[TransferEvent]] = {}
        for index, entry in enumerate(rows):
            row = _object(entry, f"transfers[{index}]")
            _only(
                row,
                {"chain_id", "token", "counterparty", "direction", "raw_amount", "block_number",
                 "transaction_hash", "occurred_at", "log_index", "decimals", "symbol"},
                f"transfers[{index}]",
            )
            chain_id = _integer(_required(row, "chain_id", "transfer"), "transfer.chain_id", minimum=1)
            try:
                event = TransferEvent(
                    chain_id=chain_id,
                    token=_text(_required(row, "token", "transfer"), "transfer.token", maximum=42),
                    counterparty=_text(
                        _required(row, "counterparty", "transfer"), "transfer.counterparty", maximum=42),
                    direction=Direction(_text(
                        _required(row, "direction", "transfer"), "transfer.direction", maximum=8)),
                    raw_amount=_integer(
                        _required(row, "raw_amount", "transfer"), "transfer.raw_amount", minimum=0),
                    block_number=_integer(
                        _required(row, "block_number", "transfer"), "transfer.block_number", minimum=0),
                    transaction_hash=_text(
                        _required(row, "transaction_hash", "transfer"),
                        "transfer.transaction_hash", maximum=66),
                    occurred_at=_timestamp(
                        _required(row, "occurred_at", "transfer"), "transfer.occurred_at"),
                    log_index=_integer(
                        _required(row, "log_index", "transfer"), "transfer.log_index", minimum=0),
                    decimals=None if row.get("decimals") is None else _integer(
                        row["decimals"], "transfer.decimals", minimum=0, maximum=36),
                    symbol=None if row.get("symbol") is None else _text(
                        row["symbol"], "transfer.symbol", maximum=32),
                )
            except (PortfolioContractError, ValueError) as exc:
                raise APIError("INVALID_TRANSFER", f"transfers[{index}]: {exc}") from exc
            self._by_chain.setdefault(chain_id, []).append(event)

    def transfers(self, chain_id: int, wallet: str) -> HistoryResult:
        return HistoryResult(
            transfers=tuple(self._by_chain.get(chain_id, ())),
            establishes_coverage=False,
            detail="supplied with the request; this service did not walk the chain itself",
        )


class _IndexedAndRecorded:
    def __init__(self, recorded: _RecordedHistory, indexed: Any,
                 indexed_chain_ids: tuple[int, ...], supported: frozenset[int], *,
                 deadline_at: float, monotonic: Callable[[], float] | None = None) -> None:
        self.recorded = recorded
        self.indexed = indexed
        self.indexed_chain_ids = indexed_chain_ids
        self.supported = supported
        self.deadline_at = deadline_at
        self.monotonic = monotonic or (lambda: time.monotonic())
        self.statuses: dict[int, dict[str, Any]] = {}

    def transfers(self, chain_id: int, wallet: str) -> HistoryResult:
        recorded = self.recorded.transfers(chain_id, wallet)
        if chain_id not in self.indexed_chain_ids:
            return recorded
        if self.monotonic() >= self.deadline_at:
            detail = "indexed history not fetched: the response deadline passed"
            self.statuses[chain_id] = {"chain_id": chain_id, "status": "UNAVAILABLE", "detail": detail}
            return HistoryResult(recorded.transfers, False, detail)
        if self.indexed is None or chain_id not in self.supported:
            detail = "no indexed history for this chain here"
            self.statuses[chain_id] = {"chain_id": chain_id, "status": "UNSUPPORTED", "detail": detail}
            return HistoryResult(recorded.transfers, False, detail)
        try:
            fetched = self.indexed.transfers(chain_id, wallet, deadline_at=self.deadline_at)
        except Exception as exc:
            detail = f"indexed history unavailable: {safe_detail(exc)}"
            self.statuses[chain_id] = {"chain_id": chain_id, "status": "UNAVAILABLE", "detail": detail}
            return HistoryResult(recorded.transfers, False, detail)
        recorded_keys = {(event.transaction_hash.lower(), event.log_index) for event in recorded.transfers}
        merged = list(recorded.transfers)
        merged.extend(event for event in fetched.transfers
                      if (event.transaction_hash.lower(), event.log_index) not in recorded_keys)
        merged.sort(key=lambda event: (event.block_number, event.log_index))
        self.statuses[chain_id] = {"chain_id": chain_id, "status": "INDEXED", "detail": fetched.detail}
        return HistoryResult(tuple(merged), fetched.establishes_coverage, fetched.detail)


def _depends_on(payload: Any) -> tuple[str, ...]:
    """Sources an observation leans on. Recorded as evidence; selection no longer pairs sources."""
    if payload is None:
        return ()
    if not isinstance(payload, list) or len(payload) > 8:
        raise APIError("INVALID_FIELD", "price.depends_on must be a list of at most 8 source names")
    return tuple(_text(item, "price.depends_on", maximum=128) for item in payload)


def _price_observation(payload: Any, index: int) -> PriceObservation:
    row = _object(payload, f"price_observations[{index}]")
    _only(
        row,
        {"schema_version", "identity", "source_id", "independence_key", "price_usd_fixed", "observed_at",
         "method_version", "depends_on"},
        f"price_observations[{index}]",
    )
    return PriceObservation(
        schema_version=_text(row.get("schema_version", "fenorium/price-observation/v1"), "price.schema_version"),
        identity=_text(_required(row, "identity", "price"), "price.identity"),
        source_id=_text(_required(row, "source_id", "price"), "price.source_id"),
        independence_key=_text(_required(row, "independence_key", "price"), "price.independence_key"),
        price_usd_fixed=_integer(_required(row, "price_usd_fixed", "price"), "price.price_usd_fixed", minimum=1),
        observed_at=_timestamp(_required(row, "observed_at", "price"), "price.observed_at"),
        method_version=_text(_required(row, "method_version", "price"), "price.method_version", maximum=128),
        depends_on=_depends_on(row.get("depends_on")),
    )


def _return_series(payload: Any, index: int) -> ReturnSeries:
    row = _object(payload, f"return_series[{index}]")
    _only(row, {"position_id", "frequency", "annualization_factor", "method_version", "returns"}, f"return_series[{index}]")
    returns = _required(row, "returns", "return_series")
    if not isinstance(returns, list) or len(returns) > 366:
        raise APIError("INVALID_FIELD", "return_series.returns must be a bounded array")
    daily: list[DailyReturn] = []
    for return_index, payload_return in enumerate(returns):
        item = _object(payload_return, f"returns[{return_index}]")
        _only(item, {"observed_at", "return_bps"}, f"returns[{return_index}]")
        daily.append(
            DailyReturn(
                _timestamp(_required(item, "observed_at", "return"), "return.observed_at"),
                _integer(_required(item, "return_bps", "return"), "return.return_bps"),
            )
        )
    return ReturnSeries(
        position_id=_text(_required(row, "position_id", "return_series"), "return_series.position_id"),
        frequency=_enum(MarkingFrequency, _required(row, "frequency", "return_series"), "return_series.frequency"),
        annualization_factor=_integer(_required(row, "annualization_factor", "return_series"), "return_series.annualization_factor"),
        returns=tuple(daily),
        method_version=_text(_required(row, "method_version", "return_series"), "return_series.method_version", maximum=128),
    )


def _cost_ledger(payload: Any, name: str = "costs") -> CostLedger:
    row = _object(payload, name)
    fields = {
        "source_swap_usd_micros", "price_impact_usd_micros", "bridge_usd_micros",
        "integrator_fee_usd_micros", "fenorium_fee_usd_micros",
        "destination_venue_usd_micros", "gas_usd_micros", "entry_usd_micros",
    }
    _only(row, fields, name)
    values = {field: _optional_integer(row.get(field), f"{name}.{field}", minimum=0) for field in fields}
    return CostLedger(**values)


def _execution_quote(payload: Any, name: str = "quote") -> ExecutionQuote:
    row = _object(payload, name)
    _only(
        row,
        {"verdict", "route_hash", "observed_at", "expires_at", "available_depth_usd_micros", "costs", "source"},
        name,
    )
    return ExecutionQuote(
        verdict=_text(_required(row, "verdict", name), f"{name}.verdict", maximum=16),
        route_hash=None if row.get("route_hash") is None else _evm_hash(row["route_hash"], f"{name}.route_hash"),
        observed_at=_timestamp(_required(row, "observed_at", name), f"{name}.observed_at"),
        expires_at=_timestamp(_required(row, "expires_at", name), f"{name}.expires_at"),
        available_depth_usd_micros=_optional_integer(row.get("available_depth_usd_micros"), f"{name}.available_depth_usd_micros", minimum=0),
        costs=_cost_ledger(_required(row, "costs", name), f"{name}.costs"),
        source=_text(row.get("source", "LI.FI"), f"{name}.source", maximum=128),
    )


def _capital_source(payload: Any) -> CapitalSource:
    row = _object(payload, "source")
    _only(
        row,
        {"identity", "kind", "available_usd_micros", "selected_usd_micros", "verdict", "explicit_variable_income_opt_in", "variable_income_limit_usd_micros"},
        "source",
    )
    return CapitalSource(
        identity=_text(_required(row, "identity", "source"), "source.identity"),
        kind=_enum(CapitalKind, _required(row, "kind", "source"), "source.kind"),
        available_usd_micros=_integer(_required(row, "available_usd_micros", "source"), "source.available_usd_micros", minimum=0),
        selected_usd_micros=_integer(_required(row, "selected_usd_micros", "source"), "source.selected_usd_micros", minimum=0),
        verdict=_text(row.get("verdict", "BID"), "source.verdict", maximum=16),
        explicit_variable_income_opt_in=_boolean(row.get("explicit_variable_income_opt_in", False), "source.explicit_variable_income_opt_in"),
        variable_income_limit_usd_micros=_optional_integer(row.get("variable_income_limit_usd_micros"), "source.variable_income_limit_usd_micros", minimum=0),
    )


def _transformation(payload: Any, *, as_of: datetime) -> PortfolioTransformation:
    row = _object(payload, "transformation")
    _only(
        row,
        {"identity", "source", "destination_identity", "destination_verdict", "quote", "before_nav_usd_micros", "after_nav_usd_micros"},
        "transformation",
    )
    return PortfolioTransformation(
        identity=_text(_required(row, "identity", "transformation"), "transformation.identity"),
        source=_capital_source(_required(row, "source", "transformation")),
        destination_identity=_text(_required(row, "destination_identity", "transformation"), "transformation.destination_identity"),
        destination_verdict=_text(_required(row, "destination_verdict", "transformation"), "transformation.destination_verdict", maximum=16),
        quote=None if row.get("quote") is None else _execution_quote(row["quote"]),
        before_nav_usd_micros=_optional_integer(row.get("before_nav_usd_micros"), "transformation.before_nav_usd_micros", minimum=0),
        after_nav_usd_micros=_optional_integer(row.get("after_nav_usd_micros"), "transformation.after_nav_usd_micros", minimum=0),
        as_of=as_of,
    )


def _capability_result(payload: Any, name: str) -> CapabilityResult | None:
    if payload is None:
        return None
    row = _object(payload, name)
    _only(row, {"status", "reason_code", "detail", "evidence"}, name)
    return CapabilityResult(
        status=_enum(CapabilityStatus, _required(row, "status", name), f"{name}.status"),
        reason_code=_enum(ReasonCode, row.get("reason_code", "COMPUTED"), f"{name}.reason_code"),
        detail=_text(row.get("detail", ""), f"{name}.detail", maximum=512, allow_empty=True),
        evidence=_strings(row.get("evidence", []), f"{name}.evidence"),
    )


def _metric_result(payload: Any, name: str) -> MetricResult | None:
    if payload is None:
        return None
    row = _object(payload, name)
    _only(
        row,
        {"status", "value", "unit", "method_version", "as_of", "window", "frequency", "observations", "coverage_bps", "reason_code", "excluded_positions", "sources", "assumptions", "limitations"},
        name,
    )
    return MetricResult(
        status=_enum(MetricStatus, _required(row, "status", name), f"{name}.status"),
        value=row.get("value"),
        unit=_text(_required(row, "unit", name), f"{name}.unit", maximum=128),
        method_version=_text(_required(row, "method_version", name), f"{name}.method_version", maximum=128),
        as_of=_timestamp(_required(row, "as_of", name), f"{name}.as_of"),
        window=None if row.get("window") is None else _text(row["window"], f"{name}.window", maximum=128),
        frequency=None if row.get("frequency") is None else _text(row["frequency"], f"{name}.frequency", maximum=128),
        observations=_optional_integer(row.get("observations"), f"{name}.observations", minimum=0),
        coverage_bps=_optional_integer(row.get("coverage_bps"), f"{name}.coverage_bps", minimum=0),
        reason_code=_enum(ReasonCode, row.get("reason_code", "COMPUTED"), f"{name}.reason_code"),
        excluded_positions=_strings(row.get("excluded_positions", []), f"{name}.excluded_positions"),
        sources=_strings(row.get("sources", []), f"{name}.sources"),
        assumptions=_strings(row.get("assumptions", []), f"{name}.assumptions"),
        limitations=_strings(row.get("limitations", []), f"{name}.limitations"),
    )


# How far a route's own USD spend may sit from the size the request asked for. Routers round and
# price in their own numeraire, so exact equality would refuse real quotes; a percent is wide
# enough for that and far too narrow to hide a route spending an order of magnitude more.
ROUTE_NOTIONAL_TOLERANCE_BPS = 100
# The most a route may keep of what is paid -- fees, impact and slippage tolerance together, from
# Fenorium's mark of its guaranteed output -- and still be offered without sale-cost acceptance.
MAX_ROUTE_COST_BPS = 300
# No confirmation can accept more than this: broken routes (9,999 bp on thin pools) are never signable.
MAX_ACCEPTED_SALE_COST_BPS = 1000
MAX_ACCEPTED_PURCHASE_COST_BPS = 1000
# A lending request's whole server work (Morpho market, chain reads, Morpho's policy check) shares one
# deadline, shorter than the dashboard's wait for it (LENDING_READ_MS in web/app.js), so an answer the
# server finishes is never one the browser has already abandoned.
LENDING_RESPONSE_SECONDS = 18


def _usd_micros(value: Any) -> int | None:
    """A router's plain-decimal USD figure as integer micros, or None when it is not one."""
    if not isinstance(value, str) or _PLAIN_DECIMAL_USD.fullmatch(value) is None:
        return None
    whole, _, fraction = value.partition(".")
    return int(whole) * 1_000_000 + int((fraction + "000000")[:6])


_PLAIN_DECIMAL_USD = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,18})?")
_PLAIN_DECIMAL = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,18})?")


def _spends_the_requested_size(binding: Any, requested_notional_usd_micros: int) -> bool:
    """Whether the bound route spends what the liquidity and cost checks were run against.

    A route with no USD figure cannot be compared, and an unverifiable spend is refused rather
    than assumed to match: the instruction moves real money and the checks that cleared it were
    run at one particular size.
    """

    if binding is None:
        return False
    return _usd_within_the_requested_size(binding.from_amount_usd, requested_notional_usd_micros)


def _usd_within_the_requested_size(from_amount_usd: Any, requested_notional_usd_micros: int) -> bool:
    """Whether a router's USD figure lies within ROUTE_NOTIONAL_TOLERANCE_BPS of the requested notional."""
    if from_amount_usd is None:
        return False
    raw = str(from_amount_usd)
    # Bounded before conversion, not after. `Fraction("1e1000000000")` expands the exponent into
    # an integer with a billion digits while the request that carried it stays inside any body
    # limit, and this runs whether or not the gate is open. A router's USD figure is a plain
    # decimal, so anything that is not one is refused rather than parsed.
    if _PLAIN_DECIMAL.fullmatch(raw) is None:
        return False
    try:
        route_micros = Fraction(raw) * 1_000_000
    except (ValueError, ZeroDivisionError):
        return False
    if requested_notional_usd_micros <= 0:
        return False
    difference = abs(route_micros - requested_notional_usd_micros)
    return difference * 10_000 <= ROUTE_NOTIONAL_TOLERANCE_BPS * requested_notional_usd_micros


_DIGITS = re.compile(r"[0-9]{1,78}")


def _ceil_usd_micros(numerator: Fraction) -> int:
    micros = numerator * 1_000_000
    return -((-micros.numerator) // micros.denominator)


def _route_fees(payload: Any) -> tuple[list[dict[str, Any]] | None, int | None, int | None]:
    """The fees a route charges, as LI.FI states them, split between LI.FI and the integrator.

    Every route LI.FI quotes today carries its own fixed fee, and the user pays it out of what they
    send; a response saying fees are zero while one is being taken says something untrue. A route
    that does not state its fees at all is not fully costed, exactly like one that will not state
    its gas: `(None, None, None)`. An explicit empty list is a statement that nothing is charged.
    Dollar figures are rounded up, because a cost is the one thing a disclosure must not understate.
    """
    estimate = payload.get("estimate") if isinstance(payload, Mapping) else None
    costs = estimate.get("feeCosts") if isinstance(estimate, Mapping) else None
    if not isinstance(costs, list):
        return None, None, None
    fees: list[dict[str, Any]] = []
    lifi = Fraction(0)
    integrator = Fraction(0)
    for cost in costs:
        if not isinstance(cost, Mapping):
            return None, None, None
        token = cost.get("token")
        amount, usd = cost.get("amount"), cost.get("amountUSD")
        split = cost.get("feeSplit")
        if (not isinstance(token, Mapping) or not isinstance(token.get("address"), str)
                or type(token.get("decimals")) is not int
                or not isinstance(amount, str) or _DIGITS.fullmatch(amount) is None
                # Bounded before conversion; see _spends_the_requested_size.
                or not isinstance(usd, str) or _PLAIN_DECIMAL.fullmatch(usd) is None
                or type(cost.get("included")) is not bool):
            return None, None, None
        raw, dollars = int(amount), Fraction(usd)
        lifi_raw, integrator_raw = raw, 0
        if split is not None:
            parts = (split.get("lifiFee"), split.get("integratorFee")) if isinstance(split, Mapping) else (None, None)
            if not all(isinstance(part, str) and _DIGITS.fullmatch(part) for part in parts):
                return None, None, None
            lifi_raw, integrator_raw = int(parts[0]), int(parts[1])
            if lifi_raw + integrator_raw != raw:
                # A split that does not add up to the fee is not a description of it.
                return None, None, None
        if raw:
            lifi += dollars * lifi_raw / raw
            integrator += dollars * integrator_raw / raw
        fees.append({
            "name": str(cost.get("name") or "")[:64],
            "token": str(token["address"]).lower(),
            "token_chain_id": token.get("chainId") if type(token.get("chainId")) is int else None,
            "token_decimals": token["decimals"],
            "amount": str(raw),
            "amount_usd": usd,
            "included": cost["included"],
            "lifi_fee": str(lifi_raw),
            "integrator_fee": str(integrator_raw),
        })
    return fees, _ceil_usd_micros(lifi), _ceil_usd_micros(integrator)


def _integrator_fee_is_as_configured(
    fees: list[dict[str, Any]], *, configured: Fraction | None, pay_address: str, amount_raw: int,
) -> bool:
    """Whether the integrator's share of this route is the one this deployment configured.

    Unconfigured, it must be nothing: an integrator fee on a route nobody asked to charge one is
    somebody else's fee, taken from our user. Configured, it must be present and never more than the
    configured fraction of what is paid. That is a bound rather than an equality on purpose: LI.FI
    keeps a share of an integrator's fee under terms it does not publish, so the integrator's part
    of the split can be below the configured rate, and a route that charged the user less than we
    disclosed is not the failure this guards against.
    """
    share = 0
    for fee in fees:
        integrator = int(fee["integrator_fee"])
        if integrator and fee["token"] != pay_address.lower():
            # A cut taken in some other token cannot be held against the amount paid.
            return False
        share += integrator
    if configured is None:
        return share == 0
    ceiling = amount_raw * configured
    return 0 < share <= ceiling.numerator // ceiling.denominator + 1


# Venues whose fixed-rate positions this server can deposit into directly. Each needs a market
# address verified on chain and a deposit call it knows the exact shape of.
_DEPOSIT_AT_MATURITY = "0x" + keccak256(b"depositAtMaturity(uint256,uint256,uint256,address)")[:4].hex()
# The deposit must return at least the principal plus this share of the yield shown at the rate the
# engine priced, or it reverts. A larger deposit dilutes a fixed pool's earnings, and the chain can
# move between quote and signature; the floor is what makes "the rate we showed you, or nothing
# happens" true rather than hoped for.
YIELD_FLOOR_BPS = 9_000
DEPOSIT_VALIDITY_SECONDS = 120
_YEAR_SECONDS = 365 * 86_400


def _word(value: int | str) -> str:
    return (value[2:].lower() if isinstance(value, str) else f"{value:x}").rjust(64, "0")


_APPROVE_SELECTOR = "0x095ea7b3"
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
# INVALID says LI.FI cannot account for the hash, not that anything was delivered or refunded, so it
# leaves a purchase unresolved rather than settling it.
_LIFI_TERMINAL = frozenset({"DONE", "FAILED"})
# LI.FI's documented FAILED substatuses (status-tracking guide) that the engine's published
# observation vocabulary predates. They are all failures; which of them leave a refund to claim is
# what a buyer needs to know. REFUNDED under FAILED means the refund already happened.
_LIFI_FAILURES = frozenset({
    "NOT_PROCESSABLE_REFUND_NEEDED", "OUT_OF_GAS", "SLIPPAGE_EXCEEDED", "INSUFFICIENT_ALLOWANCE",
    "INSUFFICIENT_BALANCE", "EXPIRED",
})


def _history_balances(payload: Any) -> dict[tuple[int, str], tuple[int, int]]:
    """The balances a caller read at its history's last block, one per asset."""
    if not isinstance(payload, list) or len(payload) > 2_000:
        raise APIError("INVALID_FIELD", "operation.balances must be a bounded array")
    balances: dict[tuple[int, str], tuple[int, int]] = {}
    for index, entry in enumerate(payload):
        row = _object(entry, f"balances[{index}]")
        _only(row, {"chain_id", "token", "raw_amount", "block_number"}, f"balances[{index}]")
        key = (_integer(_required(row, "chain_id", "balance"), "balance.chain_id", minimum=1),
               _evm_address(_required(row, "token", "balance"), "balance.token"))
        if key in balances:
            raise APIError("INVALID_FIELD", "operation.balances names one asset twice")
        raw = _required(row, "raw_amount", "balance")
        # Zero is a balance here: an asset bought and fully sold still made or lost money.
        if not isinstance(raw, str) or re.fullmatch(r"[0-9]{1,78}", raw) is None or int(raw) >= 2 ** 256:
            raise APIError("INVALID_AMOUNT", "balance.raw_amount must be a decimal integer string of token units")
        balances[key] = (int(raw), _integer(_required(row, "block_number", "balance"), "balance.block_number", minimum=0))
    return balances


def _route_chain(value: Any, name: str) -> int:
    if isinstance(value, str) and value.isdigit() and len(value) <= 12:
        value = int(value)
    if type(value) is not int or value not in ROUTE_CHAINS:
        raise APIError("INVALID_FIELD", f"{name} must be one of the chains this deployment routes between")
    return value


def _received(row: Any) -> dict[str, Any] | None:
    """What LI.FI says arrived, in raw units of the token that arrived, or None if it will not say.

    Nothing here is valued: the token that arrives after a failed destination step is not the one
    that was bought, and naming it plainly is the point.
    """
    if not isinstance(row, Mapping):
        return None
    token, amount = row.get("token"), row.get("amount")
    if (not isinstance(token, Mapping) or not isinstance(amount, str) or _DIGITS.fullmatch(amount) is None
            or len(amount) > 78 or not isinstance(token.get("address"), str)
            or re.fullmatch(r"0x[0-9a-fA-F]{40}", token["address"]) is None
            or type(token.get("decimals")) is not int or not 0 <= token["decimals"] <= 77
            or type(token.get("chainId")) is not int):
        return None
    symbol = token.get("symbol")
    return {
        "chain_id": token["chainId"],
        "token": token["address"].lower(),
        "symbol": symbol[:32] if isinstance(symbol, str) else None,
        "decimals": token["decimals"],
        "amount": amount,
    }


def _approval_for(
    binding: Any, *, pay_address: str, wallet: str, amount_raw: int, router_target: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The ERC-20 allowance a route needs before it can spend, or why it cannot have one.

    Exact amount, never unlimited: an approval outlives the route it was granted for, and an
    unlimited one hands a standing claim on the wallet to whatever the spender later becomes.
    The spender comes from the route this server fetched and must be the contract the route
    calls. Approving one address and sending the swap to another is the shape an interception
    would take, so a route whose two addresses disagree is not offered.
    """
    if pay_address.lower() in NATIVE_TOKEN_SENTINELS:
        return None, None
    spender = (binding.approval_address or "").lower()
    if not spender:
        return None, "MISSING_APPROVAL_TARGET"
    if router_target is not None and spender != router_target.lower():
        return None, "APPROVAL_TARGET_MISMATCH"
    return {
        "chainId": binding.from_chain,
        "from": wallet,
        "to": pay_address,
        "data": _APPROVE_SELECTOR + spender[2:].rjust(64, "0") + f"{amount_raw:064x}",
        "value": "0",
        "spender": spender,
        "amount": str(amount_raw),
    }, None


# Our own evidence failing, as opposed to the instrument being refused.
_UNIVERSE_FAILURES = frozenset({"UNIVERSE_UNAVAILABLE", "UNIVERSE_EXPIRED"})


def _instrument_refusals(instrument: Any, *, now: datetime) -> list[str]:
    """Why this server would not route a purchase of this instrument at all.

    Checked before anything is fetched, because these are our own answers and asking a vendor
    about a trade we have already refused spends a rate-limited budget to learn nothing. The
    instrument's own refusals are the registry's (`engine.registry.purchase_refusals`), the same
    rule `/v1/registry` publishes, so what a buyer is told is buyable and what this endpoint
    routes cannot disagree.
    """
    _path, refusals = purchase_refusals({
        "chain_id": instrument.chain_id,
        "address": instrument.address,
        "decimals": instrument.decimals,
        "category": instrument.category,
        "venue": instrument.venue,
        "verdict": instrument.verdict,
        "gate": {"status": instrument.transfer_gate},
        "maturity": instrument.raw.get("maturity"),
        "maturity_timestamp": instrument.raw.get("maturity_timestamp"),
    }, registry(), now=now)
    return refusals


def _route_pay_refusals(instrument: Any, *, pay_address: str, pay_chain: int) -> list[str]:
    refusals = []
    if pay_address.lower() == instrument.address.lower() and pay_chain == instrument.chain_id:
        # The same address on another chain is another token.
        refusals.append("PAY_ASSET_IS_THE_INSTRUMENT")
    if pay_chain != instrument.chain_id and pay_chain not in ROUTE_CHAINS:
        refusals.append("PAY_CHAIN_UNSUPPORTED")
    return refusals


@dataclass(slots=True)
class PlatformAPI:
    """Deterministic endpoint dispatcher with optional explicit bundle export."""

    _history_slots: ClassVar[threading.BoundedSemaphore] = threading.BoundedSemaphore(
        MAX_CONCURRENT_HISTORY_VERIFICATIONS)

    _lending_request_guard: ClassVar[threading.Lock] = threading.Lock()
    _lending_request_locks: ClassVar[dict[tuple[str, str], tuple[Any, int]]] = {}

    evidence_dir: Path | None = None
    # Closed unless a deployment declares otherwise. Constructing an API without saying
    # anything about execution must never be the thing that turns it on.
    gate: ExecutionGate = CLOSED
    # Authorisation is judged against this, never against the caller's `as_of`. A request
    # supplies its own timestamp so an analysis can be replayed as it stood; letting that same
    # value decide whether a quote is still live hands the clock to whoever is asking.
    clock: Callable[[], datetime] | None = None
    # Looked up at call time, so a test that patches `time.monotonic` steers every deadline here.
    monotonic: Callable[[], float] = lambda: time.monotonic()
    # The two collaborators that make a route this server's own observation rather than the
    # caller's claim. Absent, `/v1/execution-routes` refuses: a deployment that cannot ask a
    # router, or cannot say which instrument it is being asked about, has nothing to offer.
    router: Any = None
    instruments: Any = None
    midnight_policy: Any = None
    midnight_orders_enabled: bool = False
    orders: Any = None
    principal_token_store: Any = None
    order_reader_factory: Any = ChainReader
    exit_provider: Any = field(default_factory=MorphoExits)
    morpho_public_book: Any = field(default_factory=MorphoPublicBook)
    # Daily price histories published beside the universe (deploy/platform/price_history.py).
    # Absent, analytics runs on whatever return series the caller supplies, as it always has.
    price_history: Any = None
    # Current prices the acquisition job publishes (deploy/platform/current_prices.py). Absent,
    # holdings are valued only from the observations a caller supplies.
    current_prices: Any = None
    # Held-token prices fetched by the wallet reader when absent from the published feed.
    on_demand: Any = None
    transfer_verifier: Any = None
    history_source: Any = None
    token_decimals: Any = None
    # Route records and the fee ledger (deploy/platform/route_store.py). Absent, routes are
    # answered but not kept, as before.
    routes: Any = None

    @staticmethod
    def _dispatch_paths() -> tuple[str, ...]:
        """The paths `handle` knows. `service.POST_PATHS` must match, or one of them 404s."""
        return (
            "/v1/wallet-snapshots",
            "/v1/wallet-history",
            "/v1/portfolio-analytics",
            "/v1/scenarios",
            "/v1/opportunities",
            "/v1/execution-quotes",
            "/v1/execution-routes",
            "/v1/sale-routes",
            "/v1/route-previews",
            "/v1/routes/submitted",
            "/v1/position-deposits",
            "/v1/position-exits",
            "/v1/lending-orders",
            "/v1/lending-orders/publish",
            "/v1/lending-orders/cancel",
            "/v1/lending-orders/list",
            "/v1/decision-bundles",
        )

    def handle(self, path: str, body: Any) -> dict[str, Any]:
        operation = _object(body, "request body")
        canonical_data(operation)  # Reject floats before they reach any authoritative adapter.
        handlers = {
            "/v1/wallet-snapshots": self.wallet_snapshot,
            "/v1/wallet-history": self.wallet_history,
            "/v1/portfolio-analytics": self.portfolio_analytics,
            "/v1/scenarios": self.scenario,
            "/v1/opportunities": self.opportunities,
            "/v1/execution-quotes": self.execution_quote,
            "/v1/execution-routes": self.execution_route,
            "/v1/sale-routes": self.sale_route,
            "/v1/route-previews": self.route_previews,
            "/v1/routes/submitted": self.route_submitted,
            "/v1/position-deposits": self.position_deposit,
            "/v1/position-exits": self.position_exit,
            "/v1/lending-orders": self.lending_order,
            "/v1/lending-orders/publish": self.lending_order_publish,
            "/v1/lending-orders/cancel": self.lending_order_cancel,
            "/v1/lending-orders/list": self.lending_order_list,
            "/v1/decision-bundles": self.decision_bundle,
        }
        handler = handlers.get(path)
        if handler is None:
            raise APIError("NOT_FOUND", "endpoint not found", status=404)
        return self.envelope(handler(operation))

    def envelope(self, data: Any) -> dict[str, Any]:
        """The one place a response says whether this deployment executes and whether it charges."""
        fees_enabled = self._configured_fee() is not None
        return {
            "schema_version": PLATFORM_RESPONSE_SCHEMA_V2 if fees_enabled else PLATFORM_RESPONSE_SCHEMA,
            # Every instruction this deployment issues settles on one chain -- routes are fetched
            # with the same source and destination chain, deposits are direct calls -- so that is
            # the execution a caller is told about. A cross-chain route is still judged by its own
            # scope where it is answered.
            "execution_enabled": self.gate.is_open_for(cross_chain=False),
            "fees_enabled": fees_enabled,
            "data": data,
        }

    def _configured_fee(self) -> Fraction | None:
        fee = getattr(self.router, "fee", None)
        return fee if isinstance(fee, Fraction) and fee > 0 else None

    @staticmethod
    def _operation(operation: Mapping[str, Any], schema: str, extra: set[str]) -> tuple[str, datetime]:
        _only(operation, {"schema_version", "method_version", "as_of"} | extra, "operation")
        if operation.get("schema_version") != schema:
            raise APIError("SCHEMA_VERSION_MISMATCH", f"operation.schema_version must be {schema}")
        method = _text(_required(operation, "method_version", "operation"), "operation.method_version", maximum=128)
        return method, _timestamp(_required(operation, "as_of", "operation"), "operation.as_of")

    def wallet_snapshot(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        self._operation(operation, WALLET_OPERATION_SCHEMA, {"request", "observations", "enumerated"})
        _, snapshot = _discover(operation)
        return snapshot.to_dict()

    def wallet_history(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        """A wallet's past transfers, labelled where the registry recognises the token.

        The history itself values nothing. A v2 request also gets `transfer_values`: each
        transfer at the stored daily price nearest its moment, with that price's day, distance and
        source -- or the reason there is no value.
        """

        v2 = operation.get("schema_version") == HISTORY_OPERATION_SCHEMA_V2
        method, as_of = self._operation(
            operation, HISTORY_OPERATION_SCHEMA_V2 if v2 else HISTORY_OPERATION_SCHEMA,
            {"wallet", "chain_ids", "transfers", "registry"}
            | ({"balances", "indexed_chain_ids"} if v2 else set()))
        wallet = _text(_required(operation, "wallet", "operation"), "operation.wallet", maximum=42)
        raw_chains = _required(operation, "chain_ids", "operation")
        if not isinstance(raw_chains, list) or not 1 <= len(raw_chains) <= 6:
            raise APIError("INVALID_FIELD", "operation.chain_ids must hold between one and six chains")
        chain_ids = tuple(
            _integer(value, "operation.chain_ids[]", minimum=1) for value in raw_chains)
        indexed_chain_ids: tuple[int, ...] = ()
        if v2 and "indexed_chain_ids" in operation:
            raw_indexed = operation["indexed_chain_ids"]
            if (not isinstance(raw_indexed, list) or not 1 <= len(raw_indexed) <= 6
                    or any(type(value) is not int or value not in chain_ids for value in raw_indexed)):
                raise APIError("INVALID_FIELD", "operation.indexed_chain_ids must hold between one and six "
                               "integer chain IDs present in chain_ids")
            indexed_chain_ids = tuple(raw_indexed)
        registry_rows = operation.get("registry", [])
        if not isinstance(registry_rows, list) or len(registry_rows) > 2_000:
            raise APIError("INVALID_FIELD", "operation.registry must be a bounded array")
        registered_by_key: dict[tuple[int, str], str] = {}
        for index, entry in enumerate(registry_rows):
            row = _object(entry, f"registry[{index}]")
            _only(row, {"chain_id", "token", "identity"}, f"registry[{index}]")
            key = (
                _integer(_required(row, "chain_id", "registry"), "registry.chain_id", minimum=1),
                _text(_required(row, "token", "registry"), "registry.token", maximum=42).lower(),
            )
            registered_by_key[key] = _text(
                _required(row, "identity", "registry"), "registry.identity", maximum=128)
        verifying = self.transfer_verifier is not None
        expensive = verifying or (v2 and "balances" in operation)
        if expensive and not self._history_slots.acquire(blocking=False):
            raise APIError("WALLET_READS_BUSY", "wallet reads are busy", status=503)
        try:
            response_deadline_at = self.monotonic() + HISTORY_RESPONSE_SECONDS
            verifier = _DeadlineVerifier(
                self.transfer_verifier, deadline_at=response_deadline_at,
                monotonic=self.monotonic) if verifying else None
            source: _RecordedHistory | _IndexedAndRecorded = _RecordedHistory(
                _required(operation, "transfers", "operation"))
            if indexed_chain_ids:
                source = _IndexedAndRecorded(
                    source, self.history_source, indexed_chain_ids, HISTORY_INDEX_CHAIN_IDS,
                    deadline_at=response_deadline_at, monotonic=self.monotonic)
            balances = _history_balances(operation["balances"]) if v2 and "balances" in operation else {}
            priority_chain_ids = tuple(dict.fromkeys(chain_id for chain_id, _ in balances)) or chain_ids[:1]
            history = load_wallet_history(
                wallet,
                chain_ids=chain_ids,
                source=source,
                as_of=as_of,
                method_version=method,
                registered_by_key=registered_by_key,
                verifier=verifier,
                priority_chain_ids=priority_chain_ids,
            )
            if not v2:
                return history.to_dict()
            known_decimals = self._decimals_resolver()
            values = self._transfer_values(history, known_decimals)
            indexed = ([source.statuses[chain_id] for chain_id in sorted(source.statuses)]
                       if isinstance(source, _IndexedAndRecorded) else [])
            result = {"history": history.to_dict(), "transfer_values": values, "indexed": indexed}
            if "balances" in operation:
                pairs: set[tuple[int, str]] = set()

                def record_price(chain_id: int, token: str) -> None:
                    pairs.add((chain_id, token.lower()))

                # This first pass is pure computation; discard its result and record only needed prices.
                wallet_performance(
                    list(history.events), values["values"], balances, record_price,
                    chain_complete={chain.chain_id: chain.complete for chain in history.scanned_chains},
                    known_decimals=known_decimals)
                prices = self._fresh_prices(sorted(pairs), deadline_at=response_deadline_at) if pairs else {}
                result["performance"] = wallet_performance(
                    list(history.events), values["values"], balances,
                    lambda chain_id, token: prices.get((chain_id, token.lower())),
                    chain_complete={chain.chain_id: chain.complete for chain in history.scanned_chains},
                    known_decimals=known_decimals)
            return result
        except PortfolioContractError as exc:
            raise APIError("CONTRACT_REJECTED", safe_detail(exc)) from exc
        finally:
            if expensive:
                self._history_slots.release()

    def _decimals_resolver(self) -> Callable[[int, str], int | None]:
        decimals_by_key = {
            (row["chain_id"], row["address"].lower()): row["decimals"]
            for row in registry_assets(registry(), ()) if row["address"] is not None
        }

        def known_decimals(chain_id: int, token: str) -> int | None:
            decimals = decimals_by_key.get((chain_id, token.lower()))
            if decimals is None and self.instruments is not None:
                decimals = self.instruments.decimals(chain_id, token)
            if decimals is None and self.token_decimals is not None:
                decimals = self.token_decimals(chain_id, token)
            return decimals

        return known_decimals

    def _fresh_price(self, chain_id: int, token: str, *, deadline_at: float | None = None) -> int | None:
        """Prefer a fresh published price, then a recent on-demand DefiLlama observation."""
        now = (self.clock or (lambda: datetime.now(UTC)))()
        if self.current_prices is not None:
            observation = self.current_prices.observation(chain_id, token, identity="performance")
            if observation is not None and 0 <= (now - observation.observed_at).total_seconds() <= CURRENT_PRICE_MAX_AGE_SECONDS:
                return observation.price_usd_fixed
        return self._on_demand_price(chain_id, token, now, deadline_at=deadline_at)

    def _fresh_prices(self, pairs: Iterable[tuple[int, str]], *,
                      deadline_at: float) -> dict[tuple[int, str], int | None]:
        now = (self.clock or (lambda: datetime.now(UTC)))()
        prices: dict[tuple[int, str], int | None] = {}
        pending: dict[tuple[int, str], str] = {}
        for chain_id, token in pairs:
            pair = (chain_id, token)
            prices[pair] = None
            if self.current_prices is not None:
                observation = self.current_prices.observation(chain_id, token, identity="performance")
                if observation is not None and 0 <= (now - observation.observed_at).total_seconds() <= CURRENT_PRICE_MAX_AGE_SECONDS:
                    prices[pair] = observation.price_usd_fixed
                    continue
            chain = registry().get(chain_id)
            key = None if chain is None else chain.llama_coin(token)
            if key is not None:
                pending[pair] = key
        if pending and self.on_demand is not None:
            remaining = deadline_at - self.monotonic()
            if remaining > 0:
                now_text = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
                entries = self.on_demand.entries(list(dict.fromkeys(pending.values())),
                                                 now_text=now_text, budget_seconds=remaining)
                for pair, key in pending.items():
                    prices[pair] = self._on_demand_entry_price(entries.get(key))
        return prices

    def _on_demand_price(self, chain_id: int, token: str | None, now: datetime, *,
                         deadline_at: float | None = None) -> int | None:
        if self.on_demand is None:
            return None
        chain = registry().get(chain_id)
        key = None if chain is None else chain.llama_coin(token)
        if key is None:
            return None
        now_text = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if deadline_at is None:
            entries = self.on_demand.entries([key], now_text=now_text)
        else:
            remaining = deadline_at - self.monotonic()
            if remaining <= 0:
                return None
            entries = self.on_demand.entries([key], now_text=now_text, budget_seconds=remaining)
        return self._on_demand_entry_price(entries.get(key))

    def _on_demand_entry_price(self, entry: Any) -> int | None:
        if not isinstance(entry, Mapping) or entry.get("status") != "PRICED":
            return None
        price = entry.get("price_usd_fixed")
        observed_text = entry.get("observed_at")
        if type(price) is not int or price <= 0 or not isinstance(observed_text, str):
            return None
        try:
            observed_at = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if observed_at.tzinfo is None:
            return None
        # The source can observe the price while the request is in flight, so its age is judged
        # against the clock after the fetch, not the moment before it.
        checked = (self.clock or (lambda: datetime.now(UTC)))()
        age = (checked - observed_at.astimezone(UTC)).total_seconds()
        return price if 0 <= age <= CURRENT_PRICE_MAX_AGE_SECONDS else None

    def _transfer_values(self, history: Any, known_decimals: Callable[[int, str], int | None]) -> dict[str, Any]:
        values = []
        for event in history.events:
            row: dict[str, Any] = {"identity": event.identity, "verified": event.verified,
                                   "value_usd_micros": None, "price": None, "reason": None}
            point = None if self.price_history is None else self.price_history.price_at(
                event.chain_id, event.token, event.occurred_at)
            decimals = known_decimals(event.chain_id, event.token)
            if decimals is None:
                row["reason"] = "DECIMALS_UNKNOWN"
            elif event.decimals is not None and event.decimals != decimals:
                # The verifier confirms the raw amount, never the precision: a claim that
                # disagrees with trusted metadata is not valued, an absent one takes the trusted value.
                row["reason"] = "DECIMALS_MISMATCH"
            elif point is None:
                row["reason"] = "NO_PRICE_NEAR_THIS_MOMENT"
            else:
                price = Decimal(point["price_usd"])
                if price <= 0:
                    row["reason"] = "NO_PRICE_NEAR_THIS_MOMENT"
                else:
                    row["price"] = point
                    numerator, denominator = price.as_integer_ratio()
                    row["value_usd_micros"] = (
                        event.raw_amount * numerator * USD_MICROS
                        // (denominator * 10**decimals)
                    )
            values.append(row)
        return {
            "schema_version": TRANSFER_VALUES_SCHEMA,
            "method": "the stored UTC daily price nearest each transfer's block time, at most three days away",
            "values": values,
            "valued": sum(1 for row in values if row["value_usd_micros"] is not None),
            "unvalued": sum(1 for row in values if row["value_usd_micros"] is None),
        }

    def portfolio_analytics(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of = self._operation(
            operation,
            ANALYTICS_OPERATION_SCHEMA,
            {"request", "observations", "enumerated", "price_observations", "return_series",
             "max_price_age_seconds"},
        )
        history = self.price_history
        covers = None if history is None else (lambda chain_id, address: history.covers(chain_id, address, as_of=as_of))
        _, snapshot = _discover(operation, covers)
        prices = operation.get("price_observations", [])
        returns = operation.get("return_series", [])
        if not isinstance(prices, list) or len(prices) > 10_000:
            raise APIError("INVALID_FIELD", "price_observations must be a bounded array")
        if not isinstance(returns, list) or len(returns) > 2_000:
            raise APIError("INVALID_FIELD", "return_series must be a bounded array")
        observations = [_price_observation(row, index) for index, row in enumerate(prices)]
        if any(row.independence_key == DEFILLAMA_CURRENT for row in observations):
            # That source is this server's own feed, held to its own age limit. A caller naming it
            # would be borrowing both the name and the limit for evidence the server never saw.
            raise APIError("INVALID_FIELD", f"{DEFILLAMA_CURRENT} observations are supplied by this server")
        if self.current_prices is not None:
            for position in snapshot.positions:
                own = self.current_prices.observation(
                    position.asset.chain_id, position.asset.address, identity=position.identity)
                if own is not None:
                    observations.append(own)
        return self.analyse(
            snapshot, observations, [_return_series(row, index) for index, row in enumerate(returns)],
            as_of=as_of, method=method,
            max_price_age_seconds=_integer(operation.get("max_price_age_seconds", 300), "max_price_age_seconds", minimum=0),
        )

    def analyse(self, snapshot: Any, observations: list[PriceObservation], series: list[ReturnSeries], *,
                as_of: datetime, method: str, max_price_age_seconds: int,
                scope: AnalyticsScope = AnalyticsScope.TOTAL,
                position_values_usd_micros: Mapping[str, int | None] | None = None) -> dict[str, Any]:
        """Value a snapshot, then fill risk from the server's histories. Shared by the analytics
        endpoint, which is handed a snapshot's evidence, and the wallet read, which gathers it."""
        history = self.price_history
        # Reads the snapshot end to end. Narrowing to registered positions here is what let a
        # confirmed holding nobody priced sit outside the denominator while the total called
        # itself authoritative.
        valuation = value_wallet(
            snapshot,
            tuple(observations),
            as_of=as_of,
            method_version=method,
            max_price_age_seconds=max_price_age_seconds,
            source_max_age_seconds={DEFILLAMA_CURRENT: CURRENT_PRICE_MAX_AGE_SECONDS},
            position_values_usd_micros=position_values_usd_micros,
        )
        allocation = allocation_and_concentration(valuation, method_version=method)
        series = list(series)
        if history is not None:
            # A caller's own series stands; the server adds its histories for the other priced
            # positions, as many as keep the aligned window qualifying (see select_cohort).
            supplied = {row.position_id for row in series}
            # Only holdings the engine will use: priced and risk-supported. A history for a holding
            # it drops anyway must not compete for the shared window.
            eligible = {valued.identity for valued in valuation.positions
                        if valued.value_usd_micros and valued.position.risk.status is CapabilityStatus.SUPPORTED}
            candidates = []
            for valued in valuation.positions:
                asset = valued.position.asset
                if valued.identity in supplied or valued.identity not in eligible:
                    continue
                own = history.return_series(asset.chain_id, asset.address, position_id=valued.identity,
                                            as_of=as_of, method_version=method,
                                            **({"instrument_id": asset.instrument_id}
                                               if asset.kind is AssetKind.POSITION else {}))
                if own is not None:
                    candidates.append((valued.value_usd_micros, own))
            # Likewise only the caller's series the engine will use constrain it: eligible, daily,
            # this method, and in the 365-day cohort the server's histories join.
            aligned_with = [row for row in series if row.position_id in eligible
                            and row.frequency is MarkingFrequency.UTC_DAILY and row.method_version == method
                            and row.annualization_factor == 365]
            series += select_cohort(aligned_with, candidates, as_of=as_of)
        analytics = compute_portfolio_analytics(
            valuation,
            tuple(series),
            as_of=as_of,
            method_version=method,
            scope=scope,
        )
        result = {
            "wallet_snapshot": snapshot.to_dict(),
            "valuation": valuation.to_dict(),
            "allocation": allocation.to_dict(),
            "analytics": analytics.to_dict(),
        }
        if scope is AnalyticsScope.VALUED_HOLDINGS:
            excluded = [{"identity": row.identity, "symbol": row.position.asset.symbol,
                         "reason": row.reason_code.value}
                        for row in valuation.positions if row.value_usd_micros is None]
            excluded += [{"identity": identity, "symbol": "", "reason": "UNRESOLVED_POSITION"}
                         for identity in valuation.unvalued_identities]
            modelled = sum(row.provenance in ("modelled", "spliced") for row in series
                           if row.position_id in analytics.included_positions)
            result["risk_scope"] = {
                "nav_scope": scope.value,
                "valued_usd_micros": valuation.priced_nav_lower_bound_usd_micros,
                "excluded": excluded, "modelled_positions": modelled,
            }
        return result

    def scenario(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        _, as_of = self._operation(operation, SCENARIO_OPERATION_SCHEMA, {"transformation"})
        return evaluate_scenario(_transformation(_required(operation, "transformation", "operation"), as_of=as_of)).to_dict()

    def opportunities(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of = self._operation(operation, OPPORTUNITY_OPERATION_SCHEMA, {"objective", "candidates"})
        objective = _enum(Objective, _required(operation, "objective", "operation"), "operation.objective")
        candidate_rows = _required(operation, "candidates", "operation")
        if not isinstance(candidate_rows, list) or len(candidate_rows) > 1_000:
            raise APIError("INVALID_FIELD", "operation.candidates must be a bounded array")
        candidates: list[OpportunityCandidate] = []
        for index, payload in enumerate(candidate_rows):
            row = _object(payload, f"candidates[{index}]")
            _only(
                row,
                {"identity", "transformation", "mandate", "mandate_inputs", "primary_objective_delta", "net_executable_income_usd_micros", "liquidity_bps", "evidence_age_seconds", "before_expected_shortfall", "after_expected_shortfall"},
                f"candidates[{index}]",
            )
            transformation = _transformation(_required(row, "transformation", "candidate"), as_of=as_of)
            scenario = evaluate_scenario(transformation)
            mandate_payload = _object(_required(row, "mandate", "candidate"), "mandate")
            _only(
                mandate_payload,
                {
                    "allowed_issuers",
                    "allowed_protocols",
                    "allowed_chain_ids",
                    "protocol_max_bps",
                    "min_liquidity_usd_micros",
                    "max_maturity_days",
                    "required_credentials",
                    "require_supported_permission",
                },
                "mandate",
            )
            allowed_chain_ids = mandate_payload.get("allowed_chain_ids", [])
            if not isinstance(allowed_chain_ids, list) or len(allowed_chain_ids) > 256:
                raise APIError("INVALID_FIELD", "mandate.allowed_chain_ids must be a bounded array")
            mandate = Mandate(
                allowed_issuers=frozenset(_strings(mandate_payload.get("allowed_issuers", []), "mandate.allowed_issuers")),
                allowed_protocols=frozenset(_strings(mandate_payload.get("allowed_protocols", []), "mandate.allowed_protocols")),
                allowed_chain_ids=frozenset(_integer(value, "mandate.allowed_chain_ids[]", minimum=1) for value in allowed_chain_ids),
                protocol_max_bps=_optional_integer(mandate_payload.get("protocol_max_bps"), "mandate.protocol_max_bps", minimum=0),
                min_liquidity_usd_micros=_optional_integer(mandate_payload.get("min_liquidity_usd_micros"), "mandate.min_liquidity_usd_micros", minimum=0),
                max_maturity_days=_optional_integer(mandate_payload.get("max_maturity_days"), "mandate.max_maturity_days", minimum=0),
                required_credentials=frozenset(_strings(mandate_payload.get("required_credentials", []), "mandate.required_credentials")),
                require_supported_permission=_boolean(mandate_payload.get("require_supported_permission", True), "mandate.require_supported_permission"),
            )
            inputs = _object(_required(row, "mandate_inputs", "candidate"), "mandate_inputs")
            _only(
                inputs,
                {
                    "issuer",
                    "protocol",
                    "chain_id",
                    "current_protocol_value_usd_micros",
                    "added_value_usd_micros",
                    "total_nav_usd_micros",
                    "available_depth_usd_micros",
                    "maturity_days",
                    "credentials",
                    "permission",
                },
                "mandate_inputs",
            )
            credential_rows = inputs.get("credentials")
            credentials = None if credential_rows is None else frozenset(
                _strings(credential_rows, "mandate_inputs.credentials")
            )
            mandate_result = evaluate_mandate(
                mandate,
                issuer=None if inputs.get("issuer") is None else _text(inputs["issuer"], "mandate_inputs.issuer"),
                protocol=None if inputs.get("protocol") is None else _text(inputs["protocol"], "mandate_inputs.protocol"),
                chain_id=_optional_integer(inputs.get("chain_id"), "mandate_inputs.chain_id", minimum=1),
                current_protocol_value_usd_micros=_optional_integer(inputs.get("current_protocol_value_usd_micros"), "mandate_inputs.current_protocol_value_usd_micros", minimum=0),
                added_value_usd_micros=_integer(_required(inputs, "added_value_usd_micros", "mandate_inputs"), "mandate_inputs.added_value_usd_micros", minimum=0),
                total_nav_usd_micros=_optional_integer(inputs.get("total_nav_usd_micros"), "mandate_inputs.total_nav_usd_micros", minimum=0),
                permission=_capability_result(inputs.get("permission"), "mandate_inputs.permission"),
                available_depth_usd_micros=_optional_integer(inputs.get("available_depth_usd_micros"), "mandate_inputs.available_depth_usd_micros", minimum=0),
                maturity_days=_optional_integer(inputs.get("maturity_days"), "mandate_inputs.maturity_days", minimum=0),
                credentials=credentials,
            )
            candidates.append(
                OpportunityCandidate(
                    identity=_text(_required(row, "identity", "candidate"), "candidate.identity"),
                    objective=objective,
                    scenario=scenario,
                    mandate=mandate_result,
                    primary_objective_delta=_optional_integer(row.get("primary_objective_delta"), "candidate.primary_objective_delta"),
                    net_executable_income_usd_micros=_optional_integer(row.get("net_executable_income_usd_micros"), "candidate.net_executable_income_usd_micros"),
                    liquidity_bps=_optional_integer(row.get("liquidity_bps"), "candidate.liquidity_bps", minimum=0),
                    evidence_age_seconds=_optional_integer(row.get("evidence_age_seconds"), "candidate.evidence_age_seconds", minimum=0),
                    before_expected_shortfall=_metric_result(row.get("before_expected_shortfall"), "candidate.before_expected_shortfall"),
                    after_expected_shortfall=_metric_result(row.get("after_expected_shortfall"), "candidate.after_expected_shortfall"),
                )
            )
        return rank_opportunities(candidates, objective=objective, method_version=method, as_of=as_of).to_dict()

    def execution_quote(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of = self._operation(operation, QUOTE_OPERATION_SCHEMA, {
            "quote", "requested_notional_usd_micros", "router_quote", "requested_from",
        })
        quote = _execution_quote(_required(operation, "quote", "operation"))
        unsigned_request = None
        binding = None
        router_quote = operation.get("router_quote")
        if isinstance(router_quote, dict) and quote.verdict == "BID":
            try:
                binding = parse_quote(
                    router_quote,
                    observed_at=(quote.observed_at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1),
                    validity_seconds=(quote.expires_at - quote.observed_at) // timedelta(seconds=1),
                )
                if binding.route_hash != quote.route_hash:
                    raise RouteError("router quote does not match the analytical route hash")
                unsigned_request = parse_unsigned_transaction_request(
                    router_quote.get("transactionRequest"), binding=binding,
                    requested_from=operation.get("requested_from"),
                ).to_json()
            except (RouteError, TypeError, ValueError):
                # Unparseable or mismatched evidence remains absent, never a default request.
                unsigned_request = None
                binding = None
        requested_notional = _integer(
            _required(operation, "requested_notional_usd_micros", "operation"),
            "operation.requested_notional_usd_micros",
            minimum=1,
        )
        reasons: list[str] = []
        status = "ANALYTICAL_ONLY"
        if quote.verdict == "NO BID":
            status, reasons = "REJECTED", ["NO_BID"]
        elif quote.verdict == "NO QUOTE":
            status, reasons = "REJECTED", ["NO_QUOTE"]
        else:
            if not quote.is_live_at(as_of):
                reasons.append("QUOTE_EXPIRED")
            if quote.available_depth_usd_micros is None:
                reasons.append("MISSING_LIQUIDITY")
            elif quote.available_depth_usd_micros < requested_notional:
                reasons.append("MISSING_LIQUIDITY")
            if not quote.costs.complete:
                reasons.append("MISSING_COST")
            if not reasons:
                status = "QUOTE_SIMULATION_READY"
        # Two independent questions. Whether this quote is sound -- live, deep enough, fully
        # costed, and carrying a request that parsed against its own binding -- and whether this
        # deployment may act on a sound one. Only the second was being asked, so an open gate
        # would have authorised a rejected, expired or unfillable quote.
        quote_ready = status == "QUOTE_SIMULATION_READY"
        now = (self.clock or (lambda: datetime.now(UTC)))()
        if quote_ready and not quote.is_live_at(now):
            # Live when the caller says it was looked at is an analytical claim. Live now is the
            # only one that can authorise, and only the server's clock can make it.
            reasons.append("QUOTE_EXPIRED")
            quote_ready = False
        if quote_ready and unsigned_request is None:
            reasons.append("MISSING_EXECUTION_QUOTE")
            quote_ready = False
        if quote_ready and not _spends_the_requested_size(binding, requested_notional):
            # Depth and costs were checked at the requested size. A route that spends a
            # different amount was never the thing those checks cleared.
            reasons.append("ROUTE_NOTIONAL_MISMATCH")
            quote_ready = False
        if quote_ready:
            # What the deployment still lacks, from the gate rather than from a constant: a
            # response that authorised execution while naming legal review as outstanding was
            # telling the caller two different things.
            reasons.extend(self.gate.reason_codes)
            if self.gate.is_open:
                # Every input above arrived in this request: the verdict, the depth, the costs,
                # the timestamps, the router payload and the hash that ties them together. The
                # hash shows those objects agree with each other, not that any router quoted
                # them or that this engine analysed them -- a caller can edit the transaction,
                # recompute the hash, and have every check agree. So this endpoint analyses
                # supplied evidence and never authorises acting on it. Issuing an instruction
                # needs a quote this server fetched and holds, which is a separate mechanism
                # and does not exist yet.
                reasons.append("UNVERIFIED_QUOTE_PROVENANCE")
        # Deliberately not `quote_ready and self.gate.is_open`. See above.
        authorised = False
        self._persist_quote_observation(
            {
                "schema_version": QUOTE_OPERATION_SCHEMA,
                "method_version": method,
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "requested_notional_usd_micros": requested_notional,
                "quote": quote.to_dict(),
            }
        )
        return {
            "status": status,
            "reason_codes": sorted(reasons),
            "quote": quote.to_dict(),
            "requested_notional_usd_micros": requested_notional,
            # Evidence of what would be signed. Always present when a bound route supplied it,
            # because describing a transaction is not authorising one.
            "unsigned_transaction_request": unsigned_request,
            # The same bytes, offered as an instruction rather than as evidence, and only once
            # every clearance is in. The two fields stay separate so that a reader can tell
            # which of the two it is holding.
            "transaction_request": unsigned_request if authorised else None,
            "broadcast_enabled": authorised,
            "integrator_fee_usd_micros": 0,
            "fenorium_fee_usd_micros": 0,
        }

    def execution_route(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        """BUILD ROUTE: the route endpoint's answer, recorded with a route id when routes are kept."""
        response = self._execution_route(operation, check_gas=True,
                                         deadline_at=self.monotonic() + LENDING_RESPONSE_SECONDS)
        if self.routes is not None and response.get("instrument") is not None:
            response["route_id"] = self._record_route(operation, response)
        return response

    def sale_route(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        response = self._execution_route(operation, sale=True, check_gas=True,
                                         deadline_at=self.monotonic() + LENDING_RESPONSE_SECONDS)
        if self.routes is not None and response.get("instrument") is not None:
            response["route_id"] = self._record_route(operation, response)
        return response

    def _record_route(self, operation: Mapping[str, Any], response: Mapping[str, Any]) -> str:
        instrument = response["instrument"]
        pay_chain = response.get("pay_chain_id") or instrument["chain_id"]
        fee_rows = [row for row in response.get("fees") or [] if int(row.get("integrator_fee") or 0) > 0]
        fee = None
        if fee_rows and len({(row["token"].lower(), row["token_decimals"]) for row in fee_rows}) == 1:
            fee = {"token": fee_rows[0]["token"], "token_decimals": fee_rows[0]["token_decimals"],
                   "amount_raw": sum(int(row["integrator_fee"]) for row in fee_rows),
                   "usd_micros": response.get("fenorium_fee_usd_micros")}
        chain = registry().get(pay_chain)
        unsigned = response.get("unsigned_transaction_request") or {}
        return self.routes.record_build(
            wallet=str(response["wallet"]), response=response, chain_id=instrument["chain_id"],
            direction=response.get("direction", "BUY"),
            instrument_address=instrument.get("address"),
            instrument_id=asset_id(instrument["chain_id"], instrument["address"]) if instrument.get("address") else None,
            venue=instrument.get("venue"), pay_chain_id=pay_chain, pay_token=response.get("pay_address"),
            amount_raw=int(response["amount_raw"]), tx_target=unsigned.get("to"), fee=fee,
            tx_data=unsigned.get("data"), tx_value=unsigned.get("value"),
            fee_recipient=registry().fee_recipient,
            recipient_deployed=chain is not None and chain.fee_safe == "deployed",
        )

    def route_submitted(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        """SUBMITTED: the buyer broadcast a route's transaction; from here the server follows it."""
        self._operation(operation, SUBMISSION_OPERATION_SCHEMA, {"route_id", "wallet", "tx_hash", "approval_tx_hash"})
        if self.routes is None:
            raise APIError("ROUTES_UNAVAILABLE", "this deployment does not keep route records", status=503)
        route_id = _text(_required(operation, "route_id", "operation"), "operation.route_id", maximum=32)
        wallet = _evm_address(_required(operation, "wallet", "operation"), "operation.wallet")
        tx_hash = _evm_hash(_required(operation, "tx_hash", "operation"), "operation.tx_hash")
        approval = operation.get("approval_tx_hash")
        approval = None if approval is None else _evm_hash(approval, "operation.approval_tx_hash")
        try:
            return self.routes.submit(route_id, wallet=wallet, tx_hash=tx_hash, approval_tx_hash=approval)
        except RouteStateError as exc:
            raise APIError(exc.code, str(exc), status=404 if exc.code == "ROUTE_NOT_FOUND" else 409) from exc

    def _resolve_route_instrument(self, chain_id: int, address: str) -> tuple[Any, str | None, list[str]]:
        if self.instruments is None:
            return None, "ROUTE_UNAVAILABLE", ["UNIVERSE_UNAVAILABLE"]
        now = (self.clock or (lambda: datetime.now(UTC)))()
        try:
            instrument = self.instruments.resolve(
                chain_id, address, now=now,
            )
        except InstrumentError as exc:
            # A refusal of the instrument and a failure of our own evidence are different
            # answers. An unreadable or expired universe says nothing about the instrument --
            # it says this deployment cannot currently say -- and reporting that as REJECTED
            # publishes our staleness as a property of what the buyer asked for.
            status = "ROUTE_UNAVAILABLE" if exc.code in _UNIVERSE_FAILURES else "REJECTED"
            return None, status, [exc.code]
        refusals = _instrument_refusals(instrument, now=now)
        return instrument, "REJECTED" if refusals else None, refusals

    def _resolve_sale_instrument(self, chain_id: int, address: str | None) -> tuple[Any, str | None, list[str]]:
        if address is None:
            return None, "REJECTED", ["SALE_NOT_A_TOKEN"]
        instrument, status, reasons = self._resolve_route_instrument(chain_id, address)
        if instrument is None:
            return instrument, status, reasons
        reasons = []
        if not instrument.address:
            reasons.append("SALE_NOT_A_TOKEN")
        if instrument.transfer_gate == "GATED":
            reasons.append("TRANSFER_GATE_RESTRICTED")
        raw = instrument.raw or {}
        terms = raw.get("fixed_income")
        terms = terms if isinstance(terms, Mapping) else {}
        principal = (terms.get("instrument_type") == "ZERO_COUPON"
                     or str(instrument.venue).lower() in {"pendle", "spectra"})
        maturity = raw.get("maturity_timestamp")
        if maturity is None:
            maturity = terms.get("maturity_timestamp")
        if maturity is None and raw.get("maturity"):
            try:
                maturity = int(datetime.fromisoformat(str(raw["maturity"]) + "T00:00:00+00:00").timestamp())
            except ValueError:
                pass
        now = (self.clock or (lambda: datetime.now(UTC)))()
        if principal and type(maturity) is int and maturity <= now.timestamp():
            reasons.append("SALE_MATURED_REDEEM")
        return instrument, "REJECTED" if reasons else None, reasons

    def _execution_route(self, operation: Mapping[str, Any], *, sale: bool = False, check_gas: bool = False,
                         instrument_check: tuple[Any, str | None, list[str]] | None = None,
                         deadline_at: float | None = None) -> dict[str, Any]:
        """Fetch one route for what the wallet holds, and say whether this server would act on it.

        The difference from `/v1/execution-quotes` is who asked. Here the instrument is resolved
        against this deployment's own universe and the route is fetched by this process, so the
        payload underneath the answer was never in the caller's hands.
        """
        v2 = operation.get("schema_version") == ROUTE_OPERATION_SCHEMA_V2
        asset_field = "receive_with" if sale else "pay_with"
        schema = SALE_OPERATION_SCHEMA if sale else ROUTE_OPERATION_SCHEMA_V2 if v2 else ROUTE_OPERATION_SCHEMA
        method, as_of = self._operation(operation, schema, {
            "wallet", "chain_id", "instrument_address", asset_field, "amount_raw",
        } | {"accept_cost_bps"})
        accept_cost_bps = (_integer(operation["accept_cost_bps"], "operation.accept_cost_bps",
                                    minimum=0, maximum=MAX_ACCEPTED_SALE_COST_BPS if sale else 10_000)
                           if "accept_cost_bps" in operation else None)
        cost_acceptance = {"cost_acceptance": None}
        wallet = _evm_address(_required(operation, "wallet", "operation"), "operation.wallet")
        chain_id = _integer(
            _required(operation, "chain_id", "operation"), "operation.chain_id", minimum=1)
        requested_address = _required(operation, "instrument_address", "operation")
        instrument_address = (None if sale and requested_address is None else
                              _evm_address(requested_address, "operation.instrument_address"))
        pay_address, pay_decimals, pay_symbol, pay_chain = _pay_asset(
            _required(operation, asset_field, "operation"), with_chain=v2 or sale,
            optional_chain=sale, name=f"operation.{asset_field}")
        pay_chain = chain_id if pay_chain is None else pay_chain
        amount_raw = _raw_amount(
            _required(operation, "amount_raw", "operation"), "operation.amount_raw")

        receive_address, receive_chain = pay_address, pay_chain
        receive_metadata = {"address": receive_address, "chain_id": receive_chain,
                            "decimals": pay_decimals, "symbol": pay_symbol}
        recipient = None
        balance_block = None
        reader = block = None

        def respond(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = self._route_response(*args, pay_chain_id=chain_id if sale else pay_chain, **kwargs)
            if sale:
                response.update(direction="SELL", receive_address=receive_address,
                                receive_chain_id=receive_chain, pay_address=instrument_address,
                                receive_with=receive_metadata, recipient=recipient,
                                balance_block=balance_block)
            response.update(cost_acceptance)
            if (check_gas and response["status"] == "ROUTE_READY"
                    and response["unsigned_transaction_request"] is not None):
                try:
                    gas_reader = reader if reader is not None else self._order_reader(pay_chain, deadline_at)
                except (OnchainError, ValueError, OSError):
                    response["reason_codes"] = sorted(set(response["reason_codes"] + ["GAS_CHECK_UNAVAILABLE"]))
                else:
                    apply_gas_check(response, gas_reader, chain_id=chain_id if sale else pay_chain,
                                    wallet=wallet, block=block)
            return response

        resolver = self._resolve_sale_instrument if sale else self._resolve_route_instrument
        instrument, status, refusals = (resolver(chain_id, instrument_address)
                                        if instrument_check is None else instrument_check)
        refusals = list(refusals)
        if instrument is None:
            return respond(
                status, refusals, instrument=None,
                wallet=wallet, amount_raw=amount_raw, pay_address=pay_address,
            )

        refusals.extend(_route_pay_refusals(instrument, pay_address=pay_address, pay_chain=pay_chain))
        if sale:
            settlement = next((row for row in registry_assets(registry(), ())
                               if row["chain_id"] == receive_chain
                               and row["address"] == receive_address
                               and "settlement" in row["roles"]), None)
            if settlement is None:
                refusals.append("RECEIVE_ASSET_NOT_SETTLEMENT")
            elif settlement["decimals"] != pay_decimals:
                refusals.append("RECEIVE_DECIMALS_MISMATCH")
            else:
                receive_metadata["symbol"] = settlement["symbol"]
        if refusals:
            # No outbound request for a purchase this server has already refused. Spending a
            # rate-limited vendor call to be told again is the caller setting our budget.
            return respond(
                "REJECTED", refusals, instrument=instrument,
                wallet=wallet, amount_raw=amount_raw, pay_address=pay_address,
            )

        # A deployment without a router answers before any outbound call, balance reads included.
        if self.router is None:
            return respond(
                "ROUTE_UNAVAILABLE", ["ROUTER_UNAVAILABLE"], instrument=instrument,
                wallet=wallet, amount_raw=amount_raw, pay_address=pay_address,
            )

        if sale:
            try:
                reader = self._order_reader(chain_id, deadline_at)
                # The check only has to stop a sale that would revert. The sale executes against the chain
                # head, and a token bought minutes ago is not yet in a finalized block (on Arbitrum the
                # finalized block trails by many minutes), so the head is what the balance is read at.
                block = reader.head_block()
                balance = reader.balances(wallet, [instrument.address], block).get(instrument.address)
                if type(balance) is not int or balance < 0:
                    raise OnchainError("instrument balance unavailable")
                balance_block = {"chain_id": chain_id, "number": block.number, "hash": block.hash,
                                 "finality": getattr(block, "finality", None)}
            except (OnchainError, ValueError, OSError, StopIteration):
                return respond("ROUTE_UNAVAILABLE", ["BALANCE_UNAVAILABLE"], instrument=instrument,
                               wallet=wallet, amount_raw=amount_raw, pay_address=instrument.address)
            if balance < amount_raw:
                return respond("REJECTED", ["INSUFFICIENT_BALANCE"], instrument=instrument,
                               wallet=wallet, amount_raw=amount_raw, pay_address=instrument.address)

        try:
            request = RouteRequest(
                chain_id=chain_id,
                instrument_address=instrument.address,
                instrument_decimals=instrument.decimals,
                pay_address=pay_address,
                pay_decimals=pay_decimals,
                pay_symbol=pay_symbol,
                amount_raw=amount_raw,
                from_address=wallet,
                pay_chain_id=chain_id if sale else pay_chain,
                direction="sell" if sale else "buy",
                receive_chain_id=receive_chain if sale else None,
            )
            fetched = (self.router.fetch(request) if deadline_at is None else
                       self.router.fetch(request, deadline_at=deadline_at))
        except RouterError as exc:
            return respond(
                "ROUTE_UNAVAILABLE", [exc.code], instrument=instrument,
                wallet=wallet, amount_raw=amount_raw, pay_address=pay_address,
            )

        binding = fetched.binding
        if sale:
            if (binding.from_token.lower() != instrument.address.lower()
                    or binding.to_token.lower() != receive_address.lower()
                    or binding.from_chain != chain_id or binding.to_chain != receive_chain
                    or binding.from_amount != amount_raw
                    or binding.from_decimals != instrument.decimals
                    or binding.to_decimals != pay_decimals):
                return respond("ROUTE_UNAVAILABLE", ["ROUTER_UNAVAILABLE"], instrument=instrument,
                               wallet=wallet, amount_raw=amount_raw, pay_address=instrument.address)
            pay_address, pay_decimals, pay_chain = instrument.address, instrument.decimals, chain_id
        unsigned_request = None
        try:
            unsigned_request = parse_unsigned_transaction_request(
                fetched.payload.get("transactionRequest"), binding=binding, requested_from=wallet,
            ).to_json()
        except (RouteError, TypeError, ValueError):
            unsigned_request = None

        approval, approval_reason = _approval_for(
            binding, pay_address=pay_address, wallet=wallet, amount_raw=amount_raw,
            router_target=None if unsigned_request is None else unsigned_request["to"],
        )

        reasons: list[str] = []
        if approval_reason is not None:
            reasons.append(approval_reason)
        now = (self.clock or (lambda: datetime.now(UTC)))()
        if binding.expires_at <= int(now.timestamp()):
            # The server's clock, never the caller's `as_of`. A request supplies its own
            # timestamp so an analysis can be replayed as it stood; letting that value decide
            # whether a route is still live hands the clock to whoever is asking.
            reasons.append("QUOTE_EXPIRED")
        cross_chain = binding.from_chain != binding.to_chain
        action = fetched.payload.get("action") if isinstance(fetched.payload, Mapping) else None
        destination = action.get("toAddress") if isinstance(action, Mapping) else None
        recipient = destination
        if (destination is None and (cross_chain or sale)) or (
                destination is not None and str(destination).lower() != wallet.lower()):
            # What is bought must land in the wallet that pays. Across chains nothing else checks
            # it: the source transaction is signed on one chain and the delivery happens on another.
            reasons.append("DESTINATION_NOT_WALLET")
        # The bounded plain-decimal shape proves finite, non-negative monetary evidence
        # without expanding exponent notation or converting an unbounded numeral.
        fees, lifi_fee_micros, integrator_fee_micros = _route_fees(fetched.payload)
        if (not isinstance(binding.gas_cost_usd, str)
                or _PLAIN_DECIMAL.fullmatch(binding.gas_cost_usd) is None
                or fees is None):
            reasons.append("MISSING_COST")
        elif not _integrator_fee_is_as_configured(
            fees, configured=self._configured_fee(), pay_address=pay_address, amount_raw=amount_raw,
        ):
            reasons.append("FEE_MISMATCH")
        if sale and fees is not None and any(
                int(row["integrator_fee"]) > 0 and (
                    row["token_decimals"] != instrument.decimals
                    or row["token_chain_id"] not in (None, chain_id)) for row in fees):
            reasons.append("FEE_MISMATCH")
        if binding.to_amount_min <= 0:
            # A route that guarantees nothing has quoted a hope. Fenorium decides on the amount
            # a route commits to, which is exactly what toAmountMin is.
            reasons.append("UNGUARANTEED_OUTPUT")
        if unsigned_request is None:
            reasons.append("MISSING_EXECUTION_QUOTE")
        size_check = (self._sale_size_check(
            instrument, binding, receive_chain=receive_chain, receive_address=receive_address,
            receive_decimals=request.pay_decimals, amount_raw=amount_raw, fees=fees, deadline_at=deadline_at,
        ) if sale else self._size_check(
            instrument, binding, pay_chain=pay_chain, pay_address=pay_address,
            pay_decimals=pay_decimals, amount_raw=amount_raw, fees=fees, deadline_at=deadline_at,
        ))
        reasons.extend(size_check["reasons"])

        # Only a known, undisputed sale cost can be accepted; every other refusal stays hard.
        if (sale and set(reasons) == {"ROUTE_COST_TOO_HIGH"}
                and size_check["route_cost_bps"] <= MAX_ACCEPTED_SALE_COST_BPS):
            cost_bps = size_check["route_cost_bps"]
            if accept_cost_bps is not None and accept_cost_bps >= cost_bps:
                reasons.remove("ROUTE_COST_TOO_HIGH")
                size_check["reasons"] = []
                size_check["cost_accepted_bps"] = accept_cost_bps
                cost_acceptance["cost_accepted_bps"] = accept_cost_bps
            else:
                reasons = ["SALE_COST_NEEDS_ACCEPTANCE"]
                size_check["reasons"] = list(reasons)
                cost_acceptance["cost_acceptance"] = {
                    "route_cost_bps": cost_bps,
                    "max_acceptable_bps": MAX_ACCEPTED_SALE_COST_BPS,
                    "outlay_mark_usd_micros": size_check["outlay_mark_usd_micros"],
                    "proceeds_usd_micros": size_check["proceeds_usd_micros"],
                }

        if (not sale and set(reasons) == {"ROUTE_COST_TOO_HIGH"}
                and self._purchase_cost_acceptable(size_check)):
            cost_bps = size_check["route_cost_bps"]
            if accept_cost_bps is not None and accept_cost_bps >= cost_bps:
                reasons = []
                size_check["reasons"] = []
                size_check["cost_accepted_bps"] = accept_cost_bps
                cost_acceptance["cost_accepted_bps"] = accept_cost_bps
            else:
                reasons = ["PURCHASE_COST_NEEDS_ACCEPTANCE"]
                size_check["reasons"] = list(reasons)
                cost_acceptance["cost_acceptance"] = {
                    key: size_check[key] for key in (
                        "route_cost_bps", "yield_to_maturity_bps", "net_yield_to_maturity_bps",
                        "outlay_mark_usd_micros", "guaranteed_output_usd_micros")}
                cost_acceptance["cost_acceptance"]["max_acceptable_bps"] = MAX_ACCEPTED_PURCHASE_COST_BPS

        status = "ROUTE_READY" if not reasons else "ANALYTICAL_ONLY"
        # `cross_chain` is judged from the route LI.FI returned rather than from what was asked
        # for: a route that crosses chains needs the clearances a crossing needs.
        if status == "ROUTE_READY":
            # What this deployment still lacks, from the gate rather than from a constant.
            reasons.extend(self.gate.reason_codes_for(cross_chain=cross_chain))
        # The route was fetched here, so unlike `/v1/execution-quotes` there is no provenance
        # objection left and the gate is the only thing deciding. It is closed until every
        # clearance this kind of route needs is in, which is the point: the decision moved to the
        # gate instead of being structurally impossible.
        authorised = status == "ROUTE_READY" and self.gate.is_open_for(cross_chain=cross_chain)
        response = respond(
            status, reasons, instrument=instrument, wallet=wallet, amount_raw=amount_raw,
            pay_address=pay_address, route=binding.to_json(),
            unsigned_request=unsigned_request, authorised=authorised, approval=approval,
            fees=fees, lifi_fee_micros=lifi_fee_micros, integrator_fee_micros=integrator_fee_micros,
            size_check=size_check,
        )
        self._persist_observation(
            {
                "schema_version": schema,
                "direction": "SELL" if sale else "BUY",
                "balance_block": balance_block,
                "method_version": method,
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "wallet": wallet,
                "amount_raw": str(amount_raw),
                "pay_address": pay_address,
                "pay_chain_id": pay_chain,
                "instrument": instrument.to_dict(),
                "route": binding.to_json(),
                "unsigned_transaction_request": unsigned_request,
                "size_check": size_check,
                "gas_check": response["gas_check"],
                "response": response,
                "accept_cost_bps": accept_cost_bps,
                **cost_acceptance,
            },
            suffix="route",
        )
        return response

    def _instrument_mark(self, instrument: Any, now: datetime,
                         deadline_at: float | None = None) -> tuple[int, str] | None:
        """Value the resolved token independently of the router's output-price claim."""
        raw = getattr(instrument, "raw", None) or {}
        terms = raw.get("fixed_income")
        terms = terms if isinstance(terms, Mapping) else {}
        principal = (terms.get("instrument_type") == "ZERO_COUPON"
                     or (str(instrument.venue).lower() in {"pendle", "spectra"}
                         and bool(instrument.address)))
        token = raw.get("accounting_asset_address") if principal else instrument.address
        if not isinstance(token, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", token) is None:
            return None
        if principal:
            rate = raw.get("rate_bps")
            maturity = raw.get("maturity_timestamp")
            if maturity is None:
                maturity = terms.get("maturity_timestamp")
            if (raw.get("rate_kind") != "executable" or type(rate) is not int
                    or rate <= -10_000 or type(maturity) is not int or maturity < 0):
                return None
            # Preserve subsecond time without a binary floating-point timestamp.
            elapsed = now.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
            now_seconds = Fraction((elapsed.days * 86400 + elapsed.seconds) * 1_000_000
                                   + elapsed.microseconds, 1_000_000)
            remaining = max(Fraction(0), Fraction(maturity) - now_seconds)
        price = (self._fresh_native_price(instrument.chain_id, deadline_at=deadline_at)
                 if token.lower() in NATIVE_TOKEN_SENTINELS else
                 self._fresh_price(instrument.chain_id, token, deadline_at=deadline_at))
        if type(price) is not int or price <= 0:
            return None
        if not principal:
            return price, "Fenorium mark: current price"
        try:
            with localcontext() as context:
                context.prec = 80
                days = Decimal(remaining.numerator) / Decimal(remaining.denominator) / 86400
                discount = (Decimal(1) + Decimal(rate) / 10_000) ** (days / 365)
                # price is already in 8-decimal fixed point; truncate value received.
                mark = int(Decimal(price) / discount)
                source = ("Fenorium mark: accounting asset price × implied discount at "
                          f"{Decimal(rate) / 100:.2f}% for {days:.2f} days")
        except (DecimalException, ValueError, OverflowError):
            return None
        return (mark, source) if mark > 0 else None

    def _depth_check(self, instrument: Any, notional: int | None) -> tuple[list[str], Any]:
        reasons: list[str] = []
        rated = None
        if notional is None or notional <= 0:
            reasons.append("NOTIONAL_UNKNOWN")
        elif getattr(instrument, "raw", None) is not None:
            size_usd = max(1, -(-notional // 1_000_000))
            point = _rating_point(dict(instrument.raw))
            try:
                depth = Fraction(str(point.get("liquidity_usd")))
            except (ValueError, ZeroDivisionError):
                depth = None
            if depth is not None and depth < size_usd:
                reasons.append("SIZE_EXCEEDS_DEPTH")
            rating = rate_instrument(point, _rating_assumptions(size_usd, _routing_fee_bps(self._configured_fee())))
            rated = {"size_usd": size_usd, "computable": bool(rating.computable), "refusal": rating.refusal,
                     "execution_bps": int(rating.execution_bps), "net_bps": int(rating.net_bps)}
            if (not rating.computable and _is_size_depth_refusal(rating.refusal)
                    and "SIZE_EXCEEDS_DEPTH" not in reasons):
                reasons.append("SIZE_EXCEEDS_DEPTH")
        return reasons, rated

    @staticmethod
    def _route_cost(paid: int | None, guaranteed: int | None, binding: Any,
                    fees: list[dict[str, Any]] | None) -> tuple[list[str], int | None, int | None, int | None]:
        reasons: list[str] = []
        on_top = None if fees is None else _ceil_usd_micros(
            sum((Fraction(row["amount_usd"]) for row in fees if not row["included"]), Fraction(0)))
        gas = (_ceil_usd_micros(Fraction(binding.gas_cost_usd))
               if _usd_micros(binding.gas_cost_usd) is not None else None)
        cost_bps = None
        if paid and guaranteed is not None and binding.to_amount > 0 and on_top is not None and gas is not None:
            outlay = paid + on_top + gas
            cost_bps = -(-(outlay - guaranteed) * 10_000 // outlay)
            if cost_bps > MAX_ROUTE_COST_BPS:
                reasons.append("ROUTE_COST_TOO_HIGH")
            if (guaranteed - outlay) * 10_000 > 50 * outlay:
                reasons.append("VALUATION_CONFLICT")
        else:
            reasons.append("ROUTE_COST_UNKNOWN")
        return reasons, cost_bps, on_top, gas

    def _sale_size_check(self, instrument: Any, binding: Any, *, receive_chain: int,
                         receive_address: str, receive_decimals: int, amount_raw: int,
                         fees: list[dict[str, Any]] | None, deadline_at: float | None = None) -> dict[str, Any]:
        now = (self.clock or (lambda: datetime.now(UTC)))()
        mark = self._instrument_mark(instrument, now, deadline_at)
        paid = source = gap_bps = None
        if mark is not None:
            price, source = mark
            # Value relinquished rounds up; guaranteed settlement proceeds round down.
            paid = -(-(amount_raw * price * USD_MICROS) // (10 ** instrument.decimals * PRICE_SCALE))
        receive_price = self._fresh_price(receive_chain, receive_address, deadline_at=deadline_at)
        proceeds = (raw_to_usd_micros(binding.to_amount_min, decimals=receive_decimals,
                                     price_usd_fixed=receive_price)
                    if type(receive_price) is int and receive_price > 0 else None)
        reasons, rated = self._depth_check(instrument, paid)
        router_value = _usd_micros(binding.from_amount_usd)
        if paid and router_value is not None:
            numerator = abs(router_value - paid) * 10_000
            gap_bps = -(-numerator // paid)
            if numerator > 500 * paid:
                reasons.append("VALUATION_CONFLICT")
        cost_reasons, cost_bps, on_top, gas = self._route_cost(paid, proceeds, binding, fees)
        reasons.extend(cost_reasons)
        return {
            "reasons": sorted(set(reasons)), "notional_usd_micros": paid,
            "notional_source": source, "rated_at_size": rated,
            "outlay_mark_usd_micros": paid, "outlay_mark_source": source,
            "proceeds_usd_micros": proceeds, "guaranteed_output_usd_micros": proceeds,
            "router_input_usd_micros": router_value, "valuation_gap_bps": gap_bps,
            "fees_on_top_usd_micros": on_top, "gas_usd_micros": gas,
            "route_cost_bps": cost_bps, "max_route_cost_bps": MAX_ROUTE_COST_BPS,
        }

    @staticmethod
    def _purchase_cost_acceptable(size: Mapping[str, Any]) -> bool:
        cost, net = size.get("route_cost_bps"), size.get("net_yield_to_maturity_bps")
        return (type(cost) is int and MAX_ROUTE_COST_BPS < cost <= MAX_ACCEPTED_PURCHASE_COST_BPS
                and type(net) is int and net > 0)

    @staticmethod
    def _purchase_yield(instrument: Any, now: datetime) -> int | None:
        raw = getattr(instrument, "raw", None) or {}
        maturity, rate = raw.get("maturity_timestamp"), raw.get("rate_bps")
        elapsed = now.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
        seconds = Fraction((elapsed.days * 86400 + elapsed.seconds) * 1_000_000
                           + elapsed.microseconds, 1_000_000)
        if (type(maturity) is not int or maturity <= seconds
                or raw.get("rate_kind") != "executable" or type(rate) is not int):
            return None
        if rate <= -10_000:
            return -10_000
        apy, term = Fraction(rate, 10_000), (maturity - seconds) / _YEAR_SECONDS
        # Same conservative APY bound as position_deposit for nonnegative rates.
        # For negative rates ln(1+a) >= a/(1+a), while e^x-1 >= x still holds.
        bound = term * (2 * apy / (2 + apy) if rate >= 0 else apy / (1 + apy))
        basis_points = bound * 10_000
        return max(-10_000, basis_points.numerator // basis_points.denominator)

    def _size_check(self, instrument: Any, binding: Any, *, pay_chain: int, pay_address: str,
                    pay_decimals: int, amount_raw: int, fees: list[dict[str, Any]] | None,
                    deadline_at: float | None = None) -> dict[str, Any]:
        """VALIDATE SIZE: is this purchase, at its own size, still what the verdict was about?

        Two questions, answered immediately before a route is offered. Re-rated at this exact
        notional, does the instrument's measured depth still cover it? And what does the route
        guarantee: its committed output, valued at Fenorium's own mark, against what is
        paid. Costs above MAX_ROUTE_COST_BPS need explicit acceptance and positive remaining
        yield, within the purchase cap. A notional or cost nobody can state is a reason,
        never a pass.
        """
        reasons: list[str] = []
        notional, source = _usd_micros(binding.from_amount_usd), "router"
        if notional is None:
            asset = None if pay_address.lower() in NATIVE_TOKEN_SENTINELS else pay_address
            price = (self._fresh_price(pay_chain, asset, deadline_at=deadline_at) if asset else
                     self._fresh_native_price(pay_chain, deadline_at=deadline_at))
            if price is not None:
                notional, source = raw_to_usd_micros(amount_raw, decimals=pay_decimals, price_usd_fixed=price), "feed"
        depth_reasons, rated = self._depth_check(instrument, notional)
        reasons.extend(depth_reasons)
        cost_bps = guaranteed = None
        paid, received = notional, _usd_micros(binding.to_amount_usd)
        now = (self.clock or (lambda: datetime.now(UTC)))()
        mark = self._instrument_mark(instrument, now, deadline_at)
        output_mark = mark_source = gap_bps = None
        if mark is not None:
            price, mark_source = mark
            output_mark = price * USD_MICROS // PRICE_SCALE
            guaranteed = raw_to_usd_micros(binding.to_amount_min, decimals=instrument.decimals,
                                           price_usd_fixed=price)
            mark_value = raw_to_usd_micros(binding.to_amount, decimals=instrument.decimals,
                                          price_usd_fixed=price)
            if received is not None and mark_value > 0:
                gap_numerator = abs(received - mark_value) * 10_000
                gap_bps = -(-gap_numerator // mark_value)
                if gap_numerator > 500 * mark_value:
                    reasons.append("VALUATION_CONFLICT")
        # The outlay is what the buyer gives up: the amount the quote spends, the source-chain gas paid
        # from its native balance, and fees LI.FI marks `included: false` (charged on top; an included
        # one already shows in the output). Unstated fees or gas leave the cost unknown.
        cost_reasons, cost_bps, on_top, gas = self._route_cost(paid, guaranteed, binding, fees)
        reasons.extend(reason for reason in cost_reasons if reason not in reasons)
        ytm = self._purchase_yield(instrument, now)
        net = None if ytm is None or cost_bps is None else _net_yield_bps(ytm, cost_bps)
        yields = {} if ytm is None else {"yield_to_maturity_bps": ytm, "net_yield_to_maturity_bps": net}
        if net is not None and cost_bps > MAX_ROUTE_COST_BPS and net <= 0:
            reasons.append("NET_YIELD_NOT_POSITIVE")
        return {
            **yields,
            "outlay_mark_usd_micros": (paid + on_top + gas
                if paid is not None and on_top is not None and gas is not None else None),
            "reasons": reasons,
            "notional_usd_micros": notional,
            "notional_source": None if notional is None else source,
            "rated_at_size": rated,
            "output_mark_usd_micros": output_mark,
            "output_mark_source": mark_source,
            "router_output_usd_micros": received,
            "valuation_gap_bps": gap_bps,
            "guaranteed_output_usd_micros": guaranteed,
            "fees_on_top_usd_micros": on_top,
            "gas_usd_micros": gas,
            "route_cost_bps": cost_bps,
            "max_route_cost_bps": MAX_ROUTE_COST_BPS,
        }

    def _fresh_native_price(self, chain_id: int, *, deadline_at: float | None = None) -> int | None:
        now = (self.clock or (lambda: datetime.now(UTC)))()
        if self.current_prices is not None:
            observation = self.current_prices.observation(chain_id, None, identity="native")
            if observation is not None and 0 <= (now - observation.observed_at).total_seconds() <= CURRENT_PRICE_MAX_AGE_SECONDS:
                return observation.price_usd_fixed
        return self._on_demand_price(chain_id, None, now, deadline_at=deadline_at)

    def route_previews(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        """Quote one purchase, at one dollar size, from each of a few things the wallet could pay with.

        Each option is quoted exactly as `/v1/execution-routes` would build it, by this server, so a
        preview and the route built from it are the same fetch (reused while it lives) and the
        browser never asks LI.FI itself. An option this server cannot put a dollar price on is
        named, not guessed into an amount.
        """
        deadline_at = self.monotonic() + PREVIEW_RESPONSE_SECONDS
        method, as_of = self._operation(operation, PREVIEW_OPERATION_SCHEMA, {
            "wallet", "chain_id", "instrument_address", "notional_usd_micros", "pay_with",
        })
        wallet = _evm_address(_required(operation, "wallet", "operation"), "operation.wallet")
        chain_id = _integer(_required(operation, "chain_id", "operation"), "operation.chain_id", minimum=1)
        instrument = _evm_address(_required(operation, "instrument_address", "operation"), "operation.instrument_address")
        notional = _integer(_required(operation, "notional_usd_micros", "operation"),
                            "operation.notional_usd_micros", minimum=1)
        options = _required(operation, "pay_with", "operation")
        if not isinstance(options, list) or not 1 <= len(options) <= MAX_PREVIEW_OPTIONS:
            raise APIError("INVALID_FIELD", f"operation.pay_with must list between one and {MAX_PREVIEW_OPTIONS} options")
        resolved = None
        if self.router is None:
            status, refusals = "ROUTE_UNAVAILABLE", ["ROUTER_UNAVAILABLE"]
        else:
            resolved, status, refusals = self._resolve_route_instrument(chain_id, instrument)
        previews = []
        for index, option in enumerate(options):
            address, decimals, symbol, pay_chain = _pay_asset(option, with_chain=True)
            pay_chain = chain_id if pay_chain is None else pay_chain
            preview: dict[str, Any] = {"pay_with": {"chain_id": pay_chain, "address": address, "decimals": decimals,
                                                    "symbol": symbol},
                                       "yield_to_maturity_bps": None, "net_yield_to_maturity_bps": None}
            reasons = list(refusals)
            if resolved is not None and not reasons:
                reasons.extend(_route_pay_refusals(resolved, pay_address=address, pay_chain=pay_chain))
            if reasons:
                preview.update(status=status or "REJECTED", reason_codes=reasons, amount_raw=None)
                previews.append(preview)
                continue
            if deadline_at - self.monotonic() < PREVIEW_MIN_ROUTE_SECONDS:
                preview.update(status="REJECTED", reason_codes=["PREVIEW_DEADLINE"], amount_raw=None)
                previews.append(preview)
                continue
            native = address.lower() in NATIVE_TOKEN_SENTINELS
            price = (self._fresh_native_price(pay_chain, deadline_at=deadline_at) if native else
                     self._fresh_price(pay_chain, address, deadline_at=deadline_at))
            if price is None:
                preview.update(status="REJECTED", reason_codes=["PAY_PRICE_UNKNOWN"], amount_raw=None)
                previews.append(preview)
                continue
            if deadline_at - self.monotonic() < PREVIEW_MIN_ROUTE_SECONDS:
                preview.update(status="REJECTED", reason_codes=["PREVIEW_DEADLINE"], amount_raw=None)
                previews.append(preview)
                continue
            # The amount of this token worth the notional, rounded up so the purchase is not short.
            amount_raw = -(-notional * 10**decimals * PRICE_SCALE // (price * USD_MICROS))
            route = self._execution_route({
                "schema_version": ROUTE_OPERATION_SCHEMA_V2, "method_version": method,
                "as_of": as_of.isoformat().replace("+00:00", "Z"), "wallet": wallet, "chain_id": chain_id,
                "instrument_address": instrument,
                "pay_with": {"address": address, "decimals": decimals, "symbol": symbol, "chain_id": pay_chain},
                "amount_raw": str(amount_raw),
            }, instrument_check=(resolved, status, refusals), deadline_at=deadline_at)
            binding = route.get("route") or {}
            quoted_usd = binding.get("from_amount_usd")
            preview.update(
                status=route["status"], reason_codes=route["reason_codes"], amount_raw=str(amount_raw),
                quoted_usd_micros=_usd_micros(quoted_usd),
                to_amount_min=binding.get("to_amount_min"), tool=binding.get("tool"),
                expires_at=binding.get("expires_at"), size_check=route.get("size_check"),
                fees=route.get("fees"), fenorium_fee_usd_micros=route.get("fenorium_fee_usd_micros"),
                gas_cost_usd=binding.get("gas_cost_usd"),
                yield_to_maturity_bps=(route.get("size_check") or {}).get("yield_to_maturity_bps"),
                net_yield_to_maturity_bps=(route.get("size_check") or {}).get("net_yield_to_maturity_bps"),
                cost_acceptance=route.get("cost_acceptance"),
            )
            if binding and not _usd_within_the_requested_size(quoted_usd, notional):
                preview.update(status="REJECTED", reason_codes=["PREVIEW_SIZE_MISMATCH"])
            previews.append(preview)

        def offered(preview: Mapping[str, Any]) -> bool:
            return (preview["status"] == "ROUTE_READY" or
                    preview.get("reason_codes") == ["PURCHASE_COST_NEEDS_ACCEPTANCE"])

        def rank(preview: Mapping[str, Any]) -> tuple[int, int, int]:
            ready = offered(preview)
            cost = (preview.get("size_check") or {}).get("route_cost_bps")
            return (0 if ready else 1, cost if isinstance(cost, int) else 10**9, int(preview.get("to_amount_min") or 0) * -1)

        ranked = sorted(previews, key=rank)
        return {"schema_version": "fenorium/route-previews/1", "wallet": wallet, "chain_id": chain_id,
                "instrument_address": instrument, "notional_usd_micros": notional, "previews": ranked,
                "best": ranked[0] if ranked and offered(ranked[0]) else None}

    def execution_status(self, tx_hash: Any, *, from_chain: Any, to_chain: Any) -> dict[str, Any]:
        """Where a purchase that crossed chains stands, as LI.FI reports it.

        The source receipt only proves the first leg. Until LI.FI reports a terminal state the
        purchase is not done, and when it is done it may have delivered something other than what
        was bought -- which is what `received` says, in that token's own units.
        """
        if not isinstance(tx_hash, str) or _TX_HASH.fullmatch(tx_hash) is None:
            raise APIError("INVALID_FIELD", "the transaction hash must be 0x followed by 64 hex digits")
        source = _route_chain(from_chain, "from_chain")
        destination = _route_chain(to_chain, "to_chain")
        if self.router is None or not callable(getattr(self.router, "status", None)):
            raise APIError("ROUTER_UNAVAILABLE", "this deployment does not ask a router", status=503)
        tx_hash = tx_hash.lower()
        try:
            payload = self.router.status(tx_hash, from_chain=source, to_chain=destination)
        except RouterError as exc:
            raise APIError(exc.code, "the transfer status is unavailable right now; retry shortly", status=503) from exc
        now = (self.clock or (lambda: datetime.now(UTC)))()
        try:
            raw_substatus = payload.get("substatus")
            documented_failure = payload.get("status") == "FAILED" and raw_substatus in _LIFI_FAILURES
            observation = parse_lifi_status(
                dict(payload, substatus=None) if documented_failure else payload, observed_at=now)
        except PortfolioContractError as exc:
            raise APIError("ROUTER_UNAVAILABLE", "LI.FI answered outside its documented vocabulary", status=502) from exc
        if observation.source_tx is not None and observation.source_tx != tx_hash:
            raise APIError("ROUTER_UNAVAILABLE", "LI.FI described a different transfer", status=502)
        # The hash alone does not make it this transfer: the chains must be the ones asked about. A
        # refund legitimately lands back on the source chain; nothing else may land there.
        refund = observation.substatus is not None and observation.substatus.value in {"REFUNDED", "REFUND_IN_PROGRESS"}
        landing = {destination, source} if refund else {destination}
        sending, receiving = payload.get("sending"), payload.get("receiving")
        if isinstance(sending, Mapping) and sending.get("chainId") is not None and sending.get("chainId") != source:
            raise APIError("ROUTER_UNAVAILABLE", "LI.FI described a transfer from another chain", status=502)
        # A final answer must show it is about this transfer: its source hash and chain, stated.
        # Without them it settles nothing, and the purchase stays unresolved.
        identified = (observation.source_tx == tx_hash and isinstance(sending, Mapping)
                      and sending.get("chainId") == source)
        if isinstance(receiving, Mapping) and receiving.get("chainId") is not None and receiving.get("chainId") not in landing:
            raise APIError("ROUTER_UNAVAILABLE", "LI.FI described a delivery on another chain", status=502)
        arrived = _received(receiving)
        if arrived is not None and arrived["chain_id"] not in landing:
            raise APIError("ROUTER_UNAVAILABLE", "LI.FI described a delivery on another chain", status=502)
        data = {
            "schema_version": EXECUTION_STATUS_SCHEMA,
            "source_tx": tx_hash,
            "from_chain": source,
            "to_chain": destination,
            "status": observation.status.value,
            "substatus": raw_substatus if documented_failure else (
                None if observation.substatus is None else observation.substatus.value),
            "state": None if observation.suggested_state is None or (
                observation.status.value in _LIFI_TERMINAL and not identified) else (
                "REFUNDED" if observation.status.value == "FAILED" and raw_substatus == "REFUNDED"
                else observation.suggested_state.value),
            "terminal": observation.status.value in _LIFI_TERMINAL and identified,
            # A failure that has not been refunded leaves something for the buyer to recover.
            "recovery_required": observation.status.value == "FAILED" and identified and raw_substatus != "REFUNDED",
            "transaction_id": observation.transaction_id,
            "destination_tx": observation.destination_tx,
            "received": arrived,
            "observed_at": now.isoformat().replace("+00:00", "Z"),
        }
        if data["terminal"]:
            self._persist_observation(data, suffix="execution-status")
        return data

    def _lending_context(self, operation: Mapping[str, Any], extra: set[str]) -> tuple:
        method, as_of = self._operation(operation, LENDING_ORDER_OPERATION_SCHEMA,
                                        {"wallet", "chain_id"} | extra)
        wallet = _evm_address(_required(operation, "wallet", "operation"), "operation.wallet")
        chain_id = _integer(_required(operation, "chain_id", "operation"), "operation.chain_id", minimum=1)
        now = (self.clock or (lambda: datetime.now(UTC)))()
        return method, as_of, wallet, chain_id, now

    def _lending_response(self, status: str, reasons: list[str], *, wallet: str,
                          chain_id: int, record: Mapping[str, Any] | None = None,
                          unsigned_request: dict | None = None, approval: dict | None = None,
                          ready: bool = False, **fields: Any) -> dict[str, Any]:
        authorised = ready and self.gate.is_open_for(cross_chain=False)
        if ready:
            reasons = [*reasons, *self.gate.reason_codes_for(cross_chain=False)]
        response = self._route_response(
            status, reasons, instrument=None, wallet=wallet, pay_chain_id=chain_id,
            amount_raw=int((record or {}).get("amount_raw", 0)),
            pay_address=(record or {}).get("loan_token"), unsigned_request=unsigned_request,
            approval=approval, authorised=authorised, provenance="SERVER_BUILT",
            fees=[], lifi_fee_micros=0, integrator_fee_micros=0,
        )
        response.update(fields)
        response.setdefault("authorization_request", None)
        response.setdefault("unsigned_authorization_request", None)
        response["authorised"] = authorised
        response["instrument"] = (record or {}).get("instrument")
        return response

    @contextmanager
    def _lending_request_lock(self, request_key: str):
        store_key = (str(Path(self.orders.path).resolve()) if self.orders.path != ":memory:"
                     else f"memory:{id(self.orders)}")
        key = (store_key, request_key)
        with self._lending_request_guard:
            # Count waiters too, so releasing an owner cannot replace their lock.
            lock, users = self._lending_request_locks.get(key, (threading.Lock(), 0))
            self._lending_request_locks[key] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._lending_request_guard:
                _, users = self._lending_request_locks[key]
                if users == 1:
                    del self._lending_request_locks[key]
                else:
                    self._lending_request_locks[key] = (lock, users - 1)

    def lending_order(self, operation: Mapping[str, Any], *, request_key: str | None = None,
                      request_digest: str | None = None) -> dict[str, Any]:
        operation = _object(operation, "request body")
        canonical_data(operation)
        if request_key is None or self.orders is None:
            return self._build_lending_order(operation)
        _, _, wallet, chain_id, now = self._lending_context(
            operation, {"venue", "instrument", "amount_raw", "apr_bps", "expiry_seconds"})
        digest = request_digest or hashlib.sha256(
            json.dumps(dict(operation), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            with self._lending_request_lock(request_key):
                self.orders.prune_drafts(int(now.timestamp()))
                root = self.orders.reserve_request(request_key, wallet, chain_id, digest)
                if root is not None:
                    return self.lending_order_replay(operation, root)
                try:
                    build_facts = {}
                    record = self._build_lending_order(operation, persist=False, build_facts=build_facts)
                    if not record.get("root"):
                        self.orders.release_request(request_key)
                        return record
                    with self.orders.request_transaction():
                        saved = self.orders.create(record)
                        self.orders.bind_request(request_key, saved["root"])
                    return self._lending_ready(saved, authorized=build_facts["ratifier_authorized"])
                except BaseException:
                    self.orders.release_request(request_key)
                    raise
        except OrderStoreError as exc:
            raise APIError(exc.code, str(exc), status=409 if exc.code == "IDEMPOTENCY_CONFLICT" else 400) from exc

    def _build_lending_order(self, operation: Mapping[str, Any], *, persist: bool = True,
                             build_facts: dict[str, Any] | None = None) -> dict[str, Any]:
        method, as_of, wallet, chain_id, now = self._lending_context(
            operation, {"venue", "instrument", "amount_raw", "apr_bps", "expiry_seconds"})
        venue = _text(_required(operation, "venue", "operation"), "operation.venue", maximum=64)
        name = _text(_required(operation, "instrument", "operation"), "operation.instrument", maximum=128)
        amount = _raw_amount(_required(operation, "amount_raw", "operation"), "operation.amount_raw")
        if amount >= 2 ** 128:
            raise APIError("INVALID_AMOUNT", "amount_raw must fit Midnight's uint128 asset cap")
        expiry = _integer(operation.get("expiry_seconds", 86_400), "operation.expiry_seconds",
                          minimum=1, maximum=7 * 86_400)

        def refuse(status: str, code: str) -> dict[str, Any]:
            return self._lending_response(status, [code], wallet=wallet, chain_id=chain_id)

        if venue not in MIDNIGHT_VENUES:
            return refuse("REJECTED", "UNSUPPORTED_VENUE")
        if not self.midnight_orders_enabled or self.midnight_policy is None or self.orders is None:
            return refuse("ROUTE_UNAVAILABLE", "ORDERS_UNAVAILABLE")
        if self.instruments is None:
            return refuse("ROUTE_UNAVAILABLE", "UNIVERSE_UNAVAILABLE")
        try:
            row = self.instruments.resolve_position_row(chain_id, venue, name, now=now)
        except InstrumentError as exc:
            return refuse("ROUTE_UNAVAILABLE" if exc.code in _UNIVERSE_FAILURES else "REJECTED", exc.code)
        if row.get("verdict") == "NO BID":
            return refuse("REJECTED", "NO_BID")
        deadline_at = self.monotonic() + LENDING_RESPONSE_SECONDS
        try:
            public = self.morpho_public_book.is_public(row.get("market_id"), deadline_at=deadline_at)
        except ExitError:
            return refuse("ROUTE_UNAVAILABLE", "PROVIDER_UNAVAILABLE")
        if not public:
            return refuse("REJECTED", "MARKET_NOT_PUBLIC")
        if "apr_bps" in operation:
            apr_bps = _integer(operation["apr_bps"], "operation.apr_bps", minimum=0)
        else:
            rate = row.get("rate_bps")
            if type(rate) is int and rate >= 0:
                apr_bps = rate
            else:
                metadata = row.get("metadata") or {}
                rate = metadata.get("lender_ask_rate") if isinstance(metadata, Mapping) else None
                try:
                    if rate is None or isinstance(rate, bool):
                        raise ValueError("no rate")
                    apr_bps = Fraction(str(rate)) * 10_000
                    if apr_bps < 0:
                        raise ValueError("negative rate")
                except (ValueError, ZeroDivisionError):
                    return refuse("REJECTED", "NO_RATE")
        try:
            envelope = self.exit_provider.fetch(row.get("market_id"), deadline_at=deadline_at)
            payload = envelope.get("data") if isinstance(envelope, Mapping) else None
            market = parse_market(payload, chain_id)
            market_id = to_market_id(market)
            if (not isinstance(row.get("market_id"), str)
                    or market_id != row["market_id"].lower()
                    or not isinstance(payload.get("market_id"), str)
                    or payload["market_id"].lower() != market_id):
                return refuse("REJECTED", "MARKET_KEY_MISMATCH")
            loan = row.get("loan_token")
            if (not isinstance(loan, Mapping) or not isinstance(loan.get("address"), str)
                    or market.loanToken.lower() != loan["address"].lower()):
                return refuse("REJECTED", "MARKET_KEY_MISMATCH")
            decimals, symbol = loan.get("decimals"), loan.get("symbol")
            if (type(decimals) is not int or not 0 <= decimals <= 255
                    or not isinstance(symbol, str) or not symbol.strip()):
                return refuse("REJECTED", "INSTRUMENT_DECIMALS_UNKNOWN")
            reader = self._order_reader(chain_id, deadline_at)
            block = reader.head_block()
            spacing, = _words(reader.eth_call(market.midnight, calldata(
                selector("tickSpacing(bytes32)"), ("bytes32",), (market_id,)), block), 1)
            if not 0 < spacing <= MAX_TICK:
                return refuse("REJECTED", "INVALID_TICK_SPACING")
            if self.monotonic() >= deadline_at:
                return refuse("ROUTE_UNAVAILABLE", "ONCHAIN_UNAVAILABLE")
            verified = VerifiedMarket(market, market_id, decimals, symbol, spacing)
            now = (self.clock or (lambda: datetime.now(UTC)))()
            if verified.market.chainId != chain_id:
                return refuse("REJECTED", "MARKET_KEY_MISMATCH")
            row_market_id = row.get("market_id")
            if (not isinstance(row_market_id, str)
                    or verified.market_id.lower() != row_market_id.lower()):
                return refuse("REJECTED", "MARKET_KEY_MISMATCH")
            if verified.market.maturity <= int(now.timestamp()):
                return refuse("REJECTED", "MATURED")
            built = build_order(verified, wallet, amount, apr_bps, int(now.timestamp()), expiry)
            authorized = self._ratifier_authorized(reader, built["offer"], chain_id, wallet, block)
            if build_facts is not None:
                build_facts["ratifier_authorized"] = authorized
            placeholder = {"v": 27, "r": "0x" + "00" * 32, "s": "0x" + "00" * 32}
            payload = publish_transaction({**built, "chain_id": chain_id}, placeholder)
            issues = self.midnight_policy.issues(chain_id, bytes.fromhex(payload["data"][2:]), now,
                                                 deadline_at=deadline_at)
            if issues:
                return {**refuse("REJECTED", "MEMPOOL_POLICY"), "policy_issues": issues}
        except ExitError as exc:
            return refuse("ROUTE_UNAVAILABLE" if exc.code == "PROVIDER_UNAVAILABLE" else "REJECTED", exc.code)
        except OnchainError:
            return refuse("ROUTE_UNAVAILABLE", "ONCHAIN_UNAVAILABLE")
        except Throttled as exc:
            return refuse("ROUTE_UNAVAILABLE", exc.code)
        except OrderError as exc:
            unavailable = {"TENOR_UNAVAILABLE", "ORDERS_UNAVAILABLE", "PROVIDER_THROTTLED", "POLICY_UNAVAILABLE"}
            return refuse("ROUTE_UNAVAILABLE" if exc.code in unavailable else "REJECTED", exc.code)
        position = PositionRow(
            chain_id, venue, name, row.get("verdict"), row.get("reason"),
            row.get("rate_bps"), row.get("rate_kind"), datetime.fromtimestamp(verified.market.maturity, UTC),
            verified.market.loanToken, verified.decimals, verified.symbol, verified.market.midnight,
        )
        record = {**built, "wallet": wallet, "chain_id": chain_id, "amount_raw": str(amount),
                  "instrument": position.to_dict()}
        approval = {
            "chainId": chain_id, "from": wallet, "to": verified.market.loanToken,
            "data": _APPROVE_SELECTOR + _word(verified.market.midnight) + _word(amount),
            "value": "0", "spender": verified.market.midnight, "amount": str(amount),
        }
        self._persist_observation({"schema_version": LENDING_ORDER_OPERATION_SCHEMA,
                                   "method_version": method, "as_of": as_of.isoformat(),
                                   "order": record, "approval": approval}, suffix="lending-order")
        if not persist:
            return record
        saved = self.orders.create(record)
        return self._lending_ready(saved, authorized=authorized)

    def _ratifier_authorized(self, reader: Any, offer: Mapping[str, Any], chain_id: int,
                             wallet: str, block: Any) -> bool:
        addresses = ADDRESSES.get(chain_id)
        if (addresses is None or offer["ratifier"].lower() != addresses.ratifier.lower()
                or offer["market"]["midnight"].lower() != addresses.midnight.lower()
                or offer["maker"].lower() != wallet.lower()):
            raise OrderError("ORDER_RECORD_INVALID", "Only Morpho's order checker may be authorized")
        raw = reader.eth_call(addresses.midnight, calldata(selector("isAuthorized(address,address)"),
                              ("address", "address"), (wallet, addresses.ratifier)), block)
        if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw) or int(raw, 16) not in (0, 1):
            raise OnchainError("Invalid ratifier authorization answer")
        return bool(int(raw, 16))

    def _lending_ready(self, record: Mapping[str, Any], *, authorized: bool | None = None) -> dict[str, Any]:
        wallet, chain_id = record["wallet"], record["chain_id"]
        try:
            if authorized is None:
                reader = self._order_reader(chain_id)
                block = reader.head_block()
                authorized = self._ratifier_authorized(reader, record["offer"], chain_id, wallet, block)
        except OrderError as exc:
            return self._lending_response("REJECTED", [exc.code], wallet=wallet, chain_id=chain_id, record=record)
        except (OnchainError, ValueError, TypeError, KeyError):
            return self._lending_response("ROUTE_UNAVAILABLE", ["ONCHAIN_UNAVAILABLE"],
                                          wallet=wallet, chain_id=chain_id, record=record)
        addresses = ADDRESSES[chain_id]
        authorization = None if authorized else {
            "chainId": chain_id, "from": wallet, "to": addresses.midnight,
            "data": calldata(selector("setIsAuthorized(address,bool,address)"),
                             ("address", "bool", "address"), (addresses.ratifier, True, wallet)),
            "value": "0", "authorized": addresses.ratifier,
        }
        spender = record["offer"]["market"]["midnight"]
        approval = {"chainId": chain_id, "from": wallet, "to": record["loan_token"],
                    "data": _APPROVE_SELECTOR + _word(spender) + _word(int(record["amount_raw"])),
                    "value": "0", "spender": spender, "amount": record["amount_raw"]}
        fields = {key: record[key] for key in (
            "offer", "root", "height", "leaf_index", "proof", "typed_data", "mempool",
            "apr_bps", "expires_at", "market_id", "decimals", "loan_token", "created_at")}
        return self._lending_response(
            "LENDING_ORDER_READY", [] if authorized else ["RATIFIER_AUTHORIZATION_REQUIRED"],
            wallet=wallet, chain_id=chain_id, record=record,
            ready=True, approval=approval, **fields,
            unsigned_authorization_request=authorization,
            authorization_request=authorization if self.gate.is_open_for(cross_chain=False) else None,
            signing_typed_data=record["typed_data"] if authorized and self.gate.is_open_for(cross_chain=False) else None,
        )

    def lending_order_replay(self, operation: Mapping[str, Any], root: str) -> dict[str, Any]:
        # A retry of the same maker instruction must not mint another independently fillable
        # root. Its evidence stays fixed, while authorisation and expiry use the current clock.
        _, _, wallet, chain_id, now = self._lending_context(
            operation, {"venue", "instrument", "amount_raw", "apr_bps", "expiry_seconds"})
        if not self.midnight_orders_enabled or self.midnight_policy is None or self.orders is None:
            return self._lending_response("ROUTE_UNAVAILABLE", ["ORDERS_UNAVAILABLE"],
                                          wallet=wallet, chain_id=chain_id, root=root)
        record = self.orders.get(root, wallet, chain_id)
        reason = ("ORDER_NOT_FOUND" if record is None else
                  "EXPIRED" if int(record["expires_at"]) <= int(now.timestamp()) else None)
        if reason:
            return self._lending_response("REJECTED", [reason], wallet=wallet, chain_id=chain_id,
                                          record=record, root=root)
        return self._lending_ready(record)

    def _lending_existing(self, operation: Mapping[str, Any], *, publish: bool = False) -> tuple:
        context = self._lending_context(operation, {"root", "signature"} if publish else {"root"})
        root = _evm_hash(_required(operation, "root", "operation"), "operation.root")
        wallet, chain_id = context[2:4]
        record = None if self.orders is None else self.orders.get(root, wallet, chain_id)
        return (*context, root, record)

    def _order_reader(self, chain_id: int, deadline_at: float | None = None) -> Any:
        reader = self.order_reader_factory(registry().require(chain_id))
        reader.deadline_at = self.monotonic() + 12 if deadline_at is None else deadline_at
        reader.timeout = min(getattr(reader, "timeout", 8), 8)
        return reader

    def lending_order_publish(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of, wallet, chain_id, now, root, record = self._lending_existing(operation, publish=True)

        def refuse(status: str, code: str) -> dict[str, Any]:
            return self._lending_response(status, [code], wallet=wallet, chain_id=chain_id, record=record)

        if not self.midnight_orders_enabled or self.midnight_policy is None or self.orders is None:
            return refuse("ORDERS_UNAVAILABLE", "ORDERS_UNAVAILABLE")
        if record is None:
            return refuse("REJECTED", "ORDER_NOT_FOUND")
        if int(record["expires_at"]) <= int(now.timestamp()):
            return refuse("REJECTED", "EXPIRED")
        signature = _object(_required(operation, "signature", "operation"), "operation.signature")
        _only(signature, {"v", "r", "s"}, "operation.signature")
        signature = {"v": _integer(_required(signature, "v", "signature"), "signature.v"),
                     "r": _evm_hash(_required(signature, "r", "signature"), "signature.r"),
                     "s": _evm_hash(_required(signature, "s", "signature"), "signature.s")}
        evidence = {"method_version": method, "as_of": as_of.isoformat(),
                    "root": root, "wallet": wallet, "chain_id": chain_id, "signature": signature}
        try:
            deadline_at = self.monotonic() + LENDING_RESPONSE_SECONDS
            reader = self._order_reader(chain_id, deadline_at)
            block = reader.head_block()
            if not self._ratifier_authorized(reader, record["offer"], chain_id, wallet, block):
                return refuse("REJECTED", "RATIFIER_NOT_AUTHORIZED")
            verdict = verify_signature(reader, record, signature, evidence=evidence, block=block)
            evidence["ratifier_verdict"] = verdict
            if verdict != "RATIFIED":
                self._persist_observation(evidence, suffix="lending-order-publish")
                return refuse("REJECTED", verdict)
            transaction = publish_transaction(record, signature)
            at = (self.clock or (lambda: datetime.now(UTC)))()
            issues = self.midnight_policy.issues(chain_id, bytes.fromhex(transaction["data"][2:]), at,
                                                 deadline_at=deadline_at)
            if issues:
                return {**refuse("REJECTED", "MEMPOOL_POLICY"), "policy_issues": issues}
        except OnchainError:
            evidence["reason_code"] = "ONCHAIN_UNAVAILABLE"
            self._persist_observation(evidence, suffix="lending-order-publish")
            return refuse("ROUTE_UNAVAILABLE", "ONCHAIN_UNAVAILABLE")
        except OrderError as exc:
            return refuse("ROUTE_UNAVAILABLE" if exc.code == "POLICY_UNAVAILABLE" else "REJECTED", exc.code)
        except ValueError:
            return refuse("REJECTED", "SIGNATURE_UNVERIFIED")
        if int(record["expires_at"]) <= int((self.clock or (lambda: datetime.now(UTC)))().timestamp()):
            return refuse("REJECTED", "EXPIRED")
        self._persist_observation({**evidence, "unsigned_transaction_request": transaction},
                                  suffix="lending-order-publish")
        saved = self.orders.set_status(root, wallet, chain_id, "SIGNED", signature=signature)
        if saved["status"] != "SIGNED":
            return refuse("REJECTED", saved["status"])
        return self._lending_response("LENDING_ORDER_PUBLISHABLE", [], wallet=wallet, chain_id=chain_id,
                                     record=record, unsigned_request=transaction, ready=True, root=root)

    def lending_order_cancel(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of, wallet, chain_id, _now, root, record = self._lending_existing(operation)
        if not self.midnight_orders_enabled or self.midnight_policy is None or self.orders is None:
            return self._lending_response("ORDERS_UNAVAILABLE", ["ORDERS_UNAVAILABLE"],
                                          wallet=wallet, chain_id=chain_id)
        if record is None:
            return self._lending_response("REJECTED", ["ORDER_NOT_FOUND"], wallet=wallet, chain_id=chain_id)
        transaction = cancel_transaction(wallet, chain_id, root)
        return self._lending_response("CANCEL_READY", [], wallet=wallet, chain_id=chain_id,
                                     record=record, unsigned_request=transaction, ready=True, root=root)

    def lending_order_list(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of, wallet, chain_id, now = self._lending_context(operation, set())
        if not self.midnight_orders_enabled or self.midnight_policy is None or self.orders is None:
            return self._lending_response("ORDERS_UNAVAILABLE", ["ORDERS_UNAVAILABLE"],
                                          wallet=wallet, chain_id=chain_id)
        records = [record for record in self.orders.by_wallet(wallet, chain_id) if record["status"] == "SIGNED"]
        records = sorted(records, key=lambda record: (record["created_at"], record["root"]), reverse=True)
        active = [record for record in records if int(record["expires_at"]) > int(now.timestamp())]
        expired = [record for record in records if int(record["expires_at"]) <= int(now.timestamp())]
        unavailable = {"canceled": None, "position": None, "block": None,
                       "reason_codes": ["ONCHAIN_UNAVAILABLE"]}
        deadline_at = self.monotonic() + LENDING_RESPONSE_SECONDS
        try:
            facts_by_root = (read_orders_facts(self._order_reader(chain_id, deadline_at),
                                               active) if active else {})
        except (OrderError, OnchainError, ValueError):
            facts_by_root = {}
        authorization_reason = None
        if active:
            try:
                reader = self._order_reader(chain_id, deadline_at)
                block = reader.head_block()
                if not self._ratifier_authorized(reader, active[0]["offer"], chain_id, wallet, block):
                    authorization_reason = "RATIFIER_NOT_AUTHORIZED"
            except (OrderError, OnchainError, ValueError, TypeError, KeyError):
                authorization_reason = "ONCHAIN_UNAVAILABLE"
        results = []
        for record in active + expired:
            is_expired = int(record["expires_at"]) <= int(now.timestamp())
            facts = dict({"canceled": None, "position": None, "block": None} if is_expired
                         else facts_by_root.get(record["root"], unavailable))
            # The contract reports the maker's aggregate market position, not per-order fill evidence.
            facts["market_position"] = facts.pop("position")
            public_record = {key: value for key, value in record.items() if key != "signature"}
            if not is_expired:
                addresses = ADDRESSES.get(chain_id)
                reason = ("ORDER_RECORD_INVALID" if addresses is None or
                          record["offer"]["ratifier"].lower() != addresses.ratifier.lower()
                          else authorization_reason)
                if reason:
                    facts["fillable"] = False
                    facts["reason_codes"] = sorted(set(facts.get("reason_codes", []) + [reason]))
            results.append({**public_record, **facts, "expired": is_expired})
        self._persist_observation({"method_version": method, "as_of": as_of.isoformat(),
                                   "wallet": wallet, "chain_id": chain_id, "orders": results},
                                  suffix="lending-order-list")
        return self._lending_response("LENDING_ORDERS", [], wallet=wallet, chain_id=chain_id,
                                     ready=True, orders=results)

    def position_exit(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        kind = _required(operation, "kind", "operation")
        fields = {"EXACTLY_EXIT": {"market_address", "maturity"}, "TENOR_EXIT": {"market_id"},
                  "PT_REDEEM": {"instrument_address"}}
        if not isinstance(kind, str) or kind not in fields:
            raise APIError("INVALID_KIND", "operation.kind must be EXACTLY_EXIT, TENOR_EXIT or PT_REDEEM")
        method, _ = self._operation(operation, POSITION_EXIT_OPERATION_SCHEMA,
                                    {"wallet", "chain_id", "kind", "accept_cost_bps"} | fields[kind])
        if method != "exit/1":
            raise APIError("METHOD_VERSION_MISMATCH", "operation.method_version must be exit/1")
        request = dict(operation)
        request["wallet"] = _evm_address(_required(operation, "wallet", "operation"), "operation.wallet")
        request["chain_id"] = _integer(_required(operation, "chain_id", "operation"), "operation.chain_id", minimum=1)
        if "accept_cost_bps" in operation:
            request["accept_cost_bps"] = _integer(
                operation["accept_cost_bps"], "operation.accept_cost_bps", minimum=0, maximum=10000)
        if kind == "EXACTLY_EXIT":
            request["market_address"] = _evm_address(_required(operation, "market_address", "operation"), "operation.market_address")
            request["maturity"] = _integer(
                _required(operation, "maturity", "operation"), "operation.maturity",
                minimum=0, maximum=2**256 - 1)
        elif kind == "TENOR_EXIT":
            request["market_id"] = _evm_hash(_required(operation, "market_id", "operation"), "operation.market_id")
        else:
            request["instrument_address"] = _evm_address(
                _required(operation, "instrument_address", "operation"), "operation.instrument_address")
        return build_exit(self, request)

    def position_deposit(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        """Build a direct deposit into a fixed-rate position the engine priced.

        No router is involved: the wallet calls the venue's own market in one transaction on one
        chain, which either completes or reverts whole. What is deposited, where, and at what
        precision all come from this deployment's universe; the caller names the position and the
        amount and nothing else.
        """
        deadline_at = self.monotonic() + LENDING_RESPONSE_SECONDS
        method, as_of = self._operation(operation, POSITION_OPERATION_SCHEMA, {
            "wallet", "chain_id", "venue", "instrument", "amount_raw",
        })
        wallet = _evm_address(_required(operation, "wallet", "operation"), "operation.wallet")
        chain_id = _integer(_required(operation, "chain_id", "operation"), "operation.chain_id", minimum=1)
        venue = _text(_required(operation, "venue", "operation"), "operation.venue", maximum=64)
        name = _text(_required(operation, "instrument", "operation"), "operation.instrument", maximum=128)
        amount_raw = _raw_amount(_required(operation, "amount_raw", "operation"), "operation.amount_raw")
        now = (self.clock or (lambda: datetime.now(UTC)))()

        def refuse(status: str, reasons: list[str], position: Any = None) -> dict[str, Any]:
            return self._route_response(
                status, reasons, instrument=position, wallet=wallet, amount_raw=amount_raw,
                pay_address=None if position is None else position.underlying_address,
                provenance="SERVER_BUILT", pay_chain_id=chain_id,
            )

        if self.instruments is None:
            return refuse("ROUTE_UNAVAILABLE", ["UNIVERSE_UNAVAILABLE"])
        try:
            position = self.instruments.resolve_position(chain_id, venue, name, now=now)
        except InstrumentError as exc:
            return refuse("ROUTE_UNAVAILABLE" if exc.code in _UNIVERSE_FAILURES else "REJECTED", [exc.code])

        refusals: list[str] = []
        if position.venue not in DEPOSIT_VENUES:
            refusals.append("UNSUPPORTED_VENUE")
        if position.verdict == "NO BID":
            refusals.append("NO_BID")
        elif position.verdict == "NO QUOTE" or position.rate_bps is None or position.rate_kind != "executable":
            # The floor is computed from an executable rate. Without one there is no promise to
            # hold the deposit to, and a deposit with no floor is a bet on whatever the pool pays.
            refusals.append("NO_QUOTE")
        seconds = int((position.maturity_at - now).total_seconds())
        if seconds <= 0:
            refusals.append("MATURED")
        if refusals:
            return refuse("REJECTED", refusals, position)

        # The rate is an APY: compounded, annual. Treating it as simple interest overstates a
        # sub-year return, and at a high enough rate even the floor would then exceed what the
        # pool pays. t * 2a / (2 + a) is a provable lower bound on (1 + a)^t - 1 for every rate and
        # term -- it rests on e^x - 1 >= x and ln(1 + a) >= 2a / (2 + a) -- and it stays in exact
        # rational arithmetic. At 4% over a month it is within 0.2% of the compounded figure.
        apy = Fraction(position.rate_bps, 10_000)
        term = Fraction(seconds, _YEAR_SECONDS)
        expected_yield = amount_raw * term * 2 * apy / (2 + apy)
        floor_yield = expected_yield * YIELD_FLOOR_BPS / 10_000
        expected = amount_raw + expected_yield.numerator // expected_yield.denominator
        minimum = amount_raw + floor_yield.numerator // floor_yield.denominator
        maturity_ts = int(position.maturity_at.timestamp())
        market = position.market_address
        deposit = {
            "chainId": chain_id,
            "from": wallet,
            "to": market,
            "data": _DEPOSIT_AT_MATURITY + _word(maturity_ts) + _word(amount_raw) + _word(minimum) + _word(wallet),
            "value": "0",
        }
        approval = {
            "chainId": chain_id,
            "from": wallet,
            "to": position.underlying_address,
            "data": _APPROVE_SELECTOR + _word(market) + _word(amount_raw),
            "value": "0",
            "spender": market,
            "amount": str(amount_raw),
        }
        observed = int(now.timestamp())
        route = {
            "from_chain": chain_id, "to_chain": chain_id,
            "from_token": position.underlying_address, "to_token": market,
            "from_amount": str(amount_raw),
            "to_amount": str(expected),
            # At maturity, in the deposit asset's own units.
            "to_amount_min": str(minimum),
            "tool": f"{position.venue} depositAtMaturity",
            "from_decimals": position.underlying_decimals,
            "to_decimals": position.underlying_decimals,
            "observed_at": observed,
            "expires_at": observed + DEPOSIT_VALIDITY_SECONDS,
            # Nobody quotes gas for a direct call; the wallet prices it at signing and shows it.
            "gas_cost_usd": None,
            "approval_address": market,
        }
        # A direct call on one chain: it deposits or reverts as a whole.
        reasons = list(self.gate.reason_codes_for(cross_chain=False))
        authorised = self.gate.is_open_for(cross_chain=False)
        evidence = {
                "schema_version": POSITION_OPERATION_SCHEMA,
                "method_version": method,
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "wallet": wallet,
                "amount_raw": str(amount_raw),
                "position": position.to_dict(),
                "deposit": route,
                "unsigned_deposit_request": deposit,
            }
        response = self._route_response(
            "DEPOSIT_READY", reasons, instrument=position, wallet=wallet, amount_raw=amount_raw,
            pay_address=position.underlying_address, route=route, unsigned_request=deposit, pay_chain_id=chain_id,
            authorised=authorised, approval=approval, fees=[], lifi_fee_micros=0,
            integrator_fee_micros=0, provenance="SERVER_BUILT",
        )
        try:
            reader = self._order_reader(chain_id, deadline_at)
            apply_gas_check(response, reader, chain_id=chain_id, wallet=wallet,
                            default_gas=300_000)
        except (OnchainError, ValueError, OSError):
            response["gas_check"] = None
            response["reason_codes"] = sorted(set(response["reason_codes"] + ["GAS_CHECK_UNAVAILABLE"]))
        evidence["gas_check"] = response.get("gas_check")
        evidence["response"] = response
        self._persist_observation(evidence, suffix="deposit")
        return response

    def _route_response(
        self,
        status: str,
        reasons: list[str],
        *,
        instrument: Any,
        wallet: str,
        amount_raw: int,
        pay_address: str | None,
        route: dict[str, Any] | None = None,
        unsigned_request: dict[str, Any] | None = None,
        authorised: bool = False,
        approval: dict[str, Any] | None = None,
        fees: list[dict[str, Any]] | None = None,
        lifi_fee_micros: int | None = None,
        integrator_fee_micros: int | None = None,
        provenance: str = "SERVER_FETCHED",
        pay_chain_id: int | None = None,
        size_check: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reset = None if approval is None else {
            **approval,
            "data": _APPROVE_SELECTOR + _word(approval["spender"]) + _word(0),
            "amount": "0",
        }
        # LI.FI credits the integrator's share to the integrator this server names, so when it
        # names one that share is Fenorium's fee -- part of the fees listed, not added to them. A
        # response saying Fenorium charges nothing while that share is taken says something untrue.
        if getattr(self.router, "integrator", None):
            fenorium_fee_micros = integrator_fee_micros
        else:
            fenorium_fee_micros = None if integrator_fee_micros is None else 0
        return {
            "status": status,
            "gas_check": None,
            "reason_codes": sorted(set(reasons)),
            # Named on every response, including the refusals, so a reader never has to infer
            # from the endpoint it happened to call which kind of evidence it is holding.
            "route_provenance": provenance,
            "wallet": wallet,
            "pay_address": pay_address,
            # Where the payment starts; the instrument's own chain unless a v2 request said otherwise.
            "pay_chain_id": pay_chain_id if pay_chain_id is not None else (
                None if instrument is None else instrument.chain_id),
            "amount_raw": str(amount_raw),
            "instrument": None if instrument is None else instrument.to_dict(),
            "route": route,
            # Evidence of what would be signed. Present whenever a fetched route supplied it,
            # because describing a transaction is not authorising one.
            "unsigned_transaction_request": unsigned_request,
            # The same bytes offered as an instruction rather than as evidence, and only once
            # every clearance is in. Two fields so a reader can tell which one it holds.
            "transaction_request": unsigned_request if authorised else None,
            # The allowance the swap needs, for the exact amount and to the contract the route
            # calls. Evidence always; an instruction on the same terms as the swap itself.
            "unsigned_approval_request": approval,
            "approval_request": approval if authorised else None,
            # Some tokens -- Ethereum USDT the best known -- refuse to move an allowance from one
            # positive value to another. The client sends this first only when an allowance exists
            # but is short of the amount; resetting to zero is harmless for every other token.
            "unsigned_approval_reset_request": reset,
            "approval_reset_request": reset if authorised else None,
            "broadcast_enabled": authorised,
            # What LI.FI says this route charges, each fee named. None when no route was fetched or
            # the route would not state its fees; never a zero standing in for "not stated".
            "fees": fees,
            "lifi_fee_usd_micros": lifi_fee_micros,
            "integrator_fee_usd_micros": integrator_fee_micros,
            "fenorium_fee_usd_micros": fenorium_fee_micros,
            # VALIDATE SIZE, as checked right before this route was offered; None when no route was fetched.
            "size_check": None if size_check is None else {
                key: value for key, value in size_check.items() if key != "reasons"},
        }

    def _persist_quote_observation(self, observation: Mapping[str, Any]) -> bool:
        """Write one validated quote observation once, beside existing evidence artifacts."""
        return self._persist_observation(observation, suffix="quote")

    def _persist_observation(
        self, observation: Mapping[str, Any], *, suffix: str, label: str | None = None,
    ) -> bool:
        """Write one validated observation once, beside existing evidence artifacts."""
        label = label or suffix
        if self.evidence_dir is None:
            return False
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise APIError("EVIDENCE_EXPORT_FAILED", "the evidence directory is unavailable", status=500) from exc
        payload = json.dumps(
            canonical_data(observation),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8") + b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        path = self.evidence_dir / f"{digest}.{suffix}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise APIError("EVIDENCE_EXPORT_FAILED", f"the existing {label} observation is unreadable", status=500) from exc
            if existing != payload:
                raise APIError("EVIDENCE_CONFLICT", f"a {label} observation exists with different bytes", status=409)
            return True
        except OSError as exc:
            raise APIError("EVIDENCE_EXPORT_FAILED", f"the {label} observation could not be created", status=500) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise APIError("EVIDENCE_EXPORT_FAILED", f"the {label} observation could not be written", status=500) from exc
        return True

    def decision_bundle(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        method, as_of = self._operation(
            operation,
            DECISION_OPERATION_SCHEMA,
            {"portfolio_commitment", "objective", "transformation", "expires_at", "costs", "risk_results", "mandate_result", "opportunity_result", "evidence", "legacy_ref", "persist_evidence"},
        )
        evidence = operation.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 1_000 or any(not isinstance(item, dict) for item in evidence):
            raise APIError("INVALID_FIELD", "evidence must be a bounded array of objects")
        bundle = DecisionBundle(
            method_version=method,
            as_of=as_of,
            portfolio_commitment=_evm_hash(_required(operation, "portfolio_commitment", "operation"), "operation.portfolio_commitment"),
            objective=_text(_required(operation, "objective", "operation"), "operation.objective", maximum=128),
            transformation=_object(_required(operation, "transformation", "operation"), "operation.transformation"),
            expires_at=_timestamp(_required(operation, "expires_at", "operation"), "operation.expires_at"),
            costs=None if operation.get("costs") is None else _cost_ledger(operation["costs"]),
            risk_results=_object(operation.get("risk_results", {}), "operation.risk_results"),
            mandate_result=None if operation.get("mandate_result") is None else _object(operation["mandate_result"], "operation.mandate_result"),
            opportunity_result=None if operation.get("opportunity_result") is None else _object(operation["opportunity_result"], "operation.opportunity_result"),
            evidence=tuple(evidence),
            legacy_ref=None if operation.get("legacy_ref") is None else _text(operation["legacy_ref"], "operation.legacy_ref"),
        )
        persist_evidence = _boolean(operation.get("persist_evidence", False), "operation.persist_evidence")
        persisted = False
        if persist_evidence:
            persisted = self._persist_bundle(bundle)
        return {
            "bundle": bundle.to_dict(),
            "decision_hash": bundle.decision_hash,
            "canonical_json": bundle.canonical_bytes().decode("utf-8"),
            "anchored": False,
            "persisted": persisted,
        }

    def _persist_bundle(self, bundle: DecisionBundle) -> bool:
        if self.evidence_dir is None:
            raise APIError("EVIDENCE_EXPORT_DISABLED", "the evidence export path is not configured", status=409)
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise APIError("EVIDENCE_EXPORT_FAILED", "the evidence directory is unavailable", status=500) from exc
        path = self.evidence_dir / f"{bundle.decision_hash[2:].lower()}.decision.json"
        payload = bundle.canonical_bytes() + b"\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise APIError("EVIDENCE_EXPORT_FAILED", "the existing evidence artifact is unreadable", status=500) from exc
            if existing != payload:
                raise APIError("EVIDENCE_CONFLICT", "an evidence file already exists with different bytes", status=409)
            return True
        except OSError as exc:
            raise APIError("EVIDENCE_EXPORT_FAILED", "the evidence artifact could not be created", status=500) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise APIError("EVIDENCE_EXPORT_FAILED", "the evidence artifact could not be written", status=500) from exc
        return True


def _net_yield_bps(yield_bps: int, cost_bps: int) -> int:
    """Return to maturity after a route's cost, floored to bp.

    The cost is a share of what the buyer pays; the yield accrues only on what is left, so the two
    compound -- (1 - cost)(1 + yield) - 1 -- rather than subtract. At a 9% cost and a 10% yield the
    difference is about 90 bp, enough to call a losing purchase profitable.
    """
    return ((10_000 - cost_bps) * (10_000 + yield_bps) - 10_000 ** 2) // 10_000


def json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Stable transport JSON; authoritative nested bundles retain their own hashes."""
    def wire_value(value: Any, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {name: wire_value(child, str(name)) for name, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [wire_value(child) for child in value]
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and key is not None
            and (key == "raw_amount" or key.endswith("_raw_amount")
                 or key == "price_usd_fixed" or key.endswith("_usd_micros"))
        ):
            return str(value)
        return value

    return json.dumps(
        wire_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
