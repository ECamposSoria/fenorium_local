"""Capability-first classification for registered wallet positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from engine.eligibility import GateStatus

from .contracts import (
    CapabilityResult,
    CapabilityStatus,
    PortfolioContractError,
    PositionSupportStatus,
    ReasonCode,
)
from .discovery import RawPosition, WalletSnapshot


def _configured_capability(status: CapabilityStatus, *, name: str) -> CapabilityResult:
    if status is CapabilityStatus.SUPPORTED:
        return CapabilityResult(status=status, reason_code=ReasonCode.COMPUTED)
    if status is CapabilityStatus.CONTEXT_ONLY:
        return CapabilityResult(
            status=status,
            reason_code=ReasonCode.METHODOLOGY_NOT_APPLICABLE,
            detail=f"{name} is context only for this registered asset",
        )
    if status is CapabilityStatus.UNSUPPORTED:
        return CapabilityResult(
            status=status,
            reason_code=ReasonCode.METHODOLOGY_NOT_APPLICABLE,
            detail=f"{name} is not supported for this registered asset",
        )
    return CapabilityResult(
        status=status,
        reason_code=ReasonCode.UNRESOLVED_POSITION,
        detail=f"{name} support is not established",
    )


def capability_from_gate(status: GateStatus | None) -> CapabilityResult:
    """Adapt the established eligibility probe contract without making it a wallet claim."""

    if status is GateStatus.PERMISSIONLESS:
        return CapabilityResult(CapabilityStatus.SUPPORTED, ReasonCode.COMPUTED)
    if status is GateStatus.GATED:
        return CapabilityResult(
            CapabilityStatus.CONTEXT_ONLY,
            ReasonCode.LEGAL_REVIEW_REQUIRED,
            "a gate was measured, but this snapshot does not establish wallet eligibility",
        )
    return CapabilityResult(
        CapabilityStatus.UNRESOLVED,
        ReasonCode.PERMISSION_UNKNOWN,
        "permission status is unknown; unknown never passes a mandate",
    )


def _unresolved_capability(detail: str) -> CapabilityResult:
    return CapabilityResult(
        status=CapabilityStatus.UNRESOLVED,
        reason_code=ReasonCode.UNRESOLVED_POSITION,
        detail=detail,
    )


def _overall_status(capabilities: Iterable[CapabilityResult]) -> PositionSupportStatus:
    statuses = [capability.status for capability in capabilities]
    if statuses and all(status is CapabilityStatus.SUPPORTED for status in statuses):
        return PositionSupportStatus.SUPPORTED
    if any(status is CapabilityStatus.SUPPORTED for status in statuses):
        return PositionSupportStatus.PARTIALLY_SUPPORTED
    if any(status is CapabilityStatus.CONTEXT_ONLY for status in statuses):
        return PositionSupportStatus.CONTEXT_ONLY
    return PositionSupportStatus.UNRESOLVED


@dataclass(frozen=True, slots=True)
class ClassifiedPosition:
    """A raw position with independent support labels for every downstream capability."""

    raw_position: RawPosition
    overall_status: PositionSupportStatus
    valuation: CapabilityResult
    risk: CapabilityResult
    fixed_income: CapabilityResult
    permission: CapabilityResult
    execution: CapabilityResult

    @property
    def identity(self) -> str:
        return self.raw_position.identity

    @property
    def asset(self):
        return self.raw_position.asset

    @property
    def raw_amount(self) -> int | None:
        return self.raw_position.raw_amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "raw_position": self.raw_position.to_dict(),
            "overall_status": self.overall_status.value,
            "capabilities": {
                "valuation": self.valuation.to_dict(),
                "risk": self.risk.to_dict(),
                "fixed_income": self.fixed_income.to_dict(),
                "permission": self.permission.to_dict(),
                "execution": self.execution.to_dict(),
            },
        }


def classify_position(
    position: RawPosition,
    *,
    permission_gate: GateStatus | None = None,
) -> ClassifiedPosition:
    """Classify a registered position without promoting absent evidence to support."""

    if position.unresolved:
        unresolved = _unresolved_capability("balance was not obtained from the read-only adapter")
        return ClassifiedPosition(
            raw_position=position,
            overall_status=PositionSupportStatus.UNRESOLVED,
            valuation=unresolved,
            risk=unresolved,
            fixed_income=unresolved,
            permission=unresolved,
            execution=unresolved,
        )

    asset = position.asset
    permission = (
        capability_from_gate(permission_gate)
        if permission_gate is not None
        else _configured_capability(asset.permission_support, name="permission")
    )
    valuation = _configured_capability(asset.valuation_support, name="valuation")
    risk = _configured_capability(asset.risk_support, name="risk")
    fixed_income = _configured_capability(asset.fixed_income_support, name="fixed-income")
    execution = _configured_capability(asset.execution_support, name="execution")
    capabilities = (valuation, risk, fixed_income, permission, execution)
    return ClassifiedPosition(
        raw_position=position,
        overall_status=_overall_status(capabilities),
        valuation=valuation,
        risk=risk,
        fixed_income=fixed_income,
        permission=permission,
        execution=execution,
    )


def classify_snapshot(
    snapshot: WalletSnapshot,
    *,
    permission_gates: dict[str, GateStatus] | None = None,
) -> tuple[ClassifiedPosition, ...]:
    """Classify all visible positions in canonical identity order."""

    # This returns registered positions only, and `value_portfolio(classify_snapshot(...))` is
    # an exported composition somebody will write. Removing this refusal on the grounds that
    # classification produces no total missed that the composition does: a snapshot with a
    # confirmed unregistered holding came back with an authoritative NAV that excluded it.
    # `value_wallet` reads a snapshot end to end and classifies position by position, so it is
    # the way past this rather than a reason to drop it.
    if getattr(snapshot, "enumerated", ()):
        raise PortfolioContractError(
            "this snapshot carries confirmed holdings outside the registry; value it with "
            "value_wallet, which counts them, rather than classifying to positions that cannot"
        )
    if not getattr(snapshot, "enumeration_complete", True):
        raise PortfolioContractError(
            "this snapshot's search outside the registry did not finish, so the registry is not "
            "known to be the whole wallet; value it with value_wallet"
        )
    permission_gates = permission_gates or {}
    classified = tuple(
        classify_position(position, permission_gate=permission_gates.get(position.identity))
        for position in snapshot.positions
    )
    identities = [position.identity for position in classified]
    if identities != sorted(identities):
        raise PortfolioContractError("WalletSnapshot positions must be canonical before classification")
    return classified
