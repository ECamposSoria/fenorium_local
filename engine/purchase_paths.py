"""Classify the purchase path for rating costs, without changing execution policy."""

import re
from collections.abc import Mapping
from typing import Any

from engine.midnight import MIDNIGHT_VENUES


def routed_purchase(row: Mapping[str, Any]) -> bool:
    """Whether this instrument is bought through Fenorium's LI.FI route.

    PlatformAPI.execution_route purchases transferable ERC-20 instruments. Exactly's
    position_deposit calls its market directly; Midnight/Tenor lending_order signs an
    order. Declared demos use the testnet sale desks, and stablecoins are settlement cash.
    These paths do not charge the LI.FI routing fee.
    """
    if (row.get("rate_kind") == "declared_demo"
            or row.get("is_stablecoin") is True or row.get("category") == "stablecoin"
            or row.get("venue") in MIDNIGHT_VENUES or row.get("venue") == "Exactly"
            or row.get("tokenised") is False):
        return False
    address = row.get("address")
    return isinstance(address, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is not None
