"""One registry of the chains, assets and instruments Fenorium knows.

Chains come from `chains.json`, shipped with the code. Instruments come from the nightly universe,
expressed here with stable ids and the static half of their buyability. Assets are every balance
either of those names: each chain's gas token and USDC, every instrument token and every
instrument's underlying.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from engine.midnight import MIDNIGHT_VENUES
from .chains import Chain, ChainRegistry, RegistryError, Token, registry
from .instruments import (
    DEPOSIT_VENUES,
    INSTRUMENT_SCHEMA,
    PERMISSIONLESS,
    asset_id,
    instrument_id,
    instrument_record,
    parse_asset_id,
    purchase_refusals,
    universe_decimals,
)


REGISTRY_SCHEMA = "fenorium/registry/1"


def _asset(chain: Chain, address: str | None, *, symbol: str | None, decimals: int | None,
           role: str) -> dict[str, Any]:
    return {
        "asset_id": asset_id(chain.chain_id, address),
        "chain_id": chain.chain_id,
        "address": None if address is None else address.lower(),
        "kind": "NATIVE" if address is None else "ERC20",
        "symbol": symbol,
        "decimals": decimals,
        "price_key": chain.llama_coin(address),
        "roles": [role],
    }


def assets(chains: ChainRegistry, instruments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every balance the registry can name, once each, with what is known about it."""
    found: dict[str, dict[str, Any]] = {}

    def add(record: dict[str, Any]) -> None:
        existing = found.get(record["asset_id"])
        if existing is None:
            found[record["asset_id"]] = record
            return
        existing["roles"] = sorted(set(existing["roles"]) | set(record["roles"]))
        for field in ("symbol", "decimals"):
            if existing[field] is None:
                existing[field] = record[field]

    for chain in chains.chains:
        add(_asset(chain, None, symbol=chain.native_symbol, decimals=chain.native_decimals, role="native"))
        for token in ((chain.usdc,) if chain.usdc is not None else ()) + chain.settlement:
            add(_asset(chain, token.address, symbol=token.symbol, decimals=token.decimals, role="settlement"))
    for record in instruments:
        chain = chains.get(record.get("chain_id"))
        if chain is None:
            continue
        if record.get("address"):
            role = "settlement" if record.get("category") == "settlement" else "instrument"
            add(_asset(chain, record["address"], symbol=record.get("symbol"), decimals=record.get("decimals"),
                       role=role))
        underlying = record.get("underlying_asset_id")
        if underlying:
            address = underlying.rsplit(":", 1)[-1]
            add(_asset(chain, address, symbol=None, decimals=None, role="underlying"))
    return sorted(found.values(), key=lambda row: (row["chain_id"], row["asset_id"]))


def _directory_key(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    chain_id, address = row.get("chain_id"), row.get("address")
    if type(chain_id) is not int:
        return None
    if isinstance(address, str) and address:
        return ("token", chain_id, address.lower())
    if isinstance(row.get("venue"), str) and isinstance(row.get("name"), str):
        return ("position", chain_id, row["venue"], row["name"])
    return None


def registry_document(universe: Mapping[str, Any] | None, chains: ChainRegistry | None = None,
                      *, now: datetime | None = None, expired: bool = False) -> dict[str, Any]:
    """The whole registry: chains, instruments from `universe`, and the assets both name.

    `expired` says the universe is too old to buy from; every instrument then carries
    UNIVERSE_EXPIRED and none is buyable, exactly as the purchase endpoints answer.
    """
    chains = chains or registry()
    now = now or datetime.now(UTC)
    rows = [row for row in (universe.get("instruments") if isinstance(universe, Mapping) else None) or ()
            if isinstance(row, Mapping)]
    decimals = universe_decimals(rows)
    instruments = [instrument_record(row, chains, now=now, token_decimals=decimals, expired=expired)
                   for row in rows]
    # The timestamp belongs beside the calendar date in the registry's fixed-income terms.
    # Keep it on the assembled instrument, where wallet readers consume the registry document.
    for row, instrument in zip(rows, instruments):
        fixed_income = instrument.get("fixed_income")
        if fixed_income is not None:
            timestamp = row.get("maturity_timestamp")
            fixed_income["maturity_timestamp"] = (
                timestamp if type(timestamp) is int and timestamp >= 0 else None
            )
    # Two rows claiming one instrument are refused by the purchase endpoints rather than resolved
    # by picking one, so neither is advertised. Keyed exactly as InstrumentDirectory keys them.
    claims: dict[tuple[Any, ...], int] = {}
    keys = [_directory_key(row) for row in rows]
    for key in keys:
        if key is not None:
            claims[key] = claims.get(key, 0) + 1
    for key, record in zip(keys, instruments):
        if key is not None and claims[key] > 1:
            purchase = record["purchase"]
            purchase["reasons"] = sorted(set(purchase["reasons"]) | {"INSTRUMENT_AMBIGUOUS"})
            purchase["buyable"] = False
    for row, instrument in zip(rows, instruments):
        # Score is a source-universe field, separate from purchase eligibility. UNRATED rows
        # have no evidence score even if an older publisher left stale fields behind.
        unrated = str(row.get("verdict") or "").upper() == "UNRATED"
        instrument["fenorium_score"] = None if unrated else row.get("fenorium_score")
        instrument["score_cap"] = None if unrated else row.get("score_cap")
        if row.get("venue") in {"Pendle", "Spectra"}:
            # Wallet reads retain these identities after the venue stops listing the PT.
            for field in ("maturity_timestamp", "market_address", "accounting_asset_address", "underlying_address"):
                if field in row:
                    instrument[field] = row[field]
        if row.get("venue") in MIDNIGHT_VENUES:
            for field in ("market_id", "loan_token", "underlying_address", "market_address",
                          "maturity_timestamp", "rate_kind", "depth_usd", "collaterals", "listed"):
                if field in row:
                    instrument[field] = row[field]
    return {
        "schema_version": REGISTRY_SCHEMA,
        "universe_generated_at": universe.get("generated_at") if isinstance(universe, Mapping) else None,
        "universe_expired": expired,
        "fee_recipient": chains.fee_recipient,
        "chains": [chain.to_public() for chain in chains.chains],
        "assets": assets(chains, instruments),
        "instruments": instruments,
    }


__all__ = [
    "Chain",
    "ChainRegistry",
    "DEPOSIT_VENUES",
    "INSTRUMENT_SCHEMA",
    "PERMISSIONLESS",
    "REGISTRY_SCHEMA",
    "RegistryError",
    "Token",
    "asset_id",
    "assets",
    "instrument_id",
    "instrument_record",
    "parse_asset_id",
    "purchase_refusals",
    "registry",
    "registry_document",
]
