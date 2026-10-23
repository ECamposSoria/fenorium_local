#!/usr/bin/env python3
"""Update only the deployment JSON value in web/config.json from deployment manifests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "web" / "config.json"
sys.path.insert(0, str(ROOT))
from scripts.build_universe import checksum_address
from scripts.build_universe import DEMO_UNIT_PRICE_FIXED, TESTNET

SIGNATURES = {
    "quote_signature": "quote(uint256)",
    "purchase_signature": "purchase(uint256,uint256)",
    "sell_quote_signature": "sellQuote(uint256)",
    "sell_signature": "sell(uint256,uint256)",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _top_level_value_span(source: str, wanted: str) -> tuple[int, int]:
    """Return the byte-character span of one top-level JSON value using raw_decode."""
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(source) and source[pos].isspace():
        pos += 1
    if pos >= len(source) or source[pos] != "{":
        raise ValueError("config root must be a JSON object")
    pos += 1
    while True:
        while pos < len(source) and (source[pos].isspace() or source[pos] == ","):
            pos += 1
        if pos >= len(source) or source[pos] == "}":
            break
        try:
            key, key_end = decoder.raw_decode(source, pos)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid top-level config key: {exc}") from exc
        if not isinstance(key, str):
            raise ValueError("config object contains a non-string key")
        pos = key_end
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if pos >= len(source) or source[pos] != ":":
            raise ValueError("invalid top-level config member")
        pos += 1
        while pos < len(source) and source[pos].isspace():
            pos += 1
        value_start = pos
        try:
            _, value_end = decoder.raw_decode(source, value_start)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid value for config key {key!r}: {exc}") from exc
        if key == wanted:
            return value_start, value_end
        pos = value_end
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if pos < len(source) and source[pos] not in ",}":
            raise ValueError("invalid top-level config separator")
    raise ValueError(f"config has no top-level {wanted!r} member")


def _instrument_entry(mocks: dict[str, Any], symbol: str) -> dict[str, Any]:
    entries = mocks.get("instruments")
    if not isinstance(entries, dict) or not isinstance(entries.get(symbol), dict):
        raise ValueError(f"mocks manifest is missing instruments.{symbol}")
    return entries[symbol]


def _address(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"manifest entry is missing an address ({', '.join(keys)})")


def build_deployment(
    mocks: dict[str, Any], catalog: dict[str, Any], rpc_url: str,
    explorer_url: str | None = "https://sepolia.basescan.org",
) -> dict[str, Any]:
    chain_id = mocks.get("chain_id")
    if not isinstance(chain_id, int) or isinstance(chain_id, bool):
        raise ValueError("mocks manifest chain_id must be an integer")
    if catalog.get("chain_id") != chain_id:
        raise ValueError("mocks and catalog manifests have different chain_id values")

    payment = mocks.get("payment_token")
    if not isinstance(payment, dict):
        raise ValueError("mocks manifest is missing payment_token")
    payment_address = _address(payment, "address")
    catalog_payment = catalog.get("payment_token")
    if not isinstance(catalog_payment, str) or catalog_payment.lower() != payment_address.lower():
        raise ValueError("mocks and catalog payment token addresses do not match")

    instruments = {symbol: _instrument_entry(mocks, symbol) for symbol in ("mGT", "mPT", "mFT", "mVY")}
    if catalog.get("gate_registry") and str(catalog["gate_registry"]).lower() != _address(
        instruments["mGT"], "address"
    ).lower():
        raise ValueError("catalog gate_registry does not match the mGT instrument")

    testnet_rates = {row["symbol"]: row["rate_bps"] for row in TESTNET}
    declared = {
        _address(entry, "address").lower(): {
            "symbol": symbol,
            "rate_bps": testnet_rates[symbol],
        }
        for symbol, entry in instruments.items()
    }
    declared[payment_address.lower()] = {
        "symbol": "mUSDC",
        "unit_price_usd_fixed": str(DEMO_UNIT_PRICE_FIXED),
    }

    deployment: dict[str, Any] = {
        "chain_id": chain_id,
        "chain_name": "Base Sepolia" if chain_id == 84532 else str(mocks.get("chain_name", "")),
        "chain_id_hex": hex(chain_id),
        "rpc_url": rpc_url,
        "explorer_url": explorer_url,
        "payment_token": {
            "symbol": payment.get("symbol", "mUSDC"),
            "name": "Mock USDC",
            "address": checksum_address(payment_address),
            "decimals": 6,
            "faucet_address": checksum_address(payment_address),
            "faucet_method": payment.get("faucet_method", "mint(address,uint256)"),
        },
        "sales": {},
        "declared": declared,
    }

    for symbol in ("mGT", "mPT"):
        entry = instruments[symbol]
        desk = entry.get("desk") if isinstance(entry.get("desk"), dict) else entry
        sales_entry = {
            "instrument_address": checksum_address(_address(entry, "address")),
            "sale_address": checksum_address(_address(desk, "desk_address", "desk", "address")),
            "gated": bool(entry.get("gated")),
            **SIGNATURES,
        }
        deployment["sales"][symbol] = sales_entry
    return deployment


def apply_deployment(config_path: Path, deployment: dict[str, Any]) -> None:
    source_bytes = config_path.read_bytes()
    source = source_bytes.decode("utf-8")
    try:
        config = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("config root must be a JSON object")
    start, end = _top_level_value_span(source, "deployment")
    # The value takes the indentation of the line its key sits on, so the committed file and a
    # regenerated one differ only where the deployment differs.
    line_start = source.rfind("\n", 0, start) + 1
    pad = source[line_start:start][:len(source[line_start:start]) - len(source[line_start:start].lstrip())]
    replacement = ("\n" + pad).join(json.dumps(deployment, ensure_ascii=False, indent=2).splitlines())
    config_path.write_bytes((source[:start] + replacement + source[end:]).encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mocks", required=True, type=Path, help="deployed mock manifest JSON")
    parser.add_argument("--catalog", required=True, type=Path, help="catalog deployment manifest JSON")
    parser.add_argument("--rpc-url", required=True)
    explorer = parser.add_mutually_exclusive_group()
    explorer.add_argument("--explorer-url")
    explorer.add_argument("--no-explorer", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        deployment = build_deployment(
            _read_json(args.mocks), _read_json(args.catalog), args.rpc_url,
            None if args.no_explorer else (args.explorer_url or "https://sepolia.basescan.org"),
        )
        apply_deployment(args.config, deployment)
    except (OSError, ValueError) as exc:
        print(f"apply_deployment_to_page: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
