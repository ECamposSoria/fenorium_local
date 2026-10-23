"""Strict, read-only LI.FI status parsing for reconciliation fixtures.

This module does not call LI.FI, build calldata, or advance a lifecycle on its
own. It converts the documented status/substatus vocabulary into a suggested
Fenorium state; ``ReconciliationStore`` remains responsible for rejecting an
out-of-order observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .contracts import PortfolioContractError, ReasonCode, require_utc, utc_iso
from .evidence import ExecutionState, ReconciliationUpdate


LIFI_STATUS_METHOD_VERSION = "lifi-status-api/official-doc-shape-2026-09-01"


class LifiTransferStatus(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


class LifiTransferSubstatus(str, Enum):
    WAIT_SOURCE_CONFIRMATIONS = "WAIT_SOURCE_CONFIRMATIONS"
    WAIT_DESTINATION_TRANSACTION = "WAIT_DESTINATION_TRANSACTION"
    BRIDGE_NOT_AVAILABLE = "BRIDGE_NOT_AVAILABLE"
    CHAIN_NOT_AVAILABLE = "CHAIN_NOT_AVAILABLE"
    REFUND_IN_PROGRESS = "REFUND_IN_PROGRESS"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    REFUNDED = "REFUNDED"


def _optional_text(value: Any, name: str, *, maximum: int = 512) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PortfolioContractError(f"{name} must be a bounded non-empty string or null")
    return value


def _transaction_hash(row: Any, name: str) -> str | None:
    if row is None:
        return None
    if not isinstance(row, Mapping):
        raise PortfolioContractError(f"LI.FI {name} must be an object or null")
    value = _optional_text(row.get("txHash"), f"LI.FI {name}.txHash", maximum=66)
    if value is None:
        return None
    if len(value) != 66 or not value.startswith("0x"):
        raise PortfolioContractError(f"LI.FI {name}.txHash must be a 32-byte transaction hash")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise PortfolioContractError(f"LI.FI {name}.txHash contains non-hex characters") from exc
    return value.lower()


def _suggested_state(
    status: LifiTransferStatus,
    substatus: LifiTransferSubstatus | None,
) -> ExecutionState | None:
    if status is LifiTransferStatus.PENDING:
        return {
            LifiTransferSubstatus.WAIT_SOURCE_CONFIRMATIONS: ExecutionState.SOURCE_PENDING,
            LifiTransferSubstatus.WAIT_DESTINATION_TRANSACTION: ExecutionState.DESTINATION_PENDING,
            LifiTransferSubstatus.REFUND_IN_PROGRESS: ExecutionState.REFUND_PENDING,
        }.get(substatus)
    if status is LifiTransferStatus.DONE:
        return {
            LifiTransferSubstatus.COMPLETED: ExecutionState.SETTLED,
            LifiTransferSubstatus.PARTIAL: ExecutionState.FAILED_DESTINATION,
            LifiTransferSubstatus.REFUNDED: ExecutionState.REFUNDED,
        }.get(substatus)
    if status is LifiTransferStatus.FAILED:
        return ExecutionState.FAILED_DESTINATION
    return None


@dataclass(frozen=True, slots=True)
class LifiStatusObservation:
    status: LifiTransferStatus
    substatus: LifiTransferSubstatus | None
    observed_at: datetime
    method_version: str
    suggested_state: ExecutionState | None
    reason_code: ReasonCode
    transaction_id: str | None = None
    source_tx: str | None = None
    destination_tx: str | None = None
    schema_version: str = "fenorium/lifi-status-observation/v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "observed_at": utc_iso(self.observed_at, field_name="LifiStatusObservation.observed_at"),
            "status": self.status.value,
            "substatus": None if self.substatus is None else self.substatus.value,
            "suggested_state": None if self.suggested_state is None else self.suggested_state.value,
            "reason_code": self.reason_code.value,
            "transaction_id": self.transaction_id,
            "source_tx": self.source_tx,
            "destination_tx": self.destination_tx,
        }


def parse_lifi_status(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime,
    method_version: str = LIFI_STATUS_METHOD_VERSION,
) -> LifiStatusObservation:
    """Parse one recorded response without treating a suggestion as authority."""

    if not isinstance(payload, Mapping):
        raise PortfolioContractError("LI.FI status payload must be an object")
    require_utc(observed_at, field_name="LI.FI status observed_at")
    if not isinstance(method_version, str) or not method_version:
        raise PortfolioContractError("LI.FI status method_version is required")
    try:
        status = LifiTransferStatus(payload.get("status"))
    except (TypeError, ValueError) as exc:
        raise PortfolioContractError("LI.FI status is missing or outside the frozen vocabulary") from exc
    raw_substatus = payload.get("substatus")
    try:
        substatus = None if raw_substatus is None else LifiTransferSubstatus(raw_substatus)
    except (TypeError, ValueError) as exc:
        raise PortfolioContractError("LI.FI substatus is outside the frozen vocabulary") from exc
    suggested = _suggested_state(status, substatus)
    return LifiStatusObservation(
        status=status,
        substatus=substatus,
        observed_at=observed_at,
        method_version=method_version,
        suggested_state=suggested,
        reason_code=(ReasonCode.COMPUTED if suggested is not None else ReasonCode.METHODOLOGY_NOT_APPLICABLE),
        transaction_id=_optional_text(payload.get("transactionId"), "LI.FI transactionId"),
        source_tx=_transaction_hash(payload.get("sending"), "sending"),
        destination_tx=_transaction_hash(payload.get("receiving"), "receiving"),
    )


_RECONCILIATION_STATES: dict[tuple[str, str | None], ExecutionState] = {
    (LifiTransferStatus.DONE.value, LifiTransferSubstatus.COMPLETED.value): ExecutionState.SETTLED,
    (
        LifiTransferStatus.PENDING.value,
        LifiTransferSubstatus.WAIT_DESTINATION_TRANSACTION.value,
    ): ExecutionState.DESTINATION_PENDING,
    (
        LifiTransferStatus.PENDING.value,
        LifiTransferSubstatus.WAIT_SOURCE_CONFIRMATIONS.value,
    ): ExecutionState.SOURCE_PENDING,
    (LifiTransferStatus.PENDING.value, LifiTransferSubstatus.REFUND_IN_PROGRESS.value): ExecutionState.REFUND_PENDING,
    # This mapping is deliberately local to the reconciliation adapter.  The
    # strict observation parser continues to expose PARTIAL as FAILED_DESTINATION.
    (LifiTransferStatus.DONE.value, LifiTransferSubstatus.PARTIAL.value): ExecutionState.REFUND_PENDING,
    (LifiTransferStatus.DONE.value, LifiTransferSubstatus.REFUNDED.value): ExecutionState.REFUNDED,
    (LifiTransferStatus.FAILED.value, None): ExecutionState.FAILED_DESTINATION,
    (LifiTransferStatus.FAILED.value, LifiTransferSubstatus.BRIDGE_NOT_AVAILABLE.value): ExecutionState.FAILED_DESTINATION,
    (LifiTransferStatus.FAILED.value, LifiTransferSubstatus.CHAIN_NOT_AVAILABLE.value): ExecutionState.FAILED_DESTINATION,
    (LifiTransferStatus.FAILED.value, LifiTransferSubstatus.UNKNOWN_ERROR.value): ExecutionState.FAILED_DESTINATION,
}


def _adapter_transaction_hash(row: Any) -> str | None:
    """Read a hash for the permissive adapter, omitting unusable evidence."""

    if not isinstance(row, Mapping):
        return None
    value = row.get("txHash")
    if not isinstance(value, str) or len(value) != 66:
        return None
    if value[:2] != "0x":
        return None
    if any(character not in "0123456789abcdefABCDEF" for character in value[2:]):
        return None
    return value.lower()


def _adapter_transaction_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    return value


def adapt_lifi_status(
    payload: Mapping[str, Any],
    *,
    decision_hash: str,
    as_of: datetime,
) -> ReconciliationUpdate | None:
    """Adapt one supplied LI.FI response into a lifecycle observation.

    This is a pure, fail-closed adapter.  It recognizes only the positive
    status/substatus pairs in ``_RECONCILIATION_STATES``; unknown vocabulary and
    LI.FI ``code`` 1003 produce no update.  Hash fields are optional evidence:
    malformed values are omitted rather than accepted or raised.  Lifecycle
    ordering and identifier ownership remain the responsibility of
    ``ReconciliationStore``.
    """

    if not isinstance(payload, Mapping) or payload.get("code") in (1003, "1003"):
        return None
    status = payload.get("status")
    substatus = payload.get("substatus")
    if not isinstance(status, str) or (substatus is not None and not isinstance(substatus, str)):
        return None
    state = _RECONCILIATION_STATES.get((status, substatus))
    if state is None:
        return None
    return ReconciliationUpdate(
        decision_hash=decision_hash,
        state=state,
        as_of=as_of,
        lifi_transaction_id=_adapter_transaction_id(payload.get("transactionId")),
        source_tx=_adapter_transaction_hash(payload.get("sending")),
        destination_tx=_adapter_transaction_hash(payload.get("receiving")),
    )
