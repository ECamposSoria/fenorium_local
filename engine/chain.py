"""Minimal read-only client for the DecisionPassport on Base.

Deliberately dependency-free: the project has no ``web3``, and the Console must run on a laptop
at a hackathon without an install step. Only two things are needed — an ``eth_call`` and a decoder
for one known struct layout — so both are implemented directly rather than pulled in.

Read-only by construction. There is no signing path in this module.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

BASE_MAINNET_RPC = "https://mainnet.base.org"
BASE_SEPOLIA_RPC = "https://sepolia.base.org"
BASESCAN = "https://basescan.org"

# keccak256("passportOf(uint256)")[:4] and friends, precomputed at import from our own keccak.
from .keccak import keccak256  # noqa: E402

VERDICT_NAMES = {0: "NONE", 1: "BID", 2: "NO BID", 3: "NO QUOTE"}


class ChainError(RuntimeError):
    """Raised when the chain cannot be read. Never swallowed into a default value."""

    # The JSON-RPC `error` member exactly as the provider sent it, when there was one. The message
    # flattens it to text for people; code that needs its fields reads them here instead of
    # parsing them back out of that text.
    rpc_error: Any = None

    def __init__(self, message: str, *, revert_data: str | None = None) -> None:
        super().__init__(message)
        self.revert_data = revert_data


class RpcTransportError(ChainError):
    """The endpoint was unavailable or capacity-limited; another endpoint may be tried."""


def is_transport_failure(error: BaseException) -> bool:
    """Recognize a transport failure even after a higher layer adds domain context."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, RpcTransportError):
            return True
        current = current.__cause__
    return False


_TRANSPORT_RPC_CODES = frozenset({-32005, -32029})
_TRANSPORT_RPC_MESSAGE = re.compile(
    r"rate limit|too many requests?|limit exceeded|capacity|throttl", re.IGNORECASE,
)


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()[:8]


@dataclass(frozen=True, slots=True)
class OnChainPassport:
    id: int
    instrument_chain_id: int
    instrument: str
    denomination_asset: str
    verdict: str
    maturity: int
    quote_expiry: int
    recorded_at: int
    executable_apy_bps: int
    intended_size: int
    decision_hash: str
    method_version: str
    evidence_cid: str

    def quote_live_at(self, now: int) -> bool:
        return self.verdict == "BID" and self.quote_expiry > now


def rpc_call(url: str, method: str, params: list[Any], *, timeout: int = 20) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "par-console/2026.08.22"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        error_type = RpcTransportError if exc.code == 429 or exc.code >= 500 else ChainError
        raise error_type(f"{method} failed against {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # Standalone on purpose: the Console image ships this module without engine/portfolio.
        # Callers that surface this message pass it through safe_detail themselves.
        raise RpcTransportError(f"{method} failed against {url}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ChainError(f"{method} returned malformed JSON against {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChainError(f"{method} returned a non-object payload: {payload!r}")
    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        message = str(error.get("message") or "") if isinstance(error, dict) else str(error)
        error_type = (
            RpcTransportError
            if code in _TRANSPORT_RPC_CODES or _TRANSPORT_RPC_MESSAGE.search(message)
            else ChainError
        )
        failure = error_type(f"{method} returned an error against {url}: {error}")
        failure.rpc_error = error
        data = error.get("data") if isinstance(error, dict) else None
        if isinstance(data, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", data):
            failure.revert_data = data
        raise failure
    if "result" not in payload:
        raise ChainError(f"{method} returned no result: {payload}")
    return payload["result"]


def eth_call(url: str, to: str, data: str) -> bytes:
    result = rpc_call(url, "eth_call", [{"to": to, "data": data}, "latest"])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ChainError(f"eth_call returned an unusable result: {result!r}")
    raw = bytes.fromhex(result[2:])
    if not raw:
        # A bare 0x means the call reverted or the address holds no code. Both are real
        # conditions and neither is an empty passport.
        raise ChainError("eth_call returned empty data: the call reverted or there is no contract there")
    return raw


def _word(raw: bytes, index: int) -> bytes:
    start = index * 32
    if start + 32 > len(raw):
        raise ChainError(f"return data is too short for word {index}")
    return raw[start:start + 32]


def _uint(raw: bytes, index: int) -> int:
    return int.from_bytes(_word(raw, index), "big")


def _int(raw: bytes, index: int, bits: int) -> int:
    """Decode a signed integer narrower than a word.

    Solidity sign-extends a negative ``intN`` across the whole 256-bit word, so the raw value of
    ``int64(-402)`` is ``2**256 - 402``. Masking to ``bits`` first is what makes the two's
    complement conversion correct; without it a negative rate decodes as a 78-digit positive
    number rather than a negative one.
    """
    value = _uint(raw, index) & ((1 << bits) - 1)
    limit = 1 << (bits - 1)
    return value - (1 << bits) if value >= limit else value


def _address(raw: bytes, index: int) -> str:
    return "0x" + _word(raw, index)[12:].hex()


def _string_at(raw: bytes, tuple_start: int, offset_word: int) -> str:
    offset = _uint(raw, offset_word)
    start = tuple_start + offset
    length = int.from_bytes(raw[start:start + 32], "big")
    body = raw[start + 32:start + 32 + length]
    return body.decode("utf-8", errors="replace")


def decode_passport(raw: bytes, passport_id: int) -> OnChainPassport:
    """Decode the ``Passport`` struct returned by ``passportOf(uint256)``.

    Layout: one offset word to the tuple, then twelve head words, then the string tails. String
    offsets are relative to the start of the tuple, not the start of the return data.
    """
    tuple_offset = int.from_bytes(raw[0:32], "big")
    if tuple_offset != 32:
        raise ChainError(f"unexpected tuple offset {tuple_offset}; ABI layout is not what we deployed")
    base = 32
    head = raw[base:]

    return OnChainPassport(
        id=passport_id,
        instrument_chain_id=_uint(head, 0),
        instrument=_address(head, 1),
        denomination_asset=_address(head, 2),
        verdict=VERDICT_NAMES.get(_uint(head, 3), f"UNKNOWN({_uint(head, 3)})"),
        maturity=_uint(head, 4),
        quote_expiry=_uint(head, 5),
        recorded_at=_uint(head, 6),
        executable_apy_bps=_int(head, 7, 64),
        intended_size=_uint(head, 8),
        decision_hash="0x" + _word(head, 9).hex(),
        method_version=_string_at(head, 0, 10),
        evidence_cid=_string_at(head, 0, 11),
    )


def fetch_passport(url: str, contract: str, passport_id: int) -> OnChainPassport:
    data = selector("passportOf(uint256)") + f"{passport_id:064x}"
    return decode_passport(eth_call(url, contract, data), passport_id)


def fetch_total(url: str, contract: str) -> int:
    raw = eth_call(url, contract, selector("total()"))
    return int.from_bytes(raw[:32], "big")


def tx_url(tx_hash: str) -> str:
    return f"{BASESCAN}/tx/{tx_hash}"


def address_url(address: str) -> str:
    return f"{BASESCAN}/address/{address}"
