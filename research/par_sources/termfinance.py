from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


GRAPHQL = "https://api.global.termfinance.io/graphql"

# 11155111 is Sepolia. It answers like any other chain and its repos look live, so it is excluded
# by name rather than by hoping none show up.
TESTNET_CHAIN_IDS = {11155111}

RATE_SCALE = 1e18


@dataclass(frozen=True)
class TermFinanceRepo:
    chain_id: int
    repo_id: str
    servicer: str
    repo_token: str
    purchase_token: str
    purchase_symbol: str | None
    purchase_decimals: int | None
    collateral_symbols: tuple[str, ...]
    repurchase_at: datetime
    lend_rate: float | None
    lend_size: float | None
    lend_orders: int
    ask_rate: float | None
    endpoint: str


class TermFinanceClient:
    """Term Finance repos and the rate a lender could actually take today.

    The book is not crossed: a `lend` order is another lender's ask, and taking it is not
    something a lender can do. Measured 2026-08-19, lend asks sit at or above borrow bids on
    every repo carrying both sides (WETH 2.00% ask against a 1.75% bid). What a lender can
    execute is the best resting `borrow` bid, so that is what becomes the rate; a repo with no
    borrow bid has no executable lending rate and reports none rather than the ask.
    """

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def _query(self, name: str, query: str, *, prefer_cache_minutes: float | None = None) -> Any:
        payload = require_mapping(self.http.request_json(
            "term_finance", name, "POST", GRAPHQL,
            json_body={"query": query},
            validate=lambda candidate: isinstance(candidate, dict) and candidate.get("data") is not None,
            prefer_cache_minutes=prefer_cache_minutes,
        ), GRAPHQL)
        if payload.get("errors"):
            detail = json.dumps(payload["errors"])[:200]
            raise SourceError(f"{GRAPHQL} ({name}): GraphQL errors: {detail}")
        return require_key(payload, "data", GRAPHQL)

    def repos(self) -> list[dict[str, Any]]:
        query = """
        { termRepos(first: 500, where: { delisted: false }) {
            id chainId repurchaseTimestamp redemptionTimestamp
            termRepoServicer termRepoToken purchaseToken
            purchaseTokenMeta { symbol decimals }
            collateralTokensMeta { symbol }
        } }
        """
        data = require_mapping(self._query("repos", query, prefer_cache_minutes=10), GRAPHQL)
        return [require_mapping(row, GRAPHQL)
                for row in require_list(require_key(data, "termRepos", GRAPHQL), GRAPHQL)]

    def _orders(self, servicer: str, chain_id: int, kind: str) -> list[dict[str, Any]]:
        if kind not in {"lend", "borrow"}:
            raise ValueError("Term Finance order kind must be lend or borrow")
        # `orderKind` cannot be selected in the response — the resolver raises INTERNAL_SERVER_ERROR
        # on it — so it is used only to filter, and the side is known from the request instead.
        query = (
            '{ protocol_orders(termRepoServicerAddress: "%s", chainId: %d, orderKind: %s, '
            'orderState: unfilled, includeExpired: false, limit: 100) '
            '{ ... on OrderRecordList_response { orders '
            '{ id offerRate purchaseTokenAmount remainingAmount isSynthetic } } '
            '... on BadRequest_response { message } } }'
        ) % (servicer, chain_id, kind)
        data = require_mapping(
            self._query(f"orders_{kind}_{chain_id}_{servicer.lower()}", query, prefer_cache_minutes=10),
            GRAPHQL,
        )
        book = data.get("protocol_orders")
        if not isinstance(book, dict):
            return []
        if "message" in book:
            raise SourceError(f"{GRAPHQL}: protocol_orders rejected {servicer}: {book['message']}")
        orders = book.get("orders")
        return [row for row in orders if isinstance(row, dict)] if isinstance(orders, list) else []

    @staticmethod
    def _rate(order: dict[str, Any]) -> float | None:
        raw = order.get("offerRate")
        if raw is None:
            return None
        try:
            return int(raw) / RATE_SCALE
        except (TypeError, ValueError):
            return None

    def live_repos(self, now: datetime | None = None) -> list[TermFinanceRepo]:
        moment = now or datetime.now(UTC)
        result: list[TermFinanceRepo] = []
        for row in self.repos():
            chain_id = row.get("chainId")
            stamp = row.get("repurchaseTimestamp")
            if not isinstance(chain_id, int) or chain_id in TESTNET_CHAIN_IDS or stamp is None:
                continue
            try:
                repurchase = datetime.fromtimestamp(int(stamp), UTC)
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            if repurchase <= moment:
                continue
            servicer = str(row.get("termRepoServicer") or "")
            if not servicer:
                continue
            meta = row.get("purchaseTokenMeta") or {}
            decimals = meta.get("decimals")
            try:
                decimals = None if decimals is None else int(decimals)
            except (TypeError, ValueError):
                decimals = None

            bids = self._orders(servicer, chain_id, "borrow")
            asks = self._orders(servicer, chain_id, "lend")
            priced = [(rate, order) for order in bids if (rate := self._rate(order)) is not None]
            best = max(priced, key=lambda item: item[0], default=None)
            size = None
            if best is not None and decimals is not None:
                try:
                    size = int(best[1].get("remainingAmount") or 0) / 10**decimals
                except (TypeError, ValueError):
                    size = None
            ask_rates = [rate for order in asks if (rate := self._rate(order)) is not None]

            result.append(TermFinanceRepo(
                chain_id=chain_id,
                repo_id=str(row.get("id") or ""),
                servicer=servicer,
                repo_token=str(row.get("termRepoToken") or ""),
                purchase_token=str(row.get("purchaseToken") or ""),
                purchase_symbol=None if meta.get("symbol") is None else str(meta["symbol"]),
                purchase_decimals=decimals,
                collateral_symbols=tuple(
                    str((item or {}).get("symbol"))
                    for item in (row.get("collateralTokensMeta") or [])
                    if isinstance(item, dict) and item.get("symbol")
                ),
                repurchase_at=repurchase,
                lend_rate=None if best is None else best[0],
                lend_size=size,
                lend_orders=len(priced),
                ask_rate=min(ask_rates) if ask_rates else None,
                endpoint=f"POST {GRAPHQL} termRepos+protocol_orders",
            ))
        return result
