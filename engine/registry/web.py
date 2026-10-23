"""Write the chain registry into the two web files that carry it.

The terminal is static: it cannot import `chains.json`, and its tests load each script into a bare
context. So the registry is generated into a marked region of `web/portfolio.js`, which every
other script already reads as `FenoriumPortfolio`, and into `web/config.json`'s `route_chains`.
`engine/tests/test_registry.py` fails when either has drifted from the registry.

    python -m engine.registry.web          # rewrite both files
    python -m engine.registry.web --check  # exit 1 if either is stale
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .chains import ChainRegistry, registry


ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO_JS = ROOT / "web" / "portfolio.js"
CONFIG_JSON = ROOT / "web" / "config.json"
BEGIN = "  // BEGIN GENERATED CHAIN REGISTRY -- `python -m engine.registry.web` writes this; do not edit.\n"
END = "  // END GENERATED CHAIN REGISTRY\n"


def _js_chain(chain) -> dict[str, Any]:
    public = chain.to_public()
    return {
        "chain_id": public["chain_id"],
        "key": public["key"],
        "name": public["name"],
        "short": public["short"],
        "testnet": public["testnet"],
        "block_seconds": public["block_seconds"],
        "history_index": public["history_index"],
        "native": public["native"],
        "rpc_url": public["rpc_url"],
        "explorer": public["explorer"],
        "usdc": public["usdc"],
        "offer_native": public["offer_native"],
        "roles": public["roles"],
    }


def portfolio_region(chains: ChainRegistry) -> str:
    rows = ",\n".join(f"    {json.dumps(_js_chain(chain), separators=(',', ':'))}" for chain in chains.chains)
    return (
        BEGIN
        + "  const REGISTRY_CHAINS = Object.freeze([\n" + rows + ",\n  ].map((chain) => Object.freeze(chain)));\n"
        + f"  const FEE_RECIPIENT = {json.dumps(chains.fee_recipient)};\n"
        + END
    )


def route_chains(chains: ChainRegistry) -> dict[str, Any]:
    """`config.json`'s `route_chains`: every chain a buyer may pay from, in the shape app.js checks."""
    result: dict[str, Any] = {}
    # The chains the terminal reads whole portfolios on first, then those it only pays from. A chain
    # the server can buy on but whose cash the terminal does not read stays pay-only there.
    ordered = sorted(chains.with_role("pay"), key=lambda chain: (not chain.has("portfolio"), chain.chain_id))
    for chain in ordered:
        entry: dict[str, Any] = {
            "name": chain.name,
            "rpc_url": chain.rpc_public[0],
            "native": {"symbol": chain.native_symbol, "decimals": chain.native_decimals},
        }
        pay_only = not chain.has("portfolio")
        if pay_only:
            # A portfolio chain is read for every registered asset already; a pay-only chain names
            # the one token it is paid from, and where to show its receipts.
            entry["explorer"] = chain.explorer
            entry["tokens"] = [{
                "symbol": chain.usdc.symbol, "address": chain.usdc.address, "decimals": chain.usdc.decimals,
            }]
            entry["pay_only"] = True
        if not chain.offer_native:
            entry["offer_native"] = False
        result[str(chain.chain_id)] = entry
    return result


def rendered_portfolio(chains: ChainRegistry, text: str) -> str:
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        raise SystemExit(f"{PORTFOLIO_JS} has no generated chain registry region")
    return text[:start] + portfolio_region(chains) + text[end + len(END):]


def rendered_config(chains: ChainRegistry, text: str) -> str:
    """`config.json` with its `route_chains` block regenerated, one chain per line as written by hand."""
    opening = '  "route_chains": {\n'
    start = text.find(opening)
    end = text.find("\n  },\n", start)
    if start < 0 or end < 0:
        raise SystemExit(f"{CONFIG_JSON} has no route_chains block")
    lines = [f"    {json.dumps(chain_id)}: {json.dumps(entry, ensure_ascii=False)}"
             for chain_id, entry in route_chains(chains).items()]
    rendered = text[:start] + opening + ",\n".join(lines) + text[end:]
    if json.loads(rendered)["route_chains"] != route_chains(chains):
        raise SystemExit(f"{CONFIG_JSON} route_chains block could not be replaced cleanly")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail instead of writing when stale")
    args = parser.parse_args(argv)
    chains = registry()
    stale = []
    for path, render in ((PORTFOLIO_JS, rendered_portfolio), (CONFIG_JSON, rendered_config)):
        current = path.read_text(encoding="utf-8")
        wanted = render(chains, current)
        if wanted != current:
            stale.append(path)
            if not args.check:
                path.write_text(wanted, encoding="utf-8")
    if args.check and stale:
        print("stale: " + ", ".join(str(path.relative_to(ROOT)) for path in stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
