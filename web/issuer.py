#!/usr/bin/env python3
"""Par demo credential issuer — Base Sepolia only.

Issues a real EAS attestation to a wallet so the gated instrument's eligibility path can be
walked end to end. The service *issues*; the token contract *verifies* — schema, attester,
recipient, revocation and expiry are all re-checked on chain in `registerCredential`. Nothing
here is trusted by the contract, which is the point.

The public demo holds a testnet signing key, so successful issuance is deliberately scarce:
per-client and daily limits bound abuse, while a balance floor turns a drained faucet into an
explicit refusal instead of an opaque transaction error. It asserts nothing about any real person.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
# Direct execution puts web/, not the repository root, on sys.path; the shared Keccak must remain
# the single Ethereum-compatible implementation used by both the engine and this issuer.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.keccak import keccak256

RPC = os.environ.get("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org")
EAS = "0x4200000000000000000000000000000000000021"
ATTEST_SIGNATURE = "attest((bytes32,(address,uint64,bool,bytes32,bytes,uint256)))"
ATTESTED_TOPIC = "0x" + keccak256(
    b"Attested(address,address,bytes32,bytes32)"
).hex()
ZERO_UID = b"\x00" * 32
MIN_BALANCE_WEI = 20_000_000_000_000
COOLDOWN_SECONDS = 60
DAILY_CAP = 50

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")

_issuance_lock = threading.Lock()
_last_success_by_ip: dict[str, float] = {}
_daily_utc_day = ""
_daily_successes = 0


class HttpRefusal(RuntimeError):
    """A deliberate public-endpoint refusal with an HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _config() -> dict:
    return json.loads((HERE / "config.json").read_text())


def _run(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:400])
    return out.stdout.strip()


def _uint_word(value: int, bits: int, name: str) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value.to_bytes(32, "big")


def encode_attest(
    schema_uid: str,
    recipient: str,
    payload: bytes,
    *,
    expiration: int = 0,
    revocable: bool = True,
    ref_uid: bytes = ZERO_UID,
    value: int = 0,
) -> str:
    """Encode EAS ``attest`` calldata without delegating nested tuple offsets to a CLI.

    The outer request and its inner AttestationRequestData are both dynamic because the latter
    contains ``bytes``. Keeping each offset relative to its own tuple prevents the tempting but
    invalid flat encoding that reaches EAS with shifted fields and fails only at gas estimation.
    """
    if not BYTES32.fullmatch(schema_uid):
        raise ValueError("schema_uid must be a 32-byte hex value")
    if not ADDRESS.fullmatch(recipient):
        raise ValueError("recipient must be an address")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if not isinstance(revocable, bool):
        raise TypeError("revocable must be bool")
    if not isinstance(ref_uid, bytes) or len(ref_uid) != 32:
        raise ValueError("ref_uid must be exactly 32 bytes")

    schema = bytes.fromhex(schema_uid[2:])
    address = bytes.fromhex(recipient[2:]).rjust(32, b"\x00")
    payload_padding = (-len(payload)) % 32
    payload_tail = _uint_word(len(payload), 256, "payload length") + payload
    payload_tail += b"\x00" * payload_padding

    inner_head = b"".join((
        address,
        _uint_word(expiration, 64, "expiration"),
        _uint_word(int(revocable), 8, "revocable"),
        ref_uid,
        _uint_word(6 * 32, 256, "payload offset"),
        _uint_word(value, 256, "value"),
    ))
    outer_tuple = schema + _uint_word(2 * 32, 256, "inner tuple offset")
    arguments = _uint_word(32, 256, "outer tuple offset") + outer_tuple + inner_head + payload_tail
    selector = keccak256(ATTEST_SIGNATURE.encode())[:4]
    return "0x" + (selector + arguments).hex()


def _deployer_balance(key: str) -> int:
    address = _run(["cast", "wallet", "address", "--private-key", key])
    return int(_run(["cast", "balance", address, "--rpc-url", RPC]), 0)


def issue(wallet: str) -> str:
    """Send an EAS attestation and return its uid."""
    if not ADDRESS.fullmatch(wallet):
        raise ValueError("not an address")
    cfg = _config()
    schema = cfg["schema_uid"]
    key = os.environ["PRIVATE_KEY"]

    if _deployer_balance(key) < MIN_BALANCE_WEI:
        raise HttpRefusal(503, "the demo faucet is exhausted")

    encoded_payload = _run([
        "cast", "abi-encode", "f(bool,string,string)", "true", "demo", "retail",
    ])
    calldata = encode_attest(schema, wallet, bytes.fromhex(encoded_payload.removeprefix("0x")))
    receipt_text = _run([
        "cast", "send", EAS, calldata,
        "--rpc-url", RPC,
        "--private-key", key,
        "--gas-limit", "400000",
        "--json",
    ])
    receipt = json.loads(receipt_text)
    status = receipt.get("status")
    if status in (0, "0", "0x0"):
        raise RuntimeError("attestation reverted on chain")

    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if (
            log.get("address", "").lower() == EAS.lower()
            and topics
            and topics[0].lower() == ATTESTED_TOPIC.lower()
        ):
            data = log.get("data", "")
            if len(data) >= 66:
                return "0x" + data[2:66]
            for topic in topics[1:]:
                return topic
    raise RuntimeError(f"no Attested event found in receipt (status={status!r})")


def _issue_for_client(client_ip: str, wallet: str) -> str:
    """Reserve a slot, then sign outside the lock.

    The signing call blocks on the network for as long as the RPC takes. Holding the lock across
    it would serialise every client behind the slowest transaction, so the lock covers only the
    bookkeeping: admit and reserve, sign unlocked, release the reservation if signing failed.

    The address is validated before the cooldown is consulted, so a malformed request is told what
    is wrong with it rather than being handed an unrelated rate-limit message.
    """
    global _daily_utc_day, _daily_successes

    if not ADDRESS.match(wallet):
        raise HttpRefusal(400, "not an address")

    with _issuance_lock:
        now = time.monotonic()
        last_success = _last_success_by_ip.get(client_ip)
        if last_success is not None and now - last_success < COOLDOWN_SECONDS:
            remaining = math.ceil(COOLDOWN_SECONDS - (now - last_success))
            raise HttpRefusal(429, f"issuance cooldown: retry in {remaining} seconds")

        utc_day = datetime.now(UTC).date().isoformat()
        if utc_day != _daily_utc_day:
            _daily_utc_day = utc_day
            _daily_successes = 0
        if _daily_successes >= DAILY_CAP:
            raise HttpRefusal(429, "global daily issuance cap reached")

        # Reserve before releasing the lock: an in-flight signature must occupy its slot, or two
        # concurrent requests from one address would both pass the cooldown check.
        previous = last_success
        _last_success_by_ip[client_ip] = now
        _daily_successes += 1

    try:
        return issue(wallet)
    except Exception:
        with _issuance_lock:
            if previous is None:
                _last_success_by_ip.pop(client_ip, None)
            else:
                _last_success_by_ip[client_ip] = previous
            _daily_successes = max(0, _daily_successes - 1)
        raise


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        """Never let a browser hold a stale script against fresh data.

        Every deploy changes the scripts and universe.json together. A cached ui.js running
        against a newer universe.json produces wrong numbers with no error and no warning —
        observed 2026-08-23, where a cached script silently ignored a chart parameter that had
        been added minutes earlier. These files are small; correctness is worth more than the
        bytes saved.
        """
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded or self.client_address[0]

    def do_POST(self):
        if self.path != "/api/issue":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            uid = _issue_for_client(self._client_ip(), str(body.get("wallet", "")))
            response = {"uid": uid}
            code = 200
        except HttpRefusal as exc:
            response = {"error": str(exc)}
            code = exc.status
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            response = {"error": str(exc)}
            code = 400
        except Exception as exc:
            response = {"error": str(exc)}
            code = 500
        raw = json.dumps(response).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    host = os.environ.get("PAR_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8600"))
    print(f"Par demo issuer on http://{host}:{port} (holds a rate-limited testnet key)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
