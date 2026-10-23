"""Vela-facing confidential-policy contract with an honest local reference path.

No Vela runtime is bundled. ``evaluate_emulated_policy`` exists only for golden
vectors and local conformance; its result is always labelled ``EMULATED``. A
production adapter must return an attestation reference before it can claim
``ATTESTED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .contracts import (
    PortfolioContractError,
    ReasonCode,
    canonical_hash,
    canonical_json_bytes,
    require_utc,
    utc_iso,
)


CONFIDENTIAL_REQUEST_SCHEMA = "fenorium/confidential-policy-request/v1"
CONFIDENTIAL_RESULT_SCHEMA = "fenorium/confidential-policy-result/v1"


class PolicyComparator(str, Enum):
    LESS_THAN_OR_EQUAL = "LTE"
    GREATER_THAN_OR_EQUAL = "GTE"
    EQUAL = "EQ"


class PolicyCheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ConfidentialPolicyStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


class AttestationState(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    EMULATED = "EMULATED"
    ATTESTED = "ATTESTED"


def _commitment(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise PortfolioContractError(f"{name} must be a 32-byte 0x-prefixed commitment")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise PortfolioContractError(f"{name} must contain hexadecimal bytes") from exc
    return value.lower()


@dataclass(frozen=True, slots=True)
class ConfidentialPolicyCheck:
    """One private numeric comparison used only by the local/WASM input boundary."""

    check_id: str
    comparator: PolicyComparator
    observed: int | None
    limit: int | None
    failure_reason: ReasonCode

    def __post_init__(self) -> None:
        if not self.check_id or len(self.check_id) > 128:
            raise PortfolioContractError("confidential policy check_id is required and bounded")
        if not isinstance(self.comparator, PolicyComparator):
            raise PortfolioContractError("confidential policy comparator is invalid")
        if not isinstance(self.failure_reason, ReasonCode):
            raise PortfolioContractError("confidential policy failure_reason is invalid")
        for name in ("observed", "limit"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise PortfolioContractError(f"confidential policy {name} must be an integer or null")

    def private_input_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "comparator": self.comparator.value,
            "observed": self.observed,
            "limit": self.limit,
            "failure_reason": self.failure_reason.value,
        }


@dataclass(frozen=True, slots=True)
class ConfidentialPolicyRequest:
    method_version: str
    as_of: datetime
    portfolio_commitment: str
    mandate_commitment: str
    checks: tuple[ConfidentialPolicyCheck, ...]
    schema_version: str = CONFIDENTIAL_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CONFIDENTIAL_REQUEST_SCHEMA or not self.method_version:
            raise PortfolioContractError("confidential policy request version and method are required")
        require_utc(self.as_of, field_name="ConfidentialPolicyRequest.as_of")
        object.__setattr__(self, "portfolio_commitment", _commitment(self.portfolio_commitment, "portfolio_commitment"))
        object.__setattr__(self, "mandate_commitment", _commitment(self.mandate_commitment, "mandate_commitment"))
        identifiers = [check.check_id for check in self.checks]
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise PortfolioContractError("confidential policy checks must be non-empty and uniquely identified")

    def conformance_payload(self) -> dict[str, Any]:
        """Sensitive local/WASM input. Never include this payload in a result or log."""

        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="ConfidentialPolicyRequest.as_of"),
            "portfolio_commitment": self.portfolio_commitment,
            "mandate_commitment": self.mandate_commitment,
            "checks": [
                check.private_input_dict()
                for check in sorted(self.checks, key=lambda item: item.check_id)
            ],
        }

    def conformance_bytes(self) -> bytes:
        return canonical_json_bytes(self.conformance_payload())


@dataclass(frozen=True, slots=True)
class ConfidentialPolicyCheckResult:
    check_id: str
    status: PolicyCheckStatus
    reason_code: ReasonCode

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
        }


@dataclass(frozen=True, slots=True)
class ConfidentialPolicyResult:
    method_version: str
    as_of: datetime
    status: ConfidentialPolicyStatus
    checks: tuple[ConfidentialPolicyCheckResult, ...]
    portfolio_commitment: str
    mandate_commitment: str
    method_hash: str
    attestation_state: AttestationState
    attestation_ref: str | None = None
    schema_version: str = CONFIDENTIAL_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CONFIDENTIAL_RESULT_SCHEMA or not self.method_version:
            raise PortfolioContractError("confidential policy result version and method are required")
        require_utc(self.as_of, field_name="ConfidentialPolicyResult.as_of")
        object.__setattr__(self, "portfolio_commitment", _commitment(self.portfolio_commitment, "portfolio_commitment"))
        object.__setattr__(self, "mandate_commitment", _commitment(self.mandate_commitment, "mandate_commitment"))
        object.__setattr__(self, "method_hash", _commitment(self.method_hash, "method_hash"))
        if self.attestation_state is AttestationState.ATTESTED and not self.attestation_ref:
            raise PortfolioContractError("an ATTESTED result requires an attestation_ref")
        if self.attestation_state is not AttestationState.ATTESTED and self.attestation_ref is not None:
            raise PortfolioContractError("non-attested results cannot carry an attestation_ref")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="ConfidentialPolicyResult.as_of"),
            "status": self.status.value,
            "checks": [
                check.to_dict() for check in sorted(self.checks, key=lambda item: item.check_id)
            ],
            "portfolio_commitment": self.portfolio_commitment,
            "mandate_commitment": self.mandate_commitment,
            "method_hash": self.method_hash,
            "attestation_state": self.attestation_state.value,
            "attestation_ref": self.attestation_ref,
        }


def _method_hash(request: ConfidentialPolicyRequest) -> str:
    return canonical_hash(
        {
            "schema_version": request.schema_version,
            "method_version": request.method_version,
            "checks": [
                {
                    "check_id": check.check_id,
                    "comparator": check.comparator.value,
                    "failure_reason": check.failure_reason.value,
                }
                for check in sorted(request.checks, key=lambda item: item.check_id)
            ],
        }
    )


def evaluate_emulated_policy(request: ConfidentialPolicyRequest) -> ConfidentialPolicyResult:
    """Deterministic reference evaluation, explicitly not confidential or attested."""

    results: list[ConfidentialPolicyCheckResult] = []
    for check in sorted(request.checks, key=lambda item: item.check_id):
        if check.observed is None or check.limit is None:
            results.append(
                ConfidentialPolicyCheckResult(
                    check.check_id, PolicyCheckStatus.UNKNOWN, ReasonCode.UNRESOLVED_POSITION
                )
            )
            continue
        passed = {
            PolicyComparator.LESS_THAN_OR_EQUAL: check.observed <= check.limit,
            PolicyComparator.GREATER_THAN_OR_EQUAL: check.observed >= check.limit,
            PolicyComparator.EQUAL: check.observed == check.limit,
        }[check.comparator]
        results.append(
            ConfidentialPolicyCheckResult(
                check.check_id,
                PolicyCheckStatus.PASS if passed else PolicyCheckStatus.FAIL,
                ReasonCode.MANDATE_PASS if passed else check.failure_reason,
            )
        )
    status = (
        ConfidentialPolicyStatus.UNAVAILABLE
        if any(result.status is PolicyCheckStatus.UNKNOWN for result in results)
        else ConfidentialPolicyStatus.FAIL
        if any(result.status is PolicyCheckStatus.FAIL for result in results)
        else ConfidentialPolicyStatus.PASS
    )
    return ConfidentialPolicyResult(
        method_version=request.method_version,
        as_of=request.as_of,
        status=status,
        checks=tuple(results),
        portfolio_commitment=request.portfolio_commitment,
        mandate_commitment=request.mandate_commitment,
        method_hash=_method_hash(request),
        attestation_state=AttestationState.EMULATED,
    )


def unavailable_confidential_policy(request: ConfidentialPolicyRequest) -> ConfidentialPolicyResult:
    """Fail-closed adapter response used while an attested Vela runtime is unavailable."""

    return ConfidentialPolicyResult(
        method_version=request.method_version,
        as_of=request.as_of,
        status=ConfidentialPolicyStatus.UNAVAILABLE,
        checks=tuple(
            ConfidentialPolicyCheckResult(
                check.check_id, PolicyCheckStatus.UNKNOWN, ReasonCode.VELA_UNAVAILABLE
            )
            for check in sorted(request.checks, key=lambda item: item.check_id)
        ),
        portfolio_commitment=request.portfolio_commitment,
        mandate_commitment=request.mandate_commitment,
        method_hash=_method_hash(request),
        attestation_state=AttestationState.UNAVAILABLE,
    )


@runtime_checkable
class VelaConfidentialAdapter(Protocol):
    """Future adapter boundary. Production implementations must return ATTESTED evidence."""

    def evaluate(self, request: ConfidentialPolicyRequest) -> ConfidentialPolicyResult: ...
