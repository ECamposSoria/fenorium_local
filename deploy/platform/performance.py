"""What a wallet's holdings earned, from its own transfers and the stored daily prices.

Per asset: the transfers in and out, each valued at the daily price nearest its moment, are the
money that crossed the wallet's boundary; the balance it holds now, at the current price, is what
that money became. Every transfer must be verified on chain, and their net must equal the balance
read at the same block. Reconciliation alone does not establish coverage: without complete
history, the profit or loss covers only the supplied transfers and the totals remain partial.
An asset that cannot be computed is named with its reason and left out of the totals.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from engine.portfolio.valuation import raw_to_usd_micros


SCHEMA = "fenorium/wallet-performance/1"
METHOD = ("per asset: current value minus the net USD that crossed the wallet's boundary, each "
          "transfer at the stored daily price nearest its moment; only for verified histories "
          "that reconcile with the balance read at the same block; when history coverage is not "
          "established, P&L covers the supplied transfers only")


def wallet_performance(events: list[Any], values: list[Mapping[str, Any]],
                       balances: Mapping[tuple[int, str], tuple[int, int]],
                       current_price: Callable[[int, str], int | None], *,
                       chain_complete: Mapping[int, bool],
                       known_decimals: Callable[[int, str], int | None] = lambda chain_id, token: None) -> dict[str, Any]:
    """`balances[(chain_id, token)] = (raw_amount, block_number)` as read by the caller.

    `values` is `transfer_values["values"]`, in the order of `events`.
    `known_decimals` defaults to returning None, refusing monetary results without trusted precision.
    """
    by_asset: dict[tuple[int, str], list[tuple[Any, Mapping[str, Any]]]] = {}
    for event, value in zip(events, values):
        by_asset.setdefault((event.chain_id, event.token), []).append((event, value))
    assets = []
    for key in sorted(set(by_asset) | set(balances)):
        chain_id, token = key
        rows = by_asset.get(key, [])
        claimed_decimals = {event.decimals for event, _ in rows if event.decimals is not None}
        decimals = next(iter(claimed_decimals), None)
        registered_decimals = known_decimals(chain_id, token)
        record: dict[str, Any] = {"chain_id": chain_id, "token": token, "transfers": len(rows),
                                  "history_complete": chain_complete.get(chain_id, False),
                                  "status": "COMPUTED", "reason": None, "inflow_usd_micros": None,
                                  "outflow_usd_micros": None, "current_value_usd_micros": None,
                                  "pnl_usd_micros": None}
        held = balances.get(key)

        def refuse(reason: str) -> None:
            record.update(status="NOT_COMPUTABLE", reason=reason)

        if held is None:
            refuse("NO_BALANCE_EVIDENCE")
        elif not rows:
            refuse("NO_TRANSFER_HISTORY")
        elif len(claimed_decimals) > 1:
            refuse("DECIMALS_INCONSISTENT")
        elif registered_decimals is None:
            refuse("DECIMALS_UNKNOWN")
        elif decimals is not None and registered_decimals is not None and decimals != registered_decimals:
            refuse("DECIMALS_MISMATCH")
        elif any(not event.verified for event, _ in rows):
            refuse("UNVERIFIED_TRANSFERS")
        elif any(event.block_number > held[1] for event, _ in rows):
            refuse("HISTORY_PAST_BALANCE_BLOCK")
        else:
            net = sum(event.raw_amount if event.direction.value == "IN" else
                      -event.raw_amount if event.direction.value == "OUT" else 0 for event, _ in rows)
            # Only money crossing the boundary needs a price: a self-transfer or a zero amount moves none.
            flows = [(event, value) for event, value in rows
                     if event.direction.value in ("IN", "OUT") and event.raw_amount > 0]
            if net != held[0]:
                # Not whole: a transfer before the scan, or one the source missed.
                refuse("HISTORY_DOES_NOT_RECONCILE")
            elif any(value.get("value_usd_micros") is None for _, value in flows):
                refuse("MISSING_HISTORICAL_PRICE")
            else:
                price = 0 if held[0] == 0 else current_price(chain_id, token)
                if price is None:
                    refuse("MISSING_CURRENT_PRICE")
                else:
                    inflow = sum(value["value_usd_micros"] for event, value in flows if event.direction.value == "IN")
                    outflow = sum(value["value_usd_micros"] for event, value in flows if event.direction.value == "OUT")
                    # Trusted precision: a claim was only checked against it, and may be absent.
                    current = raw_to_usd_micros(held[0], decimals=registered_decimals, price_usd_fixed=price)
                    record.update(inflow_usd_micros=inflow, outflow_usd_micros=outflow,
                                  current_value_usd_micros=current, pnl_usd_micros=current - inflow + outflow)
        assets.append(record)
    computed = [row for row in assets if row["status"] == "COMPUTED"]
    history_complete = bool(assets) and all(row["history_complete"] for row in assets)
    return {
        "schema_version": SCHEMA,
        "method": METHOD,
        "assets": assets,
        "totals": {
            "assets": len(assets),
            "computed": len(computed),
            "pnl_usd_micros": sum(row["pnl_usd_micros"] for row in computed) if computed else None,
            "current_value_usd_micros": sum(row["current_value_usd_micros"] for row in computed) if computed else None,
            "net_flows_usd_micros": (sum(row["inflow_usd_micros"] - row["outflow_usd_micros"] for row in computed)
                                     if computed else None),
            "history_complete": history_complete,
            "complete": history_complete and len(computed) == len(assets),
        },
    }
