"""Read-only chain reads for the server: a pinned block, Multicall3 balances, Exactly positions.

Every read of one chain happens at one block, chosen once: the chain's finalized block where the
endpoint still serves state for it, else its safe block, else a fixed depth below the head. Which
one was used is part of the answer, because public endpoints prune old state and an L2's finalized
block is often older than they keep.

No signing path exists here. Endpoints come from the chain registry, override first; a transport
failure moves on to the next one, and every error text is passed through `safe_detail` before it
leaves, because an override URL can carry a key.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable, Sequence

from engine.chain import ChainError, rpc_call, selector
from engine.portfolio.contracts import safe_detail
from engine.registry import Chain


AGGREGATE3 = selector("aggregate3((address,bool,bytes)[])")
GET_ETH_BALANCE = selector("getEthBalance(address)")
BALANCE_OF = selector("balanceOf(address)")
EXACTLY_ACCOUNTS = selector("accounts(address)")
EXACTLY_FIXED_DEPOSIT = selector("fixedDepositPositions(uint256,address)")
EXACTLY_INTERVAL_SECONDS = 4 * 7 * 86_400
# Calls per aggregate3. A thousand balanceOf reads is a few million gas, which public endpoints
# accept; one that refuses a batch gets it again in halves, down to MIN_MULTICALL_CHUNK.
MULTICALL_CHUNK = 1000
MIN_MULTICALL_CHUNK = 100
# How far below the head a read is pinned when the chain names neither a finalized nor a safe block.
FALLBACK_DEPTH = 12
_STATE_GONE = ("missing trie node", "header not found", "pruned", "state not available",
               "historical state", "unknown block", "block not found", "not found")


class OnchainError(RuntimeError):
    """A chain could not be read. The message is already safe to show."""

    def __init__(self, message: str, *, revert_data: str | None = None) -> None:
        super().__init__(message)
        self.revert_data = revert_data


def state_gone(error: BaseException) -> bool:
    """Whether a read needs a newer block rather than a smaller batch."""
    return (getattr(error, "revert_data", None) is None
            and any(marker in str(error).lower() for marker in _STATE_GONE))


@dataclass(frozen=True, slots=True)
class PinnedBlock:
    chain_id: int
    number: int
    hash: str
    timestamp: int
    # "finalized", "safe" or "depth-<n>": what the block was chosen as.
    finality: str

    def to_dict(self) -> dict[str, Any]:
        return {"chain_id": self.chain_id, "block_number": self.number, "block_hash": self.hash,
                "timestamp": self.timestamp, "finality": self.finality}


def _word(value: int) -> str:
    return f"{value:064x}"


def _address_word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def encode_aggregate3(calls: Sequence[tuple[str, str]]) -> str:
    """aggregate3 with allowFailure on every call: one reverting token does not sink the batch."""
    head, tuples = [], []
    offset = 32 * len(calls)
    for target, data in calls:
        payload = data.removeprefix("0x")
        if len(payload) % 2:
            raise ValueError("call data must be whole bytes")
        padded = payload + "0" * ((64 - len(payload) % 64) % 64)
        encoded = _address_word(target) + _word(1) + _word(96) + _word(len(payload) // 2) + padded
        head.append(_word(offset))
        tuples.append(encoded)
        offset += len(encoded) // 2
    return AGGREGATE3 + _word(32) + _word(len(calls)) + "".join(head) + "".join(tuples)


def _rpc_bytes(result: str, context: str) -> bytes:
    try:
        return bytes.fromhex(result.removeprefix("0x"))
    except ValueError as exc:
        raise OnchainError(f"{context} returned malformed hex") from exc


def decode_aggregate3(result: str, expected: int) -> list[tuple[bool, bytes]]:
    raw = _rpc_bytes(result, "aggregate3")

    def word(at: int) -> int:
        if at < 0 or at + 32 > len(raw):
            raise OnchainError("aggregate3 returned a truncated result")
        return int.from_bytes(raw[at:at + 32], "big")

    start = word(0)
    count = word(start)
    if count != expected:
        raise OnchainError(f"aggregate3 returned {count} results for {expected} calls")
    base = start + 32
    results = []
    for index in range(count):
        tuple_at = base + word(base + 32 * index)
        success = word(tuple_at)
        if success not in (0, 1):
            raise OnchainError("aggregate3 returned an invalid success flag")
        data_at = tuple_at + word(tuple_at + 32)
        length = word(data_at)
        if data_at + 32 + length > len(raw):
            raise OnchainError("aggregate3 returned truncated call data")
        results.append((success == 1, raw[data_at + 32:data_at + 32 + length]))
    return results


class ChainReader:
    def __init__(self, chain: Chain, *, call: Callable[..., Any] = rpc_call, timeout: int = 20) -> None:
        self.chain = chain
        self._call = call
        self.timeout = timeout
        self.deadline_at: float | None = None
        self.endpoints = chain.rpc_endpoints()
        if not self.endpoints:
            raise OnchainError(f"chain {chain.chain_id} has no RPC endpoint")
        if not chain.multicall3:
            raise OnchainError(f"chain {chain.chain_id} names no Multicall3 contract")

    def rpc(self, method: str, params: list[Any], *, require_result: bool = False) -> Any:
        last: BaseException | None = None
        pruned: BaseException | None = None
        answered_none = False
        for endpoint in self.endpoints:
            now = None if self.deadline_at is None else time.monotonic()
            if now is not None and now >= self.deadline_at:
                raise OnchainError(f"{self.chain.name}: read deadline passed")
            # A call may not outlive the read's deadline: one started near it gets only what is left.
            timeout = self.timeout if now is None else max(0.1, min(self.timeout, self.deadline_at - now))
            try:
                result = self._call(endpoint, method, params, timeout=timeout)
                if result is not None or not require_result:
                    return result
                answered_none = True
            except ChainError as exc:
                # A revert is the chain's answer at this block, not an endpoint outage.
                if exc.revert_data is not None:
                    raise OnchainError(f"{self.chain.name}: {method} reverted: {safe_detail(exc)}",
                                       revert_data=exc.revert_data) from exc
                last = exc
                if pruned is None and any(marker in str(exc).lower() for marker in _STATE_GONE):
                    pruned = exc
        # An endpoint saying the state is gone is what the block ladder falls back on; neither another
        # endpoint's null answer nor its unrelated outage may hide it, or the read fails instead of stepping down.
        if answered_none and pruned is None:
            return None
        raise OnchainError(f"{self.chain.name}: {method} failed on every endpoint: {safe_detail(pruned or last)}")

    def _block(self, tag: str) -> dict[str, Any] | None:
        try:
            # A node behind on its headers answers null for a tag another serves: ask the others
            # before settling for a weaker block.
            block = self.rpc("eth_getBlockByNumber", [tag, False], require_result=True)
        except OnchainError:
            if self.deadline_at is not None and time.monotonic() >= self.deadline_at:
                raise
            return None
        return block if isinstance(block, dict) and isinstance(block.get("number"), str) else None

    def head_block(self) -> PinnedBlock:
        """The newest block an endpoint serves. Only for checks that guard against an obvious revert,
        where a balance received minutes ago must count; valuations stay on the finalized ladder."""
        block = self._block("latest")
        if block is None:
            raise OnchainError(f"{self.chain.name}: no head block could be read")
        return _pinned(self.chain.chain_id, block, "latest")

    def pinned_block_candidates(self) -> Iterable[PinnedBlock]:
        """Yield candidates in preference order as each block is resolved."""
        found = False
        for tag in ("finalized", "safe"):
            block = self._block(tag)
            if block is not None:
                found = True
                yield _pinned(self.chain.chain_id, block, tag)
        head = self._block("latest")
        try:
            head_number = int(head["number"], 16) if head is not None else None
        except ValueError:
            head_number = None
        if head_number is not None:
            depth = self._block(hex(max(0, head_number - FALLBACK_DEPTH)))
            if depth is not None:
                found = True
                yield _pinned(self.chain.chain_id, depth, f"depth-{FALLBACK_DEPTH}")
        if not found:
            raise OnchainError(f"{self.chain.name}: no block could be read")

    def pinned_blocks(self) -> list[PinnedBlock]:
        """Candidate blocks in order of preference: finalized, safe, then a fixed depth."""
        return list(self.pinned_block_candidates())

    def eth_call(self, to: str, data: str, block: PinnedBlock) -> str:
        # By hash (EIP-1898), not number: after a reorg a fallback endpoint's block at that height
        # can be another one, and its state would be attributed to the block recorded here.
        result = self.rpc("eth_call", [{"to": to, "data": data}, {"blockHash": block.hash}],
                          require_result=True)
        if not isinstance(result, str) or not result.startswith("0x"):
            raise OnchainError(f"{self.chain.name}: eth_call returned an unusable result")
        return result

    def balances(self, wallet: str, tokens: Iterable[str | None], block: PinnedBlock) -> dict[str | None, int | None]:
        """Each token's balance (None is the native coin), or None where that one call failed."""
        wanted = list(dict.fromkeys(tokens))
        answers: dict[str | None, int | None] = {}

        def read(chunk: list[str | None]) -> None:
            calls = [(self.chain.multicall3, GET_ETH_BALANCE + _address_word(wallet)) if token is None
                     else (token, BALANCE_OF + _address_word(wallet)) for token in chunk]
            try:
                decoded = decode_aggregate3(
                    self.eth_call(self.chain.multicall3, encode_aggregate3(calls), block), len(chunk))
            except OnchainError as exc:
                gone = any(marker in str(exc).lower() for marker in _STATE_GONE)
                if gone or len(chunk) <= MIN_MULTICALL_CHUNK:
                    raise
                half = len(chunk) // 2
                read(chunk[:half])
                read(chunk[half:])
                return
            for token, (success, data) in zip(chunk, decoded):
                answers[token] = int.from_bytes(data[:32], "big") if success and len(data) >= 32 else None

        for start in range(0, len(wanted), MULTICALL_CHUNK):
            read(wanted[start:start + MULTICALL_CHUNK])
        return answers

    def exactly_positions(self, wallet: str, market: str, block: PinnedBlock) -> list[dict[str, int]]:
        """Every fixed-rate deposit the wallet holds in one Exactly market, at maturity amounts."""
        raw = _rpc_bytes(self.eth_call(market, EXACTLY_ACCOUNTS + _address_word(wallet), block),
                         f"{self.chain.name}: Exactly market")
        if len(raw) < 32:
            raise OnchainError(f"{self.chain.name}: Exactly market returned no account data")
        packed = int.from_bytes(raw[:32], "big")
        base, bitmap = packed & 0xFFFFFFFF, packed >> 32
        positions = []
        for index in range(224):
            if not bitmap >> index & 1:
                continue
            maturity = base + index * EXACTLY_INTERVAL_SECONDS
            data = _rpc_bytes(self.eth_call(
                market, EXACTLY_FIXED_DEPOSIT + _word(maturity) + _address_word(wallet), block),
                f"{self.chain.name}: Exactly position")
            if len(data) < 64:
                raise OnchainError(f"{self.chain.name}: Exactly position returned invalid amounts")
            principal, fee = int.from_bytes(data[:32], "big"), int.from_bytes(data[32:64], "big")
            if principal or fee:
                positions.append({"maturity": maturity, "principal": principal, "fee": fee})
        return positions


def read_at_pinned_block(reader: ChainReader, read: Callable[[PinnedBlock], Any]) -> tuple[PinnedBlock, Any]:
    """Run `read` at the most final block whose state the endpoints still serve."""
    last: BaseException | None = None
    for block in reader.pinned_block_candidates():
        try:
            return block, read(block)
        except OnchainError as exc:
            last = exc
            if exc.revert_data is not None or not any(marker in str(exc).lower() for marker in _STATE_GONE):
                raise
    raise OnchainError(f"{reader.chain.name}: no recent block's state could be read: {safe_detail(last)}")


def _pinned(chain_id: int, block: dict[str, Any], finality: str) -> PinnedBlock:
    try:
        return PinnedBlock(chain_id=chain_id, number=int(block["number"], 16), hash=str(block["hash"]).lower(),
                           timestamp=int(block["timestamp"], 16), finality=finality)
    except (KeyError, TypeError, ValueError) as exc:
        raise OnchainError("a block header was malformed") from exc
