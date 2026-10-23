from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.registry import registry
from research.par_sources.defillama import DefiLlamaClient, resolve_stablecoin_issuer
from research.par_sources.coingecko import CoinGeckoClient
from research.par_sources.blockscout import BlockscoutClient
from research.par_sources.ethplorer import EthplorerClient
from research.par_sources.etherscan import EtherscanClient
from research.par_sources.http import HttpClient, SourceError
from research.par_sources.morpho import MorphoClient, MorphoUnderlyingFeed
from research.par_sources.oracle import (
    DESCRIPTION, LATEST_ROUND_DATA, OracleClient, OracleRound,
    decode_abi_string, decode_block_timestamp, decode_latest_round_data,
)
from research.par_sources.vault import (
    CHAIN_RPCS, VaultClient, decode_decimals, endpoints_for, request_rpc_json,
)
from research.par_sources.lifi_quotes import PayAsset, fetch_exact_sale
from engine.chain import selector
from research.par_sources.venue_oracle import (
    SUBSCRIBED_VENUES,
    VenueOracleClient,
    VenueOracleContext,
    VenueOracleFeed,
)

from .credit import TimedValue


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DATA = ROOT / "research" / "data"
# DefiLlama's coin namespace and CoinGecko's asset platform for every chain the collector covers.
CHAIN_NAMESPACES = {
    chain.chain_id: chain.defillama for chain in registry().with_role("collect") if chain.defillama
}
COINGECKO_PLATFORMS = {
    chain.chain_id: chain.coingecko_platform
    for chain in registry().with_role("collect") if chain.coingecko_platform
}
PRICE_DISAGREEMENT_TOLERANCE_BP = 100.0
# A cached liquidation quote stands for the amount it sold; beyond this share away from the amount
# now needed it is no longer evidence for the size asked.
COLLATERAL_QUOTE_AMOUNT_TOLERANCE = 0.10
COLLATERAL_QUOTE_CACHE_HOURS = 20
# Match the obtainability sweep's wall-clock budget; all collateral sales share it.
COLLATERAL_QUOTE_BUDGET_SECONDS = 1200
COLLATERAL_QUOTE_SIZE_USD = (10_000.0, 1_000_000.0)
_ORACLE_PRICE = selector("price()")
_FEED_GETTERS = (
    "BASE_FEED_1()", "BASE_FEED_2()", "QUOTE_FEED_1()", "QUOTE_FEED_2()",
)

# The unit a non-USD instrument is a claim on. Measuring a wstETH peg in dollars reports ETH's
# volatility as a credit defect; the ratio to its own unit is the question that has an answer.
NUMERAIRE_COINS = {
    "ETH": "coingecko:ethereum",
    "BTC": "coingecko:bitcoin",
    "HYPE": "coingecko:hyperliquid",
    "BNB": "coingecko:binancecoin",
}


def _day_index(stamp: float) -> int:
    """The daily grid point a price belongs to: the nearest UTC midnight, not the day it fell in.

    DefiLlama answers a midnight grid with the trade nearest to each midnight, which lands a few
    minutes before it about as often as after. Flooring to the calendar day put those points on
    the previous day and paired them with a numeraire price 24 hours away -- measured 2026-10-10
    on wstETH/ETH: 866 bp of "drawdown" that rounding to the nearest midnight makes 95 bp.
    """
    return int((stamp + 43_200) // 86_400)


def _daily_series_start(as_of: datetime) -> int:
    """Unix seconds of UTC midnight 90 days before as_of's date.

    A token series and the numeraire it is divided by are fetched and cached independently,
    hours apart. Anchoring both to midnight puts every daily point on the same instant, so the
    ratio measures the peg and not the hours between two fetches.
    """
    midnight = as_of.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - timedelta(days=90)).timestamp())


@dataclass(frozen=True)
class FeedDefinition:
    address: str
    heartbeat_seconds: float
    heartbeat_source: str


DOCUMENTED_FEEDS = {
    (1, "USDC"): FeedDefinition(
        "0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6",
        86_400.0,
        "Term Finance price-feed documentation: Chainlink Ethereum USDC/USD heartbeat 24 hours",
    ),
}


@dataclass(frozen=True)
class EvidenceBundle:
    peg_prices: tuple[TimedValue[float], ...] = ()
    protocol_tvl_usd: tuple[TimedValue[float], ...] = ()
    oracle_updated_at: TimedValue[datetime] | None = None
    oracle_heartbeat_seconds: TimedValue[float] | None = None
    holder_top10_share: TimedValue[float] | None = None
    protocol_slug: str | None = None
    issuer_name: str | None = None
    peg_mechanism: str | None = None
    peg_type: str | None = None
    oracle_kind: str | None = None
    oracle_behavior: str | None = None
    oracle_description: str | None = None
    oracle_reason: str | None = None
    numeraire: str = "USD"
    redemption_ratio: float | None = None
    market_ratio: float | None = None
    redemption_discount_bp: float | None = None
    redemption_source: str | None = None
    price_disagreement_bp: float | None = None
    unresolved: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _SharedEvidence:
    peg_prices: tuple[TimedValue[float], ...]
    protocol_tvl_usd: tuple[TimedValue[float], ...]
    holder_top10_share: TimedValue[float] | None
    protocol_slug: str | None
    issuer_name: str | None
    peg_mechanism: str | None
    peg_type: str | None
    redemption_ratio: float | None
    market_ratio: float | None
    redemption_discount_bp: float | None
    redemption_source: str | None
    unresolved: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OracleClassification:
    oracle_kind: str
    behavior: str
    reason: str


def _parse_timestamp(value: Any) -> datetime | None:
    """DefiLlama's yield charts stamp rows as ISO-8601 with a trailing Z.

    Returns None rather than raising: a row this cannot read is one row skipped, never a reason to
    discard a series that is otherwise usable.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class CrossCheckUnavailable(RuntimeError):
    """The required second source could not be consulted.

    This remains distinct from a measured disagreement so reports can say which failure occurred,
    but both outcomes refuse the field. A primary-only value is useful diagnostic evidence, not an
    underwritten input.
    """


def cross_check_prices(primary: dict[int, float], secondary: dict[int, float], *,
                       minimum_points: int, field: str) -> tuple[dict[int, float], float]:
    """Return a conservative aligned series only when every retained day corroborates it."""
    common = sorted(primary.keys() & secondary.keys())
    if len(common) < minimum_points:
        raise CrossCheckUnavailable(
            f"COULD NOT VERIFY {field}: second-source cross-check has only {len(common)} "
            f"aligned daily observations against the {minimum_points} needed"
        )
    gaps: list[tuple[int, float]] = []
    checked: dict[int, float] = {}
    for day in common:
        left, right = primary[day], secondary[day]
        gap = abs(left - right) / max(left, right) * 10_000
        gaps.append((day, gap))
        checked[day] = min(left, right)
    worst_day, worst_gap = max(gaps, key=lambda item: item[1])
    if worst_gap > PRICE_DISAGREEMENT_TOLERANCE_BP:
        observed = datetime.fromtimestamp(worst_day * 86_400, UTC).date().isoformat()
        raise RuntimeError(
            f"COULD NOT VERIFY {field}: DefiLlama and CoinGecko disagree by "
            f"{worst_gap:.1f}bp on {observed}, beyond the "
            f"{PRICE_DISAGREEMENT_TOLERANCE_BP:.0f}bp tolerance"
        )
    return checked, worst_gap


def corroborate_prices(
    primary: dict[int, float], secondary: dict[int, float] | None, *,
    minimum_points: int, field: str,
) -> tuple[dict[int, float], float | None, str]:
    """Keep the full primary series; corroboration changes diagnostic metadata only."""
    if secondary is None:
        return primary, None, "CoinGecko: not listed"
    common = sorted(primary.keys() & secondary.keys())
    if len(common) < minimum_points:
        return primary, None, f"CoinGecko: only {len(common)} aligned daily points, not used"
    worst_day, worst_gap = max(
        ((day, abs(primary[day] - secondary[day]) / max(primary[day], secondary[day]) * 10_000)
         for day in common),
        key=lambda item: item[1],
    )
    if worst_gap > PRICE_DISAGREEMENT_TOLERANCE_BP:
        observed = datetime.fromtimestamp(worst_day * 86_400, UTC).date().isoformat()
        return primary, worst_gap, (
            f"CoinGecko disagreed by {worst_gap:.1f} bp on {observed}; DefiLlama series kept"
        )
    return primary, worst_gap, f"CoinGecko agreed within {worst_gap:.1f} bp"


def _coingecko_unavailable_note(error: SourceError) -> str:
    status = re.search(r"\bHTTP ([45][0-9]{2})\b", str(error), re.IGNORECASE)
    if status is not None and status[1] == "404":
        return "CoinGecko: not listed"
    reason = f"HTTP {status[1]}" if status is not None else "price history could not be retrieved"
    return f"CoinGecko: unavailable ({reason})"


def cross_check_spot(primary: float, secondary: float, *, field: str) -> tuple[float, float]:
    gap = abs(primary - secondary) / max(primary, secondary) * 10_000
    if gap > PRICE_DISAGREEMENT_TOLERANCE_BP:
        raise RuntimeError(
            f"COULD NOT VERIFY {field}: DefiLlama and CoinGecko disagree by {gap:.1f}bp, "
            f"beyond the {PRICE_DISAGREEMENT_TOLERANCE_BP:.0f}bp tolerance"
        )
    return min(primary, secondary), gap


def classify_aggregator_feed(
    feed: MorphoUnderlyingFeed | VenueOracleFeed,
    round_data: OracleRound,
    description: str,
    block_timestamp: datetime,
) -> OracleClassification:
    if round_data.updated_at is None:
        return OracleClassification(
            "unverified", "updated_at_zero",
            f"COULD NOT VERIFY oracle_model_risk: underlying feed {feed.feed_address} "
            f"({description}) returned updatedAt == 0",
        )
    tracks_block = abs((block_timestamp - round_data.updated_at).total_seconds()) <= 300
    description_marks_adapter = any(
        word in description.casefold() for word in ("adapter", "oracle", "chainlink-compatible")
    )
    if tracks_block and description_marks_adapter:
        hardcoded = round_data.answer == 10 ** feed.feed_decimals
        behavior = "hardcoded_1to1" if hardcoded else "variable_price"
        return OracleClassification(
            "onchain_adapter", behavior,
            f"{description} is an on-chain adapter whose updatedAt tracks the block timestamp; "
            f"its {behavior} output is scored as oracle model risk, not periodic-feed freshness",
        )
    return OracleClassification(
        "genuine_feed", "heartbeat_undocumented",
        f"genuine feed {feed.feed_address} ({description}) does not track the block timestamp; "
        "its heartbeat is undocumented, so staleness cannot be judged and no heartbeat was guessed",
    )


# Public compatibility for callers that imported the old, overly narrow name.
classify_morpho_feed = classify_aggregator_feed


def combine_oracle_classifications(
    classifications: list[tuple[MorphoUnderlyingFeed | VenueOracleFeed, OracleClassification, str]],
    errors: list[str], *, chain: str, symbol: str, source_label: str = "Morpho feeds",
) -> OracleClassification:
    models = {(item.oracle_kind, item.behavior) for _feed, item, _description in classifications}
    if not errors and len(models) == 1:
        oracle_kind, behavior = next(iter(models))
        return OracleClassification(
            oracle_kind, behavior,
            " | ".join(item.reason for _feed, item, _description in classifications),
        )
    declared = [
        f"{feed.feed_address}={classification.oracle_kind}:{classification.behavior}"
        for feed, classification, _description in classifications
    ]
    declared.extend(errors)
    return OracleClassification(
        "unverified", "mixed_or_incomplete_feeds",
        f"COULD NOT VERIFY oracle_model_risk: {source_label} for {chain} {symbol} do not "
        f"resolve to one model: {declared}",
    )


class EvidenceLoader:
    def __init__(
        self,
        http: HttpClient,
        *,
        yields_pools: list[dict[str, Any]],
        protocols: list[dict[str, Any]],
        morpho_feeds: dict[tuple[int, str], set[MorphoUnderlyingFeed]] | None = None,
        morpho_oracle_errors: dict[int, str] | None = None,
        venue_oracles: VenueOracleClient | None = None,
    ) -> None:
        self.http = http
        self.llama = DefiLlamaClient(http)
        self.coingecko = CoinGeckoClient(http)
        self.ethplorer = EthplorerClient(http)
        self.etherscan = EtherscanClient(http)
        self.blockscout = BlockscoutClient(http)
        self.oracle = OracleClient(http)
        self.oracles = {
            chain_id: OracleClient(http, rpc_url=rpc_url)
            for chain_id, rpc_url in CHAIN_RPCS.items()
        }
        self.vault = VaultClient(http)
        self.yields_pools = yields_pools
        self.protocols = protocols
        self.morpho_feeds = morpho_feeds or {}
        self.morpho_oracle_errors = morpho_oracle_errors or {}
        self.venue_oracles = venue_oracles or VenueOracleClient(http)
        self._cache: dict[tuple[int, str, str, str], EvidenceBundle] = {}
        self._shared_cache: dict[tuple[int, str, str], _SharedEvidence] = {}
        self._price_disagreements: dict[tuple[int, str, str], float] = {}
        self._spot_price_disagreements: dict[tuple[int, str], float] = {}
        self._collateral_usd_cache: dict[tuple[int, str, str], tuple[TimedValue[float], ...]] = {}
        self._collateral_ratio_cache: dict[
            tuple[int, str, str, str], tuple[TimedValue[float], ...]
        ] = {}
        self._collateral_check_cache: dict[tuple[int, str, str, str, str], dict[str, Any]] = {}
        self._last_collateral_quote_at: float | None = None
        self._collateral_quote_deadline: float | None = None

    @classmethod
    def for_research_cache(
        cls,
        *,
        offline: bool,
        yields_pools: list[dict[str, Any]],
        protocols: list[dict[str, Any]],
    ) -> "EvidenceLoader":
        http = HttpClient(
            RESEARCH_DATA,
            offline=offline,
            delay_seconds=1.0,
            retries=2,
        )
        morpho_feeds: dict[tuple[int, str], set[MorphoUnderlyingFeed]] = {}
        morpho_errors: dict[int, str] = {}
        morpho = MorphoClient(http)
        for chain_id in sorted(CHAIN_NAMESPACES):
            try:
                for key, feeds in morpho.market_feeds_by_collateral((chain_id,)).items():
                    morpho_feeds.setdefault(key, set()).update(feeds)
            except Exception as exc:  # noqa: BLE001 - one unsupported chain must not erase others
                morpho_errors[chain_id] = str(exc)
        return cls(
            http,
            yields_pools=yields_pools,
            protocols=protocols,
            morpho_feeds=morpho_feeds,
            morpho_oracle_errors=morpho_errors,
        )

    def _observed_at(self) -> datetime:
        if not self.http.records:
            return self.http.utc_now()
        return datetime.fromisoformat(self.http.records[-1].fetched_at.replace("Z", "+00:00"))

    def _numeraire_series(
        self, numeraire: str, as_of: datetime, coin: str | None = None,
    ) -> tuple[dict[int, float], float | None]:
        """Daily USD price of the unit a non-USD instrument is a claim on.

        PT-wstETH measured against the dollar shows ETH's volatility as a depeg — hundreds of
        basis points of "credit defect" that are nothing of the kind. The peg question for a
        non-USD asset is whether it holds its ratio to its own unit, so that ratio is what gets
        measured, and the denominator is fetched rather than assumed.
        """
        coin = coin or NUMERAIRE_COINS.get(numeraire)
        if coin is None:
            raise RuntimeError(
                f"COULD NOT VERIFY peg_history: no priced numeraire for denomination {numeraire}"
            )
        payload = self.llama.coin_chart_sweep(
            coin,
            _daily_series_start(as_of),
            90,
            "1d",
            ("12h", "1d", "3d"),
            cache_name=f"numeraire_90d_utc_{coin.replace(':', '_').lower()}",
            minimum_points=60,
        )
        coins = payload.get("coins")
        entry = coins.get(coin) if isinstance(coins, dict) else None
        prices = entry.get("prices") if isinstance(entry, dict) else None
        if not isinstance(prices, list):
            raise RuntimeError(f"COULD NOT VERIFY peg_history: numeraire {numeraire} lost prices")
        llama_series: dict[int, float] = {}
        for row in prices:
            if not isinstance(row, dict):
                continue
            stamp, price = row.get("timestamp"), row.get("price")
            if isinstance(stamp, (int, float)) and isinstance(price, (int, float)) \
                    and not isinstance(stamp, bool) and not isinstance(price, bool) and price > 0:
                llama_series[_day_index(float(stamp))] = float(price)
        if not llama_series:
            raise RuntimeError(f"COULD NOT VERIFY peg_history: numeraire {numeraire} has no usable points")
        if len(llama_series) < 60:
            raise RuntimeError(
                f"COULD NOT VERIFY peg_history: numeraire {numeraire} has "
                f"{len(llama_series)} daily observations against the 60 needed"
            )
        if not coin.startswith("coingecko:") or not coin.partition(":")[2]:
            return llama_series, None
        coin_id = coin.partition(":")[2]
        try:
            cg_points = self.coingecko.coin_market_chart(
                coin_id, _daily_series_start(as_of), int(as_of.timestamp()),
                minimum_points=60,
            )
        except SourceError:
            return llama_series, None
        cg_series = {_day_index(point.timestamp): point.price for point in cg_points}
        series, gap, _note = corroborate_prices(
            llama_series, cg_series, minimum_points=60, field="peg_history",
        )
        return series, gap

    def _peg_series(
        self,
        chain_id: int,
        address: str,
        as_of: datetime,
        numeraire: str = "USD",
        numeraire_coin: str | None = None,
    ) -> tuple[TimedValue[float], ...]:
        namespace = CHAIN_NAMESPACES.get(chain_id)
        if namespace is None:
            raise RuntimeError(f"COULD NOT VERIFY peg_history: no DefiLlama namespace for chain {chain_id}")
        coin = f"{namespace}:{address.lower()}"
        payload = self.llama.coin_chart_sweep(
            coin,
            _daily_series_start(as_of),
            90,
            "1d",
            ("12h", "1d", "3d"),
            cache_name=f"peg_90d_utc_{namespace}_{address.lower()}",
            minimum_points=60,
        )
        coins = payload.get("coins")
        entry = coins.get(coin) if isinstance(coins, dict) else None
        prices = entry.get("prices") if isinstance(entry, dict) else None
        if not isinstance(prices, list):
            raise RuntimeError("COULD NOT VERIFY peg_history: response lost coins.*.prices")
        llama_series: dict[int, float] = {}
        for row in prices:
            if not isinstance(row, dict):
                continue
            stamp, price = row.get("timestamp"), row.get("price")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) \
                    and isinstance(price, (int, float)) and not isinstance(price, bool) \
                    and float(price) > 0:
                llama_series[_day_index(float(stamp))] = float(price)
        if len(llama_series) < 60:
            raise SourceError(
                f"COULD NOT VERIFY peg_history: {len(llama_series)} daily observations available "
                f"across searchWidth 12h/1d/3d, short of the 60 the 90-day rubric window requires"
            )
        asset_series, asset_gap = llama_series, None
        platform = COINGECKO_PLATFORMS.get(chain_id)
        if platform is None:
            note = "CoinGecko: no listing for this chain"
        else:
            try:
                cg_points = self.coingecko.contract_market_chart(
                    platform, address, _daily_series_start(as_of),
                    int(as_of.timestamp()), minimum_points=60,
                )
            except SourceError as exc:
                note = _coingecko_unavailable_note(exc)
            else:
                cg_series = (
                    {_day_index(point.timestamp): point.price for point in cg_points}
                    if cg_points is not None else None
                )
                asset_series, asset_gap, note = corroborate_prices(
                    llama_series, cg_series, minimum_points=60, field="peg_history",
                )
        source = f"GET https://coins.llama.fi/chart/{coin} (90-day daily closes); {note}"
        denominator: dict[int, float] | None = None
        numeraire_gap: float | None = None
        if numeraire != "USD":
            coin_key = numeraire_coin or NUMERAIRE_COINS.get(numeraire)
            denominator, numeraire_gap = self._numeraire_series(numeraire, as_of, coin_key)
            source = f"{source} divided by {coin_key} to price in {numeraire}"
        points = []
        for day, price in sorted(asset_series.items()):
            if denominator is not None:
                unit = denominator.get(day)
                if unit is None:
                    continue
                price = float(price) / unit
            timestamp = datetime.fromtimestamp(day * 86_400, UTC)
            if timestamp <= as_of:
                points.append(TimedValue(timestamp, float(price), source))
        if len(points) < 2:
            raise RuntimeError("COULD NOT VERIFY peg_history: fewer than two usable observations")
        observed = [gap for gap in (asset_gap, numeraire_gap) if gap is not None]
        price_key = (chain_id, address.lower(), numeraire)
        if observed:
            self._price_disagreements[price_key] = max(observed)
        else:
            self._price_disagreements.pop(price_key, None)
        return tuple(sorted(points, key=lambda point: point.timestamp))

    def collateral_usd_prices(
        self, chain_id: int, address: str, as_of: datetime,
    ) -> tuple[TimedValue[float], ...]:
        """Return DefiLlama daily USD closes for a token, or an empty evidence gap.

        This intentionally uses the peg-history cache key and sweep parameters, so a nightly
        snapshot warms the same immutable chart payload that the offline curve later reads.
        """
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        key = (chain_id, address.lower(), as_of.isoformat())
        if key in self._collateral_usd_cache:
            return self._collateral_usd_cache[key]
        namespace = CHAIN_NAMESPACES.get(chain_id)
        if namespace is None:
            self._collateral_usd_cache[key] = ()
            return ()
        coin = f"{namespace}:{address.lower()}"
        try:
            payload = self.llama.coin_chart_sweep(
                coin,
                _daily_series_start(as_of),
                90,
                "1d",
                ("12h", "1d", "3d"),
                cache_name=f"peg_90d_utc_{namespace}_{address.lower()}",
                minimum_points=60,
            )
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - offline misses are an evidence gap
            self._collateral_usd_cache[key] = ()
            return ()
        coins = payload.get("coins") if isinstance(payload, dict) else None
        entry = coins.get(coin) if isinstance(coins, dict) else None
        prices = entry.get("prices") if isinstance(entry, dict) else None
        if not isinstance(prices, list):
            self._collateral_usd_cache[key] = ()
            return ()
        by_day: dict[int, tuple[datetime, float]] = {}
        start = _daily_series_start(as_of)
        for row in prices:
            if not isinstance(row, dict):
                continue
            stamp, price = row.get("timestamp"), row.get("price")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
                continue
            stamp_value = float(stamp)
            if not math.isfinite(stamp_value):
                continue
            try:
                observed = datetime.fromtimestamp(stamp_value, UTC)
            except (OverflowError, OSError, ValueError):
                continue
            if observed > as_of:
                raise AssertionError(
                    f"anti-lookahead: {coin} datapoint {observed.isoformat()} is after as_of "
                    f"{as_of.isoformat()}"
                )
            if stamp_value < start:
                continue
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                continue
            value = float(price)
            if not (math.isfinite(value) and value > 0):
                continue
            day = _day_index(stamp_value)
            timestamp = datetime.fromtimestamp(day * 86_400, UTC)
            if timestamp <= as_of:
                by_day[day] = (timestamp, value)
        if len(by_day) < 60:
            self._collateral_usd_cache[key] = ()
            return ()
        source = f"GET https://coins.llama.fi/chart/{coin} (90-day daily closes)"
        result = tuple(
            TimedValue(timestamp, value, source)
            for timestamp, value in (by_day[day] for day in sorted(by_day))
        )
        self._collateral_usd_cache[key] = result
        return result

    def collateral_prices(
        self, chain_id: int, address: str, loan_address: str, as_of: datetime,
    ) -> tuple[TimedValue[float], ...]:
        """Return collateral/loan daily closes aligned on common UTC grid days."""
        key = (chain_id, address.lower(), loan_address.lower(), as_of.isoformat())
        if key in self._collateral_ratio_cache:
            return self._collateral_ratio_cache[key]
        collateral = self.collateral_usd_prices(chain_id, address, as_of)
        loan = self.collateral_usd_prices(chain_id, loan_address, as_of)
        collateral_by_day = {_day_index(point.timestamp.timestamp()): point for point in collateral}
        loan_by_day = {_day_index(point.timestamp.timestamp()): point for point in loan}
        common_days = sorted(collateral_by_day.keys() & loan_by_day.keys())
        if len(common_days) < 60:
            self._collateral_ratio_cache[key] = ()
            return ()
        source = (
            f"DefiLlama daily close ratio {CHAIN_NAMESPACES.get(chain_id, 'unknown')}:"
            f"{address.lower()}/{loan_address.lower()}"
        )
        result = tuple(
            TimedValue(
                collateral_by_day[day].timestamp,
                collateral_by_day[day].value / loan_by_day[day].value,
                source,
            )
            for day in common_days
        )
        self._collateral_ratio_cache[key] = result
        return result

    @staticmethod
    def _decode_rpc_word(payload: Any, endpoint: str) -> int:
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
            raise SourceError(f"{endpoint}: invalid ABI word")
        encoded = result[2:]
        return int(encoded, 16)

    def _collateral_rpc_word(
        self, chain_id: int, address: str, data: str, *, cache_name: str,
        block_tag: str,
    ) -> int:
        endpoints = endpoints_for(chain_id)
        payload, endpoint = request_rpc_json(
            self.http, chain_id, endpoints, cache_name=cache_name, method="eth_call",
            params=[{"to": address, "data": data}, block_tag],
            operation=f"eth_call {address} at {block_tag}",
            validate=lambda value, where: self._valid_rpc_word(value, where),
            prefer_cache_minutes=20 * 60,
        )
        return self._decode_rpc_word(payload, endpoint)

    def _collateral_token_decimals(self, chain_id: int, token: str, *,
                                   cache_name: str, block_tag: str) -> int:
        payload, endpoint = request_rpc_json(
            self.http, chain_id, endpoints_for(chain_id), cache_name=cache_name,
            method="eth_call", params=[{"to": token, "data": selector("decimals()")}, block_tag],
            operation=f"eth_call {token} decimals() at {block_tag}",
            validate=lambda value, where: self._valid_decimals(value, where),
            prefer_cache_minutes=20 * 60,
        )
        return decode_decimals(payload, endpoint)

    @staticmethod
    def _valid_decimals(payload: Any, endpoint: str) -> bool:
        try:
            decode_decimals(payload, endpoint)
        except SourceError:
            return False
        return True

    @classmethod
    def _valid_rpc_word(cls, payload: Any, endpoint: str) -> bool:
        try:
            cls._decode_rpc_word(payload, endpoint)
        except SourceError:
            return False
        return True

    def _collateral_snapshot_block(self, chain_id: int, as_of: datetime, oracle: str) -> tuple[str, datetime]:
        # One fresh block per oracle, cached under the oracle's own name so the offline curve reads the
        # same calls back. A pointer reused for hours made every read an archive request, which public
        # RPCs refuse, and the oracle then read as unreadable.
        cache_name = f"credit_collateral_block_{chain_id}_{oracle.lower()}"
        endpoints = endpoints_for(chain_id)
        def valid(payload: Any, endpoint: str) -> bool:
            try:
                decode_block_timestamp(payload, endpoint)
                result = payload.get("result")
                return isinstance(result, dict) and isinstance(result.get("number"), str)
            except SourceError:
                return False
        payload, endpoint = request_rpc_json(
            self.http, chain_id, endpoints, cache_name=cache_name,
            method="eth_getBlockByNumber", params=["latest", False],
            operation="eth_getBlockByNumber latest", validate=valid,
        )
        block_time = decode_block_timestamp(payload, endpoint)
        number = payload["result"]["number"]
        # Block clocks run a little ahead of ours; two minutes is drift, not lookahead.
        if (block_time > as_of.astimezone(UTC) + timedelta(seconds=120)
                or not re.fullmatch(r"0x[0-9a-fA-F]+", number)):
            raise SourceError("snapshot block is after the requested as_of time")
        return number, block_time

    def _classify_collateral_feeds(
        self, chain_id: int, oracle: str, *, block_tag: str, block_time: datetime,
        key: str,
    ) -> str:
        """Read optional ChainlinkOracleV2 feed getters and classify identified feeds."""
        identified: set[str] = set()
        for signature in _FEED_GETTERS:
            try:
                word = self._collateral_rpc_word(
                    chain_id, oracle, selector(signature),
                    cache_name=f"credit_collateral_{key}_getter_{signature[:-2].lower()}",
                    block_tag=block_tag,
                )
                if word and word < 2**160:
                    identified.add(f"0x{word:040x}")
            except SourceError:  # optional getters are absent on non-Chainlink oracle models
                continue
        if not identified:
            return "feeds unidentified"
        models: list[str] = []
        for feed_address in sorted(identified):
            try:
                decimals = self._collateral_token_decimals(
                    chain_id, feed_address,
                    cache_name=f"credit_collateral_{key}_feed_decimals_{feed_address}",
                    block_tag=block_tag,
                )
                endpoint = f"chain {chain_id} feed {feed_address}"
                round_payload, round_endpoint = request_rpc_json(
                    self.http, chain_id, endpoints_for(chain_id),
                    cache_name=f"credit_collateral_{key}_feed_round_{feed_address}",
                    method="eth_call", params=[{"to": feed_address, "data": LATEST_ROUND_DATA}, block_tag],
                    operation=f"eth_call {feed_address} latestRoundData() at {block_tag}",
                    validate=lambda value, where: self._valid_round(value, where),
                    prefer_cache_minutes=20 * 60,
                )
                answer, updated_at = decode_latest_round_data(round_payload, round_endpoint)
                desc_payload, desc_endpoint = request_rpc_json(
                    self.http, chain_id, endpoints_for(chain_id),
                    cache_name=f"credit_collateral_{key}_feed_description_{feed_address}",
                    method="eth_call", params=[{"to": feed_address, "data": DESCRIPTION}, block_tag],
                    operation=f"eth_call {feed_address} description() at {block_tag}",
                    validate=lambda value, where: self._valid_description(value, where),
                    prefer_cache_minutes=20 * 60,
                )
                description = decode_abi_string(desc_payload, desc_endpoint)
                if updated_at is not None and updated_at > block_time:
                    models.append("unverified")
                    continue
                if updated_at is None and answer > 0 and "exchange rate" in description.casefold():
                    # e.g. "weETH/ETH exchange rate": computed from the token's own rate when called, so
                    # it stamps no time and staleness does not apply. Whether that rate matches the
                    # market is what the oracle-deviation check measures.
                    models.append("exchange_rate_adapter")
                    continue
                feed = MorphoUnderlyingFeed(oracle, "collateral oracle", feed_address,
                                            int(decimals), None, None)
                classified = classify_aggregator_feed(
                    feed, OracleRound(feed_address, answer, updated_at, endpoint),
                    description, block_time,
                )
                models.append(classified.behavior)
            except SourceError:  # an identified but unreadable feed is unverified
                models.append("unverified")
        if "unverified" in models or "updated_at_zero" in models:
            known = sorted(set(models) - {"unverified", "updated_at_zero"})
            return "+".join([*known, "unverified"]) if known else "unverified"
        unique = sorted(set(models))
        return "+".join(unique)

    @staticmethod
    def _valid_round(payload: Any, endpoint: str) -> bool:
        try:
            decode_latest_round_data(payload, endpoint)
        except SourceError:
            return False
        return True

    @staticmethod
    def _valid_description(payload: Any, endpoint: str) -> bool:
        try:
            decode_abi_string(payload, endpoint)
        except SourceError:
            return False
        return True

    @staticmethod
    def _quote_size(total_units: float) -> float:
        try:
            size = float(total_units)
        except (TypeError, ValueError):
            size = COLLATERAL_QUOTE_SIZE_USD[0]
        if not math.isfinite(size):
            size = COLLATERAL_QUOTE_SIZE_USD[0]
        return min(COLLATERAL_QUOTE_SIZE_USD[1],
                   max(COLLATERAL_QUOTE_SIZE_USD[0], size))

    @staticmethod
    def _market_value_from_oracle(price_raw: int, collateral_decimals: int,
                                  loan_decimals: int) -> float:
        """Whole collateral units in whole loan-token units, per Midnight's 1e36 scale."""
        return price_raw * 10**collateral_decimals / 1e36 / 10**loan_decimals

    def _liquidation_quote(
        self, chain_id: int, token: str, loan_address: str, *, token_decimals: int,
        loan_decimals: int, size_usd: float, oracle_value: float,
        collateral_usd: float, key: str,
        as_of: datetime,
    ) -> tuple[float, float] | None:
        if not all(math.isfinite(value) and value > 0
                   for value in (size_usd, collateral_usd, oracle_value)):
            return None
        amount_raw = int(size_usd / collateral_usd * 10**token_decimals)
        cache_name = (f"credit_collateral_liquidation_{key}_{size_usd:g}_"
                      f"{token_decimals}_{loan_decimals}")
        path = self.http._latest_path("credit", cache_name)
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                stamp = datetime.fromisoformat(cached["fetched_at"])
                if stamp > as_of.astimezone(UTC):
                    return None
                if ((as_of.astimezone(UTC) - stamp).total_seconds()
                        <= COLLATERAL_QUOTE_CACHE_HOURS * 3600):
                    if cached.get("unavailable") is True:
                        return None
                    sold_raw = int(cached["sold_raw"])
                    received_raw = int(cached["received_raw"])
                    if sold_raw <= 0 or received_raw < 0:
                        raise ValueError("cached quote amount is invalid")
                    # The quote is evidence for the amount it sold. If the collateral's price has
                    # moved since, that amount no longer covers the size asked for; quote afresh.
                    if abs(sold_raw - amount_raw) > COLLATERAL_QUOTE_AMOUNT_TOLERANCE * amount_raw:
                        raise ValueError("cached quote sold a different amount")
                    sold_value = sold_raw / 10**token_decimals * oracle_value
                    if sold_value <= 0:
                        raise ValueError("cached quote has no oracle value")
                    impact_bp = (1.0 - (received_raw / 10**loan_decimals) / sold_value) * 10_000
                    if not math.isfinite(impact_bp):
                        raise ValueError("cached quote impact is not finite")
                    return size_usd, impact_bp
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        if getattr(self.http, "offline", False) or oracle_value <= 0:
            return None
        if amount_raw <= 0:
            return None
        try:
            from research.par_sources import lifi_quotes
            if self._collateral_quote_deadline is None:
                self._collateral_quote_deadline = time.monotonic() + COLLATERAL_QUOTE_BUDGET_SECONDS
            remaining = self._collateral_quote_deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self._last_collateral_quote_at is not None:
                wait = lifi_quotes.PACE_SECONDS - (time.monotonic() - self._last_collateral_quote_at)
                if wait >= remaining:
                    return None
                if wait > 0:
                    time.sleep(wait)
            self._last_collateral_quote_at = time.monotonic()
            route = fetch_exact_sale(
                chain_id, token,
                receive_with=PayAsset(loan_address, loan_decimals),
                amount_raw=amount_raw, decimals=token_decimals,
                api_key=os.environ.get("LIFI_API_KEY") or None,
                deadline=min(time.monotonic() + 40, self._collateral_quote_deadline),
            )
            received_raw = route.binding.to_amount_min
            sold_value = (amount_raw / 10**token_decimals) * oracle_value
            impact_bp = (1.0 - (received_raw / 10**loan_decimals) / sold_value) * 10_000
            payload = {"fetched_at": route.fetched_at.isoformat(), "sold_raw": str(amount_raw),
                       "received_raw": str(received_raw)}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
            if route.fetched_at > as_of.astimezone(UTC):
                return None
            return size_usd, impact_bp
        except Exception:  # noqa: BLE001 - quote failures are missing evidence, not a market result
            # A missing route remains missing offline too. Cache failures so shared collateral
            # does not repeatedly consume routing quota during the same nightly run.
            try:
                path.write_text(json.dumps({"fetched_at": datetime.now(UTC).isoformat(),
                                            "unavailable": True}), encoding="utf-8")
            except OSError:
                pass
            return None

    def collateral_checks(
        self, chain_id: int, token: str, oracle: str, loan_address: str,
        total_units: int | str | None, as_of: datetime,
    ) -> dict[str, Any]:
        """Return evidence for one Midnight collateral; ``total_units`` is raw loan-token units."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        token, oracle, loan_address = token.lower(), oracle.lower(), loan_address.lower()
        key = (chain_id, token, oracle, loan_address,
               f"{as_of.astimezone(UTC).isoformat()}_{total_units}")
        if key in self._collateral_check_cache:
            return dict(self._collateral_check_cache[key])
        result: dict[str, Any] = {
            "oracle_price_raw": None, "oracle_market_deviation_bp": None,
            "feed_model": "unverified", "liquidation_size_usd": None,
            "liquidation_impact_bp": None, "collateral_decimals": None,
            "loan_decimals": None,
        }
        identity = f"{chain_id}_{token}_{oracle}"
        try:
            block_tag, block_time = self._collateral_snapshot_block(chain_id, as_of, oracle)
            identity = f"{identity}_{block_tag}"
            try:
                price = self._collateral_rpc_word(
                    chain_id, oracle, _ORACLE_PRICE,
                    cache_name=f"credit_collateral_{identity}_price",
                    block_tag=block_tag,
                )
                result["oracle_price_raw"] = price if price > 0 else None
            except Exception:  # noqa: BLE001 - preserve other checks if price() is unreadable
                price = 0
            for name, address in (("collateral", token), ("loan", loan_address)):
                try:
                    decimals = self._collateral_token_decimals(
                        chain_id, address,
                        cache_name=f"credit_collateral_{identity}_{name}_{address}_decimals",
                        block_tag=block_tag,
                    )
                    result[f"{name}_decimals"] = decimals
                except SourceError:  # decimals failure only blocks scaling-dependent checks
                    pass
            result["feed_model"] = self._classify_collateral_feeds(
                chain_id, oracle, block_tag=block_tag, block_time=block_time, key=identity,
            )
            if (price > 0 and result["collateral_decimals"] is not None
                    and result["loan_decimals"] is not None):
                c_dec = result["collateral_decimals"]
                l_dec = result["loan_decimals"]
                try:
                    loan_units = int(total_units) / 10**l_dec
                except (TypeError, ValueError, OverflowError):
                    loan_units = 0.0
                oracle_value = self._market_value_from_oracle(price, c_dec, l_dec)
                ratios = self.collateral_prices(chain_id, token, loan_address, as_of)
                if ratios and ratios[-1].value > 0 and oracle_value > 0:
                    result["oracle_market_deviation_bp"] = abs(
                        oracle_value - ratios[-1].value
                    ) / ratios[-1].value * 10_000
                loan_points = self.collateral_usd_prices(chain_id, loan_address, as_of)
                loan_usd = loan_points[-1].value if loan_points else 0.0
                if math.isfinite(loan_usd) and loan_usd > 0:
                    quote_size = self._quote_size(loan_units * loan_usd)
                    quote = self._liquidation_quote(
                        chain_id, token, loan_address, token_decimals=c_dec,
                        loan_decimals=l_dec, size_usd=quote_size, oracle_value=oracle_value,
                        collateral_usd=(usd_points[-1].value if (usd_points :=
                                        self.collateral_usd_prices(chain_id, token, as_of)) else 0.0),
                        key=f"{chain_id}_{token}_{loan_address}", as_of=as_of,
                    )
                    if quote is not None:
                        result["liquidation_size_usd"], result["liquidation_impact_bp"] = quote
        except SourceError:  # offline cache misses leave optional fields unavailable
            pass
        self._collateral_check_cache[key] = dict(result)
        return result

    def _vault_backing(
        self, chain_id: int, address: str, registry_reason: str,
    ) -> tuple[str, tuple[TimedValue[float], ...], str | None] | None:
        """Read a vault token's own backing when no stablecoin issuer covers it.

        Strategy tokens (mHYPER, mixUSDC, yoUSD, eEARN, tmvUSDC...) have no issuer in the
        stablecoin registry, and matching them by name against DefiLlama's protocol list is
        ambiguous in the wrong direction — `PRIME` hits both DeltaPrime and Echelon Prime, and
        the vault turned out to be neither. The contract answers unambiguously; only the level
        does, though, so the 30-day trend stays a declared gap rather than a guess.
        """
        namespace = CHAIN_NAMESPACES.get(chain_id)
        if namespace is None:
            return None
        try:
            backing = self.vault.backing(chain_id, address)
        except Exception:  # noqa: BLE001 - contract evidence stays a declared registry gap
            return None
        coin = f"{namespace}:{backing.unit_token}"
        try:
            llama_price, llama_source = self.llama.current_price(coin)
        except SourceError as exc:
            return (
                f"vault:{chain_id}:{backing.vault_address}", (),
                f"COULD NOT VERIFY protocol_tvl: primary price unavailable for "
                f"{backing.unit_token}: {exc}",
            )
        # Corroboration is diagnostic: availability and agreement never change the mark.
        price = llama_price
        price_source = f"{llama_source}; DefiLlama price {llama_price:g}"
        price_key = (chain_id, address.lower())
        self._spot_price_disagreements.pop(price_key, None)
        platform = COINGECKO_PLATFORMS.get(chain_id)
        if platform is None:
            price_source += f"; CoinGecko: no platform for chain {chain_id}"
        else:
            try:
                cg_price, cg_source = self.coingecko.contract_spot(platform, backing.unit_token)
            except SourceError as exc:
                price_source += f"; {_coingecko_unavailable_note(exc)}"
            else:
                gap = abs(llama_price - cg_price) / max(llama_price, cg_price) * 10_000
                price_source += (
                    f"; {cg_source}; CoinGecko price {cg_price:g}; "
                    f"disagreement {gap:.1f}bp; DefiLlama price kept"
                )
                self._spot_price_disagreements[price_key] = gap
        value = backing.units * price
        if value <= 0:
            return None
        source = (
            f"{backing.endpoint}; {backing.kind}; priced by {price_source}; "
            f"no stablecoin issuer covers this token ({registry_reason})"
        )
        return (
            f"vault:{chain_id}:{backing.vault_address}",
            (TimedValue(self._observed_at(), value, source),),
            "COULD NOT VERIFY protocol_tvl_trend: on-chain backing gives today's level only, "
            "and a 30-day comparison would need archive state this node does not serve",
        )

    def _pool_tvl_series(
        self, address: str, chain: str, as_of: datetime,
    ) -> tuple[tuple[TimedValue[float], ...], str | None]:
        """Thirty-day TVL for the venue's own pool, when no issuer covers the token.

        `protocol_tvl_trend` normally comes from a stablecoin issuer's circulating supply. Most
        Pendle and Spectra underlyings have no issuer in that registry, so the loader fell back to
        reading vault backing on chain — which answers for today and cannot answer for last month,
        because a 30-day comparison needs archive state no free node serves. That left 74
        instruments unresolved on a field DefiLlama publishes for nothing.

        This is deliberately a DIFFERENT QUANTITY and its source string says so. Issuer circulating
        supply measures how much of the asset exists; a pool's TVL measures how much sits in this
        particular market. For a credit read the second is arguably the better signal — a market
        being abandoned is more informative than an issuer's total float — but they are not
        interchangeable, and a reader must never see one presented as the other.
        """
        target = address.lower()
        candidates = [
            pool for pool in self.yields_pools
            if str(pool.get("chain")) == chain
            and any(str(t).lower() == target for t in (pool.get("underlyingTokens") or []))
            and pool.get("pool")
        ]
        if not candidates:
            return (), (
                "COULD NOT VERIFY protocol_tvl_trend: no DefiLlama pool lists this token as an "
                "underlying, so there is no pool-level series to fall back to"
            )
        pool = max(candidates, key=lambda item: item.get("tvlUsd") or 0)
        pool_id = str(pool["pool"])
        try:
            rows = self.llama.yield_chart(pool_id)
        except Exception as exc:  # noqa: BLE001 - a source failure stays a field-specific gap
            return (), f"COULD NOT VERIFY protocol_tvl_trend: pool chart unavailable: {exc}"

        series: dict[int, float] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            stamp, value = row.get("timestamp"), row.get("tvlUsd")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                continue
            moment = _parse_timestamp(stamp)
            if moment is None or moment > as_of:
                continue
            series[int(moment.timestamp()) // 86_400] = float(value)
        if len(series) < 2:
            return (), (
                f"COULD NOT VERIFY protocol_tvl_trend: pool {pool_id} returned "
                f"{len(series)} usable daily observations"
            )
        source = (
            f"GET https://yields.llama.fi/chart/{pool_id} — POOL TVL for {pool.get('project')} "
            f"on {chain}, NOT issuer circulating supply: this measures the size of this market "
            f"rather than how much of the asset exists"
        )
        latest_day = max(series)
        target_day = latest_day - 30
        prior_day = min(series, key=lambda day: abs(day - target_day))
        if prior_day == latest_day:
            return (), (
                "COULD NOT VERIFY protocol_tvl_trend: pool chart has no observation about "
                "thirty days before the latest one"
            )
        points = tuple(
            TimedValue(datetime.fromtimestamp(day * 86_400, UTC), series[day], source)
            for day in sorted((prior_day, latest_day))
        )
        return points, None

    def _tvl_series(
        self,
        symbol: str,
        address: str,
        chain: str,
        chain_id: int,
        as_of: datetime,
    ) -> tuple[
        str | None, tuple[TimedValue[float], ...], str | None,
        str | None, str | None, str | None,
    ]:
        stablecoins = self.llama.stablecoins()
        resolution = resolve_stablecoin_issuer(
            symbol, address, chain, stablecoins,
            detail_loader=self.llama.stablecoin_detail,
        )
        stablecoin_key = None if resolution.stablecoin_id is None else f"stablecoin:{resolution.stablecoin_id}"
        if resolution.circulating_usd is None:
            backing = self._vault_backing(chain_id, address, resolution.reason)
            if backing is not None:
                # Backing answers for today only. If a pool-level series exists it can answer for
                # last month too, which is what the trend actually needs.
                if len(backing[1]) < 2:
                    pool_points, pool_error = self._pool_tvl_series(address, chain, as_of)
                    if pool_points:
                        return (
                            backing[0], pool_points, None, resolution.issuer_name,
                            resolution.peg_mechanism, resolution.peg_type,
                        )
                    backing = (backing[0], backing[1], pool_error or backing[2])
                return (
                    backing[0], backing[1], backing[2], resolution.issuer_name,
                    resolution.peg_mechanism, resolution.peg_type,
                )
            return (
                stablecoin_key, (), resolution.reason, resolution.issuer_name,
                resolution.peg_mechanism, resolution.peg_type,
            )
        observed_at = self._observed_at()
        source = (
            "GET https://stablecoins.llama.fi/stablecoins?includePrices=true; "
            f"{resolution.reason}; pegMechanism={resolution.peg_mechanism or 'COULD NOT VERIFY'}"
        )
        points = [TimedValue(observed_at, resolution.circulating_usd, source)]
        if resolution.circulating_prev_month_usd is not None:
            points.insert(0, TimedValue(
                observed_at - timedelta(days=30),
                resolution.circulating_prev_month_usd,
                source,
            ))
        return (
            stablecoin_key, tuple(points), None if len(points) == 2 else resolution.reason,
            resolution.issuer_name, resolution.peg_mechanism, resolution.peg_type,
        )

    def _load_shared(
        self,
        *,
        chain_id: int,
        chain: str,
        underlying_symbol: str,
        underlying_address: str,
        as_of: datetime,
        numeraire: str = "USD",
        numeraire_coin: str | None = None,
    ) -> _SharedEvidence:
        price_key = (chain_id, underlying_address.lower(), numeraire)
        if price_key in self._shared_cache:
            return self._shared_cache[price_key]
        unresolved: list[tuple[str, str]] = []
        try:
            peg = self._peg_series(chain_id, underlying_address, as_of, numeraire, numeraire_coin)
        except Exception as exc:  # noqa: BLE001 - source failures remain field-specific evidence gaps
            peg = ()
            unresolved.append(("peg_history", str(exc)))
        try:
            slug, tvl, tvl_error, issuer_name, peg_mechanism, peg_type = self._tvl_series(
                underlying_symbol, underlying_address, chain, chain_id, as_of
            )
        except Exception as exc:  # noqa: BLE001
            slug, tvl, tvl_error = None, (), str(exc)
            issuer_name = peg_mechanism = peg_type = None
        if not tvl:
            unresolved.append(("protocol_tvl", tvl_error or "COULD NOT VERIFY protocol_tvl"))
        if tvl_error or len(tvl) < 2:
            unresolved.append((
                "protocol_tvl_trend",
                tvl_error or "COULD NOT VERIFY protocol_tvl_trend: fewer than two issuer observations",
            ))

        # The reference a non-dollar asset falls from. A dollar stablecoin has par; an accruing
        # asset has whatever its contract says one share redeems for, and measuring the market
        # price against that makes 100bp mean the same thing in every denomination. Measured
        # 2026-08-19: wstETH reads 177bp of 90-day ratio drawdown against ETH while sitting 77bp
        # below redemption, and sENA trades 36bp ABOVE it — a premium, which is not a defect.
        redemption_ratio = market_ratio = redemption_discount_bp = None
        redemption_source = None
        if numeraire != "USD" and peg:
            try:
                redemption_ratio, redemption_source = self.vault.redemption_ratio(
                    chain_id, underlying_address,
                )
                market_ratio = peg[-1].value
                if redemption_ratio > 0:
                    redemption_discount_bp = max(
                        0.0, (redemption_ratio - market_ratio) / redemption_ratio * 10_000,
                    )
            except Exception as exc:  # noqa: BLE001 - no reference stays a declared gap
                unresolved.append(("redemption_ratio", str(exc)))

        holder = None
        authenticated_gap = None
        try:
            holder_evidence = self.etherscan.top_token_holders(chain_id, underlying_address)
            observed_at = self._observed_at()
            holder = TimedValue(
                observed_at,
                holder_evidence.top_holder_share,
                f"{holder_evidence.endpoint}; authenticated Etherscan V2; "
                "largest holder among returned top 10",
            )
        except Exception as exc:  # noqa: BLE001 - paid source is optional until a key exists
            authenticated_gap = str(exc)
            unresolved.append(("explorer_authentication", authenticated_gap))
        if holder is None and chain_id != 1:
            try:
                holder_evidence = self.blockscout.top_token_holders(chain_id, underlying_address)
                observed_at = self._observed_at()
                holder = TimedValue(
                    observed_at,
                    holder_evidence.top_holder_share,
                    f"{holder_evidence.endpoint}; Blockscout free fallback after "
                    f"{authenticated_gap}; largest holder among returned top 10",
                )
            except Exception as exc:  # noqa: BLE001
                unresolved.append(("holder_concentration", str(exc)))
        elif holder is None:
            try:
                holder_evidence = self.ethplorer.top_token_holders(underlying_address)
                observed_at = self._observed_at()
                holder = TimedValue(
                    observed_at,
                    holder_evidence.top_holder_share,
                    f"{holder_evidence.endpoint}; Ethplorer free fallback after "
                    f"{authenticated_gap}; largest holder among returned top 10",
                )
            except Exception as exc:  # noqa: BLE001
                unresolved.append(("holder_concentration", str(exc)))

        shared = _SharedEvidence(
            peg_prices=peg,
            protocol_tvl_usd=tvl,
            holder_top10_share=holder,
            protocol_slug=slug,
            issuer_name=issuer_name,
            peg_mechanism=peg_mechanism,
            peg_type=peg_type,
            redemption_ratio=redemption_ratio,
            market_ratio=market_ratio,
            redemption_discount_bp=redemption_discount_bp,
            redemption_source=redemption_source,
            unresolved=tuple(unresolved),
        )
        self._shared_cache[price_key] = shared
        return shared

    def load(
        self,
        *,
        chain_id: int,
        chain: str,
        underlying_symbol: str,
        underlying_address: str,
        as_of: datetime,
        oracle_feed: FeedDefinition | None = None,
        numeraire: str = "USD",
        numeraire_coin: str | None = None,
        venue_oracle_context: VenueOracleContext | None = None,
    ) -> EvidenceBundle:
        price_key = (chain_id, underlying_address.lower(), numeraire)
        oracle_identity = "" if venue_oracle_context is None else venue_oracle_context.identity
        key = (*price_key, oracle_identity)
        if key in self._cache:
            return self._cache[key]
        shared = self._load_shared(
            chain_id=chain_id,
            chain=chain,
            underlying_symbol=underlying_symbol,
            underlying_address=underlying_address,
            as_of=as_of,
            numeraire=numeraire,
            numeraire_coin=numeraire_coin,
        )
        unresolved = list(shared.unresolved)
        # Morpho's feed index is keyed by contract alone; the numeraire is ours, not theirs.
        feed_key = (chain_id, underlying_address.lower())
        feed = oracle_feed or DOCUMENTED_FEEDS.get((chain_id, underlying_symbol.upper()))
        oracle_updated_at = None
        oracle_heartbeat = None
        oracle_kind = None
        oracle_behavior = None
        oracle_description = None
        oracle_reason = None
        if feed is None:
            venue_feeds = self.morpho_feeds.get(feed_key, set())
            if not venue_feeds \
                    and venue_oracle_context is not None \
                    and venue_oracle_context.venue in SUBSCRIBED_VENUES:
                classifications: list[
                    tuple[MorphoUnderlyingFeed | VenueOracleFeed, OracleClassification, str]
                ] = []
                classification_errors: list[str] = []
                try:
                    observations = self.venue_oracles.resolve(chain_id, venue_oracle_context)
                    if not observations:
                        raise RuntimeError(
                            f"COULD NOT VERIFY oracle_model_risk: {venue_oracle_context.venue} "
                            "returned no exact venue oracle observations"
                        )
                    for observation in observations:
                        classification = classify_aggregator_feed(
                            observation.feed, observation.round_data, observation.description,
                            observation.block_timestamp,
                        )
                        classifications.append(
                            (observation.feed, classification, observation.description)
                        )
                except Exception as exc:  # noqa: BLE001 - venue failures are evidence gaps
                    reason = str(exc)
                    if not reason.startswith("COULD NOT VERIFY oracle_model_risk:"):
                        reason = (
                            f"COULD NOT VERIFY oracle_model_risk: "
                            f"{venue_oracle_context.venue} venue oracle could not be read: {reason}"
                        )
                    classification_errors.append(reason)
                combined = combine_oracle_classifications(
                    classifications, classification_errors,
                    chain=chain, symbol=underlying_symbol,
                    source_label=f"{venue_oracle_context.venue} venue feeds",
                )
                oracle_kind, oracle_behavior, oracle_reason = (
                    combined.oracle_kind, combined.behavior, combined.reason,
                )
                if oracle_kind != "unverified":
                    oracle_description = " | ".join(
                        description for _feed, _item, description in classifications
                    )
                else:
                    unresolved.append(("oracle_model_risk", oracle_reason))
            else:
                if venue_feeds:
                    classifications = []
                    classification_errors = []
                    oracle_client = self.oracles.get(chain_id)
                    for venue_feed in sorted(venue_feeds, key=lambda value: value.feed_address):
                        try:
                            if oracle_client is None:
                                raise RuntimeError(f"no verified RPC for chain {chain_id}")
                            cache_identity = f"{chain_id}_{venue_feed.feed_address}"
                            round_data = oracle_client.probe_latest_round_data(
                                venue_feed.feed_address,
                                cache_name=f"morpho_feed_latest_round_{cache_identity}",
                            )
                            description = oracle_client.description(
                                venue_feed.feed_address,
                                cache_name=f"morpho_feed_description_{cache_identity}",
                            )
                            block_timestamp = oracle_client.latest_block_timestamp(
                                cache_name=f"morpho_feed_block_{cache_identity}",
                            )
                            classification = classify_aggregator_feed(
                                venue_feed, round_data, description, block_timestamp,
                            )
                            classifications.append((venue_feed, classification, description))
                        except Exception as exc:  # noqa: BLE001
                            classification_errors.append(
                                f"feed {venue_feed.feed_address} could not be classified: {exc}"
                            )
                    combined = combine_oracle_classifications(
                        classifications, classification_errors,
                        chain=chain, symbol=underlying_symbol,
                    )
                    oracle_kind, oracle_behavior, oracle_reason = (
                        combined.oracle_kind, combined.behavior, combined.reason,
                    )
                    if oracle_kind != "unverified":
                        oracle_description = " | ".join(
                            description for _feed, _item, description in classifications
                        )
                    else:
                        unresolved.append(("oracle_model_risk", oracle_reason))
                else:
                    chain_error = self.morpho_oracle_errors.get(chain_id)
                    suffix = f"; Morpho lookup failed: {chain_error}" if chain_error else ""
                    oracle_reason = (
                        f"COULD NOT VERIFY oracle_model_risk: no exact Morpho baseFeedOne for "
                        f"{chain} {underlying_symbol}{suffix}"
                    )
                    unresolved.append(("oracle_model_risk", oracle_reason))
        else:
            try:
                oracle_evidence = self.oracle.latest_round_data(
                    feed.address,
                    heartbeat_seconds=feed.heartbeat_seconds,
                    heartbeat_source=feed.heartbeat_source,
                )
                observed_at = self._observed_at()
                oracle_updated_at = TimedValue(
                    observed_at, oracle_evidence.updated_at, oracle_evidence.endpoint
                )
                oracle_heartbeat = TimedValue(
                    observed_at,
                    oracle_evidence.heartbeat_seconds,
                    oracle_evidence.heartbeat_source,
                )
                oracle_kind = "genuine_feed"
                oracle_behavior = "documented_heartbeat"
                oracle_description = underlying_symbol.upper() + "/USD"
                oracle_reason = (
                    f"genuine feed scored against documented heartbeat: {feed.heartbeat_source}"
                )
            except Exception as exc:  # noqa: BLE001
                unresolved.append(("oracle_model_risk", str(exc)))

        bundle = EvidenceBundle(
            peg_prices=shared.peg_prices,
            protocol_tvl_usd=shared.protocol_tvl_usd,
            oracle_updated_at=oracle_updated_at,
            oracle_heartbeat_seconds=oracle_heartbeat,
            holder_top10_share=shared.holder_top10_share,
            protocol_slug=shared.protocol_slug,
            issuer_name=shared.issuer_name,
            peg_mechanism=shared.peg_mechanism,
            peg_type=shared.peg_type,
            oracle_kind=oracle_kind,
            oracle_behavior=oracle_behavior,
            oracle_description=oracle_description,
            oracle_reason=oracle_reason,
            numeraire=numeraire,
            redemption_ratio=shared.redemption_ratio,
            market_ratio=shared.market_ratio,
            redemption_discount_bp=shared.redemption_discount_bp,
            redemption_source=shared.redemption_source,
            price_disagreement_bp=max(
                (value for value in (
                    self._price_disagreements.get(price_key),
                    self._spot_price_disagreements.get((chain_id, underlying_address.lower())),
                ) if value is not None),
                default=None,
            ),
            unresolved=tuple(unresolved),
        )
        self._cache[key] = bundle
        return bundle
