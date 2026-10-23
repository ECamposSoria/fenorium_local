from __future__ import annotations

import argparse
import json
import logging
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from engine.registry import registry
from research.par_sources.defillama import DefiLlamaClient
from research.par_sources.http import CacheMissError, HttpClient, SourceError, cache_fetched_at
from research.par_sources.lifi import EARN_BASE, LifiEarnClient, load_api_key
from research.par_sources.morpho import MorphoClient
from research.par_sources.morpho_midnight import MorphoMidnightClient, SUPPORTED_CHAINS
from research.par_sources.normalize import strip_chain_prefix
from research.par_sources.pendle import PendleClient
from research.par_sources.spectra import NETWORK_CHAINS, SpectraClient


_LOG = logging.getLogger("par.ingest")
from research.par_sources.tenor import TenorClient
from research.par_sources.termfinance import TermFinanceClient
from research.par_sources.termmax import TermMaxClient
from research.par_sources.vault import endpoints_for, request_rpc_json
from research.par_sources.venue_oracle import EXACTLY_MARKETS, _decode_words

from .chain import selector
from .models import MarketRecord, SlippagePoint


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DATA = ROOT / "research" / "data"
ENGINE_DATA = Path(__file__).resolve().parent / "data"
# The chains the collector discovers instruments on, by the label it writes into each record.
CHAINS = {chain.chain_id: chain.label for chain in registry().with_role("collect")}
# Of those, the chains Pendle lists markets on: asking it about any other only records an error.
PENDLE_CHAINS = {chain_id: label for chain_id, label in CHAINS.items() if registry().require(chain_id).pendle}


class EngineHttpClient(HttpClient):
    """Writes engine-owned cache and reads the authoritative research cache as fallback."""

    def __init__(self, *, offline: bool) -> None:
        super().__init__(ENGINE_DATA, offline=offline)
        self._fallback = HttpClient(RESEARCH_DATA, offline=True)

    def _read_cache(self, source: str, name: str, endpoint: str, reason: str | None = None) -> Any:
        try:
            return super()._read_cache(source, name, endpoint, reason)
        except CacheMissError:
            payload = self._fallback._read_cache(source, name, endpoint, reason)
            self.records.append(self._fallback.records[-1])
            return payload


def _finite(value: Any, endpoint: str, key: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SourceError(f"{endpoint}: `{key}` must be numeric")
    try:
        result = float(value)
    except ValueError as exc:
        raise SourceError(f"{endpoint}: `{key}` must be numeric") from exc
    if not math.isfinite(result):
        raise SourceError(f"{endpoint}: `{key}` must be finite")
    return result


def _source_time(http: HttpClient, endpoint: str) -> datetime:
    matching = [
        record for record in http.records
        if record.endpoint == endpoint or endpoint in record.endpoint or record.endpoint in endpoint
    ]
    if not matching:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY consumed cache fetch timestamp")
    value = matching[-1].fetched_at
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pendle_quote_curves(cache_dirs: Iterable[Path]) -> dict[tuple[int, str], tuple[SlippagePoint, ...]]:
    pattern = re.compile(r"pendle_convert_(\d+)_(0x[a-f0-9]+)_(\d+)usd\.json$")
    curves: dict[tuple[int, str], dict[float, SlippagePoint]] = {}
    for cache_dir in cache_dirs:
        for path in cache_dir.glob("pendle_convert_*usd.json"):
            match = pattern.match(path.name)
            if not match:
                continue
            endpoint = "POST https://api-v2.pendle.finance/core/v3/sdk/{chainId}/convert"
            payload = json.loads(path.read_text(encoding="utf-8"))
            try:
                impact = abs(float(payload["routes"][0]["data"]["priceImpact"]))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise SourceError(f"{endpoint}: {path.name} lost `routes[0].data.priceImpact`") from exc
            size = float(match.group(3))
            key = (int(match.group(1)), match.group(2))
            curves.setdefault(key, {})[size] = SlippagePoint(
                size, impact, cache_fetched_at(path), str(path)
            )
    return {key: tuple(points[size] for size in sorted(points)) for key, points in curves.items()}


def _pendle_accounting_asset(http: HttpClient, chain_id: int, raw: dict[str, Any]) -> tuple[str | None, int | None]:
    def valid_asset_info(payload: Any, endpoint: str) -> bool:
        try:
            words = _decode_words(payload, endpoint, minimum_words=3)
        except SourceError:
            return False
        return (len(words) == 3 and 0 <= words[0] <= 255 and 0 <= words[1] < 2**160
                and 0 <= words[2] <= 255)

    try:
        sy = strip_chain_prefix(raw.get("sy"), chain_id, "Pendle market", "sy").lower()
        if re.fullmatch(r"0x[0-9a-f]{40}", sy) is None:
            return None, None
        payload, endpoint = request_rpc_json(
            http, chain_id, endpoints_for(chain_id),
            cache_name=f"pendle_sy_asset_info_{chain_id}_{sy}", method="eth_call",
            params=[{"to": sy, "data": selector("assetInfo()")}, "latest"],
            operation=f"eth_call {sy} assetInfo()", validate=valid_asset_info,
            prefer_cache_minutes=60,
        )
        _, address, decimals = _decode_words(payload, endpoint, minimum_words=3)
        return f"0x{address:040x}", decimals
    except SourceError:
        return None, None


def _erc20_symbol(http: HttpClient, chain_id: int, token: str) -> str | None:
    """A token's own symbol() for display names, or None; cached for a week since symbols do not change."""
    token = token.lower()
    if re.fullmatch(r"0x[0-9a-f]{40}", token) is None:
        return None

    def decode(payload: Any) -> str | None:
        result = payload.get("result") if isinstance(payload, dict) else None
        raw = bytes.fromhex(result[2:]) if isinstance(result, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", result) else b""
        if len(raw) >= 96 and int.from_bytes(raw[:32], "big") == 32:
            length = int.from_bytes(raw[32:64], "big")
            text = raw[64:64 + length] if length <= len(raw) - 64 else b""
        elif len(raw) == 32:  # bytes32 symbols (MKR-style tokens)
            text = raw.rstrip(b"\0")
        else:
            return None
        try:
            value = text.decode("ascii")
        except UnicodeDecodeError:
            return None
        return value if re.fullmatch(r"[\x21-\x7e]{1,16}", value) else None

    try:
        payload, _endpoint = request_rpc_json(
            http, chain_id, endpoints_for(chain_id),
            cache_name=f"token_symbol_{chain_id}_{token}", method="eth_call",
            params=[{"to": token, "data": selector("symbol()")}, "latest"],
            operation=f"eth_call {token} symbol()",
            validate=lambda payload, _endpoint: decode(payload) is not None,
            prefer_cache_minutes=7 * 24 * 60,
        )
        return decode(payload)
    except SourceError:
        return None


def _spectra_accounting_asset(http: HttpClient, chain_id: int, pt: str) -> tuple[str | None, int | None]:
    def valid_word(payload: Any, endpoint: str, *, maximum: int) -> bool:
        try:
            words = _decode_words(payload, endpoint)
        except SourceError:
            return False
        return len(words) == 1 and 0 <= words[0] <= maximum

    try:
        pt = pt.lower()
        if re.fullmatch(r"0x[0-9a-f]{40}", pt) is None:
            return None, None
        payload, endpoint = request_rpc_json(
            http, chain_id, endpoints_for(chain_id),
            cache_name=f"spectra_pt_underlying_{chain_id}_{pt}", method="eth_call",
            params=[{"to": pt, "data": selector("underlying()")}, "latest"],
            operation=f"eth_call {pt} underlying()",
            validate=lambda payload, endpoint: valid_word(payload, endpoint, maximum=2**160 - 1),
            prefer_cache_minutes=60,
        )
        address = f"0x{_decode_words(payload, endpoint)[0]:040x}"
        payload, endpoint = request_rpc_json(
            http, chain_id, endpoints_for(chain_id),
            cache_name=f"spectra_asset_decimals_{chain_id}_{address}", method="eth_call",
            params=[{"to": address, "data": selector("decimals()")}, "latest"],
            operation=f"eth_call {address} decimals()",
            validate=lambda payload, endpoint: valid_word(payload, endpoint, maximum=255),
            prefer_cache_minutes=60,
        )
        return address, _decode_words(payload, endpoint)[0]
    except SourceError:
        return None, None


def _pendle_records(http: HttpClient, errors: list[str] | None = None) -> list[MarketRecord]:
    client = PendleClient(http)
    curves = _pendle_quote_curves((RESEARCH_DATA / "latest", ENGINE_DATA / "latest"))
    result: list[MarketRecord] = []
    for chain_id, chain in PENDLE_CHAINS.items():
        # A chain that will not answer is one chain's worth of missing paper, not a venue outage.
        # Letting it propagate dropped all 68 Pendle markets the first time HyperEVM was added
        # with no cache behind it.
        try:
            raws = client.markets_active(chain_id)
        except SourceError as exc:
            if errors is not None:
                errors.append(f"Pendle {chain}: {exc}")
            continue
        for raw in raws:
            item = client.normalize_market(chain_id, chain, raw)
            curve = curves.get((chain_id, item.pt_address.lower()), ())
            accounting_address, accounting_decimals = _pendle_accounting_asset(http, chain_id, raw)
            result.append(MarketRecord(
                venue=item.venue, chain=item.chain, instrument=item.symbol,
                underlying=item.underlying_symbol or item.underlying_address,
                maturity=item.maturity, implied_apy=item.implied_apy,
                liquidity_usd=item.liquidity_usd,
                executable_capacity_usd=max((point.size_usd for point in curve), default=None),
                slippage_curve=curve, source_timestamp=_source_time(http, item.source_endpoint),
                source_endpoint=item.source_endpoint,
                metadata={"market_address": item.address, "pt_address": item.pt_address,
                          "underlying_address": item.underlying_address, "chain_id": chain_id,
                          "accounting_asset_address": accounting_address,
                          "accounting_asset_decimals": accounting_decimals,
                          "category_ids": list(raw.get("categoryIds") or [])},
            ))
    return result


def _spectra_records(http: HttpClient) -> list[MarketRecord]:
    client = SpectraClient(http)
    result: list[MarketRecord] = []
    for network in NETWORK_CHAINS:
        for item in client.normalize_pools(network, client.pools(network)):
            accounting_address, accounting_decimals = _spectra_accounting_asset(
                http, NETWORK_CHAINS[network][0], item.pt_address,
            )
            result.append(MarketRecord(
                venue=item.venue, chain=item.chain, instrument=item.symbol,
                underlying=item.underlying_symbol or item.underlying_address,
                maturity=item.maturity, implied_apy=item.implied_apy,
                liquidity_usd=item.liquidity_usd, executable_capacity_usd=None,
                slippage_curve=(), source_timestamp=_source_time(http, item.source_endpoint),
                source_endpoint=item.source_endpoint,
                metadata={"pool_address": item.address, "pt_address": item.pt_address,
                          "underlying_address": item.underlying_address, "network": network,
                          "accounting_asset_address": accounting_address,
                          "accounting_asset_decimals": accounting_decimals,
                          "chain_id": NETWORK_CHAINS[network][0]},
            ))
    return result


def _termmax_records(http: HttpClient) -> list[MarketRecord]:
    client = TermMaxClient(http)
    configs = client.market_config_list(1)
    # The same cached payload carries token metadata, so the underlying can be named rather than
    # left as a bare address — every other venue puts a symbol in `underlying`, and the credit
    # loader keys evidence on (chain_id, address).
    assets = {
        str(row.get("contractAddress", "")).lower(): str(row.get("symbol") or "")
        for row in client.asset_configs(1)
    }
    oracle_accessors = {
        str(row.get("contractAddress", "")).lower(): str(row.get("oracleAddress") or "").lower()
        for row in client.gt_configs(1)
    }
    collections = {str(row.get("marketAddress", "")).lower(): row for row in client.market_list_with_collection(1)}
    endpoint = f"GET {client.BASE}/v2/market/list-with-collection"
    timestamp = _source_time(http, endpoint)
    empty_books: set[str] = set()
    for cache_dir in (RESEARCH_DATA / "latest", ENGINE_DATA / "latest"):
        for path in cache_dir.glob("termmax_market_info_orders_1_*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("data") == []:
                empty_books.add(path.stem.removeprefix("termmax_market_info_orders_1_").lower())
    result: list[MarketRecord] = []
    for config in configs:
        contracts = config.get("contracts")
        if not isinstance(contracts, dict):
            raise SourceError(f"{endpoint}: market config lost `contracts`")
        address = str(contracts.get("marketAddr", "")).lower()
        symbol = config.get("symbol")
        maturity = config.get("maturity")
        if not isinstance(symbol, str) or not isinstance(maturity, str) or not address:
            raise SourceError(f"{endpoint}: market config lost symbol, maturity, or marketAddr")
        collection = collections.get(address, {})
        stats = collection.get("sortedOrderStats", [])
        if not isinstance(stats, list):
            raise SourceError(f"{endpoint}: `sortedOrderStats` must be a list")
        usable = [row for row in stats if isinstance(row, dict)]
        rated = [(rate, row) for row in usable
                 if (rate := _finite(row.get("lendApy"), endpoint, "lendApy", allow_none=True)) is not None]
        best = max(rated, key=lambda item: (item[0], str(item[1].get("orderAddress") or "")), default=None)
        lend_rate = None if best is None else best[0]
        # Rate and executable capacity are one quote pair. Summing other orders while retaining
        # only the best rate fabricates capacity at that rate (7.8237% + a lower 7.69% order was
        # the concrete failure). A later curve-aware allocator may consume multiple tranches.
        capacity = None if best is None else _finite(
            best[1].get("lendCapacityUsdValue"), endpoint, "lendCapacityUsdValue", allow_none=True,
        )
        underlying_address = str(contracts.get("underlyingAddr") or "").lower() or None
        result.append(MarketRecord(
            venue="TermMax", chain="Ethereum", instrument=symbol,
            underlying=assets.get(underlying_address or "") or underlying_address,
            maturity=datetime.fromisoformat(maturity.replace("Z", "+00:00")),
            implied_apy=lend_rate, liquidity_usd=None,
            executable_capacity_usd=capacity, slippage_curve=(),
            source_timestamp=timestamp, source_endpoint=endpoint,
            metadata={"market_address": address, "collateral_address": contracts.get("collateralAddr"),
                      "gt_address": contracts.get("gtAddr"),
                      "underlying_address": underlying_address, "chain_id": 1,
                      "oracle_accessor_address": oracle_accessors.get(
                          str(contracts.get("gtAddr") or "").lower()
                      ),
                      "version": config.get("version"), "book_has_stats": bool(usable),
                      "literal_empty_book": address in empty_books,
                      "order_address": None if best is None else best[1].get("orderAddress")},
        ))
    return result


def _tenor_offer_quote(offer: dict, now: float, endpoint: str) -> tuple[float, float, float | None] | None:
    if (offer.get("invalidated") is not False or offer.get("reduceOnly") is not False
            or str(offer.get("enterGate") or "").lower() != "0x" + "0" * 40):
        return None
    start = _finite(offer.get("start"), endpoint, "start")
    expiry = _finite(offer.get("expiry"), endpoint, "expiry")
    assets = _finite(offer.get("availableAssets"), endpoint, "availableAssets")
    if not start <= now < expiry or assets <= 0:
        return None
    maturity = _finite(offer.get("maturity"), endpoint, "maturity")
    price = _finite(offer.get("price"), endpoint, "price")
    if maturity <= now or price <= 0:
        return None
    apr = (1 / (price / 1e18) - 1) * 365 * 86400 / (maturity - now)
    loan = offer.get("loanToken") or {}
    capacity = None
    if loan.get("symbol") in {"USDC", "USDT"}:
        decimals = _finite(loan.get("decimals"), endpoint, "decimals")
        if not decimals.is_integer() or not 0 <= decimals <= 255:
            raise SourceError(f"{endpoint}: invalid loan token decimals")
        capacity = assets / 10 ** int(decimals)
    return price, apr, capacity


def _tenor_records(http: HttpClient) -> list[MarketRecord]:
    from engine.midnight import ADDRESSES as MIDNIGHT_ADDRESSES

    client = TenorClient(http)
    endpoint = f"POST {client.URL}"
    items = client.morpho_fixed_markets()
    timestamp = _source_time(http, endpoint)
    now = datetime.now(UTC).timestamp()
    offers_by_chain: dict[int, dict[str, list[tuple[float, float, float | None]]]] = {}
    book_status_by_chain: dict[int, str] = {}
    book_timestamp_by_chain: dict[int, datetime] = {}
    result: list[MarketRecord] = []
    for item in items:
        state = item.get("state")
        market = item.get("tenorMarket")
        if not isinstance(state, dict) or not isinstance(market, dict):
            raise SourceError(f"{endpoint}: market lost `state` or `tenorMarket`")
        loan = market.get("loanToken")
        if not isinstance(loan, dict):
            raise SourceError(f"{endpoint}: market lost `tenorMarket.loanToken`")
        maturity = _finite(item.get("maturity"), endpoint, "maturity")
        chain_id = int(_finite(market.get("chainId"), endpoint, "chainId"))
        if chain_id not in offers_by_chain:
            grouped: dict[str, list[tuple[float, float, float | None]]] = {}
            # The book is read beside the markets, not instead of them: a chain whose offers
            # cannot be read tonight keeps its markets at the indicative curve rate, and the
            # reason travels with each record rather than deleting the venue from the universe.
            records_before = len(http.records)
            try:
                offers = client.sell_offers(chain_id)
            except SourceError as exc:
                offers, book_status = None, f"unavailable: {exc}"
            else:
                book_status = "read"
                matching = [
                    record for record in http.records[records_before:]
                    if record.source == "tenor" and (record.name == f"sell_offers_{chain_id}"
                        or record.name.startswith(f"sell_offers_{chain_id}_p"))
                ]
                if not matching:
                    raise SourceError(f"{endpoint}: COULD NOT VERIFY consumed cache fetch timestamp")
                book_timestamp_by_chain[chain_id] = min(
                    datetime.fromisoformat(record.fetched_at.replace("Z", "+00:00"))
                    for record in matching)
            for offer in offers or ():
                identifier = str(offer.get("fixedMarketIdentifier") or "").lower()
                quote = _tenor_offer_quote(offer, now, endpoint)
                if identifier and quote is not None:
                    grouped.setdefault(identifier, []).append(quote)
            offers_by_chain[chain_id] = grouped
            book_status_by_chain[chain_id] = book_status
        identifier = item.get("fixedMarketIdentifier")
        quotes = sorted(offers_by_chain[chain_id].get(str(identifier or "").lower(), []))
        # The rate is the best ask's; the depth is what that price level holds. Summing every
        # level behind the best price would pair a size with a rate the book does not offer at it.
        best = quotes[0][0] if quotes else None
        at_best = [quote for quote in quotes if quote[0] == best]
        apr = at_best[0][1] if at_best else None
        capacity = (sum(quote[2] for quote in at_best)
                    if at_best and all(quote[2] is not None for quote in at_best) else None)
        collateral_rows = market.get("collaterals")
        oracle_feeds = tuple(sorted({
            str(row.get("oracle")).lower()
            for row in collateral_rows if isinstance(row, dict) and row.get("oracle")
        })) if isinstance(collateral_rows, list) else ()
        loan_decimals = loan.get("decimals")
        if loan_decimals is not None and (type(loan_decimals) is not int or not 0 <= loan_decimals <= 255):
            raise SourceError(f"{endpoint}: invalid loan token decimals")
        midnight = MIDNIGHT_ADDRESSES.get(chain_id)
        result.append(MarketRecord(
            venue="Tenor", chain=CHAINS.get(chain_id, str(chain_id)), instrument=str(item.get("id")),
            underlying=str(loan.get("symbol")) or None,
            maturity=datetime.fromtimestamp(maturity, UTC),
            implied_apy=apr,
            liquidity_usd=capacity,
            executable_capacity_usd=capacity,
            slippage_curve=(),
            source_timestamp=min(timestamp, book_timestamp_by_chain.get(chain_id, timestamp)),
            source_endpoint=endpoint,
            metadata={"loan_address": loan.get("address"), "has_pt_collateral": False,
                      "underlying_address": str(loan.get("address") or "").lower() or None,
                      "midnight_address": None if midnight is None else midnight.midnight,
                      "loan_decimals": loan_decimals,
                      "chain_id": chain_id, "oracle_feed_addresses": oracle_feeds,
                      "fixed_market_identifier": identifier,
                      "book_status": book_status_by_chain[chain_id],
                      "book_asks": [{"price": quote[0], "apr": quote[1], "capacity_usd": quote[2]} for quote in quotes],
                      "lender_ask_rate": _finite(state.get("supplyRateApr"), endpoint, "supplyRateApr", allow_none=True)},
        ))
    return result


def _midnight_records(http: HttpClient, errors: list[str] | None = None,
                      as_of: datetime | None = None) -> list[MarketRecord]:
    from engine.midnight import ADDRESSES

    client = MorphoMidnightClient(http)
    # Maturity and annualisation are judged at the caller's evaluation time, so a replay of the
    # same cached book at the same as-of gives the same rate whatever day it runs.
    now = (as_of or datetime.now(UTC)).timestamp()
    result: list[MarketRecord] = []

    def note(message: str) -> None:
        _LOG.warning(message)
        if errors is not None:
            errors.append(message)

    def fetch(method, chain_id):
        before = len(http.records)
        items = method(chain_id)
        pulls = [pull for pull in http.records[before:] if pull.source == "morpho_midnight"]
        if not pulls:
            raise SourceError("COULD NOT VERIFY consumed cache fetch timestamp")
        timestamp = min(datetime.fromisoformat(pull.fetched_at.replace("Z", "+00:00"))
                        for pull in pulls)
        return items, timestamp

    for chain_id in sorted(ADDRESSES.keys() & SUPPORTED_CHAINS):
        endpoint = f"GET {client.BASE}markets?chain_ids={chain_id}&limit=50"
        try:
            items, timestamp = fetch(client.markets, chain_id)
        except SourceError as exc:
            note(f"Morpho Midnight {chain_id} markets: {exc}")
            continue
        try:
            book_rows, book_timestamp = fetch(client.books, chain_id)
            books = {row["market_id"].lower(): row for row in book_rows
                     if row.get("chain_id") == chain_id}
            timestamp = min(timestamp, book_timestamp)
            book_status = "read"
        except SourceError as exc:
            books, book_status = {}, f"unavailable: {exc}"
            note(f"Morpho Midnight {chain_id} books: {exc}")
        chain = registry().require(chain_id)
        tokens = {token.address.lower(): token for token in chain.settlement}
        if chain.usdc is not None:
            tokens[chain.usdc.address.lower()] = chain.usdc
        skipped = 0
        for item in items:
            if item.get("chain_id") != chain_id or item.get("listed") is not True:
                continue
            maturity = _finite(item.get("maturity"), endpoint, "maturity")
            if maturity <= now or not any(
                _finite(item.get(key) or "0", endpoint, key) > 0
                for key in ("total_units", "lend_depth", "borrow_depth")
            ):
                continue
            loan_address = str(item.get("loan_token") or "").lower()
            loan = tokens.get(loan_address)
            if loan is None:
                skipped += 1
                continue
            identifier = item["market_id"]
            book = books.get(identifier.lower(), {})
            asks, bids = book.get("asks", []), book.get("bids", [])
            # A lender buys credit at the lowest ask; the highest bid is indicative only.
            level = (min(asks, key=lambda row: int(row["price"])) if asks else
                     max(bids, key=lambda row: int(row["price"])) if bids else None)
            rate_kind = "executable" if asks else "indicative" if bids else None
            apr, capacity = None, None
            usd = loan.symbol in {"USDC", "USDT"}
            if level is not None:
                price = _finite(level.get("price"), endpoint, "price") / 1e18
                if not 0 < price <= 1:
                    raise SourceError(f"{endpoint}: invalid book price")
                apr = (1 / price - 1) * 365 * 86400 / (maturity - now)
                if asks and usd:
                    capacity = _finite(level.get("assets"), endpoint, "assets") / 10**loan.decimals
            collaterals = [{key: row[key] for key in ("token", "lltv", "oracle")}
                           for row in item["collaterals"]]
            symbols = [tokens[row["token"].lower()].symbol if row["token"].lower() in tokens
                       else _erc20_symbol(http, chain_id, row["token"]) or row["token"][:6] + "…" + row["token"][-4:]
                       for row in collaterals]
            collaterals = [dict(row, symbol=symbol) for row, symbol in zip(collaterals, symbols)]
            maturity_date = datetime.fromtimestamp(maturity, UTC)
            metadata = {key: item.get(key) for key in (
                "market_id", "listed", "total_units", "lend_depth", "borrow_depth",
                "current_settlement_fee_cbp", "continuous_fee_rate", "market_family_id")}
            metadata.update({
                "chain_id": chain_id, "midnight_address": ADDRESSES[chain_id].midnight,
                "loan_token": {"address": loan_address, "decimals": loan.decimals, "symbol": loan.symbol},
                "underlying_address": loan_address, "loan_address": loan_address,
                "loan_decimals": loan.decimals, "collaterals": collaterals,
                "rate_kind": rate_kind, "book_status": book_status, "has_pt_collateral": False,
                "oracle_feed_addresses": tuple(sorted({row["oracle"].lower() for row in collaterals})),
            })
            result.append(MarketRecord(
                venue="Morpho Midnight", chain=chain.label,
                instrument=f"{loan.symbol} {maturity_date:%Y-%m-%d} · {', '.join(symbols)} · {identifier.removeprefix('0x')[:8]}",
                underlying=loan.symbol, maturity=maturity_date, implied_apy=apr,
                liquidity_usd=(_finite(item.get("lend_depth"), endpoint, "lend_depth", allow_none=True)
                               / 10**loan.decimals if usd and item.get("lend_depth") is not None else None),
                executable_capacity_usd=capacity, slippage_curve=(), source_timestamp=timestamp,
                source_endpoint=endpoint, metadata=metadata,
            ))
        if skipped:
            note(f"Morpho Midnight {chain_id}: skipped {skipped} market(s) with unregistered loan token")
    return result


CHAIN_NAMESPACES = {chain_id: registry().require(chain_id).defillama for chain_id in CHAINS}


EXACTLY_MATURITY = re.compile(r"Fixed mat:\s*(\d{4}-\d{2}-\d{2})")

# Chains as DefiLlama names them, mapped to the ids the credit loader keys evidence on.
LLAMA_CHAIN_IDS = {
    name: chain.chain_id for chain in registry().with_role("collect") for name in chain.llama_yields
}


def _exactly_records(http: HttpClient) -> list[MarketRecord]:
    """Exactly's fixed-maturity pools, read from the yields index we already cache.

    Exactly publishes the maturity inside `poolMeta` as `Fixed mat: YYYY-MM-DD`, which is the
    same marker used to find it: scanning all 15,622 cached pools for a maturity marker turns up
    only Exactly, plus penpie and equilibria wrappers of a Pendle market we already carry.

    A zero APY here means no fixed rate is quoted for that maturity, not a zero-rate market, so
    it arrives as no rate at all rather than as a measured 0%.
    """
    client = DefiLlamaClient(http)
    endpoint = "GET https://yields.llama.fi/pools"
    pools = client.yields_pools()
    timestamp = _source_time(http, endpoint)
    result: list[MarketRecord] = []
    for pool in pools:
        if str(pool.get("project")) != "exactly":
            continue
        match = EXACTLY_MATURITY.search(str(pool.get("poolMeta") or ""))
        if not match:
            continue
        chain_id = LLAMA_CHAIN_IDS.get(str(pool.get("chain")))
        if chain_id is None:
            continue
        maturity = datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
        tokens = pool.get("underlyingTokens")
        address = None
        if isinstance(tokens, list) and tokens and isinstance(tokens[0], str):
            address = tokens[0].lower()
        rate = _finite(pool.get("apyBase"), endpoint, "apyBase", allow_none=True)
        symbol = str(pool.get("symbol") or "")
        result.append(MarketRecord(
            venue="Exactly", chain=CHAINS.get(chain_id, str(pool.get("chain"))),
            instrument=f"{symbol}@{maturity.date().isoformat()}",
            underlying=symbol or address,
            maturity=maturity,
            implied_apy=None if not rate else rate / 100.0,
            liquidity_usd=_finite(pool.get("tvlUsd"), endpoint, "tvlUsd", allow_none=True),
            executable_capacity_usd=None,
            slippage_curve=(), source_timestamp=timestamp, source_endpoint=endpoint,
            metadata={"pool_id": pool.get("pool"), "chain_id": chain_id,
                      "underlying_address": address,
                      "oracle_market_address": EXACTLY_MARKETS.get((chain_id, address or "")),
                      "pool_meta": pool.get("poolMeta"),
                      "literal_empty_book": not rate},
        ))
    return result


def _termfinance_records(http: HttpClient) -> list[MarketRecord]:
    """Term Finance repos, rated at what a lender can execute rather than at what lenders ask.

    Its book is not crossed: a `lend` order is another lender's ask. Measured 2026-08-19, only 6
    of 53 live repos carry a resting borrow bid at all, and the largest is $5,000 — so most repos
    correctly arrive with no rate, exactly as TermMax's empty books do.
    """
    client = TermFinanceClient(http)
    llama = DefiLlamaClient(http)
    prices: dict[tuple[int, str], float | None] = {}
    result: list[MarketRecord] = []
    for repo in client.live_repos():
        capacity = None
        if repo.lend_size is not None and repo.purchase_token:
            key = (repo.chain_id, repo.purchase_token.lower())
            if key not in prices:
                namespace = CHAIN_NAMESPACES.get(repo.chain_id)
                try:
                    prices[key] = None if namespace is None else llama.current_price(
                        f"{namespace}:{repo.purchase_token.lower()}"
                    )[0]
                except Exception:  # noqa: BLE001 - an unpriced size stays unstated, never guessed
                    prices[key] = None
            price = prices[key]
            capacity = None if price is None else repo.lend_size * price
        collateral = ", ".join(repo.collateral_symbols) or "unavailable"
        result.append(MarketRecord(
            venue="Term Finance", chain=CHAINS.get(repo.chain_id, str(repo.chain_id)),
            instrument=f"{repo.purchase_symbol or repo.purchase_token[:10]}/{collateral}"
                       f"@{repo.repurchase_at.date().isoformat()}",
            underlying=repo.purchase_symbol or repo.purchase_token,
            maturity=repo.repurchase_at,
            implied_apy=repo.lend_rate,
            liquidity_usd=None,
            executable_capacity_usd=capacity,
            slippage_curve=(), source_timestamp=_source_time(http, repo.endpoint),
            source_endpoint=repo.endpoint,
            metadata={"repo_id": repo.repo_id, "servicer": repo.servicer,
                      "repo_token": repo.repo_token, "chain_id": repo.chain_id,
                      "underlying_address": repo.purchase_token.lower() or None,
                      "collateral_symbols": list(repo.collateral_symbols),
                      "lender_ask_rate": repo.ask_rate,
                      "resting_borrow_bids": repo.lend_orders,
                      "literal_empty_book": repo.lend_rate is None},
        ))
    return result


def _lifi_records(http: HttpClient) -> list[MarketRecord]:
    api_key = load_api_key()
    if not api_key and not http.offline:
        raise SourceError(
            "COULD NOT VERIFY — LI.FI Earn needs LIFI_API_KEY in the environment or repo .env"
        )
    client = LifiEarnClient(http, api_key=api_key)
    morpho = MorphoClient(http)
    endpoint = f"GET {EARN_BASE}/v1/vaults"
    result: list[MarketRecord] = []
    for chain_id in (1, 8453):
        # Cross-check Morpho vault rates against Morpho itself. LI.FI overstated one Base vault by
        # 274bp (a reward component Morpho does not recognise) while agreeing with eleven others to
        # a few bp, so the disagreement is per-vault and cannot be corrected away in aggregate.
        # Where the two disagree we take the LOWER: a hurdle must never be inflated by an
        # unverifiable reward leg.
        try:
            morpho_rates = morpho.net_apy_by_address(chain_id)
        except Exception as exc:  # noqa: BLE001 - degrade to LI.FI alone, but say so
            morpho_rates = {}
            _LOG.warning("Morpho cross-check unavailable for chain %s: %s", chain_id, exc)
        raw_vaults = client.vaults(chain_id=chain_id, limit=100)
        if not raw_vaults:
            raise SourceError(f"{endpoint}: chain {chain_id} carried no usable vault evidence")
        timestamp = _source_time(http, endpoint)
        for raw in raw_vaults:
            vault = client.normalize_vault(raw)
            if "USDC" not in vault.underlying:
                continue
            tokens = raw.get("underlyingTokens") if isinstance(raw.get("underlyingTokens"), list) else []
            usdc = next(
                (token for token in tokens if isinstance(token, dict) and token.get("symbol") == "USDC"),
                {},
            )
            lifi_apy = None if vault.apy_total is None else vault.apy_total / 100.0
            native = morpho_rates.get(vault.address.lower()) if vault.protocol == "morpho" else None
            native_apy = native.get("net_apy") if native else None
            if lifi_apy is None:
                native_apy = None
            # 25bp tolerance: sources rounding a few tenths of a bp apart is agreement, not a
            # dispute. Only a material gap suppresses LI.FI's decomposition; the conservative
            # min() below still applies either way.
            _native_wins = native_apy is not None and (lifi_apy - native_apy) > 0.0025
            result.append(MarketRecord(
                venue="LI.FI Earn",
                chain=vault.chain,
                instrument=vault.name,
                underlying="USDC",
                maturity=None,
                implied_apy=lifi_apy if native_apy is None else min(lifi_apy, native_apy),
                liquidity_usd=vault.tvl_usd,
                executable_capacity_usd=None,
                slippage_curve=(),
                source_timestamp=timestamp,
                source_endpoint=endpoint,
                metadata={
                    "protocol": vault.protocol,
                    "chain_id": vault.chain_id,
                    "address": vault.address,
                    # Suppressed when the protocol-native rate wins: LI.FI's base/reward legs
                    # decompose LI.FI's total, so showing them beside a different total would not sum.
                    "apy_base": None if (vault.apy_base is None or _native_wins)
                        else vault.apy_base / 100.0,
                    "apy_reward": None if (vault.apy_reward is None or _native_wins)
                        else vault.apy_reward / 100.0,
                    "apy_30d": None if vault.apy_30d is None else vault.apy_30d / 100.0,
                    "redeem_steps": vault.redeem_steps,
                    "instant_exit": vault.instant_exit,
                    "is_redeemable": vault.is_redeemable,
                    "tags": vault.tags,
                    "underlying_price_usd": _finite(
                        usdc.get("priceUsd"), endpoint, "underlyingTokens.USDC.priceUsd", allow_none=True
                    ),
                    "verification_status": raw.get("verificationStatus"),
                    "lifi_apy_total": lifi_apy,
                    "protocol_native_apy": native_apy,
                    "protocol_native_registry": (native or {}).get("registry"),
                    "rate_source": "protocol-native (Morpho)" if _native_wins else "LI.FI Earn",
                    "rate_disagreement_bp": None if (native_apy is None or lifi_apy is None)
                        else round((lifi_apy - native_apy) * 10_000, 1),
                },
            ))
    if not result:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY any USDC venue")
    return result


def ingest_all(*, offline: bool = False, as_of: datetime | None = None) -> tuple[list[MarketRecord], list[str]]:
    http = EngineHttpClient(offline=offline)
    moment = as_of or datetime.now(UTC)
    records: list[MarketRecord] = []
    errors: list[str] = []
    for name, loader in (("Pendle", _pendle_records), ("Spectra", _spectra_records),
                         ("TermMax", _termmax_records), ("Morpho Midnight", _midnight_records),
                         ("Term Finance", _termfinance_records),
                         ("Exactly", _exactly_records),
                         ("LI.FI Earn", _lifi_records)):
        try:
            if name == "Morpho Midnight":
                records.extend(loader(http, errors, as_of=moment))
            else:
                records.extend(loader(http, errors) if name == "Pendle" else loader(http))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    records.sort(key=lambda row: (row.venue, row.chain, row.instrument,
                                  row.maturity or datetime.max.replace(tzinfo=UTC)))
    return records, errors


def write_snapshot(records: Iterable[MarketRecord], path: Path = ENGINE_DATA / "markets.json") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"written_at": datetime.now(UTC).isoformat(), "records": [record.as_dict() for record in records]}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize Par venues into one schema")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path, default=ENGINE_DATA / "markets.json")
    args = parser.parse_args()
    records, errors = ingest_all(offline=args.offline)
    print(write_snapshot(records, args.output))
    print(f"records={len(records)} errors={len(errors)}")
    for error in errors:
        print(f"UNAVAILABLE — {error}")
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
