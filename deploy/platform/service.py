"""Small stdlib HTTP boundary for the Fenorium analytical terminal."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
import hashlib
import ipaddress
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
import select
import socket
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .assistant import Assistant, AssistantError
from .execution_gate import CLOSED, ExecutionGate, from_environment
from .instruments import FRESH_UNDER_SECONDS, STALE_UNDER_SECONDS, InstrumentDirectory
from .current_prices import CurrentPrices
from engine.acquire.runner import STATUS_FILE as ACQUISITION_STATUS_FILE
from .price_history import MAX_LAG_DAYS, TRAILING_DAYS, PriceHistory
from .ratelimit import ClientLimits, Throttled
from .route_store import RouteStore
from .order_store import OrderStore
from .route_tracker import RouteTracker
from .on_demand_prices import OnDemandPrices
from .wallets import RoutableTokens, WalletBusy, WalletReader
from .api import APIError, PlatformAPI, json_bytes
from engine.onchain import ChainReader
from engine.registry import parse_asset_id, registry, registry_document
from engine.rating import IMPACT_MODEL, RATING_METHOD_VERSION
from engine.registry.instruments import fixed_income_terms, instrument_id
from engine.portfolio.analytics import (
    DAILY_RETURN_PLACES, MIN_ES_OBSERVATIONS, _round_decimal,
    return_statistics,
)
from .rerating import (  # noqa: F401 - re-exported: tests and callers read them from here
    EXECUTION_COST_BPS,
    EXECUTION_COST_MODEL,
    HOLDING_PERIOD_DAYS,
    IMPACT_COEFFICIENT_BPS,
    IMPACT_EXPONENT,
    TIER_HAIRCUT_BPS,
    _is_size_depth_refusal,
    _rating_assumptions,
    _rating_json,
    _rating_point,
    _rerated_rows,
    restate_declared_days,
)


def _instrument_price_history(
    history: PriceHistory | None, row: dict[str, Any], requested_id: str, now: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    if row.get("rate_kind") == "declared_demo":
        return None, "declared testnet instrument: its price path is declared, not observed"
    if history is None or not history.path.is_file():
        return None, "no price history is published"
    address = row.get("address") or None
    today = now.astimezone(UTC).date()
    series = history.series(row.get("chain_id"), address,
                            end=today,
                            **({"instrument_id": requested_id} if not address else {}))
    if series is None or not series["prices"]:
        return None, "no daily price is published for this instrument"
    prices = [(date.fromisoformat(day), Decimal(value)) for day, value in series["prices"]]
    last_day, last_price = prices[-1]
    stale = (today - last_day).days > MAX_LAG_DAYS
    reasons = []
    if stale:
        reasons.append(f"the series ends on {last_day}, more than {MAX_LAG_DAYS} days before today")
    with localcontext() as context:
        context.prec = 28
        returns = [(day, (value / previous - 1) * 10_000)
                   for (previous_day, previous), (day, value) in zip(prices, prices[1:])
                   if (day - previous_day).days == 1]
        # ES95 and volatility rate the trailing window; the count and histogram describe that window.
        risk_window_start = today - timedelta(days=TRAILING_DAYS - 1)
        statistics = {"observations": sum(1 for day, _ in returns if day >= risk_window_start),
                      "risk_window_start": risk_window_start.isoformat(), "expected_shortfall_95_bps": None,
                      "annualized_volatility_bps": None, "max_drawdown_bps": None,
                      "return_30d_bps": None, "return_90d_bps": None}
        peak = prices[0][1]
        drawdown = Decimal(0)
        for _, value in prices:
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
        if len(prices) >= 2:
            statistics["max_drawdown_bps"] = _round_decimal(drawdown * 10_000)
        else:
            reasons.append("fewer than 2 prices for max drawdown")
        by_day = dict(prices)
        for days in (30, 90):
            previous = by_day.get(last_day - timedelta(days=days))
            # A missing horizon price is unknown; no nearest-day substitution or interpolation.
            if not stale:
                if previous is None:
                    reasons.append(f"no price exactly {days} days before the last price")
                else:
                    statistics[f"return_{days}d_bps"] = _round_decimal((last_price / previous - 1) * 10_000)
        daily_returns = [{"day": day.isoformat(), "return_bps": str(value.quantize(
            DAILY_RETURN_PLACES, rounding=ROUND_HALF_UP))} for day, value in returns]
        risk_series = history.return_series(
            row.get("chain_id"), address, position_id=requested_id, as_of=now,
            method_version="instrument-analytics/1",
            **({"instrument_id": requested_id} if not address else {}))
    if risk_series is not None:
        # The portfolio engine rates the trailing window only; return_series checks that window's
        # coverage but returns every stored day.
        window_start = today - timedelta(days=TRAILING_DAYS - 1)
        risk_statistics = return_statistics(
            [(item.day, Fraction(item.return_bps)) for item in risk_series.returns if item.day >= window_start],
            risk_series.annualization_factor)
        for key in ("expected_shortfall_95_bps", "annualized_volatility_bps"):
            statistics[key] = risk_statistics[key]
    elif not stale:
        if returns and (today - returns[-1][0]).days > MAX_LAG_DAYS:
            reasons.append(f"the daily return series ends on {returns[-1][0]}, more than {MAX_LAG_DAYS} days before today")
        else:
            reasons.append(f"fewer than {MIN_ES_OBSERVATIONS} daily returns in the trailing window")
    return {
        # A spliced path contains modelled prices and cannot claim to be wholly observed.
        "provenance": "modelled" if series["provenance"] == "spliced" else series["provenance"],
        "source": series["source"], "method": series.get("method"), "prices": series["prices"],
        "daily_returns": daily_returns,
        "statistics": statistics, "statistics_reason": "; ".join(reasons) or None,
    }, None


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8516
MAX_BODY_BYTES = 1_048_576
MAX_IDEMPOTENCY_KEY_BYTES = 128
MAX_IDEMPOTENCY_ENTRIES = 256
ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
DEFAULT_UNIVERSE_PATH = WEB_ROOT / "universe.json"
FRESH = "FRESH"
STALE = "STALE"
EXPIRED = "EXPIRED"
MIN_SIZE_USD = 1
MAX_SIZE_USD = 1_000_000_000
PRICEABILITY_REFERENCE_SIZE_USD = 10_000
INSTRUMENT_ANALYTICS_SIZES = (1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000,
                              500000, 1000000, 2500000, 5000000)
# Where the acquisition jobs publish, beside the universe: a directory they own, inside the one
# the platform already mounts read-only.
ACQUIRE_DIRECTORY = "acquire"
POST_PATHS = frozenset(
    {
        "/v1/wallet-snapshots",
        "/v1/wallet-history",
        "/v1/portfolio-analytics",
        "/v1/scenarios",
        "/v1/opportunities",
        "/v1/execution-quotes",
        "/v1/execution-routes",
        "/v1/sale-routes",
        "/v1/route-previews",
        "/v1/routes/submitted",
        "/v1/position-deposits",
        "/v1/position-exits",
        "/v1/lending-orders",
        "/v1/lending-orders/publish",
        "/v1/lending-orders/cancel",
        "/v1/lending-orders/list",
        "/v1/decision-bundles",
    }
)
# Opting in to outbound requests is a deployment decision, and it defaults off. Without it the
# platform speaks to nothing, exactly as it did before `/v1/execution-routes` existed; with the
# router absent that endpoint refuses rather than inventing a route.
ASSISTANT_BACKEND_VARIABLE = "FENORIUM_ASSISTANT_BACKEND"
ASSISTANT_URL_VARIABLE = "FENORIUM_ASSISTANT_URL"
ASSISTANT_PAIRING_TOKEN_VARIABLE = "FENORIUM_ASSISTANT_PAIRING_TOKEN"
GEMINI_API_KEY_VARIABLE = "FENORIUM_GEMINI_API_KEY"
GEMINI_MODEL_VARIABLE = "FENORIUM_GEMINI_MODEL"
ASSISTANT_ACCESS_KEY_VARIABLE = "FENORIUM_ASSISTANT_ACCESS_KEY"
ASSISTANT_DAILY_LIMIT_VARIABLE = "FENORIUM_ASSISTANT_DAILY_LIMIT"
ROUTER_ENVIRONMENT_VARIABLE = "FENORIUM_ROUTER"
LIFI_ROUTER = "lifi"
TENOR_MARKETS_VARIABLE = "FENORIUM_TENOR_MARKETS"
# Provider clients use fixed URLs. Opt-in grants only these hosts; callers cannot supply one.
OUTBOUND_PROVIDER_HOSTS = {"lifi": frozenset({"li.quest"}),
                           "morpho": frozenset({"api.morpho.org"})}
LIFI_API_KEY_VARIABLE = "FENORIUM_LIFI_API_KEY"
# The integration's name exactly as the LI.FI Partner Portal fixed it at creation. Unset, routes
# are filed under LI.FI's default and attributed to nobody.
LIFI_INTEGRATOR_VARIABLE = "FENORIUM_LIFI_INTEGRATOR"
# The routing fee, as a fraction of each purchase. Unset means none is charged and the platform
# answers with the v1 envelope, whose `fees_enabled` can only be false.
LIFI_FEE_VARIABLE = "FENORIUM_LIFI_FEE"
# Reading wallets makes this server call public RPCs on every chain it reads. Off unless a
# deployment says `on`, exactly as the router is.
WALLET_READS_VARIABLE = "FENORIUM_WALLET_READS"
# The deployment's own LI.FI allowance, shared by every caller. Defaults sit well inside what an
# API key is allowed, so this server is refused by its own budget before LI.FI ever refuses it.
LIFI_BUDGET_MINUTE_VARIABLE = "FENORIUM_LIFI_BUDGET_PER_MINUTE"
LIFI_BUDGET_DAY_VARIABLE = "FENORIUM_LIFI_BUDGET_PER_DAY"
DEFAULT_LIFI_PER_MINUTE = 30
DEFAULT_LIFI_PER_DAY = 5_000
# Routing state (LI.FI allowance, route records) beside the purchase evidence, on its volume.
ROUTING_STATE_FILE = "routing.db"
ORDER_STATE_FILE = "lending-orders.db"


def _port(value: str | None) -> int:
    try:
        parsed = DEFAULT_PORT if value is None else int(value)
    except ValueError as exc:
        raise ValueError("FENORIUM_PLATFORM_PORT must be an integer") from exc
    if not 1 <= parsed <= 65_535:
        raise ValueError("FENORIUM_PLATFORM_PORT must be in [1, 65535]")
    return parsed


def _evidence_dir(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    request_digest: str
    status: int
    payload: bytes


class _IdempotencyCache:
    """Bounded, process-local replay protection; request bodies are not persisted."""

    def __init__(self, maximum: int = MAX_IDEMPOTENCY_ENTRIES) -> None:
        self._maximum = maximum
        self._entries: OrderedDict[tuple[str, str], _CachedResponse] = OrderedDict()
        self._lock = RLock()

    def get(self, path: str, key: str, digest: str) -> _CachedResponse | None:
        with self._lock:
            cached = self._entries.get((path, key))
            if cached is None:
                return None
            if cached.request_digest != digest:
                raise APIError(
                    "IDEMPOTENCY_CONFLICT",
                    "the idempotency key was already used for a different request",
                    status=HTTPStatus.CONFLICT,
                )
            self._entries.move_to_end((path, key))
            return cached

    def put(self, path: str, key: str, response: _CachedResponse) -> None:
        with self._lock:
            self._entries[(path, key)] = response
            self._entries.move_to_end((path, key))
            while len(self._entries) > self._maximum:
                self._entries.popitem(last=False)


def _size_usd(query: str) -> int | None:
    values = parse_qs(query, keep_blank_values=True).get("size_usd")
    if values is None:
        return None
    if len(values) != 1:
        raise APIError(
            "INVALID_SIZE_USD",
            "size_usd must be supplied exactly once as a base-10 integer",
        )
    value = values[0]
    if not value or not value.isascii() or not value.isdigit():
        raise APIError(
            "INVALID_SIZE_USD",
            "size_usd must be a positive base-10 integer",
        )
    parsed = int(value)
    if not MIN_SIZE_USD <= parsed <= MAX_SIZE_USD:
        raise APIError(
            "SIZE_USD_OUT_OF_RANGE",
            f"size_usd must be between {MIN_SIZE_USD} and {MAX_SIZE_USD}",
        )
    return parsed


# Route requests and deposits spend the deployment's LI.FI allowance or build instructions; wallet
# reads call RPCs on every chain. Each class has its own per-client rate (deploy/platform/ratelimit.py).
_ROUTE_POSTS = frozenset({"/v1/execution-routes", "/v1/sale-routes", "/v1/route-previews",
                          "/v1/position-deposits", "/v1/position-exits", "/v1/lending-orders",
                          "/v1/lending-orders/publish", "/v1/lending-orders/cancel"})
# Listing a wallet's orders reads the store and the chain; it spends no provider allowance and the
# dashboard asks on every connect and refresh, so it is paced like other reads, not like a route.
SESSION_HEADER = "X-Fenorium-Session"
_SESSION = re.compile(r"[A-Za-z0-9_-]{8,64}")
# Peers whose X-Forwarded-For is believed: the reverse proxy reaches the container through Docker's
# port proxy, so its address is the default bridge's gateway, never loopback. Only that gateway:
# every other container on the bridge could otherwise claim to be a proxy, reach the host-only
# operations endpoints and choose its own client address. Other networks set the variable.
TRUSTED_PROXIES_VARIABLE = "FENORIUM_TRUSTED_PROXIES"
DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,172.17.0.1/32"


def _networks(raw: str) -> tuple[Any, ...]:
    networks = []
    for part in raw.split(","):
        try:
            networks.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


_CLOCK_DEPENDENT_PATHS = frozenset({"/v1/execution-quotes", "/v1/execution-routes", "/v1/sale-routes", "/v1/route-previews",
                                    "/v1/position-deposits", "/v1/position-exits", "/v1/lending-orders",
                                    "/v1/lending-orders/publish", "/v1/lending-orders/cancel",
                                    "/v1/lending-orders/list"})


class PlatformHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        api: PlatformAPI,
        web_root: Path = WEB_ROOT,
        universe_path: Path = DEFAULT_UNIVERSE_PATH,
        clock: Callable[[], datetime] | None = None,
        gate: ExecutionGate = CLOSED,
        acquisition_status_path: Path | None = None,
        wallets: Any = None,
        limits: ClientLimits | None = None,
        trusted_proxies: str = DEFAULT_TRUSTED_PROXIES,
        assistant: Any = None,
    ):
        self.assistant = assistant if assistant is not None else Assistant.from_env(os.environ)
        self.api = api
        self.wallets = wallets
        self.limits = limits or ClientLimits()
        self.trusted_proxies = _networks(trusted_proxies)
        self.gate = gate
        self.web_root = web_root.resolve()
        self.universe_path = Path(universe_path).resolve()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.idempotency = _IdempotencyCache()
        self.acquisition_status_path = acquisition_status_path
        self._registry_lock = RLock()
        self._registry_cache: tuple[tuple[str, str, bool, int], dict[str, Any]] | None = None
        self._registry_maturities: tuple[str, list[int]] | None = None
        super().__init__(address, PlatformRequestHandler)

    def registry(self, universe: dict[str, Any]) -> dict[str, Any]:
        """The registry for this universe, rebuilt when the universe, its freshness band or the UTC
        day or a maturity changes. An expired universe is one the purchase endpoints refuse to
        buy from, so its registry calls nothing buyable."""
        expired = universe.get("freshness") == EXPIRED
        now = self.clock().astimezone(UTC)
        source = str(universe.get("source_sha256"))
        with self._registry_lock:
            if self._registry_maturities is None or self._registry_maturities[0] != source:
                timestamps = sorted(row["maturity_timestamp"] for row in universe.get("instruments", [])
                                    if isinstance(row, dict) and type(row.get("maturity_timestamp")) is int
                                    and row["maturity_timestamp"] >= 0)
                self._registry_maturities = (source, timestamps)
            key = (source, now.date().isoformat(), expired,
                   bisect_right(self._registry_maturities[1], now.timestamp()))
            if self._registry_cache is None or self._registry_cache[0] != key:
                document = registry_document(universe, now=now, expired=expired)
                self._registry_cache = (key, document)
            return self._registry_cache[1]


class PlatformRequestHandler(SimpleHTTPRequestHandler):
    """Same-origin static delivery plus a bounded JSON API."""

    server: PlatformHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Method/path/status only. Query strings, request bodies, holdings, prompts, and tokens
        # are never logged. The stdlib's default request-line logger is intentionally not used.
        status = args[1] if len(args) > 1 else "-"
        print(f"[fenorium-platform] {self.command} {self._path()} {status}", flush=True)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' https:; img-src 'self' data:; "
            "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")

    def end_headers(self) -> None:
        self._security_headers()
        super().end_headers()

    def _send_json(self, status: int, payload: dict[str, Any], *, encoded: bytes | None = None) -> None:
        body = encoded if encoded is not None else json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: APIError) -> None:
        if error.code == "WALLET_READS_BUSY":
            self._wallet_busy()
            return
        self._send_json(
            int(error.status),
            {
                "schema_version": "fenorium/platform-error/1",
                "error": error.code,
                "message": str(error),
            },
        )

    def _wallet_busy(self) -> None:
        body = json_bytes({"schema_version": "fenorium/platform-error/1", "error": "WALLET_READS_BUSY",
                           "message": "wallet reads are busy", "retry_after_seconds": 5})
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "5")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _universe_error(self, error: APIError) -> None:
        self._send_json(
            int(error.status),
            {
                "schema_version": "fenorium/platform-error/1",
                "error": error.code,
                "reason_code": error.code,
                "message": str(error),
            },
        )

    def _universe(self, size_usd: int | None = None) -> dict[str, Any]:
        try:
            raw = self.server.universe_path.read_bytes()
        except FileNotFoundError as error:
            raise APIError(
                "UNIVERSE_FILE_MISSING",
                "the configured universe file does not exist",
                status=HTTPStatus.NOT_FOUND,
            ) from error
        except OSError as error:
            raise APIError(
                "UNIVERSE_FILE_UNREADABLE",
                "the configured universe file could not be read",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            ) from error

        source_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise APIError(
                "UNIVERSE_FILE_UNPARSEABLE",
                "the configured universe file is not valid UTF-8 JSON",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            ) from error
        if not isinstance(document, dict):
            raise APIError(
                "UNIVERSE_FILE_INVALID",
                "the configured universe file must contain a JSON object",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        generated_at = document.get("generated_at")
        instruments = document.get("instruments")
        if not isinstance(generated_at, str):
            raise APIError(
                "UNIVERSE_FILE_INVALID",
                "the configured universe file requires generated_at",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        try:
            generated_datetime = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise APIError(
                "UNIVERSE_FILE_INVALID",
                "the configured universe generated_at is not an ISO-8601 timestamp",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            ) from error
        if generated_datetime.tzinfo is None or generated_datetime.utcoffset() is None:
            raise APIError(
                "UNIVERSE_FILE_INVALID",
                "the configured universe generated_at must include a timezone",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        input_freshness = document.get("input_freshness")
        if input_freshness is not None:
            if not isinstance(input_freshness, dict):
                raise APIError(
                    "UNIVERSE_FILE_INVALID",
                    "the configured universe input_freshness must be an object",
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            oldest_input = input_freshness.get("oldest_cache_entry_at")
            if not isinstance(oldest_input, str):
                raise APIError(
                    "UNIVERSE_FILE_INVALID",
                    "the configured universe input freshness requires oldest_cache_entry_at",
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            try:
                oldest_input_datetime = datetime.fromisoformat(oldest_input.replace("Z", "+00:00"))
            except ValueError as error:
                raise APIError(
                    "UNIVERSE_FILE_INVALID",
                    "the configured universe oldest cache input timestamp is invalid",
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                ) from error
            if oldest_input_datetime.tzinfo is None or oldest_input_datetime.utcoffset() is None:
                raise APIError(
                    "UNIVERSE_FILE_INVALID",
                    "the configured universe oldest cache input timestamp needs a timezone",
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            input_freshness = dict(input_freshness)
            input_freshness["age_seconds"] = max(
                0, int((self.server.clock() - oldest_input_datetime).total_seconds())
            )
        if not isinstance(instruments, list):
            raise APIError(
                "UNIVERSE_FILE_INVALID",
                "the configured universe instruments field must be a list",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if not instruments:
            raise APIError(
                "UNIVERSE_FILE_EMPTY",
                "the configured universe instruments list must not be empty",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if any(not isinstance(instrument, dict) for instrument in instruments):
            raise APIError(
                "UNIVERSE_FILE_INVALID",
                "the configured universe instruments list must contain objects",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        age_seconds = max(0, int((self.server.clock() - generated_datetime).total_seconds()))
        if age_seconds < FRESH_UNDER_SECONDS:
            freshness = FRESH
        elif age_seconds < STALE_UNDER_SECONDS:
            freshness = STALE
        else:
            freshness = EXPIRED
        # Copy rows before normalizing explicit UNRATED entries, preserving the source snapshot.
        served_instruments = [dict(instrument) for instrument in instruments]
        chains = registry()
        for row in served_instruments:
            try:
                row["instrument_id"] = instrument_id(row, chains)
            except Exception:
                row["instrument_id"] = None
            if str(row.get("verdict") or "").upper() == "UNRATED":
                row["fenorium_score"] = None
                row["score_cap"] = None
        response = dict(document)
        response["instruments"] = served_instruments
        if input_freshness is not None:
            response["input_freshness"] = input_freshness
        response.update(
            {
                "source": str(self.server.universe_path),
                "source_sha256": source_sha256,
                "generated_at": generated_at,
                "age_seconds": age_seconds,
                "freshness": freshness,
                "freshness_thresholds": {
                    "fresh_under_seconds": FRESH_UNDER_SECONDS,
                    "stale_under_seconds": STALE_UNDER_SECONDS,
                },
                "instrument_count": len(instruments),
            }
        )
        if size_usd is not None:
            # Only rows rated here get today's day count: an unsized read keeps the snapshot's
            # days beside the snapshot's ratings.
            restate_declared_days(served_instruments, self.server.clock().astimezone(UTC).date())
            rerated, priceable_count = _rerated_rows(served_instruments, size_usd)
            _, reference_priceable_count = _rerated_rows(served_instruments, PRICEABILITY_REFERENCE_SIZE_USD)
            assumptions = _rating_assumptions(size_usd).to_json()
            assumptions.update(
                {
                    "size_usd": size_usd,
                    "accept_realized": True,
                    "execution_cost_model": EXECUTION_COST_MODEL,
                }
            )
            verdicts = ("BID", "NO BID", "NO QUOTE", "UNRATED")
            verdict_counts = {
                verdict: sum(
                    1 for row in rerated
                    if str(row.get("verdict") or "UNRATED").upper() == verdict
                )
                for verdict in verdicts
            }
            response.update(
                {
                    "instruments": rerated,
                    "size_usd": size_usd,
                    "assumptions": assumptions,
                    "rating_method": {
                        "method_version": RATING_METHOD_VERSION,
                        "model": IMPACT_MODEL,
                        "mode": "FULL_RECOMPUTE",
                    },
                    "source_rating_method": (
                        dict(document["rating_method"], status="RECORDED")
                        if isinstance(document.get("rating_method"), dict)
                        and isinstance(document["rating_method"].get("method_version"), str)
                        else {"method_version": None, "status": "UNRECORDED_LEGACY"}
                    ),
                    "priceable_count": priceable_count,
                    "refused_count": len(rerated) - priceable_count,
                    "rating_counts": {
                        "computable": priceable_count,
                        "refused": len(rerated) - priceable_count,
                    },
                    "verdict_counts": verdict_counts,
                    "priceability_reference": {
                        "size_usd": PRICEABILITY_REFERENCE_SIZE_USD,
                        "priceable_count": reference_priceable_count,
                        "delta": priceable_count - reference_priceable_count,
                    },
                }
            )
        return response

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _client_ip(self) -> str:
        """The caller's address: the peer's, unless the peer is a trusted proxy that forwarded it."""
        peer = self.client_address[0] if self.client_address else ""

        def trusted(text: str) -> bool:
            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                return False
            return any(address in network for network in self.server.trusted_proxies)

        if not trusted(peer):
            return peer
        forwarded = [part.strip() for part in (self.headers.get("X-Forwarded-For") or "").split(",") if part.strip()]
        # The rightmost address a trusted proxy did not add is the one the client reached it from.
        for candidate in reversed(forwarded):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                return peer
            if not trusted(candidate):
                return candidate
        return peer

    def _limited(self, kind: str, *, wallet: str | None = None) -> bool:
        """Spend one request of `kind`; when refused, answer 429 with Retry-After and return True."""
        session = self.headers.get(SESSION_HEADER)
        try:
            self.server.limits.check(kind, ip=self._client_ip(), wallet=wallet,
                                     session=session if session and _SESSION.fullmatch(session) else None)
        except Throttled as exc:
            body = json_bytes({"schema_version": "fenorium/platform-error/1", "error": exc.code,
                               "message": str(exc), "retry_after_seconds": exc.retry_after})
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", str(exc.retry_after))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return True
        return False

    def _local_caller(self) -> bool:
        """A request from this host: a trusted peer that forwarded no client address."""
        peer = self.client_address[0] if self.client_address else ""
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        trusted = any(address in network for network in self.server.trusted_proxies)
        return trusted and not (self.headers.get("X-Forwarded-For") or "").strip()

    def _routes(self, path: str) -> dict[str, Any]:
        store = self.server.api.routes
        if store is None:
            raise APIError("ROUTES_UNAVAILABLE", "this deployment does not keep route records", status=HTTPStatus.SERVICE_UNAVAILABLE)
        if path.startswith("/v1/wallets/"):
            wallet = path[len("/v1/wallets/"):-len("/routes")]
            if re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet) is None:
                raise APIError("INVALID_FIELD", "the wallet must be a 0x-prefixed 20-byte address")
            # Submitted hashes are untrusted claims until the tracker verifies the transaction.
            routes = store.by_wallet(wallet, limit=100, exclude_states=("BUILT", "REFUSED"), verified_only=True)
            return {"schema_version": "fenorium/wallet-routes/1", "wallet": wallet.lower(), "routes": routes}
        route_id = path[len("/v1/routes/"):]
        if re.fullmatch(r"[0-9a-f]{32}", route_id) is None:
            raise APIError("INVALID_FIELD", "a route id is 32 lowercase hex characters")
        record = store.get(route_id)
        if record is None:
            raise APIError("ROUTE_NOT_FOUND", "no route with this id", status=HTTPStatus.NOT_FOUND)
        return record

    def _wallet_portfolio(self, path: str, query: dict[str, list[str]]) -> dict[str, Any] | None:
        reader = self.server.wallets
        if reader is None:
            raise APIError("WALLET_READS_DISABLED", "this deployment does not read wallets", status=HTTPStatus.NOT_IMPLEMENTED)
        wallet = path[len("/v1/wallets/"):-len("/portfolio")]
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet) is None:
            raise APIError("INVALID_FIELD", "the wallet must be a 0x-prefixed 20-byte address")
        chains = None
        if "chains" in query:
            values = query["chains"]
            if len(values) != 1:
                raise APIError("INVALID_FIELD", "chains must be given once")
            try:
                chains = sorted({int(text) for text in values[0].split(",") if text})
            except ValueError as exc:
                raise APIError("INVALID_FIELD", "chains must be comma-separated chain ids") from exc
            unknown = set(chains) - set(reader.chains())
            if not chains or unknown:
                raise APIError("INVALID_FIELD", "chains must name chains this deployment reads wallets on")
        try:
            document = self.server.registry(self._universe())
        except APIError:
            # Without a universe the wallet is still read for every chain's cash and routable tokens.
            document = registry_document(None, now=self.server.clock())
        try:
            return reader.read(wallet, document, chain_ids=chains)
        except WalletBusy:
            self._wallet_busy()
            return None

    def _read_endpoint(self, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        def single(name: str, *, required: bool = False) -> str | None:
            values = query.get(name)
            if values is None:
                if required:
                    raise APIError("MISSING_FIELD", f"{name} is required")
                return None
            if len(values) != 1 or not values[0] or len(values[0]) > 128:
                raise APIError("INVALID_FIELD", f"{name} must be given once")
            return values[0]

        def asset(name: str = "asset_id") -> tuple[str, int, str | None]:
            text = single(name, required=True)
            try:
                chain_id, address = parse_asset_id(text)
            except ValueError as exc:
                raise APIError("INVALID_FIELD", str(exc)) from exc
            return text, chain_id, address

        if path == "/v1/instrument-analytics":
            ids = query.get("instrument_id")
            if ids is None:
                raise APIError("MISSING_FIELD", "instrument_id is required")
            if len(ids) != 1 or not ids[0] or len(ids[0]) > 512:
                raise APIError("INVALID_FIELD", "instrument_id must be given once and contain at most 512 characters")
            requested_id = ids[0]
            universe = self._universe()
            matches = []
            chains = registry()
            for row in universe["instruments"]:
                try:
                    row_id = instrument_id(row, chains)
                except Exception:
                    continue
                if row_id == requested_id:
                    matches.append(row)
            if not matches:
                raise APIError("INSTRUMENT_NOT_IN_UNIVERSE", "no instrument with this instrument_id",
                               status=HTTPStatus.NOT_FOUND)
            if len(matches) > 1:
                raise APIError("INSTRUMENT_AMBIGUOUS", "more than one instrument has this instrument_id",
                               status=HTTPStatus.UNPROCESSABLE_ENTITY)
            row = dict(matches[0])
            now = self.server.clock()
            restate_declared_days([row], now.astimezone(UTC).date())
            def rated_at(size: int) -> dict[str, Any]:
                rated = _rerated_rows([row], size)[0][0]
                return {"size_usd": size, **{key: rated.get(key) for key in (
                    "rating", "verdict", "fenorium_score", "score_cap")}}

            points = [rated_at(size) for size in INSTRUMENT_ANALYTICS_SIZES]
            # The page's own notional is rated in the same snapshot, whether or not it is on the grid.
            selected_size = _size_usd(urlsplit(self.path).query)
            history, reason = _instrument_price_history(self.server.api.price_history, row, requested_id, now)
            return {
                "schema_version": "fenorium/instrument-analytics/1",
                "instrument_id": requested_id, "generated_at": universe["generated_at"],
                "source_sha256": universe["source_sha256"],
                "size_curve": {
                    "sizes_usd": list(INSTRUMENT_ANALYTICS_SIZES), "points": points,
                    "reported_depth_usd": row.get("depth_usd"),
                    "days_to_maturity": row.get("days_to_maturity"),
                    "holding_period_days": HOLDING_PERIOD_DAYS,
                },
                "selected": None if selected_size is None else rated_at(selected_size),
                "terms": fixed_income_terms(row, chain_id=row.get("chain_id"), now=now),
                "price_history": history, "price_history_reason": reason,
            }
        if path == "/v1/prices/current":
            prices = self.server.api.current_prices
            document = None if prices is None else prices.document()
            if document is None:
                raise APIError("PRICES_UNAVAILABLE", "no current prices are published", status=HTTPStatus.SERVICE_UNAVAILABLE)
            if "asset_id" not in query:
                return document
            text, _, _ = asset()
            return {"asset_id": text, "generated_at": document.get("generated_at"),
                    "price": document["assets"].get(text) or {"status": "UNPRICED", "reason": "NOT_TRACKED"}}
        if path == "/v1/prices/history":
            text, chain_id, address = asset()
            history = self.server.api.price_history
            if history is None:
                raise APIError("PRICES_UNAVAILABLE", "no price history is published", status=HTTPStatus.SERVICE_UNAVAILABLE)
            at = single("at")
            if at is not None:
                moment = _query_time(at, "at")
                point = history.price_at(chain_id, address, moment)
                return {"asset_id": text, "at": at, "price": point,
                        "reason": None if point is not None else "NO_PRICE_NEAR_THIS_MOMENT"}
            start, end = single("from"), single("to")
            series = history.series(chain_id, address, start=_query_day(start, "from"), end=_query_day(end, "to"))
            return {"asset_id": text, "series": series, "reason": None if series is not None else "NO_HISTORY"}
        if path == "/v1/ops/fees":
            store = self.server.api.routes
            balances_path = None if self.server.acquisition_status_path is None else \
                self.server.acquisition_status_path.parent / "fee-balances.json"
            try:
                balances = json.loads(balances_path.read_text(encoding="utf-8")) if balances_path else None
            except (OSError, ValueError):
                balances = None
            return {
                "schema_version": "fenorium/ops-fees/1",
                "ledger": None if store is None else {
                    "by_status": store.summary()["fees_by_status"],
                    "entries": store.fee_entries(limit=200),
                },
                "recipient_balances": balances,
            }
        status_path = self.server.acquisition_status_path
        try:
            status = json.loads(status_path.read_text(encoding="utf-8")) if status_path is not None else None
        except (OSError, ValueError):
            status = None
        router = self.server.api.router
        budget = getattr(router, "budget", None)
        cache = getattr(router, "cache", None)
        store = self.server.api.routes
        in_flight, oldest = (0, None) if store is None else store.in_flight()
        return {
            "schema_version": "fenorium/ops-status/1",
            "service": "fenorium-platform",
            "execution_enabled": self.server.gate.is_open_for(cross_chain=False),
            "acquisition": status,
            "routing": {
                "router": None if router is None else type(router).__name__,
                "client_requests": dict(sorted(self.server.limits.counters.items())),
                "provider_budget": None if budget is None else budget.status(),
                "route_cache": None if cache is None else dict(cache.counters),
                "routes": None if store is None else {
                    **store.summary(), "in_flight": in_flight,
                    "oldest_in_flight_seconds": None if oldest is None else max(0, int(
                        (self.server.clock() - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds())),
                },
            },
        }

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib hook
        # The platform terminal is same-origin. It intentionally exposes no CORS surface.
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "CORS_DISABLED"})

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        path = self._path()
        if path.startswith("/v1/"):
            wallet_read = path.startswith("/v1/wallets/") and path.endswith("/portfolio")
            kind = "wallet-read" if wallet_read else "route" if path.startswith("/v1/executions/") else "general"
            wallet = path[len("/v1/wallets/"):-len("/portfolio")] if wallet_read else None
            if self._limited(kind, wallet=wallet):
                return
        if path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "fenorium-platform",
                    "version": "0.1.0",
                    "execution_enabled": self.server.gate.is_open_for(cross_chain=False),
                },
            )
            return
        if path == "/v1/assistant":
            data = {"schema_version": "fenorium/assistant-status/1", **self.server.assistant.status()}
            self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path == "/v1/universe":
            try:
                size_usd = _size_usd(urlsplit(self.path).query)
                data = self._universe(size_usd)
            except APIError as error:
                self._universe_error(error)
            else:
                # The API's envelope, so a deployment that charges a fee says so here too rather
                # than answering the universe as if it charged nothing.
                self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path == "/v1/registry":
            try:
                data = self.server.registry(self._universe())
            except APIError as error:
                self._universe_error(error)
            else:
                self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path.startswith("/v1/ops/") and not self._local_caller():
            # Operations data -- volumes, fees, failures -- is for this host's own tooling, which
            # reaches the service directly rather than through the public proxy.
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        if path.startswith("/v1/routes/") or (path.startswith("/v1/wallets/") and path.endswith("/routes")):
            try:
                data = self._routes(path)
            except APIError as error:
                self._error(error)
            else:
                self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path.startswith("/v1/wallets/") and path.endswith("/portfolio"):
            try:
                data = self._wallet_portfolio(path, parse_qs(urlsplit(self.path).query, keep_blank_values=True))
            except APIError as error:
                self._error(error)
            else:
                if data is not None:
                    self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path in ("/v1/instrument-analytics", "/v1/prices/current", "/v1/prices/history", "/v1/ops/status", "/v1/ops/fees"):
            try:
                data = self._read_endpoint(path, parse_qs(urlsplit(self.path).query, keep_blank_values=True))
            except APIError as error:
                if path == "/v1/instrument-analytics" and error.code.startswith("UNIVERSE_"):
                    self._universe_error(error)
                else:
                    self._error(error)
            else:
                self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path.startswith("/v1/executions/"):
            gate = self.server.gate
            if not gate.is_open_for(cross_chain=False):
                # Closed means clearances are outstanding, and this names them.
                self._send_json(
                    HTTPStatus.NOT_IMPLEMENTED,
                    {
                        "schema_version": "fenorium/platform-error/1",
                        "error": "EXECUTION_DISABLED",
                        "reason_codes": list(gate.reason_codes_for(cross_chain=False)),
                    },
                )
                return
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)

            def single(name: str) -> str | None:
                values = query.get(name)
                return values[0] if values is not None and len(values) == 1 else None

            try:
                data = self.server.api.execution_status(
                    unquote(path[len("/v1/executions/"):]),
                    from_chain=single("from_chain"), to_chain=single("to_chain"),
                )
            except APIError as error:
                self._error(error)
                return
            self._send_json(HTTPStatus.OK, self.server.api.envelope(data))
            return
        if path.startswith("/v1/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        self._serve_static(path)

    def _serve_static(self, path: str) -> None:
        decoded = unquote(path)
        relative = "index.html" if decoded in ("", "/") else decoded.lstrip("/")
        candidate = (self.server.web_root / relative).resolve()
        try:
            candidate.relative_to(self.server.web_root)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file() or candidate.name.startswith("."):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if candidate.name.endswith((".html", ".js")) else "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _assistant_chat(self, raw: bytes) -> None:
        session = None
        monitor = None
        finished = Event()
        started = False
        try:
            try:
                body = _decode_request_body(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise APIError("INVALID_JSON", "request body is not valid UTF-8 JSON")
            if self._limited("assistant"):
                return
            session = self.server.assistant.prepare(body, self.headers.get("X-Fenorium-Assistant-Key"))
            def watch_disconnect():
                while not finished.wait(0.1):
                    try:
                        readable, _, _ = select.select([self.connection], [], [], 0)
                        if readable and not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                            session.cancel()
                            return
                    except (OSError, ValueError):
                        return

            if hasattr(session, "cancel"):
                monitor = Thread(target=watch_disconnect, daemon=True)
                monitor.start()
            if hasattr(session, "open"):
                session.open()
            if getattr(session, "closed", False):
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            self.close_connection = True
            for event in session:
                self.wfile.write(json_bytes(event) + b"\n")
                self.wfile.flush()
        except AssistantError as error:
            if started:
                self.wfile.write(json_bytes({"type": "error", "code": error.code,
                                             "message": str(error)}) + b"\n")
                self.wfile.flush()
            else:
                payload = {"schema_version": "fenorium/platform-error/1", "error": error.code,
                           "message": str(error)}
                encoded = json_bytes(payload)
                self.send_response(error.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                if error.retry_after is not None:
                    self.send_header("Retry-After", str(error.retry_after))
                self.end_headers()
                self.wfile.write(encoded)
        except APIError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            finished.set()
            if monitor is not None:
                monitor.join(0.2)
            if session is not None and hasattr(session, "close"):
                session.close()

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        path = self._path()
        if path not in POST_PATHS and path != "/v1/assistant/chat":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self._error(APIError("CONTENT_TYPE_REQUIRED", "application/json is required", status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE))
            return
        key = self.headers.get("Idempotency-Key")
        if key is None or not key or len(key.encode("utf-8")) > MAX_IDEMPOTENCY_KEY_BYTES or any(ord(character) < 33 for character in key):
            self._error(APIError("IDEMPOTENCY_KEY_REQUIRED", "a bounded visible Idempotency-Key header is required"))
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(APIError("CONTENT_LENGTH_REQUIRED", "Content-Length is required", status=HTTPStatus.LENGTH_REQUIRED))
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._error(APIError("INVALID_CONTENT_LENGTH", "Content-Length must be an integer"))
            return
        if path == "/v1/assistant/chat" and (length < 0 or length > 64 * 1024):
            self._error(APIError("INVALID_FIELD", "assistant request must be at most 64 KiB"))
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(APIError("BODY_TOO_LARGE", "request body exceeds the 1 MiB limit", status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE))
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._error(APIError("INCOMPLETE_BODY", "request body ended before Content-Length"))
            return
        if path == "/v1/assistant/chat":
            self._assistant_chat(raw)
            return
        digest = hashlib.sha256(raw).hexdigest()
        try:
            cached = self.server.idempotency.get(path, key, digest)
            if cached is not None and path not in _CLOCK_DEPENDENT_PATHS:
                self._send_json(cached.status, {}, encoded=cached.payload)
                return
            try:
                body = _decode_request_body(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise APIError("INVALID_JSON", "request body is not valid UTF-8 JSON") from exc
            wallet = body.get("wallet") if isinstance(body, dict) and isinstance(body.get("wallet"), str) else None
            rate_class = ("route-report" if path == "/v1/routes/submitted"
                          else "route" if path in _ROUTE_POSTS
                          else "wallet-read" if path == "/v1/lending-orders/list"
                          or (path == "/v1/wallet-history"
                              and self.server.api.transfer_verifier is not None) else "general")
            if self._limited(rate_class, wallet=wallet):
                return
            if path == "/v1/lending-orders":
                result = self.server.api.envelope(self.server.api.lending_order(
                    body, request_key=key, request_digest=digest))
            else:
                result = self.server.api.handle(path, body)
            encoded = json_bytes(result)
            response = _CachedResponse(digest, HTTPStatus.OK, encoded)
            # Always recorded, because the entry is what detects the same key being reused for
            # a different request. What a clock-dependent path declines is being *served* from
            # it: replaying one hands back a freshness verdict computed at an earlier moment,
            # and the same key after expiry returned a quote still described as live.
            self.server.idempotency.put(path, key, response)
            self._send_json(response.status, result, encoded=encoded)
        except APIError as error:
            self._error(error)
        except (TypeError, ValueError) as error:
            # Engine contract failures become bounded validation errors; raw payloads are omitted.
            self._error(APIError("CONTRACT_REJECTED", str(error)[:512]))


def _query_day(value: str | None, name: str):
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise APIError("INVALID_FIELD", f"{name} must be a YYYY-MM-DD date") from exc


def _query_time(value: str, name: str) -> datetime:
    if not value.endswith("Z"):
        raise APIError("INVALID_FIELD", f"{name} must be a UTC ISO-8601 time ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise APIError("INVALID_FIELD", f"{name} must be a UTC ISO-8601 time ending in Z") from exc


class _JsonFraction(str):
    """A number that was written with a fraction, remembered as the lexeme it was written as.

    Decoding with `parse_float=str` outright would have let a fractional analytical value in
    where a float was previously refused -- `transformation.amount_usd_micros: 1.5` would stop
    being rejected and start being hashed into a decision bundle as "1.5". Marking the value
    instead keeps the two cases apart: it is still a string, so the lexeme survives exactly, and
    it is still recognisable, so it can be refused everywhere it has no business being.
    """


# Router evidence is the one place a fraction is legitimate: every LI.FI quote carries
# `action.slippage` and a `feePercent` per step. It is a vendor's record of what it offered,
# not one of our figures, and nothing computes with it.
_FRACTION_ALLOWED_AT = frozenset({"router_quote"})


def _reject_stray_fractions(node: object, *, path: str) -> None:
    if isinstance(node, _JsonFraction):
        raise APIError("INVALID_FIELD", f"{path or 'request'} must not be a fractional number")
    if isinstance(node, dict):
        for key, value in node.items():
            if path == "" and key in _FRACTION_ALLOWED_AT:
                continue
            _reject_stray_fractions(value, path=f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_stray_fractions(value, path=f"{path}[{index}]")


def _plain_strings(node: object) -> object:
    if isinstance(node, _JsonFraction):
        return str(node)
    if isinstance(node, dict):
        return {key: _plain_strings(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_plain_strings(value) for value in node]
    return node


def _decode_request_body(raw: bytes) -> object:
    """Decode a request body without ever producing a float, and without widening what is allowed.

    A fraction survives only inside router evidence, as the lexeme the router wrote. Anywhere
    else it is refused exactly as a float was before this existed.
    """
    body = json.loads(raw, parse_float=_JsonFraction)
    _reject_stray_fractions(body, path="")
    return _plain_strings(body)


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    evidence_dir: Path | None = None,
    web_root: Path = WEB_ROOT,
    universe_path: Path = DEFAULT_UNIVERSE_PATH,
    clock: Callable[[], datetime] | None = None,
    gate: ExecutionGate | None = None,
    router: Any = None,
    midnight_orders_enabled: bool = False,
    midnight_policy: Any = None,
    wallets: bool = False,
    trusted_proxies: str | None = None,
    limits: ClientLimits | None = None,
    assistant: Any = None,
) -> PlatformHTTPServer:
    resolved = from_environment() if gate is None else gate
    # One clock for the service and the API it wraps: freshness for authorisation is judged
    # against the server's, so a deployment that overrides it must override it everywhere.
    ticking = clock or (lambda: datetime.now(UTC))
    on_demand = OnDemandPrices()
    transfer_verifier = None
    history_source = None
    routable = None
    token_decimals = None
    if wallets:
        from research.par_sources.blockscout_history import BlockscoutWalletHistory, ReceiptTransferVerifier

        transfer_verifier = ReceiptTransferVerifier(timeout_seconds=8)
        history_source = BlockscoutWalletHistory(timeout_seconds=8)
        routable = RoutableTokens(Path(universe_path).parent / ACQUIRE_DIRECTORY / "lifi-tokens.json")

        def token_decimals(chain_id: int, address: str) -> int | None:
            token = routable.token(chain_id, address)
            decimals = None if token is None else token.get("decimals")
            return decimals if type(decimals) is int and decimals >= 0 else None
    # Wallet reads retain Midnight markets and PTs so matured balances survive universe removal.
    order_store = (OrderStore(Path(evidence_dir) / ORDER_STATE_FILE)
                   if evidence_dir is not None and (midnight_orders_enabled or wallets) else None)
    server = PlatformHTTPServer(
        (host, port),
        api=PlatformAPI(
            evidence_dir=evidence_dir,
            gate=resolved,
            clock=ticking,
            router=router,
            midnight_orders_enabled=midnight_orders_enabled,
            midnight_policy=midnight_policy,
            orders=order_store if midnight_orders_enabled else None,
            principal_token_store=order_store,
            # The same file the service already serves as `/v1/universe`, so what a buyer is
            # shown and what a route is fetched against cannot drift apart.
            instruments=InstrumentDirectory(universe_path),
            # Published beside the universe by the same nightly refresh.
            price_history=PriceHistory(Path(universe_path).parent / "price-history.json"),
            # Published every few minutes by the acquisition jobs, in their own directory under it.
            current_prices=CurrentPrices(Path(universe_path).parent / ACQUIRE_DIRECTORY / "prices-current.json"),
            on_demand=on_demand,
            transfer_verifier=transfer_verifier,
            history_source=history_source,
            token_decimals=token_decimals,
            # Kept beside the purchase evidence, on its volume, with the LI.FI allowance.
            routes=None if evidence_dir is None else RouteStore(Path(evidence_dir) / ROUTING_STATE_FILE, clock=ticking),
        ),
        gate=resolved,
        web_root=web_root,
        universe_path=universe_path,
        clock=ticking,
        acquisition_status_path=Path(universe_path).parent / ACQUIRE_DIRECTORY / ACQUISITION_STATUS_FILE,
        trusted_proxies=trusted_proxies or DEFAULT_TRUSTED_PROXIES,
        limits=limits,
        assistant=assistant,
    )
    if wallets:
        server.wallets = WalletReader(
            api=server.api,
            routable=routable,
            on_demand=on_demand,
            reader_factory=lambda chain: ChainReader(chain, timeout=8),
            clock=ticking,
            market_store=order_store,
        )
    return server


def _universe_path(raw: str | None) -> Path:
    return DEFAULT_UNIVERSE_PATH if not raw else Path(raw)


def _router(
    raw: str | None, api_key: str | None, integrator: str | None = None, fee: str | None = None,
    *, state_path: Path | None = None, environ: dict[str, str] | None = None,
):
    """Build the configured router, or none at all.

    Imported here rather than at module scope: the router reaches the network, and a deployment
    that has not asked for one should not even load the client that would make the call.
    `state_path`, when given, is where the LI.FI allowance is counted across restarts.
    """
    if (raw or "").strip().lower() != LIFI_ROUTER:
        return None
    from .ratelimit import ProviderBudget, SingleFlightCache
    from .router import ROUTE_CACHE_SECONDS, LifiRouter

    environ = os.environ if environ is None else environ
    return LifiRouter(
        api_key=api_key,
        integrator=(integrator or "").strip() or None,
        fee=(fee or "").strip() or None,
        budget=ProviderBudget(
            "LI.FI",
            per_minute=_positive(environ.get(LIFI_BUDGET_MINUTE_VARIABLE), DEFAULT_LIFI_PER_MINUTE),
            per_day=_positive(environ.get(LIFI_BUDGET_DAY_VARIABLE), DEFAULT_LIFI_PER_DAY),
            path=state_path,
        ),
        cache=SingleFlightCache(ROUTE_CACHE_SECONDS),
    )


def _positive(raw: str | None, default: int) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _midnight_orders_enabled(raw: str | None) -> bool:
    # Keep the deployed opt-in variable; it now enables Morpho Midnight orders.
    return (raw or "").strip().lower() == "on"


def _midnight_policy(raw: str | None, *, state_path: Path | None = None):
    if (raw or "").strip().lower() != "on":
        return None
    from .midnight_orders import MORPHO_MEMPOOL_VALIDATE, MidnightPolicy
    from .ratelimit import ProviderBudget

    if urlsplit(MORPHO_MEMPOOL_VALIDATE).hostname not in OUTBOUND_PROVIDER_HOSTS["morpho"]:
        raise ValueError("Morpho host is not allowed")
    return MidnightPolicy(budget=ProviderBudget("Morpho mempool", per_minute=30, per_day=5_000, path=state_path))


def main() -> None:
    host = os.environ.get("FENORIUM_PLATFORM_HOST", DEFAULT_HOST)
    port = _port(os.environ.get("FENORIUM_PLATFORM_PORT"))
    evidence_dir = _evidence_dir(os.environ.get("FENORIUM_EVIDENCE_DIR"))
    universe_path = _universe_path(os.environ.get("FENORIUM_UNIVERSE_PATH"))
    router = _router(
        os.environ.get(ROUTER_ENVIRONMENT_VARIABLE),
        os.environ.get(LIFI_API_KEY_VARIABLE),
        os.environ.get(LIFI_INTEGRATOR_VARIABLE),
        os.environ.get(LIFI_FEE_VARIABLE),
        state_path=None if evidence_dir is None else evidence_dir / ROUTING_STATE_FILE,
    )
    server = create_server(
        host=host, port=port, evidence_dir=evidence_dir, universe_path=universe_path,
        router=router,
        midnight_orders_enabled=_midnight_orders_enabled(os.environ.get(TENOR_MARKETS_VARIABLE)),
        midnight_policy=_midnight_policy(os.environ.get(TENOR_MARKETS_VARIABLE),
                                         state_path=None if evidence_dir is None else evidence_dir / ROUTING_STATE_FILE),
        wallets=os.environ.get(WALLET_READS_VARIABLE, "").strip().lower() == "on",
        trusted_proxies=os.environ.get(TRUSTED_PROXIES_VARIABLE) or None,
    )
    if server.api.routes is not None and router is not None:
        # Follows submitted purchases to an end: the source receipt, LI.FI's status, the fee.
        RouteTracker(server.api.routes, server.api).start()
    print(
        f"[fenorium-platform] listening on http://{host}:{port} "
        f"execution_enabled={str(server.gate.is_open_for(cross_chain=False)).lower()} "
        f"cross_chain_enabled={str(server.gate.is_open_for(cross_chain=True)).lower()} "
        f"router={LIFI_ROUTER if router is not None else 'none'} "
        f"fee={router.fee if router is not None and router.fee else 'none'} "
        f"missing={','.join(server.gate.missing) or 'none'}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
