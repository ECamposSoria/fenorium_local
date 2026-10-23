"""Pinned-block, unsigned exits for Exactly deposits and Midnight lender credit."""

from __future__ import annotations

import http.client
import json
import re
import time
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Mapping

from engine.chain import selector
from engine.midnight import (ADDRESSES, MIDNIGHT_VENUES, MARKET_ABI, OFFER_ABI, MAX_TICK, WAD, ZERO_ADDRESS,
                             tick_to_price,
                             Market, Offer, CollateralParams, abi_encode)
from engine.onchain import EXACTLY_FIXED_DEPOSIT, OnchainError
from .instruments import InstrumentError
from .midnight_orders import (CALLBACK_SUCCESS, OrderError, _json_offer, _open_no_redirect, accrue_position,
                              market_values, offer_values, read_position, to_market_id)
from .ratelimit import ProviderBudget, Throttled
from .gas_check import apply_gas_check

# A Midnight bid must outlive the time to sign and land the sale: the route previews' two minutes.
EXIT_SIGNING_WINDOW_SECONDS = 120

EXACTLY_WITHDRAW = selector("withdrawAtMaturity(uint256,uint256,uint256,address,address)")
TENOR_WITHDRAW = selector(f"withdraw({MARKET_ABI},uint256,address,address)")
TENOR_TAKE = selector(f"take({OFFER_ABI},bytes,uint256,address,address,address,bytes)")
PT_ROUTER = "0x888888888889758f76e7103c6cbf23abbf58f946"
PENDLE_REDEEM = selector("redeemPyToToken(address,address,uint256,(address,uint256,address,address,(uint8,address,bytes,bool)))")
SPECTRA_REDEEM = selector("redeem(uint256,address,address,uint256)")
# Pendle's exchange rate may move after the quote and before inclusion.
PT_REDEEM_DRIFT_BPS = 10
RATIFY = selector(f"isRatified({OFFER_ABI},bytes,address)")
# The signed Exactly floor allows 20 bp of drift between simulation and inclusion.
EXACTLY_DRIFT_BPS = 20
YEAR = 365 * 86400
_SETTLEMENT_FEE_CAPS = ((86400, 14_000_000_000_000), (7 * 86400, 98_000_000_000_000),
                        (30 * 86400, 417_000_000_000_000), (90 * 86400, 1_250_000_000_000_000),
                        (180 * 86400, 2_500_000_000_000_000), (360 * 86400, 5_000_000_000_000_000))
MAX_RESPONSE_BYTES = 512_000
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_REVERTS = {selector(name + "()"): code for name, code in (
    ("InsufficientProtocolLiquidity", "INSUFFICIENT_PROTOCOL_LIQUIDITY"),
    ("Disagreement", "DISAGREEMENT"), ("SelfTake", "SELF_TAKE"),
    ("OfferExpired", "OFFER_EXPIRED"), ("OfferNotStarted", "OFFER_NOT_STARTED"),
    ("ConsumedAssets", "CONSUMED_ASSETS"), ("ConsumedUnits", "CONSUMED_UNITS"),
    ("RatifierFailed", "RATIFIER_FAILED"))}


class ExitError(ValueError):
    def __init__(self, code: str, status: str = "REJECTED") -> None:
        super().__init__(code)
        self.code, self.status = code, status


class MorphoExits:
    """Fixed-host, bounded public market/book reads sharing the request deadline."""

    def __init__(self, *, opener=_open_no_redirect, clock=time.monotonic, budget=None):
        self.opener, self.clock = opener, clock
        self.budget = budget if budget is not None else ProviderBudget("Morpho exits", per_minute=30, per_day=5000)

    def fetch(self, market_id: str, *, bids: bool = False, deadline_at: float) -> Any:
        if not isinstance(market_id, str) or not _HASH.fullmatch(market_id):
            raise ExitError("MARKET_KEY_MISMATCH")
        path = f"books/{market_id}/bids/takeable-offers" if bids else f"markets/{market_id}"
        url = "https://api.morpho.org/v0/midnight/" + path
        try:
            self.budget.reserve()
            remaining = min(8, deadline_at - self.clock())
            if remaining <= 0:
                raise ValueError("deadline")
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with self.opener(request, timeout=remaining) as response:
                if response.geturl() != url or not 200 <= response.status < 300:
                    raise ValueError("provider response")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES or self.clock() >= deadline_at:
                raise ValueError("response limit")
            return json.loads(raw)
        except (Throttled, OSError, urllib.error.URLError, http.client.HTTPException,
                ValueError, TypeError, RecursionError) as exc:
            raise ExitError("PROVIDER_UNAVAILABLE", "EXIT_UNAVAILABLE") from exc


class MorphoPublicBook:
    """Cached, fixed-host check for whether a Tenor market appears in Morpho's public book."""

    def __init__(self, *, opener=_open_no_redirect, clock=time.monotonic, budget=None):
        self.opener, self.clock = opener, clock
        self.budget = budget if budget is not None else ProviderBudget(
            "Morpho public books", per_minute=30, per_day=5000)
        self._cache: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def is_public(self, market_id: str, *, deadline_at: float) -> bool:
        if not isinstance(market_id, str) or not _HASH.fullmatch(market_id):
            raise ExitError("PROVIDER_UNAVAILABLE", "ROUTE_UNAVAILABLE")
        now = self.clock()
        if deadline_at - now <= 0:
            raise ExitError("PROVIDER_UNAVAILABLE", "ROUTE_UNAVAILABLE")
        with self._lock:
            cached = self._cache.get(market_id)
            if cached is not None and cached[0] > now:
                return cached[1]
        url = f"https://api.morpho.org/v0/midnight/books/{market_id}"
        try:
            self.budget.reserve()
            remaining = min(8, deadline_at - self.clock())
            if remaining <= 0:
                raise ValueError("deadline")
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            try:
                response = self.opener(request, timeout=remaining)
            except urllib.error.HTTPError as exc:
                try:
                    if exc.geturl() != url:
                        raise ValueError("redirect")
                    if exc.code == 404:
                        answer = False
                    else:
                        raise ValueError("provider response") from exc
                finally:
                    exc.close()
            else:
                with response:
                    if response.geturl() != url:
                        raise ValueError("redirect")
                    if response.status == 404:
                        answer = False
                    elif response.status == 200:
                        raw = response.read(MAX_RESPONSE_BYTES + 1)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ValueError("response limit")
                        answer = True
                    else:
                        raise ValueError("provider response")
            if self.clock() >= deadline_at:
                raise ValueError("deadline")
        except (Throttled, OSError, urllib.error.URLError, http.client.HTTPException,
                ValueError, TypeError, RecursionError) as exc:
            raise ExitError("PROVIDER_UNAVAILABLE", "ROUTE_UNAVAILABLE") from exc
        with self._lock:
            self._cache[market_id] = (self.clock() + 600, answer)
        return answer


def _uint(value: Any, bits: int = 256) -> int:
    # Morpho sends small fields (chain id, maturity, tick, start, expiry) as JSON integers and amounts as
    # decimal strings.
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,78}", value):
        result = int(value)
    else:
        raise ExitError("INVALID_PROVIDER_DATA")
    if result < 0:
        raise ExitError("INVALID_PROVIDER_DATA")
    if result >= 1 << bits:
        raise ExitError("INVALID_PROVIDER_DATA")
    return result


def _address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ExitError("INVALID_PROVIDER_DATA")
    return value.lower()


def _bytes(value: Any, size: int | None = None) -> bytes:
    if (not isinstance(value, str) or len(value) > 131074
            or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value)):
        raise ExitError("INVALID_PROVIDER_DATA")
    result = bytes.fromhex(value[2:])
    if size is not None and len(result) != size:
        raise ExitError("INVALID_PROVIDER_DATA")
    return result


def parse_market(raw: Any, chain_id: int, *, offer: bool = False) -> Market:
    if not isinstance(raw, Mapping) or chain_id not in ADDRESSES:
        raise ExitError("INVALID_PROVIDER_DATA")
    try:
        collaterals = raw["collaterals"]
        if not isinstance(collaterals, list) or len(collaterals) > 256:
            raise ExitError("INVALID_PROVIDER_DATA")
        market = Market(_uint(raw["chain_id"]), ADDRESSES[chain_id].midnight,
                        _address(raw["loan_token"]), tuple(
                            CollateralParams(_address(c["token"]), _uint(c["lltv"]),
                                             _uint(c["liquidation_cursor"]), _address(c["oracle"]))
                            for c in collaterals), _uint(raw["maturity"]),
                        _uint(raw["rcf_threshold"]), _address(raw["enter_gate"]), _address(raw["liquidator_gate"]))
        if market.chainId != chain_id or (offer and _address(raw["midnight"]) != market.midnight.lower()):
            raise ExitError("MARKET_KEY_MISMATCH")
        return market
    except (KeyError, TypeError) as exc:
        raise ExitError("INVALID_PROVIDER_DATA") from exc


def parse_bid(raw: Any, chain_id: int) -> tuple[Offer, bytes, int, int]:
    try:
        value = raw["offer"]
        if type(value["buy"]) is not bool or type(value["reduce_only"]) is not bool:
            raise ExitError("INVALID_PROVIDER_DATA")
        group = value["group"]
        group = _bytes(group, 32) if isinstance(group, str) and group.startswith("0x") else _uint(group).to_bytes(32, "big")
        offer = Offer(parse_market(value["market"], chain_id, offer=True), value["buy"],
                      _address(value["maker"]), _uint(value["start"]), _uint(value["expiry"]),
                      _uint(value["tick"]), group, _address(value["callback"]), _bytes(value["callback_data"]),
                      _address(value["receiver_if_maker_is_seller"]), _address(value["ratifier"]),
                      value["reduce_only"], _uint(value["max_units"], 128), _uint(value["max_assets"], 128),
                      _uint(value["continuous_fee_cap"]))
        if offer.tick > MAX_TICK or not isinstance(raw["market_id"], str) or not _HASH.fullmatch(raw["market_id"]):
            raise ExitError("INVALID_PROVIDER_DATA")
        warning = raw.get("warning")
        if warning is not None and (not isinstance(warning, dict) or not isinstance(warning.get("code"), str)):
            raise ExitError("INVALID_PROVIDER_DATA")
        return offer, _bytes(raw["ratifier_data"]), _uint(raw["units"], 128), _uint(raw["valid_until"])
    except (KeyError, TypeError) as exc:
        raise ExitError("INVALID_PROVIDER_DATA") from exc


def calldata(prefix: str, types: tuple, values: tuple) -> str:
    return prefix + abi_encode(types, values).hex()


def _words(raw: Any, count: int) -> tuple[int, ...]:
    if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{%d}" % (64 * count), raw):
        raise OnchainError("exit read returned invalid data")
    return tuple(int(raw[2 + i * 64:2 + (i + 1) * 64], 16) for i in range(count))


def _ceil(n: int, d: int) -> int:
    return (n + d - 1) // d


def _midnight_settlement_fee_upper_cap(time_to_maturity: int) -> int:
    """Return the highest fee allowed anywhere in the current maturity bracket."""
    for boundary, cap in _SETTLEMENT_FEE_CAPS:
        if time_to_maturity <= boundary:
            return cap
    return _SETTLEMENT_FEE_CAPS[-1][1]


def _midnight_sale_floor(units: int, tick: int, time_to_maturity: int, simulated_payout: int) -> int:
    """Bound sale proceeds against the highest settlement fee still allowed for this maturity."""
    fee_cap = _midnight_settlement_fee_upper_cap(time_to_maturity)
    price = max(0, tick_to_price(tick) - fee_cap)
    return min(simulated_payout, units * price // WAD)


def _mark(face: int, row: dict, remaining: int) -> tuple[int, str]:
    rate = row.get("rate_bps")
    if row.get("rate_kind") == "executable" and type(rate) is int and rate >= 0:
        return (face * 10000 * YEAR // (10000 * YEAR + rate * max(0, remaining)),
                "face value discounted at the venue's executable rate")
    return face, "face value; no executable rate is available"


def _build_pt_redeem(api: Any, operation: Mapping[str, Any], result: dict[str, Any],
                     evidence: dict[str, Any], deadline: float, check_deadline: Any,
                     tx: Any) -> dict[str, Any]:
    """Build a whole-balance redemption against one pinned head block."""
    from .api import (_APPROVE_SELECTOR, _word, MAX_ACCEPTED_SALE_COST_BPS,
                      MAX_ROUTE_COST_BPS)

    wallet, chain_id = operation["wallet"].lower(), operation["chain_id"]
    address = operation["instrument_address"].lower()
    try:
        row = None
        if api.instruments is not None:
            try:
                row = api.instruments.principal_token(chain_id, address)
            except InstrumentError:
                pass
        remembered = None
        store = api.principal_token_store if api.principal_token_store is not None else api.orders
        if store is not None:
            remembered = store.principal_token(chain_id, address)
        instrument = remembered or row
        if instrument is None:
            raise ExitError("UNKNOWN_INSTRUMENT")
        if row is not None and remembered is not None:
            instrument = {**row, **remembered}
        venue = instrument.get("venue")
        if venue not in {"Pendle", "Spectra"}:
            raise ExitError("UNKNOWN_INSTRUMENT")
        maturity = instrument.get("maturity_timestamp")
        if (type(maturity) is not int or maturity < 0 or type(instrument.get("decimals")) is not int
                or not 0 <= instrument["decimals"] <= 255):
            raise ExitError("UNKNOWN_INSTRUMENT")
        result["position"] = {"venue": venue, "address": address, "maturity": maturity,
                               "maturity_timestamp": maturity}
        check_deadline()
        reader = api._order_reader(chain_id, deadline)
        block = reader.head_block()
        result["block"] = {"number": block.number, "hash": block.hash}
        evidence["block"] = block.to_dict()
        check_deadline()

        def read_raw(to: str, signature: str, types: tuple = (), values: tuple = ()):
            check_deadline()
            data = calldata(selector(signature), types, values)
            raw = reader.eth_call(to, data, block)
            evidence["reads"].append({"to": to.lower(), "data": data, "result": raw})
            check_deadline()
            return raw

        def read(to: str, signature: str, types: tuple = (), values: tuple = ()):
            return _words(read_raw(to, signature, types, values), 1)

        def pt_simulate(transaction: dict, count: int) -> tuple[int, ...]:
            check_deadline()
            observation = {"transaction": transaction}
            evidence["simulations"].append(observation)
            call = {key: transaction[key] for key in ("from", "to", "data")}
            call["value"] = "0x0"
            try:
                raw = reader.rpc("eth_call", [call, {"blockHash": block.hash}], require_result=True)
                observation["result"] = raw
                check_deadline()
                return _words(raw, count)
            except OnchainError as exc:
                observation["revert_data"] = exc.revert_data
                if exc.revert_data is not None:
                    raise ExitError("SIMULATION_REVERTED") from exc
                raise

        balance, = read(address, "balanceOf(address)", ("address",), (wallet,))
        if balance == 0:
            raise ExitError("NO_POSITION")
        result["units_raw_amount"] = str(balance)
        if venue == "Pendle":
            expired, = read(address, "isExpired()")
            if expired != 1:
                raise ExitError("NOT_MATURED")
            yt_word, = read(address, "YT()")
            sy_word, = read(address, "SY()")
            if yt_word >> 160 or sy_word >> 160 or not yt_word or not sy_word:
                raise ExitError("INCONSISTENT_INSTRUMENT")
            yt, sy = "0x" + yt_word.to_bytes(32, "big")[-20:].hex(), "0x" + sy_word.to_bytes(32, "big")[-20:].hex()
            yt_pt, = read(yt, "PT()")
            yt_sy, = read(yt, "SY()")
            if yt_pt >> 160 or yt_sy >> 160 or yt_pt != int(address, 16) or yt_sy != int(sy, 16):
                raise ExitError("INCONSISTENT_INSTRUMENT")
            rate, = read(sy, "exchangeRate()")
            stored, = read(yt, "pyIndexStored()")
            index = max(rate, stored)
            if not index:
                raise ExitError("NO_REDEMPTION_OUTPUT")
            sy_amount = balance * 10**18 // index
            # Dynamic address[] returned as offset, length, then one word per address.
            raw = read_raw(sy, "getTokensOut()")
            if not isinstance(raw, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})+", raw):
                raise OnchainError("invalid getTokensOut response")
            encoded = bytes.fromhex(raw[2:])
            offset = int.from_bytes(encoded[:32], "big")
            if offset != 32 or offset + 32 > len(encoded):
                raise OnchainError("invalid getTokensOut response")
            count = int.from_bytes(encoded[offset:offset + 32], "big")
            if count > 256 or offset + 32 + count * 32 != len(encoded):
                raise OnchainError("invalid getTokensOut response")
            token_words = [int.from_bytes(encoded[offset + 32 + i * 32:offset + 64 + i * 32], "big")
                           for i in range(count)]
            if any(word >> 160 for word in token_words):
                raise OnchainError("invalid getTokensOut address")
            tokens = ["0x" + word.to_bytes(32, "big")[-20:].hex() for word in token_words]
            preferred = [instrument.get("underlying_address"), instrument.get("accounting_asset_address")]
            token_out = next((str(item).lower() for item in preferred if isinstance(item, str)
                              and _ADDRESS.fullmatch(str(item)) and str(item).lower() in tokens
                              and str(item).lower() != ZERO_ADDRESS), None)
            if token_out is None:
                token_out = next((item for item in tokens if item != ZERO_ADDRESS), None)
            if token_out is None:
                raise ExitError("NO_REDEMPTION_OUTPUT")
            decimals, = read(token_out, "decimals()")
            symbol = api.instruments.symbol(chain_id, token_out) if api.instruments is not None else None
            if not symbol:
                try:
                    symbol = _decode_symbol(read_raw(token_out, "symbol()"))
                except (OnchainError, ExitError, ValueError, TypeError):
                    symbol = None
                check_deadline()
            if not isinstance(symbol, str) or not symbol:
                raise ExitError("RECEIVE_ASSET_UNKNOWN")
            preview, = read(sy, "previewRedeem(address,uint256)", ("address", "uint256"), (token_out, sy_amount))
            allowance, = read(address, "allowance(address,address)", ("address", "address"), (wallet, PT_ROUTER))
            check_deadline()
            code = reader.rpc("eth_getCode", [PT_ROUTER, {"blockHash": block.hash}], require_result=True)
            check_deadline()
            if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", code) or int(code[2:] or "0", 16) == 0:
                raise ExitError("ROUTER_UNAVAILABLE", "EXIT_UNAVAILABLE")
            minimum = preview - _ceil(preview * PT_REDEEM_DRIFT_BPS, 10000)
            # minTokenOut sits in the static tuple; the nested swapData bytes are empty.
            redeem_data = calldata(PENDLE_REDEEM,
                ("address", "address", "uint256", "(address,uint256,address,address,(uint8,address,bytes,bool))"),
                (wallet, yt, balance, (token_out, minimum, token_out, ZERO_ADDRESS, (0, ZERO_ADDRESS, b"", False))))
            redeem_tx = tx(PT_ROUTER, redeem_data)
            simulated = allowance >= balance
            proceeds = preview
            if simulated:
                proceeds, _intermediate_sy = pt_simulate(redeem_tx, 2)
                if proceeds < minimum:
                    raise ExitError("SIMULATION_REVERTED")
            result["simulated"] = simulated
            approval = {"chainId": chain_id, "from": wallet, "to": address,
                        "data": _APPROVE_SELECTOR + _word(PT_ROUTER) + f"{balance:064x}", "value": "0",
                        "spender": PT_ROUTER, "amount": str(balance)}
            reset = {**approval, "data": _APPROVE_SELECTOR + _word(PT_ROUTER) + _word(0), "amount": "0"}
            result.update(unsigned_approval_request=approval, unsigned_approval_reset_request=reset)
            result["action"] = "PENDLE_REDEEM"
        else:
            maturity_value, = read(address, "maturity()")
            underlying_word, = read(address, "underlying()")
            if underlying_word >> 160 or underlying_word == 0:
                raise ExitError("INCONSISTENT_INSTRUMENT")
            maturity = maturity_value
            result["position"]["maturity"] = maturity
            result["position"]["maturity_timestamp"] = maturity
            receive_address = "0x" + underlying_word.to_bytes(32, "big")[-20:].hex()
            expected_underlying = instrument.get("underlying_address")
            if expected_underlying and str(expected_underlying).lower() != receive_address:
                raise ExitError("INCONSISTENT_INSTRUMENT")
            if block.timestamp < maturity_value:
                raise ExitError("NOT_MATURED")
            preview, = read(address, "previewRedeem(uint256)", ("uint256",), (balance,))
            minimum = preview - _ceil(preview * PT_REDEEM_DRIFT_BPS, 10000)
            redeem_data = calldata(SPECTRA_REDEEM, ("uint256", "address", "address", "uint256"),
                                   (balance, wallet, wallet, minimum))
            redeem_tx = tx(address, redeem_data)
            proceeds, = pt_simulate(redeem_tx, 1)
            if proceeds < minimum:
                raise ExitError("SIMULATION_REVERTED")
            decimals, = read(receive_address, "decimals()")
            symbol = api.instruments.symbol(chain_id, receive_address) if api.instruments is not None else None
            if not symbol:
                try:
                    symbol = _decode_symbol(read_raw(receive_address, "symbol()"))
                except (OnchainError, ExitError, ValueError, TypeError):
                    symbol = None
                check_deadline()
            if decimals is None or not symbol:
                raise ExitError("RECEIVE_ASSET_UNKNOWN")
            result["action"] = "SPECTRA_REDEEM"
        if proceeds <= 0:
            # A matured dust balance can redeem for nothing; a zero-output transaction is not an exit.
            raise ExitError("NO_REDEMPTION_OUTPUT")
        if type(decimals) is not int or not 0 <= decimals <= 255:
            raise ExitError("RECEIVE_ASSET_UNKNOWN")
        result["receive"] = {"address": receive_address if venue == "Spectra" else token_out,
                              "decimals": decimals, "symbol": symbol}
        guaranteed_cost = (_ceil((proceeds - minimum) * 10000, proceeds)
                           if minimum < proceeds else 0)
        if guaranteed_cost > MAX_ACCEPTED_SALE_COST_BPS:
            raise ExitError("EXIT_COST_TOO_HIGH")
        result.update(proceeds_raw_amount=str(proceeds), min_proceeds_raw_amount=str(minimum),
                      face_raw_amount=str(proceeds), mark_raw_amount=str(proceeds),
                      mark_basis="venue redemption at par", route_cost_bps=guaranteed_cost,
                      unsigned_transaction_request=redeem_tx)
        if guaranteed_cost > MAX_ROUTE_COST_BPS and operation.get("accept_cost_bps", -1) < guaranteed_cost:
            result["cost_acceptance"] = {"route_cost_bps": guaranteed_cost,
                                         "max_acceptable_bps": MAX_ACCEPTED_SALE_COST_BPS,
                                         "mark_raw_amount": str(proceeds),
                                         "proceeds_raw_amount": str(proceeds)}
            raise ExitError("SALE_COST_NEEDS_ACCEPTANCE", "ANALYTICAL_ONLY")
        if guaranteed_cost > MAX_ROUTE_COST_BPS:
            result["cost_accepted_bps"] = operation["accept_cost_bps"]
        result["reason_codes"] = list(api.gate.reason_codes_for(cross_chain=False))
        result["status"] = "EXIT_READY"
        check_deadline()
        if api.gate.is_open_for(cross_chain=False):
            result["transaction_request"] = redeem_tx
            if venue == "Pendle":
                result["approval_request"] = result["unsigned_approval_request"]
                result["approval_reset_request"] = result["unsigned_approval_reset_request"]
            result["broadcast_enabled"] = True
        apply_gas_check(result, reader, chain_id=chain_id, wallet=wallet, block=block,
                        default_gas=250_000 if venue == "Pendle" else 700_000 if venue == "Spectra" else None)
    except ExitError as exc:
        result.update(status=exc.status, reason_codes=[exc.code])
    except OnchainError as exc:
        evidence["revert_data"] = exc.revert_data
        result.update(status="REJECTED" if exc.revert_data is not None else "EXIT_UNAVAILABLE",
                      reason_codes=["SIMULATION_REVERTED" if exc.revert_data is not None else "ONCHAIN_UNAVAILABLE"])
    except (InstrumentError, ValueError, KeyError, TypeError, OSError):
        result.update(status="EXIT_UNAVAILABLE", reason_codes=["ONCHAIN_UNAVAILABLE"])
    result["reason_codes"] = sorted(set(result["reason_codes"]))
    evidence.update(response=result, action=result["action"],
                    unsigned_transaction_request=result["unsigned_transaction_request"])
    api._persist_observation(evidence, suffix="position-exit")
    return result


def _decode_symbol(raw: Any) -> str | None:
    if not isinstance(raw, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})+", raw):
        return None
    data = bytes.fromhex(raw[2:])
    try:
        offset = int.from_bytes(data[:32], "big")
        if offset != 32 or offset + 32 > len(data):
            return None
        length = int.from_bytes(data[offset:offset + 32], "big")
        if length > 32 or offset + 32 + length > len(data):
            return None
        value = data[offset + 32:offset + 32 + length]
        text = value.decode("ascii")
        return text if 1 <= len(text) <= 32 and text.strip() and text.isprintable() else None
    except (UnicodeDecodeError, ValueError):
        return None


def build_exit(api: Any, operation: Mapping[str, Any]) -> dict[str, Any]:
    from .api import LENDING_RESPONSE_SECONDS, MAX_ROUTE_COST_BPS, MAX_ACCEPTED_SALE_COST_BPS

    wallet, chain_id, kind = operation["wallet"].lower(), operation["chain_id"], operation["kind"]
    result = dict(status="EXIT_UNAVAILABLE", reason_codes=[], kind=kind, action=None, wallet=wallet,
                  chain_id=chain_id, position=None, block=None, receive=None,
                  proceeds_raw_amount=None, min_proceeds_raw_amount=None, face_raw_amount=None,
                  mark_raw_amount=None, mark_basis=None, route_cost_bps=None,
                  unsigned_transaction_request=None, transaction_request=None, broadcast_enabled=False,
                  gas_check=None, expires_at=None)
    if kind == "PT_REDEEM":
        result.update(units_raw_amount=None, unsigned_approval_request=None,
                      approval_request=None, unsigned_approval_reset_request=None,
                      approval_reset_request=None)
    evidence: dict[str, Any] = {"request": dict(operation), "reads": [], "simulations": []}
    deadline = api.monotonic() + LENDING_RESPONSE_SECONDS

    def check_deadline():
        if api.monotonic() >= deadline:
            raise OnchainError("exit response deadline passed")

    def simulate(tx: dict, count: int) -> tuple[int, ...]:
        check_deadline()
        observation = {"transaction": tx}
        evidence["simulations"].append(observation)
        try:
            call = {key: tx[key] for key in ("from", "to", "data")}
            call["value"] = "0x0"
            raw = reader.rpc("eth_call", [call, {"blockHash": block.hash}], require_result=True)
            observation["result"] = raw
            check_deadline()
            return _words(raw, count)
        except OnchainError as exc:
            observation["revert_data"] = exc.revert_data
            raise

    def tx(to: str, data: str) -> dict:
        return {"chainId": chain_id, "from": wallet, "to": to.lower(), "data": data, "value": "0"}

    def fetch(*, bids: bool = False):
        try:
            if api.monotonic() >= deadline:
                raise ExitError("PROVIDER_UNAVAILABLE", "EXIT_UNAVAILABLE")
            answer = api.exit_provider.fetch(identity, bids=bids, deadline_at=deadline)
            if api.monotonic() >= deadline:
                raise ExitError("PROVIDER_UNAVAILABLE", "EXIT_UNAVAILABLE")
            return answer
        except (OSError, Throttled, http.client.HTTPException) as exc:
            raise ExitError("PROVIDER_UNAVAILABLE", "EXIT_UNAVAILABLE") from exc

    try:
        if kind == "PT_REDEEM":
            return _build_pt_redeem(api, operation, result, evidence, deadline, check_deadline, tx)
        if kind == "TENOR_EXIT" and not api.midnight_orders_enabled:
            raise ExitError("EXITS_UNAVAILABLE", "EXIT_UNAVAILABLE")
        venue = "Exactly" if kind == "EXACTLY_EXIT" else "Tenor"
        identity = operation["market_address"] if kind == "EXACTLY_EXIT" else operation["market_id"]
        directory_error = None
        try:
            venues = (venue,) if kind == "EXACTLY_EXIT" else sorted(MIDNIGHT_VENUES)
            row = None
            if api.instruments is not None:
                for candidate in venues:
                    row = api.instruments.exit_market(chain_id, candidate, identity, operation.get("maturity"))
                    if row is not None:
                        break
        except InstrumentError as exc:
            directory_error, row = exc, None
        if row is None and kind == "TENOR_EXIT" and api.orders is not None:
            row = next((r for r in api.orders.midnight_markets(chain_id)
                        if str(r.get("market_id", "")).lower() == identity.lower()), None)
        if row is None:
            if directory_error is not None:
                raise directory_error
            raise ExitError("UNKNOWN_MARKET")
        venue = row.get("venue", venue)
        loan = row.get("loan_token")
        if not isinstance(loan, dict):
            loan = {"address": loan, "decimals": row.get("decimals"), "symbol": row.get("symbol", "")}
        receive = {"address": _address(loan.get("address")), "decimals": loan.get("decimals"),
                   "symbol": loan.get("symbol", "")}
        # Tenor rows name the loan token without its symbol; the universe's token row has it.
        if (isinstance(receive["symbol"], str) and not receive["symbol"].strip()
                and api.instruments is not None):
            receive["symbol"] = api.instruments.symbol(chain_id, receive["address"]) or ""
        if type(receive["decimals"]) is not int or not 0 <= receive["decimals"] <= 255 or not isinstance(receive["symbol"], str):
            raise ExitError("INSTRUMENT_DECIMALS_UNKNOWN")
        result["receive"] = receive
        check_deadline()
        reader = api._order_reader(chain_id, deadline)
        block = reader.head_block()
        result["block"] = {"number": block.number, "hash": block.hash}
        evidence["block"] = block.to_dict()
        check_deadline()
        if kind == "EXACTLY_EXIT":
            maturity, market = operation["maturity"], identity.lower()
            result["position"] = {"venue": venue, "market_address": market, "maturity": maturity}
            data = calldata(EXACTLY_FIXED_DEPOSIT, ("uint256", "address"), (maturity, wallet))
            raw = reader.eth_call(market, data, block)
            evidence["reads"].append({"to": market, "data": data, "result": raw})
            principal, fee = _words(raw, 2)
            face = principal + fee
            if face == 0:
                raise ExitError("NO_POSITION")
            types = ("uint256", "uint256", "uint256", "address", "address")
            mature = block.timestamp >= maturity
            result["action"] = "EXACTLY_WITHDRAW_AT_MATURITY" if mature else "EXACTLY_WITHDRAW_EARLY"
            probe = tx(market, calldata(EXACTLY_WITHDRAW, types, (maturity, face, 0, wallet, wallet)))
            payout, = simulate(probe, 1)
            if mature and payout != face:
                raise OnchainError("mature Exactly payout differs from face")
            minimum = payout if mature else payout - _ceil(payout * EXACTLY_DRIFT_BPS, 10000)
            transaction = tx(market, calldata(EXACTLY_WITHDRAW, types, (maturity, face, minimum, wallet, wallet)))
            # The final calldata, including the floor the wallet signs, is checked at the same block.
            if transaction != probe and simulate(transaction, 1) != (payout,):
                raise OnchainError("Exactly simulation changed at pinned block")
            mark, basis = (payout, "venue pays face value") if mature else _mark(face, row, maturity - block.timestamp)
        else:
            if chain_id not in ADDRESSES:
                raise ExitError("EXITS_UNAVAILABLE", "EXIT_UNAVAILABLE")
            envelope = fetch()
            payload = envelope.get("data") if isinstance(envelope, dict) else None
            if not isinstance(payload, dict):
                raise ExitError("INVALID_PROVIDER_DATA")
            market = parse_market(payload, chain_id)
            if (to_market_id(market) != identity.lower() or not isinstance(payload.get("market_id"), str)
                    or payload["market_id"].lower() != identity.lower() or market.loanToken != receive["address"]):
                raise ExitError("MARKET_KEY_MISMATCH")
            evidence["market"] = {"market_id": identity.lower(), "values": market_values(market)}
            maturity = market.maturity
            result["position"] = {"venue": venue, "market_id": identity.lower(), "maturity": maturity,
                                  "midnight_address": market.midnight.lower()}
            check_deadline()
            position = read_position(reader, identity, wallet, block)
            evidence["reads"].append({"position": position})
            values = []
            for name in ("lossFactor", "withdrawable"):
                check_deadline()
                data = calldata(selector(name + "(bytes32)"), ("bytes32",), (identity,))
                raw = reader.eth_call(market.midnight, data, block)
                evidence["reads"].append({"to": market.midnight, "data": data, "result": raw})
                values.append(_words(raw, 1)[0])
            credit, pending, accrued_fee = accrue_position(position, values[0], maturity, block.timestamp)
            evidence["accrual"] = {"credit": credit, "pending_fee": pending, "accrued_fee": accrued_fee}
            if not credit:
                raise ExitError("NO_POSITION")
            if values[1] > 0:
                units = min(max(0, credit - pending) if block.timestamp < maturity else credit, values[1])
                result["action"] = "TENOR_WITHDRAW"
                transaction = tx(market.midnight, calldata(TENOR_WITHDRAW,
                    (MARKET_ABI, "uint256", "address", "address"), (market_values(market), units, wallet, wallet)))
                # Midnight withdraw has no return values; a successful call proves the encoded payout.
                simulate(transaction, 0)
                payout = minimum = units
                face = max(0, units - _ceil(pending * units, credit))
                mark, basis = payout, "venue pays face value"
            elif block.timestamp >= maturity:
                raise ExitError("AWAITING_REPAYMENT")
            else:
                result["action"] = "TENOR_SELL"
                book = fetch(bids=True)
                if not isinstance(book, dict) or not isinstance(book.get("data"), list):
                    raise ExitError("PROVIDER_UNAVAILABLE", "EXIT_UNAVAILABLE")
                candidates = []
                # The block can trail the clock; a bid must still be live when the wallet signs.
                now = max(block.timestamp, int((api.clock or (lambda: datetime.now(UTC)))().timestamp()))
                for candidate in book["data"]:
                    check_deadline()
                    try:
                        offer, ratifier, available, valid_until = parse_bid(candidate, chain_id)
                        expires_at = min(offer.expiry, valid_until)
                        if (offer.buy and to_market_id(offer.market) == identity.lower()
                                and candidate["market_id"].lower() == identity.lower() and offer.maker != wallet
                                and offer.start <= block.timestamp
                                and now + EXIT_SIGNING_WINDOW_SECONDS <= expires_at):
                            candidates.append((offer, ratifier, available, expires_at))
                    except (ExitError, OrderError):
                        continue
                candidates.sort(key=lambda c: (c[0].tick, c[2]), reverse=True)
                transaction = None
                for offer, ratifier, available, expires_at in candidates[:5]:
                    check_deadline()
                    # Before maturity, further fee accrual can never exceed the current pending fee.
                    # Future loss-factor slashes remain a residual risk.
                    units = min(max(0, credit - pending), available)
                    if not units or (units < credit and units * 100 < credit):
                        continue
                    ratify_data = calldata(RATIFY, (OFFER_ABI, "bytes", "address"), (offer_values(offer), ratifier, wallet))
                    observation = {"to": offer.ratifier, "data": ratify_data}
                    evidence["reads"].append(observation)
                    try:
                        ratified = reader.eth_call(offer.ratifier, ratify_data, block)
                        observation["result"] = ratified
                        if ratified != CALLBACK_SUCCESS:
                            continue
                        attempt = tx(market.midnight, calldata(TENOR_TAKE,
                            (OFFER_ABI, "bytes", "uint256", "address", "address", "address", "bytes"),
                            (offer_values(offer), ratifier, units, wallet, wallet, ZERO_ADDRESS, b"")))
                        buyer, payout = simulate(attempt, 2)
                        if buyer < payout:
                            raise OnchainError("Midnight settlement fee is negative")
                        transaction = attempt
                        result["expires_at"] = expires_at
                        evidence["chosen_offer"] = {"offer": _json_offer(offer),
                                                    "ratifier_data": "0x" + ratifier.hex(), "available": available}
                        break
                    except OnchainError as exc:
                        if exc.revert_data is None:
                            raise
                        observation["revert_data"] = exc.revert_data
                if transaction is None:
                    raise ExitError("NO_TAKEABLE_BID")
                result["action"] = "TENOR_SELL"
                minimum = _midnight_sale_floor(units, offer.tick, maturity - block.timestamp, payout)
                face = max(0, units - _ceil(pending * units, credit))
                mark, basis = _mark(face, row, maturity - block.timestamp)
                result["settlement_fee_raw_amount"] = buyer - payout
            result.update(units_raw_amount=units, remaining_units_raw_amount=credit - units)
        if mark == 0:
            raise ExitError("MARK_UNAVAILABLE", "EXIT_UNAVAILABLE")
        cost = (_ceil((mark - minimum) * 10000, mark) if minimum < mark
                else -((minimum - mark) * 10000 // mark))
        result.update(proceeds_raw_amount=payout, min_proceeds_raw_amount=minimum, face_raw_amount=face,
                      mark_raw_amount=mark, mark_basis=basis, route_cost_bps=cost,
                      unsigned_transaction_request=transaction)
        check_deadline()
        if cost > MAX_ACCEPTED_SALE_COST_BPS:
            raise ExitError("EXIT_COST_TOO_HIGH")
        if cost > MAX_ROUTE_COST_BPS and operation.get("accept_cost_bps", -1) < cost:
            result["cost_acceptance"] = {"route_cost_bps": cost, "max_acceptable_bps": MAX_ACCEPTED_SALE_COST_BPS,
                                         "mark_raw_amount": mark, "proceeds_raw_amount": payout}
            raise ExitError("SALE_COST_NEEDS_ACCEPTANCE", "ANALYTICAL_ONLY")
        if cost > MAX_ROUTE_COST_BPS:
            result["cost_accepted_bps"] = operation["accept_cost_bps"]
        result["status"] = "EXIT_READY"
        result["reason_codes"] = list(api.gate.reason_codes_for(cross_chain=False))
        if api.gate.is_open_for(cross_chain=False):
            result.update(transaction_request=transaction, broadcast_enabled=True)
        default_gas = (250_000 if result["action"] in {"EXACTLY_WITHDRAW_AT_MATURITY", "EXACTLY_WITHDRAW_EARLY"} else
                       200_000 if result["action"] == "TENOR_WITHDRAW" else
                       400_000 if result["action"] == "TENOR_SELL" else None)
        apply_gas_check(result, reader, chain_id=chain_id, wallet=wallet, block=block,
                        default_gas=default_gas)
    except ExitError as exc:
        result.update(status=exc.status, reason_codes=[exc.code])
    except InstrumentError as exc:
        result.update(status="EXIT_UNAVAILABLE", reason_codes=[exc.code])
    except OrderError as exc:
        result.update(status="EXIT_UNAVAILABLE" if exc.code == "ONCHAIN_READ_UNAVAILABLE" else "REJECTED",
                      reason_codes=["ONCHAIN_UNAVAILABLE" if exc.code == "ONCHAIN_READ_UNAVAILABLE" else exc.code])
    except OnchainError as exc:
        evidence["revert_data"] = exc.revert_data
        result.update(status="REJECTED" if exc.revert_data is not None else "EXIT_UNAVAILABLE",
                      reason_codes=[_REVERTS.get(exc.revert_data[:10].lower(), "SIMULATION_REVERTED")
                                    if exc.revert_data is not None else "ONCHAIN_UNAVAILABLE"])
    except (ValueError, KeyError, TypeError, OSError):
        result.update(status="EXIT_UNAVAILABLE", reason_codes=["ONCHAIN_UNAVAILABLE"])
    result["reason_codes"] = sorted(set(result["reason_codes"]))
    unsigned = result["unsigned_transaction_request"]
    if unsigned is None and evidence["simulations"]:
        unsigned = evidence["simulations"][-1]["transaction"]
    evidence.update(response=result, action=result["action"], unsigned_transaction_request=unsigned)
    api._persist_observation(evidence, suffix="position-exit")
    return result
