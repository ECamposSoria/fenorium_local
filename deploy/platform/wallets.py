"""A wallet's holdings on every chain Fenorium reads, gathered and valued by the server.

For each chain a wallet can hold spendable funds on, the server reads -- at one pinned block -- the
native coin, every registry asset and every token LI.FI can route there, in Multicall3 batches.
What it holds then goes through the engine exactly as a browser's snapshot does: registry assets
and held routable tokens become registered assets, the engine values them, and risk is filled from
the server's histories. Exactly fixed-rate deposits, which are positions rather than tokens, are
read from their markets and valued at their existing present values.

One chain failing is that chain's error, never the wallet's: the answer names the chain and why,
and the total is marked incomplete -- a lower bound -- rather than presented as the whole.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime
import json
import urllib.error
import urllib.parse
import urllib.request
import http.client
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping

from engine.acquire.jobs.current_prices import METHOD_VERSION as FEED_METHOD
from engine.composer import NATIVE_TOKEN_SENTINELS
from engine.portfolio import (
    DEFILLAMA_CURRENT,
    AssetKind,
    BlockReference,
    CapabilityStatus,
    DiscoveryScope,
    PriceObservation,
    RegisteredAsset,
    WalletScanRequest,
    discover_wallet,
)
from engine.portfolio.analytics import AnalyticsScope
from engine.portfolio.discovery import AdapterError, RawPosition
from engine.registry.instruments import instrument_id
from engine.portfolio.contracts import ReasonCode, safe_detail
from engine.portfolio.valuation import raw_to_usd_micros
from engine.registry import asset_id, registry

from .current_prices import MAX_AGE_SECONDS
from .on_demand_prices import OnDemandPrices
from .ratelimit import FlightTimeout, SingleFlightCache
from engine.onchain import ChainReader, OnchainError, PinnedBlock, read_at_pinned_block, state_gone
from engine.onchain import MULTICALL_CHUNK, MIN_MULTICALL_CHUNK, decode_aggregate3, encode_aggregate3
from engine.keccak import keccak256
from engine.midnight import ADDRESSES, MIDNIGHT_VENUES
from .midnight_orders import _open_no_redirect, accrue_position
from .ratelimit import ProviderBudget, Throttled


SCHEMA = "fenorium/wallet-portfolio/1"
NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"
METHOD_VERSION = "server-multicall3-portfolio/1"
CACHE_SECONDS = 60
MAX_WORKERS = 8
MAX_CONCURRENT_SCANS = 2
READ_DEADLINE_SECONDS = 20
RESPONSE_DEADLINE_SECONDS = 25
MORPHO_POSITIONS_URL = "https://api.morpho.org/v0/midnight/users/{wallet}/positions"
MAX_MORPHO_PAGES = 10
MAX_MORPHO_BYTES = 512_000


class MorphoWalletPositions:
    """Bounded fixed-host index read; callers use it only to discover market keys."""

    def __init__(self, *, opener=None, clock=time.monotonic, budget=None) -> None:
        self.opener = opener or _open_no_redirect
        self.clock = clock
        self.budget = budget if budget is not None else ProviderBudget(
            "Morpho wallet positions", per_minute=30, per_day=5000)

    def fetch(self, wallet: str, *, deadline_at: float) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_MORPHO_PAGES):
            url = MORPHO_POSITIONS_URL.format(wallet=urllib.parse.quote(wallet, safe=""))
            if cursor:
                url += "?" + urllib.parse.urlencode({"cursor": cursor})
            self.budget.reserve()
            remaining = min(5, deadline_at - self.clock())
            if remaining <= 0:
                raise TimeoutError("deadline")
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with self.opener(request, timeout=remaining) as response:
                if response.geturl() != url or not 200 <= response.status < 300:
                    raise ValueError("Morpho response")
                raw = response.read(MAX_MORPHO_BYTES + 1)
            if len(raw) > MAX_MORPHO_BYTES or self.clock() >= deadline_at:
                raise ValueError("Morpho response limit")
            answer = json.loads(raw)
            if not isinstance(answer, Mapping) or not isinstance(answer.get("data"), list):
                raise ValueError("Morpho positions shape")
            rows.extend(row for row in answer["data"] if isinstance(row, dict))
            cursor = answer.get("cursor")
            if cursor is None or cursor == "":
                break
            if not isinstance(cursor, str):
                raise ValueError("Morpho cursor")
        return rows


class WalletBusy(RuntimeError):
    """The process is already reading its maximum number of distinct wallets."""


class RoutableTokens:
    """`lifi-tokens.json`: the tokens LI.FI routes on each paying chain, reloaded when it changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._stamp: tuple[int, int] | None = None
        self._chains: dict[int, dict[str, dict[str, Any]]] = {}
        self.generated_at: str | None = None

    def _load(self) -> dict[int, dict[str, dict[str, Any]]]:
        with self._lock:
            try:
                stat = self.path.stat()
            except OSError:
                self._stamp, self._chains, self.generated_at = None, {}, None
                return self._chains
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._stamp:
                chains: dict[int, dict[str, dict[str, Any]]] = {}
                generated = None
                try:
                    document = json.loads(self.path.read_bytes().decode("utf-8"))
                    if document.get("schema_version") == "fenorium/lifi-tokens/1":
                        generated = document.get("generated_at")
                        for chain_id, row in (document.get("chains") or {}).items():
                            chains[int(chain_id)] = {token["address"]: token for token in row.get("tokens") or []
                                                     if isinstance(token, dict) and isinstance(token.get("address"), str)}
                except (OSError, UnicodeDecodeError, ValueError, TypeError, AttributeError, KeyError):
                    chains, generated = {}, None
                self._stamp, self._chains, self.generated_at = stamp, chains, generated
            return self._chains

    def available(self, chain_id: int) -> bool:
        return chain_id in self._load()

    def on(self, chain_id: int) -> dict[str, dict[str, Any]]:
        return self._load().get(chain_id, {})

    def token(self, chain_id: int, address: str | None) -> dict[str, Any] | None:
        if address is None:
            return None
        return self.on(chain_id).get(address.lower())


class _Prefetched:
    """The engine's read-only adapter, answering from balances already read at the pinned block."""

    def __init__(self, balances: Mapping[int, Mapping[str | None, int | None]]) -> None:
        self.balances = balances

    def _read(self, chain_id: int, token: str | None) -> int:
        value = self.balances.get(chain_id, {}).get(token)
        if value is None:
            raise OnchainError("this balance could not be read at the pinned block")
        return value

    def native_balance(self, chain_id: int, _wallet: str, _block: BlockReference) -> int:
        return self._read(chain_id, None)

    def token_balance(self, chain_id: int, token: str, _wallet: str, _block: BlockReference) -> int:
        return self._read(chain_id, token.lower())


def _category(row: Mapping[str, Any]) -> tuple[AssetKind, str, bool]:
    category = str(row.get("category") or "").lower()
    if category == "vault":
        return AssetKind.VAULT, "variable_income", True
    if category == "native":
        return AssetKind.PT, "fixed_income", False
    if category == "rwa":
        return AssetKind.ERC20, "fixed_income", False
    return AssetKind.ERC20, category or "token", False


class WalletReader:
    _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    _morpho_pool = ThreadPoolExecutor(max_workers=4)
    _scans = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)

    def __init__(self, *, api: Any, routable: RoutableTokens, on_demand: OnDemandPrices | None = None,
                 market_store: Any = None,
                 morpho_positions: Any = None,
                 reader_factory: Callable[..., ChainReader] = ChainReader,
                 clock: Callable[[], datetime] | None = None,
                 read_deadline_seconds: float = READ_DEADLINE_SECONDS,
                 response_deadline_seconds: float = RESPONSE_DEADLINE_SECONDS) -> None:
        self.api = api
        self.routable = routable
        self.on_demand = on_demand
        self.market_store = market_store
        self.morpho_positions = morpho_positions if morpho_positions is not None else MorphoWalletPositions()
        self.reader_factory = reader_factory
        self.read_deadline_seconds = read_deadline_seconds
        self.response_deadline_seconds = response_deadline_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cache = SingleFlightCache(CACHE_SECONDS)

    @staticmethod
    def chains() -> list[int]:
        return sorted(chain.chain_id for chain in registry().chains
                      if not chain.testnet and chain.roles & {"pay", "portfolio"})

    def read(self, wallet: str, document: Mapping[str, Any], *, chain_ids: list[int] | None = None) -> dict[str, Any]:
        """`document` is the registry for the current universe (engine.registry.registry_document)."""
        wallet = wallet.lower()
        scanned = tuple(sorted(set(chain_ids or self.chains())))
        if self.market_store is not None:
            seen_at = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            tenor_rows = [row for row in document.get("instruments") or []
                          if row.get("venue") in MIDNIGHT_VENUES and row.get("market_id") and row.get("loan_token")
                          and row.get("chain_id") is not None]
            for chain_id in {int(row["chain_id"]) for row in tenor_rows}:
                self.market_store.remember_midnight_markets(
                    chain_id, [row for row in tenor_rows if int(row["chain_id"]) == chain_id], seen_at)
            principal_rows = [row for row in document.get("instruments") or []
                              if row.get("venue") in {"Pendle", "Spectra"} and row.get("address")
                              and type(row.get("decimals")) is int
                              and type(row.get("maturity_timestamp")) is int
                              and row.get("chain_id") is not None]
            for chain_id in {int(row["chain_id"]) for row in principal_rows}:
                self.market_store.remember_principal_tokens(
                    chain_id, [row for row in principal_rows if int(row["chain_id"]) == chain_id], seen_at)
        key = (wallet, scanned, str(document.get("universe_generated_at")))
        try:
            return self._cache.get(key, lambda: self._scan(wallet, scanned, document),
                                   fresh=lambda result: not self._timed_out(result))
        except FlightTimeout as exc:
            raise WalletBusy("wallet reads are busy") from exc

    @staticmethod
    def _timed_out(result: Mapping[str, Any]) -> bool:
        return any(chain["status"] == "FAILED" and (chain.get("error") or "").startswith("timed out")
                   for chain in result["chains"])

    def _scan(self, wallet: str, scanned: tuple[int, ...], document: Mapping[str, Any]) -> dict[str, Any]:
        if not self._scans.acquire(blocking=False):
            raise WalletBusy("wallet reads are busy")
        futures: list[Future] = []
        try:
            return self._read(wallet, scanned, document, submitted=futures)
        finally:
            pending = {future for future in futures if not future.done()}
            if not pending:
                self._scans.release()
            else:
                lock = threading.Lock()

                def finished(future: Future) -> None:
                    with lock:
                        pending.remove(future)
                        if not pending:
                            self._scans.release()

                for future in tuple(pending):
                    future.add_done_callback(finished)

    def _registry_assets(self, document: Mapping[str, Any], chain_id: int) -> dict[str | None, dict[str, Any]]:
        """Every registry asset on a chain with known decimals, keyed by address (None: native)."""
        found: dict[str | None, dict[str, Any]] = {}
        rows = {row["address"]: row for row in document.get("instruments") or [] if row.get("chain_id") == chain_id
                and row.get("address")}
        for asset in document.get("assets") or []:
            if asset["chain_id"] != chain_id:
                continue
            address = asset["address"]
            if address is None and not registry().require(chain_id).offer_native:
                continue
            decimals = asset.get("decimals")
            listed = self.routable.token(chain_id, address)
            if decimals is None and listed is not None:
                decimals = listed["decimals"]
            if type(decimals) is not int:
                continue
            found[address] = {**asset, "decimals": decimals, "row": rows.get(address),
                              "symbol": asset.get("symbol") or (listed or {}).get("symbol") or ""}
        return found

    def _read_chain(self, wallet: str, chain_id: int, document: Mapping[str, Any],
                    deadline_at: float, position_future: Future | None = None) -> dict[str, Any]:
        chain = registry().require(chain_id)
        known = self._registry_assets(document, chain_id)
        listed_available = self.routable.available(chain_id)
        listed = self.routable.on(chain_id) if listed_available else {}
        tokens = ([None] if chain.offer_native else []) + [address for address in known if address is not None
                  and address.lower() not in NATIVE_TOKEN_SENTINELS]
        tokens += [address for address in listed if address not in known
                   and address.lower() not in NATIVE_TOKEN_SENTINELS]
        markets = sorted({row["market_address"] for row in document.get("instruments") or []
                          if row.get("chain_id") == chain_id and row.get("venue") == "Exactly"
                          and row.get("market_address")})
        try:
            reader = self.reader_factory(chain)
            reader.deadline_at = deadline_at

            def read(block: PinnedBlock) -> tuple[
                    dict[str | None, int | None], list[dict[str, Any]], list[str], list[dict[str, Any]],
                    list[dict[str, Any]], list[dict[str, Any]]]:
                balances = reader.balances(wallet, tokens, block)
                positions, failures, midnight_positions, redeemable = [], [], [], []
                for market in markets:
                    try:
                        for position in reader.exactly_positions(wallet, market, block):
                            positions.append({"market_address": market, **position})
                    except OnchainError as exc:
                        # Pruned state means this block, not this market: the read moves to a newer one.
                        if state_gone(exc):
                            raise
                        failures.append(f"Exactly {market}: {safe_detail(exc)}")
                current_tenor_rows = [row for row in document.get("instruments") or []
                                      if row.get("chain_id") == chain_id and row.get("venue") in MIDNIGHT_VENUES
                                      and row.get("market_id") and row.get("loan_token")]
                current_pt_rows = [row for row in document.get("instruments") or []
                                   if row.get("chain_id") == chain_id and row.get("venue") in {"Pendle", "Spectra"}
                                   and row.get("address") and type(row.get("decimals")) is int
                                   and type(row.get("maturity_timestamp")) is int]
                # Current metadata wins for markets still listed. The store fills gaps after the
                # universe drops matured markets, preserving the last published valuation fields.
                tenor_by_market = {}
                if self.market_store is not None:
                    for row in self.market_store.midnight_markets(chain_id):
                        if row.get("market_id"):
                            tenor_by_market[str(row["market_id"]).lower()] = row
                for row in current_tenor_rows:
                    tenor_by_market[str(row["market_id"]).lower()] = row
                discovery_note = None
                discovered_rows = []
                if chain_id in {1, 8453, 5042}:
                    try:
                        api_rows = (position_future.result(timeout=max(0, min(2.0, deadline_at - time.monotonic())))
                                    if position_future is not None else [])
                        known_tokens = {str(address).lower(): info for address, info in known.items()
                                        if address is not None}
                        chain_tokens = list(chain.settlement) + ([chain.usdc] if chain.usdc is not None else [])
                        chain_tokens_by_address = {token.address.lower(): token for token in chain_tokens}
                        for item in api_rows:
                            user = item.get("user_address")
                            market_id = item.get("market_id")
                            loan_address = item.get("loan_token")
                            maturity = item.get("maturity")
                            if (user.lower() != wallet.lower() if isinstance(user, str) else True):
                                continue
                            if (type(item.get("chain_id")) is not int or item.get("chain_id") != chain_id
                                    or not isinstance(market_id, str)
                                    or not re.fullmatch(r"0x[0-9a-fA-F]{64}", market_id)
                                    or not isinstance(loan_address, str)
                                    or not re.fullmatch(r"0x[0-9a-fA-F]{40}", loan_address)
                                    or not _valid_tenor_timestamp(maturity)):
                                continue
                            key = market_id.lower()
                            if key in tenor_by_market:
                                continue
                            info = known_tokens.get(loan_address.lower())
                            if info is None:
                                info = self.routable.token(chain_id, loan_address)
                            token = chain_tokens_by_address.get(loan_address.lower())
                            decimals = (info or {}).get("decimals")
                            symbol = (info or {}).get("symbol") or (None if token is None else token.symbol)
                            if type(decimals) is not int and token is not None:
                                decimals = token.decimals
                            if type(decimals) is not int or not 0 <= decimals <= 255:
                                decimals = None
                            maturity_date = datetime.fromtimestamp(maturity, UTC).strftime("%Y-%m-%d")
                            discovered = {"venue": "Morpho Midnight", "chain_id": chain_id,
                                          "market_id": market_id.lower(), "loan_token": {
                                              "address": loan_address.lower(), "decimals": decimals,
                                              "symbol": symbol}, "maturity": maturity_date,
                                          "maturity_timestamp": maturity,
                                          "name": f"{symbol or 'Midnight market'} {maturity_date} · {market_id[:10]}"}
                            tenor_by_market[key] = discovered
                            discovered_rows.append(discovered)
                    except Exception as exc:
                        discovery_note = f"Morpho wallet positions: {safe_detail(exc)}"
                tenor_rows = list(tenor_by_market.values())
                pt_by_address = {}
                if self.market_store is not None:
                    pt_by_address = {row["address"].lower(): row
                                     for row in self.market_store.principal_tokens(chain_id)}
                for row in current_pt_rows:
                    pt_by_address.setdefault(str(row["address"]).lower(), row)
                pt_rows = [row for row in pt_by_address.values()
                           if type(row.get("maturity_timestamp")) is int and row["maturity_timestamp"] >= 0
                           and row["maturity_timestamp"] <= block.timestamp
                           and isinstance(row.get("address"), str)
                           and re.fullmatch(r"0x[0-9a-fA-F]{40}", row["address"])]
                pt_calls = []
                balance_selector = "0x" + keccak256(b"balanceOf(address)")[:4].hex()
                for row in pt_rows:
                    call = balance_selector + wallet.removeprefix("0x").rjust(64, "0")
                    pt_calls.append((row["address"].lower(), call, row))
                if pt_calls:
                    try:
                        decoded = decode_aggregate3(reader.eth_call(
                            reader.chain.multicall3,
                            encode_aggregate3([(target, data) for target, data, _ in pt_calls]), block),
                            len(pt_calls))
                        for (_, _, row), (success, data) in zip(pt_calls, decoded):
                            if not success or len(data) != 32:
                                failures.append(f"PT {row['address']}: balance call failed or malformed")
                                continue
                            amount = int.from_bytes(data, "big")
                            if amount:
                                timestamp = int(row["maturity_timestamp"])
                                try:
                                    maturity_date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
                                except (OverflowError, OSError, ValueError):
                                    failures.append(f"PT {row['address']}: invalid maturity timestamp")
                                    continue
                                redeemable.append({"chain_id": chain_id, "address": row["address"].lower(),
                                                   "venue": row["venue"], "name": row.get("name") or "",
                                                   "decimals": row["decimals"], "raw_amount": str(amount),
                                                   "maturity": maturity_date,
                                                   "maturity_timestamp": timestamp})
                    except OnchainError as exc:
                        if state_gone(exc):
                            raise
                        failures.append(f"PT balance batch: {safe_detail(exc)}")
                config = ADDRESSES.get(chain_id)
                calls, seen_markets = [], set()
                if discovery_note:
                    failures.append(discovery_note)
                for row in tenor_rows:
                    market_id = row.get("market_id")
                    loan = row.get("loan_token")
                    address = loan.get("address") if isinstance(loan, Mapping) else loan
                    if (not isinstance(market_id, str)
                            or not re.fullmatch(r"0x[0-9a-fA-F]{64}", market_id)
                            or not isinstance(address, str)
                            or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address)
                            or config is None):
                        failures.append(f"{row.get('venue', 'Tenor')} {market_id}: invalid market metadata")
                        continue
                    decimals = loan.get("decimals") if isinstance(loan, Mapping) else None
                    if decimals is not None and (type(decimals) is not int or not 0 <= decimals <= 255):
                        failures.append(f"{row.get('venue', 'Tenor')} {market_id}: invalid loan token decimals")
                        continue
                    if market_id.lower() in seen_markets:
                        continue
                    seen_markets.add(market_id.lower())
                    selector = "0x" + keccak256(b"position(bytes32,address)")[:4].hex()
                    call = selector + market_id.removeprefix("0x").lower() + wallet.removeprefix("0x").rjust(64, "0")
                    loss_selector = "0x" + keccak256(b"lossFactor(bytes32)")[:4].hex()
                    loss_call = loss_selector + market_id.removeprefix("0x").lower()
                    calls.extend(((config.midnight, call, row, address.lower(), "position"),
                                  (config.midnight, loss_call, row, address.lower(), "loss_factor")))

                def read_positions(chunk):
                    try:
                        decoded = decode_aggregate3(reader.eth_call(
                            reader.chain.multicall3,
                            encode_aggregate3([(target, data) for target, data, _, _, _ in chunk]), block), len(chunk))
                    except OnchainError as exc:
                        if state_gone(exc):
                            raise
                        if len(chunk) > MIN_MULTICALL_CHUNK:
                            middle = len(chunk) // 2
                            read_positions(chunk[:middle])
                            read_positions(chunk[middle:])
                        else:
                            failures.append(f"Tenor position batch: {safe_detail(exc)}")
                        return
                    for (_, _, row, loan_address, kind), (success, data) in zip(chunk, decoded):
                        market_id = row.get("market_id")
                        expected = 6 * 32 if kind == "position" else 32
                        if not success or len(data) != expected:
                            label = "position" if kind == "position" else "lossFactor"
                            failures.append(f"{row.get('venue', 'Tenor')} {market_id}: {label} call failed or malformed")
                            continue
                        value = int.from_bytes(data[:32], "big") if kind == "loss_factor" else None
                        if kind == "loss_factor":
                            pending_market_data.setdefault(str(market_id).lower(), {})[kind] = value
                            continue
                        words = [int.from_bytes(data[i:i + 32], "big") for i in range(0, len(data), 32)]
                        pending_market_data.setdefault(str(market_id).lower(), {}).update(
                            {"row": row, "loan_address": loan_address, "position": words})

                pending_market_data = {}

                for start in range(0, len(calls), MULTICALL_CHUNK):
                    read_positions(calls[start:start + MULTICALL_CHUNK])
                for market_data in pending_market_data.values():
                    words = market_data.get("position")
                    loss_factor = market_data.get("loss_factor")
                    if words is None or loss_factor is None:
                        continue
                    if words[0] == 0 and words[4] == 0:
                        continue
                    midnight_positions.append({"market_id": market_data["row"]["market_id"],
                                               "row": market_data["row"],
                                               "loan_address": market_data["loan_address"],
                                               "position": {"credit": words[0], "pendingFee": words[1],
                                                            "lastLossFactor": words[2], "lastAccrual": words[3]},
                                               "loss_factor": loss_factor, "debt": words[4],
                                               "at": block.timestamp})
                return balances, positions, failures, midnight_positions, redeemable, discovered_rows

            block, (balances, positions, failures, midnight_positions, redeemable, discovered_rows) = read_at_pinned_block(reader, read)
        except OnchainError as exc:
            return {"chain_id": chain_id, "status": "FAILED", "error": safe_detail(exc), "known": known,
                    "balances": {}, "positions": [], "midnight_positions": [], "redeemable": [], "block": None}
        if not listed_available:
            failures.append("routable token list unavailable")
        failed_balances = sum(1 for address in tokens if balances.get(address) is None)
        if failed_balances:
            failures.append(f"{failed_balances} balances could not be read")
        return {"chain_id": chain_id, "status": "READ" if not failures else "PARTIAL",
                "error": "; ".join(failures) or None, "known": known, "balances": balances,
                "positions": positions, "midnight_positions": midnight_positions,
                "redeemable": redeemable, "midnight_market_rows": discovered_rows, "block": block}

    def _read(self, wallet: str, scanned: tuple[int, ...], document: Mapping[str, Any], *,
              submitted: list[Future] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        deadline_at = started + self.read_deadline_seconds
        response_deadline_at = started + self.response_deadline_seconds
        scan_started = self.clock()
        futures = {}
        position_future = None
        if set(scanned) & {1, 8453, 5042}:
            position_future = self._morpho_pool.submit(self.morpho_positions.fetch, wallet, deadline_at=deadline_at)
            if submitted is not None:
                submitted.append(position_future)
        for chain_id in scanned:
            future = self._pool.submit(self._read_chain, wallet, chain_id, document, deadline_at, position_future)
            futures[future] = chain_id
            if submitted is not None:
                submitted.append(future)
        done, _ = wait(futures, timeout=max(0.0, deadline_at - time.monotonic()))
        reads = []
        for future, chain_id in futures.items():
            if future in done:
                reads.append(future.result())
            else:
                future.cancel()
                reads.append({"chain_id": chain_id, "status": "FAILED",
                              "error": f"timed out after {self.read_deadline_seconds:g}s", "known": {},
                              "balances": {}, "positions": [], "block": None})
        if self.market_store is not None:
            seen_at = scan_started.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            for read in reads:
                learned = read.get("midnight_market_rows") or []
                if learned:
                    self.market_store.remember_midnight_markets(read["chain_id"], learned, seen_at)
        assets: list[RegisteredAsset] = []
        meta: dict[str, dict[str, Any]] = {}
        blocks: list[BlockReference] = []
        balances: dict[int, dict[str | None, int | None]] = {}
        read_chains: list[int] = []
        for read in reads:
            chain_id = read["chain_id"]
            if read["block"] is None:
                continue
            read_chains.append(chain_id)
            block: PinnedBlock = read["block"]
            blocks.append(BlockReference(chain_id=chain_id, block_number=block.number, block_hash=block.hash,
                                         finality=block.finality, finalized=block.finality == "finalized"))
            balances[chain_id] = {(None if token is None else token.lower()): value
                                  for token, value in read["balances"].items()}
            for address, info in read["known"].items():
                assets.append(self._asset(chain_id, address, info, source="registry", meta=meta))
            for address, value in balances[chain_id].items():
                if address is None or address in read["known"] or value == 0:
                    continue
                listed = self.routable.token(chain_id, address)
                if listed is not None:
                    assets.append(self._asset(chain_id, address, listed, source="lifi", meta=meta))
        snapshot = discover_wallet(
            WalletScanRequest(
                wallet=wallet, as_of=scan_started, method_version=METHOD_VERSION,
                registered_assets=tuple(assets), block_references=tuple(blocks),
                chain_ids=tuple(read_chains), include_base_sepolia_demo=False,
            ),
            _Prefetched(balances),
        ) if read_chains else None
        if snapshot is not None:
            snapshot = replace(snapshot, schema_version="fenorium/wallet-snapshot/v2",
                               discovery_scope=DiscoveryScope.REGISTERED_PLUS_ENUMERATED,
                               enumeration_complete=False)
        observations = self._observations(snapshot, meta, deadline_at=response_deadline_at) if snapshot is not None else []
        as_of = self.clock()
        result: dict[str, Any] = {
            "schema_version": SCHEMA,
            "valuation_scope": "tokens held and fixed-rate venue positions",
            "wallet": wallet,
            "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method_version": METHOD_VERSION,
            "chains": [{
                "chain_id": read["chain_id"], "name": registry().require(read["chain_id"]).name,
                "status": read["status"], "error": read["error"],
                "block": None if read["block"] is None else read["block"].to_dict(),
            } for read in reads],
            "redeemable": [item for read in reads for item in read.get("redeemable", [])],
        }
        if snapshot is None:
            result.update(wallet_snapshot=None, holdings=[], positions=[], valuation=None, allocation=None,
                          analytics=None,
                          totals={"value_usd_micros": None, "complete": False,
                                  "scope": "registry-and-lifi-catalog", "chains_complete": False})
            return result
        positions = self._venue_positions(reads, document, as_of)
        fixed, position_values = self._valuation_positions(positions, reads, document, blocks)
        snapshot = replace(snapshot, positions=tuple(sorted(
            (*snapshot.positions, *fixed), key=lambda position: position.identity)))
        analysed = self.api.analyse(snapshot, observations, [], as_of=as_of, method=METHOD_VERSION,
                                    max_price_age_seconds=300, scope=AnalyticsScope.VALUED_HOLDINGS,
                                    position_values_usd_micros=position_values)
        valued = {row["identity"]: row for row in analysed["valuation"]["positions"]}
        holdings = []
        for position in snapshot.positions:
            if not position.raw_amount or position.asset.kind is AssetKind.POSITION:
                continue
            info = meta[position.identity]
            row = valued.get(position.identity) or {}
            holdings.append({
                "asset_id": info["asset_id"], "identity": position.identity, "chain_id": position.asset.chain_id,
                "address": position.asset.address, "symbol": position.asset.symbol,
                "decimals": position.asset.decimals, "raw_amount": position.raw_amount,
                "kind": position.asset.kind.value, "category": position.asset.category, "source": info["source"],
                "instrument_id": info.get("instrument_id"), "routable": info.get("routable", False),
                "value_usd_micros": row.get("value_usd_micros"), "price": row.get("price"),
                "reason_code": row.get("reason_code"),
            })
        held_value = [row["value_usd_micros"] for row in holdings]
        position_value = [row["present_value_usd_micros"] for row in positions]
        unresolved = [position.identity for position in snapshot.positions if position.raw_amount is None]
        holdings.sort(key=lambda row: -(row["value_usd_micros"] or 0))
        result.update(
            holdings=holdings,
            # What this wallet could pay for a purchase with: what it holds that LI.FI routes, on a
            # chain a buyer may pay from, largest first. The real quote is fetched at the size chosen.
            pay_options=[{
                "chain_id": row["chain_id"],
                "address": row["address"] or NATIVE_SENTINEL,
                "symbol": row["symbol"], "decimals": row["decimals"], "raw_amount": row["raw_amount"],
                "value_usd_micros": row["value_usd_micros"], "asset_id": row["asset_id"],
            } for row in holdings if row["routable"] and registry().require(row["chain_id"]).has("pay")],
            positions=positions,
            # The snapshot the valuation was made from, so a client can render this read with the
            # same contract it applies to its own analytics answers.
            wallet_snapshot=analysed["wallet_snapshot"],
            valuation=analysed["valuation"], allocation=analysed["allocation"], analytics=analysed["analytics"],
            risk_scope=analysed["risk_scope"],
            totals={
                "holdings_usd_micros": sum(value for value in held_value if value is not None),
                "positions_usd_micros": sum(value for value in position_value if value is not None),
                "unpriced_holdings": sum(1 for value in held_value if value is None),
                "unpriced_positions": sum(1 for value in position_value if value is None),
                "unresolved_registry_assets": len(unresolved),
                "chains_read": sum(1 for read in reads if read["block"] is not None),
                "chains_failed": sum(1 for read in reads if read["block"] is None),
                # Catalog reads cannot establish that every holding was discovered.
                "value_usd_micros": sum(value for value in held_value + position_value if value is not None),
                "complete": False,
                "scope": "registry-and-lifi-catalog",
                "chains_complete": all(read["status"] == "READ" for read in reads),
            },
        )
        return result

    def _asset(self, chain_id: int, address: str | None, info: Mapping[str, Any], *, source: str,
               meta: dict[str, dict[str, Any]]) -> RegisteredAsset:
        row = info.get("row") or {}
        if address is None:
            kind, category, variable = AssetKind.NATIVE, "crypto", True
        elif source == "lifi":
            kind, category, variable = AssetKind.ERC20, "token", False
        else:
            kind, category, variable = _category(row) if row else (AssetKind.ERC20, "settlement", False)
        chain = registry().require(chain_id)
        settlement = next((token for token in chain.settlement
                           if address is not None and token.address.lower() == address), None)
        is_usdc = address is not None and chain.usdc is not None and address.lower() == chain.usdc.address.lower()
        history = self.api.price_history
        covered = history is not None and history.covers(chain_id, address, as_of=self.clock())
        asset = RegisteredAsset(
            chain_id=chain_id, kind=kind, decimals=int(info["decimals"]), address=address,
            symbol=str(info.get("symbol") or "")[:64], category=category,
            protocol=str(row.get("venue") or ("LI.FI routable" if source == "lifi" else "UNKNOWN"))[:128],
            is_stablecoin=is_usdc or bool(settlement and settlement.stablecoin) or row.get("is_stablecoin") is True,
            is_variable_income=variable, valuation_support=CapabilityStatus.SUPPORTED,
            risk_support=CapabilityStatus.SUPPORTED if covered else CapabilityStatus.UNRESOLVED,
        )
        listed = self.routable.token(chain_id, address)
        meta[asset.identity] = {
            "asset_id": asset_id(chain_id, address), "source": source,
            "instrument_id": row.get("instrument_id") if row else None,
            # The native coin is routable wherever the chain offers it; a token where LI.FI lists it.
            "routable": (address is None and registry().require(chain_id).offer_native) or listed is not None,
        }
        return asset

    def _observations(self, snapshot: Any, meta: Mapping[str, Mapping[str, Any]], *,
                      deadline_at: float) -> list[PriceObservation]:
        as_of = self.clock()
        observations: list[PriceObservation] = []
        missing: dict[str, list[Any]] = {}
        stale: dict[str, PriceObservation] = {}
        prices = self.api.current_prices
        for position in snapshot.positions:
            if not position.raw_amount:
                continue
            asset = position.asset
            own = None if prices is None else prices.observation(asset.chain_id, asset.address, identity=position.identity)
            if own is not None:
                age = (as_of - own.observed_at).total_seconds()
                if 0 <= age <= MAX_AGE_SECONDS:
                    observations.append(own)
                    continue
                stale[position.identity] = own
            chain = registry().require(asset.chain_id)
            coin = chain.llama_coin(asset.address)
            if coin is not None:
                missing.setdefault(coin, []).append(position)
        remaining = deadline_at - time.monotonic()
        if missing and self.on_demand is not None and remaining > 0:
            stamp = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
            for coin, entry in self.on_demand.entries(list(missing), now_text=stamp, budget_seconds=remaining).items():
                if entry.get("status") != "PRICED":
                    continue
                for position in missing[coin]:
                    observations.append(PriceObservation(
                        identity=position.identity,
                        source_id=f"DefiLlama current price {coin} observed_at={entry['observed_at']} (on demand)",
                        independence_key=DEFILLAMA_CURRENT, price_usd_fixed=entry["price_usd_fixed"],
                        observed_at=datetime.fromisoformat(entry["observed_at"][:-1] + "+00:00"),
                        method_version=FEED_METHOD,
                    ))
                    stale.pop(position.identity, None)
        observations.extend(stale.values())
        return observations

    def _valuation_positions(
        self, positions: list[dict[str, Any]], reads: list[Mapping[str, Any]],
        document: Mapping[str, Any], blocks: list[BlockReference],
    ) -> tuple[list[RawPosition], dict[str, int | None]]:
        """Use the same face amounts and present values as the exit-position response."""
        fixed: list[RawPosition] = []
        values: dict[str, int | None] = {}
        for position in positions:
            chain_id = position["chain_id"]
            if position["venue"] == "Exactly":
                row = next((row for row in document.get("instruments", [])
                            if row.get("chain_id") == chain_id and row.get("venue") == "Exactly"
                            and row.get("market_address") == position["market_address"]
                            and (row.get("fixed_income") or {}).get("maturity") == position["maturity"]), None)
                face = int(position["principal_raw_amount"]) + int(position["fee_raw_amount"])
                fallback = f'{position["market_address"]}:{position["maturity"]}'
            else:
                row = next((item["row"] for read in reads if read["chain_id"] == chain_id
                            for item in read.get("midnight_positions", [])
                            if item["market_id"] == position["market_id"]), None)
                face = position["face_value_raw_amount"]
                face = None if face is None else int(face)
                fallback = position["market_id"]
            identity_row = row if row is not None and row.get("address") is None and row.get("name") else {
                "chain_id": chain_id, "venue": position["venue"], "name": fallback}
            identity = instrument_id(identity_row, registry())
            # A loan token absent from every catalog has no known precision: the quantity stays
            # unresolved instead of being shown in raw units as though it had none.
            precision_unknown = position["decimals"] is None
            asset = RegisteredAsset(
                chain_id=chain_id, kind=AssetKind.POSITION, instrument_id=identity,
                decimals=0 if precision_unknown else position["decimals"],
                symbol=str((row or {}).get("symbol") or (row or {}).get("name") or position["venue"])[:64],
                category="fixed-income", protocol=position["venue"],
                risk_support=CapabilityStatus.SUPPORTED, fixed_income_support=CapabilityStatus.SUPPORTED)
            block = next(block for block in blocks if block.chain_id == chain_id)
            if precision_unknown:
                fixed.append(RawPosition(asset, None, block, adapter_error=AdapterError(
                    chain_id, identity, ReasonCode.UNRESOLVED_POSITION,
                    "the position's token precision is unknown, so its quantity cannot be stated")))
                continue
            fixed.append(RawPosition(asset, face, block))
            values[identity] = position["present_value_usd_micros"]
        return fixed, values

    def _venue_positions(self, reads: list[Mapping[str, Any]], document: Mapping[str, Any],
                         as_of: datetime) -> list[dict[str, Any]]:
        rows = [row for row in document.get("instruments") or [] if row.get("venue") == "Exactly"]
        result = []
        prices = self.api.current_prices
        for read in reads:
            for position in read["positions"]:
                chain_id = read["chain_id"]
                market = position["market_address"]
                match = next((row for row in rows if row.get("chain_id") == chain_id
                              and row.get("market_address") == market and _maturity(row) == position["maturity"]), None)
                any_row = match or next((row for row in rows if row.get("chain_id") == chain_id
                                         and row.get("market_address") == market), None)
                underlying = (any_row or {}).get("underlying_asset_id")
                info = read["known"].get(underlying.rsplit(":", 1)[-1]) if underlying else None
                price = None
                if info is not None and prices is not None:
                    observation = prices.observation(chain_id, info["address"], identity=info["asset_id"])
                    if (observation is not None
                            and 0 <= (self.clock() - observation.observed_at).total_seconds() <= MAX_AGE_SECONDS):
                        price = observation
                decimals = None if info is None else info["decimals"]

                def usd(raw: int) -> int | None:
                    if price is None or decimals is None:
                        return None
                    return raw_to_usd_micros(raw, decimals=decimals, price_usd_fixed=price.price_usd_fixed)

                principal_usd = usd(position["principal"])
                face_usd = usd(position["principal"] + position["fee"])
                matured = position["maturity"] <= int(as_of.timestamp())
                rate_bps = None if match is None else match.get("rate_bps")
                has_rate = (match is not None and match.get("rate_kind") == "executable"
                            and type(rate_bps) is int)
                if matured:
                    present_value = face_usd
                    valuation_basis = "matured; face value"
                elif has_rate:
                    remaining_seconds = position["maturity"] - int(as_of.timestamp())
                    year_seconds = 365 * 86_400
                    denominator = 10_000 * year_seconds + rate_bps * remaining_seconds
                    if denominator <= 0:
                        present_value = None
                        valuation_basis = "face value; venue executable rate cannot produce a valid valuation"
                    else:
                        present_value = (None if face_usd is None else
                                         face_usd * 10_000 * year_seconds // denominator)
                        valuation_basis = "face value discounted at the venue's executable rate for the remaining term"
                else:
                    present_value = principal_usd
                    valuation_basis = "principal; the venue publishes no executable rate"

                result.append({
                    "venue": "Exactly", "chain_id": chain_id, "market_address": market,
                    "instrument_id": None if match is None else match["instrument_id"],
                    "maturity": datetime.fromtimestamp(position["maturity"], UTC).strftime("%Y-%m-%d"),
                    "matured": matured,
                    "underlying_asset_id": underlying, "decimals": decimals,
                    "principal_raw_amount": str(position["principal"]), "fee_raw_amount": str(position["fee"]),
                    "principal_usd_micros": principal_usd,
                    "at_maturity_usd_micros": face_usd,
                    "present_value_usd_micros": present_value, "valuation_basis": valuation_basis,
                })
            for position in read.get("midnight_positions", []):
                chain_id = read["chain_id"]
                row = position["row"]
                loan = row.get("loan_token")
                loan_meta = loan if isinstance(loan, Mapping) else {}
                address = position["loan_address"]
                known = next((info for token, info in read["known"].items()
                              if token is not None and token.lower() == address), None)
                decimals = loan_meta.get("decimals")
                known_decimals = None if known is None else known.get("decimals")
                if type(decimals) is int and type(known_decimals) is int and decimals != known_decimals:
                    decimals = None
                elif type(decimals) is not int:
                    decimals = known_decimals
                if type(decimals) is not int or not 0 <= decimals <= 255:
                    decimals = None
                underlying = f"eip155:{chain_id}/erc20:{address}"
                price = None
                if self.api.current_prices is not None:
                    observation = self.api.current_prices.observation(chain_id, address, identity=underlying)
                    if (observation is not None and
                            0 <= (self.clock() - observation.observed_at).total_seconds() <= MAX_AGE_SECONDS):
                        price = observation
                maturity_ts = _tenor_maturity(row)
                try:
                    maturity_date = (datetime.fromtimestamp(maturity_ts, UTC).strftime("%Y-%m-%d")
                                     if maturity_ts is not None else None)
                except (OverflowError, OSError, ValueError):
                    maturity_ts, maturity_date = None, None
                # Midnight debt accrues through a separate path; only value lender credit here.
                try:
                    accrued = (None if maturity_ts is None else accrue_position(
                        position["position"], position["loss_factor"], maturity_ts, position["at"]))
                except (KeyError, TypeError, ValueError):
                    accrued = None
                if accrued is None:
                    credit, pending_fee, face_value = None, None, None
                else:
                    credit, pending_fee, _accrued_fee = accrued
                    face_value = credit - pending_fee
                face_usd = (None if face_value is None or price is None or decimals is None else
                            raw_to_usd_micros(face_value, decimals=decimals,
                                              price_usd_fixed=price.price_usd_fixed))
                as_of_utc = as_of.astimezone(UTC)
                elapsed = as_of_utc - datetime(1970, 1, 1, tzinfo=UTC)
                as_of_micros = ((elapsed.days * 86_400 + elapsed.seconds) * 1_000_000
                                + elapsed.microseconds)
                matured = None if maturity_ts is None else maturity_ts * 1_000_000 <= as_of_micros
                rate_bps = row.get("rate_bps")
                has_rate = type(rate_bps) is int and rate_bps >= 0
                principal_usd = None
                if matured is True:
                    principal_usd = face_usd
                    valuation_basis = "matured; face value"
                elif has_rate and maturity_ts is not None:
                    remaining_micros = maturity_ts * 1_000_000 - as_of_micros
                    year_micros = 365 * 86_400 * 1_000_000
                    if face_usd is not None:
                        principal_usd = (face_usd * 10_000 * year_micros
                                         // (10_000 * year_micros + rate_bps * remaining_micros))
                    valuation_basis = "face value discounted at the venue's rate for the remaining term"
                else:
                    valuation_basis = "face value; the venue publishes no rate to discount at"
                income = row.get("fixed_income") if isinstance(row.get("fixed_income"), Mapping) else {}
                published_maturity_timestamp = row.get("maturity_timestamp")
                if not _valid_tenor_timestamp(published_maturity_timestamp):
                    published_maturity_timestamp = income.get("maturity_timestamp")
                maturity_value = row.get("maturity", income.get("maturity"))
                if not _valid_tenor_timestamp(published_maturity_timestamp) \
                        and isinstance(maturity_value, str) \
                        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", maturity_value):
                    valuation_basis += "; maturity hour unknown"
                result.append({
                    "venue": row.get("venue") if row.get("venue") in MIDNIGHT_VENUES else "Tenor",
                    "chain_id": chain_id, "market_id": position["market_id"],
                    "maturity": maturity_date,
                    "matured": matured,
                    "underlying_asset_id": underlying, "decimals": decimals,
                    "credit_raw_amount": None if credit is None else str(credit),
                    "debt_raw_amount": str(position["debt"]),
                    "pending_fee_raw_amount": None if pending_fee is None else str(pending_fee),
                    "face_value_raw_amount": None if face_value is None else str(face_value),
                    "at_maturity_usd_micros": face_usd, "principal_usd_micros": principal_usd,
                    "present_value_usd_micros": principal_usd,
                    "valuation_basis": valuation_basis,
                })
        return result


def _maturity(row: Mapping[str, Any]) -> int | None:
    income = row.get("fixed_income") or {}
    try:
        return int(datetime.fromisoformat(f"{income.get('maturity')}T00:00:00+00:00").timestamp())
    except (TypeError, ValueError):
        return None


def _valid_tenor_timestamp(value: Any) -> bool:
    if type(value) is not int or value < 0:
        return False
    try:
        datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _tenor_maturity(row: Mapping[str, Any]) -> int | None:
    """Read a Tenor maturity as Unix seconds or a strict UTC date, without numeric coercion."""
    timestamp = row.get("maturity_timestamp")
    income = row.get("fixed_income") if isinstance(row.get("fixed_income"), Mapping) else {}
    if not _valid_tenor_timestamp(timestamp):
        timestamp = income.get("maturity_timestamp")
    if _valid_tenor_timestamp(timestamp):
        return timestamp
    value = row.get("maturity", income.get("maturity"))
    if _valid_tenor_timestamp(value):
        return value
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                at = datetime.fromisoformat(value).replace(tzinfo=UTC, hour=23, minute=59, second=59)
                return int(at.timestamp())
            except (OverflowError, OSError, ValueError):
                return None
        if re.fullmatch(r"\d+", value):
            try:
                timestamp = int(value)
                return timestamp if _valid_tenor_timestamp(timestamp) else None
            except ValueError:
                return None
    return None
