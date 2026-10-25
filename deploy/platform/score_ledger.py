"""Read-only access to immutable ledger snapshots beside the mounted universe."""
import json
from datetime import date
from pathlib import Path
import re
from threading import RLock

from engine.score_ledger import instrument_identity


def positive_parameter(params: dict, name: str) -> int:
    values = params.get(name, [])
    if len(values) != 1 or not re.fullmatch(r"[0-9]{1,20}", values[0]) or int(values[0]) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(values[0])


def maturity_parameter(params: dict) -> str:
    if "maturity" not in params:
        return ""
    values = params["maturity"]
    if len(values) != 1 or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", values[0]):
        raise ValueError("maturity must be YYYY-MM-DD")
    date.fromisoformat(values[0])
    return values[0]


class ScoreLedger:
    def __init__(self, directory: Path):
        self.directory = directory
        self._cache = {}
        self._cache_hashes = {}
        self._lock = RLock()

    def index(self) -> dict:
        try:
            entries = json.loads((self.directory / "index.json").read_bytes())
        except FileNotFoundError:
            entries = []
        entries = sorted(entries, key=lambda entry: entry["generated_at_unix"])
        active = [entry for entry in entries if not entry.get("superseded_by")]
        latest = active[-1] if active else {}
        return {"entries": entries, "contract": latest.get("contract"), "chain_id": latest.get("chain_id")}

    def snapshot(self, stamp: int, snapshot_id: int) -> bytes:
        return (self.directory / f"{stamp}-{snapshot_id}.json").read_bytes()

    def history(self, chain_id: int | None, key: str, maturity: str = "") -> list:
        points = []
        for entry in self.index()["entries"]:
            if entry.get("superseded_by"):
                continue
            stamp = entry["generated_at_unix"]
            with self._lock:
                if self._cache_hashes.get(stamp) != entry["sha256"]:
                    document = json.loads(self.snapshot(stamp, entry["snapshot_id"]))
                    compact = {}
                    for row in document["instruments"]:
                        identity = (row.get("chain_id"), *instrument_identity(row))
                        compact.setdefault(identity, (row.get("fenorium_score"), row.get("verdict")))
                    self._cache[stamp] = compact
                    self._cache_hashes[stamp] = entry["sha256"]
                score = self._cache[stamp].get((chain_id, key, maturity))
            if score is not None:
                points.append({"generated_at": entry["generated_at"], "fenorium_score": score[0],
                               "verdict": score[1], **{name: entry[name] for name in
                               ("snapshot_id", "tx_hash", "cid", "chain_id", "generated_at_unix", "sha256")}})
        return points
