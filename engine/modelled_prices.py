"""Historical simulation of today's fixed-rate position; excludes default risk.

Daily carry, underlying moves and reference-rate duration shocks are modelled, not
observed token prices. Maturity is held constant throughout the simulation.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Mapping

from engine.registry import registry
from engine.registry.instruments import instrument_id

METHOD = "historical-duration-v1"
NOTE = "Modelled rate moves and underlying prices only; excludes default risk."
REFERENCE_POOLS = {"USD": "aa70268e-4b52-42bf-a116-608b370f9501",
                   "ETH": "e880e828-ca59-4ec6-8d4f-27182a4dc23d"}
VENUES = frozenset({"Exactly", "Tenor", "Morpho Midnight", "Term Finance", "TermMax", "Pendle", "Spectra"})
MODEL_DAYS = 400
# The longest hole in the inputs a model walks across. A dropped spike day is the usual one; the
# underlying's own levels on either side make the multi-day return exact, so nothing is invented.
MAX_GAP_DAYS = 7


def observed_prices(entry: Mapping[str, Any]) -> list:
    """A previous simulation must never become evidence on the next collection."""
    if entry.get("source") == "modelled":
        return []
    return [point for point in entry.get("prices", [])
            if not entry.get("spliced") or point[0] >= entry["modelled_before"]]


def reference_days(points: list[dict]) -> dict[date, Decimal]:
    """Use the last reported base APY within each UTC date (percent). Rewards switch on and off; the
    lending rate itself is what moves a fixed-rate position's value."""
    latest = {}
    for point in points:
        stamp = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("reference timestamp has no timezone")
        stamp = stamp.astimezone(UTC)
        raw = point.get("apyBase") if point.get("apyBase") is not None else point["apy"]
        rate = Decimal(str(raw)) / 100
        if not rate.is_finite():
            raise ValueError("non-finite reference rate")
        day = stamp.date()
        if day not in latest or stamp > latest[day][0]:
            latest[day] = (stamp, rate)
    return {day: value for day, (_, value) in latest.items()}


def simulate(underlying: Mapping[date, Decimal], reference: Mapping[date, Decimal], *,
             today: date, rate_bps: Any, tau_days: Decimal) -> list[list[str]]:
    """Walk backwards from today's discounted underlying.

    A hole of up to MAX_GAP_DAYS in either input is crossed with the exact return between the days
    on either side and that many days of carry; the missing days stay missing, as in the input, so a
    consumer counting consecutive-day returns loses those returns and nothing else. A longer hole
    ends the walk.
    """
    with localcontext() as ctx:
        ctx.prec = 40
        y0 = Decimal(str(rate_bps)) / 10000
        tau = tau_days / 365
        if not y0.is_finite() or y0 <= -1 or not tau.is_finite() or tau <= 0:
            raise ValueError("invalid current rate or maturity")
        if today not in underlying or today not in reference:
            return []
        price = underlying[today] * (1 + y0) ** (-tau)
        result = []
        day = today
        while True:
            if not price.is_finite() or price <= 0:
                raise ValueError("non-positive model price")
            result.append([day.isoformat(), format(price, "f")])
            before = next((candidate for step in range(1, MAX_GAP_DAYS + 2)
                           if (candidate := day - timedelta(days=step)) in underlying and candidate in reference),
                          None)
            if before is None or (today - before).days >= MODEL_DAYS:
                break
            span = (day - before).days
            y_before = y0 + reference[before] - reference[today]
            y_day = y0 + reference[day] - reference[today]
            if underlying[before] <= 0 or underlying[day] <= 0 or y_before <= -1:
                raise ValueError("invalid underlying or shifted rate")
            gross = (underlying[day] / underlying[before] + span * y_before / 365
                     - tau * (y_day - y_before) / (1 + y_before))
            if not gross.is_finite() or gross <= 0:
                raise ValueError("non-positive model gross return")
            price /= gross
            day = before
        return result[::-1]


def _usable_returns(points: list, today: date) -> int:
    """Consecutive-day returns inside the consumer's trailing 365 days, counted the way it counts them.

    A row count overstates it: one missing day inside 252 rows leaves 251 prices but 249 returns.
    """
    days = {date.fromisoformat(day) for day, _price in points}
    start = today - timedelta(days=364)
    return sum(1 for day in days if start <= day <= today and day - timedelta(days=1) in days)


def add_models(universe: Mapping[str, Any], series: dict, *, llama: Any, today: date,
               required_returns: int, failures: dict, pools: list[dict] | None = None) -> dict:
    """Apply each model atomically; bad inputs or a failed reference never cost observed data."""
    skips: Counter = Counter()
    references: dict[str, dict] = {}
    count = 0
    as_of = datetime(today.year, today.month, today.day, tzinfo=UTC)
    # Snapshot observed inputs: modelling one row must not affect another row's underlying.
    inputs = {key: observed_prices(entry) for key, entry in series.items()}
    for row in universe.get("instruments") or []:
        key = str(row.get("name", "unknown")) if isinstance(row, dict) else "unknown"
        try:
            if not isinstance(row, dict) or row.get("venue") not in VENUES:
                skips["venue"] += 1
                continue
            if row.get("maturity_timestamp") is None or Decimal(str(row["maturity_timestamp"])) <= Decimal(int(as_of.timestamp())):
                skips["maturity"] += 1
                continue
            if row.get("rate_bps") is None:
                skips["rate"] += 1
                continue
            denomination = row.get("denomination")
            if denomination not in REFERENCE_POOLS:
                skips["denomination"] += 1
                continue
            address = row.get("address")
            key = (f"{row['chain_id']}:{address.lower()}" if address else instrument_id(row, registry()))
            observed = inputs.get(key, [])
            if address and _usable_returns(observed, today) >= required_returns:
                skips["sufficient_history"] += 1
                continue
            if address and not observed:
                skips["observed"] += 1
                continue
            underlying = (row.get("underlying_address") or (row.get("loan_token") or {}).get("address")
                          or row.get("accounting_asset_address"))
            if underlying and underlying.lower() in {"0x" + "0" * 40, "0x" + "e" * 40}:
                underlying = "native"
            underlying_key = f"{row['chain_id']}:{underlying.lower()}" if underlying else None
            if not inputs.get(underlying_key):
                skips["underlying"] += 1
                continue
            pool = REFERENCE_POOLS[denomination]
            if denomination not in references:
                references[denomination] = {}
                try:
                    if pools is None:
                        pools = llama.yields_pools()
                    matches = [p for p in pools if p.get("pool") == pool]
                    symbol = "USDC" if denomination == "USD" else "WETH"
                    if len(matches) != 1 or any(matches[0].get(k) != v for k, v in
                                               {"project": "aave-v3", "chain": "Ethereum", "symbol": symbol}.items()):
                        raise ValueError("reference pool identity mismatch")
                    references[denomination] = reference_days(llama.yield_chart(pool))
                except Exception as exc:  # one reference failure affects only its models
                    failures[f"model-reference:{pool}"] = str(exc)
            reference = references[denomination]
            if today not in reference:
                skips["reference"] += 1
                continue
            with localcontext() as ctx:
                ctx.prec = 40
                tau_days = (Decimal(str(row["maturity_timestamp"])) - Decimal(int(as_of.timestamp()))) / 86400
                prices = simulate({date.fromisoformat(d): Decimal(p) for d, p in inputs[underlying_key]},
                                  reference, today=today, rate_bps=row["rate_bps"], tau_days=tau_days)
                if not prices:
                    skips["underlying"] += 1
                    continue
                model = {"rate_bps": row["rate_bps"], "tau_days": format(tau_days, "f"),
                         "reference_pool": pool, "underlying_key": underlying_key,
                         "as_of": as_of.isoformat().replace("+00:00", "Z")}
                if address:
                    join = observed[0][0]
                    by_day = dict(prices)
                    if join not in by_day or prices[0][0] >= join:
                        skips["splice_overlap"] += 1
                        continue
                    scale = Decimal(observed[0][1]) / Decimal(by_day[join])
                    prefix = [[d, format(Decimal(p) * scale, "f")] for d, p in prices if d < join]
                    entry = {**series[key], "prices": prefix + observed, "spliced": True,
                             "modelled_before": join, "model": model}
                else:
                    entry = {"chain_id": row["chain_id"], "address": None, "instrument_id": key,
                             "symbol": row.get("name"), "source": "modelled", "method": METHOD,
                             "model": model, "refreshed": True, "spikes": {}, "prices": prices}
                series[key] = entry
                count += 1
        except Exception as exc:  # a malformed row must never fail the collector
            failures[f"model:{key}"] = str(exc)
            skips["failure"] += 1
    return {"modelled": count, "model_method": METHOD, "model_note": NOTE,
            "reference_rates": dict(REFERENCE_POOLS), "model_skips": dict(skips)}
