"""The tokens LI.FI can route on every chain a buyer may pay from, once a day.

This is the set a pay option is drawn from and the list a wallet's balances are read against: a
token LI.FI does not list cannot be swapped by it, whatever it is worth. Tokens LI.FI's screening
flagged are left out; unverified ones are kept with their status, because on several younger
chains nothing is verified yet.

One item per chain. A chain that does not answer keeps yesterday's list, marked as carried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import re
from typing import Any, Callable, Mapping, Sequence

from engine.composer import NATIVE_TOKEN_SENTINELS as _SENTINELS
from engine.onchain import ChainReader, OnchainError, read_at_pinned_block
from engine.portfolio.contracts import safe_detail
from engine.registry import registry

from ..fetch import FetchError, Fetcher
from ..runner import Context, Item, ItemResult, bound_reader


SCHEMA = "fenorium/lifi-tokens/1"
SOURCE = "lifi"
URL = "https://li.quest/v1/tokens?chains={chain_id}"
API_KEY_VARIABLES = ("FENORIUM_LIFI_API_KEY", "LIFI_API_KEY")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
EXCLUDED = frozenset({"flagged"})
# An address that holds nothing, asked for its balance to tell a readable token from a broken one.
PROBE_HOLDER = "0x000000000000000000000000000000000000dEaD"


def _token(raw: Any, chain_id: int) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or raw.get("chainId") != chain_id:
        return None
    address, decimals, symbol = raw.get("address"), raw.get("decimals"), raw.get("symbol")
    if (not isinstance(address, str) or _ADDRESS.fullmatch(address) is None
            or type(decimals) is not int or not 0 <= decimals <= 36
            or not isinstance(symbol, str) or not symbol.strip()):
        return None
    status = raw.get("verificationStatus") if isinstance(raw.get("verificationStatus"), str) else "unknown"
    if status in EXCLUDED:
        return None
    price = raw.get("priceUSD")
    return {
        "address": address.lower(),
        "symbol": symbol[:32],
        "name": str(raw.get("name") or "")[:64],
        "decimals": decimals,
        # LI.FI's indicative price: used to rank and to show, never to value a holding.
        "price_usd": price if isinstance(price, str) and len(price) <= 40 else None,
        "coin_key": raw.get("coinKey") if isinstance(raw.get("coinKey"), str) else None,
        "verification": status,
    }


@dataclass(slots=True)
class LifiTokensJob:
    fetcher: Fetcher = field(default_factory=lambda: Fetcher(timeout_seconds=60, retries=3, budget_seconds=240))
    reader_factory: Callable[..., Any] = ChainReader
    name: str = "lifi-tokens"
    artifact: str = "lifi-tokens.json"
    batch_size: int = 1
    max_failure_ratio: float = 0.5
    resume_within: timedelta | None = timedelta(hours=30)

    @property
    def fetch_budget_seconds(self) -> float:
        return float(getattr(self.fetcher, "budget_seconds", 240.0))

    def items(self, context: Context) -> Sequence[Item]:
        return [Item(str(chain.chain_id), chain.chain_id) for chain in registry().with_role("pay")]

    def fetch(self, context: Context, batch: Sequence[Item]) -> Mapping[str, ItemResult]:
        (item,) = batch
        key = next((context.environ.get(name, "").strip() for name in API_KEY_VARIABLES
                    if context.environ.get(name, "").strip()), "")
        try:
            request_options = {"headers": {"x-lifi-api-key": key} if key else None}
            if item.key in context.fetch_budgets and isinstance(self.fetcher, Fetcher):
                request_options["budget_seconds"] = context.fetch_budgets[item.key]
            payload = self.fetcher.get_json(URL.format(chain_id=item.payload),
                                            **request_options)
        except FetchError as exc:
            return {item.key: ItemResult(ok=False, source=SOURCE, error=safe_detail(exc), transient=exc.transient,
                                         retry_after=exc.retry_after)}
        tokens = payload.get("tokens") if isinstance(payload, Mapping) else None
        listed = tokens.get(item.key) if isinstance(tokens, Mapping) else None
        if not isinstance(listed, list):
            return {item.key: ItemResult(ok=False, source=SOURCE, error="response lists no tokens for this chain",
                                         transient=True)}
        kept = {}
        for raw in listed:
            token = _token(raw, item.payload)
            if token is not None:
                kept.setdefault(token["address"], token)
        unreadable = self._unreadable(context, item.payload,
                                      [address for address in kept if address not in _SENTINELS])
        return {item.key: ItemResult(ok=True, source=SOURCE, value={
            "fetched_at": context.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "listed": len(listed),
            "tokens": sorted((token for address, token in kept.items() if address not in (unreadable or ())),
                             key=lambda token: token["address"]),
            # Listed, but whose balanceOf reverts for any holder: no wallet read could ever see them.
            # None when the probe could not run, in which case nothing was left out.
            "unreadable": None if unreadable is None else sorted(unreadable),
        })}

    def _unreadable(self, context: Context, chain_id: int, addresses: list[str]) -> set[str] | None:
        """Tokens whose balanceOf fails for an address that holds nothing, or None if not probed."""
        if not addresses:
            return set()
        try:
            reader = self.reader_factory(registry().require(chain_id))
            item_deadline = context.fetch_deadlines.get(str(chain_id))
            if isinstance(reader, ChainReader) and item_deadline is not None:
                bound_reader(reader, item_deadline, context.now)
            _, balances = read_at_pinned_block(reader, lambda block: reader.balances(PROBE_HOLDER, addresses, block))
        except OnchainError:
            return None
        return {address for address, value in balances.items() if value is None}

    def build(self, context: Context, results: Mapping[str, ItemResult], items: Sequence[Item],
              previous: Any | None) -> dict[str, Any]:
        earlier = (previous or {}).get("chains") if isinstance(previous, Mapping) else None
        earlier = earlier if isinstance(earlier, Mapping) else {}
        chains: dict[str, Any] = {}
        for item in items:
            result = results[item.key]
            if result.ok:
                chains[item.key] = result.value
            elif isinstance(earlier.get(item.key), Mapping):
                chains[item.key] = {**earlier[item.key], "carried": True}
        return {
            "schema_version": SCHEMA,
            "generated_at": context.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": SOURCE,
            "excluded_verification": sorted(EXCLUDED),
            "chains": chains,
            "counts": {"chains": len(chains), "tokens": sum(len(row["tokens"]) for row in chains.values())},
        }

    def validate(self, document: Mapping[str, Any], previous: Any | None) -> None:
        chains = document.get("chains")
        if document.get("schema_version") != SCHEMA or not isinstance(chains, Mapping) or not chains:
            raise ValueError("token artifact is malformed or empty")
        for chain_id, row in chains.items():
            if registry().get(int(chain_id)) is None:
                raise ValueError(f"chain {chain_id} is not in the registry")
            for token in row["tokens"]:
                if _ADDRESS.fullmatch(token["address"]) is None or type(token["decimals"]) is not int:
                    raise ValueError(f"chain {chain_id} lists a malformed token")
        before = (previous or {}).get("counts", {}).get("tokens") if isinstance(previous, Mapping) else None
        if isinstance(before, int) and before >= 100 and document["counts"]["tokens"] * 2 < before:
            raise ValueError(f"routable tokens fell from {before} to {document['counts']['tokens']}")
