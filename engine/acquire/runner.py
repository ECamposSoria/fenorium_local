"""One run of one acquisition job: lease, fetch in batches with checkpoints, publish, record.

A job says what its items are, how to fetch a batch of them and how to turn the results into
an artifact. The runner owns everything that must be the same for every job:

- One run at a time per job (a lease in the state store); a crashed run is resumed from its
  checkpoints when the job allows it.
- Every item's outcome is recorded with its source and error, whatever happens to the run.
- Items that failed transiently get one more attempt at the end of the run, after a pause.
- One item's failure never fails the run. The run fails when the job's own artifact fails
  validation, when too large a share of items failed, or when the job raised.
- The artifact is replaced atomically and only after it validated, so a reader sees the last
  good one or the new good one, never a partial or a refused one.

Outcomes: SUCCEEDED (everything answered), PARTIAL (published, some items failed), FAILED,
DEFERRED (a provider cooldown is still active), SKIPPED (another run held the lease). The exit
status is 1 only for FAILED.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from engine.onchain import ChainReader, OnchainError
from engine.portfolio.contracts import safe_detail

from .state import JobBusy, Lease, LeaseLost, StateStore


STATUS_FILE = "acquisition-status.json"
STATUS_SCHEMA = "fenorium/acquisition-status/1"
# An item failing this many runs in a row is listed as needing attention.
ATTENTION_AFTER_FAILURES = 3
# `fenorium-acquire@.service` allows 900 seconds from start. Keep 60 seconds for validation,
# status recording, and atomic publication after acquisition has finished.
RUN_BUDGET_SECONDS = 840.0
MIN_ITEM_BUDGET_SECONDS = 20.0


def bound_reader(reader: ChainReader, deadline_at: datetime, now: Callable[[], datetime]) -> None:
    """Keep endpoint rotation and every RPC within the item's remaining read budget."""
    seconds_left = max(0.0, (deadline_at - now()).total_seconds())
    deadline = time.monotonic() + seconds_left
    reader.deadline_at = deadline
    call = reader._call

    def bounded_call(endpoint, method, params, *, timeout):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OnchainError(f"chain {reader.chain.chain_id}: read budget exhausted")
        return call(endpoint, method, params, timeout=min(timeout, remaining))

    reader._call = bounded_call


@dataclass(frozen=True, slots=True)
class Item:
    key: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class ItemResult:
    ok: bool
    value: Any = None
    source: str | None = None
    error: str | None = None
    # Whether asking again may help: a timeout or a 429, not "this token has no price".
    transient: bool = False
    retry_after: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "value": self.value, "source": self.source, "error": self.error,
                "transient": self.transient, "retry_after": self.retry_after}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ItemResult":
        return cls(ok=bool(data.get("ok")), value=data.get("value"), source=data.get("source"),
                   error=data.get("error"), transient=bool(data.get("transient")),
                   retry_after=data.get("retry_after"))


@dataclass(slots=True)
class Context:
    """What a job may read and where it publishes."""

    publish_dir: Path
    inputs: Mapping[str, Path] = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    budget_seconds: float = RUN_BUDGET_SECONDS
    fetch_budgets: Mapping[str, float] = field(default_factory=dict)
    fetch_deadlines: Mapping[str, datetime] = field(default_factory=dict)

    def read_json(self, name: str) -> Any | None:
        """A JSON input, or None when it is absent or unreadable."""
        path = self.inputs.get(name)
        if path is None:
            return None
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def previous(self, artifact: str) -> Any | None:
        try:
            return json.loads((self.publish_dir / artifact).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class Job(Protocol):
    name: str
    artifact: str
    batch_size: int
    # The share of items that may fail before the run itself is a failure.
    max_failure_ratio: float
    # How long after a crash its checkpoints may still be reused; None never resumes.
    resume_within: timedelta | None

    def items(self, context: Context) -> Sequence[Item]: ...

    def fetch(self, context: Context, batch: Sequence[Item]) -> Mapping[str, ItemResult]: ...

    def build(self, context: Context, results: Mapping[str, ItemResult], items: Sequence[Item],
              previous: Any | None) -> dict[str, Any]: ...

    def validate(self, document: Mapping[str, Any], previous: Any | None) -> None: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    job: str
    run_id: str | None
    status: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    reused: int = 0
    published: bool = False
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "FAILED" else 0


def write_atomic(path: Path, document: Any) -> None:
    """Replace `path` in one step, readable by the platform's unprivileged user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, separators=(",", ":"), sort_keys=True)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def run(job: Job, store: StateStore, context: Context, *, retry_pause_seconds: float = 5.0) -> RunOutcome:
    now = context.now()
    deadline_at = now + timedelta(seconds=max(0.0, context.budget_seconds))
    try:
        lease = store.acquire(job.name, now=now, resume_within=job.resume_within)
    except JobBusy as exc:
        return RunOutcome(job=job.name, run_id=None, status="SKIPPED", error=safe_detail(exc))
    results: dict[str, ItemResult] = {}
    attempt_times: dict[str, datetime] = {}
    reused = 0
    items: Sequence[Item] = ()
    try:
        stored_cooldown = store.cooldown(job.name)
        if stored_cooldown is not None and stored_cooldown > context.now():
            store.finish(lease, status="DEFERRED", now=context.now(), attempted=0, succeeded=0, failed=0,
                         reused=0, published=False, error=None,
                         summary={"not_before": _iso_utc(stored_cooldown)})
            return RunOutcome(job=job.name, run_id=lease.run_id, status="DEFERRED")
        try:
            items = job.items(context)
            keys = {item.key for item in items}
            for key, data in store.checkpoints(lease.resumes).items():
                attempted_at = _checkpoint_attempted_at(data)
                if key in keys and data.get("ok") and attempted_at is not None:
                    results[key] = ItemResult.from_json(data)
                    attempt_times[key] = attempted_at
                    store.checkpoint(lease, key, data)
                    reused += 1
            pending = [item for item in items if item.key not in results]
            cooldown = _fetch_all(job, store, context, lease, pending, results, attempt_times,
                                  retry_pause_seconds=retry_pause_seconds, deadline_at=deadline_at)
            retry = [item for item in pending if not results[item.key].ok and results[item.key].transient
                     and (results[item.key].retry_after is None
                          or results[item.key].retry_after <= retry_pause_seconds)]
            if retry:
                remaining = max(0.0, (deadline_at - context.now()).total_seconds())
                if remaining > 0:
                    context.sleep(min(retry_pause_seconds, remaining))
                cooldown = _fetch_all(job, store, context, lease, retry, results, attempt_times,
                                      retry_pause_seconds=retry_pause_seconds, cooldown=cooldown,
                                      deadline_at=deadline_at)
            if cooldown is not None and cooldown > context.now():
                store.set_cooldown(job.name, cooldown)
            for item in items:
                result = results[item.key]
                store.record_item(job.name, item.key, ok=result.ok, source=result.source, error=result.error,
                                  now=attempt_times[item.key])
            failed = sum(1 for item in items if not results[item.key].ok)
            succeeded = len(items) - failed
            if items and failed / len(items) > job.max_failure_ratio:
                raise _RunFailure(f"{failed} of {len(items)} items failed, above the "
                                  f"{job.max_failure_ratio:.0%} this job tolerates")
            previous = context.previous(job.artifact)
            document = job.build(context, results, items, previous)
            job.validate(document, previous)
            store.heartbeat(lease, now=context.now())
            write_atomic(context.publish_dir / job.artifact, document)
            status = "SUCCEEDED" if failed == 0 else "PARTIAL"
            outcome = RunOutcome(job.name, lease.run_id, status, attempted=len(items) - reused,
                                 succeeded=succeeded, failed=failed, reused=reused, published=True)
        except LeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 - a job's failure is recorded, then reported
            failed = sum(1 for item in items if item.key in results and not results[item.key].ok)
            outcome = RunOutcome(job.name, lease.run_id, "FAILED", attempted=max(0, len(results) - reused),
                                 succeeded=sum(1 for result in results.values() if result.ok), failed=failed,
                                 reused=reused, published=False, error=safe_detail(exc, limit=1000))
        store.finish(lease, status=outcome.status, now=context.now(), attempted=outcome.attempted,
                     succeeded=outcome.succeeded, failed=outcome.failed, reused=outcome.reused,
                     published=outcome.published, error=outcome.error)
        return outcome
    except LeaseLost:
        return RunOutcome(job.name, lease.run_id, "ABANDONED", attempted=max(0, len(results) - reused),
                          succeeded=sum(1 for result in results.values() if result.ok),
                          failed=sum(1 for result in results.values() if not result.ok),
                          reused=reused, published=False, error="another run took over the lease")


class _RunFailure(RuntimeError):
    pass


def _fetch_all(job: Job, store: StateStore, context: Context, lease: Lease, items: Sequence[Item],
               results: dict[str, ItemResult], attempt_times: dict[str, datetime], *,
               retry_pause_seconds: float, deadline_at: datetime,
               cooldown: datetime | None = None) -> datetime | None:
    for start in range(0, len(items), max(1, job.batch_size)):
        batch = items[start:start + max(1, job.batch_size)]
        run_remaining = max(0.0, (deadline_at - context.now()).total_seconds())
        remaining = 0.0 if cooldown is None else max(0.0, (cooldown - context.now()).total_seconds())
        if 0 < remaining <= retry_pause_seconds:
            context.sleep(min(remaining, run_remaining))
            remaining = max(0.0, (cooldown - context.now()).total_seconds())
        unattempted: set[str] = set()
        if remaining > retry_pause_seconds:
            answered = {item.key: ItemResult(ok=False, error="provider cooldown", transient=True,
                                             retry_after=remaining)
                        for item in batch}
        else:
            fetchable: list[Item] = []
            budgets: dict[str, float] = {}
            deadlines: dict[str, datetime] = {}
            configured_budget = getattr(job, "fetch_budget_seconds", None)
            if configured_budget is None:
                configured_budget = float("inf")
            for offset, item in enumerate(batch):
                remaining = max(0.0, (deadline_at - context.now()).total_seconds())
                if remaining <= 0:
                    unattempted.add(item.key)
                    if item.key not in results:
                        results[item.key] = ItemResult(ok=False, error="run budget exhausted before this item",
                                                       transient=True)
                        attempt_times[item.key] = context.now()
                    continue
                pending_count = len(items) - start - offset
                fair_share = remaining / max(1, pending_count)
                budgets[item.key] = min(configured_budget, max(MIN_ITEM_BUDGET_SECONDS, fair_share))
                deadlines[item.key] = context.now() + timedelta(seconds=budgets[item.key])
                fetchable.append(item)
            if not fetchable:
                answered = {}
            else:
                fetch_context = replace(context, fetch_budgets=budgets, fetch_deadlines=deadlines)
                try:
                    answered = dict(job.fetch(fetch_context, fetchable))
                except LeaseLost:
                    raise
                except Exception as exc:  # noqa: BLE001 - a failed batch fails its items, not the run
                    answered = {item.key: ItemResult(ok=False, error=safe_detail(exc, limit=500),
                                                     transient=bool(getattr(exc, "transient", True)))
                                for item in fetchable}
            retry_after = max((result.retry_after or 0.0 for result in answered.values()), default=0.0)
            if retry_after > 0:
                until = context.now() + timedelta(seconds=retry_after)
                cooldown = until if cooldown is None else max(cooldown, until)
        for item in batch:
            if item.key in unattempted:
                continue
            result = answered.get(item.key) or ItemResult(ok=False, error="the source did not answer for this item")
            results[item.key] = result
            attempted_at = context.now()
            attempt_times[item.key] = attempted_at
            checkpoint = result.to_json()
            checkpoint["attempted_at"] = _iso_utc(attempted_at)
            store.checkpoint(lease, item.key, checkpoint)
        store.heartbeat(lease, now=context.now())
    return cooldown


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _checkpoint_attempted_at(data: Mapping[str, Any]) -> datetime | None:
    raw = data.get("attempted_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def status_document(store: StateStore, *, now: datetime) -> dict[str, Any]:
    """Every job's recent runs and the items that keep failing, for the platform to serve."""
    jobs: dict[str, Any] = {}
    for name in store.jobs():
        runs = store.runs(name, limit=50)
        last = runs[0] if runs else None
        finished = [row for row in runs if row["status"] in ("SUCCEEDED", "PARTIAL", "FAILED", "DEFERRED")]
        last_success = next((row for row in finished if row["status"] in ("SUCCEEDED", "PARTIAL")), None)
        streak = 0
        for row in finished:
            if row["status"] != "FAILED":
                break
            streak += 1
        items = list(store.items(name))
        # A run that publishes records every item in the job's current set, so an item it did not
        # attempt has left that set: its history is kept, but it can no longer fail and must not keep
        # an alert open forever.
        reference = last_success["started_at"] if last_success is not None else None
        current = [row for row in items if reference is None or row["last_attempt_at"] >= reference]
        attention = [row for row in current if row["consecutive_failures"] >= ATTENTION_AFTER_FAILURES]
        jobs[name] = {
            "last_run": None if last is None else {
                key: last[key] for key in ("run_id", "status", "started_at", "finished_at", "attempted",
                                           "succeeded", "failed", "reused", "published", "error", "summary")},
            "last_success_at": None if last_success is None else last_success["finished_at"],
            "consecutive_failed_runs": streak,
            "items": {
                "total": len(items),
                "failing": sum(1 for row in current if row["consecutive_failures"] > 0),
                "retired": len(items) - len(current),
                "needs_attention": len(attention),
                "examples": [{key: row[key] for key in ("item_key", "consecutive_failures", "last_success_at",
                                                        "last_attempt_at", "error")}
                             for row in sorted(attention, key=lambda row: -row["consecutive_failures"])[:20]],
            },
        }
    return {"schema_version": STATUS_SCHEMA, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "jobs": jobs}
