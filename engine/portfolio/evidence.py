"""Versioned decision/outcome evidence and idempotent execution reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from engine.manifest import MANIFEST_SCHEMA

from .contracts import (
    DECISION_BUNDLE_SCHEMA_VERSION,
    OUTCOME_BUNDLE_SCHEMA_VERSION,
    CanonicalizationError,
    PortfolioContractError,
    canonical_data,
    canonical_hash,
    canonical_json_bytes,
    require_utc,
    utc_iso,
)
from .opportunities import CostLedger


class ExecutionState(str, Enum):
    QUOTED = "QUOTED"
    PASSPORT_READY = "PASSPORT_READY"
    USER_SIGNED = "USER_SIGNED"
    SOURCE_PENDING = "SOURCE_PENDING"
    BRIDGED = "BRIDGED"
    DESTINATION_PENDING = "DESTINATION_PENDING"
    SETTLED = "SETTLED"
    FAILED_DESTINATION = "FAILED_DESTINATION"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    EXPIRED = "EXPIRED"


ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.QUOTED: frozenset({ExecutionState.PASSPORT_READY, ExecutionState.EXPIRED}),
    ExecutionState.PASSPORT_READY: frozenset({ExecutionState.USER_SIGNED, ExecutionState.EXPIRED}),
    ExecutionState.USER_SIGNED: frozenset({ExecutionState.SOURCE_PENDING, ExecutionState.EXPIRED}),
    ExecutionState.SOURCE_PENDING: frozenset(
        {
            ExecutionState.BRIDGED,
            # A same-chain swap has no bridge leg and never passes through BRIDGED or
            # DESTINATION_PENDING, so without this it can never reach a terminal state at all:
            # the router reports the transfer complete while the record sits in SOURCE_PENDING
            # and the reconciliation is refused as an invalid transition. Every route this app
            # quotes today is same-chain. A cross-chain route is unaffected, because the router
            # does not call a transfer complete while only its source leg has landed.
            ExecutionState.SETTLED,
            ExecutionState.FAILED_DESTINATION,
            ExecutionState.REFUND_PENDING,
            ExecutionState.EXPIRED,
        }
    ),
    ExecutionState.BRIDGED: frozenset(
        {
            ExecutionState.DESTINATION_PENDING,
            ExecutionState.FAILED_DESTINATION,
            ExecutionState.REFUND_PENDING,
        }
    ),
    ExecutionState.DESTINATION_PENDING: frozenset(
        {
            ExecutionState.SETTLED,
            ExecutionState.FAILED_DESTINATION,
            ExecutionState.REFUND_PENDING,
        }
    ),
    # Failure remains reconcilable because it may later enter the refund path.
    ExecutionState.FAILED_DESTINATION: frozenset({ExecutionState.REFUND_PENDING}),
    ExecutionState.REFUND_PENDING: frozenset({ExecutionState.REFUNDED}),
    ExecutionState.SETTLED: frozenset(),
    ExecutionState.REFUNDED: frozenset(),
    ExecutionState.EXPIRED: frozenset(),
}
OUTCOME_WRITABLE_STATES = frozenset(
    {
        ExecutionState.SETTLED,
        ExecutionState.REFUNDED,
        ExecutionState.EXPIRED,
    }
)


class ReconciliationError(PortfolioContractError):
    """Raised when lifecycle evidence would become contradictory or mutable."""


def _canonical_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonical-sort an unordered evidence collection without weakening validation."""

    normalized = [canonical_data(row) for row in rows]
    return sorted(normalized, key=canonical_json_bytes)


@dataclass(frozen=True, slots=True)
class DecisionBundle:
    """Off-chain DecisionBundle/v2, hashed with the existing Keccak implementation."""

    method_version: str
    as_of: datetime
    portfolio_commitment: str
    objective: str
    transformation: Mapping[str, Any]
    expires_at: datetime
    costs: CostLedger | None = None
    risk_results: Mapping[str, Any] = field(default_factory=dict)
    mandate_result: Mapping[str, Any] | None = None
    opportunity_result: Mapping[str, Any] | None = None
    evidence: tuple[Mapping[str, Any], ...] = ()
    legacy_ref: str | None = None
    schema_version: str = DECISION_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_BUNDLE_SCHEMA_VERSION:
            raise PortfolioContractError("DecisionBundle must use DecisionBundle/v2")
        if not self.method_version or not self.portfolio_commitment or not self.objective:
            raise PortfolioContractError("decision bundle version, commitment, and objective are required")
        require_utc(self.as_of, field_name="DecisionBundle.as_of")
        require_utc(self.expires_at, field_name="DecisionBundle.expires_at")
        if self.expires_at <= self.as_of:
            raise PortfolioContractError("decision bundle expiry must follow its as_of timestamp")
        if self.costs is not None and not isinstance(self.costs, CostLedger):
            raise PortfolioContractError("DecisionBundle.costs must be a CostLedger or null")
        canonical_data(self.transformation)
        canonical_data(self.risk_results)
        canonical_data(self.mandate_result)
        canonical_data(self.opportunity_result)
        _canonical_rows(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        risk_results = {
            key: canonical_data(value)
            for key, value in sorted(self.risk_results.items(), key=lambda item: item[0])
        }
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="DecisionBundle.as_of"),
            "portfolio_commitment": self.portfolio_commitment,
            "objective": self.objective,
            "transformation": canonical_data(self.transformation),
            "costs": None if self.costs is None else self.costs.to_dict(),
            "risk_results": risk_results,
            "mandate_result": canonical_data(self.mandate_result),
            "opportunity_result": canonical_data(self.opportunity_result),
            "evidence": _canonical_rows(self.evidence),
            "expires_at": utc_iso(self.expires_at, field_name="DecisionBundle.expires_at"),
            "legacy_ref": self.legacy_ref,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def decision_hash(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class OutcomeBundle:
    """Write-once OutcomeBundle/v1 for a fully reconciled terminal lifecycle state."""

    decision_hash: str
    execution_id: str
    state: ExecutionState
    method_version: str
    as_of: datetime
    lifi_transaction_id: str | None = None
    source_tx: str | None = None
    destination_tx: str | None = None
    realized_costs: CostLedger | None = None
    realized_position: Mapping[str, Any] | None = None
    expected_vs_realized: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = OUTCOME_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OUTCOME_BUNDLE_SCHEMA_VERSION:
            raise PortfolioContractError("OutcomeBundle must use OutcomeBundle/v1")
        if not self.decision_hash.startswith("0x") or not self.execution_id or not self.method_version:
            raise PortfolioContractError("outcome needs decision hash, execution identity, and method version")
        require_utc(self.as_of, field_name="OutcomeBundle.as_of")
        if not isinstance(self.state, ExecutionState):
            raise PortfolioContractError("OutcomeBundle.state must be an ExecutionState")
        if self.state not in OUTCOME_WRITABLE_STATES:
            raise PortfolioContractError("outcome bundles can only be written for terminal states")
        if self.realized_costs is not None and not isinstance(self.realized_costs, CostLedger):
            raise PortfolioContractError("OutcomeBundle.realized_costs must be a CostLedger or null")
        for name in ("lifi_transaction_id", "source_tx", "destination_tx"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise PortfolioContractError(f"OutcomeBundle.{name} must be a string or null")
        canonical_data(self.realized_position)
        canonical_data(self.expected_vs_realized)
        _canonical_rows(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "method_version": self.method_version,
            "as_of": utc_iso(self.as_of, field_name="OutcomeBundle.as_of"),
            "decision_hash": self.decision_hash.lower(),
            "execution_id": self.execution_id,
            "state": self.state.value,
            "lifi_transaction_id": self.lifi_transaction_id,
            "source_tx": self.source_tx,
            "destination_tx": self.destination_tx,
            "realized_costs": None if self.realized_costs is None else self.realized_costs.to_dict(),
            "realized_position": canonical_data(self.realized_position),
            "expected_vs_realized": canonical_data(self.expected_vs_realized),
            "evidence": _canonical_rows(self.evidence),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def outcome_hash(self) -> str:
        return canonical_hash(self.to_dict())

    @property
    def realized_ref(self) -> str:
        """Digest suitable for the legacy ledger's write-once realized reference."""

        return self.outcome_hash


@dataclass(frozen=True, slots=True)
class LegacyDecisionRead:
    """Read-only normalized view of an older DecisionManifest."""

    schema_version: str
    identity: str
    verdict: str | None
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "verdict": self.verdict,
            "payload": canonical_data(self.payload),
        }


class LegacyBundleRegistry:
    """Registry of readable legacy schemas; it never upgrades or mutates old bytes."""

    def __init__(self) -> None:
        self._readers = {MANIFEST_SCHEMA: self._read_par_manifest}

    def register(self, schema_version: str, reader) -> None:
        if not schema_version or schema_version in self._readers:
            raise PortfolioContractError("legacy schema registration must be unique")
        self._readers[schema_version] = reader

    def read(self, payload: Mapping[str, Any]) -> LegacyDecisionRead:
        schema = payload.get("schema") or payload.get("schema_version")
        if not isinstance(schema, str) or schema not in self._readers:
            raise PortfolioContractError(f"no legacy read model is registered for {schema!r}")
        # Validate the float prohibition at the boundary even for a legacy read
        # representation.  This does not recompute or mutate the old manifest.
        canonical_data(payload)
        return self._readers[schema](payload)

    @staticmethod
    def _read_par_manifest(payload: Mapping[str, Any]) -> LegacyDecisionRead:
        identity = payload.get("identity")
        decision = payload.get("decision")
        if not isinstance(identity, Mapping) or not isinstance(decision, Mapping):
            raise PortfolioContractError("legacy par manifest lacks identity or decision object")
        chain = identity.get("chain_id")
        instrument = identity.get("instrument")
        if not isinstance(chain, int) or not isinstance(instrument, str):
            raise PortfolioContractError("legacy par manifest has no stable instrument identity")
        return LegacyDecisionRead(
            schema_version=MANIFEST_SCHEMA,
            identity=f"eip155:{chain}/legacy:{instrument.lower()}",
            verdict=decision.get("verdict") if isinstance(decision.get("verdict"), str) else None,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class ReconciliationUpdate:
    """One idempotent observation from a status poll or locally recorded receipt."""

    decision_hash: str
    state: ExecutionState
    as_of: datetime
    lifi_transaction_id: str | None = None
    source_tx: str | None = None
    destination_tx: str | None = None
    outcome: OutcomeBundle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_hash, str) or not self.decision_hash.startswith("0x"):
            raise PortfolioContractError("reconciliation decision_hash must be a 0x-prefixed hash")
        require_utc(self.as_of, field_name="ReconciliationUpdate.as_of")
        if not isinstance(self.state, ExecutionState):
            raise PortfolioContractError("ReconciliationUpdate.state must be an ExecutionState")
        for name in ("lifi_transaction_id", "source_tx", "destination_tx"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise PortfolioContractError(f"ReconciliationUpdate.{name} must be a string or null")
        if self.outcome is not None:
            if self.outcome.decision_hash.lower() != self.decision_hash.lower():
                raise PortfolioContractError("outcome decision hash does not match reconciliation key")
            if self.outcome.state is not self.state:
                raise PortfolioContractError("outcome state does not match reconciliation state")


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """Serializable record that can seed a fresh store after a process restart."""

    decision_hash: str
    state: ExecutionState
    as_of: datetime
    lifi_transaction_id: str | None = None
    source_tx: str | None = None
    destination_tx: str | None = None
    outcome: OutcomeBundle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ExecutionState):
            raise PortfolioContractError("ReconciliationRecord.state must be an ExecutionState")
        require_utc(self.as_of, field_name="ReconciliationRecord.as_of")
        if self.outcome is not None and self.outcome.decision_hash.lower() != self.decision_hash.lower():
            raise PortfolioContractError("record outcome does not match its decision hash")
        for name in ("lifi_transaction_id", "source_tx", "destination_tx"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise PortfolioContractError(f"ReconciliationRecord.{name} must be a string or null")

    @property
    def outcome_written(self) -> bool:
        return self.outcome is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_hash": self.decision_hash,
            "state": self.state.value,
            "as_of": utc_iso(self.as_of, field_name="ReconciliationRecord.as_of"),
            "lifi_transaction_id": self.lifi_transaction_id,
            "source_tx": self.source_tx,
            "destination_tx": self.destination_tx,
            "outcome": None if self.outcome is None else self.outcome.to_dict(),
        }


class ReconciliationStore:
    """Small deterministic in-memory reconciliation projection.

    Persistence belongs to a later platform adapter.  Passing previous records back
    into this constructor provides restart/replay semantics without duplicating a
    terminal outcome.
    """

    def __init__(self, records: Iterable[ReconciliationRecord] = ()) -> None:
        self._records: dict[str, ReconciliationRecord] = {}
        self._identifiers: dict[tuple[str, str], str] = {}
        for record in records:
            key = record.decision_hash.lower()
            if key in self._records:
                raise ReconciliationError(f"duplicate decision record {record.decision_hash}")
            self._records[key] = record
            self._index_record(record)

    @staticmethod
    def _identifier_items(record: ReconciliationRecord | ReconciliationUpdate) -> tuple[tuple[str, str], ...]:
        values = (
            ("decision_hash", record.decision_hash.lower()),
            ("lifi_transaction_id", record.lifi_transaction_id),
            ("source_tx", record.source_tx),
            ("destination_tx", record.destination_tx),
        )
        return tuple((kind, value.lower()) for kind, value in values if value)

    def _index_record(self, record: ReconciliationRecord) -> None:
        decision = record.decision_hash.lower()
        items = self._identifier_items(record)
        for item in items:
            prior = self._identifiers.get(item)
            if prior is not None and prior != decision:
                raise ReconciliationError(
                    f"reconciliation identifier {item[0]}={item[1]} belongs to another decision"
                )
        for item in items:
            self._identifiers[item] = decision

    @staticmethod
    def _merged_identifier(old: str | None, new: str | None, *, name: str) -> str | None:
        if old is not None and new is not None and old.lower() != new.lower():
            raise ReconciliationError(f"conflicting {name} for one decision")
        return old if old is not None else new

    def reconcile(self, update: ReconciliationUpdate) -> ReconciliationRecord:
        """Apply an observation once; replaying identical evidence returns the stored record."""

        decision = update.decision_hash.lower()
        record = self._records.get(decision)
        if record is None:
            if update.state is not ExecutionState.QUOTED:
                raise ReconciliationError("a new lifecycle must begin at QUOTED")
            record = ReconciliationRecord(
                decision_hash=update.decision_hash.lower(),
                state=update.state,
                as_of=update.as_of,
                lifi_transaction_id=update.lifi_transaction_id,
                source_tx=update.source_tx,
                destination_tx=update.destination_tx,
                outcome=update.outcome,
            )
            if update.outcome is not None:
                raise ReconciliationError("a lifecycle cannot write an outcome before leaving QUOTED")
            self._index_record(record)
            self._records[decision] = record
            return record

        # Check all route identifiers before changing state.  This is the idempotency
        # key union: decision hash, LI.FI transaction ID, source tx, destination tx.
        for identifier in self._identifier_items(update):
            existing_decision = self._identifiers.get(identifier)
            if existing_decision is not None and existing_decision != decision:
                raise ReconciliationError(
                    f"reconciliation identifier {identifier[0]}={identifier[1]} belongs to another decision"
                )
        if record.outcome_written:
            if (
                update.state is record.state
                and update.outcome is not None
                and update.outcome.canonical_bytes() == record.outcome.canonical_bytes()
            ):
                return record
            raise ReconciliationError(
                "OUTCOME_ALREADY_WRITTEN: terminal outcome is immutable and cannot be duplicated"
            )

        merged = ReconciliationRecord(
            decision_hash=record.decision_hash,
            state=record.state,
            as_of=record.as_of,
            lifi_transaction_id=self._merged_identifier(
                record.lifi_transaction_id, update.lifi_transaction_id, name="LI.FI transaction ID"
            ),
            source_tx=self._merged_identifier(record.source_tx, update.source_tx, name="source transaction"),
            destination_tx=self._merged_identifier(
                record.destination_tx, update.destination_tx, name="destination transaction"
            ),
            outcome=None,
        )
        if update.state is record.state:
            # A repeated poll with no new evidence is not a new observation of
            # execution progress. Preserve the record, including its timestamp.
            if merged == record and update.outcome is None:
                return record
            next_record = replace(merged, as_of=max(require_utc(record.as_of), require_utc(update.as_of)))
        else:
            if update.state not in ALLOWED_TRANSITIONS[record.state]:
                raise ReconciliationError(
                    f"INVALID_TRANSITION: {record.state.value} -> {update.state.value} is not allowed"
                )
            next_record = replace(merged, state=update.state, as_of=update.as_of)

        if update.outcome is not None:
            if next_record.state not in OUTCOME_WRITABLE_STATES:
                raise ReconciliationError("outcome write requires a terminal lifecycle state")
            next_record = replace(next_record, outcome=update.outcome)
        self._index_record(next_record)
        self._records[decision] = next_record
        return next_record

    def get(self, decision_hash: str) -> ReconciliationRecord | None:
        return self._records.get(decision_hash.lower())

    def records(self) -> tuple[ReconciliationRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda row: row.decision_hash))


def decision_bundle_hash(bundle: DecisionBundle) -> str:
    return bundle.decision_hash


def outcome_bundle_hash(bundle: OutcomeBundle) -> str:
    return bundle.outcome_hash
