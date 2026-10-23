"""Follows each submitted route until it is done, and reconciles the fee it should have earned.

One pass looks at every route in flight:

- SUBMITTED: the source chain's receipt. Not mined after SOURCE_WAIT is a failure at the source;
  reverted is one; a receipt from another wallet or to another contract than the route's is an
  unrelated hash, and is refused rather than believed. Mined and matching is SOURCE_CONFIRMED, and
  the fee is reconciled from that same receipt.
- Same chain: the receipt must show the instrument arriving in the wallet; then COMPLETED with
  the amount received, else FAILED at the destination.
- Across chains: LI.FI's status, through the API's own strict reading of it (only an answer about
  this hash from this source chain counts). Waiting for the destination is DESTINATION_PURCHASE;
  done with the instrument delivered is COMPLETED; done with something else delivered -- the
  destination call failed and the bridged token went to the fallback -- is FAILED at the
  destination, with what arrived; a bridge failure or refund is FAILED at the bridge.

A route still unresolved after GIVE_UP_AFTER is FAILED at TRACKING, so nothing waits forever
unseen. Every failure names its stage, which is what monitoring counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import logging
import threading
from typing import Any, Callable, Mapping

from engine.composer import NATIVE_TOKEN_SENTINELS
from engine.keccak import keccak256
from engine.portfolio.contracts import safe_detail
from engine.registry import registry

from engine.onchain import ChainReader, OnchainError

from .router import PROVIDER_THROTTLED


TRANSFER_TOPIC = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()
FEES_COLLECTED_TOPIC = "0x28a87b6059180e46de5fb9ab35eb043e8fe00ab45afcc7789e3934ecbbcde3ea"
# lifinance/contracts deployments/<network>.json, read 2026-10-11.
LIFI_FEE_COLLECTORS = {
    1: "0x3ef238c36035880efbdfa239d218186b79ad1d6f",
    10: "0x271970ee66c03c9d62d8e8c718b9841f2ea7d817",
    56: "0xbd6c7b0d2f68c2b7805d88388319cfb6ecb50ea9",
    100: "0xbd6c7b0d2f68c2b7805d88388319cfb6ecb50ea9",
    130: "0x262de706627fe02b46a3643a60948fa6be007f7b",
    137: "0xbd6c7b0d2f68c2b7805d88388319cfb6ecb50ea9",
    143: "0x954d55105cdf5371224268691faf6178be5f62f5",
    146: "0xafb8cc8fcd71cd768ce117c11eb723119fcdb1f8",
    196: "0xc69994fd72824ca98f8a0b1e2abc954e65a91cf4",
    324: "0x8dbf6f59187b2eb36b980f3d8f4cfc6dc4e4642e",
    480: "0x50d5a8acfae13dceb217e9a071f6c6bd5bdb4155",
    999: "0x7cac679cc2df9c602590ea4965091f54ec32f2fc",
    1672: "0x949343d563394016ce0c52bb6511dfe5aeec8e8a",
    2818: "0x24ff5343caa5cd6b56e0acbcebed4e739627404c",
    4326: "0xf061930bc5154b72bf90ba22840a6bef20f4c83f",
    5000: "0xf048e5816b0c7951ac179f656c5b86e5a79bd7b5",
    5042: "0x0604811f11863aa7904cf07c27875c80134744c7",
    8453: "0x0a6d96e7f4d7b96cfe42185df61e64d255c12dff",
    42161: "0xb0210de78e28e2633ca200609d9f528c13c26cd9",
    42220: "0xf048e5816b0c7951ac179f656c5b86e5a79bd7b5",
    43114: "0xb0210de78e28e2633ca200609d9f528c13c26cd9",
    57073: "0x8295805320853d6b28778fc8f5199327e62e3d87",
    59144: "0xa4a24bdd4608d7dfc496950850f9763b674f0db2",
    534352: "0xf048e5816b0c7951ac179f656c5b86e5a79bd7b5",
}
SAFE_EXECUTION_SUCCESS_TOPIC = "0x" + keccak256(b"ExecutionSuccess(bytes32,uint256)").hex()
SAFE_EXECUTION_FAILURE_TOPIC = "0x" + keccak256(b"ExecutionFailure(bytes32,uint256)").hex()
SOURCE_WAIT = timedelta(minutes=30)
SOURCE_STATES = ("SUBMITTED", "SOURCE_CONFIRMED")
CROSSING_STATES = ("BRIDGING", "DESTINATION_PURCHASE")
GIVE_UP_AFTER = timedelta(hours=24)
# LI.FI status costs the deployment's allowance; a crossing is asked about at most this often.
STATUS_EVERY = timedelta(seconds=60)
log = logging.getLogger("fenorium.route-tracker")


def _parse(text: str | None) -> datetime | None:
    return None if not text else datetime.fromisoformat(text.replace("Z", "+00:00"))


def _topic_address(topic: Any) -> str | None:
    if not isinstance(topic, str) or len(topic) != 66:
        return None
    return "0x" + topic[-40:].lower()


def _safe_execution(input_hex: str) -> tuple[str, int, str] | None:
    if not isinstance(input_hex, str) or input_hex[:10].lower() != "0x6a761202":
        return None
    payload = input_hex[10:]
    if len(payload) % 64 or any(char not in "0123456789abcdefABCDEF" for char in payload):
        return None
    raw = bytes.fromhex(payload)
    if len(raw) < 320:
        return None
    words = [int.from_bytes(raw[at:at + 32], "big") for at in range(0, 320, 32)]
    if words[3] != 0 or any(words[index] >= 1 << 160 for index in (0, 7, 8)):
        return None
    ranges = []
    for offset in (words[2], words[9]):
        if offset < 320 or offset % 32 or offset + 32 > len(raw):
            return None
        length = int.from_bytes(raw[offset:offset + 32], "big")
        end = offset + 32 + length
        padded_end = offset + 32 + ((length + 31) // 32) * 32
        if padded_end > len(raw) or any(raw[end:padded_end]):
            return None
        ranges.append((offset, end, padded_end))
    data, signatures = ranges
    if not (data[2] <= signatures[0] or signatures[2] <= data[0]):
        return None
    return "0x" + raw[12:32].hex(), words[1], "0x" + raw[data[0] + 32:data[1]].hex()


def transfers_to(receipt: Mapping[str, Any], *, token: str, recipient: str) -> int:
    """The sum of `token` Transfer logs to `recipient` in one receipt."""
    total = 0
    for entry in receipt.get("logs") or []:
        topics = entry.get("topics") if isinstance(entry, Mapping) else None
        if (not isinstance(topics, list) or len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC
                or str(entry.get("address", "")).lower() != token.lower()
                or _topic_address(topics[2]) != recipient.lower()):
            continue
        try:
            total += int(str(entry.get("data")), 16)
        except ValueError:
            continue
    return total


class RouteTracker:
    def __init__(self, store: Any, api: Any, *, reader_factory: Callable[..., Any] = ChainReader,
                 clock: Callable[[], datetime] | None = None, interval_seconds: float = 15.0) -> None:
        self.store = store
        self.api = api
        self.reader_factory = reader_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval_seconds = interval_seconds
        self.batch_size = 200
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="route-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.step()

    def step(self) -> None:
        # LI.FI's allowance is shared: once it refuses, every other crossing would be refused too.
        # Unexpired routes wait for the next pass with their place in the queue, instead of being
        # marked checked behind routes that did spend the allowance.
        throttled = False
        # Two batches: routes waiting on a chain receipt need no LI.FI allowance, so throttled
        # crossings holding their place must not fill the batch they are drawn from.
        records = (self.store.active(states=SOURCE_STATES, limit=self.batch_size)
                   + self.store.active(states=CROSSING_STATES, limit=self.batch_size))
        for record in records:
            checked = False
            try:
                if throttled and record["state"] in CROSSING_STATES:
                    checked = self._expire_if_due(record)
                else:
                    checked = self.check(record)
            except Exception as exc:  # noqa: BLE001 - one route's failure must not stop the others
                if getattr(exc, "code", None) == PROVIDER_THROTTLED:
                    throttled = True
                else:
                    checked = True
                    log.warning("route %s could not be checked: %s", record["route_id"], safe_detail(exc))
            finally:
                if checked:
                    self.store.touch(record["route_id"])

    def check(self, record: Mapping[str, Any]) -> bool:
        if self._expire_if_due(record):
            return True
        now = self.clock()
        if record["state"] in ("SUBMITTED", "SOURCE_CONFIRMED"):
            self._source(record, now)
            return True
        elif record["cross_chain"]:
            last = _parse(record.get("last_checked_at"))
            if last is None or now - last >= STATUS_EVERY:
                self._crossing(record)
                return True
        return False

    def _expire_if_due(self, record: Mapping[str, Any]) -> bool:
        now = self.clock()
        submitted = _parse(record.get("submitted_at"))
        if submitted is not None and now - submitted > GIVE_UP_AFTER:
            self.store.advance(record["route_id"], "FAILED", failed_stage="TRACKING", failure_reason="TRACKING_EXPIRED",
                               detail={"after_hours": GIVE_UP_AFTER.total_seconds() / 3600})
            return True
        return False

    def _source(self, record: Mapping[str, Any], now: datetime) -> None:
        reader = self.reader_factory(registry().require(record["pay_chain_id"]))
        receipt = reader.rpc("eth_getTransactionReceipt", [record["tx_hash"]], require_result=True)
        route_id = record["route_id"]
        if not isinstance(receipt, Mapping):
            # A confirmed route's receipt was already seen at a final block: a node answering
            # nothing now is unavailable, not evidence the transaction never mined.
            submitted = _parse(record.get("submitted_at"))
            if record["state"] == "SUBMITTED" and submitted is not None and now - submitted > SOURCE_WAIT:
                self.store.advance(route_id, "FAILED", failed_stage="SOURCE", failure_reason="SOURCE_NOT_MINED")
            return
        sender = str(receipt.get("from", "")).lower()
        target = str(receipt.get("to", "")).lower()
        safe = sender != record["wallet"] and target == record["wallet"]
        direct = sender == record["wallet"] and (not record.get("tx_target") or target == record["tx_target"])
        if not direct and not safe:
            self.store.advance(route_id, "FAILED", failed_stage="SOURCE", failure_reason="SOURCE_TRANSACTION_MISMATCH",
                               detail={"from": receipt.get("from"), "to": receipt.get("to")})
            return
        if safe or record.get("tx_data_sha256"):
            transaction = reader.rpc("eth_getTransactionByHash", [record["tx_hash"]], require_result=True)
            if not isinstance(transaction, Mapping):
                # Behind a load balancer the node that served the receipt may not have the
                # transaction yet: ask again on the next pass rather than fail a mined purchase.
                return
            matches = False
            if safe:
                execution = _safe_execution(transaction.get("input"))
                if execution is None:
                    return
                inner_target, value, data = execution
                matches = (
                    (not record.get("tx_target") or inner_target == record["tx_target"])
                    and (not record.get("tx_data_sha256")
                         or hashlib.sha256(data.encode("ascii")).hexdigest() == record["tx_data_sha256"])
                    and value == int(record.get("tx_value") or 0)
                )
            elif isinstance(transaction.get("input"), str):
                try:
                    matches = (
                        hashlib.sha256(transaction["input"].lower().encode("ascii")).hexdigest()
                        == record["tx_data_sha256"]
                        and int(transaction.get("value"), 16) == int(record.get("tx_value") or 0)
                    )
                except (TypeError, ValueError, UnicodeError):
                    matches = False
            if not matches:
                self.store.advance(route_id, "FAILED", failed_stage="SOURCE",
                                   failure_reason="SOURCE_TRANSACTION_MISMATCH")
                return
        try:
            block = int(str(receipt.get("blockNumber")), 16)
        except ValueError:
            return
        if record["state"] != "SOURCE_CONFIRMED" or receipt.get("status") != "0x1":
            try:
                # The first usable block is enough: resolving the rest of the ladder (safe, then a
                # fixed depth) can cost every endpoint's timeout, and routes are tracked one at a time.
                final = next(iter(reader.pinned_block_candidates()), None)
                if final is None:
                    return
                if block > final.number:
                    return
                canonical = reader.rpc("eth_getBlockByNumber", [hex(block), False])
            except OnchainError:
                return
            receipt_hash = receipt.get("blockHash")
            if (not isinstance(canonical, Mapping) or not isinstance(receipt_hash, str)
                    or not receipt_hash or not isinstance(canonical.get("hash"), str)
                    or canonical["hash"].lower() != receipt_hash.lower()):
                return
            if receipt.get("status") != "0x1":
                self.store.advance(route_id, "FAILED", failed_stage="SOURCE", failure_reason="SOURCE_REVERTED",
                                   fields={"source_block": block, "tx_verified": 1})
                return
        if safe:
            outcomes = set()
            for entry in receipt.get("logs") or []:
                topics = entry.get("topics") if isinstance(entry, Mapping) else None
                if (isinstance(topics, list) and topics
                        and str(entry.get("address", "")).lower() == record["wallet"]):
                    outcomes.add(str(topics[0]).lower())
            if SAFE_EXECUTION_SUCCESS_TOPIC not in outcomes:
                if SAFE_EXECUTION_FAILURE_TOPIC in outcomes:
                    self.store.advance(route_id, "FAILED", failed_stage="SOURCE",
                                       failure_reason="SAFE_EXECUTION_FAILED",
                                       fields={"source_block": block, "tx_verified": 1})
                return
        if record["state"] != "SOURCE_CONFIRMED":
            if not self.store.advance(route_id, "SOURCE_CONFIRMED",
                                      fields={"source_block": block, "tx_verified": 1},
                                      detail={"block": block, "finality": final.finality}):
                return
        self._reconcile_fee(record, receipt, reader, block)
        if record["cross_chain"]:
            self.store.advance(route_id, "BRIDGING")
            return
        token, destination_chain = self._destination(record)
        received = transfers_to(receipt, token=token, recipient=record["wallet"]) if token else 0
        if received > 0:
            fields = {"received": {"chain_id": destination_chain, "token": token, "amount": str(received)}}
            if record.get("to_amount_min") is not None and received < int(record["to_amount_min"]):
                self.store.advance(route_id, "FAILED", failed_stage="DESTINATION",
                                   failure_reason=self._below_minimum_reason(record), fields=fields)
            else:
                self.store.advance(route_id, "COMPLETED", fields=fields)
        else:
            self.store.advance(route_id, "FAILED", failed_stage="DESTINATION", failure_reason="INSTRUMENT_NOT_RECEIVED")

    def _crossing(self, record: Mapping[str, Any]) -> None:
        route_id = record["route_id"]
        data = self.api.execution_status(record["tx_hash"], from_chain=record["pay_chain_id"],
                                         to_chain=self._destination(record)[1])
        if not data.get("terminal"):
            if data.get("substatus") == "WAIT_DESTINATION_TRANSACTION":
                self.store.advance(route_id, "DESTINATION_PURCHASE", detail={"substatus": data.get("substatus")})
            return
        received = data.get("received")
        fields = {"destination_tx": data.get("destination_tx"), "received": received}
        delivered = (isinstance(received, Mapping) and bool(received.get("token"))
                     and received.get("chain_id") is not None
                     and (record.get("to_amount_min") is None or received.get("amount") is not None))
        if data.get("status") == "DONE" and data.get("substatus") in ("COMPLETED", "PARTIAL") and not delivered:
            # Done, but what arrived is not stated yet: nothing shows the purchase failed. Ask again.
            if record["state"] != "DESTINATION_PURCHASE":
                self.store.advance(route_id, "DESTINATION_PURCHASE",
                                   detail={"substatus": data.get("substatus"), "awaiting": "delivery evidence"})
            return
        expected_token, expected_chain = self._destination(record)
        received_token = received.get("token") if isinstance(received, Mapping) else None
        if data.get("status") == "DONE" and data.get("substatus") == "COMPLETED" and received \
                and str(received_token or "").lower() == str(expected_token or "").lower() \
                and received.get("chain_id") == expected_chain:
            if record.get("to_amount_min") is not None and received.get("amount") is not None \
                    and int(received["amount"]) < int(record["to_amount_min"]):
                self.store.advance(route_id, "FAILED", failed_stage="DESTINATION",
                                   failure_reason=self._below_minimum_reason(record), fields=fields)
            else:
                self.store.advance(route_id, "COMPLETED", fields=fields)
        elif data.get("status") == "DONE" and data.get("substatus") in ("COMPLETED", "PARTIAL"):
            # Delivered, but not the instrument: the destination step failed and the bridged token
            # went to the fallback address. The buyer has it; the purchase did not happen.
            self.store.advance(route_id, "FAILED", failed_stage="DESTINATION",
                               failure_reason="DESTINATION_PURCHASE_FAILED", fields=fields,
                               detail={"substatus": data.get("substatus")})
        else:
            self.store.advance(route_id, "FAILED", failed_stage="BRIDGE",
                               failure_reason=str(data.get("substatus") or data.get("status")), fields=fields,
                               detail={"recovery_required": bool(data.get("recovery_required"))})

    def _reconcile_fee(self, record: Mapping[str, Any], receipt: Mapping[str, Any], reader: Any, block: int) -> None:
        fee = record.get("fee")
        if not fee or fee.get("status") not in ("EXPECTED", "PENDING"):
            return
        expected, recipient, token = int(fee["expected_raw"]), fee["recipient"], fee["token"]
        token = token.lower()
        collector = LIFI_FEE_COLLECTORS.get(record["pay_chain_id"])
        event_token = "0x" + "00" * 20 if token in NATIVE_TOKEN_SENTINELS else token
        if collector is not None:
            for entry in receipt.get("logs") or []:
                topics = entry.get("topics") if isinstance(entry, Mapping) else None
                if (not isinstance(topics, list) or len(topics) != 3
                        or str(topics[0]).lower() != FEES_COLLECTED_TOPIC
                        or str(entry.get("address", "")).lower() != collector
                        or _topic_address(topics[1]) != event_token
                        or _topic_address(topics[2]) != recipient.lower()):
                    continue
                data = entry.get("data")
                if (not isinstance(data, str) or len(data) != 130 or not data.startswith("0x")
                        or any(char not in "0123456789abcdefABCDEF" for char in data[2:])):
                    continue
                observed = int(data[2:66], 16)
                self.store.reconcile_fee(
                    record["route_id"], status="RECONCILED" if observed == expected else "MISMATCH",
                    observed_raw=observed, method="fee-collector-event",
                    detail=None if observed == expected else
                    f"expected {expected} for the integrator, the collector event shows {observed}")
                return
        missing_event = ("no collector event was found" if collector is not None else
                         f"no collector is known for chain {record['pay_chain_id']}")
        if token in NATIVE_TOKEN_SENTINELS:
            try:
                after = int(str(reader.rpc("eth_getBalance", [recipient, hex(block)])), 16)
                before = int(str(reader.rpc("eth_getBalance", [recipient, hex(block - 1)])), 16)
            except (OnchainError, ValueError):
                self.store.reconcile_fee(record["route_id"], status="UNVERIFIED", observed_raw=None,
                                         method="native-balance-delta",
                                         detail=f"{missing_event}; the balance across the block could not be read")
                return
            delta = after - before
            self.store.reconcile_fee(record["route_id"], status="UNVERIFIED",
                                     observed_raw=delta, method="native-balance-delta",
                                     detail=f"{missing_event}; the recipient's balance changed by {delta} "
                                            "in that block, which other transactions may share")
            return
        observed = transfers_to(receipt, token=token, recipient=recipient)
        if observed == expected:
            self.store.reconcile_fee(record["route_id"], status="RECONCILED", observed_raw=observed,
                                     method="receipt-transfer-log",
                                     detail=missing_event if collector is None else None)
        else:
            self.store.reconcile_fee(record["route_id"], status="MISMATCH", observed_raw=observed,
                                     method="receipt-transfer-log",
                                     detail=f"{missing_event}; expected {expected} to the recipient, "
                                            f"the receipt shows {observed}")

    @staticmethod
    def _destination(record: Mapping[str, Any]) -> tuple[str | None, int]:
        """Return the token and chain the bound route promises to deliver."""
        route = (record.get("evidence") or {}).get("route") or {}
        if str(record.get("direction", "BUY")).upper() == "SELL":
            return route.get("to_token"), int(route.get("to_chain", record["chain_id"]))
        return record.get("instrument_address"), int(record["chain_id"])

    @staticmethod
    def _below_minimum_reason(record: Mapping[str, Any]) -> str:
        return ("OUTPUT_BELOW_MINIMUM" if str(record.get("direction", "BUY")).upper() == "SELL"
                else "INSTRUMENT_BELOW_MINIMUM")
