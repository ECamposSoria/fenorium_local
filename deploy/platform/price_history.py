"""Daily return histories for the analytics endpoint, read from the served price history.

The browser holds no history, so it sends no return series, and without them volatility and
expected shortfall can never compute. This reads the artifact the nightly refresh publishes next
to the universe (engine/price_history.py) and turns a position's daily prices into integer
basis-point returns. Consecutive days only: a gap is skipped, never filled.

A series more than MAX_LAG_DAYS behind the request is not used. The engine refuses a portfolio
whose latest aligned day is older than that, so one delisted token's history, ending months ago,
would otherwise drag every holder's whole window into the past.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
import json
from pathlib import Path
from threading import RLock
from typing import Any

from engine.portfolio import DailyReturn, ReturnSeries
from engine.portfolio.contracts import PRICE_SCALE
from engine.portfolio.analytics import MIN_ES_OBSERVATIONS, MarkingFrequency
from engine.price_history import history_key

MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_LAG_DAYS = 2
# The engine's trailing window. It aligns every included series on their common days, so one
# young token's 120 days would cut a whole portfolio's window to 120 and fail it; a history too
# short to qualify on its own is not attached, and its position stays in the NAV denominator.
TRAILING_DAYS = 365
BPS = Decimal(10_000)
# A historical value is read at the daily price nearest its moment, and only that near: three
# days either side covers a weekend's gap in a source's series and nothing more.
MAX_PRICE_GAP_SECONDS = 3 * 86_400
PRICE_AT_METHOD = "price-history/utc-daily-nearest-v1"


class PriceHistory:
    """Reloads the artifact when its file changes; an absent or unreadable file means no history."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._stamp: tuple[int, int] | None = None
        self._prices: dict[str, tuple[tuple[date, Decimal], ...]] = {}
        self._sources: dict[str, str] = {}
        self._provenance: dict[str, str] = {}
        self._methods: dict[str, str | None] = {}
        self._modelled_before: dict[str, date] = {}

    def _load(self) -> dict[str, tuple[tuple[date, Decimal], ...]]:
        with self._lock:
            try:
                stat = self.path.stat()
            except OSError:
                self._stamp, self._prices, self._sources = None, {}, {}
                self._provenance, self._methods = {}, {}
                self._modelled_before = {}
                return self._prices
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return self._prices
            prices: dict[str, tuple[tuple[date, Decimal], ...]] = {}
            sources: dict[str, str] = {}
            provenance: dict[str, str] = {}
            methods: dict[str, str | None] = {}
            modelled_before: dict[str, date] = {}
            if stat.st_size <= MAX_HISTORY_BYTES:
                try:
                    document = json.loads(self.path.read_bytes().decode("utf-8"))
                    for key, row in (document.get("series") or {}).items():
                        parsed = tuple((date.fromisoformat(day), Decimal(value)) for day, value in row["prices"])
                        if parsed and all(value > 0 and value.is_finite() for _, value in parsed):
                            prices[key] = parsed
                            source = str(row.get("source") or "unknown")
                            sources[key] = source
                            # A spliced history is still a token history, but its early prices
                            # are modelled. Keep that distinction visible to analytics.
                            provenance[key] = "spliced" if row.get("spliced") is True else (
                                "modelled" if source == "modelled" else "observed")
                            methods[key] = str(row.get("method")) if row.get("method") else None
                            if row.get("spliced"):
                                modelled_before[key] = date.fromisoformat(row["modelled_before"])
                except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
                    prices, sources, provenance, methods = {}, {}, {}, {}
                    modelled_before = {}
            self._stamp, self._prices, self._sources = stamp, prices, sources
            self._provenance, self._methods = provenance, methods
            self._modelled_before = modelled_before
            return prices

    @staticmethod
    def _key(chain_id: int, address: str | None, instrument_id: str | None = None) -> str:
        """Resolve addressless instruments directly by their registry identity."""
        return instrument_id if instrument_id is not None else history_key(chain_id, address)

    def price_at(self, chain_id: int, address: str | None, at: datetime, *,
                 instrument_id: str | None = None) -> dict[str, Any] | None:
        """The daily price nearest `at`, with how far away it was; None beyond MAX_PRICE_GAP_SECONDS.

        A source's daily point lands near UTC midnight, so the day whose midnight is closest to
        the moment is the one that describes it. Nothing is interpolated: a value is a price the
        source published, at a stated distance from the moment it is used for.
        """
        key = self._key(chain_id, address, instrument_id)
        with self._lock:
            prices = self._load().get(key)
            source = self._sources.get(key)
            boundary = self._modelled_before.get(key)
        if not prices or source == "modelled":
            return None
        moment = at.astimezone(UTC)
        if boundary is not None:
            # Never extend observed valuation backwards into a simulated prefix, even
            # when its first observed point is within the nearest-price tolerance.
            if moment.date() < boundary:
                return None
            prices = tuple((day, value) for day, value in prices if day >= boundary)
            if not prices:
                return None
        days = [day for day, _ in prices]
        midnight = lambda day: datetime(day.year, day.month, day.day, tzinfo=UTC)
        index = bisect_left(days, moment.date())
        nearby = [prices[position] for position in (index - 1, index, index + 1) if 0 <= position < len(prices)]
        day, value = min(nearby, key=lambda item: (abs((midnight(item[0]) - moment).total_seconds()), item[0]))
        gap = abs(int((midnight(day) - moment).total_seconds()))
        if gap > MAX_PRICE_GAP_SECONDS:
            return None
        return {
            "day": day.isoformat(),
            "price_usd": format(value, "f"),
            "price_usd_fixed": int((value * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)),
            "gap_seconds": gap,
            "source": source,
            "method_version": PRICE_AT_METHOD,
        }

    def series(self, chain_id: int, address: str | None, *, start: date | None = None,
               end: date | None = None, instrument_id: str | None = None) -> dict[str, Any] | None:
        """The stored daily prices for one asset, optionally within [start, end]."""
        key = self._key(chain_id, address, instrument_id)
        with self._lock:
            prices = self._load().get(key)
            source = self._sources.get(key)
            provenance = self._provenance.get(key, "observed")
            method = self._methods.get(key)
        if not prices:
            return None
        rows = [[day.isoformat(), format(value, "f")] for day, value in prices
                if (start is None or day >= start) and (end is None or day <= end)]
        result = {"source": source, "frequency": "UTC_DAILY", "prices": rows,
                  "provenance": provenance}
        if method:
            result["method"] = method
        return result

    def return_series(self, chain_id: int, address: str | None, *, position_id: str, as_of: datetime,
                      method_version: str, instrument_id: str | None = None) -> ReturnSeries | None:
        key = self._key(chain_id, address, instrument_id)
        with self._lock:
            prices = self._load().get(key)
            provenance = self._provenance.get(key, "observed")
        if not prices:
            return None
        cutoff = as_of.astimezone(UTC).date()
        usable = [(day, value) for day, value in prices if day <= cutoff]
        returns = []
        for (previous_day, previous), (day, value) in zip(usable, usable[1:]):
            if (day - previous_day).days != 1:
                continue
            bps = int(((value / previous - 1) * BPS).to_integral_value(rounding=ROUND_HALF_EVEN))
            # Prices are positive, so a loss rounds to -10,000 bp only by rounding; the engine
            # reserves -10,000 for a position that no longer exists.
            returns.append(DailyReturn(datetime(day.year, day.month, day.day, tzinfo=UTC), max(bps, -9_999)))
        # Judged on the last return, not the last price: a lone current price after a gap yields
        # no current return, and would pass a price check while the series stays in the past.
        if not returns or (cutoff - returns[-1].day).days > MAX_LAG_DAYS:
            return None
        window_start = cutoff - timedelta(days=TRAILING_DAYS - 1)
        if sum(1 for row in returns if row.day >= window_start) < MIN_ES_OBSERVATIONS:
            return None
        return ReturnSeries(position_id=position_id, frequency=MarkingFrequency.UTC_DAILY,
                            annualization_factor=365, returns=tuple(returns), method_version=method_version,
                            provenance=provenance)

    def covers(self, chain_id: int, address: str | None, *, as_of: datetime,
               instrument_id: str | None = None) -> bool:
        return self.return_series(chain_id, address, position_id="probe", as_of=as_of,
                                  method_version="probe", instrument_id=instrument_id) is not None


def select_cohort(supplied: list[ReturnSeries], candidates: list[tuple[int, ReturnSeries]], *,
                  as_of: datetime) -> list[ReturnSeries]:
    """The server's histories to add to the caller's, largest holding first, each kept only while
    the days all included series share inside the engine's window stay at its minimum.

    `supplied` must hold only the caller's series the engine will actually use; a series for a
    holding it excludes would otherwise narrow the window for nothing.

    The engine uses only the days every included series has, so a qualifying history with one
    missing day can still pull a portfolio below 250; such a holding is left out of risk and
    stays in the NAV denominator instead of failing everyone's metrics.
    """
    last = as_of.astimezone(UTC).date()
    first = last - timedelta(days=TRAILING_DAYS - 1)

    def days(row: ReturnSeries) -> set[date]:
        return {day for day in row.by_day() if first <= day <= last}

    aligned = set.intersection(*(days(row) for row in supplied)) if supplied else None
    chosen: list[ReturnSeries] = []
    for _, row in sorted(candidates, key=lambda item: (-item[0], item[1].position_id)):
        joined = days(row) if aligned is None else aligned & days(row)
        # Enough shared days, and a recent one: two current histories can share only old days,
        # and the engine refuses a window whose newest common day is past its staleness bar.
        if len(joined) >= MIN_ES_OBSERVATIONS and (last - max(joined)).days <= MAX_LAG_DAYS:
            chosen.append(row)
            aligned = joined
    return chosen
