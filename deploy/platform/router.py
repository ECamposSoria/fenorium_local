"""The server fetching a route itself, so that what it holds has provenance.

``/v1/execution-quotes`` analyses a route the caller supplies. Every input arrives in the
request body, including the hash that ties them together, so the hash shows the caller's own
objects agree with each other and nothing more: edit the transaction, recompute the hash, and
every check still passes. That endpoint therefore refuses to authorise anything.

This module is the other half. The caller says what it holds and which instrument it wants;
the request to LI.FI is made here, and the payload that comes back was never in the caller's
hands. A route obtained this way is the only kind that could ever authorise an instruction --
whether it does is the execution gate's decision, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re
import time
from threading import Lock

from engine.obtainable import MARKET_REFUSAL_PREFIX

# The LI.FI client is imported inside `LifiRouter`, not here. `api` imports this module for
# `RouteRequest` and `RouterError` whether or not a router is configured, and a deployment that
# never asked to reach the network should not have a network client loaded on its behalf.


ROUTER_UNAVAILABLE = "ROUTER_UNAVAILABLE"
ROUTER_BUSY = "ROUTER_BUSY"
# LI.FI asked this deployment to wait, or the deployment's own LI.FI allowance is spent.
PROVIDER_THROTTLED = "PROVIDER_THROTTLED"
# How long an identical route request is answered from the last fetch, while its quote still has
# at least ROUTE_REUSE_MARGIN_SECONDS to live.
ROUTE_CACHE_SECONDS = 15
ROUTE_REUSE_MARGIN_SECONDS = 30
NO_ROUTE_AT_SIZE = "NO_ROUTE_AT_SIZE"
DEFAULT_TIMEOUT_SECONDS = 20.0
# One outbound request per this interval, for the whole process. An unauthenticated POST that
# makes the server call a rate-limited vendor is an amplifier; refusing early keeps a caller
# from spending the deployment's quota, and never queues, which would block the next reader.
MIN_INTERVAL_SECONDS = 1.2
MAX_REFUSAL_CHARS = 240
INTEGRATOR_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
# A fraction of the amount paid, written as a plain decimal below one. Capped at 1% so that a
# mistyped "0.1" becomes a startup error instead of a ten per cent charge on every purchase.
FEE_PATTERN = re.compile(r"0\.[0-9]{1,6}")
MAX_INTEGRATOR_FEE = Fraction(1, 100)
STATUS_URL = "https://li.quest/v1/status"
# A status body is a few kilobytes; anything far larger is not one.
MAX_STATUS_BYTES = 256 * 1024


class RouterError(Exception):
    """The route could not be obtained, with the reason code the API will report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """One buyer, one holding, one instrument."""

    chain_id: int
    instrument_address: str
    instrument_decimals: int
    pay_address: str
    pay_decimals: int
    pay_symbol: str
    amount_raw: int
    from_address: str
    # Where the payment starts. None keeps the route on the instrument's own chain.
    pay_chain_id: int | None = None
    direction: str = "buy"
    # Where a sale's settlement asset is received. None keeps it on the instrument chain.
    receive_chain_id: int | None = None


class LifiRouter:
    """Fetches one exact-amount route from LI.FI per request, paced across the process."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        integrator: str | None = None,
        fee: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        min_interval_seconds: float = MIN_INTERVAL_SECONDS,
        monotonic=time.monotonic,
        budget=None,
        cache=None,
    ) -> None:
        self._api_key = api_key or None
        if integrator is not None and not INTEGRATOR_PATTERN.fullmatch(integrator):
            # Read at startup and sent on every route. A value that would not survive a query
            # string is a misconfiguration to refuse now, not an attribution to lose silently.
            raise ValueError("the LI.FI integrator must be 1-64 characters of [A-Za-z0-9._-]")
        self._integrator = integrator or None
        fee = fee or None
        if fee is not None:
            if self._integrator is None:
                raise ValueError("a LI.FI fee needs an integrator to be paid to")
            if not FEE_PATTERN.fullmatch(fee) or not 0 < Fraction(fee) <= MAX_INTEGRATOR_FEE:
                raise ValueError("the LI.FI fee must be a plain decimal above 0 and at most 0.01")
        self._fee = fee
        self._timeout_seconds = float(timeout_seconds)
        self._min_interval_seconds = float(min_interval_seconds)
        self._monotonic = monotonic
        self._lock = Lock()
        self._last_request_at: float | None = None
        # With a budget (deploy/platform/ratelimit.ProviderBudget) the deployment's LI.FI allowance
        # decides, not a one-request slot: calls are counted per minute and per day, and LI.FI's
        # own 429 stops them for as long as it asks. Without one the slot stands, as before.
        self.budget = budget
        self.cache = cache

    @property
    def fee(self) -> Fraction | None:
        """The share of each purchase this deployment asks LI.FI to collect for it, if any."""
        return None if self._fee is None else Fraction(self._fee)

    @property
    def integrator(self) -> str | None:
        """The name LI.FI credits the integrator's share of every route to."""
        return self._integrator

    def _reserve(self) -> None:
        if self.budget is not None:
            from .ratelimit import Throttled
            try:
                self.budget.reserve()
            except Throttled as exc:
                raise RouterError(PROVIDER_THROTTLED, str(exc)) from exc
            return
        with self._lock:
            now = self._monotonic()
            if (
                self._last_request_at is not None
                and now - self._last_request_at < self._min_interval_seconds
            ):
                raise RouterError(
                    ROUTER_BUSY,
                    "this deployment is already fetching a route; retry in a moment",
                )
            self._last_request_at = now

    def fetch(self, request: RouteRequest, *, deadline_at: float | None = None):
        """One route for this exact request. An identical request within a few seconds -- a double
        click, two tabs, a preview then a build -- is answered from that fetch while its quote has
        time left, so it costs LI.FI nothing."""
        if self.cache is None:
            return self._fetch(request, deadline_at=deadline_at)
        now = time.time()
        from .ratelimit import FlightTimeout
        wait = {} if deadline_at is None else {"wait_seconds": max(0.0, deadline_at - self._monotonic())}
        try:
            return self.cache.get(request, lambda: self._fetch(request, deadline_at=deadline_at),
                                  fresh=lambda fetched: fetched.binding.expires_at - time.time() > ROUTE_REUSE_MARGIN_SECONDS
                                  and fetched.fetched_at.timestamp() <= now + 1, **wait)
        except FlightTimeout as exc:
            raise RouterError(ROUTER_BUSY, "an identical route request is still being fetched") from exc

    def _fetch(self, request: RouteRequest, *, deadline_at: float | None = None):
        from research.par_sources.lifi_quotes import (
            DEFAULT_FROM_ADDRESS, PayAsset, fetch_exact, fetch_exact_sale,
        )

        from engine.composer import RouteError

        self._reserve()
        try:
            route_deadline = self._monotonic() + self._timeout_seconds
            if deadline_at is not None:
                route_deadline = min(route_deadline, deadline_at)
            pay_with = PayAsset(
                address=request.pay_address,
                decimals=request.pay_decimals,
                symbol=request.pay_symbol,
            )
            if request.direction == "sell":
                return fetch_exact_sale(
                    request.chain_id,
                    request.instrument_address,
                    receive_with=pay_with,
                    amount_raw=request.amount_raw,
                    decimals=request.instrument_decimals,
                    from_address=request.from_address or DEFAULT_FROM_ADDRESS,
                    api_key=self._api_key,
                    deadline=route_deadline,
                    integrator=self._integrator,
                    fee=self._fee,
                    to_chain=request.receive_chain_id,
                )
            return fetch_exact(
                request.chain_id,
                request.instrument_address,
                pay_with=pay_with,
                amount_raw=request.amount_raw,
                decimals=request.instrument_decimals,
                from_address=request.from_address or DEFAULT_FROM_ADDRESS,
                api_key=self._api_key,
                deadline=route_deadline,
                integrator=self._integrator,
                fee=self._fee,
                from_chain=request.pay_chain_id,
            )
        except (RouteError, AttributeError, TypeError, KeyError) as exc:
            if "(HTTP 429)" in str(exc) and self.budget is not None:
                self.budget.trip(getattr(exc, "retry_after", None))
                raise RouterError(PROVIDER_THROTTLED, "LI.FI asked this deployment to wait") from exc
            raise _classify(exc) from exc

    def status(self, tx_hash: str, *, from_chain: int, to_chain: int) -> dict:
        """LI.FI's account of one transfer, paced like a route fetch.

        LI.FI answers 200 with `NOT_FOUND` for a hash it has not indexed yet, so only a failure to
        get any answer is this deployment's outage.
        """
        import http.client
        import json
        import urllib.error
        import urllib.parse
        import urllib.request

        from research.par_sources.lifi_quotes import USER_AGENT

        self._reserve()
        query = urllib.parse.urlencode({"txHash": tx_hash, "fromChain": from_chain, "toChain": to_chain})
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self._api_key:
            headers["x-lifi-api-key"] = self._api_key
        request = urllib.request.Request(f"{STATUS_URL}?{query}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read(MAX_STATUS_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and self.budget is not None:
                from research.par_sources.lifi_quotes import _retry_after_seconds
                self.budget.trip(_retry_after_seconds(exc.headers))
                raise RouterError(PROVIDER_THROTTLED, "LI.FI asked this deployment to wait") from exc
            # LI.FI documents 404 with code 1003 as "not indexed yet": a transfer it has not seen,
            # which is a normal moment after broadcasting, not our outage.
            try:
                refusal = json.loads(exc.read(4096) or b"{}")
            except (ValueError, OSError):
                refusal = None
            if exc.code == 404 and isinstance(refusal, dict) and str(refusal.get("code")) == "1003":
                return {"status": "NOT_FOUND"}
            raise RouterError(ROUTER_UNAVAILABLE, "LI.FI status is unavailable") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise RouterError(ROUTER_UNAVAILABLE, "LI.FI status is unavailable") from exc
        if len(body) > MAX_STATUS_BYTES:
            raise RouterError(ROUTER_UNAVAILABLE, "LI.FI status answer was not a status")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise RouterError(ROUTER_UNAVAILABLE, "LI.FI status answer was not JSON") from exc
        if not isinstance(payload, dict):
            raise RouterError(ROUTER_UNAVAILABLE, "LI.FI status answer was not an object")
        return payload


def _classify(error: Exception) -> RouterError:
    """A market's refusal is a finding. Everything else is this deployment's problem.

    LI.FI answers "nobody will fill this" with an HTTP 404 body, which the adapter marks. A
    throttle, a timeout or a rejected key says nothing whatsoever about whether the instrument
    can be bought, and reporting one as though it did would publish our own outage as a
    property of the market.
    """
    message = str(error)
    if message.startswith(MARKET_REFUSAL_PREFIX):
        return RouterError(NO_ROUTE_AT_SIZE, message[:MAX_REFUSAL_CHARS])
    # The vendor's prose is not echoed back: it is diagnostic for this deployment's logs, and a
    # caller learning which key was rejected learns something about the deployment, not the
    # market.
    return RouterError(ROUTER_UNAVAILABLE, "this deployment could not obtain a route")
