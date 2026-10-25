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
        self._lock = RLock()

    def index(self) -> dict:
        try:
            entries = json.loads((self.directory / "index.json").read_bytes())
        except FileNotFoundError:
            entries = []
        entries = sorted(entries, key=lambda entry: entry["generated_at_unix"])
        latest = entries[-1] if entries else {}
        return {"entries": entries, "contract": latest.get("contract"), "chain_id": latest.get("chain_id")}

    def snapshot(self, stamp: int) -> bytes:
        with self._lock:
            if stamp not in self._cache:
                raw = (self.directory / f"{stamp}.json").read_bytes()
                self._cache[stamp] = (raw, json.loads(raw))
            return self._cache[stamp][0]

    def history(self, chain_id: int | None, key: str, maturity: str = "") -> list:
        points = []
        for entry in self.index()["entries"]:
            stamp = entry["generated_at_unix"]
            self.snapshot(stamp)
            document = self._cache[stamp][1]
            row = next((row for row in document["instruments"]
                        if row.get("chain_id") == chain_id
                        and instrument_identity(row) == (key, maturity)), None)
            if row is not None:
                points.append({"generated_at": entry["generated_at"], "fenorium_score": row.get("fenorium_score"),
                               "verdict": row.get("verdict"), **{name: entry[name] for name in
                               ("snapshot_id", "tx_hash", "cid", "chain_id", "generated_at_unix", "sha256")}})
        return points
