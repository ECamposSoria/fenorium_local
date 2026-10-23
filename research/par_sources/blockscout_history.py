"""Wallet transfer history from Blockscout's multichain index, plus an on-chain verifier.

The index is a starting point, never the record. It reports what it believes happened; the
chain is then asked whether each transfer is really in the transaction it was attributed to.
That answers a source being wrong. It cannot answer a source being incomplete, which is why
`HistoryResult` carries a coverage claim the source has to justify separately.

Nothing here produces a value. What a transfer was worth at the time is a pricing problem with
its own evidence standard, and it is deliberately not solved by pretending the index knows.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.chain import ChainError, RpcTransportError, rpc_call
from engine.keccak import keccak256
from engine.portfolio.contracts import safe_detail
from engine.portfolio.history import Direction, HistoryResult, TransferEvent
from engine.registry import registry
from engine.rpc_endpoints import rpc_endpoints


BASE = "https://api.blockscout.com"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# Same coverage as the balance index: BSC (56) is absent and 404s.
HISTORY_CHAIN_IDS = frozenset(chain.chain_id for chain in registry().chains if chain.blockscout_index)

TRANSFER_TOPIC = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()


class BlockscoutHistoryError(RuntimeError):
    """The index did not answer usefully. Never raised to mean 'did nothing'."""


def load_api_key() -> str | None:
    key = os.environ.get("BLOCKSCOUT_API_KEY")
    if key:
        return key.strip()
    if not _ENV_PATH.exists():
        return None
    for line in _ENV_PATH.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "BLOCKSCOUT_API_KEY":
            return value.strip() or None
    return None


def _timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class BlockscoutWalletHistory:
    """Conforms to `engine.portfolio.history.WalletHistorySource`."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_pages: int = 4,
    ) -> None:
        self.api_key = api_key if api_key is not None else load_api_key()
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages

    def _history_url(self, chain_id: int, wallet: str, params: dict[str, Any]) -> str:
        """The gateway bills a key it needs; a chain whose registry entry names its own public
        instance is read there when no key is configured, so a keyless deployment (the testnet)
        still has an index. With a key the gateway serves every chain, as before."""
        chain = registry().get(chain_id)
        instance = None if self.api_key else (chain.blockscout_api if chain is not None else None)
        base = f"{instance}/v2" if instance else f"{BASE}/{chain_id}/api/v2"
        return f"{base}/addresses/{wallet}/token-transfers?" + urllib.parse.urlencode(params)

    def _get(self, url: str, *, timeout: float | None = None) -> Any:
        request = urllib.request.Request(
            url, headers={"User-Agent": "par-research/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds if timeout is None else timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise BlockscoutHistoryError(f"HTTP {exc.code} from {url.split('?')[0]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            # IncompleteRead is an HTTPException, not an OSError, so a body that stops early
            # escaped every handler here and took the pagination recovery with it.
            raise BlockscoutHistoryError(f"transport failure: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BlockscoutHistoryError(f"malformed JSON: {exc}") from exc

    def transfers(self, chain_id: int, wallet: str, *,
                  deadline_at: float | None = None) -> HistoryResult:
        """ERC-20 transfers this wallet was a party to, newest first, bounded by `max_pages`.

        Coverage is never established. The walk stops at a page limit, and a wallet with more
        history than that has older transfers nobody looked at -- which is a gap, not an
        absence of activity. Rows that cannot be read are counted rather than skipped quietly.
        """

        if chain_id not in HISTORY_CHAIN_IDS:
            raise BlockscoutHistoryError(f"chain {chain_id} is not covered by the index")
        lowered = wallet.lower()
        events: list[TransferEvent] = []
        seen: set[str] = set()
        unusable = 0
        repeated = 0
        cursor: dict[str, Any] | None = None
        pages = 0
        exhausted = False
        failure: str | None = None
        deadline_passed = False

        while pages < self.max_pages:
            remaining = None if deadline_at is None else deadline_at - time.monotonic()
            if remaining is not None and remaining <= 0:
                deadline_passed = True
                break
            params: dict[str, Any] = {"type": "ERC-20"}
            if self.api_key:
                params["apikey"] = self.api_key
            if cursor:
                params.update(cursor)
            url = self._history_url(chain_id, wallet, params)
            try:
                if remaining is None:
                    payload = self._get(url)
                else:
                    payload = self._get(
                        url, timeout=min(self.timeout_seconds, max(1.0, remaining)))
                rows = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    raise BlockscoutHistoryError("token-transfers did not return a list")
            except BlockscoutHistoryError as exc:
                # A page that fails after earlier pages succeeded is a shorter walk, not an
                # empty past. Throwing here would discard evidence already in hand and report
                # the wallet as having done nothing.
                if not events and pages == 0:
                    raise
                # Redacted here too. Returning successfully with a partial walk puts this text
                # straight into the serialized errors, which is the one path that never reaches
                # the redaction the loader applies to a raised exception.
                failure = safe_detail(exc)
                break
            pages += 1
            for row in rows:
                event = self._event(chain_id, lowered, row)
                if event is None:
                    unusable += 1
                    continue
                if event.identity in seen:
                    # Pages can overlap. A repeat is the same log seen twice, and appending it
                    # would make WalletHistory reject the whole walk over one duplicate.
                    repeated += 1
                    continue
                seen.add(event.identity)
                events.append(event)
            nxt = payload.get("next_page_params") if isinstance(payload, dict) else None
            if not isinstance(nxt, dict) or not nxt:
                exhausted = True
                break
            cursor = nxt

        if deadline_passed:
            detail = (f"the walk stopped after {pages} page(s) at the response deadline; "
                      "older transfers were not read")
        elif failure is not None:
            detail = f"the walk stopped after {pages} page(s) on a failure: {failure}"
        elif exhausted:
            detail = "the walk reached the end of this wallet's ERC-20 transfers"
        else:
            detail = f"the walk stopped after {pages} page(s); older transfers were not read"
        if unusable:
            detail += f"; {unusable} row(s) could not be read"
        if repeated:
            detail += f"; {repeated} row(s) repeated across pages"
        # Even an exhausted walk is the index's account of the past, not the chain's. Coverage
        # is a claim about having seen everything, and an index cannot demonstrate that.
        return HistoryResult(
            transfers=tuple(events),
            establishes_coverage=False,
            detail=detail,
        )

    def _event(self, chain_id: int, wallet: str, row: Any) -> TransferEvent | None:
        if not isinstance(row, dict):
            return None
        token = row.get("token")
        total = row.get("total")
        if not isinstance(token, dict) or not isinstance(total, dict):
            return None
        address = token.get("address") or token.get("address_hash")
        if (not isinstance(address, str) or len(address) != 42
                or not address.startswith("0x")
                or any(c not in "0123456789abcdefABCDEF" for c in address[2:])):
            return None
        sender = (row.get("from") or {}).get("hash") if isinstance(row.get("from"), dict) else None
        receiver = (row.get("to") or {}).get("hash") if isinstance(row.get("to"), dict) else None
        if not isinstance(sender, str) or not isinstance(receiver, str):
            return None
        raw_value = total.get("value")
        if not isinstance(raw_value, (str, int)) or isinstance(raw_value, bool):
            return None
        try:
            amount = int(raw_value)
        except ValueError:
            return None
        occurred = _timestamp(row.get("timestamp"))
        digest = row.get("transaction_hash")
        block = row.get("block_number")
        log_index = row.get("log_index")
        if occurred is None or not isinstance(digest, str):
            return None
        if not isinstance(block, int) or not isinstance(log_index, int):
            return None

        low_sender, low_receiver = sender.lower(), receiver.lower()
        if low_sender == wallet and low_receiver == wallet:
            direction, counterparty = Direction.SELF, wallet
        elif low_receiver == wallet:
            direction, counterparty = Direction.IN, low_sender
        elif low_sender == wallet:
            direction, counterparty = Direction.OUT, low_receiver
        else:
            # Not a transfer this wallet was party to; the index should not have returned it.
            return None

        decimals = token.get("decimals")
        try:
            decimals = int(decimals) if decimals is not None else None
        except (TypeError, ValueError):
            decimals = None
        symbol = token.get("symbol")
        try:
            return TransferEvent(
                chain_id=chain_id,
                token=address,
                counterparty=counterparty,
                direction=direction,
                raw_amount=amount,
                block_number=block,
                transaction_hash=digest,
                occurred_at=occurred,
                log_index=log_index,
                decimals=decimals,
                symbol=symbol if isinstance(symbol, str) else None,
            )
        except Exception:  # noqa: BLE001 - a row the contract rejects is an unusable row
            return None


class ReceiptTransferVerifier:
    """Confirms a claimed transfer is exactly the log it was attributed to.

    Matching is by log index, verified on 2026-09-22 against four live Ethereum receipts: the
    index's `log_index` is the block-scoped `logIndex` an RPC receipt reports, not a per-
    transaction counter. Depending on it is therefore exact, and it is what makes the match
    one-to-one -- two claims cannot both land on one log, so an index reporting the same
    transfer twice under different indexes cannot have both halves confirmed.

    The wallet is passed in rather than read off the event. An event carries its counterparty
    and a direction, so without the wallet a receipt in which the counterparty paid somebody
    else entirely satisfies a content match, and an outgoing transfer satisfies an incoming
    claim. Both of those confirmed before this took the wallet.
    """

    def __init__(self, *, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def confirms(self, event: TransferEvent, wallet: str, *, deadline_at: float | None = None) -> bool:
        holder = wallet.lower()
        # A transfer with the wallet on both sides is a self-transfer whatever a source called
        # it. Without this, IN and OUT both build (holder, holder) from a counterparty that is
        # the wallet, so one genuine receipt confirms either direction.
        if event.counterparty == holder and event.direction is not Direction.SELF:
            return False
        if event.direction is Direction.IN:
            expected = (event.counterparty, holder)
        elif event.direction is Direction.OUT:
            expected = (holder, event.counterparty)
        else:
            # A self-transfer has the wallet on both sides, so a claim naming anyone else as
            # the counterparty is not describing this receipt even when the receipt is genuine.
            if event.counterparty != holder:
                return False
            expected = (holder, holder)

        receipt = self._receipt(event, deadline_at=deadline_at)
        if receipt is None:
            return False
        try:
            if int(str(receipt.get("blockNumber") or "0x0"), 16) != event.block_number:
                # The right transaction in the wrong block is a wrong claim, and history is
                # ordered by block, so an unchecked one would also sort into the wrong place.
                return False
        except ValueError:
            return False
        receipt_hash = receipt.get("blockHash")
        if not isinstance(receipt_hash, str) or len(receipt_hash) != 66:
            return False

        for log in receipt.get("logs") or []:
            if not isinstance(log, dict):
                continue
            raw_index = log.get("logIndex")
            # An absent index is not index zero. Defaulting it let a log that never said where
            # it was satisfy a claim that said exactly where it was.
            if not isinstance(raw_index, str) or not raw_index:
                continue
            try:
                if int(raw_index, 16) != event.log_index:
                    continue
            except ValueError:
                continue
            # The claimed log, and only it. Everything below is now a yes or a no.
            if str(log.get("address") or "").lower() != event.token:
                return False
            topics = log.get("topics") or []
            # Exactly three. ERC-721 shares this signature but indexes its tokenId as a fourth
            # topic and leaves data empty, which a "< 3" test accepts and the decoder below
            # then reads as a zero amount -- an NFT confirming a token transfer.
            if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                return False
            sender = "0x" + str(topics[1])[-40:].lower()
            receiver = "0x" + str(topics[2])[-40:].lower()
            if (sender, receiver) != expected:
                return False
            data = str(log.get("data") or "")
            # One uint256 word, encoded. Absent data is not a zero transfer.
            if not data.startswith("0x") or len(data) != 66:
                return False
            try:
                amount = int(data, 16)
            except ValueError:
                return False
            if amount != event.raw_amount:
                return False
            # The date is shown to a user as when this happened, so it is part of the claim.
            # Its authority is the block's own timestamp, not the index's opinion of it.
            canonical = self._canonical_block(event.chain_id, event.block_number, deadline_at=deadline_at)
            if canonical is None:
                return False
            canonical_hash, stamp = canonical
            # A height is not an identity. A receipt fetched from one endpoint can belong to a
            # block that lost a reorg while the header at that height is its replacement, and
            # if the two share a timestamp everything else here still agrees. The hash is what
            # says the receipt is on the chain we are reading.
            if receipt_hash.lower() != canonical_hash:
                return False
            return int(event.occurred_at.timestamp()) == stamp
        return False

    def _rpc_timeout(self, deadline_at: float | None) -> float:
        if deadline_at is None:
            return self.timeout_seconds
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("verification deadline passed")
        return min(self.timeout_seconds, max(1.0, remaining))

    def _receipt(self, event: TransferEvent, *, deadline_at: float | None = None) -> dict[str, Any] | None:
        """A receipt from any endpoint that has one.

        An endpoint answering null has not seen the transaction -- it may be old, or not yet
        indexed there. That is this endpoint's answer, not the chain's, so the walk continues.
        """

        last: Exception | None = None
        for url in rpc_endpoints(event.chain_id):
            timeout = self._rpc_timeout(deadline_at)
            try:
                receipt = rpc_call(
                    url, "eth_getTransactionReceipt", [event.transaction_hash],
                    timeout=timeout,
                )
            except (ChainError, RpcTransportError, http.client.HTTPException) as exc:
                # IncompleteRead is an HTTPException and escapes rpc_call, so without
                # it here a truncated body skips every healthy endpoint behind it.
                last = exc
                continue
            if isinstance(receipt, dict):
                return receipt
        if last is not None:
            raise last
        return None

    def _canonical_block(
        self, chain_id: int, block_number: int, *, deadline_at: float | None = None,
    ) -> tuple[str, int] | None:
        """The canonical block at this height: its hash and its timestamp, read every time.

        This used to cache, guarded by the chain's finalized height. It cannot be made cheap
        and correct together: the header comes from whichever endpoint answers first and the
        finality judgment from whichever answers next, so one endpoint's stale fork could be
        blessed by another's finality and then pinned for the verifier's lifetime -- canonical
        receipts rejected, orphaned ones verified, for as long as the instance lived.

        The saving was one header read per transfer sharing a block, on a path that is bounded
        by a verification budget and already pays a receipt read per transfer. Correctness is
        worth more than halving a bounded cost.
        """

        last: Exception | None = None
        for url in rpc_endpoints(chain_id):
            timeout = self._rpc_timeout(deadline_at)
            try:
                block = rpc_call(
                    url, "eth_getBlockByNumber", [hex(block_number), False],
                    timeout=timeout,
                )
            except (ChainError, RpcTransportError, http.client.HTTPException) as exc:
                last = exc
                continue
            if not isinstance(block, dict):
                continue
            digest, raw = block.get("hash"), block.get("timestamp")
            if not isinstance(digest, str) or len(digest) != 66 or not isinstance(raw, str):
                continue
            try:
                stamp = int(raw, 16)
            except ValueError:
                continue
            return (digest.lower(), stamp)
        if last is not None:
            raise last
        return None
