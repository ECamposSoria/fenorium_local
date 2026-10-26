"""Regenerate every data file the terminal needs from the local deployment.

Runs once per `make up`, inside the builder image, after the deployer has finished: it reads the
Foundry broadcast records and the local node, and writes the deployment manifests, the page's
catalog and scenarios, the page config and workspace file, and the universe the platform serves.
It mirrors the staging steps of upstream deploy/testnet-deploy.sh.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/app")
OUT = Path(os.environ.get("OUT_DIR", "/out"))
CURVE = Path(os.environ.get("CURVE_DIR", "/curve"))
WEB = Path(os.environ.get("WEB_DIR", "/web"))
RPC = os.environ.get("BASE_SEPOLIA_RPC_URL", "http://anvil:8545")
PAGE_RPC = os.environ.get("LOCAL_PAGE_RPC_URL", "http://127.0.0.1:8545")
LOCAL_NODE = os.environ.get("LOCAL_NODE_NAME", "Anvil")


def run(*args: str, **env: str) -> None:
    print("==>", " ".join(args), flush=True)
    subprocess.run([sys.executable, *args], check=True, cwd=APP, env={**os.environ, **env})


def main() -> int:
    # The container starts as root only to hand the fresh volumes to the image's unprivileged user,
    # then runs everything else as that user (the same hand-over as chain/deploy.sh).
    if os.getuid() == 0:
        for path in (OUT, CURVE, WEB):
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["chown", "-R", "10001:10001", str(path)], check=True)
        os.execvp("runuser", ["runuser", "-u", "fenorium", "--", sys.executable, "-m", "chain.build"])
    recorded = OUT / "local-deployment.env"
    if not recorded.is_file():
        raise SystemExit("the deployer has not recorded a deployment; run it first")

    # 1. Manifests from the broadcast records and the node's own answers.
    run("scripts/write_deployment_manifests.py", "--rpc", RPC, "--broadcast", "contracts/broadcast",
        "--out", str(OUT), "--chain-id", "84532")
    manifests = {
        "FENORIUM_CATALOG_MANIFEST": str(OUT / "base-sepolia-catalog.json"),
        "FENORIUM_MOCKS_MANIFEST": str(OUT / "base-sepolia-mocks.json"),
    }
    for path in manifests.values():
        if not Path(path).is_file():
            raise SystemExit(f"manifest not written: {path}")

    # 2. The page tree: the exported web/ plus the generated files, served by the platform from a
    #    volume (the image's own copy is shadowed).
    if WEB.exists():
        for child in WEB.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(APP / "web", WEB, dirs_exist_ok=True)
    run("scripts/build_catalog_specs.py", "--page-catalog", **manifests)
    run("scripts/build_testnet_scenarios.py", **manifests)
    for name in ("testnet-catalog.json", "testnet-scenarios.json"):
        shutil.copy2(APP / "web" / "data" / name, WEB / "data" / name)
    run("scripts/apply_deployment_to_page.py", "--mocks", manifests["FENORIUM_MOCKS_MANIFEST"],
        "--catalog", manifests["FENORIUM_CATALOG_MANIFEST"], "--rpc-url", PAGE_RPC, "--no-explorer",
        "--config", str(WEB / "config.json"))

    # The same closures the testnet image applies: no mainnet routes, execution or fees here.
    config_path = WEB / "config.json"
    config = json.loads(config_path.read_text())
    config["execution_enabled"] = False
    config["fees_enabled"] = False
    config.setdefault("route_quotes", {})["enabled"] = False
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    (WEB / "testnet-workspace.json").write_text(
        json.dumps({"enabled": True, "chain_id": 84532, "local": {"node": LOCAL_NODE}}) + "\n")
    index = WEB / "index.html"
    marker = '    <script src="app.js" defer></script>'
    text = index.read_text()
    if '<script src="testnet.js"' not in text:
        assert text.count(marker) == 1, "expected one app.js script tag"
        index.write_text(text.replace(marker, '    <script src="testnet.js" defer></script>\n' + marker, 1))

    # 3. The universe: the five mocks and the catalog, measured against the local node.
    sys.path.insert(0, str(APP))
    for key, value in manifests.items():
        os.environ[key] = value
    from scripts.build_universe import (  # noqa: E402
        measure_demo_supply, measure_eligibility, rate_open_ended, restate_demo_rows,
        testnet_catalog_rows, testnet_rows,
    )
    generated = datetime.now(timezone.utc)
    rows = testnet_rows(generated) + testnet_catalog_rows(generated)
    restate_demo_rows(rows, generated)
    rate_open_ended(rows)
    measure_eligibility(rows)
    measure_demo_supply(rows)
    gates = [(row.get("gate") or {}).get("status") for row in rows]
    print(f"rows={len(rows)}, gated={gates.count('GATED')}, permissionless={gates.count('PERMISSIONLESS')}, "
          f"unmeasured gates={sum(status not in ('GATED', 'PERMISSIONLESS') for status in gates)}, "
          f"fund sizes measured={sum(row.get('tvl_usd') is not None for row in rows)}", flush=True)
    catalog = json.loads(Path(manifests["FENORIUM_CATALOG_MANIFEST"]).read_text())
    expected = 5 + len(catalog["instruments"])
    if len(rows) != expected or any(row.get("chain_id") != 84532 for row in rows):
        raise SystemExit(f"expected {expected} Base Sepolia rows, built {len(rows)}")
    CURVE.mkdir(parents=True, exist_ok=True)
    document = {"generated_at": generated.isoformat().replace("+00:00", "Z"),
                "instruments": rows, "instrument_count": len(rows)}
    (CURVE / "universe.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    print(f"prepared {len(rows)} rows at {document['generated_at']}", flush=True)

    # 4. Commit the universe to the local passport before the platform starts.
    from deploy.score_ledger import cast_number, log_transaction, snapshot_hash

    def ledger_command(*args: str, input: str | None = None) -> str:
        return subprocess.run(
            [sys.executable, "-m", "deploy.score_ledger", *args], input=input,
            check=True, cwd=APP, capture_output=True, text=True,
        ).stdout.strip()

    def cast(*args: str) -> str:
        return subprocess.run(
            ["cast", *args, "--rpc-url", RPC], check=True, capture_output=True, text=True,
        ).stdout.strip()

    deployment = dict(line.split("=", 1) for line in recorded.read_text().splitlines()
                      if "=" in line)
    contract = deployment.get("DECISION_PASSPORT")
    if not contract:
        raise SystemExit("local-deployment.env has no DECISION_PASSPORT")
    private_key = os.environ["PRIVATE_KEY"]
    ledger = CURVE / "ledger"
    pending = ledger / "pending"
    # The curve volume outlives the chain: after a fresh chain, a kept index would show records whose
    # transactions are gone. Keep it only if every indexed record still holds the same hash on chain.
    index = ledger / "index.json"
    if index.is_file():
        entries = json.loads(index.read_text())
        total = cast_number(json.loads(cast("call", contract, "total()(uint256)", "--json")))
        stale = any(entry.get("contract", "").lower() != contract.lower() or entry["snapshot_id"] > total
                    or snapshot_hash(cast("call", contract, "snapshotOf(uint256)", str(entry["snapshot_id"])))
                    != entry["sha256"] for entry in entries)
        if stale:
            shutil.move(str(ledger), str(CURVE / f"ledger.stale-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"))
            print("previous score ledger belonged to an earlier chain; moved aside", flush=True)
    stamp = ledger_command("build", "--universe", str(CURVE / "universe.json"),
                           "--pending", str(pending))
    summary_path = pending / f"{stamp}.summary.json"
    summary = json.loads(summary_path.read_text())
    snapshot_id = cast_number(json.loads(cast("call", contract, "idByGeneratedAt(uint64)(uint256)",
                                              stamp, "--json")))
    if snapshot_id == 0:
        receipt = json.loads(cast(
            "send", contract, "recordSnapshot((uint64,bytes32,string,string,uint32,uint32,uint32))",
            ledger_command("tuple", str(summary_path)), "--private-key", private_key, "--json",
        ))
        tx_hash = receipt["transactionHash"]
        snapshot_id = cast_number(json.loads(cast("call", contract, "idByGeneratedAt(uint64)(uint256)",
                                                  stamp, "--json")))
        if snapshot_id == 0:
            raise SystemExit("snapshot transaction did not record a snapshot id")
    else:
        logs = json.loads(cast("logs", "--address", contract, "--from-block", "0",
                               "--to-block", "latest", "--json"))
        tx_hash = log_transaction(logs, snapshot_id, contract)
        receipt = json.loads(cast("receipt", tx_hash, "--json"))
    if snapshot_hash(cast("call", contract, "snapshotOf(uint256)", str(snapshot_id))) != summary["sha256"]:
        raise SystemExit("on-chain snapshot hash does not match the local universe")
    receipt_path = pending / f"{stamp}.receipt.json"
    ledger_command("receipt", tx_hash, str(receipt_path), input=json.dumps(receipt))
    # index-add publishes the snapshot and removes its pending files.
    ledger_command("index-add", "--ledger", str(ledger), "--summary", str(summary_path),
                   "--receipt", str(receipt_path), "--chain-id", "84532", "--contract", contract,
                   "--snapshot-id", str(snapshot_id))
    print(f"recorded score snapshot {stamp} as #{snapshot_id} (tx {tx_hash})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
