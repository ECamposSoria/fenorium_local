from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


GENESIS_HASH = "0" * 64
ACTIONS = {"BID", "NO BID", "LEND"}


class AppendOnlyViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class DecisionRecord:
    timestamp: datetime
    instrument: str
    action: str
    forecast_net_yield: float | None
    forecast_slippage: float | None
    forecast_executable_capacity: float | None
    reason: str
    decision_id: str = ""


@dataclass(frozen=True)
class RealizedRecord:
    timestamp: datetime
    decision_id: str
    realized_net_yield: float | None
    realized_slippage: float | None
    realized_capacity: float | None
    note: str


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash(previous_hash: str, event: dict[str, Any]) -> str:
    return hashlib.sha256(previous_hash.encode("ascii") + b"\n" + _canonical(event)).hexdigest()


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read_verified(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        previous = GENESIS_HASH
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AppendOnlyViolation(f"cannot read ledger {self.path}: {exc}") from exc
        for number, line in enumerate(lines, 1):
            try:
                envelope = json.loads(line)
                event = envelope["event"]
                recorded_previous = envelope["previous_hash"]
                recorded_hash = envelope["hash"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise AppendOnlyViolation(f"ledger line {number} is malformed") from exc
            if recorded_previous != previous or recorded_hash != _hash(previous, event):
                raise AppendOnlyViolation(f"ledger hash chain failed at line {number}; history was modified")
            previous = recorded_hash
            events.append(event)
        return events

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            events = self._read_verified()
            decision_ids = {row["decision_id"] for row in events if row.get("event_type") == "decision"}
            realized_ids = {row["decision_id"] for row in events if row.get("event_type") == "realized"}
            identifier = event.get("decision_id")
            if event.get("event_type") == "decision" and identifier in decision_ids:
                raise AppendOnlyViolation(f"decision {identifier} already exists; historical records cannot be replaced")
            if event.get("event_type") == "realized":
                if identifier not in decision_ids:
                    raise AppendOnlyViolation(f"realization references unknown decision {identifier}")
                if identifier in realized_ids:
                    raise AppendOnlyViolation(f"decision {identifier} already has a realization; append a new decision instead")
            previous = GENESIS_HASH
            if self.path.stat().st_size:
                previous = json.loads(self.path.read_text(encoding="utf-8").splitlines()[-1])["hash"]
            envelope = {"previous_hash": previous, "event": event, "hash": _hash(previous, event)}
            os.write(descriptor, _canonical(envelope) + b"\n")
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def append_decision(self, record: DecisionRecord) -> str:
        if record.timestamp.tzinfo is None:
            raise ValueError("decision timestamp must be timezone-aware")
        if record.action not in ACTIONS:
            raise ValueError(f"action must be one of {sorted(ACTIONS)}")
        for name, value in (("forecast_net_yield", record.forecast_net_yield),
                            ("forecast_slippage", record.forecast_slippage),
                            ("forecast_executable_capacity", record.forecast_executable_capacity)):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite or unavailable")
        if record.forecast_executable_capacity is not None and record.forecast_executable_capacity < 0:
            raise ValueError("forecast_executable_capacity cannot be negative")
        identifier = record.decision_id or uuid4().hex
        event = asdict(record)
        event.update({"event_type": "decision", "decision_id": identifier,
                      "timestamp": record.timestamp.astimezone(UTC).isoformat()})
        self._append(event)
        return identifier

    def append_realized(self, record: RealizedRecord) -> None:
        if record.timestamp.tzinfo is None:
            raise ValueError("realized timestamp must be timezone-aware")
        events = self._read_verified()
        decision = next((row for row in events if row.get("event_type") == "decision"
                         and row.get("decision_id") == record.decision_id), None)
        if decision is None:
            raise AppendOnlyViolation(f"realization references unknown decision {record.decision_id}")
        event = asdict(record)
        event.update({
            "event_type": "realized", "timestamp": record.timestamp.astimezone(UTC).isoformat(),
            "net_yield_delta": _delta(record.realized_net_yield, decision.get("forecast_net_yield")),
            "slippage_delta": _delta(record.realized_slippage, decision.get("forecast_slippage")),
            "capacity_delta": _delta(record.realized_capacity, decision.get("forecast_executable_capacity")),
        })
        self._append(event)

    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._read_verified())


def _delta(realized: float | None, forecast: float | None) -> float | None:
    if realized is None or forecast is None:
        return None
    if not math.isfinite(realized) or not math.isfinite(forecast):
        raise ValueError("forecast and realized values must be finite")
    return realized - forecast


def render_table(events: Iterable[dict[str, Any]]) -> str:
    decisions: dict[str, dict[str, Any]] = {}
    realized: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") == "decision":
            decisions[str(event["decision_id"])] = event
        elif event.get("event_type") == "realized":
            realized[str(event["decision_id"])] = event
    header = ("timestamp | instrument | action | forecast net | realized net | delta | "
              "forecast slip | realized slip | forecast capacity | realized capacity | reason")
    lines = [header, "--- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---"]
    for identifier, decision in decisions.items():
        outcome = realized.get(identifier, {})
        lines.append(" | ".join((
            str(decision.get("timestamp")), str(decision.get("instrument")), str(decision.get("action")),
            _fmt(decision.get("forecast_net_yield")), _fmt(outcome.get("realized_net_yield")),
            _fmt(outcome.get("net_yield_delta")), _fmt(decision.get("forecast_slippage")),
            _fmt(outcome.get("realized_slippage")), _fmt(decision.get("forecast_executable_capacity")),
            _fmt(outcome.get("realized_capacity")), str(decision.get("reason", "")),
        )))
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return "unavailable" if value is None else f"{value:.6f}" if isinstance(value, float) else str(value)
