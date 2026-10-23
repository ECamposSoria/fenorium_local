#!/usr/bin/env python3
"""Build deployment manifests from Foundry broadcast receipts and Base Sepolia reads."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_BROADCAST = ROOT / "contracts/broadcast"
DEFAULT_OUT = ROOT / "contracts/deployments"
CHAIN_ID = 84532
SIGNATURES = {
    "quote_signature": "quote(uint256)",
    "purchase_signature": "purchase(uint256,uint256)",
    "sell_quote_signature": "sellQuote(uint256)",
    "sell_signature": "sell(uint256,uint256)",
}
EAS = "0x4200000000000000000000000000000000000021"
SCHEMA_REGISTRY = "0x4200000000000000000000000000000000000020"
SCHEMA_VERSION = "fenorium/testnet-mocks-deployment/1"


def rpc_request(rpc_url: str, method: str, params: list[Any]) -> Any:
    """Make one JSON-RPC request using only the standard library."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("error"):
        raise ValueError(f"RPC {method} failed: {result['error']}")
    return result.get("result")


def _broadcast(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Foundry broadcast {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("transactions"), list):
        raise ValueError(f"invalid Foundry broadcast: {path}")
    receipts = data.get("receipts")
    if not isinstance(receipts, list):
        raise ValueError(f"broadcast has no receipts: {path}")
    by_hash = {str(r.get("transactionHash", "")).lower(): r for r in receipts if isinstance(r, dict)}
    return {"data": data, "receipts": by_hash}


def _nonce(tx: dict[str, Any]) -> int:
    nonce = (tx.get("transaction") or {}).get("nonce")
    if nonce is None:
        raise ValueError(f"broadcast transaction {tx.get('hash')} has no nonce")
    return int(str(nonce), 0)


def _block_for(tx: dict[str, Any], receipts: dict[str, Any]) -> int:
    receipt = receipts.get(str(tx.get("hash", "")).lower())
    if not isinstance(receipt, dict) or not receipt.get("blockNumber"):
        raise ValueError(f"missing receipt block for transaction {tx.get('hash')}")
    if receipt.get("status") not in (None, "0x1", 1):
        raise ValueError(f"transaction {tx.get('hash')} did not succeed")
    try:
        return int(str(receipt["blockNumber"]), 0)
    except ValueError as exc:
        raise ValueError(f"invalid receipt block for transaction {tx.get('hash')}") from exc


def _selector(signature: str) -> str:
    from engine.keccak import keccak256
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def _word(value: int) -> str:
    return f"{value:064x}"


def _decode_address(data: str) -> str:
    raw = data.removeprefix("0x")
    if len(raw) < 64:
        raise ValueError("RPC returned short ABI address data")
    return "0x" + raw[-40:].lower()


def _eth_call(rpc: Callable[[str, list[Any]], Any], address: str, data: str) -> str:
    result = rpc("eth_call", [{"to": address, "data": data}, "latest"])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("eth_call returned invalid ABI data")
    return result


def _catalog_tuples(argument: Any) -> list[tuple[str, str, bool, int]]:
    if not isinstance(argument, str):
        raise ValueError("catalog list transaction has invalid arguments")
    matches = re.findall(r"\(([^(),]+), ([^(),]+), (true|false), (\d+)\)", argument)
    if not matches:
        raise ValueError("could not parse catalog batch from Foundry arguments")
    normalized = [(name.strip(), symbol.strip(), gated == "true", int(price))
                  for name, symbol, gated, price in matches]
    if argument.count("(") != len(normalized) or argument.count(")") != len(normalized):
        raise ValueError("catalog batch arguments contain an unparsed tuple")
    return normalized


def build_catalog_manifest(
    rpc_url: str,
    broadcast_dir: Path = DEFAULT_BROADCAST,
    chain_id: int = CHAIN_ID,
    rpc: Callable[[str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    """Reconcile catalog broadcasts with factory state and declared catalog specs."""
    rpc = rpc or (lambda method, params: rpc_request(rpc_url, method, params))
    if chain_id != CHAIN_ID:
        raise ValueError("catalog deployment manifests are supported only for Base Sepolia (84532)")
    rpc_chain = rpc("eth_chainId", [])
    if not isinstance(rpc_chain, str) or int(rpc_chain, 16) != chain_id:
        raise ValueError(f"RPC chain id does not match requested chain id {chain_id}")
    directory = broadcast_dir / "DeployDemoCatalog.s.sol" / str(chain_id)
    latest = _broadcast(directory / "run-latest.json")

    def target_address(tx: dict[str, Any]) -> str:
        return str(tx.get("contractAddress") or (tx.get("transaction") or {}).get("to", "")).lower()

    targets = {target_address(tx) for tx in latest["data"]["transactions"]
               if (tx.get("contractName") == "DemoCatalogFactory" and tx.get("transactionType") == "CREATE")
               or tx.get("function") == "list((string,string,bool,uint256)[])"}
    if len(targets) != 1:
        raise ValueError("expected exactly one catalog factory in latest broadcast")
    target = targets.pop()
    runs = [_broadcast(path) for path in sorted(directory.glob("run-*.json"))]
    runs.sort(key=lambda run: int(run["data"].get("timestamp", 0)))
    # Foundry keeps every run. After a chain reset the same deployer and nonce rebuild the factory
    # at the same address, and a replayed transaction keeps its hash while a changed batch does
    # not, so each transaction is reconciled only when the connected chain has its receipt.
    known: dict[str, Any] = {}

    def on_chain(tx_hash: str) -> bool:
        if tx_hash not in known:
            known[tx_hash] = rpc("eth_getTransactionReceipt", [tx_hash])
        return isinstance(known[tx_hash], dict)

    txs, receipts, seen = [], {}, set()
    for run in runs:
        for tx in run["data"]["transactions"]:
            tx_hash = str(tx.get("hash", "")).lower()
            if target_address(tx) == target and tx_hash in run["receipts"]:
                receipts[tx_hash] = run["receipts"][tx_hash]
    for run in runs:
        for tx in run["data"]["transactions"]:
            if target_address(tx) != target:
                continue
            tx_hash = str(tx.get("hash", "")).lower()
            # A batch that reverted and its retry carry different hashes; only confirmed
            # transactions describe the deployment.
            receipt = receipts.get(tx_hash)
            if not isinstance(receipt, dict) or receipt.get("status") not in (None, "0x1", 1):
                continue
            if tx_hash not in seen and on_chain(tx_hash):
                # The chain's receipt over the broadcast's: a replayed transaction keeps its hash
                # but lands in another block.
                receipts[tx_hash] = {**receipt, **known[tx_hash]}
                txs.append(tx)
                seen.add(tx_hash)
    if not txs:
        raise ValueError("no catalog broadcast is known to the connected chain")
    # Run order is not send order once a replay keeps some batches and redoes others; one
    # deployer's nonces are, and they are the order the chain executed the batches in. (The
    # receipts' block and index are not: Anvil with a block time packs several batches into one
    # block and reports them in another order.)
    txs.sort(key=_nonce)
    creations = [tx for tx in txs if tx.get("transactionType") == "CREATE" and tx.get("contractName") == "DemoCatalogFactory"]
    if len(creations) != 1:
        raise ValueError("expected exactly one DemoCatalogFactory creation in broadcast")
    creation = creations[0]
    args = creation.get("arguments")
    if not isinstance(args, list) or len(args) < 2:
        raise ValueError("factory creation arguments are incomplete")
    factory = creation.get("contractAddress")
    if not isinstance(factory, str) or not factory.startswith("0x"):
        raise ValueError("factory creation has no address")
    list_txs = [tx for tx in txs if tx.get("function") == "list((string,string,bool,uint256)[])"]
    from scripts.build_catalog_specs import specs
    declared = specs()
    cursor = 0
    tx_by_index: list[tuple[dict[str, Any], int]] = []
    for tx in list_txs:
        if target_address(tx) != factory.lower():
            raise ValueError("catalog list transaction targets a different factory")
        batch = _catalog_tuples((tx.get("arguments") or [None])[0])
        expected = declared[cursor:cursor + len(batch)]
        expected_tuples = [(row["name"], row["symbol"], row["gated"], row["price_micros"]) for row in expected]
        if batch != expected_tuples:
            raise ValueError(f"catalog broadcast batch does not match declared specs at index {cursor}")
        tx_by_index.extend((tx, _block_for(tx, receipts)) for _ in batch)
        cursor += len(batch)
    if cursor != len(declared):
        raise ValueError(f"catalog broadcast lists {cursor} instruments; expected {len(declared)}")
    count_data = _eth_call(rpc, factory, _selector("count()"))
    count = int(count_data, 16)
    if count != len(declared):
        raise ValueError(f"on-chain factory count {count} does not match declared catalog count {len(declared)}")

    instruments = []
    for index, row in enumerate(declared):
        raw = _eth_call(rpc, factory, _selector("instruments(uint256)") + _word(index))
        instrument = _decode_address(raw)
        desk_raw = _eth_call(rpc, factory, _selector("deskFor(address)") + _word(int(instrument, 16)))
        desk = _decode_address(desk_raw)
        tx, block = tx_by_index[index]
        instruments.append({
            "index": index, "symbol": row["symbol"], "name": row["name"],
            "category": row["category"], "instrument": instrument, "desk": desk,
            "gated": row["gated"], "price_micros": row["price_micros"],
            "deployed_block": block, "tx": tx["hash"],
        })
    return {
        "schema_version": "fenorium/testnet-catalog-deployment/1",
        "chain_id": chain_id, "factory": factory.lower(), "factory_block": _block_for(creation, receipts),
        "payment_token": args[0], "gate_registry": args[1], **SIGNATURES,
        "instruments": instruments,
    }


def build_mocks_manifest(broadcast_dir: Path = DEFAULT_BROADCAST, chain_id: int = CHAIN_ID) -> dict[str, Any]:
    """Build mock-token deployment metadata from the corresponding Foundry broadcasts."""
    if chain_id != CHAIN_ID:
        raise ValueError("mock deployment manifests are supported only for Base Sepolia (84532)")

    def load(script: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        captured = _broadcast(broadcast_dir / f"{script}.s.sol" / str(chain_id) / "run-latest.json")
        return captured["data"]["transactions"], captured["receipts"]

    instruments_txs, instruments_receipts = load("DeployDemoInstruments")
    exposure_txs, exposure_receipts = load("DeployDemoExposureMocks")
    sales_txs, sales_receipts = load("DeployDemoSales")
    desks_txs, desks_receipts = load("DeployDemoDesks")

    def created(txs: list[dict[str, Any]], receipts: dict[str, Any], name: str) -> dict[str, Any]:
        found = [tx for tx in txs if tx.get("transactionType") == "CREATE" and tx.get("contractName") == name]
        if len(found) != 1:
            raise ValueError(f"expected one {name} creation in broadcast")
        tx = found[0]
        return {"address": tx["contractAddress"], "deployed_block": _block_for(tx, receipts), "arguments": tx.get("arguments", [])}

    gated = created(instruments_txs, instruments_receipts, "GatedTreasuryMock")
    open_token = created(instruments_txs, instruments_receipts, "PermissionlessTreasuryMock")
    exposure_creates = [tx for tx in exposure_txs if tx.get("transactionType") == "CREATE" and tx.get("contractName") == "DemoExposureMock"]
    if len(exposure_creates) != 2:
        raise ValueError("expected two DemoExposureMock creations")
    by_symbol = {str(tx.get("arguments", [None, None])[1]): tx for tx in exposure_creates}
    if set(by_symbol) != {"mFT", "mVY"}:
        raise ValueError("exposure mock constructor symbols do not match mFT and mVY")
    fixed_tx = by_symbol["mFT"]
    variable_tx = by_symbol["mVY"]
    # Sales and desks are joined by the instrument argument, preserving constructor order.
    payment = next((tx for tx in sales_txs if tx.get("transactionType") == "CREATE" and tx.get("contractName") == "MockUSDC"), None)
    if payment is None:
        raise ValueError("missing MockUSDC creation")
    payment_entry = {"symbol": "mUSDC", "address": payment["contractAddress"].lower(), "deployed_block": _block_for(payment, sales_receipts), "faucet_method": "mint(address,uint256)"}
    sale_creates = [tx for tx in sales_txs if tx.get("transactionType") == "CREATE" and tx.get("contractName") == "DemoInstrumentSale"]
    sale_by_instrument = {str(tx.get("arguments", [None, None])[1]).lower(): tx for tx in sale_creates}
    desk_creates = [tx for tx in desks_txs if tx.get("transactionType") == "CREATE" and tx.get("contractName") == "DemoInstrumentDesk"]
    desk_by_instrument = {str(tx.get("arguments", [None, None])[1]).lower(): tx for tx in desk_creates}

    def entry(token: dict[str, Any], gated_flag: bool) -> dict[str, Any]:
        address = str(token["address"])
        sale, desk = sale_by_instrument.get(address.lower()), desk_by_instrument.get(address.lower())
        if sale is None or desk is None:
            raise ValueError(f"missing sale or desk deployment for instrument {address}")
        for tx in (sale, desk):
            if tx["arguments"][0].lower() != payment_entry["address"]:
                raise ValueError(f"sale or desk payment token differs for instrument {address}")
        _block_for(sale, sales_receipts)
        _block_for(desk, desks_receipts)
        return {"address": address.lower(), "deployed_block": token["deployed_block"],
                "desk": desk["contractAddress"].lower(), "sale": sale["contractAddress"].lower(), "gated": gated_flag}

    return {
        "schema_version": SCHEMA_VERSION, "chain_id": chain_id,
        "eas": EAS, "schema_registry": SCHEMA_REGISTRY,
        "schema_uid": gated["arguments"][1].lower(), "trusted_attester": gated["arguments"][2].lower(),
        "payment_token": payment_entry,
        "instruments": {
            "mGT": entry(gated, True),
            "mPT": entry(open_token, False),
            "mFT": {"address": fixed_tx["contractAddress"].lower(), "deployed_block": _block_for(fixed_tx, exposure_receipts)},
            "mVY": {"address": variable_tx["contractAddress"].lower(), "deployed_block": _block_for(variable_tx, exposure_receipts)},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", required=True, help="Base Sepolia JSON-RPC URL")
    parser.add_argument("--broadcast", type=Path, default=DEFAULT_BROADCAST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--chain-id", type=int, default=CHAIN_ID)
    args = parser.parse_args(argv)
    try:
        catalog = build_catalog_manifest(args.rpc, args.broadcast, args.chain_id)
        mocks = build_mocks_manifest(args.broadcast, args.chain_id)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "base-sepolia-catalog.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
        (args.out / "base-sepolia-mocks.json").write_text(json.dumps(mocks, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"write_deployment_manifests: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
