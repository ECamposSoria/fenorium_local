from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable

from research.par_sources.venue_oracle import VenueOracleContext

from .models import MarketRecord
from .midnight import MIDNIGHT_VENUES


# Denomination classes a venue can declare. `stables` is Pendle's own tag; `hype` appears on
# HyperEVM. Anything outside this vocabulary stays UNKNOWN rather than being guessed from the
# symbol — `yoUSD` is not a wrapper of `oUSD`, and a symbol that ends in USD is not a promise.
CATEGORY_DENOMINATIONS = {
    "stables": "USD",
    "eth": "ETH",
    "btc": "BTC",
    "hype": "HYPE",
}

UNKNOWN = "UNKNOWN"

# Canonical base assets, keyed by contract and carrying the symbol the contract itself reports.
# The address is the identity and the symbol is the check: a wrong address fails the assertion
# instead of quietly denominating something in the wrong unit. Read on-chain 2026-08-19.
CANONICAL_UNITS = {
    (1, "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"): ("ETH", "WETH"),
    (8453, "0x4200000000000000000000000000000000000006"): ("ETH", "WETH"),
    (42161, "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"): ("ETH", "WETH"),
    (1, "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"): ("ETH", "stETH"),
    (56, "0xb0b84d294e0c75a6abe60171b70edeb2efd14a1b"): ("BNB", "slisBNB"),
    (10, "0x4200000000000000000000000000000000000006"): ("ETH", "WETH"),
    (10, "0x1f32b1c2345538c0c6f582fcb022739c4a194ebb"): ("ETH", "wstETH"),
    (10, "0x68f180fcce6836688e9084f035309e29bf0a2095"): ("BTC", "WBTC"),
    (1, "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"): ("BTC", "WBTC"),
}


@dataclass(frozen=True)
class UniverseRow:
    """One fixed-maturity instrument, from any venue, with the unit it is a claim on."""

    venue: str
    chain: str
    chain_id: int
    symbol: str
    underlying_symbol: str | None
    underlying_address: str | None
    maturity: datetime | None
    implied_apy: float | None
    liquidity_usd: float | None
    source_endpoint: str
    denomination: str
    denomination_source: str
    identity: str
    numeraire_coin: str | None = None
    oracle_context: VenueOracleContext | None = None
    collaterals: tuple[dict[str, Any], ...] | None = None
    total_units: str | None = None

    @property
    def name(self) -> str:
        stamp = "no-maturity" if self.maturity is None else self.maturity.date().isoformat()
        return f"{self.symbol}/{self.chain}/{stamp}"

    @property
    def evidence_key(self) -> tuple[int, str, str] | None:
        if not self.underlying_address or not self.underlying_symbol:
            return None
        return (self.chain_id, self.underlying_address.lower(), self.denomination)


def _stablecoin_pool_index(pools: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    """(chain, underlying address) pairs DefiLlama marks stable, from single-asset pools only.

    A multi-token pool flagged `stablecoin: true` says the pool is a stablecoin pool, not that
    every leg is a stablecoin. Reading all legs marked WETH as dollar-denominated through pools
    named `USDC+WBTC+ETH` and `CRVUSDETHCRV`, which sent seven Term Finance WETH repos down the
    USD path where ETH's own volatility scores as a 2,672bp depeg.
    """
    index: set[tuple[str, str]] = set()
    for pool in pools:
        if pool.get("stablecoin") is not True:
            continue
        chain = str(pool.get("chain") or "")
        tokens = pool.get("underlyingTokens")
        if not chain or not isinstance(tokens, list) or len(tokens) != 1:
            continue
        token = tokens[0]
        if isinstance(token, str) and token.startswith("0x"):
            index.add((chain.casefold(), token.lower()))
    return index


def resolve_denomination(
    record: MarketRecord, stablecoin_pools: set[tuple[str, str]],
) -> tuple[str, str]:
    """Take the unit from a source that declares it, or say it is unknown.

    Two sources, in order: the venue's own classification, then DefiLlama's stablecoin flag on a
    pool holding the same contract. Neither is inference from the ticker.
    """
    categories = record.metadata.get("category_ids")
    if isinstance(categories, list):
        declared = {CATEGORY_DENOMINATIONS[str(item)] for item in categories
                    if str(item) in CATEGORY_DENOMINATIONS}
        if len(declared) == 1:
            unit = declared.pop()
            return unit, f"{record.venue} classifies this market as {unit.lower()}-denominated"
        if len(declared) > 1:
            return UNKNOWN, (
                f"COULD NOT VERIFY denomination: {record.venue} tags this market as "
                f"{'/'.join(sorted(declared))} at once"
            )

    address = str(record.metadata.get("underlying_address") or "").lower()
    chain_id = record.metadata.get("chain_id")
    canonical = CANONICAL_UNITS.get((chain_id, address)) if isinstance(chain_id, int) else None
    if canonical is not None:
        unit, expected = canonical
        return unit, f"canonical {expected} contract {address} is a claim on {unit}"

    if address and (record.chain.casefold(), address) in stablecoin_pools:
        return "USD", "DefiLlama marks a single-asset pool of this contract as a stablecoin pool"

    return UNKNOWN, (
        f"COULD NOT VERIFY denomination: neither {record.venue} nor DefiLlama declares the unit "
        f"{record.underlying or record.symbol} is a claim on"
    )


def record_identity(record: MarketRecord) -> str | None:
    """Stable key for one market across the universe and the capacity table."""
    chain_id = record.metadata.get("chain_id")
    if not isinstance(chain_id, int):
        return None
    handle = str(
        record.metadata.get("pt_address")
        or record.metadata.get("market_address")
        or record.metadata.get("pool_address")
        or record.instrument
    ).lower()
    return f"{record.venue}:{chain_id}:{handle}"


def build_universe(
    records: Iterable[MarketRecord],
    yields_pools: Iterable[dict[str, Any]],
) -> list[UniverseRow]:
    """Every fixed-maturity instrument we ingest, not only the ones from one venue.

    Floating positions (no maturity) are the reserve ladder's business and are excluded here;
    everything with a maturity is fixed income and belongs in the curve, whatever unit it pays in.
    """
    stablecoin_pools = _stablecoin_pool_index(yields_pools)
    rows: list[UniverseRow] = []
    for record in records:
        if record.maturity is None:
            continue
        chain_id = record.metadata.get("chain_id")
        if not isinstance(chain_id, int):
            continue
        denomination, source = resolve_denomination(record, stablecoin_pools)
        address = record.metadata.get("underlying_address")
        identity = record_identity(record)
        if identity is None:
            continue
        rows.append(UniverseRow(
            venue=record.venue, chain=record.chain, chain_id=chain_id,
            symbol=record.instrument,
            underlying_symbol=record.underlying,
            underlying_address=None if not isinstance(address, str) else address,
            maturity=record.maturity, implied_apy=record.implied_apy,
            liquidity_usd=record.liquidity_usd, source_endpoint=record.source_endpoint,
            denomination=denomination, denomination_source=source,
            identity=identity,
            oracle_context=VenueOracleContext(
                venue=record.venue,
                identity=identity,
                underlying_address=address if isinstance(address, str) else "",
                market_address=record.metadata.get("oracle_market_address")
                    if isinstance(record.metadata.get("oracle_market_address"), str) else (
                        record.metadata.get("market_address")
                        if isinstance(record.metadata.get("market_address"), str) else (
                            record.metadata.get("servicer")
                            if isinstance(record.metadata.get("servicer"), str) else None
                        )
                    ),
                pt_address=record.metadata.get("pt_address")
                    if isinstance(record.metadata.get("pt_address"), str) else None,
                pool_address=record.metadata.get("pool_address")
                    if isinstance(record.metadata.get("pool_address"), str) else None,
                oracle_accessor_address=record.metadata.get("oracle_accessor_address")
                    if isinstance(record.metadata.get("oracle_accessor_address"), str) else None,
                oracle_feed_addresses=tuple(record.metadata.get("oracle_feed_addresses") or ()),
            ),
            collaterals=(tuple(record.metadata.get("collaterals") or ())
                         if record.venue in MIDNIGHT_VENUES else None),
            total_units=(str(record.metadata.get("total_units"))
                         if record.venue in MIDNIGHT_VENUES
                         and record.metadata.get("total_units") is not None else None),
        ))
    rows.sort(key=lambda row: (row.venue, row.chain, row.symbol, row.name))
    return rows


def refine_unknown_denominations(
    rows: list[UniverseRow],
    stablecoins: list[dict[str, Any]],
    detail_loader: Any,
) -> list[UniverseRow]:
    """Last source for rows no venue classified: the issuer registry, matched by contract.

    Only issuers whose symbol matches an unresolved row are looked up, so this costs a handful of
    detail calls rather than 417. The symbol narrows the search; the **contract** decides, because
    44 symbols in that registry belong to more than one issuer.
    """
    unresolved = [row for row in rows if row.denomination == UNKNOWN and row.underlying_address]
    if not unresolved:
        return rows
    wanted = {str(row.underlying_symbol or "").casefold() for row in unresolved}
    wanted.discard("")
    resolved: dict[str, tuple[str, str]] = {}
    for issuer in stablecoins:
        if str(issuer.get("symbol") or "").casefold() not in wanted:
            continue
        peg_type = str(issuer.get("pegType") or "")
        if peg_type != "peggedUSD":
            continue
        issuer_id = issuer.get("id")
        if issuer_id is None:
            continue
        try:
            detail = detail_loader(str(issuer_id))
        except Exception:  # noqa: BLE001 - an unreachable issuer leaves the row unresolved
            continue
        for address in _issuer_addresses(detail):
            resolved[address] = (str(issuer.get("name") or issuer_id), str(issuer_id))
    if not resolved:
        return rows
    out: list[UniverseRow] = []
    for row in rows:
        hit = resolved.get((row.underlying_address or "").lower()) if row.denomination == UNKNOWN else None
        if hit is None:
            out.append(row)
            continue
        name, issuer_id = hit
        out.append(replace(
            row, denomination="USD",
            denomination_source=(
                f"DefiLlama stablecoin issuer {name} (id {issuer_id}) issues this contract, "
                f"pegType peggedUSD"
            ),
        ))
    return out


def _issuer_addresses(detail: Any) -> set[str]:
    """Every contract the issuer detail payload attributes to itself, on any chain."""
    found: set[str] = set()
    if not isinstance(detail, dict):
        return found
    primary = detail.get("address")
    if isinstance(primary, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", primary):
        found.add(primary.lower())
    chains = (detail.get("chainConfig") or {}).get("chains")
    if isinstance(chains, dict):
        for config in chains.values():
            if not isinstance(config, dict):
                continue
            for group in config.values():
                if not isinstance(group, list):
                    continue
                for item in group:
                    if isinstance(item, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", item):
                        found.add(item.lower())
    return found


def refine_with_onchain_asset(
    rows: list[UniverseRow], vault_client: Any, chain_namespaces: dict[int, str],
) -> list[UniverseRow]:
    """Last source: an ERC-4626 share declares, on-chain, the token it is a claim on.

    `sENA` answers ENA and `sKAITO` answers KAITO. The unit becomes that asset's own symbol and
    the numeraire becomes its own contract, so no slug table has to be kept and no ticker is
    interpreted. A token that exposes no `asset()` stays unresolved.
    """
    unresolved = [row for row in rows if row.denomination == UNKNOWN and row.underlying_address]
    if not unresolved:
        return rows
    resolved: dict[tuple[int, str], tuple[str, str, str]] = {}
    for row in unresolved:
        key = (row.chain_id, (row.underlying_address or "").lower())
        if key in resolved:
            continue
        namespace = chain_namespaces.get(row.chain_id)
        if namespace is None:
            continue
        try:
            asset_address, _decimals = vault_client.underlying_asset(row.chain_id, key[1])
            symbol = vault_client.symbol(row.chain_id, asset_address)
        except Exception:  # noqa: BLE001 - a token that will not declare stays unresolved
            continue
        canonical = CANONICAL_UNITS.get((row.chain_id, asset_address))
        unit = canonical[0] if canonical else symbol.upper()
        resolved[key] = (unit, f"{namespace}:{asset_address}",
                         f"ERC-4626 asset() declares this share is a claim on {symbol}")
    if not resolved:
        return rows
    out: list[UniverseRow] = []
    for row in rows:
        hit = resolved.get((row.chain_id, (row.underlying_address or "").lower())) \
            if row.denomination == UNKNOWN else None
        if hit is None:
            out.append(row)
            continue
        unit, coin, reason = hit
        out.append(replace(row, denomination=unit, denomination_source=reason, numeraire_coin=coin))
    return out
