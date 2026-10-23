from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


@dataclass(frozen=True)
class ProtocolResolution:
    slug: str | None
    reason: str


@dataclass(frozen=True)
class ProtocolTvlPoint:
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class StablecoinResolution:
    stablecoin_id: str | None
    issuer_name: str | None
    circulating_usd: float | None
    circulating_prev_month_usd: float | None
    peg_mechanism: str | None
    peg_type: str | None
    reason: str


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _pegged_usd(value: Any) -> float | None:
    amount = value.get("peggedUSD") if isinstance(value, dict) else None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    number = float(amount)
    return number if math.isfinite(number) and number >= 0 else None


def _chain_matches(row: dict[str, Any], chain: str) -> bool:
    chains = row.get("chains")
    return isinstance(chains, list) and any(str(item).casefold() == chain.casefold() for item in chains)


def _addresses_for_chain(payload: Any, chain: str) -> set[str]:
    """Read explicit address fields only, scoped to the requested chain.

    Stablecoin detail payloads have changed shape over time. This deliberately supports both a
    mapping keyed by chain and rows carrying an explicit `chain`/`network` field, but never treats
    an arbitrary address elsewhere in the document as proof for a chain.
    """
    found: set[str] = set()

    def add(candidate: Any) -> bool:
        # `chainConfig.chains.<chain>.issued` holds bare address strings in a list rather than
        # under an `address` key, so keying only on the field name found nothing and every
        # colliding symbol was refused — including Sky's USDS, the largest issuer in the set.
        if isinstance(candidate, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", candidate):
            found.add(candidate.lower())
            return True
        return False

    def collect_addresses(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in {"address", "tokenaddress", "contractaddress"} \
                        and add(child):
                    continue
                collect_addresses(child)
        elif isinstance(value, list):
            for child in value:
                if not add(child):
                    collect_addresses(child)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() == chain.casefold():
                    collect_addresses(child)
            declared_chain = value.get("chain", value.get("network"))
            if isinstance(declared_chain, str) and declared_chain.casefold() == chain.casefold():
                collect_addresses(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return found


# Deployments an issuer owns but does not declare in this registry. The registry lists USD Coin's
# Ethereum contract and nothing else, so its Base and OP Mainnet deployments arrive unclaimed and
# a strict identity check refuses 21 instruments of the least ambiguous asset in the set. Each
# entry was read on-chain 2026-08-20 and carries the symbol and name the contract itself reports,
# so a wrong address fails the check rather than silently widening an issuer.
ISSUER_CONTRACTS: dict[str, dict[str, tuple[str, str]]] = {
    "2": {  # USD Coin
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", "USD Coin"),   # Base
        "0x0b2c639c533813f4aa9d7837caf62653d097ff85": ("USDC", "USD Coin"),   # OP Mainnet
    },
}


def _issuer_claims_contract(payload: Any, chain: str, address: str) -> bool:
    """Does this issuer declare the contract as its own, anywhere it declares contracts?

    Distinct from `_addresses_for_chain`, which is chain-scoped because it discriminates BETWEEN
    competing issuers. This answers identity, and for that the issuer's canonical top-level
    `address` counts too: USD Coin and Tether publish theirs there with `chainConfig: null`, so a
    chain-scoped read finds nothing and refuses the two least ambiguous contracts in DeFi.
    """
    target = address.lower()
    if not target:
        return False
    if target in _addresses_for_chain(payload, chain):
        return True
    canonical = payload.get("address") if isinstance(payload, dict) else None
    if isinstance(canonical, str) and canonical.lower() == target:
        return True
    issuer_id = payload.get("id") if isinstance(payload, dict) else None
    return target in ISSUER_CONTRACTS.get(str(issuer_id), {})


CIRCULATING_TOLERANCE = 0.25


def _summed_chain_balances(detail: Any) -> list[tuple[int, float]]:
    """Sum the issuer's per-chain daily balances onto one date axis, oldest first."""
    balances = detail.get("chainBalances") if isinstance(detail, dict) else None
    if not isinstance(balances, dict):
        return []
    totals: dict[int, float] = {}
    for series in balances.values():
        tokens = series.get("tokens") if isinstance(series, dict) else None
        if not isinstance(tokens, list):
            continue
        for point in tokens:
            if not isinstance(point, dict):
                continue
            amount = _pegged_usd(point.get("circulating"))
            stamp = point.get("date")
            if amount is None or stamp is None:
                continue
            try:
                key = int(stamp)
            except (TypeError, ValueError):
                continue
            totals[key] = totals.get(key, 0.0) + amount
    return sorted(totals.items())


def _cross_checked_circulating(
    summary_current: float, summary_previous: float | None, detail: Any,
) -> tuple[float | None, float | None, str | None]:
    """Reconcile the registry's summary figures against the sum of its own chain balances.

    Measured 2026-08-19: Resolv USD reports 758,722 in the summary while its per-chain balances
    sum to 6,107,233 on the same date — 88% apart. A false low manufactures a falling-TVL credit
    rejection exactly as a false high once handed USD3 a TIER_A on a venue's 8.1bn.

    Inside the tolerance the lower figure is taken, and the 30-day comparison is drawn from the
    same axis as the level so the trend never mixes sources: the tier bands sit ~5x apart, so a
    disagreement this size can only lower a tier, never lift one.
    """
    series = _summed_chain_balances(detail)
    if not series:
        return summary_current, summary_previous, None
    last_stamp, summed = series[-1]
    if summed <= 0:
        return summary_current, summary_previous, None
    gap = abs(summed - summary_current) / max(summed, summary_current)
    if gap > CIRCULATING_TOLERANCE:
        return None, None, (
            f"COULD NOT VERIFY protocol_tvl: DefiLlama contradicts itself — summary circulating "
            f"${summary_current:,.0f} against ${summed:,.0f} summed from its own per-chain "
            f"balances ({gap * 100:.0f}% apart)"
        )
    if summed <= summary_current:
        target = last_stamp - 30 * 86_400
        prior = min(series, key=lambda point: abs(point[0] - target))
        within_reach = abs(prior[0] - target) <= 3 * 86_400 and prior[1] > 0
        return summed, (prior[1] if within_reach else None), None
    return summary_current, summary_previous, None


def resolve_stablecoin_issuer(
    symbol: str,
    address: str,
    chain: str,
    stablecoins: list[dict[str, Any]],
    *,
    detail_loader: Callable[[str], dict[str, Any]] | None = None,
) -> StablecoinResolution:
    """Resolve issuer evidence from the stablecoin registry, never from a trading venue."""
    matches = [row for row in stablecoins
               if str(row.get("symbol") or "").casefold() == symbol.casefold()
               and _chain_matches(row, chain)]
    if not matches:
        return StablecoinResolution(
            None, None, None, None, None, None,
            f"COULD NOT VERIFY protocol_tvl: no DefiLlama stablecoin issuer matched {chain} {symbol}",
        )
    if len(matches) > 1:
        target = address.lower()
        resolved: list[dict[str, Any]] = []
        if target and detail_loader is not None:
            for row in matches:
                stablecoin_id = row.get("id")
                if stablecoin_id is None:
                    continue
                detail = detail_loader(str(stablecoin_id))
                if _issuer_claims_contract(detail, chain, target):
                    resolved.append(row)
        if len(resolved) != 1:
            ids = sorted(str(row.get("id")) for row in matches)
            return StablecoinResolution(
                None, None, None, None, None, None,
                f"COULD NOT VERIFY protocol_tvl: {chain} symbol {symbol!r} is ambiguous across "
                f"stablecoin ids {ids}; contract address did not resolve exactly one issuer",
            )
        matches = resolved

    row = matches[0]
    # A single symbol match is NOT identity. Measured 2026-08-20: two different USD3 tokens live
    # on Ethereum — 0x056b269e… ("USD3", 64,013,967 outstanding, the one Pendle's PT is a claim
    # on) and 0x0d86883f… ("Web 3 Dollar", 1,594,419, the one in this registry). Accepting the
    # lone symbol match attributed Web 3 Dollar's 62% collapse to a token four times larger that
    # was never in trouble. The contract has to agree, whether or not the symbol was ambiguous.
    if detail_loader is not None:
        target = address.lower()
        stablecoin_id = row.get("id")
        confirmed = False
        if target and stablecoin_id is not None:
            try:
                confirmed = _issuer_claims_contract(
                    detail_loader(str(stablecoin_id)), chain, target,
                )
            except SourceError:
                confirmed = False
        if not confirmed:
            # The rejected issuer's name belongs in the reason, never in `issuer_name`: exporting
            # it would leave the decision trace reading "issuer: Web 3 Dollar" for a contract we
            # just refused to attribute to Web 3 Dollar.
            return StablecoinResolution(
                None, None, None, None, None, None,
                f"COULD NOT VERIFY protocol_tvl: {chain} symbol {symbol!r} matches issuer "
                f"{row.get('name')!r} (id {stablecoin_id}) but that issuer does not claim "
                f"contract {address.lower() or 'unknown'}; a shared ticker is not identity",
            )

    current = _pegged_usd(row.get("circulating"))
    previous = _pegged_usd(row.get("circulatingPrevMonth"))
    stablecoin_id = str(row.get("id")) if row.get("id") is not None else None
    if current is not None and stablecoin_id is not None and detail_loader is not None:
        try:
            detail = detail_loader(stablecoin_id)
        except SourceError:
            detail = None
        if detail is not None:
            current, previous, disagreement = _cross_checked_circulating(current, previous, detail)
            if current is None:
                return StablecoinResolution(
                    stablecoin_id,
                    str(row.get("name")) if row.get("name") is not None else None,
                    None, None,
                    str(row.get("pegMechanism")) if row.get("pegMechanism") is not None else None,
                    str(row.get("pegType")) if row.get("pegType") is not None else None,
                    disagreement or "COULD NOT VERIFY protocol_tvl",
                )
    issuer = str(row.get("name")) if row.get("name") is not None else None
    mechanism = str(row.get("pegMechanism")) if row.get("pegMechanism") is not None else None
    peg_type = str(row.get("pegType")) if row.get("pegType") is not None else None
    if current is None or previous is None:
        return StablecoinResolution(
            stablecoin_id, issuer, current, previous, mechanism, peg_type,
            f"COULD NOT VERIFY protocol_tvl_trend: stablecoin id {stablecoin_id} lacks usable "
            "circulating.peggedUSD or circulatingPrevMonth.peggedUSD",
        )
    return StablecoinResolution(
        stablecoin_id, issuer, current, previous, mechanism, peg_type,
        f"exact DefiLlama stablecoin issuer match for {chain} {symbol}: {issuer} (id {stablecoin_id})",
    )


def _protocol_index(protocols: list[dict[str, Any]], fields: tuple[str, ...]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for protocol in protocols:
        slug = protocol.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        for field in fields:
            key = _normalized(protocol.get(field))
            if key and key != "-":
                index.setdefault(key, set()).add(slug)
    return index


DOWNSTREAM_PROJECTS = {
    "beefy",
    "convex-finance",
    "curve-dex",
    "equilibria",
    "pendle",
    "penpie",
    "spectra-v2",
    "stake-dao",
}


def resolve_protocol_slug(
    underlying_symbol: str,
    underlying_address: str,
    chain: str,
    pools: list[dict[str, Any]],
    protocols: list[dict[str, Any]],
) -> ProtocolResolution:
    symbol_key = _normalized(underlying_symbol)
    if not symbol_key:
        return ProtocolResolution(None, "COULD NOT VERIFY protocol slug: underlying symbol is empty")
    direct_index = _protocol_index(protocols, ("name", "symbol"))
    project_index = _protocol_index(protocols, ("slug", "name"))
    direct = direct_index.get(symbol_key, set())
    if len(direct) == 1:
        slug = next(iter(direct))
        return ProtocolResolution(slug, f"exact DefiLlama protocol symbol/name match for {underlying_symbol}")
    if len(direct) > 1:
        return ProtocolResolution(
            None,
            f"COULD NOT VERIFY protocol slug: exact symbol {underlying_symbol!r} is ambiguous across {sorted(direct)}",
        )

    address = underlying_address.lower()
    candidates: set[str] = set()
    for pool in pools:
        if _normalized(pool.get("symbol")) != symbol_key or str(pool.get("chain")) != chain:
            continue
        tokens = pool.get("underlyingTokens")
        token_addresses = {
            str(token).lower() for token in tokens if isinstance(token, str)
        } if isinstance(tokens, list) else set()
        if address and address in token_addresses:
            continue
        project_key = _normalized(pool.get("project"))
        if str(pool.get("project")) in DOWNSTREAM_PROJECTS:
            continue
        matches = project_index.get(project_key, set())
        if len(matches) == 1:
            candidates.update(matches)
    if len(candidates) == 1:
        slug = next(iter(candidates))
        return ProtocolResolution(
            slug,
            f"exact {chain} underlying symbol {underlying_symbol} matched native DefiLlama project {slug}",
        )
    if candidates:
        return ProtocolResolution(
            None,
            f"COULD NOT VERIFY protocol slug: exact symbol {underlying_symbol!r} maps to multiple native projects {sorted(candidates)}",
        )
    return ProtocolResolution(
        None,
        f"COULD NOT VERIFY protocol slug: no exact native DefiLlama project matched {chain} {underlying_symbol}",
    )


def parse_protocol_tvl(payload: Any, endpoint: str) -> tuple[ProtocolTvlPoint, ...]:
    mapping = require_mapping(payload, endpoint)
    rows = require_list(require_key(mapping, "tvl", endpoint), endpoint)
    points: list[ProtocolTvlPoint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp, value = row.get("date"), row.get("totalLiquidityUSD")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(float(value)) or float(value) < 0:
            continue
        points.append(ProtocolTvlPoint(datetime.fromtimestamp(float(stamp), UTC), float(value)))
    unique = {point.timestamp: point for point in points}
    ordered = tuple(unique[stamp] for stamp in sorted(unique))
    if len(ordered) < 2:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY protocol TVL trend; fewer than two usable daily points")
    return ordered


# A series sampled once a day is not worth re-downloading within the same day. Long enough that
# the two nightly collectors share one pull, short enough that tomorrow's point still arrives.
DAILY_SERIES_CACHE_MINUTES = 20 * 60


class DefiLlamaClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def yields_pools(self) -> list[dict[str, Any]]:
        url = "https://yields.llama.fi/pools"
        payload = require_mapping(self.http.request_json("defillama", "yields_pools", "GET", url), url)
        return [require_mapping(item, url) for item in require_list(require_key(payload, "data", url), url)]

    def yield_chart(self, pool: str) -> list[dict[str, Any]]:
        url = f"https://yields.llama.fi/chart/{pool}"

        def has_history(payload: Any) -> bool:
            data = payload.get("data") if isinstance(payload, dict) else None
            return isinstance(data, list) and bool(data)

        payload = require_mapping(self.http.request_json(
            "defillama", f"yield_chart_{pool}", "GET", url, validate=has_history,
            prefer_cache_minutes=DAILY_SERIES_CACHE_MINUTES, prefer_cached_errors=False,
        ), url)
        return [require_mapping(item, url) for item in require_list(require_key(payload, "data", url), url)]

    def coin_chart(
        self,
        coin: str,
        start: int,
        span: int,
        period: str = "1d",
        search_width: str = "6h",
        *,
        cache_name: str | None = None,
        minimum_points: int | None = None,
        on_observation: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        url = f"https://coins.llama.fi/chart/{coin}"
        name = cache_name or f"coin_chart_{coin}_{start}_{span}_{period}"

        # A truncated series is as useless as an empty one and silently corrupts any window
        # computed from it, so require the response to cover most of the span requested.
        min_points = minimum_points if minimum_points is not None else (
            max(2, int(span * 0.5)) if span > 1 else 1
        )

        def has_prices(payload: Any) -> bool:
            coins = payload.get("coins") if isinstance(payload, dict) else None
            entry = coins.get(coin) if isinstance(coins, dict) else None
            prices = entry.get("prices") if isinstance(entry, dict) else None
            if not isinstance(prices, list):
                return False
            if on_observation is not None:
                on_observation(len(prices))
            return len(prices) >= min_points

        # A daily series gains one point a day, and this one is 90 of them. Fetching it fresh on
        # every run cost 3,286 downloads of 94 assets' history -- 35 copies each -- and the
        # refusals that came back were written into the cache as the missing `peg_history` the
        # credit gate then reported. Two collectors run an hour apart nightly; the second reuses
        # what the first pulled, and the next night refreshes it.
        return require_mapping(self.http.request_json("defillama", name, "GET", url,
            params={"start": start, "span": span, "period": period, "searchWidth": search_width},
            validate=has_prices, prefer_cache_minutes=DAILY_SERIES_CACHE_MINUTES,
            prefer_cached_errors=False), url)

    def coin_chart_sweep(
        self,
        coin: str,
        start: int,
        span: int,
        period: str = "1d",
        search_widths: tuple[str, ...] = ("12h", "1d", "3d"),
        *,
        cache_name: str | None = None,
        minimum_points: int,
    ) -> dict[str, Any]:
        observed: list[int] = []
        for search_width in search_widths:
            try:
                return self.coin_chart(
                    coin, start, span, period, search_width,
                    cache_name=cache_name,
                    minimum_points=minimum_points,
                    on_observation=observed.append,
                )
            except SourceError:
                continue
        # What the source did return is the finding, not the stack of failed attempts: a token
        # with 24 observations over 23 days has not been watched through stress, and saying so
        # is the product. Widening the search never invents history that is not there.
        best = max(observed, default=None)
        if best is None:
            raise SourceError(
                f"COULD NOT VERIFY peg_history: the price source returned no usable series at "
                f"searchWidth {'/'.join(search_widths)}"
            )
        raise SourceError(
            f"COULD NOT VERIFY peg_history: {best} daily observations available across "
            f"searchWidth {'/'.join(search_widths)}, short of the {minimum_points} the 90-day "
            f"rubric window requires"
        )

    def stablecoins(self) -> list[dict[str, Any]]:
        url = "https://stablecoins.llama.fi/stablecoins?includePrices=true"

        def has_assets(payload: Any) -> bool:
            assets = payload.get("peggedAssets") if isinstance(payload, dict) else None
            return isinstance(assets, list) and bool(assets)

        payload = require_mapping(self.http.request_json(
            "defillama", "stablecoins", "GET", url, validate=has_assets,
            prefer_cache_minutes=30,
        ), url)
        return [require_mapping(item, url)
                for item in require_list(require_key(payload, "peggedAssets", url), url)]

    def stablecoin_detail(self, stablecoin_id: str) -> dict[str, Any]:
        url = f"https://stablecoins.llama.fi/stablecoin/{stablecoin_id}"
        return require_mapping(self.http.request_json(
            "defillama", f"stablecoin_{stablecoin_id}", "GET", url,
            validate=lambda payload: isinstance(payload, dict) and bool(payload),
            prefer_cache_minutes=30,
        ), url)

    def current_price(self, coin: str) -> tuple[float, str]:
        """Spot price for one `chain:address` key, with the confidence the source reports."""
        url = f"https://coins.llama.fi/prices/current/{coin}"

        def has_price(payload: Any) -> bool:
            coins = payload.get("coins") if isinstance(payload, dict) else None
            entry = coins.get(coin) if isinstance(coins, dict) else None
            price = entry.get("price") if isinstance(entry, dict) else None
            return isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0

        payload = require_mapping(self.http.request_json(
            "defillama", f"price_current_{coin}", "GET", url,
            validate=has_price, prefer_cache_minutes=30,
        ), url)
        entry = payload["coins"][coin]
        return float(entry["price"]), f"GET {url}"

    def protocols(self) -> list[dict[str, Any]]:
        url = "https://api.llama.fi/protocols"
        payload = self.http.request_json("defillama", "protocols", "GET", url)
        return [require_mapping(item, url) for item in require_list(payload, url)]

    def protocol(self, slug: str) -> dict[str, Any]:
        url = f"https://api.llama.fi/protocol/{slug}"
        def has_tvl_history(payload: Any) -> bool:
            try:
                parse_protocol_tvl(payload, url)
            except SourceError:
                return False
            return True

        return require_mapping(self.http.request_json(
            "defillama", f"protocol_{slug}", "GET", url, validate=has_tvl_history,
        ), url)

    def protocol_tvl_history(self, slug: str) -> tuple[ProtocolTvlPoint, ...]:
        url = f"https://api.llama.fi/protocol/{slug}"
        return parse_protocol_tvl(self.protocol(slug), url)
