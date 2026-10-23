"""Daily price history for every registered token, published next to the universe.

The risk engine needs 250 aligned daily observations inside a 365-day window. The evidence
pipeline fetched 90 days, and nothing handed any history to the analytics endpoint, so
volatility and expected shortfall could never compute. This builds 400 days of UTC-daily prices
per token from DefiLlama's coin chart, and asks CoinGecko only for tokens DefiLlama has nothing
for. One source is enough; each series records which one it came from.

A day's price is the source's price nearest that UTC midnight (within the search width), so day
D carries the value at the D-1/D boundary. Days the source has no price for are absent -- never
filled -- and the returns computed from them skip the gap.

A token whose fetch fails keeps its series from the previous artifact rather than disappearing;
`refreshed` says which is which, and the API ignores any series that has fallen behind.

A single day that jumps away from both neighbours and snaps back (a USDC vault "down 6%" for one
day) is usually the source's glitch, but a one-day depeg looks the same, and deleting a real one
understates risk. So such a day is removed only when the second source, asked about that day,
shows no such move. Without a second source the day stays. Every verdict is recorded on the
series and reused, so only new candidates cost a call.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from engine.portfolio.analytics import MIN_ES_OBSERVATIONS
from engine.registry import registry
from engine.modelled_prices import add_models, observed_prices

SCHEMA = "fenorium/price-history/1"
# Two years: as far back as a wallet's transfer history is scanned (web/portfolio.js
# SCAN_SPAN_SECONDS), so a transfer can be valued at a price from its own day.
WINDOW_DAYS = 730
DAY_SECONDS = 86_400
# The most daily points DefiLlama's coin chart serves in one request.
CHART_MAX_POINTS = 500
# Every mainnet an instrument is collected, bought or held on, with its ids from the chain registry.
_CHAINS = tuple(chain for chain in registry().chains if not chain.testnet
                and chain.roles & {"collect", "buy", "portfolio"} and chain.defillama)
CHAIN_SLUGS = {chain.chain_id: chain.defillama for chain in _CHAINS}
COINGECKO_PLATFORMS = {chain.chain_id: chain.coingecko_platform for chain in _CHAINS}
# CoinGecko ids of each chain's gas token; DefiLlama serves them as `coingecko:<id>`.
NATIVE_COINS = {chain.chain_id: chain.native_coingecko_id for chain in _CHAINS}


# A spike must move at least this far from both neighbours, and far more than the token's own
# typical day, so an ordinary volatile day is never a candidate.
SPIKE_FLOOR = Decimal("0.005")
SPIKE_MULTIPLE = 8
# The second source "shows no such move" when its own deviation that day is under a quarter of it.
CONFIRM_FRACTION = Decimal("0.25")
# CoinGecko's keyless tier allows a handful of calls a minute and the refresh has a time limit, so
# a night makes at most this many CoinGecko requests -- fallbacks and spike checks together; what
# is left waits for a later night.
MAX_COINGECKO_CALLS = 20
# Must match deploy/platform/price_history.TRAILING_DAYS: the window the API counts returns in.
CONSUMER_WINDOW_DAYS = 365
# The API attaches a history only with this many returns inside that window.
REQUIRED_RETURNS = MIN_ES_OBSERVATIONS
# Must match deploy/platform/price_history.MAX_LAG_DAYS: the API's bar for using a series at all.
CURRENT_WITHIN_DAYS = 2


class PriceHistoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Target:
    chain_id: int
    address: str | None  # None is the chain's native coin
    symbol: str

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.address or 'native'}"


def history_key(chain_id: int, address: str | None) -> str:
    """The key a position is looked up by: chain id and lowercase address, or `native`."""
    return f"{chain_id}:{address.lower() if address else 'native'}"


def targets(universe: Mapping[str, Any]) -> list[Target]:
    found: dict[str, Target] = {}
    for row in universe.get("instruments") or []:
        if not isinstance(row, dict) or row.get("chain_id") not in CHAIN_SLUGS:
            continue
        addresses = [(row.get("address"), str(row.get("symbol") or row.get("name") or ""))]
        if row.get("maturity_timestamp") is not None:
            # Modelled prices for fixed-rate instruments are built on their underlying's history, which
            # is not always an instrument of its own (a PT's accounting asset, a deposit's loan token).
            loan = row.get("loan_token") if isinstance(row.get("loan_token"), dict) else {}
            addresses += [(row.get("underlying_address"), ""), (loan.get("address"), str(loan.get("symbol") or "")),
                          (row.get("accounting_asset_address"), "")]
        for address, symbol in addresses:
            if (not isinstance(address, str) or len(address) != 42 or not address.startswith("0x")
                    or address.lower() in ("0x" + "0" * 40, "0x" + "e" * 40)):
                continue
            target = Target(row["chain_id"], address.lower(), symbol)
            found.setdefault(target.key, target)
    for chain_id, coin in NATIVE_COINS.items():
        target = Target(chain_id, None, coin)
        found.setdefault(target.key, target)
    return [found[key] for key in sorted(found)]


def daily_prices(points: Iterable[tuple[int, Any]], start_day: date, days: int) -> list[tuple[date, Decimal]]:
    """Place each (unix seconds, price) on the grid day nearest its timestamp; drop the rest."""
    origin = int(datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC).timestamp())
    best: dict[int, tuple[int, Decimal]] = {}
    for timestamp, price in points:
        try:
            value = Decimal(str(price))
        except (InvalidOperation, ValueError):
            continue
        if not value.is_finite() or value <= 0:
            continue
        offset = int(timestamp) - origin
        index = round(offset / DAY_SECONDS)
        if not 0 <= index < days:
            continue
        distance = abs(offset - index * DAY_SECONDS)
        if index not in best or distance < best[index][0]:
            best[index] = (distance, value)
    return [(start_day + timedelta(days=index), best[index][1]) for index in sorted(best)]


def _deviation(prices: Mapping[date, Decimal], day: date) -> Decimal | None:
    before, value, after = prices.get(day - timedelta(days=1)), prices.get(day), prices.get(day + timedelta(days=1))
    if before is None or value is None or after is None:
        return None
    return value / ((before + after) / 2) - 1


def spike_candidates(closes: list[tuple[date, Decimal]]) -> list[date]:
    """Days that leave both neighbours by far more than this token's typical day, and return."""
    by_day = dict(closes)
    moves = [abs(value / by_day[day - timedelta(days=1)] - 1) for day, value in closes
             if day - timedelta(days=1) in by_day]
    if len(moves) < 10:
        return []
    threshold = max(SPIKE_FLOOR, SPIKE_MULTIPLE * statistics.median(moves))
    found = []
    for day, value in closes:
        before, after = by_day.get(day - timedelta(days=1)), by_day.get(day + timedelta(days=1))
        if before is None or after is None:
            continue
        up, down = value / before - 1, after / value - 1
        if abs(up) > threshold and abs(down) > threshold and up * down < 0 and abs(after / before - 1) < threshold / 2:
            found.append(day)
    return found


def adjudicate(closes: list[tuple[date, Decimal]], *, second: list[tuple[date, Decimal]] | None,
               earlier: Mapping[str, str], asked: bool = True) -> tuple[list[tuple[date, Decimal]], dict[str, str]]:
    """Drop the spike days the second source contradicts; record a verdict for every candidate."""
    # A decided day keeps its verdict whether or not tonight's answer happens to include it, so a
    # glitch dropped once cannot slip back in on a night the second source is unavailable.
    verdicts: dict[str, str] = dict(earlier)
    other = dict(second or [])
    ours = dict(closes)
    for day in spike_candidates(closes):
        key = day.isoformat()
        if key in earlier:
            verdicts[key] = earlier[key]
            continue
        if not asked:
            continue
        theirs = _deviation(other, day)
        if theirs is None:
            # Final only when the second source has nothing, or nothing that far back -- it never
            # extends backwards. A missing recent neighbour is a source that has not caught up
            # yet (or a cached answer from yesterday), so the day stays pending for a later night.
            if not other or day - timedelta(days=1) < min(other):
                verdicts[key] = "kept-unverified"
        elif abs(theirs) < abs(_deviation(ours, day)) * CONFIRM_FRACTION:
            verdicts[key] = "dropped"
        else:
            verdicts[key] = "kept-confirmed"
    dropped = {key for key, verdict in verdicts.items() if verdict == "dropped"}
    return [(day, value) for day, value in closes if day.isoformat() not in dropped], verdicts


def _defillama_points(llama: Any, target: Target, start: int) -> list[tuple[int, Any]]:
    coin = (f"coingecko:{NATIVE_COINS[target.chain_id]}" if target.address is None
            else f"{CHAIN_SLUGS[target.chain_id]}:{target.address}")
    points: list[tuple[int, Any]] = []
    # The chart answers at most CHART_MAX_POINTS points ("Requested 730 data points exceeds the
    # maximum of 500"), so the window is asked for in consecutive parts, newest first. The newest
    # must answer, as the whole window had to. An older one may hold nothing -- a token younger than
    # the window -- and the client refuses an empty answer, so an older part that fails is skipped:
    # it costs only days that a held series from the same source keeps anyway.
    offsets = list(range(0, WINDOW_DAYS, CHART_MAX_POINTS))
    for offset in reversed(offsets):
        span = min(CHART_MAX_POINTS, WINDOW_DAYS - offset)
        try:
            payload = llama.coin_chart(coin, start + offset * DAY_SECONDS, span, "1d", "6h",
                                       cache_name=f"price_history_{WINDOW_DAYS}d_{offset}_{coin}", minimum_points=1)
        except Exception:  # noqa: BLE001 - see above; the newest part's failure still propagates
            if offset == offsets[-1]:
                raise
            continue
        entry = (payload.get("coins") or {}).get(coin) or {}
        points.extend((row["timestamp"], row["price"]) for row in entry.get("prices") or []
                      if isinstance(row, dict) and "timestamp" in row and "price" in row)
    return points


def _coingecko_points(coingecko: Any, target: Target, start: int, end: int) -> list[tuple[int, Any]]:
    # The keyless API serves at most 365 days of history; the older 35 days simply stay absent.
    start = max(start, end - 365 * DAY_SECONDS)
    if target.address is None:
        series = coingecko.coin_market_chart(NATIVE_COINS[target.chain_id], start, end,
                                             minimum_points=1, cache_tag=f"{WINDOW_DAYS}d")
    else:
        series = coingecko.contract_market_chart(COINGECKO_PLATFORMS[target.chain_id], target.address, start, end,
                                                 minimum_points=1, cache_tag=f"{WINDOW_DAYS}d")
    return [(point.timestamp, point.price) for point in series]


def build(universe: Mapping[str, Any], *, llama: Any, coingecko: Any | None, today: date,
          previous: Mapping[str, Any] | None = None,
          pools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One pass per token: what is held, DefiLlama's answer, CoinGecko's when needed, then one choice.

    Three properties hold by construction. A token's spike verdicts are one record, carried and
    updated whichever source serves it. Every dropped day is removed from every DefiLlama-derived
    series -- the held one included -- before anything is compared. And series are compared on
    what the API will use: current first, then consecutive-day returns inside its window.
    """
    start_day = today - timedelta(days=WINDOW_DAYS - 1)
    start = int(datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC).timestamp())
    end = int(datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp()) + DAY_SECONDS
    earlier = (previous or {}).get("series") or {}
    series: dict[str, Any] = {}
    failures: dict[str, str] = {}
    calls = 0  # CoinGecko requests tonight, fallbacks and spike checks alike
    # The last night CoinGecko was asked about each token. The budget is spent in processing order,
    # so the tokens that have waited longest go first; a fixed or slowly rotating order hands the
    # same tokens the budget night after night and lets the others go stale.
    checked: dict[str, str] = dict((previous or {}).get("coingecko_checked") or {})

    def ask_coingecko(target: Target) -> list[tuple[date, Decimal]]:
        nonlocal calls
        calls += 1
        checked[target.key] = today.isoformat()
        return daily_prices(_coingecko_points(coingecko, target, start, end), start_day, WINDOW_DAYS)

    def fail(key: str, reason: str) -> None:
        failures[key] = f"{failures.get(key, '')} {reason}".strip()

    for target in sorted(targets(universe), key=lambda target: (checked.get(target.key, ""), target.key)):
        held = earlier.get(target.key) or {}
        held_source = held.get("source")
        held_raw = {date.fromisoformat(day): Decimal(value) for day, value in observed_prices(held)
                    if date.fromisoformat(day) >= start_day}
        verdicts = {day: verdict for day, verdict in (held.get("spikes") or {}).items()
                    if date.fromisoformat(day) >= start_day}

        def cleaned(prices: dict[date, Decimal]) -> dict[date, Decimal]:
            dropped = {day for day, verdict in verdicts.items() if verdict == "dropped"}
            return {day: value for day, value in prices.items() if day.isoformat() not in dropped}

        def extend(source: str, prices: dict[date, Decimal]) -> dict[date, Decimal]:
            # Past daily prices do not change, so a night's answer extends what is held from the
            # same source instead of replacing it: a truncated answer costs nothing already held.
            # Never across sources -- two sources meeting at a seam would invent a day's return.
            return {**held_raw, **prices} if held_source == source else prices

        options: dict[str, dict[date, Decimal]] = {}
        fetched: list[tuple[date, Decimal]] | None = None  # tonight's CoinGecko answer, asked at most once
        tried = False
        merged_returns = 0
        try:
            raw = dict(daily_prices(_defillama_points(llama, target, start), start_day, WINDOW_DAYS))
        except Exception as exc:  # noqa: BLE001 - one token must not cost the others their history
            raw = {}
            fail(target.key, f"defillama: {exc}")
        if raw:
            merged = extend("defillama", raw)
            pending = [day for day in spike_candidates(sorted(merged.items())) if day.isoformat() not in verdicts]
            second, asked = None, True  # with no second source configured, the verdict is "unverified"
            if pending and coingecko is not None:
                asked = calls < MAX_COINGECKO_CALLS
                if asked:
                    tried = True
                    try:
                        second = fetched = ask_coingecko(target)
                    except Exception as exc:  # noqa: BLE001 - no second opinion means the days stay
                        # "Not listed" is an answer and is recorded; a rate limit or an outage is
                        # not, so those days stay unjudged and are asked about again later.
                        asked = "HTTP 404" in str(exc)
            _, verdicts = adjudicate(sorted(merged.items()), second=second, earlier=verdicts, asked=asked)
            options["defillama"] = cleaned(merged)
            merged_returns = _quality(merged, today)[1]
        held_prices = cleaned(held_raw) if held_source == "defillama" else held_raw
        held_quality = _quality(held_prices, today)

        primary = options.get("defillama")
        primary_quality = _quality(primary, today) if primary is not None else (False, 0)
        # The fallback is wanted when the primary gave nothing, is not current (it may keep serving
        # a history it stopped advancing), is worse than what is already held, or met the API's
        # minimum until a removed spike day took it below. Not merely for being short: a young
        # token is short everywhere, and asking for each one nightly would spend the whole budget.
        # An answer CoinGecko gave tonight for a spike check is reused rather than asked again.
        cleaned_below = merged_returns >= REQUIRED_RETURNS > primary_quality[1]
        wanted = primary is None or not primary_quality[0] or cleaned_below or primary_quality < held_quality
        if coingecko is not None and wanted:
            if fetched is None and not tried and calls < MAX_COINGECKO_CALLS:
                tried = True
                try:
                    fetched = ask_coingecko(target)
                except Exception as exc:  # noqa: BLE001
                    fail(target.key, f"coingecko: {exc}")
            if fetched:
                options["coingecko"] = extend("coingecko", dict(fetched))

        # Nothing replaces what is held unless it is at least as good; ties keep the primary.
        chosen = max(options.items(), key=lambda item: _quality(item[1], today), default=None)
        if chosen is not None and held_prices and _quality(chosen[1], today) < held_quality:
            fail(target.key, f"{chosen[0]} answered {len(chosen[1])} days against {len(held_prices)} held; "
                             "kept the held series")
            chosen = None
        if chosen is not None:
            source, prices, refreshed = chosen[0], chosen[1], True
        elif held_prices:
            # Kept while anything held is still inside the window; an exhausted history is dropped
            # rather than published empty, which validation would refuse for the whole artifact.
            source, prices, refreshed = held_source, held_prices, False
        else:
            continue
        series[target.key] = {
            "chain_id": target.chain_id, "address": target.address, "symbol": target.symbol,
            "source": source, "refreshed": refreshed, "spikes": verdicts,
            "prices": [[day.isoformat(), format(value, "f")] for day, value in sorted(prices.items())],
        }
    model_metadata = add_models(universe, series, llama=llama, today=today,
                                required_returns=REQUIRED_RETURNS, failures=failures, pools=pools)
    observed = {key: row for key, row in series.items()
                if row.get("source") != "modelled" and not row.get("spliced")}
    refreshed_count = sum(1 for row in observed.values() if row["refreshed"])
    current = sum(1 for row in observed.values() if is_current(row["prices"], today))
    return {
        **model_metadata,
        "schema_version": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frequency": "UTC_DAILY",
        "window_days": WINDOW_DAYS,
        "targets": sum(1 for target in targets(universe) if not series.get(target.key, {}).get("spliced")),
        "refreshed": refreshed_count,
        "current": current,
        "series": series,
        "coingecko_checked": {target.key: checked[target.key] for target in targets(universe) if target.key in checked},
        "failures": {key: reason[:300] for key, reason in failures.items()},
    }


def is_current(prices: list[list[str]], today: date) -> bool:
    """Whether the newest consecutive-day return is within the API's two-day freshness bar.

    A live answer can still carry only old prices, and the API rejects such a series; counting
    it as current would keep the low-coverage alert quiet while analytics went dark.
    """
    return _current_days([date.fromisoformat(row[0]) for row in prices], today)


def _quality(prices: Mapping[date, Any], today: date) -> tuple[bool, int]:
    """Current first, then consecutive-day returns inside the API's window -- what it will use."""
    days = sorted(prices)
    window_start = today - timedelta(days=CONSUMER_WINDOW_DAYS - 1)
    returns = sum(1 for earlier, later in zip(days, days[1:])
                  if (later - earlier).days == 1 and window_start <= later <= today)
    return _current_days(days, today), returns


def _current_days(days: list[date], today: date) -> bool:
    ends = [later for earlier, later in zip(days, days[1:]) if (later - earlier).days == 1]
    return bool(ends) and (today - ends[-1]).days <= CURRENT_WITHIN_DAYS


def validate(document: Mapping[str, Any], *, previous: Mapping[str, Any] | None = None) -> None:
    """Refuse a structurally broken artifact, or one that lost more than a quarter of its series."""
    if document.get("schema_version") != SCHEMA or document.get("frequency") != "UTC_DAILY":
        raise PriceHistoryError("price history has the wrong schema or frequency")
    series = document.get("series")
    if not isinstance(series, dict) or not series:
        raise PriceHistoryError("price history carries no series")
    for key, row in series.items():
        prices = row.get("prices") if isinstance(row, dict) else None
        if not isinstance(prices, list) or not prices:
            raise PriceHistoryError(f"series {key} has no prices")
        days = [entry[0] for entry in prices]
        if days != sorted(set(days)):
            raise PriceHistoryError(f"series {key} has unsorted or repeated days")
        for day, value in prices:
            date.fromisoformat(day)
            if not Decimal(value).is_finite() or Decimal(value) <= 0:
                raise PriceHistoryError(f"series {key} has a non-positive price on {day}")
    model_keys = {key for rows in (series, (previous or {}).get("series") or {})
                  for key, row in rows.items() if isinstance(row, dict)
                  and (row.get("source") == "modelled" or row.get("spliced"))}
    series = {key: row for key, row in series.items() if key not in model_keys}
    prior = {key: row for key, row in ((previous or {}).get("series") or {}).items()
             if isinstance(row, dict) and key not in model_keys}
    before = len(prior)
    if before and len(series) * 4 < before * 3:
        raise PriceHistoryError(f"price history fell from {before} series to {len(series)}")
    # Counting tokens alone let one-point answers replace every full history and still pass.
    observations = sum(len(row["prices"]) for row in series.values())
    held = sum(len(row.get("prices") or []) for row in prior.values()
               if isinstance(row, dict))
    if held and observations * 4 < held * 3:
        raise PriceHistoryError(f"price history fell from {held} daily observations to {observations}")


# During an outage the client answers from its cache; without an age limit a days-old series
# would come back as tonight's and be counted refreshed while the API rejects it as stale. The
# daily series are reused for 20 hours anyway, so anything older is a failure, not a fallback.
CACHE_FALLBACK_MINUTES = 26 * 60


def research_http(client_class: Any, data_dir: Path) -> Any:
    return client_class(data_dir, delay_seconds=0.3, retries=2, max_age_minutes=CACHE_FALLBACK_MINUTES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)

    from research.par_sources.coingecko import CoinGeckoClient
    from research.par_sources.defillama import DefiLlamaClient
    from research.par_sources.http import HttpClient
    from engine.evidence_inputs import RESEARCH_DATA

    universe = json.loads(args.universe.read_text(encoding="utf-8"))
    previous = None
    if args.previous and args.previous.is_file():
        try:
            previous = json.loads(args.previous.read_text(encoding="utf-8"))
        except ValueError:
            previous = None
    http = research_http(HttpClient, RESEARCH_DATA)
    document = build(universe, llama=DefiLlamaClient(http), coingecko=CoinGeckoClient(http),
                     today=datetime.now(UTC).date(), previous=previous)
    validate(document, previous=previous)
    args.output.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(f"price history: {len(document['series'])} series for {document['targets']} tokens, "
          f"{document['refreshed']} refreshed, {len(document['failures'])} fetch failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
