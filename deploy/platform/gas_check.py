"""Native balance checks for wallet transactions returned by the platform."""

from __future__ import annotations

import re
from typing import Any

from engine.chain import selector
from engine.onchain import OnchainError
from engine.registry import registry


_ALLOWANCE = selector("allowance(address,address)")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def _quantity(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        result = int(value, 16) if value.startswith("0x") else int(value, 10)
    else:
        raise ValueError(f"invalid {name}")
    if result < 0 or (positive and result == 0):
        raise ValueError(f"invalid {name}")
    return result


def _hex(value: int) -> str:
    return hex(value)


def _add_reason(response: dict[str, Any], reason: str) -> None:
    reasons = response.get("reason_codes")
    if not isinstance(reasons, list):
        reasons = []
    if reason not in reasons:
        reasons.append(reason)
    response["reason_codes"] = reasons


def _word_address(value: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ValueError("invalid approval address")
    return value[2:].lower().rjust(64, "0")


def _approval_allowance(reader: Any, approval: dict[str, Any], *, wallet: str, block: Any) -> int:
    token, spender = approval.get("to"), approval.get("spender")
    if not isinstance(token, str) or not _ADDRESS.fullmatch(token):
        raise ValueError("invalid approval token")
    data = _ALLOWANCE + _word_address(wallet) + _word_address(spender)
    raw = reader.rpc("eth_call", [{"to": token, "data": data}, {"blockHash": block.hash}],
                     require_result=True)
    if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", raw):
        raise ValueError("invalid allowance result")
    return int(raw[2:], 16)


def _wallet_has_code(reader: Any, wallet: str, block: Any) -> bool:
    """Return true for a normal contract, false for an EOA or EIP-7702 delegation account."""
    if not isinstance(wallet, str) or not _ADDRESS.fullmatch(wallet):
        raise ValueError("invalid wallet address")
    raw = reader.rpc("eth_getCode", [wallet, {"blockHash": block.hash}], require_result=True)
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise ValueError("invalid wallet code")
    code = raw[2:]
    if len(code) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", code):
        raise ValueError("invalid wallet code")
    if not code:
        return False
    # EIP-7702 designator: exactly 0xef0100 followed by one 20-byte address.
    if len(code) == 46 and code[:6].lower() == "ef0100":
        return False
    return True


def _clear_signable(response: dict[str, Any]) -> None:
    for key in ("transaction_request", "approval_request", "approval_reset_request", "authorization_request"):
        response[key] = None
    response["broadcast_enabled"] = False


def apply_gas_check(
    response: dict[str, Any], reader: Any, *, chain_id: int, wallet: str,
    block: Any = None, default_gas: int | None = None,
) -> dict[str, Any] | None:
    """Attach a bounded native gas check; unavailable reads are informational.

    ``reader`` is the route's existing deadline-aware reader. ``block`` may pin the
    operation to an already selected block. The helper never creates a reader.
    """
    tx = response.get("unsigned_transaction_request")
    if not isinstance(tx, dict):
        response.setdefault("gas_check", None)
        return None
    response["gas_check"] = None
    try:
        chain = registry().get(chain_id)
        if chain is None or not isinstance(wallet, str) or not wallet:
            raise ValueError("unknown chain or wallet")
        pinned = block or reader.head_block()
        if getattr(pinned, "chain_id", chain_id) != chain_id:
            raise ValueError("pinned block chain mismatch")
        header = reader.rpc("eth_getBlockByHash", [pinned.hash, False], require_result=True)
        if not isinstance(header, dict):
            raise ValueError("block header unavailable")
        base_fee = _quantity(header["baseFeePerGas"], "base fee")
        try:
            priority = _quantity(reader.rpc("eth_maxPriorityFeePerGas", [], require_result=True),
                                 "priority fee")
        except (OnchainError, ValueError, TypeError, OSError, AttributeError):
            priority = 0

        code_read_failed = False
        try:
            external_payer = _wallet_has_code(reader, wallet, pinned)
        except (OnchainError, ValueError, TypeError, OSError, AttributeError):
            # Keep the conservative wallet policy, but code uncertainty must not refuse signing.
            external_payer = False
            code_read_failed = True

        balance = _quantity(reader.rpc("eth_getBalance", [wallet, {"blockHash": pinned.hash}],
                                       require_result=True), "native balance")
        if "chainId" in tx and _quantity(tx["chainId"], "transaction chain id") != chain_id:
            raise ValueError("transaction chain mismatch")
        if "from" in tx and (not isinstance(tx["from"], str)
                             or tx["from"].lower() != wallet.lower()):
            raise ValueError("transaction sender mismatch")
        value = _quantity(tx.get("value", "0"), "transaction value")
        approval = response.get("unsigned_approval_request")
        reset = response.get("unsigned_approval_reset_request")
        available_approvals = sum(isinstance(item, dict) for item in (approval, reset))
        approvals_required = 0
        approvals_counted = available_approvals
        if isinstance(approval, dict):
            try:
                allowance = _approval_allowance(reader, approval, wallet=wallet, block=pinned)
                amount = _quantity(approval.get("amount"), "approval amount")
                approvals_required = int(allowance < amount)
                if (approvals_required and isinstance(reset, dict) and allowance > 0):
                    approvals_required += 1
                approvals_counted = approvals_required
            except (OnchainError, ValueError, TypeError, OSError, AttributeError):
                # An unreadable allowance reserves every available approval, but is not evidence
                # that approval gas is required for the current transaction.
                approvals_required = 0
        authorization_required = int(isinstance(response.get("unsigned_authorization_request"), dict))
        approvals_required += authorization_required
        approvals_counted += authorization_required
        gas_limit = tx.get("gasLimit", tx.get("gas"))
        source: str
        if gas_limit is not None:
            units = _quantity(gas_limit, "gas limit", positive=True)
            source = "router gas limit"
        else:
            rpc_fields = ("from", "to", "data", "value", "gasPrice", "maxFeePerGas",
                          "maxPriorityFeePerGas", "nonce", "chainId", "accessList", "type")
            rpc_tx = {key: tx[key] for key in rpc_fields if key in tx}
            if "from" not in rpc_tx:
                rpc_tx["from"] = wallet
            for key in ("value", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas",
                        "nonce", "chainId", "type"):
                if key in rpc_tx:
                    rpc_tx[key] = _hex(_quantity(rpc_tx[key], f"transaction {key}"))
            try:
                estimate = _quantity(reader.rpc("eth_estimateGas", [rpc_tx, {"blockHash": pinned.hash}],
                                                require_result=True), "gas estimate", positive=True)
                units = (estimate * 13 + 9) // 10
                source = "estimate"
            except OnchainError as exc:
                approvals = sum(response.get(key) is not None for key in
                                ("unsigned_approval_request", "unsigned_approval_reset_request", "unsigned_authorization_request"))
                if exc.revert_data is None or not approvals or default_gas is None:
                    raise
                units = _quantity(default_gas, "default gas", positive=True)
                source = "default"
        base_units = units
        required_units = base_units + 60_000 * approvals_required
        units = base_units + 60_000 * approvals_counted

        required = required_units * (base_fee + priority) + value
        reservation = units * (2 * base_fee + priority) + value
        response["gas_check"] = {
            "chain_id": chain_id,
            "native_symbol": chain.native_symbol,
            "native_decimals": chain.native_decimals,
            "native_balance_raw_amount": str(balance),
            "required_raw_amount": str(required),
            "reservation_raw_amount": str(reservation),
            "gas_units": units,
            "approvals_counted": approvals_counted,
            "payer": "external" if external_payer else "wallet",
            "base_fee_wei": base_fee,
            "priority_fee_wei": priority,
            "source": source,
        }
        if code_read_failed:
            _add_reason(response, "GAS_CHECK_UNAVAILABLE")
        if external_payer:
            if balance < value:
                _add_reason(response, "INSUFFICIENT_NATIVE_VALUE")
                _clear_signable(response)
        elif not code_read_failed and balance < required:
            _add_reason(response, "INSUFFICIENT_GAS")
            _clear_signable(response)
        elif balance < reservation and not code_read_failed:
            _add_reason(response, "LOW_GAS_BALANCE")
    except (OnchainError, ValueError, TypeError, KeyError, OSError, AttributeError):
        response["gas_check"] = None
        _add_reason(response, "GAS_CHECK_UNAVAILABLE")
    return response.get("gas_check")
