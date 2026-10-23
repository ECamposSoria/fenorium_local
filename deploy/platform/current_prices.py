"""Current USD prices published by the acquisition job (engine/acquire/jobs/current_prices.py).

The job writes `prices-current.json` every few minutes; this reloads it when the file changes.
An absent or unreadable file means no prices, never a stale copy presented as current: every
answer carries the source's own observation time, and the caller judges its age.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from engine.acquire.jobs.current_prices import MAX_AGE_SECONDS
from engine.portfolio import DEFILLAMA_CURRENT, PriceObservation
from engine.registry import asset_id


SCHEMA = "fenorium/prices-current/1"
MAX_BYTES = 16 * 1024 * 1024


def _utc(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        return None


class CurrentPrices:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._stamp: tuple[int, int] | None = None
        self._document: dict[str, Any] | None = None

    def document(self) -> dict[str, Any] | None:
        with self._lock:
            try:
                stat = self.path.stat()
            except OSError:
                self._stamp, self._document = None, None
                return None
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._stamp:
                document = None
                if stat.st_size <= MAX_BYTES:
                    try:
                        parsed = json.loads(self.path.read_bytes().decode("utf-8"))
                    except (OSError, UnicodeDecodeError, ValueError):
                        parsed = None
                    if (isinstance(parsed, dict) and parsed.get("schema_version") == SCHEMA
                            and isinstance(parsed.get("assets"), dict)):
                        document = parsed
                self._stamp, self._document = stamp, document
            return self._document

    def entry(self, asset: str) -> Mapping[str, Any] | None:
        document = self.document()
        row = (document or {}).get("assets", {}).get(asset)
        return row if isinstance(row, Mapping) else None

    def observation(self, chain_id: int, address: str | None, *, identity: str) -> PriceObservation | None:
        """This holding's published price as a valuation observation, or None when unpriced."""
        try:
            key = asset_id(chain_id, address)
        except ValueError:
            return None
        row = self.entry(key)
        if row is None or row.get("status") != "PRICED":
            return None
        price, observed = row.get("price_usd_fixed"), _utc(row.get("observed_at"))
        if type(price) is not int or price <= 0 or observed is None:
            return None
        return PriceObservation(
            identity=identity,
            source_id=f"DefiLlama current price {row.get('source_key')} observed_at={row.get('observed_at')}",
            independence_key=DEFILLAMA_CURRENT,
            price_usd_fixed=price,
            observed_at=observed.astimezone(UTC),
            method_version=str((self.document() or {}).get("method_version") or "defillama-current"),
        )
