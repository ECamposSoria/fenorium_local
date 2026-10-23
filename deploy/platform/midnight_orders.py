"""Tenor's verified market terms and unsigned Midnight maker-order primitives.

This module deliberately owns the boundary between Tenor's API and the frozen encoder in
``engine.midnight``. A market is usable only when its CREATE2 identifier agrees with the key
Tenor returned; no order is built from a guessed address or market.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
import http.client
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Mapping

from engine.acquire.fetch import retry_after_seconds
from engine.keccak import keccak256
from engine.midnight import (
    ADDRESSES, MARKET_ABI, OFFER_ABI, ZERO_ADDRESS, CollateralParams, Market,
    Offer, RatifierSignature, abi_encode,
    encode_payload, lending_bid, lender_apr, ratifier_data, tick_to_price,
    tree_leaves, tree_proof, tree_root, typed_data,
)
from engine.onchain import (MULTICALL_CHUNK, MIN_MULTICALL_CHUNK, OnchainError,
                            decode_aggregate3, encode_aggregate3, read_at_pinned_block, state_gone)
from .ratelimit import ProviderBudget, Throttled


MORPHO_MEMPOOL_VALIDATE = "https://api.morpho.org/v0/midnight/mempool/validate"
TENOR_GRAPHQL = "https://api.tenor.finance/graphql"
TENOR_QUERY = """query FixedMarket {
  morphoFixedMarketById(id: {market_uuid}) {
    id fixedMarketIdentifier chainId maturity fixedMarketKey {
      chainId loanTokenAddress maturity rcfThreshold enterGate liquidatorGate
      collaterals { token lltv liquidationCursor oracle }
    }
    tenorMarket { id chainId loanToken { address decimals symbol } }
    state { supplyRateApr }
  }
}"""
SSTORE2_PREFIX = bytes.fromhex("600b380380600b5f395ff3")
CALLBACK_SUCCESS = "0x" + keccak256(b"morpho.midnight.callbackSuccess").hex()
MAX_RESPONSE_BYTES = 256_000
DEFAULT_TIMEOUT = 8.0
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


class OrderError(ValueError):
    """A safe, stable refusal code for a lending-order operation."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VerifiedMarket:
    market: Market
    market_id: str
    decimals: int
    symbol: str
    spacing: int = 4


def market_values(market: Market) -> tuple:
    """Return ABI tuple values, recursively shaped as engine.midnight expects."""
    return (market.chainId, market.midnight, market.loanToken,
            tuple((c.token, c.lltv, c.liquidationCursor, c.oracle) for c in market.collateralParams),
            market.maturity, market.rcfThreshold, market.enterGate, market.liquidatorGate)


def offer_values(offer: Offer) -> tuple:
    return (market_values(offer.market), offer.buy, offer.maker, offer.start, offer.expiry,
            offer.tick, offer.group, offer.callback, offer.callbackData,
            offer.receiverIfMakerIsSeller, offer.ratifier, offer.reduceOnly,
            offer.maxUnits, offer.maxAssets, offer.continuousFeeCap)


def to_market_id(market: Market) -> str:
    """Calculate Midnight's CREATE2 market id from the exact ABI-encoded market tuple."""
    addresses = ADDRESSES.get(market.chainId)
    if addresses is None or market.midnight.lower() != addresses.midnight.lower():
        raise OrderError("MARKET_KEY_MISMATCH", "market does not match the configured Midnight deployment")
    encoded = abi_encode((MARKET_ABI,), (market_values(market),))
    init_code_hash = keccak256(SSTORE2_PREFIX + encoded)
    raw = keccak256(b"\xff" + bytes.fromhex(market.midnight[2:]) + bytes(32) + init_code_hash)
    return "0x" + raw.hex()


def _uint(value: Any, field: str) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0x[0-9a-fA-F]{1,64}|[0-9]{1,78})", value):
        result = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise OrderError("TENOR_INVALID_MARKET", f"Tenor returned invalid {field}")
    if not 0 <= result < 1 << 256:
        raise OrderError("TENOR_INVALID_MARKET", f"Tenor returned invalid {field}")
    return result


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise OrderError("TENOR_INVALID_MARKET", f"Tenor returned invalid {field}")
    return "0x" + value[2:].lower()


def _json_offer(value: Offer) -> dict[str, Any]:
    return {
        "market": {
            "chainId": str(value.market.chainId), "midnight": value.market.midnight,
            "loanToken": value.market.loanToken,
            "collateralParams": [
                {"token": c.token, "lltv": str(c.lltv), "liquidationCursor": str(c.liquidationCursor), "oracle": c.oracle}
                for c in value.market.collateralParams
            ],
            "maturity": str(value.market.maturity), "rcfThreshold": str(value.market.rcfThreshold),
            "enterGate": value.market.enterGate, "liquidatorGate": value.market.liquidatorGate,
        },
        "buy": value.buy, "maker": value.maker, "start": str(value.start), "expiry": str(value.expiry),
        "tick": str(value.tick), "group": "0x" + value.group.hex(), "callback": value.callback,
        "callbackData": "0x" + value.callbackData.hex(),
        "receiverIfMakerIsSeller": value.receiverIfMakerIsSeller, "ratifier": value.ratifier,
        "reduceOnly": value.reduceOnly, "maxUnits": str(value.maxUnits), "maxAssets": str(value.maxAssets),
        "continuousFeeCap": str(value.continuousFeeCap),
    }


def offer_from_json(value: Mapping[str, Any]) -> Offer:
    """Reconstitute a stored offer without accepting Python-specific serialized objects."""
    try:
        m = value["market"]
        market = Market(_uint(m["chainId"], "chainId"), _address(m["midnight"], "midnight"),
                        _address(m["loanToken"], "loanToken"),
                        tuple(CollateralParams(_address(c["token"], "collateral token"),
                                               _uint(c["lltv"], "lltv"),
                                               _uint(c["liquidationCursor"], "liquidationCursor"),
                                               _address(c["oracle"], "oracle")) for c in m["collateralParams"]),
                        _uint(m["maturity"], "maturity"), _uint(m["rcfThreshold"], "rcfThreshold"),
                        _address(m["enterGate"], "enterGate"), _address(m["liquidatorGate"], "liquidatorGate"))
        return Offer(market, value["buy"], _address(value["maker"], "maker"), _uint(value["start"], "start"),
                     _uint(value["expiry"], "expiry"), _uint(value["tick"], "tick"),
                     bytes.fromhex(value["group"].removeprefix("0x")),
                     _address(value["callback"], "callback"),
                     bytes.fromhex(value["callbackData"].removeprefix("0x")),
                     _address(value["receiverIfMakerIsSeller"], "receiverIfMakerIsSeller"),
                     _address(value["ratifier"], "ratifier"), value["reduceOnly"],
                     _uint(value["maxUnits"], "maxUnits"), _uint(value["maxAssets"], "maxAssets"),
                     _uint(value["continuousFeeCap"], "continuousFeeCap"))
    except OrderError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise OrderError("ORDER_RECORD_INVALID", "stored order terms are malformed") from exc


class MidnightPolicy:
    """Bounded policy-only validation of the exact Midnight mempool payload."""

    def __init__(self, budget: Any = None, *, opener: Callable[..., Any] = _open_no_redirect,
                 timeout: float = DEFAULT_TIMEOUT, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget if budget is not None else ProviderBudget("Morpho mempool", per_minute=30, per_day=5_000)
        self.opener, self.timeout, self.clock = opener, timeout, clock

    def issues(self, chain_id: int, payload: bytes, at: datetime, *,
               deadline_at: float | None = None) -> list[dict]:
        deadline = min(self.clock() + self.timeout, deadline_at) if deadline_at is not None else self.clock() + self.timeout
        try:
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError("policy timestamp must be timezone-aware")
            # Morpho's validator rejects microseconds ("must be an ISO-8601 UTC timestamp"); whole seconds and
            # milliseconds pass (measured 2026-10-13). Every pre-flight had been refused since it shipped.
            timestamp = at.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            url = MORPHO_MEMPOOL_VALIDATE + "?timestamp=" + timestamp
            body = json.dumps({"chain_id": chain_id, "payload": "0x" + payload.hex()}, separators=(",", ":")).encode()
            request = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Accept": "application/json", "Content-Type": "application/json"})
            if deadline <= self.clock():
                raise OrderError("POLICY_UNAVAILABLE")
            self.budget.reserve()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise OrderError("POLICY_UNAVAILABLE")
            with self.opener(request, timeout=remaining) as response:
                if response.geturl() != url:
                    raise OrderError("POLICY_UNAVAILABLE")
                status = response.status
                if status == 429:
                    self.budget.trip(retry_after_seconds(response.headers.get("Retry-After")))
                if not 200 <= status < 300:
                    raise OrderError("POLICY_UNAVAILABLE")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES or self.clock() >= deadline:
                raise OrderError("POLICY_UNAVAILABLE")
            answer = json.loads(raw)
            data = answer.get("data") if isinstance(answer, dict) else None
            issues = data.get("issues") if isinstance(data, dict) else None
            if not isinstance(issues, list) or any(
                not isinstance(issue, dict) or not isinstance(issue.get("rule"), str) or not issue["rule"]
                or "details" not in issue or (issue["details"] is not None and not isinstance(issue["details"], dict))
                for issue in issues
            ):
                raise OrderError("POLICY_UNAVAILABLE")
            return issues
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self.budget.trip(retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None))
            raise OrderError("POLICY_UNAVAILABLE") from exc
        except (Throttled, urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException,
                ValueError, TypeError, AttributeError, RecursionError) as exc:
            raise OrderError("POLICY_UNAVAILABLE") from exc


class TenorMarkets:
    """Bounded, budgeted read-only access to Tenor's fixed-market GraphQL endpoint."""

    def __init__(self, budget: Any = None, *, opener: Callable[..., Any] = _open_no_redirect,
                 timeout: float = DEFAULT_TIMEOUT, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget, self.opener, self.timeout, self.clock = budget, opener, timeout, clock

    def fetch(self, market_uuid: str, chain_id: int, *, deadline_at: float | None = None) -> VerifiedMarket:
        try:
            market_uuid = str(uuid.UUID(market_uuid))
        except (ValueError, TypeError, AttributeError) as exc:
            raise OrderError("MARKET_NOT_FOUND", "market id is not a UUID") from exc
        addresses = ADDRESSES.get(chain_id)
        if addresses is None:
            raise OrderError("UNSUPPORTED_CHAIN", "Midnight is not configured for this chain")
        remaining = self.timeout if deadline_at is None else min(self.timeout, deadline_at - self.clock())
        if remaining <= 0:
            raise OrderError("ORDERS_UNAVAILABLE", "Tenor request deadline passed")
        if self.budget is not None:
            try:
                self.budget.reserve()
            except Throttled as exc:
                raise OrderError(exc.code, str(exc)) from exc
        query = TENOR_QUERY.replace("{market_uuid}", json.dumps(market_uuid))
        body = json.dumps({"query": query}, separators=(",", ":")).encode()
        request = urllib.request.Request(TENOR_GRAPHQL, data=body,
                                         headers={"Accept": "application/json", "Content-Type": "application/json"},
                                         method="POST")
        try:
            with self.opener(request, timeout=remaining) as response:
                if getattr(response, "geturl", lambda: TENOR_GRAPHQL)() != TENOR_GRAPHQL:
                    raise OrderError("ORDERS_UNAVAILABLE", "Tenor redirected the market request")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except OrderError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and self.budget is not None:
                self.budget.trip(retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None))
            raise OrderError("ORDERS_UNAVAILABLE", "Tenor market source is unavailable") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise OrderError("ORDERS_UNAVAILABLE", "Tenor market source is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OrderError("ORDERS_UNAVAILABLE", "Tenor returned an oversized market answer")
        if deadline_at is not None and self.clock() >= deadline_at:
            raise OrderError("ORDERS_UNAVAILABLE", "Tenor request deadline passed")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OrderError("ORDERS_UNAVAILABLE", "Tenor returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise OrderError("MARKET_NOT_FOUND", "Tenor did not return the requested market")
        data = payload.get("data")
        row = data.get("morphoFixedMarketById") if isinstance(data, dict) else None
        if not isinstance(row, dict) or str(row.get("id", "")).lower() != market_uuid.lower():
            raise OrderError("MARKET_NOT_FOUND", "Tenor did not return the requested market")
        key, tm = row.get("fixedMarketKey"), row.get("tenorMarket")
        loan = tm.get("loanToken") if isinstance(tm, dict) else None
        if not isinstance(key, dict) or not isinstance(loan, dict):
            raise OrderError("TENOR_INVALID_MARKET", "Tenor market key is incomplete")
        if _uint(row.get("chainId"), "chainId") != chain_id or _uint(key.get("chainId"), "key chainId") != chain_id:
            raise OrderError("TENOR_INVALID_MARKET", "Tenor market chain does not match the request")
        if _uint(tm.get("chainId"), "Tenor chainId") != chain_id:
            raise OrderError("TENOR_INVALID_MARKET", "Tenor loan token chain does not match the request")
        maturity = _uint(key.get("maturity"), "maturity")
        try:
            datetime.fromtimestamp(maturity, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise OrderError("TENOR_INVALID_MARKET", "Tenor maturity is outside the supported date range") from exc
        if maturity != _uint(row.get("maturity"), "maturity"):
            raise OrderError("TENOR_INVALID_MARKET", "Tenor market maturity fields disagree")
        loan_token = _address(key.get("loanTokenAddress"), "loanTokenAddress")
        if loan_token != _address(loan.get("address"), "loan token"):
            raise OrderError("TENOR_INVALID_MARKET", "Tenor loan token fields disagree")
        collaterals = key.get("collaterals")
        if not isinstance(collaterals, list):
            raise OrderError("TENOR_INVALID_MARKET", "Tenor collateral terms are incomplete")
        params = tuple(CollateralParams(_address(c.get("token"), "collateral token"),
                                        _uint(c.get("lltv"), "lltv"),
                                        _uint(c.get("liquidationCursor"), "liquidationCursor"),
                                        _address(c.get("oracle"), "oracle")) for c in collaterals if isinstance(c, dict))
        if len(params) != len(collaterals):
            raise OrderError("TENOR_INVALID_MARKET", "Tenor collateral terms are malformed")
        market = Market(chain_id, addresses.midnight, loan_token, params, maturity,
                        _uint(key.get("rcfThreshold"), "rcfThreshold"),
                        _address(key.get("enterGate"), "enterGate"),
                        _address(key.get("liquidatorGate"), "liquidatorGate"))
        returned_id = row.get("fixedMarketIdentifier")
        if not isinstance(returned_id, str) or not _HASH.fullmatch(returned_id):
            raise OrderError("MARKET_KEY_MISMATCH", "Tenor's fixed market identifier is malformed")
        calculated_id = to_market_id(market)
        if calculated_id.lower() != returned_id.lower():
            raise OrderError("MARKET_KEY_MISMATCH", "Tenor's market identifier does not match its encoded key")
        decimals = _uint(loan.get("decimals"), "loan token decimals")
        if decimals > 255:
            raise OrderError("TENOR_INVALID_MARKET", "Tenor returned invalid loan token decimals")
        symbol = loan.get("symbol")
        if not isinstance(symbol, str) or not symbol or len(symbol) > 64:
            raise OrderError("TENOR_INVALID_MARKET", "Tenor returned invalid loan token symbol")
        # The requested Tenor query does not expose spacing; use the SDK default unless a
        # future source explicitly includes a valid value.
        spacing = _uint(row.get("tickSpacing", 4), "tickSpacing")
        if not 1 <= spacing <= 6744:
            raise OrderError("TENOR_INVALID_MARKET", "Tenor returned invalid tick spacing")
        return VerifiedMarket(market, calculated_id, decimals, symbol, spacing)


def build_order(verified: VerifiedMarket, wallet: str, amount_raw: int, apr_bps: int | Fraction,
                start: int, expiry_seconds: int) -> dict[str, Any]:
    """Build one maker bid and the complete wallet-signable evidence for it."""
    if (verified.market.chainId not in ADDRESSES
            or not isinstance(verified.market_id, str)
            or verified.market_id.lower() != to_market_id(verified.market).lower()
            or type(verified.decimals) is not int or type(verified.spacing) is not int
            or not 0 <= verified.decimals <= 255
            or not 1 <= verified.spacing <= 6744):
        raise OrderError("MARKET_KEY_MISMATCH", "verified Tenor market terms no longer match their identifier")
    wallet = _address(wallet, "maker")
    if type(amount_raw) is not int or amount_raw <= 0:
        raise OrderError("INVALID_AMOUNT", "amount_raw must be a positive integer")
    if (type(apr_bps) is not int and not isinstance(apr_bps, Fraction)) or apr_bps < 0:
        raise OrderError("INVALID_RATE", "apr_bps must be a non-negative integer")
    if type(expiry_seconds) is not int or not 1 <= expiry_seconds <= 7 * 86_400:
        raise OrderError("INVALID_EXPIRY", "expiry_seconds must be between one second and seven days")
    if start >= verified.market.maturity:
        raise OrderError("MATURED", "the Tenor market has matured")
    expiry = min(start + expiry_seconds, verified.market.maturity)
    try:
        offer = lending_bid(verified.market, wallet, amount_raw, Fraction(apr_bps) / 10_000,
                            start, expiry, verified.market.chainId, verified.spacing)
    except ValueError as exc:
        raise OrderError("INVALID_ORDER", str(exc)) from exc
    leaves = tree_leaves([offer])
    root = tree_root(leaves)
    proof = tree_proof(leaves, 0)
    height = (len(leaves) - 1).bit_length()
    achieved = lender_apr(tick_to_price(offer.tick), verified.market.maturity - start)
    achieved_bps = achieved.numerator * 10_000 // achieved.denominator
    addresses = ADDRESSES[verified.market.chainId]
    now = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "offer": _json_offer(offer), "root": "0x" + root.hex(), "height": height,
        "leaf_index": 0, "proof": ["0x" + p.hex() for p in proof],
        "typed_data": typed_data([offer], verified.market.chainId, addresses.ratifier),
        "mempool": addresses.mempool, "apr_bps": achieved_bps,
        "expires_at": expiry, "market_id": verified.market_id, "decimals": verified.decimals,
        "loan_token": verified.market.loanToken, "created_at": now,
    }


def _signature(value: Mapping[str, Any]) -> RatifierSignature:
    try:
        v = value["v"]
        if type(v) is not int:
            raise ValueError("signature v must be an integer")
        r, s = value["r"], value["s"]
        signature = RatifierSignature(v, r, s)
    except (KeyError, TypeError, ValueError) as exc:
        raise OrderError("INVALID_SIGNATURE", "signature must contain v, r and s") from exc
    return signature


def make_ratifier_data(signature: Mapping[str, Any] | RatifierSignature, record: Mapping[str, Any]) -> bytes:
    signature = signature if isinstance(signature, RatifierSignature) else _signature(signature)
    try:
        return ratifier_data(signature, record["root"], int(record["leaf_index"]), record["proof"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OrderError("ORDER_RECORD_INVALID", "stored order proof is malformed") from exc


def _offer_calldata(offer: Offer, ratifier_bytes: bytes) -> str:
    data = keccak256(b"isRatified(" + OFFER_ABI.encode() + b",bytes,address)")[:4]
    data += abi_encode((OFFER_ABI, "bytes", "address"), (offer_values(offer), ratifier_bytes, ZERO_ADDRESS))
    return "0x" + data.hex()


def verify_signature(reader: Any, record: Mapping[str, Any], signature: Mapping[str, Any],
                     *, evidence: dict[str, Any] | None = None, block: Any = None) -> str:
    """Read the ratifier's exact verdict; an unavailable chain has given no verdict."""
    offer = offer_from_json(record["offer"])
    addresses = ADDRESSES.get(int(record["chain_id"]))
    if addresses is None:
        raise OrderError("UNSUPPORTED_CHAIN", "Midnight is not configured for this chain")
    ratifier_bytes = make_ratifier_data(signature, record)
    call_data = _offer_calldata(offer, ratifier_bytes)
    try:
        if block is None:
            _, result = read_at_pinned_block(reader, lambda block: reader.eth_call(addresses.ratifier, call_data, block))
        else:
            result = reader.eth_call(addresses.ratifier, call_data, block)
    except OnchainError as exc:
        if exc.revert_data is None:
            raise
        selector = exc.revert_data[:10].lower()
        if evidence is not None:
            evidence.update(revert_data=exc.revert_data, revert_selector=selector)
        if selector == "0xec7bcf5b":
            return "ORDER_CANCELED"
        if selector in {"0x09bde339", "0x118f6218", "0x0fda59e6"}:
            return "ORDER_RECORD_INVALID"
        return "SIGNATURE_UNVERIFIED"
    if evidence is not None:
        evidence["ratifier_result"] = result
    return "RATIFIED" if result == CALLBACK_SUCCESS else "SIGNATURE_UNVERIFIED"


def publish_transaction(record: Mapping[str, Any], signature: Mapping[str, Any]) -> dict[str, Any]:
    offer = offer_from_json(record["offer"])
    chain_id = int(record["chain_id"])
    addresses = ADDRESSES.get(chain_id)
    if addresses is None:
        raise OrderError("UNSUPPORTED_CHAIN", "Midnight is not configured for this chain")
    calldata = encode_payload([(offer, make_ratifier_data(signature, record))])
    return {"chainId": chain_id, "from": offer.maker, "to": addresses.mempool,
            "data": "0x" + calldata.hex(), "value": "0"}


def cancel_transaction(wallet: str, chain_id: int, root: str) -> dict[str, Any]:
    wallet = _address(wallet, "maker")
    if not isinstance(root, str) or not _HASH.fullmatch(root):
        raise OrderError("INVALID_ROOT", "root must be a 32-byte hex value")
    addresses = ADDRESSES.get(chain_id)
    if addresses is None:
        raise OrderError("UNSUPPORTED_CHAIN", "Midnight is not configured for this chain")
    call = keccak256(b"cancelRoot(address,bytes32)")[:4] + abi_encode(("address", "bytes32"), (wallet, root))
    return {"chainId": chain_id, "from": wallet, "to": addresses.ratifier,
            "data": "0x" + call.hex(), "value": "0"}


def accrue_position(position: Mapping[str, int], loss_factor: int, maturity: int,
                    at: int) -> tuple[int, int, int]:
    """Mirror Midnight.updatePositionView's slash and continuous-fee rounding."""
    maximum = 2**128 - 1
    credit, pending = position["credit"], position["pendingFee"]
    last_loss, last_accrual = position["lastLossFactor"], position["lastAccrual"]
    if (any(type(value) is not int or not 0 <= value <= maximum
            for value in (credit, pending, last_loss, last_accrual, loss_factor))
            or pending > credit or (last_loss < maximum and loss_factor < last_loss)
            or type(maturity) is not int or type(at) is not int or min(maturity, at) < 0):
        raise ValueError("invalid Midnight accrual inputs")
    post_credit = credit * (maximum - loss_factor) // (maximum - last_loss) if last_loss < maximum else 0
    post_pending = pending - (pending * (credit - post_credit) + credit - 1) // credit if credit else 0
    accrual_end = min(at, maturity)
    if last_accrual < maturity and accrual_end < last_accrual:
        raise ValueError("Midnight accrual precedes last accrual")
    fee = (post_pending * (accrual_end - last_accrual) // (maturity - last_accrual)
           if last_accrual < maturity else 0)
    return post_credit - fee, post_pending - fee, fee


def read_position(reader: Any, market_id: str, wallet: str, block: Any) -> dict[str, int]:
    """Read the six static words returned by Midnight position(bytes32,address)."""
    if not _HASH.fullmatch(market_id):
        raise OrderError("INVALID_MARKET_ID", "market id must be a 32-byte hex value")
    wallet = _address(wallet, "wallet")
    call = keccak256(b"position(bytes32,address)")[:4] + abi_encode(("bytes32", "address"), (market_id, wallet))
    raw = bytes.fromhex(reader.eth_call(ADDRESSES[reader.chain.chain_id].midnight, "0x" + call.hex(), block).removeprefix("0x"))
    if len(raw) != 6 * 32:
        raise OrderError("ONCHAIN_READ_UNAVAILABLE", "Midnight position read returned incomplete data")
    names = ("credit", "pendingFee", "lastLossFactor", "lastAccrual", "debt", "collateralBitmap")
    return dict(zip(names, (int.from_bytes(raw[index:index + 32], "big") for index in range(0, 192, 32))))


def read_order_facts(reader: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """Read root cancellation and the market position at one pinned block."""
    chain_id = int(record["chain_id"])
    addresses = ADDRESSES.get(chain_id)
    if addresses is None or reader.chain.chain_id != chain_id:
        raise OrderError("UNSUPPORTED_CHAIN", "order chain does not match its reader")
    offer = offer_from_json(record["offer"])
    def read(block):
        canceled_call = keccak256(b"isRootCanceled(address,bytes32)")[:4] + abi_encode(
            ("address", "bytes32"), (offer.maker, record["root"]))
        canceled_raw = bytes.fromhex(reader.eth_call(addresses.ratifier, "0x" + canceled_call.hex(), block)
                                     .removeprefix("0x"))
        if len(canceled_raw) != 32 or int.from_bytes(canceled_raw, "big") not in (0, 1):
            raise OrderError("ONCHAIN_READ_UNAVAILABLE", "cancellation read returned incomplete data")
        position = read_position(reader, record["market_id"], offer.maker, block)
        return canceled_raw, position
    try:
        block, (canceled_raw, position) = read_at_pinned_block(reader, read)
    except OrderError:
        raise
    except OnchainError as exc:
        raise OrderError("ONCHAIN_READ_UNAVAILABLE", "order state is unavailable") from exc
    position_strings = {key: str(value) for key, value in position.items()}
    return {"canceled": bool(int.from_bytes(canceled_raw, "big")), "position": position_strings,
            "block": block.to_dict() if hasattr(block, "to_dict") else block}


def read_orders_facts(reader: Any, records: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Batch root cancellations and distinct maker positions at one pinned block."""
    if not records:
        return {}
    addresses = ADDRESSES.get(reader.chain.chain_id)
    if addresses is None or any(int(record["chain_id"]) != reader.chain.chain_id for record in records):
        raise OrderError("UNSUPPORTED_CHAIN", "order chain does not match its reader")
    calls, positions, roots, maturities, losses = [], {}, {}, {}, {}
    for record in records:
        offer = offer_from_json(record["offer"])
        maker = offer.maker
        maturities[record["market_id"]] = offer.market.maturity
        canceled = keccak256(b"isRootCanceled(address,bytes32)")[:4] + abi_encode(
            ("address", "bytes32"), (maker, record["root"]))
        roots[record["root"]] = (len(calls), (record["market_id"], maker))
        calls.append((addresses.ratifier, "0x" + canceled.hex()))
        positions[(record["market_id"], maker)] = None
    for market, maker in positions:
        positions[(market, maker)] = len(calls)
        position = keccak256(b"position(bytes32,address)")[:4] + abi_encode(
            ("bytes32", "address"), (market, maker))
        calls.append((addresses.midnight, "0x" + position.hex()))
    for market in maturities:
        losses[market] = len(calls)
        loss = keccak256(b"lossFactor(bytes32)")[:4] + abi_encode(("bytes32",), (market,))
        calls.append((addresses.midnight, "0x" + loss.hex()))

    def read(block):
        def chunk_reads(chunk):
            try:
                return decode_aggregate3(reader.eth_call(reader.chain.multicall3,
                                         encode_aggregate3(chunk), block), len(chunk))
            except OnchainError as exc:
                if state_gone(exc):
                    raise
                if len(chunk) <= MIN_MULTICALL_CHUNK:
                    return [(False, b"")] * len(chunk)
                half = len(chunk) // 2
                return chunk_reads(chunk[:half]) + chunk_reads(chunk[half:])
        answers = []
        for start in range(0, len(calls), MULTICALL_CHUNK):
            answers.extend(chunk_reads(calls[start:start + MULTICALL_CHUNK]))
        return answers

    block, answers = read_at_pinned_block(reader, read)
    result = {}
    names = ("credit", "pendingFee", "lastLossFactor", "lastAccrual", "debt", "collateralBitmap")
    for root, (cancel_index, position_key) in roots.items():
        canceled_ok, canceled = answers[cancel_index]
        position_ok, position = answers[positions[position_key]]
        loss_ok, loss = answers[losses[position_key[0]]]
        if (not canceled_ok or len(canceled) != 32 or int.from_bytes(canceled, "big") not in (0, 1)
                or not position_ok or len(position) != 192
                or not loss_ok or len(loss) != 32 or int.from_bytes(loss, "big") >= 2**128):
            result[root] = {"canceled": None, "position": None, "block": None,
                            "reason_codes": ["ONCHAIN_UNAVAILABLE"]}
            continue
        values = dict(zip(names, (int.from_bytes(position[i:i + 32], "big") for i in range(0, 192, 32))))
        try:
            # Accrual runs to the pinned block's time; a block without one cannot value the position.
            credit, pending, _ = accrue_position(values, int.from_bytes(loss, "big"),
                                                maturities[position_key[0]], getattr(block, "timestamp", None))
        except ValueError:
            result[root] = {"canceled": None, "position": None, "block": None,
                            "reason_codes": ["ONCHAIN_UNAVAILABLE"]}
            continue
        # Debt remains stored; Midnight accrues debt through a separate path.
        values.update(credit=credit, pendingFee=pending, face_value=credit - pending)
        result[root] = {"canceled": bool(int.from_bytes(canceled, "big")),
                        "position": {key: str(value) for key, value in values.items()},
                        "block": block.to_dict() if hasattr(block, "to_dict") else block}
    return result
