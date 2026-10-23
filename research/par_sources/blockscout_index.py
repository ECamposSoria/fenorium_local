"""Token enumeration from Blockscout's multichain index.

This names candidate token contracts for a wallet. It is deliberately incapable of reporting a
balance: `TokenEnumerationSource` takes addresses only, so an index that is stale, incomplete,
or seeded by an airdropped token cannot put a number anywhere. The scanner reads every balance
itself, on chain, at its own finalized block.

The endpoint is the unified `api.blockscout.com/<chain_id>/api/v2/...` surface, which is not the
same as the per-instance v1 hosts in `blockscout.py`: one key serves every chain it covers.
BSC (56) is not among them and answers 404, so that chain stays registered-assets-only.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from engine.portfolio.discovery import EnumerationResult
from engine.registry import registry
from engine.portfolio.contracts import safe_detail


BASE = "https://api.blockscout.com"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# Chains the multichain index answers for (`blockscout_index` in the chain registry). BSC is not
# one: it 404s, and a chain that cannot be enumerated must say so rather than look like a wallet
# holding nothing.
ENUMERABLE_CHAIN_IDS = frozenset(chain.chain_id for chain in registry().chains if chain.blockscout_index)


class BlockscoutIndexError(RuntimeError):
    """The index did not answer usefully. Never raised to mean 'holds nothing'."""


def load_api_key() -> str | None:
    """`BLOCKSCOUT_API_KEY` from the environment, else from the repo's gitignored `.env`."""
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


class BlockscoutTokenIndex:
    """Conforms to `engine.portfolio.discovery.TokenEnumerationSource`."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_pages: int = 10,
    ) -> None:
        self.api_key = api_key if api_key is not None else load_api_key()
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages

    def _get(self, url: str) -> Any:
        request = urllib.request.Request(
            url, headers={"User-Agent": "par-research/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise BlockscoutIndexError(f"HTTP {exc.code} from {url.split('?')[0]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise BlockscoutIndexError(
                f"transport failure against {url.split('?')[0]}: {safe_detail(exc)}"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BlockscoutIndexError(f"malformed JSON from {url.split('?')[0]}: {exc}") from exc

    def tokens_held(self, chain_id: int, wallet: str, block: Any = None) -> EnumerationResult:
        """Every ERC-20 contract the index associates with this wallet, addresses only.

        This never establishes coverage, and says so. Two reasons, both structural:

        The endpoint reports the index's present view, while a scan asks about a specific
        finalized block. A token held at that block and disposed of before the read is simply
        absent from the answer, so a list that looks whole cannot be shown to be whole. Closing
        that needs a source that can be asked about a block -- SQL against a self-hosted
        instance, or an archive-backed index -- not a different flag here.

        A row the parser cannot use is also counted rather than quietly skipped. Dropping one
        silently is what turns an unreadable entry into apparent completeness.
        """

        if chain_id not in ENUMERABLE_CHAIN_IDS:
            raise BlockscoutIndexError(f"chain {chain_id} is not covered by the multichain index")
        query = urllib.parse.urlencode({"type": "ERC-20", **({"apikey": self.api_key} if self.api_key else {})})
        url = f"{BASE}/{chain_id}/api/v2/addresses/{wallet}/token-balances?{query}"
        payload = self._get(url)
        rows = payload if isinstance(payload, list) else payload.get("items")
        if not isinstance(rows, list):
            raise BlockscoutIndexError("token-balances did not return a list")

        addresses: list[str] = []
        seen: set[str] = set()
        unusable = 0
        for row in rows:
            if not isinstance(row, dict):
                unusable += 1
                continue
            token = row.get("token")
            if not isinstance(token, dict):
                unusable += 1
                continue
            # Only fungible tokens. An NFT is a position this scanner has no way to value, and
            # it is skipped by kind rather than discarded as unreadable.
            if str(token.get("type") or "").upper() not in ("ERC-20", "ERC20"):
                continue
            address = token.get("address") or token.get("address_hash")
            if (not isinstance(address, str) or len(address) != 42
                    or not address.startswith("0x")
                    or any(c not in "0123456789abcdefABCDEF" for c in address[2:])):
                unusable += 1
                continue
            lowered = address.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            addresses.append(lowered)

        detail = "the index reports its present view, not the requested finalized block"
        if unusable:
            detail += f"; {unusable} row(s) could not be read"
        return EnumerationResult(
            addresses=tuple(sorted(addresses)),
            establishes_coverage=False,
            detail=detail,
        )
