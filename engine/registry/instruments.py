"""Stable identities for assets and instruments, and the static half of "can this be bought".

An asset is a balance a wallet can hold: a chain's gas token or an ERC-20 contract. An
instrument is something Fenorium rates: usually a token (then it is also an asset and shares its
id), sometimes a position in a venue's own contract, which has no token to hold.

The ids never contain a ticker, a display name or a classification. A vault reclassified as a PT,
or a symbol a venue renamed, is still the same balance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

from engine.midnight import MIDNIGHT_VENUES
from .chains import ChainRegistry


INSTRUMENT_SCHEMA = "fenorium/registry-instrument/1"
PERMISSIONLESS = "PERMISSIONLESS"
SETTLEMENT_CATEGORY = "settlement"
# Venues where a position is bought by depositing into the venue's contract directly, never
# through a route. See `deploy/platform/api.py` `position_deposit`.
DEPOSIT_VENUES = frozenset({"Exactly"})
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_SAFE = "@._-"


def asset_id(chain_id: int, address: str | None) -> str:
    """`eip155:<chain>/native` for a gas token, `eip155:<chain>/erc20:<lowercase address>` otherwise."""
    if type(chain_id) is not int or chain_id <= 0:
        raise ValueError("chain_id must be a positive integer")
    if address is None:
        return f"eip155:{chain_id}/native"
    if not isinstance(address, str) or _ADDRESS.fullmatch(address) is None:
        raise ValueError("address must be a 0x-prefixed 20-byte address")
    return f"eip155:{chain_id}/erc20:{address.lower()}"


_ASSET_ID = re.compile(r"eip155:([1-9][0-9]{0,11})/(?:native|erc20:(0x[0-9a-f]{40}))")


def parse_asset_id(value: Any) -> tuple[int, str | None]:
    """(chain_id, lowercase address or None for the gas token) from an asset id, or ValueError."""
    match = _ASSET_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("not an asset id: expected eip155:<chain>/native or eip155:<chain>/erc20:<address>")
    return int(match.group(1)), match.group(2)


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    return quote(re.sub(r"\s+", "-", text) or "unknown", safe=_SAFE)


def _address(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and _ADDRESS.fullmatch(value) else None


def _evm_chain(row: Mapping[str, Any], chains: ChainRegistry) -> int | None:
    """The registered EVM chain a row lives on, from its id or else from its chain label."""
    chain_id = row.get("chain_id")
    if type(chain_id) is int and chains.get(chain_id) is not None:
        return chain_id
    if chain_id is None:
        return chains.resolve(row.get("chain"))
    return None


def instrument_id(row: Mapping[str, Any], chains: ChainRegistry) -> str:
    """The stable id of one universe row.

    A token is its asset id. A position is named by its chain, venue and the venue's own name for
    it. A row on a chain the registry does not know (Solana, XRPL, ...) keeps its source label, so
    it is identifiable and explicitly unsupported rather than silently dropped.
    """
    chain_id = _evm_chain(row, chains)
    address = _address(row.get("address"))
    if chain_id is not None and address is not None:
        return asset_id(chain_id, address)
    venue, name = _slug(row.get("venue")), quote(str(row.get("name") or "").strip(), safe=_SAFE)
    if chain_id is not None:
        return f"eip155:{chain_id}/position:{venue}:{name}"
    return f"unsupported:{_slug(row.get('chain'))}/{venue}:{name}"


def _maturity_at(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(f"{value}T00:00:00+00:00")
    except (TypeError, ValueError):
        return None


def purchase_refusals(row: Mapping[str, Any], chains: ChainRegistry, *, now: datetime | None = None,
                      token_decimals: Callable[[int, str], int | None] | None = None) -> tuple[str | None, list[str]]:
    """How this row would be bought ("ROUTE" or "DEPOSIT"), and every static reason it cannot be.

    Static means decided by the row, the registry and the clock alone. Whether a route exists at
    the size a buyer asks for, and at what price, is answered per request and is not decided here.
    A deposit is refused on exactly the terms `/v1/position-deposits` refuses it, so the registry
    never advertises a position that endpoint would turn away. That includes knowing the deposit
    asset's decimals, which only the universe can say: `token_decimals(chain_id, address)` answers
    from it, and without it a deposit is never called buyable.
    """
    reasons: list[str] = []
    chain_id = _evm_chain(row, chains)
    chain = chains.get(chain_id)
    if chain is None:
        return None, ["CHAIN_UNSUPPORTED"]
    if type(row.get("chain_id")) is not int:
        # The chain was recognised from the row's label, which is enough to name it but not to buy
        # it: the purchase endpoints find an instrument only by the chain id the row states.
        reasons.append("CHAIN_ID_MISSING")
    if row.get("testnet") is True or chain.testnet:
        return None, ["INSTRUMENT_IS_TESTNET"]
    if row.get("category") == SETTLEMENT_CATEGORY:
        return None, ["NOT_AN_INSTRUMENT"]
    if not chain.has("buy"):
        reasons.append("CHAIN_NOT_A_DESTINATION")
    verdict = row.get("verdict")
    address = _address(row.get("address"))
    if address is None:
        if row.get("venue") not in DEPOSIT_VENUES:
            if verdict in ("NO BID", "NO QUOTE"):
                reasons.append(verdict.replace(" ", "_"))
            return None, reasons + ["POSITION_NOT_PURCHASABLE"]
        underlying = _address(row.get("underlying_address"))
        if underlying is None or _address(row.get("market_address")) is None:
            reasons.append("POSITION_CONTRACT_UNKNOWN")
        elif token_decimals is None or token_decimals(chain.chain_id, underlying) is None:
            reasons.append("INSTRUMENT_DECIMALS_UNKNOWN")
        if verdict == "NO BID":
            reasons.append("NO_BID")
        elif verdict == "NO QUOTE" or type(row.get("rate_bps")) is not int or row.get("rate_kind") != "executable":
            # The deposit's floor is computed from an executable rate; without one there is none.
            reasons.append("NO_QUOTE")
        maturity = _maturity_at(row.get("maturity"))
        if maturity is None:
            reasons.append("MATURITY_UNKNOWN")
        elif maturity <= (now or datetime.now(UTC)):
            reasons.append("MATURED")
        return "DEPOSIT", reasons
    if verdict == "NO BID":
        reasons.append("NO_BID")
    elif verdict == "NO QUOTE":
        reasons.append("NO_QUOTE")
    decimals = row.get("decimals")
    if type(decimals) is not int or decimals < 0:
        reasons.append("INSTRUMENT_DECIMALS_UNKNOWN")
    gate = row.get("gate")
    status = gate.get("status") if isinstance(gate, Mapping) else None
    if status != PERMISSIONLESS:
        # Unknown is refused rather than assumed open: absent evidence is never evidence of absence.
        reasons.append("TRANSFER_GATE_UNKNOWN" if status in (None, "UNKNOWN") else "TRANSFER_GATE_RESTRICTED")
    timestamp = row.get("maturity_timestamp")
    at = now or datetime.now(UTC)
    maturity = _maturity_at(row.get("maturity"))
    matured = timestamp <= at.timestamp() if type(timestamp) is int and timestamp >= 0 else maturity is not None and maturity <= at
    if matured:
        reasons.append("MATURED")
    return "ROUTE", reasons


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _days_to(maturity: Any, now: datetime) -> int | None:
    try:
        at = datetime.fromisoformat(f"{maturity}T00:00:00+00:00")
    except (TypeError, ValueError):
        return None
    return (at - now).days


# Venues whose tokens redeem at maturity for exactly one unit of the asset they account in: a
# Pendle or Spectra principal token. A position (Exactly, Term Finance, Tenor) redeems for its own
# principal plus fixed interest, which belongs to the position, not to a unit.
ONE_FOR_ONE_PT_VENUES = frozenset({"Pendle", "Spectra"})
FIXED_INCOME_METHOD = "fenorium/registry-fixed-income-terms/v1"


def fixed_income_terms(row: Mapping[str, Any], *, chain_id: int | None, now: datetime) -> dict[str, Any]:
    """What kind of fixed-income instrument a row is, and the terms that follow from its structure.

    Structural only: maturity, what it redeems into, the cash flow that structure implies and the
    duration a zero-coupon instrument has by definition. Nothing here is computed from a price, and
    no yield-dependent measure is derived from a quoted APY -- engine.portfolio.fixed_income does
    that, and only from a verified schedule and a verified price.
    """
    category = str(row.get("category") or "").lower()
    address = _address(row.get("address"))
    underlying = _address(row.get("underlying_address"))
    underlying_id = asset_id(chain_id, underlying) if chain_id is not None and underlying else None
    kind = {"native": "ZERO_COUPON" if address else "TERM_POSITION", "vault": "OPEN_ENDED",
            "rwa": "FUND_SHARE"}.get(category, "NOT_FIXED_INCOME")
    terms: dict[str, Any] = {"method_version": FIXED_INCOME_METHOD, "instrument_type": kind, "maturity": None,
                             "maturity_timestamp": None,
                             "years_to_maturity": None, "matured": None, "redemption": None, "cashflows": [],
                             "quoted_yield_bps": None, "yield_kind": None, "macaulay_duration_years": None}
    rate, rate_kind = _int_or_none(row.get("rate_bps")), _text_or_none(row.get("rate_kind"))
    if rate is not None:
        terms.update(quoted_yield_bps=rate, yield_kind=rate_kind)
    maturity = _text_or_none(row.get("maturity"))
    if kind not in ("ZERO_COUPON", "TERM_POSITION") or maturity is None:
        return terms
    maturity_timestamp = _int_or_none(row.get("maturity_timestamp"))
    if maturity_timestamp is not None and maturity_timestamp < 0:
        maturity_timestamp = None
    if maturity_timestamp is not None and maturity_timestamp >= 0:
        try:
            at = datetime.fromtimestamp(maturity_timestamp, UTC)
        except (OverflowError, OSError, ValueError):
            maturity_timestamp = None
    if maturity_timestamp is None:
        try:
            at = datetime.fromisoformat(f"{maturity}T00:00:00+00:00")
        except ValueError:
            return terms
        if row.get("venue") in MIDNIGHT_VENUES:
            # Midnight venue rows may publish a calendar date without an hour. Keep them live
            # through that UTC day; Exactly's date-only terms retain their midnight maturity.
            at = at.replace(hour=23, minute=59, second=59)
    years = Decimal(max(0, int((at - now).total_seconds()))) / Decimal(365 * 86_400)
    years_text = format(years.quantize(Decimal("0.000001")), "f")
    terms.update(maturity=maturity, maturity_timestamp=maturity_timestamp,
                 years_to_maturity=years_text, matured=at <= now)
    if kind == "ZERO_COUPON":
        one_for_one = row.get("venue") in ONE_FOR_ONE_PT_VENUES
        accounting = _address(row.get("accounting_asset_address"))
        accounting_id = (asset_id(chain_id, None if accounting == "0x" + "00" * 20 else accounting)
                         if chain_id is not None and accounting is not None else None)
        terms["redemption"] = {
            "asset_id": accounting_id if one_for_one else underlying_id,
            "per_unit": "1" if one_for_one and accounting_id is not None else None,
            "basis": ("a principal token redeems at maturity for one unit of the asset it accounts in"
                      if one_for_one and accounting_id is not None else
                      "redeems one-for-one in the asset its SY accounts in; that asset is not established for this row"
                      if one_for_one else "this venue's redemption per unit is not stated"),
        }
        if one_for_one and accounting_id is not None:
            cashflow_at = at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            terms["cashflows"] = [{"at": cashflow_at, "asset_id": accounting_id, "per_unit": "1"}]
        # A zero-coupon instrument pays once, at maturity: its Macaulay duration is its remaining life.
        terms["macaulay_duration_years"] = years_text
    else:
        terms["redemption"] = {"asset_id": underlying_id, "per_unit": None,
                               "basis": "a position redeems for its own principal and fixed interest"}
    return terms


def universe_decimals(rows: Iterable[Any]) -> Callable[[int, str], int | None]:
    """The decimals of a token as the universe states them -- only when exactly one row names it,
    as `InstrumentDirectory` requires before a deposit is built."""
    found: dict[tuple[int, str], list[Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and type(row.get("chain_id")) is int and _address(row.get("address")):
            found.setdefault((row["chain_id"], _address(row["address"])), []).append(row.get("decimals"))

    def lookup(chain_id: int, address: str) -> int | None:
        values = found.get((chain_id, address.lower()), ())
        value = values[0] if len(values) == 1 else None
        return value if type(value) is int and value >= 0 else None

    return lookup


def instrument_record(row: Mapping[str, Any], chains: ChainRegistry, *, now: datetime | None = None,
                      token_decimals: Callable[[int, str], int | None] | None = None,
                      expired: bool = False) -> dict[str, Any]:
    """One universe row in the registry's vocabulary. `expired` is the universe's own age: past
    it, the purchase endpoints refuse everything, so nothing is called buyable."""
    now = now or datetime.now(UTC)
    chain_id = _evm_chain(row, chains)
    chain = chains.get(chain_id)
    address = _address(row.get("address"))
    underlying = _address(row.get("underlying_address"))
    path, reasons = purchase_refusals(row, chains, now=now, token_decimals=token_decimals)
    if expired:
        reasons.append("UNIVERSE_EXPIRED")
    gate = row.get("gate") if isinstance(row.get("gate"), Mapping) else {}
    obtainable = row.get("obtainable") if isinstance(row.get("obtainable"), Mapping) else None
    maturity = _text_or_none(row.get("maturity"))
    return {
        "schema_version": INSTRUMENT_SCHEMA,
        "instrument_id": instrument_id(row, chains),
        "asset_id": asset_id(chain_id, address) if chain_id is not None and address else None,
        "chain_id": chain_id,
        "chain_key": chain.key if chain else None,
        "vm": "evm" if chain else "unsupported",
        "address": address,
        "symbol": _text_or_none(row.get("symbol")),
        "name": _text_or_none(row.get("name")),
        "venue": _text_or_none(row.get("venue")),
        "category": _text_or_none(row.get("category")),
        "kind": "TOKEN" if address else "POSITION",
        "decimals": _int_or_none(row.get("decimals")),
        "underlying_asset_id": asset_id(chain_id, underlying) if chain_id is not None and underlying else None,
        "market_address": _address(row.get("market_address")),
        "rate_bps": _int_or_none(row.get("rate_bps")),
        "rate_kind": _text_or_none(row.get("rate_kind")),
        "depth_usd": _int_or_none(row.get("depth_usd")),
        "tvl_usd": _int_or_none(row.get("tvl_usd")),
        "verdict": _text_or_none(row.get("verdict")),
        "transfer_gate": _text_or_none(gate.get("status")),
        "quote": None if obtainable is None else {
            "measured": obtainable.get("measured") is True,
            "max_quoted_usd": _int_or_none(obtainable.get("max_quoted_usd")),
            "refusal": _text_or_none(obtainable.get("refusal")),
            "measured_at": _text_or_none(obtainable.get("measured_at")),
        },
        "fixed_income": None if maturity is None else {
            "maturity": maturity,
            "days_to_maturity": _days_to(maturity, now),
        },
        "terms": fixed_income_terms(row, chain_id=chain_id, now=now),
        "testnet": row.get("testnet") is True or bool(chain and chain.testnet),
        "purchase": {
            "path": path,
            "buyable": path is not None and not reasons,
            "reasons": sorted(set(reasons)),
        },
    }
