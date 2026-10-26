"""Offline snapshot/index operations and strict parsing of Foundry JSON output."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from engine.score_ledger import build_snapshot, snapshot_summary
from engine.keccak import keccak256

SNAPSHOT_EVENTS = {
    "0x" + keccak256(signature.encode()).hex() for signature in (
        "SnapshotRecorded(uint256,uint64,bytes32,string,string,uint32,uint32,uint32)",
        "SnapshotCorrected(uint256,uint256,uint64,bytes32,string,string)",
    )
}


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build(universe: Path, pending: Path) -> dict:
    payload = build_snapshot(json.loads(universe.read_bytes()))
    summary = snapshot_summary(payload)
    if not needs_recording(summary, pending.parent, reconcile=True):
        return summary
    name = str(summary["generated_at_unix"])
    target = pending / f"{name}.json"
    if target.exists() and target.read_bytes() != payload:
        raise ValueError("pending timestamp already has different content; correction must be recorded by hand")
    atomic_write(target, payload)
    atomic_write(pending / f"{name}.summary.json", json_bytes(summary))
    return summary


def index_add(ledger: Path, entry: dict, payload: bytes, supersedes: int = 0) -> bool:
    summary = snapshot_summary(payload)
    for key, value in summary.items():
        if key != "bytes" and entry.get(key) != value:
            raise ValueError(f"entry does not match snapshot: {key}")
    index = ledger / "index.json"
    entries = json.loads(index.read_bytes()) if index.exists() else []
    for old in entries:
        if old["generated_at_unix"] == entry["generated_at_unix"] and not old.get("superseded_by"):
            if old["sha256"] != entry["sha256"]:
                if not supersedes or supersedes != old["snapshot_id"] or entry["snapshot_id"] == supersedes:
                    raise ValueError("timestamp conflict; correction must be recorded by hand")
                old["superseded_by"] = entry["snapshot_id"]
            else:
                return False
    target = ledger / f'{entry["generated_at_unix"]}-{entry["snapshot_id"]}.json'
    if target.exists() and target.read_bytes() != payload:
        raise ValueError("snapshot file conflict; correction must be recorded by hand")
    if not target.exists():
        atomic_write(target, payload)
    entries.append(entry)
    entries.sort(key=lambda row: row["generated_at_unix"])
    atomic_write(index, json_bytes(entries))
    return True


def needs_recording(summary: dict, ledger: Path, reconcile: bool = False) -> bool:
    index = ledger / "index.json"
    for entry in json.loads(index.read_bytes()) if index.exists() else []:
        if entry["generated_at_unix"] == summary["generated_at_unix"] and not entry.get("superseded_by"):
            if entry["sha256"] != summary["sha256"]:
                if reconcile:
                    return True
                raise ValueError("timestamp conflict; correction must be recorded by hand")
            return False
    return True


def pending_generations(pending: Path, ledger: Path, reconcile: bool = False) -> list[int]:
    generations = []
    conflicts = []
    for path in pending.glob("*.summary.json"):
        summary = json.loads(path.read_bytes())
        try:
            needed = needs_recording(summary, ledger, reconcile)
        except ValueError:
            conflicts.append(summary["generated_at_unix"])
            needed = True
        if needed:
            generations.append(summary["generated_at_unix"])
    if conflicts:
        raise ValueError(f"pending generations: {sorted(generations)}; timestamp conflicts: {sorted(conflicts)}; "
                         "correction must be recorded by hand")
    return sorted(generations)


def build_new(universe: Path, pending: Path, processed: int) -> bool:
    summary = build(universe, pending)
    return (needs_recording(summary, pending.parent, reconcile=True)
            and summary["generated_at_unix"] > processed)


def remove_pending(pending: Path, stamp: int) -> None:
    for suffix in (".json", ".summary.json", ".tx.json", ".receipt.json"):
        (pending / f"{stamp}{suffix}").unlink(missing_ok=True)


def cast_number(value) -> int:
    if isinstance(value, list) and len(value) == 1:
        return cast_number(value[0])
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value.strip()):
        number = int(value, 16 if value.strip().startswith("0x") else 10)
    else:
        raise ValueError("invalid cast integer")
    if number < 0:
        raise ValueError("negative cast integer")
    return number


def cast_hash(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("invalid cast hash")
    return value.lower()


def snapshot_hash(value) -> str:
    # With no return signature cast emits ABI bytes, avoiding version-dependent tuple display.
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raw = value[2:]
        if len(raw) < 11 * 64 or len(raw) % 64 or int(raw[:64], 16) != 32:
            raise ValueError("invalid snapshotOf ABI output")
        return cast_hash("0x" + raw[128:192])
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, list) or len(value) != 10:
        raise ValueError("invalid snapshotOf output")
    return cast_hash(value[1])


def snapshot_recorded_at(value) -> int:
    snapshot_hash(value)  # Validate the same ABI/tuple shape before reading recordedAt.
    if isinstance(value, str):
        return cast_number("0x" + value[2 + 8 * 64:2 + 9 * 64])
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    return cast_number(value[7])


def snapshot_supersedes(value) -> int:
    snapshot_hash(value)
    if isinstance(value, str):
        return cast_number("0x" + value[2 + 9 * 64:2 + 10 * 64])
    while isinstance(value, list) and len(value) == 1:
        value = value[0]
    return cast_number(value[8])


def receipt_record(value: dict, expected_tx: str) -> dict:
    if cast_number(value.get("status")) != 1:
        raise ValueError("snapshot transaction failed")
    tx_hash = cast_hash(value.get("transactionHash"))
    if tx_hash != cast_hash(expected_tx):
        raise ValueError("receipt transaction mismatch")
    return {"tx_hash": tx_hash, "block_number": cast_number(value.get("blockNumber"))}


def tx_action(receipt_json: str, snapshot_id: int) -> str:
    snapshot_id = cast_number(snapshot_id)
    receipt = json.loads(receipt_json) if receipt_json.strip() else None
    if receipt is None:
        # Base already holds the record, so our saved hash was replaced (e.g. a sped-up tx):
        # the recording transaction is found from the event instead.
        return "recover" if snapshot_id else "wait"
    status = cast_number(receipt.get("status"))
    if status not in (0, 1):
        raise ValueError("invalid receipt status")
    cast_number(receipt.get("blockNumber"))
    if status == 0:
        return "recover" if snapshot_id else "resend"
    return "use"


def fail_transaction(summary_path: Path) -> None:
    summary = json.loads(summary_path.read_bytes())
    stamp = summary["generated_at_unix"]
    source = summary_path.parent / f"{stamp}.tx.json"
    target = summary_path.parent / f"{stamp}.tx.failed-{int(time.time())}.json"
    # Never overwrite evidence, including two failures within the same second.
    os.link(source, target)
    source.unlink()


def log_transaction(logs: list, snapshot_id: int, contract: str) -> str:
    matches = [log for log in logs if not log.get("removed", False)
               and log.get("address", "").lower() == contract.lower()
               and len(log.get("topics", [])) > 1 and log["topics"][0].lower() in SNAPSHOT_EVENTS
               and cast_number(log["topics"][1]) == snapshot_id]
    if len(matches) != 1:
        raise ValueError("cannot uniquely recover snapshot transaction")
    return cast_hash(matches[0]["transactionHash"])


def snapshot_tuple(summary: dict) -> str:
    return "(" + ",".join((str(summary["generated_at_unix"]), summary["sha256"],
                           json.dumps(summary["cid"]), json.dumps(summary["method_version"]),
                           *(str(summary[key]) for key in ("instrument_count", "scored_count", "passing_count")))) + ")"


def known_transaction(summary_path: Path, ledger: Path) -> str:
    summary = json.loads(summary_path.read_bytes())
    stamp = summary["generated_at_unix"]
    pending_tx = summary_path.parent / f"{stamp}.tx.json"
    if pending_tx.exists():
        saved = json.loads(pending_tx.read_bytes())
        if saved["sha256"] == summary["sha256"]:
            return cast_hash(saved["tx_hash"])
    index = ledger / "index.json"
    for entry in json.loads(index.read_bytes()) if index.exists() else []:
        if (entry["generated_at_unix"] == stamp and entry["sha256"] == summary["sha256"]
                and not entry.get("superseded_by")):
            return cast_hash(entry["tx_hash"])
    return ""


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build")
    command.add_argument("--universe", type=Path, default=Path("/curve/universe.json"))
    command.add_argument("--pending", type=Path, default=Path("/curve/ledger/pending"))
    command = commands.add_parser("build-new")
    command.add_argument("--universe", type=Path, default=Path("/curve/universe.json"))
    command.add_argument("--pending", type=Path, default=Path("/curve/ledger/pending"))
    command.add_argument("--processed", type=int, default=0)
    command = commands.add_parser("pending")
    command.add_argument("--reconcile", action="store_true")
    command.add_argument("--pending", type=Path, default=Path("/curve/ledger/pending"))
    command.add_argument("--ledger", type=Path, default=Path("/curve/ledger"))
    command = commands.add_parser("needs-recording")
    command.add_argument("summary", type=Path)
    command.add_argument("--ledger", type=Path, default=Path("/curve/ledger"))
    command = commands.add_parser("index-add")
    command.add_argument("--ledger", type=Path, default=Path("/curve/ledger"))
    command.add_argument("--summary", type=Path, required=True)
    command.add_argument("--receipt", type=Path, required=True)
    command.add_argument("--chain-id", type=int, required=True)
    command.add_argument("--contract", required=True)
    command.add_argument("--snapshot-id", type=int, required=True)
    command.add_argument("--supersedes", type=int, default=0)
    command = commands.add_parser("field")
    command.add_argument("file", type=Path)
    command.add_argument("key")
    command = commands.add_parser("tuple")
    command.add_argument("file", type=Path)
    command = commands.add_parser("save-tx")
    command.add_argument("summary", type=Path)
    command.add_argument("tx_hash")
    command = commands.add_parser("known-tx")
    command.add_argument("summary", type=Path)
    command.add_argument("--ledger", type=Path, default=Path("/curve/ledger"))
    command = commands.add_parser("tx-action")
    command.add_argument("receipt_json")
    command.add_argument("snapshot_id", type=int)
    command = commands.add_parser("fail-tx")
    command.add_argument("summary", type=Path)
    commands.add_parser("number")
    commands.add_parser("snapshot-hash")
    commands.add_parser("snapshot-recorded-at")
    commands.add_parser("snapshot-supersedes")
    command = commands.add_parser("receipt")
    command.add_argument("tx_hash")
    command.add_argument("output", type=Path)
    command = commands.add_parser("log-tx")
    command.add_argument("snapshot_id", type=int)
    command.add_argument("contract")
    command = commands.add_parser("balance")
    command.add_argument("minimum", type=int)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build(args.universe, args.pending)["generated_at_unix"])
    elif args.command == "build-new":
        print(int(build_new(args.universe, args.pending, args.processed)))
    elif args.command == "pending":
        generations = pending_generations(args.pending, args.ledger, args.reconcile)
        for path in args.pending.glob("*.summary.json"):
            summary = json.loads(path.read_bytes())
            if not needs_recording(summary, args.ledger, args.reconcile):
                remove_pending(args.pending, summary["generated_at_unix"])
        for stamp in generations:
            print(stamp)
    elif args.command == "needs-recording":
        summary = json.loads(args.summary.read_bytes())
        needed = needs_recording(summary, args.ledger)
        if not needed:
            remove_pending(args.summary.parent, summary["generated_at_unix"])
        print(int(needed))
    elif args.command == "index-add":
        summary = json.loads(args.summary.read_bytes())
        entry = {key: value for key, value in summary.items() if key != "bytes"}
        entry.update(json.loads(args.receipt.read_bytes()), chain_id=args.chain_id,
                     contract=args.contract, snapshot_id=args.snapshot_id)
        pending = args.summary.parent / f'{summary["generated_at_unix"]}.json'
        index_add(args.ledger, entry, pending.read_bytes(), args.supersedes)
        remove_pending(args.summary.parent, summary["generated_at_unix"])
    elif args.command == "field":
        print(json.loads(args.file.read_bytes())[args.key])
    elif args.command == "tuple":
        print(snapshot_tuple(json.loads(args.file.read_bytes())))
    elif args.command == "save-tx":
        summary = json.loads(args.summary.read_bytes())
        atomic_write(args.summary.parent / f'{summary["generated_at_unix"]}.tx.json',
                     json_bytes({"sha256": summary["sha256"], "tx_hash": cast_hash(args.tx_hash)}))
    elif args.command == "known-tx":
        print(known_transaction(args.summary, args.ledger))
    elif args.command == "tx-action":
        print(tx_action(args.receipt_json, args.snapshot_id))
    elif args.command == "fail-tx":
        fail_transaction(args.summary)
    else:
        raw = sys.stdin.read().strip()
        # Decimal cast output can exceed JSON's floating-point range in other runtimes.
        value = json.loads(raw) if raw.startswith(("{", "[", '"')) else raw
        if args.command == "number":
            print(cast_number(value))
        elif args.command == "snapshot-hash":
            print(snapshot_hash(value))
        elif args.command == "snapshot-recorded-at":
            print(snapshot_recorded_at(value))
        elif args.command == "snapshot-supersedes":
            print(snapshot_supersedes(value))
        elif args.command == "receipt":
            atomic_write(args.output, json_bytes(receipt_record(value, args.tx_hash)))
        elif args.command == "log-tx":
            print(log_transaction(value, args.snapshot_id, args.contract))
        elif args.command == "balance":
            if args.minimum < 0 or cast_number(value) < args.minimum:
                raise ValueError("issuer balance below LEDGER_MIN_BALANCE_WEI; fund the issuer")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from None
