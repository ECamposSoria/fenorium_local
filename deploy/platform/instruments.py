"""Resolving an instrument against this deployment's own universe.

A purchase names what is being bought. If the caller also supplied the token address, the
chain and the decimals, then the caller decides what "this instrument" means, and a mis-stated
decimals field silently rescales the destination amount. So the request names an instrument by
chain and address, and everything else -- what it is, whether it can be transferred, what the
engine said about it -- is read here, from the file this deployment serves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any, Mapping

from engine.registry.instruments import SETTLEMENT_CATEGORY


FRESH_UNDER_SECONDS = 6 * 60 * 60
STALE_UNDER_SECONDS = 48 * 60 * 60

MAX_UNIVERSE_BYTES = 32 * 1024 * 1024


class InstrumentError(Exception):
    """An instrument could not be resolved, with the reason code the API will report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    """The part of a universe row a purchase depends on."""

    chain_id: int
    address: str
    decimals: int
    name: str
    venue: str
    category: str
    verdict: str | None
    reason: str | None
    transfer_gate: str | None
    rate_bps: int | None
    rate_kind: str | None
    # The universe row itself, for re-rating at a purchase's own size. Never serialised.
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)
    fenorium_score: int | None = None
    score_cap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "address": self.address,
            "decimals": self.decimals,
            "name": self.name,
            "venue": self.venue,
            "category": self.category,
            "verdict": self.verdict,
            "fenorium_score": self.fenorium_score,
            "score_cap": self.score_cap,
            "reason": self.reason,
            "transfer_gate": self.transfer_gate,
            "rate_bps": self.rate_bps,
            "rate_kind": self.rate_kind,
        }


@dataclass(frozen=True, slots=True)
class PositionRow:
    """A position in a venue's own contract: what a deposit needs, and what the engine said."""

    chain_id: int
    venue: str
    name: str
    verdict: str | None
    reason: str | None
    rate_bps: int | None
    rate_kind: str | None
    maturity_at: datetime
    underlying_address: str
    underlying_decimals: int
    underlying_symbol: str
    market_address: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "venue": self.venue,
            "name": self.name,
            "verdict": self.verdict,
            "reason": self.reason,
            "rate_bps": self.rate_bps,
            "rate_kind": self.rate_kind,
            "maturity": self.maturity_at.date().isoformat(),
            "maturity_timestamp": int(self.maturity_at.timestamp()),
            "underlying_address": self.underlying_address,
            "underlying_decimals": self.underlying_decimals,
            "underlying_symbol": self.underlying_symbol,
            "market_address": self.market_address,
        }


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int else None


def _row(raw: Mapping[str, Any], *, chain_id: int, address: str) -> InstrumentRow:
    decimals = raw.get("decimals")
    if type(decimals) is not int or decimals < 0:
        # Never defaulted to 18. The destination amount is scaled by this, so a guess here is a
        # guess about how much of the instrument the buyer receives.
        raise InstrumentError(
            "INSTRUMENT_DECIMALS_UNKNOWN",
            "the universe row for this instrument does not state its decimals",
        )
    if raw.get("category") == SETTLEMENT_CATEGORY:
        # Cash is in the universe so a wallet's holdings can be read and priced. It is what a
        # buyer pays with, never what they are sold.
        raise InstrumentError(
            "NOT_AN_INSTRUMENT",
            "this is a settlement asset, registered to be read and paid with, not offered",
        )
    if raw.get("testnet") is True:
        raise InstrumentError(
            "INSTRUMENT_IS_TESTNET",
            "this instrument is a testnet mock and is not purchasable",
        )
    gate = raw.get("gate")
    verdict = _text_or_none(raw.get("verdict"))
    return InstrumentRow(
        chain_id=chain_id,
        address=address,
        decimals=decimals,
        name=_text_or_none(raw.get("name")) or "",
        venue=_text_or_none(raw.get("venue")) or "",
        category=_text_or_none(raw.get("category")) or "",
        verdict=verdict,
        fenorium_score=(None if verdict == "UNRATED" else _int_or_none(raw.get("fenorium_score"))),
        score_cap=(None if verdict == "UNRATED" else _text_or_none(raw.get("score_cap"))),
        reason=_text_or_none(raw.get("reason")),
        transfer_gate=_text_or_none(gate.get("status")) if isinstance(gate, Mapping) else None,
        rate_bps=_int_or_none(raw.get("rate_bps")),
        rate_kind=_text_or_none(raw.get("rate_kind")),
        raw=dict(raw),
    )


class InstrumentDirectory:
    """Reads the served universe file and answers one question: which row is this."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._generated_at: datetime | None = None
        self._stamp: tuple[int, int] | None = None
        self._index: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
        self._positions: dict[tuple[int, str, str], list[Mapping[str, Any]]] = {}

    def _load(self) -> dict[tuple[int, str], list[Mapping[str, Any]]]:
        with self._lock:
            try:
                stat = self.path.stat()
            except OSError as exc:
                raise InstrumentError(
                    "UNIVERSE_UNAVAILABLE", "the configured universe file could not be read"
                ) from exc
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return self._index
            if stat.st_size > MAX_UNIVERSE_BYTES:
                raise InstrumentError(
                    "UNIVERSE_UNAVAILABLE", "the configured universe file is larger than expected"
                )
            try:
                document = json.loads(self.path.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InstrumentError(
                    "UNIVERSE_UNAVAILABLE", "the configured universe file is not valid UTF-8 JSON"
                ) from exc
            rows = document.get("instruments") if isinstance(document, Mapping) else None
            if not isinstance(rows, list):
                raise InstrumentError(
                    "UNIVERSE_UNAVAILABLE", "the configured universe file has no instruments list"
                )
            index: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
            positions: dict[tuple[int, str, str], list[Mapping[str, Any]]] = {}
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                chain_id = row.get("chain_id")
                address = row.get("address")
                if type(chain_id) is not int:
                    continue
                if isinstance(address, str) and address:
                    index.setdefault((chain_id, address.lower()), []).append(row)
                elif isinstance(row.get("venue"), str) and isinstance(row.get("name"), str):
                    # A position has no address; it is named by venue and instrument instead.
                    positions.setdefault((chain_id, row["venue"], row["name"]), []).append(row)
            try:
                generated_at = datetime.fromisoformat(document.get("generated_at"))
                if generated_at.utcoffset() is None:
                    generated_at = None
            except (TypeError, ValueError):
                generated_at = None
            self._generated_at = generated_at
            self._index = index
            self._positions = positions
            self._stamp = stamp
            return index

    def decimals(self, chain_id: int, address: str) -> int | None:
        """Return trusted precision when the universe has one unambiguous token row."""
        try:
            matches = self._load().get((chain_id, address.lower()), ())
        except InstrumentError:
            return None
        if len(matches) != 1:
            return None
        decimals = matches[0].get("decimals")
        return decimals if type(decimals) is int and decimals >= 0 else None

    def symbol(self, chain_id: int, address: str) -> str | None:
        """Return the symbol of one unambiguous token row, as `decimals` does for precision."""
        try:
            matches = self._load().get((chain_id, address.lower()), ())
        except InstrumentError:
            return None
        if len(matches) != 1:
            return None
        symbol = matches[0].get("symbol")
        return symbol if isinstance(symbol, str) and symbol.strip() else None

    def principal_token(self, chain_id: int, address: str) -> dict[str, Any] | None:
        """Resolve a held PT without applying the purchase maturity or freshness gates."""
        with self._lock:
            rows = self._load().get((chain_id, address.lower()), ())
            if len(rows) != 1:
                return None
            row = rows[0]
            if (row.get("venue") not in {"Pendle", "Spectra"}
                    or type(row.get("decimals")) is not int or not 0 <= row["decimals"] <= 255
                    or type(row.get("maturity_timestamp")) is not int or row["maturity_timestamp"] < 0):
                return None
            return dict(row)

    def exit_market(self, chain_id: int, venue: str, identity: str,
                    maturity: int | None = None) -> dict[str, Any] | None:
        """Resolve held positions, including maturities no longer offered for deposits."""
        with self._lock:
            self._load()
            key = "market_address" if venue == "Exactly" else "market_id"
            rows = [row for group in (*self._positions.values(), *self._index.values()) for row in group
                    if row.get("chain_id") == chain_id and row.get("venue") == venue
                    and str(row.get(key, "")).lower() == identity.lower()]
            if not rows:
                return None
            exact = []
            for row in rows:
                at = row.get("maturity_timestamp", row.get("maturity"))
                if isinstance(at, str):
                    try:
                        at = int(datetime.fromisoformat(at.replace("Z", "+00:00")).replace(
                            tzinfo=UTC).timestamp())
                    except ValueError:
                        at = None
                if at == maturity:
                    exact.append(row)
            result = dict((exact or rows)[0])
            if venue == "Exactly":
                underlying = result.get("underlying_address")
                tokens = self._index.get((chain_id, str(underlying).lower()), ())
                if len(tokens) != 1:
                    raise InstrumentError("INSTRUMENT_DECIMALS_UNKNOWN", "exit asset is ambiguous or absent")
                result["loan_token"] = {"address": underlying, "decimals": tokens[0].get("decimals"),
                                        "symbol": tokens[0].get("symbol", "")}
                if not exact:
                    result["rate_bps"] = None
            return result

    def _require_fresh(self, now: datetime) -> None:
        # Rechecked on every call, cache hits included: a cached snapshot keeps ageing.
        if (self._generated_at is None
                or (now - self._generated_at).total_seconds() >= STALE_UNDER_SECONDS):
            raise InstrumentError(
                "UNIVERSE_EXPIRED", "the universe snapshot has expired or its age is unknown",
            )

    def resolve(self, chain_id: int, address: str, *, now: datetime) -> InstrumentRow:
        key = (chain_id, address.lower())
        with self._lock:
            index = self._load()
            self._require_fresh(now)
            matches = index.get(key, ())
        if not matches:
            raise InstrumentError(
                "INSTRUMENT_NOT_IN_UNIVERSE",
                "no instrument with this chain and address is in this deployment's universe",
            )
        if len(matches) > 1:
            # Two rows for one token is a universe defect. Picking one of them would decide,
            # silently, which instrument the buyer is being sold.
            raise InstrumentError(
                "INSTRUMENT_AMBIGUOUS",
                "more than one universe row claims this chain and address",
            )
        return _row(matches[0], chain_id=chain_id, address=address.lower())

    def resolve_position(self, chain_id: int, venue: str, name: str, *, now: datetime) -> PositionRow:
        """The position a deposit goes into, and what it is deposited in, from this universe."""
        with self._lock:
            self._load()
            self._require_fresh(now)
            matches = self._positions.get((chain_id, venue, name), ())
            if not matches:
                raise InstrumentError(
                    "INSTRUMENT_NOT_IN_UNIVERSE",
                    "no position with this chain, venue and name is in this deployment's universe",
                )
            if len(matches) > 1:
                raise InstrumentError("INSTRUMENT_AMBIGUOUS", "more than one universe row claims this position")
            raw = matches[0]
            underlying, market = raw.get("underlying_address"), raw.get("market_address")
            if not (isinstance(underlying, str) and _ADDRESS.fullmatch(underlying)
                    and isinstance(market, str) and _ADDRESS.fullmatch(market)):
                # Never inferred from the instrument's symbol: "USDC" on Optimism is two markets,
                # native and bridged, and they were once transposed in this very codebase.
                raise InstrumentError(
                    "POSITION_CONTRACT_UNKNOWN",
                    "the universe row does not name this position's market and deposit asset",
                )
            underlying_rows = self._index.get((chain_id, underlying.lower()), ())
            decimals = underlying_rows[0].get("decimals") if len(underlying_rows) == 1 else None
            if type(decimals) is not int or decimals < 0:
                raise InstrumentError(
                    "INSTRUMENT_DECIMALS_UNKNOWN",
                    "the deposit asset's decimals are not stated in this universe",
                )
            maturity = raw.get("maturity")
            try:
                maturity_at = datetime.fromisoformat(f"{maturity}T00:00:00+00:00")
            except (TypeError, ValueError):
                raise InstrumentError("MATURITY_UNKNOWN", "the position's maturity is not a date") from None
            return PositionRow(
                chain_id=chain_id,
                venue=venue,
                name=name,
                verdict=_text_or_none(raw.get("verdict")),
                reason=_text_or_none(raw.get("reason")),
                rate_bps=_int_or_none(raw.get("rate_bps")),
                rate_kind=_text_or_none(raw.get("rate_kind")),
                maturity_at=maturity_at,
                underlying_address=underlying.lower(),
                underlying_decimals=decimals,
                underlying_symbol=_text_or_none(underlying_rows[0].get("symbol")) or "",
                market_address=market.lower(),
            )

    def resolve_position_row(
        self, chain_id: int, venue: str, name: str, *, now: datetime
    ) -> dict[str, Any]:
        """Return the unique raw universe row for a position, after the same freshness check."""
        with self._lock:
            self._load()
            self._require_fresh(now)
            matches = self._positions.get((chain_id, venue, name), ())
            if not matches:
                raise InstrumentError(
                    "INSTRUMENT_NOT_IN_UNIVERSE",
                    "no position with this chain, venue and name is in this deployment's universe",
                )
            if len(matches) > 1:
                raise InstrumentError(
                    "INSTRUMENT_AMBIGUOUS", "more than one universe row claims this position"
                )
            return dict(matches[0])
