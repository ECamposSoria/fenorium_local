"""Current USD prices for every asset the registry names, every few minutes, from DefiLlama.

Each asset ends up in one of two states, and never at zero:

- PRICED: the price, the time the source says it observed it, and the source.
- UNPRICED: a reason -- the source has no price, its price is below the precision Fenorium
  stores, its confidence is too low to use, or it could not be asked this run and there was no
  earlier price to keep.

One source is enough (owner rule). A run that cannot reach DefiLlama for an asset keeps that
asset's last price, marked as carried, with the source's original timestamp so it keeps ageing;
consumers decide by that timestamp whether it is still fresh enough for them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from engine.portfolio.contracts import PRICE_SCALE, safe_detail
from engine.registry import registry, registry_document

from ..fetch import FetchError, Fetcher
from ..runner import Context, Item, ItemResult


SCHEMA = "fenorium/prices-current/1"
# How old a published price may be and still value a holding, for every consumer. The job runs
# every five minutes; DefiLlama refreshes liquid tokens within minutes and thinner ones less often.
MAX_AGE_SECONDS = 60 * 60
SOURCE = "defillama"
METHOD_VERSION = "defillama-current/coins-prices-current-v1"
URL = "https://coins.llama.fi/prices/current/"
# DefiLlama's own score for how far it trusts a price. Below this it is not a price to value with.
MIN_CONFIDENCE = Decimal("0.5")
# Keys per request: each is ~50 characters and the whole URL must stay a few kilobytes.
BATCH = 60


def fixed_price(value: Any) -> int | None:
    """A source's float as an integer at PRICE_SCALE, or None when it is not a usable price."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        return None
    if not decimal.is_finite() or decimal <= 0:
        return None
    return int((decimal * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))


def _utc(seconds: Any) -> str | None:
    # NaN passes a `<= 0` test and infinity passes it too; either would raise in int() and take the
    # whole batch down with the one row (review cr).
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        return None
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class CurrentPricesJob:
    fetcher: Fetcher = field(default_factory=lambda: Fetcher(timeout_seconds=20, retries=2, budget_seconds=60))
    name: str = "current-prices"
    artifact: str = "prices-current.json"
    batch_size: int = BATCH
    max_failure_ratio: float = 0.5
    # A price run is cheap and only worth anything fresh: never resume an old one.
    resume_within: timedelta | None = None

    def items(self, context: Context) -> Sequence[Item]:
        document = registry_document(context.read_json("universe"), registry(), now=context.now())
        chains = {chain["chain_id"]: chain for chain in document["chains"]}
        return [Item(asset["asset_id"], asset["price_key"]) for asset in document["assets"]
                if asset["price_key"] and not chains[asset["chain_id"]]["testnet"]]

    def fetch(self, context: Context, batch: Sequence[Item]) -> Mapping[str, ItemResult]:
        keys = ",".join(quote(item.payload, safe=":") for item in batch)
        try:
            request_options = {}
            budgets = [context.fetch_budgets[item.key] for item in batch if item.key in context.fetch_budgets]
            if budgets and isinstance(self.fetcher, Fetcher):
                # One request serves the batch, so it must honour every item's budget.
                request_options["budget_seconds"] = min(self.fetcher.budget_seconds, min(budgets))
            payload = self.fetcher.get_json(f"{URL}{keys}?searchWidth=6h", **request_options)
        except FetchError as exc:
            return {item.key: ItemResult(ok=False, source=SOURCE, error=safe_detail(exc), transient=exc.transient,
                                         retry_after=exc.retry_after)
                    for item in batch}
        coins = payload.get("coins") if isinstance(payload, Mapping) else None
        if not isinstance(coins, Mapping):
            return {item.key: ItemResult(ok=False, source=SOURCE, error="response has no coins object",
                                         transient=True) for item in batch}
        found = {str(key).lower(): value for key, value in coins.items()}
        fetched_at = context.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        results: dict[str, ItemResult] = {}
        for item in batch:
            results[item.key] = ItemResult(ok=True, source=SOURCE,
                                           value=_entry(item.payload, found.get(item.payload.lower()), fetched_at))
        return results

    def build(self, context: Context, results: Mapping[str, ItemResult], items: Sequence[Item],
              previous: Any | None) -> dict[str, Any]:
        earlier = (previous or {}).get("assets") if isinstance(previous, Mapping) else None
        earlier = earlier if isinstance(earlier, Mapping) else {}
        assets: dict[str, Any] = {}
        for item in items:
            result = results[item.key]
            if result.ok:
                assets[item.key] = result.value
                continue
            held = earlier.get(item.key)
            if isinstance(held, Mapping) and held.get("status") == "PRICED":
                # The source was not reachable for it this run. Its last price stands with the
                # time it was observed, so it ages exactly as it would have.
                assets[item.key] = {**held, "carried": True}
            else:
                assets[item.key] = {"status": "UNPRICED", "reason": "SOURCE_UNREACHABLE",
                                    "source": SOURCE, "source_key": item.payload}
        priced = sum(1 for row in assets.values() if row["status"] == "PRICED")
        return {
            "schema_version": SCHEMA,
            "generated_at": context.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method_version": METHOD_VERSION,
            "price_scale": PRICE_SCALE,
            "assets": assets,
            "counts": {
                "assets": len(assets),
                "priced": priced,
                "unpriced": len(assets) - priced,
                "carried": sum(1 for row in assets.values() if row.get("carried")),
            },
        }

    def validate(self, document: Mapping[str, Any], previous: Any | None) -> None:
        assets = document.get("assets")
        if document.get("schema_version") != SCHEMA or not isinstance(assets, Mapping) or not assets:
            raise ValueError("price artifact is malformed or empty")
        for key, row in assets.items():
            if row.get("status") == "PRICED":
                if not isinstance(row.get("price_usd_fixed"), int) or row["price_usd_fixed"] <= 0:
                    raise ValueError(f"{key} is priced at a non-positive value")
                if not isinstance(row.get("observed_at"), str):
                    raise ValueError(f"{key} is priced without an observation time")
            elif row.get("status") != "UNPRICED" or not row.get("reason"):
                raise ValueError(f"{key} is neither priced nor unpriced with a reason")
        before = (previous or {}).get("counts", {}).get("priced") if isinstance(previous, Mapping) else None
        if isinstance(before, int) and before >= 20 and document["counts"]["priced"] * 2 < before:
            # Half the prices vanishing at once is the source failing, not the market.
            raise ValueError(f"priced assets fell from {before} to {document['counts']['priced']}")


def _entry(key: str, raw: Any, fetched_at: str) -> dict[str, Any]:
    unpriced = {"status": "UNPRICED", "source": SOURCE, "source_key": key, "fetched_at": fetched_at}
    if not isinstance(raw, Mapping):
        return {**unpriced, "reason": "SOURCE_HAS_NO_PRICE"}
    fixed = fixed_price(raw.get("price"))
    if fixed is None:
        return {**unpriced, "reason": "SOURCE_PRICE_INVALID" if raw.get("price") not in (None, 0) else "SOURCE_HAS_NO_PRICE"}
    if fixed == 0:
        return {**unpriced, "reason": "BELOW_PRICE_PRECISION"}
    observed_at = _utc(raw.get("timestamp"))
    if observed_at is None:
        return {**unpriced, "reason": "SOURCE_TIMESTAMP_MISSING"}
    confidence = raw.get("confidence")
    try:
        trusted = confidence is None or Decimal(str(confidence)) >= MIN_CONFIDENCE
    except InvalidOperation:
        trusted = False
    if not trusted:
        return {**unpriced, "reason": "SOURCE_CONFIDENCE_LOW", "confidence": str(confidence)}
    return {
        "status": "PRICED",
        "price_usd": str(raw.get("price")),
        "price_usd_fixed": fixed,
        "observed_at": observed_at,
        "fetched_at": fetched_at,
        "source": SOURCE,
        "source_key": key,
        "confidence": None if confidence is None else str(confidence),
        "symbol": raw.get("symbol") if isinstance(raw.get("symbol"), str) else None,
    }
