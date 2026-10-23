"""Every route this server builds, what happened to it, and the fee it should have earned.

A route record is written whenever `/v1/execution-routes` answers about a purchase, ready or not,
and then moves through the purchase's lifecycle:

    BUILT -> SUBMITTED -> SOURCE_CONFIRMED -> (BRIDGING -> DESTINATION_PURCHASE ->) COMPLETED
                                           \\-> FAILED (with the stage it failed at)
    REFUSED  (a route this server would not offer, with its reasons)

DISCOVER, QUOTE and VALIDATE SIZE happen before a record exists: the wallet read, the previews, and
the size check whose result the record keeps. Transitions only move forward; a late or repeated
observation of an earlier state is ignored rather than moving a record backwards.

The fee ledger has one entry per route that carries a Fenorium fee: EXPECTED when built, PENDING
once submitted, then RECONCILED (the fee was seen arriving at the recipient on chain), MISMATCH
(something else arrived), UNVERIFIED (the chain cannot show it) or VOID (the source transaction
failed, so nothing was charged). The recipient's address receives fees whether or not a contract is
deployed there yet, so nothing here depends on that.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterable, Mapping
import uuid


ROUTE_SCHEMA = "fenorium/route-record/1"
ACTIVE_STATES = ("SUBMITTED", "SOURCE_CONFIRMED", "BRIDGING", "DESTINATION_PURCHASE")
TERMINAL_STATES = ("COMPLETED", "FAILED", "REFUSED")
_NEXT: dict[str, frozenset[str]] = {
    "BUILT": frozenset({"SUBMITTED"}),
    "SUBMITTED": frozenset({"SOURCE_CONFIRMED", "FAILED"}),
    "SOURCE_CONFIRMED": frozenset({"BRIDGING", "DESTINATION_PURCHASE", "COMPLETED", "FAILED"}),
    "BRIDGING": frozenset({"DESTINATION_PURCHASE", "COMPLETED", "FAILED"}),
    "DESTINATION_PURCHASE": frozenset({"COMPLETED", "FAILED"}),
}
FEE_STATES = ("EXPECTED", "PENDING", "RECONCILED", "MISMATCH", "UNVERIFIED", "VOID")


class RouteStateError(ValueError):
    """A request to move a route somewhere its lifecycle does not allow."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RouteStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    wallet TEXT NOT NULL, state TEXT NOT NULL, failed_stage TEXT, failure_reason TEXT,
                    chain_id INTEGER NOT NULL, instrument_address TEXT, instrument_id TEXT, venue TEXT,
                    direction TEXT NOT NULL DEFAULT 'BUY',
                    pay_chain_id INTEGER NOT NULL, pay_token TEXT, amount_raw TEXT NOT NULL,
                    cross_chain INTEGER NOT NULL DEFAULT 0, build_status TEXT NOT NULL, reason_codes TEXT NOT NULL,
                    notional_usd_micros INTEGER, route_cost_bps INTEGER, tool TEXT, to_amount_min TEXT,
                    expires_at INTEGER, tx_target TEXT, tx_data_sha256 TEXT, tx_value TEXT,
                    fee_usd_micros INTEGER, lifi_fee_usd_micros INTEGER,
                    tx_hash TEXT, tx_verified INTEGER NOT NULL DEFAULT 0, approval_tx_hash TEXT, submitted_at TEXT, source_block INTEGER,
                    destination_tx TEXT, received TEXT, completed_at TEXT, last_checked_at TEXT,
                    checks INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS routes_by_wallet ON routes (wallet, created_at);
                CREATE INDEX IF NOT EXISTS routes_by_state ON routes (state);
                CREATE TABLE IF NOT EXISTS route_events (
                    route_id TEXT NOT NULL, at TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS route_events_by_route ON route_events (route_id, at);
                CREATE TABLE IF NOT EXISTS fee_ledger (
                    route_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, chain_id INTEGER NOT NULL, payer TEXT NOT NULL, token TEXT NOT NULL,
                    token_decimals INTEGER, expected_raw TEXT NOT NULL, expected_usd_micros INTEGER,
                    recipient TEXT NOT NULL, recipient_deployed INTEGER NOT NULL, tx_hash TEXT,
                    observed_raw TEXT, method TEXT, detail TEXT);
                CREATE INDEX IF NOT EXISTS fee_ledger_by_status ON fee_ledger (status);
            """)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(routes)")}
            for name, definition in (("tx_data_sha256", "TEXT"), ("tx_value", "TEXT"),
                                     ("tx_verified", "INTEGER NOT NULL DEFAULT 0"),
                                     ("direction", "TEXT NOT NULL DEFAULT 'BUY'")):
                if name not in columns:
                    self._db.execute(f"ALTER TABLE routes ADD COLUMN {name} {definition}")

            with _Transaction(self._db):
                self._db.execute("DROP INDEX IF EXISTS routes_by_tx")
                self._db.execute("CREATE INDEX routes_by_tx ON routes (tx_hash)")
                self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS routes_by_verified_tx "
                                 "ON routes (tx_hash) WHERE tx_verified = 1")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writes ---------------------------------------------------------------------------------

    def record_build(self, *, wallet: str, response: Mapping[str, Any], chain_id: int,
                     instrument_address: str | None, instrument_id: str | None, venue: str | None,
                     pay_chain_id: int, pay_token: str | None, amount_raw: int, tx_target: str | None,
                     fee: Mapping[str, Any] | None, fee_recipient: str | None,
                     recipient_deployed: bool, tx_data: str | None = None,
                     tx_value: int | str | None = None, direction: str = "BUY") -> str:
        """Record one answer of the route endpoint and return its route id."""
        now = _iso(self.clock())
        route = response.get("route") or {}
        size = response.get("size_check") or {}
        state = "BUILT" if response.get("status") == "ROUTE_READY" else "REFUSED"
        route_id = uuid.uuid4().hex
        evidence = {"route": route, "size_check": size, "fees": response.get("fees"),
                    "instrument": response.get("instrument")}
        if direction == "SELL":
            evidence.update(direction="SELL", balance_block=response.get("balance_block"),
                            receive_address=response.get("receive_address"),
                            receive_chain_id=response.get("receive_chain_id"), recipient=response.get("recipient"))
        with self._lock, _Transaction(self._db):
            self._db.execute("""
                INSERT INTO routes (route_id, created_at, updated_at, wallet, state, chain_id, instrument_address,
                    direction,
                    instrument_id, venue, pay_chain_id, pay_token, amount_raw, cross_chain, build_status,
                    reason_codes, notional_usd_micros, route_cost_bps, tool, to_amount_min, expires_at, tx_target,
                    fee_usd_micros, lifi_fee_usd_micros, evidence, tx_data_sha256, tx_value)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (route_id, now, now, wallet.lower(), state, chain_id, (instrument_address or "").lower() or None,
                  str(direction).upper(),
                  instrument_id, venue, pay_chain_id, (pay_token or "").lower() or None, str(amount_raw),
                  int(bool(route) and route.get("from_chain") != route.get("to_chain")),
                  str(response.get("status")), json.dumps(sorted(response.get("reason_codes") or [])),
                  size.get("notional_usd_micros"), size.get("route_cost_bps"), route.get("tool"),
                  route.get("to_amount_min"), route.get("expires_at"), (tx_target or "").lower() or None,
                  response.get("fenorium_fee_usd_micros"), response.get("lifi_fee_usd_micros"),
                  json.dumps(evidence, sort_keys=True),
                  hashlib.sha256(tx_data.lower().encode("ascii")).hexdigest() if tx_data is not None else None,
                  str(int(tx_value)) if tx_value is not None else None))
            self._event(route_id, state, {"reason_codes": sorted(response.get("reason_codes") or [])}, now)
            if state == "BUILT" and fee is not None and fee_recipient:
                self._db.execute("""
                    INSERT INTO fee_ledger (route_id, created_at, updated_at, status, chain_id, payer, token,
                        token_decimals, expected_raw, expected_usd_micros, recipient, recipient_deployed)
                    VALUES (?, ?, ?, 'EXPECTED', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (route_id, now, now, pay_chain_id, wallet.lower(), str(fee["token"]).lower(),
                      fee.get("token_decimals"), str(fee["amount_raw"]), fee.get("usd_micros"),
                      fee_recipient.lower(), int(recipient_deployed)))
        return route_id

    def submit(self, route_id: str, *, wallet: str, tx_hash: str, approval_tx_hash: str | None) -> dict[str, Any]:
        """The buyer broadcast the route's transaction. Repeating the same report changes nothing."""
        now = _iso(self.clock())
        with self._lock, _Transaction(self._db):
            row = self._row(route_id)
            if row is None:
                raise RouteStateError("ROUTE_NOT_FOUND", "no route with this id")
            if row["wallet"] != wallet.lower():
                raise RouteStateError("ROUTE_NOT_FOUND", "no route with this id for this wallet")
            if row["tx_hash"] == tx_hash.lower():
                return self._record(row)
            if row["state"] != "BUILT":
                raise RouteStateError("ROUTE_NOT_SUBMITTABLE", f"a {row['state']} route cannot be submitted")
            clash = self._db.execute("SELECT route_id FROM routes WHERE tx_hash = ? AND tx_verified = 1", (tx_hash.lower(),)).fetchone()
            if clash is not None:
                raise RouteStateError("TRANSACTION_ALREADY_CLAIMED", "this transaction belongs to another route")
            self._db.execute("""
                UPDATE routes SET state = 'SUBMITTED', tx_hash = ?, approval_tx_hash = ?, submitted_at = ?,
                    updated_at = ? WHERE route_id = ?
            """, (tx_hash.lower(), (approval_tx_hash or "").lower() or None, now, now, route_id))
            self._db.execute("UPDATE fee_ledger SET status = 'PENDING', tx_hash = ?, updated_at = ? "
                             "WHERE route_id = ? AND status = 'EXPECTED'", (tx_hash.lower(), now, route_id))
            self._event(route_id, "SUBMITTED", {"tx_hash": tx_hash.lower()}, now)
            return self._record(self._row(route_id))

    def advance(self, route_id: str, state: str, *, detail: Mapping[str, Any] | None = None,
                failed_stage: str | None = None, failure_reason: str | None = None,
                fields: Mapping[str, Any] | None = None) -> bool:
        """Move a route forward. Returns False for an invalid step or a failed transaction claim."""
        now = _iso(self.clock())
        allowed = {"source_block", "destination_tx", "received", "tx_verified"}
        with self._lock, _Transaction(self._db):
            row = self._row(route_id)
            if row is None or state not in _NEXT.get(row["state"], frozenset()):
                return False
            sets = {"state": state, "updated_at": now}
            for key, value in (fields or {}).items():
                if key not in allowed:
                    raise ValueError(f"{key} is not a route field the tracker sets")
                sets[key] = json.dumps(value, sort_keys=True) if key == "received" and value is not None else value
            if state == "FAILED":
                sets.update(failed_stage=failed_stage, failure_reason=failure_reason)
            if state in ("COMPLETED", "FAILED"):
                sets["completed_at"] = now
            assignments = ", ".join(f"{key} = ?" for key in sets)
            claimed = True
            try:
                self._db.execute(f"UPDATE routes SET {assignments} WHERE route_id = ?", (*sets.values(), route_id))
            except sqlite3.IntegrityError:
                if sets.get("tx_verified") != 1 or self._db.execute(
                        "SELECT 1 FROM routes WHERE tx_hash = ? AND tx_verified = 1 AND route_id != ?",
                        (row["tx_hash"], route_id)).fetchone() is None:
                    raise
                claimed = False
                state, failed_stage, failure_reason = "FAILED", "SOURCE", "TRANSACTION_ALREADY_CLAIMED"
                self._db.execute("UPDATE routes SET state = ?, failed_stage = ?, failure_reason = ?, "
                                 "updated_at = ?, completed_at = ? WHERE route_id = ?",
                                 (state, failed_stage, failure_reason, now, now, route_id))
            self._event(route_id, state, {**(detail or {}), **({"stage": failed_stage, "reason": failure_reason}
                                                                if state == "FAILED" else {})}, now)
            if state == "FAILED" and failed_stage == "SOURCE":
                if failure_reason in ("SOURCE_REVERTED", "SAFE_EXECUTION_FAILED", "SOURCE_TRANSACTION_MISMATCH",
                                      "TRANSACTION_ALREADY_CLAIMED"):
                    fee_status = "VOID"
                    fee_detail = "the source transaction failed, so nothing was charged"
                elif failure_reason == "SOURCE_NOT_MINED":
                    fee_status = "UNVERIFIED"
                    fee_detail = "the source transaction was not seen mined; whether a fee was paid is unknown"
                else:
                    fee_status = "UNVERIFIED"
                    fee_detail = "the source failure does not prove whether a fee was paid"
                self._db.execute("UPDATE fee_ledger SET status = ?, updated_at = ?, detail = ? "
                                 "WHERE route_id = ? AND status IN ('EXPECTED', 'PENDING')",
                                 (fee_status, now, fee_detail, route_id))
            elif state == "FAILED":
                self._db.execute("UPDATE fee_ledger SET status = 'UNVERIFIED', updated_at = ?, detail = ? "
                                 "WHERE route_id = ? AND status IN ('EXPECTED', 'PENDING')",
                                 (now, "tracking ended before the fee could be reconciled", route_id))
            return claimed

    def touch(self, route_id: str) -> None:
        with self._lock, _Transaction(self._db):
            self._db.execute("UPDATE routes SET last_checked_at = ?, checks = checks + 1 WHERE route_id = ?",
                             (_iso(self.clock()), route_id))

    def reconcile_fee(self, route_id: str, *, status: str, observed_raw: int | None, method: str,
                      detail: str | None = None) -> None:
        with self._lock, _Transaction(self._db):
            if status not in FEE_STATES:
                raise ValueError(f"unknown fee status {status}")
            self._db.execute("""
                UPDATE fee_ledger SET status = ?, observed_raw = ?, method = ?, detail = ?, updated_at = ?
                WHERE route_id = ? AND status IN ('EXPECTED', 'PENDING')
            """, (status, None if observed_raw is None else str(observed_raw), method, detail, _iso(self.clock()), route_id))

    def _event(self, route_id: str, state: str, detail: Mapping[str, Any], at: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO route_events VALUES (?, ?, ?, ?)",
                             (route_id, at, state, json.dumps(dict(detail), sort_keys=True)))

    # -- reads ----------------------------------------------------------------------------------

    def _row(self, route_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM routes WHERE route_id = ?", (route_id,)).fetchone()

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        with self._lock:
            record = dict(row)
            record["schema_version"] = ROUTE_SCHEMA
            record["cross_chain"] = bool(record["cross_chain"])
            record["reason_codes"] = json.loads(record["reason_codes"])
            record["evidence"] = json.loads(record["evidence"])
            record["received"] = json.loads(record["received"]) if record["received"] else None
            fee = self._db.execute("SELECT * FROM fee_ledger WHERE route_id = ?", (record["route_id"],)).fetchone()
            record["fee"] = None if fee is None else {**dict(fee), "recipient_deployed": bool(fee["recipient_deployed"])}
            return record

    def get(self, route_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._row(route_id)
            if row is None:
                return None
            record = self._record(row)
            record["events"] = [{"at": event["at"], "state": event["state"], "detail": json.loads(event["detail"])}
                                for event in self._db.execute(
                                    "SELECT * FROM route_events WHERE route_id = ? ORDER BY at, rowid", (route_id,))]
            return record

    def by_wallet(self, wallet: str, *, limit: int = 50,
                  exclude_states: tuple[str, ...] = (), verified_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            excluded = ""
            if exclude_states:
                placeholders = ",".join("?" for _ in exclude_states)
                excluded = f" AND state NOT IN ({placeholders})"
            verified = " AND tx_verified = 1" if verified_only else ""
            rows = self._db.execute(f"SELECT * FROM routes WHERE wallet = ?{excluded}{verified} ORDER BY created_at DESC LIMIT ?",
                                    (wallet.lower(), *exclude_states, limit))
            return [self._record(row) for row in rows]

    def active(self, *, limit: int = 200, states: tuple[str, ...] = ACTIVE_STATES) -> list[dict[str, Any]]:
        with self._lock:
            placeholders = ",".join("?" for _ in states)
            rows = self._db.execute(f"SELECT * FROM routes WHERE state IN ({placeholders}) "
                                    f"ORDER BY COALESCE(last_checked_at, ''), submitted_at LIMIT ?", (*states, limit))
            return [self._record(row) for row in rows]

    def in_flight(self) -> tuple[int, str | None]:
        """How many purchases are in flight and the earliest submission among them, over all of them."""
        with self._lock:
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            row = self._db.execute(f"SELECT COUNT(*) AS n, MIN(submitted_at) AS oldest FROM routes "
                                   f"WHERE state IN ({placeholders})", ACTIVE_STATES).fetchone()
            return row["n"], row["oldest"]

    def summary(self) -> dict[str, Any]:
        """What monitoring and fee analytics need: routes by state, failures by stage, money."""
        with self._lock:
            states = {row["state"]: row["n"] for row in self._db.execute(
                "SELECT state, COUNT(*) AS n FROM routes GROUP BY state")}
            stages = {row["failed_stage"] or "UNKNOWN": row["n"] for row in self._db.execute(
                "SELECT failed_stage, COUNT(*) AS n FROM routes WHERE state = 'FAILED' GROUP BY failed_stage")}
            refusals: dict[str, int] = {}
            for row in self._db.execute("SELECT reason_codes FROM routes WHERE state = 'REFUSED'"):
                for code in json.loads(row["reason_codes"]):
                    refusals[code] = refusals.get(code, 0) + 1
            money = self._db.execute("""
                SELECT COALESCE(SUM(CASE WHEN state = 'COMPLETED' THEN notional_usd_micros END), 0) AS completed_notional,
                       COALESCE(SUM(CASE WHEN state IN ('SUBMITTED','SOURCE_CONFIRMED','BRIDGING','DESTINATION_PURCHASE')
                                         THEN notional_usd_micros END), 0) AS in_flight_notional
                FROM routes""").fetchone()
            fees = {row["status"]: {"entries": row["n"], "expected_usd_micros": row["usd"] or 0}
                    for row in self._db.execute(
                        "SELECT status, COUNT(*) AS n, SUM(expected_usd_micros) AS usd FROM fee_ledger GROUP BY status")}
            return {"routes_by_state": states, "failures_by_stage": stages, "refusals_by_reason": refusals,
                    "completed_notional_usd_micros": money["completed_notional"],
                    "in_flight_notional_usd_micros": money["in_flight_notional"], "fees_by_status": fees}

    def fee_entries(self, *, status: Iterable[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            if status is None:
                rows = self._db.execute("SELECT * FROM fee_ledger ORDER BY created_at DESC LIMIT ?", (limit,))
            else:
                wanted = tuple(status)
                placeholders = ",".join("?" for _ in wanted)
                rows = self._db.execute(f"SELECT * FROM fee_ledger WHERE status IN ({placeholders}) "
                                        f"ORDER BY created_at DESC LIMIT ?", (*wanted, limit))
            return [{**dict(row), "recipient_deployed": bool(row["recipient_deployed"])} for row in rows]


class _Transaction:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __enter__(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind, value, traceback) -> None:
        self.db.execute("COMMIT" if kind is None else "ROLLBACK")
