"""Morpho's own API — the source of truth for Morpho vault rates.

Exists because third-party aggregators disagree with it. Measured 2026-08-18: LI.FI Earn reported
`SIRLOINUSDC` (Base) at 5.648% total, decomposed as 2.825% base + 2.727% reward, while Morpho
reported `netApy` 2.911% with `netApyExcludingRewards` identical — i.e. Morpho recognises no reward
component at all. A 274bp overstatement on a vault that was, at the time, the engine's selected
reserve. Eleven other Base venues agreed with LI.FI to within a few bp, so this is not a systematic
offset that could be corrected away; it has to be cross-checked per vault.

Two registries, and a vault in one is absent from the other:
  `vaults`     — V1 (Metamorpho). Rates live under `state { netApy }`.
  `vaultV2s`   — V2. Rates are top-level fields, and `state` does not exist on the type.
Both paginate; `pageInfo.countTotal` is the real total and `first` caps at 1000 per page. Filters
such as `address_in` are checksum-sensitive, so match case-insensitively over a full listing
instead of querying by address.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .http import HttpClient, require_key, require_list, require_mapping


ENDPOINT = "https://api.morpho.org/graphql"
_PAGE = 1000
_MARKET_PAGE = 500

_V1 = """{ vaults(first: %d, skip: %d, where: { chainId_in: [%d] }) {
  pageInfo { countTotal } items { address symbol state { netApy totalAssetsUsd } } } }"""

_V2 = """{ vaultV2s(first: %d, skip: %d, where: { chainId_in: [%d] }) {
  pageInfo { countTotal } items { address symbol netApy totalAssetsUsd } } }"""

_MARKET_ORACLES = """{ markets(first: %d, skip: %d, where: { chainId_in: [%s], listed: true }) {
  pageInfo { countTotal } items { chain { id } collateralAsset { address } oracle { address } } } }"""

_MARKET_FEEDS = """{ markets(first: %d, skip: %d, where: { chainId_in: [%d] }) {
  pageInfo { countTotal } items {
    collateralAsset { symbol address } loanAsset { symbol }
    oracle { address type data { ... on MorphoChainlinkOracleV2Data {
      baseFeedOne { address decimals } baseFeedTwo { address } quoteFeedOne { address }
    } } }
  } } }"""


@dataclass(frozen=True)
class MorphoUnderlyingFeed:
    oracle_address: str = field(compare=False)
    oracle_type: str | None = field(compare=False)
    feed_address: str
    feed_decimals: int
    collateral_symbol: str | None = field(compare=False)
    loan_symbol: str | None = field(compare=False)


class MorphoClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _page(self, query: str, root: str, name: str) -> dict[str, Any]:
        def has_root(payload: Any) -> bool:
            data = payload.get("data") if isinstance(payload, dict) else None
            block = data.get(root) if isinstance(data, dict) else None
            return isinstance(block, dict) and isinstance(block.get("items"), list)

        payload = require_mapping(self.http.request_json(
            "morpho", name, "POST", ENDPOINT, json_body={"query": query},
            validate=has_root,
        ), ENDPOINT)
        data = require_mapping(require_key(payload, "data", ENDPOINT), ENDPOINT)
        return require_mapping(require_key(data, root, ENDPOINT), ENDPOINT)

    def _collect(self, template: str, root: str, chain_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            block = self._page(template % (_PAGE, skip, chain_id), root, f"{root}_{chain_id}_{skip}")
            items = require_list(require_key(block, "items", ENDPOINT), ENDPOINT)
            out.extend(require_mapping(item, ENDPOINT) for item in items)
            total = (block.get("pageInfo") or {}).get("countTotal")
            skip += _PAGE
            if not items or not isinstance(total, int) or skip >= total:
                return out

    def net_apy_by_address(self, chain_id: int) -> dict[str, dict[str, Any]]:
        """`{lowercase address: {net_apy (fraction), tvl_usd, registry}}` across V1 and V2."""
        found: dict[str, dict[str, Any]] = {}
        for template, root, registry in ((_V1, "vaults", "V1"), (_V2, "vaultV2s", "V2")):
            for item in self._collect(template, root, chain_id):
                address = str(item.get("address") or "").lower()
                if not address:
                    continue
                state = item.get("state") if isinstance(item.get("state"), dict) else item
                net = state.get("netApy")
                if not isinstance(net, (int, float)):
                    continue
                found[address] = {
                    "net_apy": float(net),
                    "tvl_usd": state.get("totalAssetsUsd"),
                    "registry": registry,
                    "symbol": item.get("symbol"),
                }
        return found

    def market_oracles_by_collateral(self, chain_ids: tuple[int, ...]) -> dict[tuple[int, str], set[str]]:
        found: dict[tuple[int, str], set[str]] = {}
        skip = 0
        chain_list = ",".join(str(chain_id) for chain_id in chain_ids)
        while True:
            query = _MARKET_ORACLES % (_PAGE, skip, chain_list)
            block = self._page(query, "markets", f"market_oracles_{'_'.join(map(str, chain_ids))}_{skip}")
            items = require_list(require_key(block, "items", ENDPOINT), ENDPOINT)
            for raw in items:
                item = require_mapping(raw, ENDPOINT)
                chain = item.get("chain")
                collateral = item.get("collateralAsset")
                oracle = item.get("oracle")
                if not isinstance(chain, dict) or not isinstance(collateral, dict) or not isinstance(oracle, dict):
                    continue
                chain_id = chain.get("id")
                address = collateral.get("address")
                oracle_address = oracle.get("address")
                if not isinstance(chain_id, int) or not isinstance(address, str) or not isinstance(oracle_address, str):
                    continue
                found.setdefault((chain_id, address.lower()), set()).add(oracle_address.lower())
            total = (block.get("pageInfo") or {}).get("countTotal")
            skip += _PAGE
            if not items or not isinstance(total, int) or skip >= total:
                return found

    def market_feeds_by_collateral(
        self, chain_ids: tuple[int, ...]
    ) -> dict[tuple[int, str], set[MorphoUnderlyingFeed]]:
        """Resolve MorphoChainlinkOracleV2 one level down to `baseFeedOne`."""
        found: dict[tuple[int, str], set[MorphoUnderlyingFeed]] = {}
        for chain_id in chain_ids:
            skip = 0
            while True:
                query = _MARKET_FEEDS % (_MARKET_PAGE, skip, chain_id)
                block = self._page(query, "markets", f"market_feeds_{chain_id}_{skip}")
                items = require_list(require_key(block, "items", ENDPOINT), ENDPOINT)
                for raw in items:
                    item = require_mapping(raw, ENDPOINT)
                    collateral = item.get("collateralAsset")
                    loan = item.get("loanAsset")
                    oracle = item.get("oracle")
                    if not isinstance(collateral, dict) or not isinstance(oracle, dict):
                        continue
                    data = oracle.get("data")
                    base_feed = data.get("baseFeedOne") if isinstance(data, dict) else None
                    address = collateral.get("address")
                    oracle_address = oracle.get("address")
                    feed_address = base_feed.get("address") if isinstance(base_feed, dict) else None
                    decimals = base_feed.get("decimals") if isinstance(base_feed, dict) else None
                    if not isinstance(address, str) or not isinstance(oracle_address, str) \
                            or not isinstance(feed_address, str) or isinstance(decimals, bool) \
                            or not isinstance(decimals, int):
                        continue
                    feed = MorphoUnderlyingFeed(
                        oracle_address=oracle_address.lower(),
                        oracle_type=str(oracle.get("type")) if oracle.get("type") is not None else None,
                        feed_address=feed_address.lower(),
                        feed_decimals=decimals,
                        collateral_symbol=str(collateral.get("symbol"))
                            if collateral.get("symbol") is not None else None,
                        loan_symbol=str(loan.get("symbol"))
                            if isinstance(loan, dict) and loan.get("symbol") is not None else None,
                    )
                    found.setdefault((chain_id, address.lower()), set()).add(feed)
                total = (block.get("pageInfo") or {}).get("countTotal")
                skip += _MARKET_PAGE
                if not items or not isinstance(total, int) or skip >= total:
                    break
        return found
