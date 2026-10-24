"""Who may ask this server for how much, and how much this server may ask LI.FI for.

Two different protections, kept apart because they fail differently:

- Per client (IP, wallet, session), token buckets per class of endpoint. A client over its rate
  gets 429 THROTTLED with Retry-After; nothing upstream is spent on it.
- Per provider, a global budget per minute and per day, persisted so a restart does not hand out
  a fresh day, and a circuit that opens for as long as the provider's own Retry-After says when it
  answers 429. While the circuit is open or the budget is spent, routes answer 503
  PROVIDER_THROTTLED at once -- no request is queued behind a refusal, so no retry storm forms.

Identical quotes asked for within a few seconds are answered once: the first caller fetches, the
others wait for its answer (single flight), and the answer is reused until it expires.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Hashable


class Throttled(Exception):
    """Refused for rate; `retry_after` is how many seconds until asking again can succeed."""

    def __init__(self, code: str, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = max(1, math.ceil(retry_after))


class FlightTimeout(RuntimeError):
    """A caller joined a fetch that did not finish within the bounded wait."""


@dataclass(frozen=True, slots=True)
class Rate:
    per_minute: int
    burst: int


# Per client and endpoint class; reports have their own allowance because they spend no LI.FI quota.
CLIENT_RATES: dict[str, dict[str, Rate]] = {
    "assistant": {"ip": Rate(6, 4), "session": Rate(6, 4)},
    "route": {"ip": Rate(12, 6), "wallet": Rate(8, 4), "session": Rate(8, 4)},
    "route-report": {"ip": Rate(60, 20), "wallet": Rate(30, 10), "session": Rate(30, 10)},
    "wallet-read": {"ip": Rate(20, 6), "wallet": Rate(10, 4), "session": Rate(10, 4)},
    "general": {"ip": Rate(240, 60), "session": Rate(240, 60)},
}


class TokenBuckets:
    """Many buckets of one rate, keyed; the least recently used are forgotten past `maximum`."""

    def __init__(self, rate: Rate, *, clock: Callable[[], float] = time.monotonic, maximum: int = 20_000) -> None:
        self.rate = rate
        self.clock = clock
        self.maximum = maximum
        self._buckets: OrderedDict[Hashable, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key: Hashable) -> float:
        """Spend one token for `key`. Returns 0 when allowed, else seconds until one is available."""
        refill = self.rate.per_minute / 60.0
        with self._lock:
            now = self.clock()
            tokens, at = self._buckets.pop(key, (float(self.rate.burst), now))
            tokens = min(float(self.rate.burst), tokens + (now - at) * refill)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                wait = 0.0
            else:
                self._buckets[key] = (tokens, now)
                wait = (1 - tokens) / refill
            while len(self._buckets) > self.maximum:
                self._buckets.popitem(last=False)
            return wait


_WALLET = re.compile(r"0x[0-9a-fA-F]{40}")


class ClientLimits:
    def __init__(self, rates: dict[str, dict[str, Rate]] | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rates = rates or CLIENT_RATES
        self._buckets = {(kind, scope): TokenBuckets(rate, clock=clock)
                         for kind, scopes in self.rates.items() for scope, rate in scopes.items()}
        self.counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def check(self, kind: str, *, ip: str | None, wallet: str | None = None, session: str | None = None) -> None:
        """Spend one request of `kind` for each identity given; raise Throttled on the first refusal."""
        # A wallet is named by the caller, never proven, so its allowance belongs to the client naming
        # it: keyed by address alone, anyone could spend a victim's routes and purchase reports. Only
        # an address gets a bucket: any other string is unbounded caller input kept as a key.
        if not (isinstance(wallet, str) and _WALLET.fullmatch(wallet)):
            wallet = None
        identities = {"ip": ip, "wallet": wallet and f"{ip}|{wallet.lower()}", "session": session}
        waits = []
        for scope, value in identities.items():
            bucket = self._buckets.get((kind, scope))
            if bucket is not None and value:
                waits.append((bucket.take(value), scope))
        refused = [(wait, scope) for wait, scope in waits if wait > 0]
        with self._lock:
            self.counters[f"{kind}.requests"] = self.counters.get(f"{kind}.requests", 0) + 1
            if refused:
                self.counters[f"{kind}.throttled"] = self.counters.get(f"{kind}.throttled", 0) + 1
        if refused:
            wait, scope = max(refused)
            raise Throttled("THROTTLED", f"too many {kind} requests from this {scope}; slow down", wait)


class ProviderBudget:
    """A provider's allowance: per minute, per UTC day, and a circuit its own 429s open.

    Counts live in SQLite when a path is given, so a restart neither resets the day nor forgets
    an open circuit.
    """

    def __init__(self, name: str, *, per_minute: int, per_day: int, path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.name = name
        self.per_minute = per_minute
        self.per_day = per_day
        self.clock = clock
        self._lock = threading.Lock()
        self._db = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS provider_budget (provider TEXT, window TEXT, used INTEGER, "
                             "PRIMARY KEY (provider, window))")
            self._db.execute("CREATE TABLE IF NOT EXISTS provider_circuit (provider TEXT PRIMARY KEY, open_until REAL)")
        self._memory: dict[str, int] = {}
        self._open_until = self._load_circuit()
        self.counters = {"calls": 0, "refused_budget": 0, "refused_circuit": 0, "provider_throttles": 0}

    def _windows(self, now: float) -> tuple[str, str]:
        moment = datetime.fromtimestamp(now, UTC)
        return moment.strftime("m%Y%m%d%H%M"), moment.strftime("d%Y%m%d")

    def _used(self, window: str) -> int:
        if self._db is None:
            return self._memory.get(window, 0)
        row = self._db.execute("SELECT used FROM provider_budget WHERE provider = ? AND window = ?",
                               (self.name, window)).fetchone()
        return 0 if row is None else int(row[0])

    def _add(self, window: str) -> None:
        if self._db is None:
            self._memory[window] = self._memory.get(window, 0) + 1
            return
        self._db.execute("INSERT INTO provider_budget VALUES (?, ?, 1) ON CONFLICT (provider, window) "
                         "DO UPDATE SET used = used + 1", (self.name, window))

    def _load_circuit(self) -> float:
        if self._db is None:
            return 0.0
        row = self._db.execute("SELECT open_until FROM provider_circuit WHERE provider = ?", (self.name,)).fetchone()
        return 0.0 if row is None else float(row[0])

    def reserve(self) -> None:
        """Spend one call, or raise Throttled(PROVIDER_THROTTLED) without spending anything."""
        with self._lock:
            now = self.clock()
            if now < self._open_until:
                self.counters["refused_circuit"] += 1
                raise Throttled("PROVIDER_THROTTLED", f"{self.name} asked this deployment to wait",
                                self._open_until - now)
            minute, day = self._windows(now)
            if self._used(minute) >= self.per_minute:
                self.counters["refused_budget"] += 1
                raise Throttled("PROVIDER_THROTTLED", f"this deployment's {self.name} allowance for this minute is spent",
                                60 - now % 60)
            if self._used(day) >= self.per_day:
                self.counters["refused_budget"] += 1
                raise Throttled("PROVIDER_THROTTLED", f"this deployment's {self.name} allowance for today is spent",
                                86_400 - now % 86_400)
            self._add(minute)
            self._add(day)
            self.counters["calls"] += 1
            if self._db is not None and int(now) % 50 == 0:
                # Old windows are only history; keep an hour of minutes and a week of days. Each kind
                # needs its own cutoff: every "m" key sorts after every "d" key.
                self._db.execute(
                    "DELETE FROM provider_budget WHERE provider = ? AND ("
                    "(window LIKE 'm%' AND window < ?) OR (window LIKE 'd%' AND window < ?))",
                    (self.name, datetime.fromtimestamp(now - 3_600, UTC).strftime("m%Y%m%d%H%M"),
                     datetime.fromtimestamp(now - 7 * 86_400, UTC).strftime("d%Y%m%d")))

    def trip(self, retry_after: float | None) -> None:
        """The provider answered 429: stop asking for as long as it said, or a minute if it did not."""
        with self._lock:
            wait = 60.0 if retry_after is None else min(max(float(retry_after), 1.0), 86_400.0)
            self._open_until = max(self._open_until, self.clock() + wait)
            self.counters["provider_throttles"] += 1
            if self._db is not None:
                self._db.execute("INSERT OR REPLACE INTO provider_circuit VALUES (?, ?)", (self.name, self._open_until))

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            minute, day = self._windows(now)
            return {
                "provider": self.name,
                "per_minute": {"limit": self.per_minute, "used": self._used(minute)},
                "per_day": {"limit": self.per_day, "used": self._used(day)},
                "circuit_open_for_seconds": max(0, math.ceil(self._open_until - now)),
                **self.counters,
            }


@dataclass
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class SingleFlightCache:
    """Answer identical requests once while a fetch is in flight, and reuse the answer until `ttl`."""

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 maximum: int = 512, wait_seconds: float = 60) -> None:
        self.ttl = ttl_seconds
        self.clock = clock
        self.maximum = maximum
        self.wait_seconds = wait_seconds
        self._answers: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._flights: dict[Hashable, _Flight] = {}
        self._lock = threading.Lock()
        self.counters = {"hits": 0, "misses": 0, "joined": 0}

    def get(self, key: Hashable, fetch: Callable[[], Any], *, fresh: Callable[[Any], bool] = lambda _: True,
            wait_seconds: float | None = None) -> Any:
        with self._lock:
            cached = self._answers.get(key)
            if cached is not None and self.clock() - cached[0] < self.ttl and fresh(cached[1]):
                self.counters["hits"] += 1
                return cached[1]
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()
                self.counters["misses"] += 1
            else:
                self.counters["joined"] += 1
        if not leader:
            if not flight.done.wait(timeout=self.wait_seconds if wait_seconds is None else wait_seconds):
                raise FlightTimeout("the shared fetch did not finish in time")
            if flight.error is not None:
                raise flight.error
            return flight.result
        try:
            flight.result = fetch()
        except BaseException as exc:  # noqa: BLE001 - every waiter gets the same failure
            flight.error = exc
            raise
        else:
            with self._lock:
                self._answers[key] = (self.clock(), flight.result)
                while len(self._answers) > self.maximum:
                    self._answers.popitem(last=False)
            return flight.result
        finally:
            with self._lock:
                self._flights.pop(key, None)
            flight.done.set()
