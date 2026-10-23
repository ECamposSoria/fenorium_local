from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


SUPPORTED_CHAINS = frozenset({1, 8453, 5042})


class MorphoMidnightClient:
    BASE = "https://api.morpho.org/v0/midnight/"

    def __init__(self, http: HttpClient) -> None:
        # HttpClient supplies the shared par-research User-Agent and fetch provenance.
        self.http = http

    def _pages(self, resource: str, chain_id: int, limit: int) -> list[dict[str, Any]]:
        if chain_id not in SUPPORTED_CHAINS:
            raise SourceError(f"Morpho Midnight: unsupported chain {chain_id}")
        rows: list[dict[str, Any]] = []
        cursor = None
        seen: set[str] = set()
        for page in range(60):
            params = {"chain_ids": chain_id, "limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            url = f"{self.BASE}{resource}?{urlencode(params)}"
            name = f"{resource}_{chain_id}" + (f"_p{page}" if page else "")
            payload = require_mapping(self.http.request_json("morpho_midnight", name, "GET", url), url)
            rows.extend(require_mapping(item, url) for item in
                        require_list(require_key(payload, "data", url), url))
            cursor = require_key(payload, "cursor", url)
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise SourceError(f"{url}: invalid or repeated cursor; list not read")
            seen.add(cursor)
        raise SourceError(f"{self.BASE}{resource}: exceeds 60 pages; list not read")

    def markets(self, chain_id: int) -> list[dict[str, Any]]:
        return self._pages("markets", chain_id, 50)

    def books(self, chain_id: int) -> list[dict[str, Any]]:
        return self._pages("books", chain_id, 20)
