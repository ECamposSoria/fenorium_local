"""Briefly cached current prices for held tokens absent from the published feed."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote

from engine.acquire.fetch import FetchError, Fetcher
from engine.acquire.jobs.current_prices import _entry as price_entry


LLAMA_URL = "https://coins.llama.fi/prices/current/"
ON_DEMAND_TTL_SECONDS = 300
MAX_CACHED = 5000


@dataclass
class OnDemandPrices:
    """DefiLlama's current price for held tokens the five-minute feed does not track."""

    fetcher: Fetcher = field(default_factory=lambda: Fetcher(timeout_seconds=10, retries=1, budget_seconds=20))
    clock: Callable[[], float] = time.monotonic
    _cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    _not_before: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def entries(self, keys: list[str], *, now_text: str,
                budget_seconds: float | None = None) -> dict[str, dict[str, Any]]:
        now = self.clock()
        with self._lock:
            expired = [key for key, (at, _) in self._cache.items() if now - at >= ON_DEMAND_TTL_SECONDS]
            for key in expired:
                del self._cache[key]
            fresh = {key: entry for key, (_, entry) in self._cache.items()}
            if now < self._not_before:
                return {key: fresh[key] for key in keys if key in fresh}
        wanted = [key for key in dict.fromkeys(keys) if key not in fresh]
        for start in range(0, len(wanted), 60):
            with self._lock:
                if self.clock() < self._not_before:
                    break
            batch = wanted[start:start + 60]
            fetcher = self.fetcher
            if budget_seconds is not None:
                remaining = budget_seconds - (self.clock() - now)
                if remaining <= 0:
                    break
                fetcher = replace(self.fetcher,
                                  timeout_seconds=min(self.fetcher.timeout_seconds, max(1.0, remaining)),
                                  budget_seconds=remaining)
            try:
                payload = fetcher.get_json(LLAMA_URL + ",".join(quote(key, safe=":") for key in batch))
            except FetchError as exc:
                with self._lock:
                    if exc.retry_after is not None:
                        self._not_before = max(self._not_before, self.clock() + exc.retry_after)
                break
            coins = payload.get("coins") if isinstance(payload, Mapping) else None
            found = {str(key).lower(): value for key, value in (coins or {}).items()} if isinstance(coins, Mapping) else {}
            with self._lock:
                for key in batch:
                    entry = price_entry(key, found.get(key.lower()), now_text)
                    self._cache.pop(key, None)
                    self._cache[key] = (now, entry)
                    while len(self._cache) > MAX_CACHED:
                        del self._cache[next(iter(self._cache))]
                    fresh[key] = entry
        return {key: fresh[key] for key in keys if key in fresh}
