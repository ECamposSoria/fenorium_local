"""Durable state for acquisition jobs, in one SQLite file.

Three things are kept. Every run, with its counts, its outcome and whether it published. Every
item a job has ever handled, with when it was last attempted, when it last succeeded, which source
answered and the last error -- so a token that has silently failed for a week is a query, not a
mystery. And per-run checkpoints, so a run killed halfway is resumed rather than started over.

A run holds a lease on its job. A second run of the same job while the lease is live is refused;
a lease whose holder stopped renewing it is taken over, and that run is recorded as abandoned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid


SCHEMA_VERSION = 1
LEASE_SECONDS = 15 * 60


class JobBusy(RuntimeError):
    """Another run of this job holds a live lease."""


class LeaseLost(RuntimeError):
    """Another run has taken over this lease."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: str
    job: str
    started_at: datetime
    # The abandoned run this one continues, whose checkpoints it may reuse.
    resumes: str | None


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, job TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, finished_at TEXT,
                resumes TEXT, attempted INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, reused INTEGER NOT NULL DEFAULT 0,
                published INTEGER NOT NULL DEFAULT 0, error TEXT, summary TEXT);
            CREATE INDEX IF NOT EXISTS runs_by_job ON runs (job, started_at);
            CREATE TABLE IF NOT EXISTS items (
                job TEXT NOT NULL, item_key TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
                last_success_at TEXT, source TEXT, error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (job, item_key));
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL, item_key TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY (run_id, item_key));
            CREATE TABLE IF NOT EXISTS provider_cooldowns (
                job TEXT PRIMARY KEY, not_before TEXT NOT NULL);
        """)
        self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self._db.close()

    def cooldown(self, job: str) -> datetime | None:
        row = self._db.execute("SELECT not_before FROM provider_cooldowns WHERE job = ?", (job,)).fetchone()
        return None if row is None else _parse(row["not_before"])

    def set_cooldown(self, job: str, not_before: datetime) -> None:
        stamp = _iso(not_before)
        with self._transaction():
            self._db.execute("""
                INSERT INTO provider_cooldowns (job, not_before) VALUES (?, ?)
                ON CONFLICT (job) DO UPDATE SET not_before =
                    CASE WHEN excluded.not_before > provider_cooldowns.not_before
                         THEN excluded.not_before ELSE provider_cooldowns.not_before END
            """, (job, stamp))

    def _transaction(self) -> "_Transaction":
        return _Transaction(self._db)

    def acquire(self, job: str, *, now: datetime, resume_within: timedelta | None) -> Lease:
        """Start a run of `job`, or raise JobBusy while another run's lease is live."""
        with self._transaction():
            live = self._db.execute(
                "SELECT run_id, started_at, heartbeat_at FROM runs WHERE job = ? AND status = 'RUNNING'",
                (job,),
            ).fetchall()
            resumes = None
            for row in live:
                if now - _parse(row["heartbeat_at"]) < timedelta(seconds=LEASE_SECONDS):
                    raise JobBusy(f"{job} is already running as {row['run_id']}")
                self._db.execute(
                    "UPDATE runs SET status = 'ABANDONED', finished_at = ?, error = ? WHERE run_id = ?",
                    (_iso(now), "the run stopped renewing its lease and was taken over", row["run_id"]),
                )
                if resume_within is not None and now - _parse(row["started_at"]) <= resume_within:
                    resumes = row["run_id"]
            run_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO runs (run_id, job, status, started_at, heartbeat_at, resumes) VALUES (?, ?, 'RUNNING', ?, ?, ?)",
                (run_id, job, _iso(now), _iso(now), resumes),
            )
        return Lease(run_id=run_id, job=job, started_at=now, resumes=resumes)

    def heartbeat(self, lease: Lease, *, now: datetime) -> None:
        updated = self._db.execute("UPDATE runs SET heartbeat_at = ? WHERE run_id = ? AND status = 'RUNNING'",
                         (_iso(now), lease.run_id))
        if updated.rowcount == 0:
            raise LeaseLost("another run took over the lease")

    def checkpoint(self, lease: Lease, item_key: str, result: dict[str, Any]) -> None:
        self._db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?)",
                         (lease.run_id, item_key, json.dumps(result, sort_keys=True)))

    def checkpoints(self, run_id: str | None) -> dict[str, dict[str, Any]]:
        if run_id is None:
            return {}
        rows = self._db.execute("SELECT item_key, result FROM checkpoints WHERE run_id = ?", (run_id,))
        return {row["item_key"]: json.loads(row["result"]) for row in rows}

    def record_item(self, job: str, item_key: str, *, ok: bool, source: str | None, error: str | None,
                    now: datetime) -> None:
        stamp = _iso(now)
        if ok:
            self._db.execute("""
                INSERT INTO items (job, item_key, last_attempt_at, last_success_at, source, error,
                                   consecutive_failures, attempts)
                VALUES (?, ?, ?, ?, ?, NULL, 0, 1)
                ON CONFLICT (job, item_key) DO UPDATE SET last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at, source = excluded.source, error = NULL,
                    consecutive_failures = 0, attempts = items.attempts + 1
            """, (job, item_key, stamp, stamp, source))
        else:
            self._db.execute("""
                INSERT INTO items (job, item_key, last_attempt_at, last_success_at, source, error,
                                   consecutive_failures, attempts)
                VALUES (?, ?, ?, NULL, ?, ?, 1, 1)
                ON CONFLICT (job, item_key) DO UPDATE SET last_attempt_at = excluded.last_attempt_at,
                    error = excluded.error, consecutive_failures = items.consecutive_failures + 1,
                    attempts = items.attempts + 1
            """, (job, item_key, stamp, source, (error or "")[:500]))

    def finish(self, lease: Lease, *, status: str, now: datetime, attempted: int, succeeded: int,
               failed: int, reused: int, published: bool, error: str | None,
               summary: dict[str, Any] | None = None) -> None:
        with self._transaction():
            updated = self._db.execute("""
                UPDATE runs SET status = ?, finished_at = ?, heartbeat_at = ?, attempted = ?, succeeded = ?,
                    failed = ?, reused = ?, published = ?, error = ?, summary = ?
                WHERE run_id = ? AND status = 'RUNNING'
            """, (status, _iso(now), _iso(now), attempted, succeeded, failed, reused, int(published),
                  (error or None) and error[:1000], json.dumps(summary or {}, sort_keys=True), lease.run_id))
            if updated.rowcount == 0:
                raise LeaseLost("another run took over the lease")
            # A finished run's checkpoints have served their purpose; an abandoned one's are kept
            # only until a later run of the job finishes.
            self._db.execute("""
                DELETE FROM checkpoints WHERE run_id IN (
                    SELECT run_id FROM runs WHERE job = ? AND status != 'RUNNING')
            """, (lease.job,))
            # Bounded history: the last 500 runs of each job are enough to see any trend.
            self._db.execute("""
                DELETE FROM runs WHERE job = ? AND run_id NOT IN (
                    SELECT run_id FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT 500)
            """, (lease.job, lease.job))

    def runs(self, job: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT ?", (job, limit))
        return [_run_row(row) for row in rows]

    def jobs(self) -> list[str]:
        return [row["job"] for row in self._db.execute("SELECT DISTINCT job FROM runs ORDER BY job")]

    def items(self, job: str) -> Iterator[dict[str, Any]]:
        for row in self._db.execute("SELECT * FROM items WHERE job = ? ORDER BY item_key", (job,)):
            yield dict(row)


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["published"] = bool(result["published"])
    result["summary"] = json.loads(result["summary"]) if result.get("summary") else {}
    return result


class _Transaction:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __enter__(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind, value, traceback) -> None:
        self.db.execute("COMMIT" if kind is None else "ROLLBACK")
