"""Versioned, integer-only contracts shared by Fenorium portfolio modules.

The existing engine has deliberately small, purpose-built contracts.  The portfolio
surface adds a second layer around them, but keeps the same evidence rule: a value
that reaches a decision hash is either an integer, a string, a boolean, or an
explicit null.  In particular, Python floats are refused before canonical JSON is
created.
"""

from __future__ import annotations

import re

from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
import json
from typing import Any, Mapping, Sequence

from engine.keccak import keccak256


PORTFOLIO_SCHEMA_VERSION = "fenorium/portfolio/v1"
DECISION_BUNDLE_SCHEMA_VERSION = "DecisionBundle/v2"
OUTCOME_BUNDLE_SCHEMA_VERSION = "OutcomeBundle/v1"
USD_MICROS = 1_000_000
PRICE_SCALE = 100_000_000
BPS = 10_000


class PortfolioContractError(ValueError):
    """Raised when a portfolio contract cannot be represented honestly."""


class CanonicalizationError(PortfolioContractError):
    """Raised when a hashed payload contains an unsupported value."""


class PositionSupportStatus(str, Enum):
    """The overall support label; capability labels remain authoritative."""

    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    UNRESOLVED = "UNRESOLVED"


class CapabilityStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    UNRESOLVED = "UNRESOLVED"
    UNSUPPORTED = "UNSUPPORTED"


class MetricStatus(str, Enum):
    COMPUTED = "COMPUTED"
    NOT_COMPUTABLE = "NOT_COMPUTABLE"
    INTERNAL_POLICY_ONLY = "INTERNAL_POLICY_ONLY"


_ENDPOINT_PATTERN = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)


def redact_endpoints(text: str) -> str:
    """Replace every URL in a diagnostic with `<endpoint>`; see safe_detail for why."""

    return _ENDPOINT_PATTERN.sub("<endpoint>", text)


def safe_detail(exc: BaseException, *, limit: int = 300) -> str:
    """An exception rendered for a payload a user or a file may see.

    `rpc_call` names the endpoint it failed against, and `rpc_endpoints` exists so a scheduled
    job can inject a private or paid RPC without putting its key in source control. Those two
    facts meet here: interpolating the exception copies that key into serialized evidence. The
    endpoint is replaced rather than trimmed, since a key can sit in the host, the path or the
    query, and the useful part of the diagnostic is the method and the failure, not the URL.
    """

    text = redact_endpoints(f"{type(exc).__name__}: {exc}")
    return text[:limit]


class DiscoveryScope(str, Enum):
    REGISTERED_ASSETS_ONLY = "REGISTERED_ASSETS_ONLY"
    # Every token an index named, on top of the registry. It widens what was *looked at*, and
    # nothing else: an enumerated token is disclosure, never a valued position.
    REGISTERED_PLUS_ENUMERATED = "REGISTERED_PLUS_ENUMERATED"


class ReasonCode(str, Enum):
    """Stable, machine-readable reasons.  Detail strings are never the contract."""

    COMPUTED = "COMPUTED"
    REGISTERED_ASSETS_ONLY = "REGISTERED_ASSETS_ONLY"
    ZERO_BALANCE = "ZERO_BALANCE"
    ADAPTER_ERROR = "ADAPTER_ERROR"
    UNSUPPORTED_CHAIN = "UNSUPPORTED_CHAIN"
    DEMO_CHAIN_NOT_ENABLED = "DEMO_CHAIN_NOT_ENABLED"
    UNRESOLVED_POSITION = "UNRESOLVED_POSITION"
    UNREGISTERED_ASSET = "UNREGISTERED_ASSET"
    ENUMERATION_TRUNCATED = "ENUMERATION_TRUNCATED"
    ENUMERATION_UNAVAILABLE = "ENUMERATION_UNAVAILABLE"
    HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"
    HISTORY_TRUNCATED = "HISTORY_TRUNCATED"
    MISSING_PRICE = "MISSING_PRICE"
    STALE_INPUT = "STALE_INPUT"
    INSUFFICIENT_SOURCES = "INSUFFICIENT_SOURCES"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    VALUE_COVERAGE_NOT_COMPUTABLE = "VALUE_COVERAGE_NOT_COMPUTABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    INCOMPATIBLE_MARKING_FREQUENCY = "INCOMPATIBLE_MARKING_FREQUENCY"
    MISSING_LIQUIDITY = "MISSING_LIQUIDITY"
    MISSING_EXECUTION_QUOTE = "MISSING_EXECUTION_QUOTE"
    METHODOLOGY_NOT_APPLICABLE = "METHODOLOGY_NOT_APPLICABLE"
    LEGAL_REVIEW_REQUIRED = "LEGAL_REVIEW_REQUIRED"
    VELA_UNAVAILABLE = "VELA_UNAVAILABLE"
    PERMISSION_UNKNOWN = "PERMISSION_UNKNOWN"
    NO_BID = "NO_BID"
    NO_QUOTE = "NO_QUOTE"
    MISSING_COST = "MISSING_COST"
    VARIABLE_INCOME_OPT_IN_REQUIRED = "VARIABLE_INCOME_OPT_IN_REQUIRED"
    VARIABLE_INCOME_LIMIT_EXCEEDED = "VARIABLE_INCOME_LIMIT_EXCEEDED"
    ISSUER_NOT_ALLOWED = "ISSUER_NOT_ALLOWED"
    PROTOCOL_NOT_ALLOWED = "PROTOCOL_NOT_ALLOWED"
    CHAIN_NOT_ALLOWED = "CHAIN_NOT_ALLOWED"
    LIQUIDITY_FLOOR_FAIL = "LIQUIDITY_FLOOR_FAIL"
    MATURITY_LIMIT_FAIL = "MATURITY_LIMIT_FAIL"
    CREDENTIAL_REQUIREMENT_FAIL = "CREDENTIAL_REQUIREMENT_FAIL"
    PROTOCOL_CONCENTRATION_FAIL = "PROTOCOL_CONCENTRATION_FAIL"
    MANDATE_PASS = "MANDATE_PASS"
    ES_REDUCTION = "ES_REDUCTION"
    INCOME_INCREASE = "INCOME_INCREASE"
    SUFFICIENT_DEPTH = "SUFFICIENT_DEPTH"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    OUTCOME_ALREADY_WRITTEN = "OUTCOME_ALREADY_WRITTEN"


def require_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Return an exactly UTC datetime and reject naive timestamps."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PortfolioContractError(f"{field_name} must be a timezone-aware UTC timestamp")
    return value.astimezone(UTC)


def utc_iso(value: datetime, *, field_name: str = "timestamp") -> str:
    """Stable ISO-8601 UTC rendering with a terminal Z."""

    normalized = require_utc(value, field_name=field_name)
    rendered = normalized.isoformat(timespec="microseconds")
    return rendered.replace("+00:00", "Z")


def normalize_address(value: str) -> str:
    """Normalize an EVM address without pretending that a symbol is identity."""

    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise PortfolioContractError("EVM identity must be a 0x-prefixed 20-byte address")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise PortfolioContractError("EVM identity contains non-hex characters") from exc
    return value.lower()


def stable_identity(chain_id: int, asset: str | None, *, kind: str) -> str:
    """Return the stable identity used in sort keys and hashes.

    Native balance has one explicit identity per chain.  Contract-backed positions
    use the contract address; tickers, display names, and protocol labels are never
    part of the key.
    """

    if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id <= 0:
        raise PortfolioContractError("chain_id must be a positive integer")
    if not isinstance(kind, str) or not kind:
        raise PortfolioContractError("position kind is required for a stable identity")
    normalized_asset = "native" if asset is None else normalize_address(asset)
    return f"eip155:{chain_id}/{kind.lower()}:{normalized_asset}"


def _primitive(value: Any, *, path: str) -> Any:
    """Convert a supported value to canonical JSON primitives and reject floats."""

    if isinstance(value, float):
        raise CanonicalizationError(
            f"float found at {path}; hashed portfolio bundles require integer fixed-point values"
        )
    if isinstance(value, Enum):
        return _primitive(value.value, path=path)
    if isinstance(value, datetime):
        return utc_iso(value, field_name=path)
    if is_dataclass(value):
        if hasattr(value, "to_dict"):
            return _primitive(value.to_dict(), path=path)
        return _primitive(asdict(value), path=path)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string object key at {path}")
            normalized[key] = _primitive(child, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (tuple, list)):
        return [_primitive(child, path=f"{path}[{index}]") for index, child in enumerate(value)]
    if isinstance(value, set | frozenset):
        normalized = [_primitive(child, path=f"{path}[]") for child in value]
        return sorted(normalized, key=lambda child: canonical_json_bytes(child))
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise CanonicalizationError(f"unsupported canonical value {type(value).__name__} at {path}")


def canonical_data(payload: Any) -> Any:
    """Return normalized JSON-compatible data after enforcing the float prohibition."""

    return _primitive(payload, path="$")


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize a contract to reproducible UTF-8 bytes.

    Mapping keys are sorted by the JSON encoder.  Callers retain order for semantic
    sequences and must explicitly sort any collection that is conceptually
    unordered; the supplied bundle and result contracts do that in their to_dict
    methods.
    """

    return json.dumps(
        canonical_data(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(payload: Any) -> str:
    """Keccak-256 digest of canonical JSON, compatible with engine.manifest."""

    return "0x" + keccak256(canonical_json_bytes(payload)).hex()


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """One independently reportable capability of a classified position."""

    status: CapabilityStatus
    reason_code: ReasonCode = ReasonCode.COMPUTED
    detail: str = ""
    evidence: tuple[str, ...] = ()
    schema_version: str = "fenorium/capability-result/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "fenorium/capability-result/v1":
            raise PortfolioContractError("CapabilityResult must use fenorium/capability-result/v1")
        if not isinstance(self.status, CapabilityStatus) or not isinstance(self.reason_code, ReasonCode):
            raise PortfolioContractError("CapabilityResult status and reason_code must use their enums")
        if not isinstance(self.detail, str) or any(not isinstance(item, str) for item in self.evidence):
            raise PortfolioContractError("CapabilityResult detail and evidence must be strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "detail": self.detail,
            "evidence": sorted(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class MetricResult:
    """A coverage-aware quantitative result or a first-class reason it is absent."""

    status: MetricStatus
    value: int | str | Mapping[str, Any] | None
    unit: str
    method_version: str
    as_of: datetime
    window: str | None = None
    frequency: str | None = None
    observations: int | None = None
    coverage_bps: int | None = None
    reason_code: ReasonCode = ReasonCode.COMPUTED
    excluded_positions: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: str = "fenorium/metric-result/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "fenorium/metric-result/v1":
            raise PortfolioContractError("MetricResult must use fenorium/metric-result/v1")
        if not isinstance(self.status, MetricStatus) or not isinstance(self.reason_code, ReasonCode):
            raise PortfolioContractError("MetricResult status and reason_code must use their enums")
        if not isinstance(self.method_version, str) or not self.method_version or not isinstance(self.unit, str) or not self.unit:
            raise PortfolioContractError("MetricResult method_version and unit are required strings")
        require_utc(self.as_of, field_name="MetricResult.as_of")
        if self.status is MetricStatus.COMPUTED and self.value is None:
            raise PortfolioContractError("a computed metric must carry a value")
        if self.observations is not None and (
            isinstance(self.observations, bool) or not isinstance(self.observations, int) or self.observations < 0
        ):
            raise PortfolioContractError("MetricResult.observations cannot be negative")
        if self.coverage_bps is not None and (
            isinstance(self.coverage_bps, bool)
            or not isinstance(self.coverage_bps, int)
            or not 0 <= self.coverage_bps <= BPS
        ):
            raise PortfolioContractError("MetricResult.coverage_bps must be in [0, 10000]")
        collections = (
            self.excluded_positions,
            self.sources,
            self.assumptions,
            self.limitations,
        )
        if any(any(not isinstance(item, str) for item in collection) for collection in collections):
            raise PortfolioContractError("MetricResult evidence collections must contain strings")
        canonical_data(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "value": canonical_data(self.value),
            "unit": self.unit,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="MetricResult.as_of"),
            "window": self.window,
            "frequency": self.frequency,
            "observations": self.observations,
            "coverage_bps": self.coverage_bps,
            "reason_code": self.reason_code.value,
            "excluded_positions": sorted(self.excluded_positions),
            "sources": sorted(self.sources),
            "assumptions": sorted(self.assumptions),
            "limitations": sorted(self.limitations),
        }


def computed_metric(
    value: int | str | Mapping[str, Any],
    *,
    unit: str,
    method_version: str,
    as_of: datetime,
    window: str | None = None,
    frequency: str | None = None,
    observations: int | None = None,
    coverage_bps: int | None = None,
    excluded_positions: Sequence[str] = (),
    sources: Sequence[str] = (),
    assumptions: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> MetricResult:
    """Small constructor used by modules that have passed all methodology gates."""

    return MetricResult(
        status=MetricStatus.COMPUTED,
        value=value,
        unit=unit,
        method_version=method_version,
        as_of=as_of,
        window=window,
        frequency=frequency,
        observations=observations,
        coverage_bps=coverage_bps,
        excluded_positions=tuple(excluded_positions),
        sources=tuple(sources),
        assumptions=tuple(assumptions),
        limitations=tuple(limitations),
    )


def unavailable_metric(
    reason_code: ReasonCode,
    *,
    unit: str,
    method_version: str,
    as_of: datetime,
    window: str | None = None,
    frequency: str | None = None,
    observations: int | None = None,
    coverage_bps: int | None = None,
    excluded_positions: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> MetricResult:
    """Return an explicit non-computability result rather than a synthetic zero."""

    return MetricResult(
        status=MetricStatus.NOT_COMPUTABLE,
        value=None,
        unit=unit,
        method_version=method_version,
        as_of=as_of,
        window=window,
        frequency=frequency,
        observations=observations,
        coverage_bps=coverage_bps,
        reason_code=reason_code,
        excluded_positions=tuple(excluded_positions),
        limitations=tuple(limitations),
    )
