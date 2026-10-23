"""LI.FI clients.

Two surfaces on two different hosts — do not mix them up:

- **Earn Data API** (`https://earn.li.fi`) — vault discovery and portfolio. Requires an
  `x-lifi-api-key` header. This is NOT on `li.quest`; every `li.quest/v1/earn*` path 404s.
- **Composer / routing** (`https://li.quest`) — quotes and executable transactions. Keyless.
  Composer activates automatically when `toToken` is a protocol's vault/deposit token, which is
  how a swap + bridge + deposit collapses into one transaction.

The key is read from the environment (`LIFI_API_KEY`), falling back to the repo's gitignored
`.env`. It is never written into a cache file: `HttpClient` archives the response body and the
method+URL, never request headers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


EARN_BASE = "https://earn.li.fi"
QUOTE_BASE = "https://li.quest"
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_api_key() -> str | None:
    """`LIFI_API_KEY` from the environment, else from the repo's gitignored `.env`."""
    key = os.environ.get("LIFI_API_KEY")
    if key:
        return key.strip()
    if not _ENV_PATH.exists():
        return None
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == "LIFI_API_KEY":
            return value.strip().strip('"').strip("'") or None
    return None


@dataclass(frozen=True)
class EarnVault:
    """One depositable venue, normalised.

    `redeem_steps` is the field that matters for allocation: "instant" means the reserve can be
    recalled in one transaction, anything else means the exit is staged and the position is not
    a true cash equivalent.
    """

    protocol: str
    name: str
    chain: str
    chain_id: int
    address: str
    apy_total: float | None
    apy_base: float | None
    apy_reward: float | None
    apy_30d: float | None
    tvl_usd: float | None
    underlying: tuple[str, ...]
    redeem_steps: str | None
    is_redeemable: bool
    tags: tuple[str, ...]

    @property
    def instant_exit(self) -> bool:
        return self.is_redeemable and self.redeem_steps == "instant"


class LifiEarnClient:
    """Vault discovery. Live requests require a key; offline cache replay does not."""

    def __init__(self, http: HttpClient, api_key: str | None = None) -> None:
        self.http = http
        self.api_key = api_key if api_key is not None else load_api_key()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            if self.http.offline:
                return {}
            raise SourceError(
                "LI.FI Earn needs LIFI_API_KEY (set it in the environment or the repo's .env). "
                "The keyless li.quest quote endpoint still works without it."
            )
        return {"x-lifi-api-key": self.api_key}

    def protocols(self) -> list[dict[str, Any]]:
        url = f"{EARN_BASE}/v1/protocols"
        payload = self.http.request_json("lifi", "earn_protocols", "GET", url, headers=self._headers())
        return [require_mapping(item, url) for item in require_list(payload, url)]

    def vaults(self, chain_id: int | None = None, *, limit: int = 100, tags: str | None = None) -> list[dict[str, Any]]:
        url = f"{EARN_BASE}/v1/vaults"
        params: dict[str, Any] = {"limit": limit}
        if chain_id is not None:
            params["chainId"] = chain_id
        if tags:
            params["tags"] = tags
        name = f"earn_vaults_{chain_id or 'all'}_{tags or 'any'}"
        payload = require_mapping(self.http.request_json("lifi", name, "GET", url, params=params, headers=self._headers()), url)
        return [require_mapping(item, url) for item in require_list(require_key(payload, "data", url), url)]

    @staticmethod
    def normalize_vault(raw: dict[str, Any]) -> EarnVault:
        analytics = raw.get("analytics") if isinstance(raw.get("analytics"), dict) else {}
        apy = analytics.get("apy") if isinstance(analytics.get("apy"), dict) else {}
        tvl = analytics.get("tvl") if isinstance(analytics.get("tvl"), dict) else {}
        protocol = raw.get("protocol") if isinstance(raw.get("protocol"), dict) else {}
        redeem = raw.get("redeemPacks") if isinstance(raw.get("redeemPacks"), list) else []
        steps = redeem[0].get("stepsType") if redeem and isinstance(redeem[0], dict) else None
        tokens = raw.get("underlyingTokens") if isinstance(raw.get("underlyingTokens"), list) else []
        return EarnVault(
            protocol=str(protocol.get("id") or "COULD NOT VERIFY"),
            name=str(raw.get("name") or "COULD NOT VERIFY"),
            chain=str(raw.get("network") or "COULD NOT VERIFY"),
            chain_id=int(raw.get("chainId") or 0),
            address=str(raw.get("address") or ""),
            apy_total=_number(apy.get("total")),
            apy_base=_number(apy.get("base")),
            apy_reward=_number(apy.get("reward")),
            apy_30d=_number(analytics.get("apy30d")),
            tvl_usd=_number(tvl.get("usd")),
            underlying=tuple(str(t.get("symbol")) for t in tokens if isinstance(t, dict) and t.get("symbol")),
            redeem_steps=str(steps) if steps else None,
            is_redeemable=bool(raw.get("isRedeemable")),
            tags=tuple(str(t) for t in (raw.get("tags") or []) if isinstance(t, str)),
        )


class LifiQuoteClient:
    """Composer / routing. No API key required."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def chains(self) -> list[dict[str, Any]]:
        url = f"{QUOTE_BASE}/v1/chains"
        payload = require_mapping(self.http.request_json("lifi", "quote_chains", "GET", url), url)
        return [require_mapping(item, url) for item in require_list(require_key(payload, "chains", url), url)]

    def quote(self, *, from_chain: int, to_chain: int, from_token: str, to_token: str,
              from_amount: str, from_address: str) -> dict[str, Any]:
        """Executable route. Setting `to_token` to a vault token activates Composer."""
        url = f"{QUOTE_BASE}/v1/quote"
        params = {
            "fromChain": from_chain, "toChain": to_chain,
            "fromToken": from_token, "toToken": to_token,
            "fromAmount": from_amount, "fromAddress": from_address,
        }
        name = f"quote_{from_chain}_{to_chain}_{to_token}_{from_amount}"
        return require_mapping(self.http.request_json("lifi", name, "GET", url, params=params), url)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None
