"""Small durable store for wallet-owned Midnight lending orders."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping


# How long an expired draft stays replayable under its request key before it is forgotten.
DRAFT_RETENTION_SECONDS = 7 * 86_400


class OrderStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OrderStore:
    """Persist order terms and monotonically advancing local lifecycle status."""

    _STATUSES = frozenset({"UNSIGNED", "SIGNED"})

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("""CREATE TABLE IF NOT EXISTS lending_orders (
            root TEXT PRIMARY KEY, wallet TEXT NOT NULL, chain_id INTEGER NOT NULL,
            status TEXT NOT NULL, signature TEXT, record TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS lending_requests (
            request_key TEXT PRIMARY KEY, wallet TEXT NOT NULL, chain_id INTEGER NOT NULL,
            body_digest TEXT NOT NULL, root TEXT, created_at TEXT NOT NULL)""")
        # Old cancellation requests were unauthenticated; only chain facts can cancel a root.
        self._db.execute("UPDATE lending_orders SET status=CASE WHEN signature IS NULL THEN 'UNSIGNED' ELSE 'SIGNED' END "
                         "WHERE status='CANCEL_REQUESTED'")
        self._db.execute("CREATE INDEX IF NOT EXISTS lending_orders_by_wallet ON lending_orders(wallet, chain_id, created_at)")
        self._db.execute("""CREATE TABLE IF NOT EXISTS midnight_markets (
            chain_id INTEGER NOT NULL, market_id TEXT NOT NULL, loan_token TEXT NOT NULL,
            loan_decimals INTEGER, maturity_timestamp INTEGER, maturity TEXT, rate_bps INTEGER,
            name TEXT, last_seen_at TEXT NOT NULL, row TEXT NOT NULL,
            PRIMARY KEY(chain_id, market_id))""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS principal_tokens (
            chain_id INTEGER NOT NULL, address TEXT NOT NULL, venue TEXT NOT NULL,
            name TEXT, decimals INTEGER NOT NULL, maturity_timestamp INTEGER NOT NULL,
            market_address TEXT, accounting_asset_address TEXT, underlying_address TEXT,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            PRIMARY KEY(chain_id,address))""")

    @contextmanager
    def request_transaction(self):
        """Keep short store mutations atomic, including nested calls."""
        with self._lock:
            nested = self._db.in_transaction
            if not nested:
                self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                if not nested:
                    self._db.execute("COMMIT")
            except BaseException:
                if not nested:
                    self._db.execute("ROLLBACK")
                raise

    def reserve_request(self, key: str, wallet: str, chain_id: int, digest: str) -> str | None:
        with self.request_transaction():
            row = self._db.execute("SELECT * FROM lending_requests WHERE request_key=?", (key,)).fetchone()
            if row is not None:
                if (row["wallet"], row["chain_id"], row["body_digest"]) != (wallet.lower(), chain_id, digest):
                    raise OrderStoreError("IDEMPOTENCY_CONFLICT", "the idempotency key was already used for a different request")
                return row["root"]
            self._db.execute("INSERT INTO lending_requests(request_key,wallet,chain_id,body_digest,created_at) "
                             "VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (key, wallet.lower(), chain_id, digest))
            return None

    def prune_drafts(self, now: int) -> None:
        """Forget drafts a week past their expiry, bindings included.

        The week is for the request that made the draft: replayed under its key it keeps answering
        EXPIRED; pruned at expiry, the same stale retry would mint a fresh, fillable order.
        """
        with self.request_transaction():
            expired = ("SELECT root FROM lending_orders WHERE status='UNSIGNED' "
                       "AND json_extract(record, '$.expires_at')<=?")
            cutoff = (now - DRAFT_RETENTION_SECONDS,)
            self._db.execute("DELETE FROM lending_requests WHERE root IN (" + expired + ")", cutoff)
            self._db.execute("DELETE FROM lending_orders WHERE root IN (" + expired + ")", cutoff)

    def bind_request(self, key: str, root: str) -> None:
        with self.request_transaction():
            row = self._db.execute("SELECT root FROM lending_requests WHERE request_key=?", (key,)).fetchone()
            if row is None or row["root"] not in (None, root):
                raise OrderStoreError("IDEMPOTENCY_CONFLICT", "the request cannot be bound to another root")
            self._db.execute("UPDATE lending_requests SET root=? WHERE request_key=?", (root, key))

    def release_request(self, key: str) -> None:
        with self.request_transaction():
            self._db.execute("DELETE FROM lending_requests WHERE request_key=? AND root IS NULL", (key,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def remember_midnight_markets(self, chain_id: int, rows: list[Mapping[str, Any]],
                                  last_seen_at: str) -> None:
        """Keep the latest published Tenor metadata so matured markets remain readable."""
        prepared = []
        for row in rows:
            market_id = row.get("market_id")
            loan = row.get("loan_token")
            address = loan.get("address") if isinstance(loan, Mapping) else loan
            if not isinstance(market_id, str) or not isinstance(address, str):
                continue
            income = row.get("fixed_income") if isinstance(row.get("fixed_income"), Mapping) else {}
            maturity_timestamp = row.get("maturity_timestamp", income.get("maturity_timestamp"))
            maturity = row.get("maturity", income.get("maturity"))
            decimals = loan.get("decimals") if isinstance(loan, Mapping) else None
            # Keep malformed or out-of-range published values in the JSON snapshot, but do not
            # bind them into SQLite's narrower scalar columns.
            if type(maturity_timestamp) is not int or not -(2**63) <= maturity_timestamp < 2**63:
                maturity_timestamp = None
            if not isinstance(maturity, str):
                maturity = None
            if type(decimals) is not int or not 0 <= decimals <= 255:
                decimals = None
            rate_bps = row.get("rate_bps")
            if type(rate_bps) is not int or not -(2**63) <= rate_bps < 2**63:
                rate_bps = None
            name = row.get("name")
            if not isinstance(name, str):
                name = None
            prepared.append((int(chain_id), market_id.lower(), address.lower(), decimals,
                             maturity_timestamp, maturity, rate_bps, name,
                             str(last_seen_at), json.dumps(dict(row), sort_keys=True, separators=(",", ":"))))
        if not prepared:
            return
        with self._lock:
            self._db.executemany("""INSERT INTO midnight_markets
                (chain_id,market_id,loan_token,loan_decimals,maturity_timestamp,maturity,rate_bps,name,last_seen_at,row)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain_id,market_id) DO UPDATE SET
                loan_token=excluded.loan_token, loan_decimals=excluded.loan_decimals,
                maturity_timestamp=excluded.maturity_timestamp, maturity=excluded.maturity,
                rate_bps=excluded.rate_bps, name=excluded.name, last_seen_at=excluded.last_seen_at,
                row=excluded.row""", prepared)

    def midnight_markets(self, chain_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT row FROM midnight_markets WHERE chain_id=? ORDER BY market_id",
                                    (int(chain_id),)).fetchall()
        return [json.loads(row["row"]) for row in rows]

    def remember_principal_tokens(self, chain_id: int, rows: list[Mapping[str, Any]], seen_at: str) -> None:
        """Remember published Pendle and Spectra PT metadata, keeping its original venue and maturity."""
        prepared = []
        for row in rows:
            venue, address, decimals, maturity = (row.get("venue"), row.get("address"),
                                                   row.get("decimals"), row.get("maturity_timestamp"))
            if (venue not in {"Pendle", "Spectra"} or not isinstance(address, str)
                    or type(decimals) is not int or not 0 <= decimals <= 255
                    or type(maturity) is not int or not 0 <= maturity <= 253402300799):
                continue
            def addr(field):
                value = row.get(field)
                return value.lower() if isinstance(value, str) else None
            name = row.get("name")
            prepared.append((int(chain_id), address.lower(), venue, name if isinstance(name, str) else None,
                             decimals, maturity, addr("market_address"), addr("accounting_asset_address"),
                             addr("underlying_address"), str(seen_at), str(seen_at)))
        if not prepared:
            return
        with self._lock:
            self._db.executemany("""INSERT INTO principal_tokens
                (chain_id,address,venue,name,decimals,maturity_timestamp,market_address,
                 accounting_asset_address,underlying_address,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain_id,address) DO UPDATE SET
                name=COALESCE(excluded.name,principal_tokens.name),
                decimals=excluded.decimals, market_address=COALESCE(excluded.market_address,principal_tokens.market_address),
                accounting_asset_address=COALESCE(excluded.accounting_asset_address,principal_tokens.accounting_asset_address),
                underlying_address=COALESCE(excluded.underlying_address,principal_tokens.underlying_address),
                last_seen=excluded.last_seen""", prepared)

    @staticmethod
    def _principal_token(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def principal_tokens(self, chain_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM principal_tokens WHERE chain_id=? ORDER BY address",
                                    (int(chain_id),)).fetchall()
        return [dict(row) for row in rows]

    def principal_token(self, chain_id: int, address: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM principal_tokens WHERE chain_id=? AND address=?",
                                   (int(chain_id), str(address).lower())).fetchone()
        return self._principal_token(row)

    @staticmethod
    def _identity(root: str, wallet: str, chain_id: int) -> tuple[str, str, int]:
        return str(root).lower(), str(wallet).lower(), int(chain_id)

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = json.loads(row["record"])
        result.update({"root": row["root"], "wallet": row["wallet"], "chain_id": row["chain_id"],
                       "status": row["status"], "signature": json.loads(row["signature"]) if row["signature"] else None,
                       "created_at": row["created_at"], "updated_at": row["updated_at"]})
        return result

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        try:
            root, wallet, chain_id = self._identity(record["root"], record["wallet"], record["chain_id"])
            created = str(record["created_at"])
            payload = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderStoreError("ORDER_RECORD_INVALID", "lending order record is incomplete") from exc
        with self._lock:
            try:
                self._db.execute("INSERT INTO lending_orders(root,wallet,chain_id,status,signature,record,created_at,updated_at) "
                                 "VALUES(?,?,?,'UNSIGNED',NULL,?,?,?)", (root, wallet, chain_id, payload, created, created))
            except sqlite3.IntegrityError as exc:
                existing = self._db.execute("SELECT wallet,chain_id FROM lending_orders WHERE root=?", (root,)).fetchone()
                if existing and (existing["wallet"], existing["chain_id"]) == (wallet, chain_id):
                    return self._record(self._db.execute("SELECT * FROM lending_orders WHERE root=?", (root,)).fetchone())
                raise OrderStoreError("ORDER_ROOT_COLLISION", "this order root is already assigned") from exc
            return self._record(self._db.execute("SELECT * FROM lending_orders WHERE root=?", (root,)).fetchone())

    def get(self, root: str, wallet: str, chain_id: int) -> dict[str, Any] | None:
        root, wallet, chain_id = self._identity(root, wallet, chain_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM lending_orders WHERE root=? AND wallet=? AND chain_id=?",
                                   (root, wallet, chain_id)).fetchone()
        return self._record(row)

    def by_wallet(self, wallet: str, chain_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM lending_orders WHERE wallet=? AND chain_id=? ORDER BY created_at,root",
                                    (str(wallet).lower(), int(chain_id))).fetchall()
        return [self._record(row) for row in rows]

    def set_status(self, root: str, wallet: str, chain_id: int, status: str,
                   signature: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if status not in self._STATUSES:
            raise OrderStoreError("INVALID_ORDER_STATUS", "unsupported lending order status")
        root, wallet, chain_id = self._identity(root, wallet, chain_id)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM lending_orders WHERE root=? AND wallet=? AND chain_id=?",
                                       (root, wallet, chain_id)).fetchone()
                if row is None:
                    raise OrderStoreError("ORDER_NOT_FOUND", "no lending order with this root for this wallet")
                current = row["status"]
                # The write lock makes transition validation and update atomic across store instances.
                allowed = {"UNSIGNED": {"UNSIGNED", "SIGNED"}, "SIGNED": {"SIGNED"}}
                next_status = status if status in allowed[current] else current
                encoded_signature = row["signature"]
                if status in allowed[current] and signature is not None:
                    encoded_signature = json.dumps(dict(signature), sort_keys=True, separators=(",", ":"))
                self._db.execute("UPDATE lending_orders SET status=?,signature=?,updated_at=CURRENT_TIMESTAMP "
                                 "WHERE root=? AND wallet=? AND chain_id=? AND status=?",
                                 (next_status, encoded_signature, root, wallet, chain_id, current))
                updated = self._db.execute("SELECT * FROM lending_orders WHERE root=? AND wallet=? AND chain_id=?",
                                           (root, wallet, chain_id)).fetchone()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self._record(updated)
