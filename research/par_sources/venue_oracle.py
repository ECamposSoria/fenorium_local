from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .http import HttpClient, SourceError, require_mapping
from .oracle import (
    DESCRIPTION,
    LATEST_ROUND_DATA,
    OracleRound,
    decode_abi_string,
    decode_block_timestamp,
    decode_latest_round_data,
)
from .vault import CHAIN_RPCS, endpoints_for, request_rpc_json


SUBSCRIBED_VENUES = frozenset({
    "Pendle", "Spectra", "TermMax", "Tenor", "Term Finance", "Exactly",
})

# Official Exactly deployment registry, keyed by the asset contract the market wraps.  Keeping
# both sides of the mapping as addresses avoids the symbol collision class of failures.
EXACTLY_MARKETS = {
    (1, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"): "0x660e2fc185a9ffe722af253329ceaad4c9f6f928",
    (1, "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"): "0xc4d4500326981eacd020e20a81b1c479c161c7ef",
    (1, "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"): "0x8644c0fded361d1920e068ba4b09996e26729435",
    (1, "0x6b175474e89094c44da98b954eedeac495271d0f"): "0x163538e22f4d38c1eb21b79939f3d2ee274198ff",
    (1, "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"): "0x3843c41da1d7909c86fad51c47b9a97cf62a29e1",
    # Optimism runs two markets whose symbols differ by two characters. Verified against the
    # Exactly Auditor at 0xaEb62e6F27BC103702E7BC879AE98bceA56f027E on 2026-08-22 by reading
    # each market's own asset(): these two entries were transposed, which pointed native-USDC
    # evidence at the bridged market and vice versa.
    (10, "0x0b2c639c533813f4aa9d7837caf62653d097ff85"): "0x6926b434cce9b5b7966ae1bfeef6d0a7dcf3a8bb",  # exactly USDC (native)
    (10, "0x7f5c764cbc14f9669b88837ca1490cca17c31607"): "0x81c9a7b55a4df39a9b7b5f781ec0e53539694873",  # exactly USDC.e (bridged)
    (10, "0x4200000000000000000000000000000000000006"): "0xc4d4500326981eacd020e20a81b1c479c161c7ef",
    (10, "0x68f180fcce6836688e9084f035309e29bf0a2095"): "0x6f748fd65d7c71949ba6641b3248c4c191f3b322",
    (10, "0x1f32b1c2345538c0c6f582fcb022739c4a194ebb"): "0x22ab31cd55130435b5efbf9224b6a9d5ec36533f",
    (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"): "0x61edacb54aa8a689013682529df8914c87692e4b",
    (8453, "0x4200000000000000000000000000000000000006"): "0x52ee5238e5676598551c8d2bbccb62c72fc3a0c4",
}

# Selectors are pinned to the venue interfaces, not inferred from token symbols.
READ_TOKENS = "0x2c8ce6bc"          # Pendle IPMarket.readTokens()
EXCHANGE_RATE = "0x3ba0b9a9"        # Pendle IStandardizedYield.exchangeRate()
GET_PT_RATE = "0xa1c5b3e1"          # Spectra IPrincipalToken.getPTRate()
AUDITOR = "0x3ec045a6"              # Exactly Market.auditor()
MARKETS = "0x8e8f294b"              # Exactly Auditor.markets(address)
ORACLES = "0xaddd5099"              # TermMax OracleAggregator.oracles(address)
DECIMALS = "0x313ce567"             # AggregatorV3.decimals()


@dataclass(frozen=True)
class VenueOracleContext:
    venue: str
    identity: str
    underlying_address: str
    market_address: str | None = None
    pt_address: str | None = None
    pool_address: str | None = None
    oracle_accessor_address: str | None = None
    oracle_feed_addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class VenueOracleFeed:
    feed_address: str
    feed_decimals: int
    accessor: str


@dataclass(frozen=True)
class VenueOracleObservation:
    feed: VenueOracleFeed
    round_data: OracleRound
    description: str
    block_timestamp: datetime


def _address(value: str | None, field: str) -> str:
    candidate = str(value or "").lower()
    if len(candidate) != 42 or not candidate.startswith("0x"):
        raise SourceError(f"COULD NOT VERIFY oracle_model_risk: {field} is not an exact contract address")
    try:
        int(candidate[2:], 16)
    except ValueError as exc:
        raise SourceError(
            f"COULD NOT VERIFY oracle_model_risk: {field} is not an exact contract address"
        ) from exc
    if candidate == "0x" + "0" * 40:
        raise SourceError(f"COULD NOT VERIFY oracle_model_risk: {field} returned the zero address")
    return candidate


def _address_arg(value: str) -> str:
    return _address(value, "oracle accessor argument")[2:].rjust(64, "0")


def _decode_words(payload: Any, endpoint: str, *, minimum_words: int = 1) -> list[int]:
    result = require_mapping(payload, endpoint).get("result")
    if not isinstance(result, str) or not result.startswith("0x") or len(result) <= 2:
        raise SourceError(f"{endpoint}: COULD NOT VERIFY oracle_model_risk; missing hex result")
    encoded = result[2:]
    if len(encoded) % 64 or len(encoded) < minimum_words * 64:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY oracle_model_risk; malformed ABI result"
        )
    try:
        return [int(encoded[offset:offset + 64], 16)
                for offset in range(0, len(encoded), 64)]
    except ValueError as exc:
        raise SourceError(
            f"{endpoint}: COULD NOT VERIFY oracle_model_risk; malformed ABI hex"
        ) from exc


def _word_address(word: int, field: str) -> str:
    if word >> 160:
        raise SourceError(f"COULD NOT VERIFY oracle_model_risk: {field} is not ABI address data")
    return _address(f"0x{word:040x}", field)


def _is_nav_description(description: str) -> bool:
    normalized = description.casefold()
    return "net asset value" in normalized or "nav" in normalized.split()


class VenueOracleClient:
    """Read the oracle model selected by a venue for one exact market contract."""

    def __init__(
        self,
        http: HttpClient,
        rpc_endpoints: dict[int, tuple[str, ...]] | None = None,
        *,
        backoff_seconds: float = 0.25,
    ) -> None:
        self.http = http
        self.rpc_endpoints = (
            {chain_id: endpoints_for(chain_id) for chain_id in CHAIN_RPCS}
            if rpc_endpoints is None else dict(rpc_endpoints)
        )
        self.backoff_seconds = backoff_seconds

    def _endpoints(self, chain_id: int) -> tuple[str, ...]:
        configured = self.rpc_endpoints.get(chain_id, ())
        if not configured:
            raise SourceError(
                f"COULD NOT VERIFY oracle_model_risk: no verified RPC for chain {chain_id}"
            )
        return configured

    def _request(
        self,
        chain_id: int,
        *,
        cache_name: str,
        method: str,
        params: list[Any],
        operation: str,
        validate: Any,
        prefer_cache_minutes: float | None = None,
    ) -> tuple[Any, str]:
        return request_rpc_json(
            self.http, chain_id, self._endpoints(chain_id),
            cache_name=cache_name,
            method=method,
            params=params,
            operation=operation,
            validate=validate,
            backoff_seconds=self.backoff_seconds,
            prefer_cache_minutes=prefer_cache_minutes,
        )

    def _call_words(
        self, chain_id: int, contract: str, calldata: str, *, cache_name: str,
        minimum_words: int = 1,
    ) -> list[int]:
        target = _address(contract, "venue oracle accessor")

        def has_words(payload: Any, endpoint: str) -> bool:
            try:
                _decode_words(payload, endpoint, minimum_words=minimum_words)
            except SourceError:
                return False
            return True

        payload, endpoint = self._request(
            chain_id,
            cache_name=cache_name,
            method="eth_call",
            params=[{"to": target, "data": calldata}, "latest"],
            operation=f"eth_call {target} {calldata[:10]}",
            validate=has_words,
        )
        return _decode_words(payload, endpoint, minimum_words=minimum_words)

    def _latest_round_data(
        self, chain_id: int, feed_address: str, *, cache_name: str,
    ) -> OracleRound:
        address = _address(feed_address, "venue oracle feed")

        def has_round(payload: Any, endpoint: str) -> bool:
            try:
                decode_latest_round_data(payload, endpoint)
            except SourceError:
                return False
            return True

        payload, endpoint = self._request(
            chain_id,
            cache_name=cache_name,
            method="eth_call",
            params=[{"to": address, "data": LATEST_ROUND_DATA}, "latest"],
            operation=f"eth_call {address} latestRoundData()",
            validate=has_round,
        )
        answer, updated_at = decode_latest_round_data(payload, endpoint)
        return OracleRound(address, answer, updated_at, endpoint)

    def _description(self, chain_id: int, feed_address: str, *, cache_name: str) -> str:
        address = _address(feed_address, "venue oracle feed")

        def has_description(payload: Any, endpoint: str) -> bool:
            try:
                decode_abi_string(payload, endpoint)
            except SourceError:
                return False
            return True

        payload, endpoint = self._request(
            chain_id,
            cache_name=cache_name,
            method="eth_call",
            params=[{"to": address, "data": DESCRIPTION}, "latest"],
            operation=f"eth_call {address} description()",
            validate=has_description,
        )
        return decode_abi_string(payload, endpoint)

    def _latest_block_timestamp(self, chain_id: int, *, cache_name: str) -> datetime:
        def has_timestamp(payload: Any, endpoint: str) -> bool:
            try:
                decode_block_timestamp(payload, endpoint)
            except SourceError:
                return False
            return True

        payload, endpoint = self._request(
            chain_id,
            cache_name=cache_name,
            method="eth_getBlockByNumber",
            params=["latest", False],
            operation="eth_getBlockByNumber latest",
            validate=has_timestamp,
            prefer_cache_minutes=1,
        )
        return decode_block_timestamp(payload, endpoint)

    def _standard_feed(
        self, chain_id: int, feed_address: str, *, accessor: str, cache_prefix: str,
    ) -> VenueOracleObservation:
        feed_address = _address(feed_address, "venue oracle feed")
        decimals = self._call_words(
            chain_id, feed_address, DECIMALS,
            cache_name=f"{cache_prefix}_decimals_{feed_address}",
        )[0]
        if decimals > 255:
            raise SourceError(
                f"COULD NOT VERIFY oracle_model_risk: feed {feed_address} returned invalid decimals"
            )
        round_data = self._latest_round_data(
            chain_id, feed_address,
            cache_name=f"{cache_prefix}_latest_round_{feed_address}",
        )
        description = self._description(
            chain_id, feed_address,
            cache_name=f"{cache_prefix}_description_{feed_address}",
        )
        if _is_nav_description(description):
            raise SourceError(
                f"COULD NOT VERIFY oracle_model_risk: venue feed {feed_address} "
                f"({description}) is NAV, not market price discovery"
            )
        block_timestamp = self._latest_block_timestamp(
            chain_id, cache_name=f"{cache_prefix}_block_{feed_address}",
        )
        return VenueOracleObservation(
            VenueOracleFeed(feed_address, int(decimals), accessor),
            round_data, description, block_timestamp,
        )

    def _native_adapter(
        self, chain_id: int, feed_address: str, *, answer: int, decimals: int,
        description: str, accessor: str, cache_prefix: str,
    ) -> VenueOracleObservation:
        if answer <= 0:
            raise SourceError(
                f"COULD NOT VERIFY oracle_model_risk: {accessor} returned a non-positive value"
            )
        block_timestamp = self._latest_block_timestamp(
            chain_id, cache_name=f"{cache_prefix}_block",
        )
        feed = VenueOracleFeed(_address(feed_address, "venue oracle adapter"), decimals, accessor)
        return VenueOracleObservation(
            feed,
            OracleRound(feed.feed_address, answer, block_timestamp, accessor),
            description,
            block_timestamp,
        )

    def resolve(
        self, chain_id: int, context: VenueOracleContext,
    ) -> list[VenueOracleObservation]:
        if context.venue not in SUBSCRIBED_VENUES:
            return []
        prefix = f"venue_oracle_{context.venue.lower().replace(' ', '_')}_{chain_id}_{context.identity}"

        if context.oracle_feed_addresses:
            return [
                self._standard_feed(
                    chain_id, feed, accessor=f"{context.venue} published oracle feed",
                    cache_prefix=prefix,
                )
                for feed in sorted(set(context.oracle_feed_addresses))
            ]

        if context.venue == "Pendle":
            market = _address(context.market_address, "Pendle market")
            words = self._call_words(
                chain_id, market, READ_TOKENS,
                cache_name=f"{prefix}_read_tokens", minimum_words=3,
            )
            sy = _word_address(words[0], "Pendle SY")
            answer = self._call_words(
                chain_id, sy, EXCHANGE_RATE,
                cache_name=f"{prefix}_exchange_rate",
            )[0]
            return [self._native_adapter(
                chain_id, sy, answer=answer, decimals=18,
                description="Pendle on-chain SY exchange-rate oracle adapter",
                accessor=f"Pendle market {market} readTokens().SY.exchangeRate()",
                cache_prefix=prefix,
            )]

        if context.venue == "Spectra":
            pt = _address(context.pt_address, "Spectra principal token")
            answer = self._call_words(
                chain_id, pt, GET_PT_RATE, cache_name=f"{prefix}_pt_rate",
            )[0]
            return [self._native_adapter(
                chain_id, pt, answer=answer, decimals=27,
                description="Spectra on-chain principal-token rate oracle adapter",
                accessor=f"Spectra principal token {pt} getPTRate()",
                cache_prefix=prefix,
            )]

        if context.venue == "TermMax":
            accessor = _address(context.oracle_accessor_address, "TermMax oracle aggregator")
            asset = _address(context.underlying_address, "TermMax underlying asset")
            words = self._call_words(
                chain_id, accessor, ORACLES + _address_arg(asset),
                cache_name=f"{prefix}_oracle_config", minimum_words=3,
            )
            feed = _word_address(words[0], "TermMax primary aggregator")
            return [self._standard_feed(
                chain_id, feed,
                accessor=f"TermMax oracle aggregator {accessor} oracles({asset})",
                cache_prefix=prefix,
            )]

        if context.venue == "Exactly":
            market = _address(context.market_address, "Exactly market")
            auditor = _word_address(self._call_words(
                chain_id, market, AUDITOR, cache_name=f"{prefix}_auditor",
            )[0], "Exactly auditor")
            words = self._call_words(
                chain_id, auditor, MARKETS + _address_arg(market),
                cache_name=f"{prefix}_market_config", minimum_words=5,
            )
            feed = _word_address(words[4], "Exactly market price feed")
            return [self._standard_feed(
                chain_id, feed,
                accessor=f"Exactly market {market} auditor().markets(market).priceFeed",
                cache_prefix=prefix,
            )]

        # Tenor can publish exact feed addresses in its market GraphQL object. Term Finance's
        # current public repo response supplies a servicer but no externally readable feed
        # accessor. Both remain explicit gaps unless ingestion supplied exact feed contracts.
        handle = context.market_address or context.pool_address or context.identity
        raise SourceError(
            f"COULD NOT VERIFY oracle_model_risk: {context.venue} market {handle} did not "
            "publish an exact readable oracle feed contract"
        )
