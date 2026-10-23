"""JSON over HTTP for acquisition jobs: stdlib only, bounded retries, and believed Retry-After.

A job runs every few minutes, so nothing here archives responses: the artifact a job publishes is
its record. What this does guarantee is that a refusing host is not hammered -- retries back off
exponentially with jitter, a server's own Retry-After is honoured up to a ceiling, and the total
time one request may take is capped so a run cannot outlive its timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
import email.utils
import http.client
import json
import math
import random
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request

from engine.portfolio.contracts import safe_detail


USER_AGENT = "fenorium-acquire/1 (+https://app.fenorium.com)"
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_BODY_BYTES = 32 * 1024 * 1024


class FetchError(RuntimeError):
    """A request did not produce usable JSON. `transient` says whether asking later may help."""

    def __init__(self, message: str, *, transient: bool, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Fetcher:
    timeout_seconds: float = 20.0
    retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    # Per request, all attempts and waits included.
    budget_seconds: float = 120.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    opener: Callable[..., Any] = urllib.request.urlopen

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None,
                 budget_seconds: float | None = None) -> Any:
        return self.request_json("GET", url, headers=headers, budget_seconds=budget_seconds)

    def request_json(self, method: str, url: str, *, body: Any = None,
                     headers: Mapping[str, str] | None = None,
                     budget_seconds: float | None = None) -> Any:
        started = self.clock()
        budget = self.budget_seconds if budget_seconds is None else max(0.0, budget_seconds)
        data = None if body is None else json.dumps(body).encode("utf-8")
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            merged["Content-Type"] = "application/json"
        merged.update(headers or {})
        last: FetchError | None = None
        for attempt in range(self.retries + 1):
            remaining = budget - (self.clock() - started)
            if remaining <= 0:
                if last is None:
                    last = FetchError(f"{url}: request budget exhausted before an attempt", transient=True)
                break
            asked: float | None = None
            try:
                request = urllib.request.Request(url, data=data, headers=merged, method=method)
                with self.opener(request, timeout=min(self.timeout_seconds, remaining)) as response:
                    raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise FetchError(f"{url}: response larger than {MAX_BODY_BYTES} bytes", transient=False)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FetchError(f"{url}: response is not JSON", transient=True) from exc
            except urllib.error.HTTPError as exc:
                status = exc.code
                transient = status == 429 or status >= 500
                asked = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                last = FetchError(f"{url}: HTTP {status}", transient=transient, status=status,
                                  retry_after=asked)
            except FetchError as exc:
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                last = FetchError(f"{url}: network error: {safe_detail(exc)}", transient=True)
            if not last.transient or attempt == self.retries:
                break
            wait = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** attempt))
            # Full jitter keeps two jobs that failed together from retrying together.
            wait = wait * (0.5 + self.jitter() / 2)
            elapsed = self.clock() - started
            if asked is not None:
                remaining = budget - elapsed
                if asked > remaining or asked > MAX_RETRY_AFTER_SECONDS:
                    break
                # A limiter that names its own cooldown is believed over our guess.
                wait = max(wait, asked)
            if elapsed + wait > budget:
                break
            self.sleep(wait)
        assert last is not None
        if (last.transient and self.clock() - started >= budget
                and "budget exhausted" not in str(last).lower()):
            last = FetchError(f"{url}: request budget exhausted after {budget:g}s: {last}",
                              transient=True, status=last.status, retry_after=last.retry_after)
        raise last


def retry_after_seconds(raw: Any, *, now: float | None = None) -> float | None:
    """A Retry-After value in seconds: either form HTTP allows, a delay or a date to wait until."""
    text = "" if raw is None else str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return max(0.0, moment.timestamp() - (time.time() if now is None else now))
    return seconds if math.isfinite(seconds) and seconds >= 0.0 else None
