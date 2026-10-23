"""What the fee recipient holds on every chain buyers pay on, once a day.

LI.FI pays Fenorium's share of each route to the recipient's address on the chain the buyer paid
on, in the token they paid with. So for every paying chain this reads, at one pinned block, the
recipient's balance of the chain's native coin, its cash tokens and every token LI.FI routes
there, and whether a contract (the Safe) is deployed at the address yet -- checked against what
the chain registry states, because fees landing where the Safe is not deployed cannot be moved
until it is.

Values come from the current-price feed when it has the token, and are left unstated otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from engine.acquire.jobs.current_prices import MAX_AGE_SECONDS
from engine.composer import NATIVE_TOKEN_SENTINELS
from engine.onchain import ChainReader, OnchainError, read_at_pinned_block
from engine.portfolio.contracts import safe_detail
from engine.portfolio.valuation import raw_to_usd_micros
from engine.registry import asset_id, registry

from ..runner import Context, Item, ItemResult, bound_reader


SCHEMA = "fenorium/fee-balances/1"
SOURCE = "rpc"
# The Safe's getThreshold(): a deployed Safe answers it, an address without code answers nothing. It
# is asked by eth_call at the pinned block's hash, which every chain serves; eth_getCode by hash is
# not served everywhere (Pharos).
SAFE_GET_THRESHOLD = "0xe75235b8"


@dataclass(slots=True)
class FeeBalancesJob:
    reader_factory: Callable[..., Any] = ChainReader
    name: str = "fee-balances"
    artifact: str = "fee-balances.json"
    batch_size: int = 1
    max_failure_ratio: float = 0.5
    resume_within: timedelta | None = timedelta(hours=6)
    _tokens: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    def items(self, context: Context) -> Sequence[Item]:
        listed = context.read_json("lifi_tokens") or {}
        chains = listed.get("chains") if isinstance(listed, Mapping) else None
        self._tokens = {int(key): row.get("tokens") or [] for key, row in (chains or {}).items()
                        if isinstance(row, Mapping)}
        return [Item(str(chain.chain_id), chain.chain_id) for chain in registry().with_role("pay")]

    def fetch(self, context: Context, batch: Sequence[Item]) -> Mapping[str, ItemResult]:
        (item,) = batch
        chain = registry().require(item.payload)
        recipient = registry().fee_recipient.lower()
        decimals: dict[str | None, int] = {}
        symbols: dict[str | None, str] = {}
        if chain.offer_native:
            decimals[None], symbols[None] = chain.native_decimals, chain.native_symbol
        for token in ([chain.usdc] if chain.usdc else []) + list(chain.settlement):
            decimals[token.address.lower()], symbols[token.address.lower()] = token.decimals, token.symbol
        # The catalog keeps both native sentinels; the native balance is read through None, and
        # a balanceOf on a sentinel answers nothing and would count as an unread token.
        for token in self._tokens.get(chain.chain_id, []):
            address = str(token.get("address", "")).lower()
            if address and address not in NATIVE_TOKEN_SENTINELS and type(token.get("decimals")) is int:
                decimals.setdefault(address, token["decimals"])
                symbols.setdefault(address, str(token.get("symbol") or ""))
        try:
            reader = self.reader_factory(chain)
            item_deadline = context.fetch_deadlines.get(item.key)
            if isinstance(reader, ChainReader) and item_deadline is not None:
                bound_reader(reader, item_deadline, context.now)
            block, (balances, code) = read_at_pinned_block(
                reader, lambda pinned: (reader.balances(recipient, list(decimals), pinned),
                                        reader.eth_call(recipient, SAFE_GET_THRESHOLD, pinned)))
        except OnchainError as exc:
            return {item.key: ItemResult(ok=False, source=SOURCE, error=safe_detail(exc), transient=True)}
        held = [{"asset_id": asset_id(chain.chain_id, token), "symbol": symbols[token], "decimals": decimals[token],
                 "raw_amount": str(value)} for token, value in balances.items() if value]
        unread = sum(1 for value in balances.values() if value is None)
        token_catalog = "read" if chain.chain_id in self._tokens else "missing"
        return {item.key: ItemResult(ok=True, source=SOURCE, value={
            "block": block.to_dict(),
            "contract_deployed": isinstance(code, str) and len(code) > 2,
            "registry_states": chain.fee_safe,
            "balances": sorted(held, key=lambda row: row["asset_id"]),
            "unread_tokens": unread,
            "token_catalog": token_catalog,
        })}

    def build(self, context: Context, results: Mapping[str, ItemResult], items: Sequence[Item],
              previous: Any | None) -> dict[str, Any]:
        prices = context.read_json("prices_current") or {}
        priced = prices.get("assets") if isinstance(prices, Mapping) else None
        priced = priced if isinstance(priced, Mapping) else {}
        chains: dict[str, Any] = {}
        total = 0
        unvalued = 0
        token_catalog_missing = 0
        drift = []
        for item in items:
            result = results[item.key]
            if not result.ok:
                chains[item.key] = {"error": result.error}
                continue
            row = dict(result.value)
            if row.get("token_catalog") == "missing":
                token_catalog_missing += 1
            row["balances"] = [dict(balance) for balance in row["balances"]]
            for balance in row["balances"]:
                entry = priced.get(balance["asset_id"])
                if isinstance(entry, Mapping) and entry.get("status") == "PRICED":
                    try:
                        observed_at = datetime.strptime(entry.get("observed_at"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                        age = (context.now() - observed_at).total_seconds()
                    except (TypeError, ValueError):
                        age = None
                    if age is not None and 0 <= age <= MAX_AGE_SECONDS:
                        balance["value_usd_micros"] = raw_to_usd_micros(
                            int(balance["raw_amount"]), decimals=balance["decimals"], price_usd_fixed=entry["price_usd_fixed"])
                        balance["price_observed_at"] = entry["observed_at"]
                        total += balance["value_usd_micros"]
                    else:
                        balance["value_usd_micros"] = None
                        balance["unvalued_reason"] = "PRICE_STALE"
                        unvalued += 1
                else:
                    balance["value_usd_micros"] = None
                    balance["unvalued_reason"] = "UNPRICED"
                    unvalued += 1
            deployed = row["contract_deployed"]
            if deployed != (row["registry_states"] == "deployed"):
                # The registry says one thing and the chain another: whichever changed, the registry
                # must be corrected, since buyers' fees are routed by it.
                drift.append(item.key)
            chains[item.key] = row
        return {
            "schema_version": SCHEMA,
            "generated_at": context.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recipient": registry().fee_recipient.lower(),
            "chains": chains,
            "totals": {"value_usd_micros": total, "unvalued_balances": unvalued,
                       "registry_drift": sorted(drift, key=int),
                       "token_catalog_missing_chains": token_catalog_missing},
        }

    def validate(self, document: Mapping[str, Any], previous: Any | None) -> None:
        if document.get("schema_version") != SCHEMA or not document.get("chains"):
            raise ValueError("fee balance artifact is malformed or empty")
