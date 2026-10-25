"""Canonical, single raw IPFS block commitments to the published score series."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import json
import math

from engine.manifest import cid_v1_raw, require_cid_v1_raw
from engine.rating import RATING_METHOD_VERSION

FIELDS = ("chain_id", "chain", "venue", "name", "category", "address", "market_id", "market_address",
          "maturity", "fenorium_score", "score_cap", "verdict", "rate_bps", "rate_kind", "tier", "reason")
MAX_BYTES = 1_000_000


def _integers(value, field):
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"non-integer float in {field}")
        return int(value)
    if isinstance(value, Mapping):
        return {key: _integers(item, f"{field}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_integers(item, f"{field}[{i}]") for i, item in enumerate(value)]
    return value


def instrument_key(row: Mapping) -> str:
    address = row.get("address") or row.get("market_id") or row.get("market_address")
    if address:
        return str(address).lower()
    return "name:" + "|".join(str(row.get(field) or "").lower() for field in ("venue", "chain", "name"))


def instrument_identity(row: Mapping) -> tuple[str, str]:
    return instrument_key(row), row.get("maturity") or ""


def build_snapshot(universe: Mapping) -> bytes:
    generated_at = universe["generated_at"]
    instant = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    rows = [_integers({key: row.get(key) for key in FIELDS}, f"instruments[{i}]")
            for i, row in enumerate(universe["instruments"])
            if row.get("category") not in {"stablecoin", "settlement"}]
    rows.sort(key=lambda row: (row["chain_id"] if row["chain_id"] is not None else -1,
                              row["venue"] or "", instrument_key(row), row["maturity"] or "", row["name"] or ""))
    scores = [row["fenorium_score"] for row in rows if type(row["fenorium_score"]) is int]
    obj = _integers({
        "schema": "fenorium/score-snapshot/1", "generated_at": generated_at,
        "generated_at_unix": int(instant.timestamp()),
        "method_version": (universe.get("rating_method") or {}).get("method_version", RATING_METHOD_VERSION),
        "instrument_count": len(rows), "scored_count": len(scores),
        "passing_count": sum(score >= 60 for score in scores), "instruments": rows,
    }, "snapshot")
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ValueError(f"snapshot exceeds {MAX_BYTES} bytes: must fit one raw IPFS block")
    return payload


def snapshot_summary(payload: bytes) -> dict:
    obj = json.loads(payload)
    return {"sha256": "0x" + sha256(payload).hexdigest(), "cid": require_cid_v1_raw(cid_v1_raw(payload)),
            **{key: obj[key] for key in ("generated_at", "generated_at_unix", "method_version",
                                       "instrument_count", "scored_count", "passing_count")}, "bytes": len(payload)}
