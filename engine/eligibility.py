"""Measured token holdability, established by exercising the transfer path.

Function selectors are an inventory of code, not proof of policy. A token can enforce eligibility
inside an ordinary transfer without exposing a named gate, while a detected registry says nothing
about whether the caller would pass it. The decisive observation is therefore a simulated transfer
from an address with no token balance: reaching the balance check shows that no earlier sender gate
blocked the call; an explicit compliance rejection shows the opposite; every other outcome remains
unknown.

Registry reads and EAS logs add identity-system context, but neither can promote missing evidence
into permission. All outputs from this module are measured observations, and transport failures are
surfaced instead of being converted into empty registries or credential sets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .attestation import MEASURED
from .chain import ChainError, RpcTransportError, eth_call, rpc_call, selector
from .keccak import keccak256

EAS_BASE = "0x4200000000000000000000000000000000000021"
DEFAULT_PROBE_FROM = "0x000000000000000000000000000000000000bEEF"
DEFAULT_PROBE_TO = "0x000000000000000000000000000000000000dEaD"
REGISTRY_SIGNATURES = (
    "identityRegistry()",
    "getDSService(uint256)",
    "kycRegistry()",
    "sanctionsList()",
)

_ERROR_SELECTOR = bytes.fromhex("08c379a0")
_ZERO_ADDRESS = "0x" + "0" * 40
_HEX_DATA = re.compile(r"0x[0-9a-fA-F]{8,}")
_REVERT_PREFIX = re.compile(r"(?:execution\s+)?reverted(?:\s+with\s+reason\s+string)?\s*:\s*", re.I)


class EligibilityError(RuntimeError):
    """Raised when eligibility evidence is malformed or cannot be read."""


class GateStatus(str, Enum):
    PERMISSIONLESS = "PERMISSIONLESS"
    GATED = "GATED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class GateProbe:
    chain_id: int
    token: str
    status: GateStatus
    revert_reason: str | None
    probed_at: datetime
    evidence_class: str = MEASURED


def _decode_error_bytes(raw: bytes) -> str | None:
    if len(raw) < 4 + 32 + 32 or raw[:4] != _ERROR_SELECTOR:
        return None
    offset = int.from_bytes(raw[4:36], "big")
    length_start = 4 + offset
    if offset < 32 or length_start + 32 > len(raw):
        return None
    length = int.from_bytes(raw[length_start:length_start + 32], "big")
    body_start = length_start + 32
    body_end = body_start + length
    if body_end > len(raw):
        return None
    try:
        return raw[body_start:body_end].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decode_error_value(value: Any) -> str | None:
    if isinstance(value, bytes):
        return _decode_error_bytes(value)
    if isinstance(value, str):
        for candidate in _HEX_DATA.findall(value):
            try:
                reason = _decode_error_bytes(bytes.fromhex(candidate[2:]))
            except ValueError:
                continue
            if reason is not None:
                return reason
        match = _REVERT_PREFIX.search(value)
        if match is not None:
            reason = value[match.end():].strip().strip("'\"")
            return reason or None
        return None
    if isinstance(value, dict):
        for key in ("data", "return", "result", "message", "error"):
            if key in value:
                reason = _decode_error_value(value[key])
                if reason is not None:
                    return reason
        for nested in value.values():
            reason = _decode_error_value(nested)
            if reason is not None:
                return reason
    if isinstance(value, (list, tuple)):
        for nested in value:
            reason = _decode_error_value(nested)
            if reason is not None:
                return reason
    return None


def decode_revert(raw_error: dict) -> str | None:
    """Decode a Solidity ``Error(string)`` from common JSON-RPC error envelopes."""
    if not isinstance(raw_error, dict):
        return None
    return _decode_error_value(raw_error)


# Modern contracts revert with a 4-byte custom-error selector and no string at all. A decoder
# that only understands Error(string) reads every one of them as UNKNOWN — measured 2026-08-22,
# that was 44 of 47 instruments, because OpenZeppelin v5 vaults raise ERC20InsufficientBalance.
# Selectors computed from their signatures, never guessed.
CUSTOM_ERROR_STATUS = {
    "0xe450d38c": GateStatus.PERMISSIONLESS,  # ERC20InsufficientBalance(address,uint256,uint256)
    "0x96c6fd1e": GateStatus.PERMISSIONLESS,  # ERC20InvalidSender(address)
    "0xec442f05": GateStatus.PERMISSIONLESS,  # ERC20InvalidReceiver(address)
    "0xb12c8f91": GateStatus.GATED,           # NotVerified(address)
    "0x7f63bd0f": GateStatus.GATED,           # NotPermissioned()
    "0xc397ba60": GateStatus.GATED,           # InsufficientPermissions()
    "0xf90e674a": GateStatus.GATED,           # TransferBlocked()
}


def classify_custom_error(payload: str | None) -> GateStatus:
    """Classify a revert carrying a custom-error selector rather than a string."""
    if not payload or not payload.startswith("0x") or len(payload) < 10:
        return GateStatus.UNKNOWN
    return CUSTOM_ERROR_STATUS.get(payload[:10].lower(), GateStatus.UNKNOWN)


def classify_revert(reason: str | None) -> GateStatus:
    """Classify only reasons that prove where execution stopped."""
    if reason is None:
        return GateStatus.UNKNOWN
    normalized = " ".join(reason.casefold().split())
    if any(phrase in normalized for phrase in (
        "insufficient balance",
        "balance is insufficient",
        "transfer amount exceeds balance",
        "amount exceeds balance",
        "exceeds the balance",
    )):
        return GateStatus.PERMISSIONLESS
    if any(phrase in normalized for phrase in (
        "kyc",
        "compliance",
        "whitelist",
        "white list",
        "allowlist",
        "allow list",
        "identity",
        "accredited",
        "qualified purchaser",
        "not eligible",
        "not authorized",
        "not authorised",
        "sanction",
    )):
        return GateStatus.GATED
    return GateStatus.UNKNOWN


def _address_word(address: str) -> str:
    try:
        value = int(address, 16)
    except (TypeError, ValueError) as exc:
        raise EligibilityError(f"invalid address {address!r}") from exc
    if not 0 <= value < 1 << 160:
        raise EligibilityError(f"invalid address {address!r}")
    return f"{value:064x}"


def _bytes32_word(value: str) -> str:
    candidate = value[2:] if isinstance(value, str) and value.startswith("0x") else value
    if not isinstance(candidate, str) or len(candidate) != 64:
        raise EligibilityError(f"expected bytes32 hex value, got {value!r}")
    try:
        int(candidate, 16)
    except ValueError as exc:
        raise EligibilityError(f"expected bytes32 hex value, got {value!r}") from exc
    return "0x" + candidate.lower()


def _eth_call_transfer(
    rpc: str, token: str, *, probe_from: str, probe_to: str, amount: int,
) -> TransferOutcome:
    data = transfer_probe_calldata(probe_to=probe_to, amount=amount)
    try:
        result = rpc_call(
            rpc,
            "eth_call",
            [{"from": probe_from, "to": token, "data": data}, "latest"],
        )
    except ChainError as exc:
        if isinstance(exc, RpcTransportError):
            raise
        if "failed against" in str(exc):
            raise EligibilityError(f"transfer probe failed: {exc}") from exc
        detail = str(exc)
        # The provider's own error object when rpc_call kept it; the flattened text otherwise.
        # Reading "reverted:" out of the text takes everything after it, the repr's quotes and
        # the next field included, so a bare selector never looks like one.
        structured = exc.rpc_error if isinstance(exc.rpc_error, dict) else None
        reason = decode_revert(structured if structured is not None else {"error": detail})
        # Anvil names a custom error inside the message ("custom error 0xe450d38c: <arguments>")
        # where op-geth leaves only the data; keep the selector form the data decoder produces, so
        # the same revert classifies the same whichever node answered.
        described = re.fullmatch(r"custom error (0x[0-9a-fA-F]{8})(?::.*)?", (reason or "").strip(), re.S)
        if described:
            reason = f"custom error {described.group(1).lower()}"
        if reason is None or re.fullmatch(r"0x[0-9a-fA-F]+", reason.strip()):
            # Hex-only text is payload, not a decoded Error(string) reason.
            match = re.search(r"0x[0-9a-fA-F]{8,}", detail)
            if match:
                digits = match.group(0)[2:]
                reason = _describe_revert_data(bytes.fromhex(digits[: len(digits) // 2 * 2]))
        return TransferOutcome(refused=True, reason=reason)
    if not isinstance(result, str) or not result.startswith("0x"):
        raise EligibilityError(f"transfer eth_call returned an unusable result: {result!r}")
    try:
        raw = bytes.fromhex(result[2:])
    except ValueError as exc:
        raise EligibilityError(f"transfer eth_call returned an unusable result: {result!r}") from exc
    if _returned_false(raw):
        return TransferOutcome(refused=True, reason="returned false")
    return TransferOutcome(refused=False)


def probe_transfer_gate(
    rpc: str,
    chain_id: int,
    token: str,
    *,
    probe_from: str = DEFAULT_PROBE_FROM,
    probe_to: str = DEFAULT_PROBE_TO,
) -> GateProbe:
    one = _eth_call_transfer(rpc, token, probe_from=probe_from, probe_to=probe_to, amount=1)
    zero = _eth_call_transfer(rpc, token, probe_from=probe_from, probe_to=probe_to, amount=0)
    probed_at = datetime.now(timezone.utc)
    probe = classify_transfer_pair(chain_id, token, one=one, zero=zero, probed_at=probed_at)
    if probe is None:
        # The sequential reader has always reported a reasonless refusal as UNKNOWN rather than
        # as a failed read; the batched reader's "no evidence" means the same thing there.
        return GateProbe(chain_id, token, GateStatus.UNKNOWN, None, probed_at)
    return probe


# A sanctions oracle screens named addresses; an identity registry decides who may hold the
# instrument at all. Conflating them would tell every reader that Ondo's USDY requires onboarding,
# when USDY transfers freely and only refuses sanctioned counterparties. The system is still
# reported, because it is a real fact about the token — it just does not gate the reader.
GATING_SYSTEMS = frozenset({"erc3643", "securitize-ds", "ondo-cash-kyc"})


def decode_registry_results(results: dict[str, bytes]) -> dict[str, str | None]:
    """Select the first non-zero registry address in doctrine-preserving probe order."""
    for signature, system in (
        ("identityRegistry()", "erc3643"),
        ("getDSService(uint256)", "securitize-ds"),
        ("kycRegistry()", "ondo-cash-kyc"),
        ("sanctionsList()", "chainalysis-sanctions"),
    ):
        raw = results.get(signature)
        if raw is None or len(raw) < 32:
            continue
        registry = "0x" + raw[:32][12:].hex()
        if registry != _ZERO_ADDRESS:
            return {"system": system, "registry": registry}
    return {"system": None, "registry": None}


def detect_registry(rpc: str, token: str) -> dict[str, str | None]:
    results: dict[str, bytes] = {}
    for signature in REGISTRY_SIGNATURES:
        data = registry_probe_calldata(signature)
        try:
            results[signature] = eth_call(rpc, token, data)
        except ChainError as exc:
            if isinstance(exc, RpcTransportError):
                raise
            if "returned an error" not in str(exc) and "returned empty data" not in str(exc):
                raise EligibilityError(f"registry probe failed at {signature}: {exc}") from exc
            continue
        decoded = decode_registry_results(results)
        if decoded["registry"] is not None:
            return decoded
    return {"system": None, "registry": None}


def registry_probe_calldata(signature: str) -> str:
    """Return one registry probe exactly as used by the sequential classifier."""
    if signature not in REGISTRY_SIGNATURES:
        raise EligibilityError(f"unsupported registry probe {signature!r}")
    data = selector(signature)
    if signature == "getDSService(uint256)":
        data += f"{4:064x}"
    return data


def transfer_probe_calldata(*, probe_to: str = DEFAULT_PROBE_TO, amount: int = 1) -> str:
    """Build the zero-balance transfer probe used in sequential and batched reads."""
    return selector("transfer(address,uint256)") + _address_word(probe_to) + f"{amount:064x}"


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """One simulated transfer: whether the token refused it, and what it said if it did."""

    refused: bool
    reason: str | None = None


_PANIC_SELECTOR = bytes.fromhex("4e487b71")


def _describe_revert_data(raw: bytes) -> str | None:
    reason = _decode_error_bytes(raw)
    if reason is not None:
        return reason
    if raw[:4] == _PANIC_SELECTOR and len(raw) >= 36:
        return f"panic 0x{int.from_bytes(raw[4:36], 'big'):x}"
    if len(raw) >= 4:
        return f"custom error 0x{raw[:4].hex()}"
    return None


def _returned_false(raw: bytes) -> bool:
    return len(raw) == 32 and not any(raw)


def multicall_transfer_outcome(success: bool, return_data: bytes) -> TransferOutcome:
    """Read one Multicall3 transfer sub-call. An ERC-20 answering `false` has refused too."""
    if success:
        if _returned_false(return_data):
            return TransferOutcome(refused=True, reason="returned false")
        return TransferOutcome(refused=False)
    return TransferOutcome(refused=True, reason=_describe_revert_data(return_data))


def _names_a_gate(reason: str | None) -> bool:
    if classify_revert(reason) is GateStatus.GATED:
        return True
    if reason and reason.startswith("custom error "):
        return classify_custom_error(reason[len("custom error "):]) is GateStatus.GATED
    return False


# Selectors computed from their signatures, never guessed. This is deliberately narrower
# than CUSTOM_ERROR_STATUS: an invalid sender or receiver is not balance evidence.
_BALANCE_ERROR_REASONS = frozenset(
    "custom error 0x" + keccak256(signature.encode())[:4].hex()
    for signature in (
        "ERC20InsufficientBalance(address,uint256,uint256)",
        "BorrowTooSmall()",
        "NotCollateralized()",
    )
)


def _has_balance_evidence(reason: str) -> bool:
    normalized = re.sub(r"[-_/]", " ", reason)
    return (
        classify_revert(normalized) is GateStatus.PERMISSIONLESS
        or reason == "panic 0x11"
        or reason in _BALANCE_ERROR_REASONS
    )


def classify_transfer_pair(
    chain_id: int,
    token: str,
    *,
    one: TransferOutcome,
    zero: TransferOutcome,
    probed_at: datetime,
) -> GateProbe | None:
    """Decide the gate from a transfer of one unit and a transfer of zero, between the same addresses.

    Reaching the balance check says nothing on its own. Centrifuge and Securitize check the
    balance before they check who is on the other side, so a permissioned tranche reverts a
    one-unit probe with "insufficient-balance" exactly as an open token does. A zero-amount
    transfer removes the balance requirement, but may also bypass amount-dependent restrictions.
    Permissionless requires zero going through or hitting only AmountIsZero(), and understood
    balance evidence from the one-unit refusal. Every other unexplained refusal remains unknown.
    Returns None when a refusal carried no data at all, which is no evidence either way.
    """
    for outcome in (zero, one):
        if outcome.refused and _names_a_gate(outcome.reason):
            return GateProbe(chain_id, token, GateStatus.GATED, outcome.reason, probed_at)
    zero_amount_guard = zero.refused and zero.reason == "custom error 0x43ad20fc"  # AmountIsZero()
    if zero.refused and not zero_amount_guard:
        if zero.reason is None:
            return None
        return GateProbe(
            chain_id, token, GateStatus.UNKNOWN,
            f"a zero-amount transfer was refused: {zero.reason}", probed_at,
        )
    if not one.refused:
        return GateProbe(
            chain_id, token, GateStatus.UNKNOWN,
            "a transfer from an empty wallet went through", probed_at,
        )
    if one.reason is None:
        return None
    return GateProbe(
        chain_id, token,
        GateStatus.PERMISSIONLESS if _has_balance_evidence(one.reason) else GateStatus.UNKNOWN,
        (f"{one.reason}; the zero-amount transfer hit AmountIsZero()" if zero_amount_guard
         else f"{one.reason}; the same transfer of zero went through"), probed_at,
    )


def classify_multicall_transfer(
    chain_id: int,
    token: str,
    *,
    one: tuple[bool, bytes],
    zero: tuple[bool, bytes],
    probed_at: datetime,
) -> GateProbe | None:
    """Classify the two Multicall3 transfer sub-calls issued for one token."""
    return classify_transfer_pair(
        chain_id,
        token,
        one=multicall_transfer_outcome(*one),
        zero=multicall_transfer_outcome(*zero),
        probed_at=probed_at,
    )


def read_attestation_uids(rpc: str, wallet: str, schema_uid: str) -> list[str]:
    event_topic = "0x" + keccak256(
        b"Attested(address,address,bytes32,bytes32)"
    ).hex()
    recipient_topic = "0x" + _address_word(wallet)
    result = rpc_call(
        rpc,
        "eth_getLogs",
        [{
            "address": EAS_BASE,
            "fromBlock": "0x0",
            "toBlock": "latest",
            "topics": [event_topic, recipient_topic, None, _bytes32_word(schema_uid)],
        }],
    )
    if not isinstance(result, list):
        raise EligibilityError(f"eth_getLogs returned an unusable result: {result!r}")
    uids: list[str] = []
    for log in result:
        if not isinstance(log, dict) or not isinstance(log.get("data"), str):
            raise EligibilityError(f"eth_getLogs returned a malformed attestation log: {log!r}")
        data = log["data"]
        if not data.startswith("0x") or len(data) < 66:
            raise EligibilityError(f"attestation log has no UID data word: {log!r}")
        try:
            bytes.fromhex(data[2:66])
        except ValueError as exc:
            raise EligibilityError(f"attestation log UID is not hex: {data!r}") from exc
        uids.append("0x" + data[2:66].lower())
    return uids


def classify_holdability(
    gate: GateProbe,
    *,
    credentials: frozenset[str],
    required: frozenset[str],
) -> tuple[str, str]:
    if gate.status is GateStatus.UNKNOWN:
        return (
            "not_available",
            "gate status unknown: absence of a detected gate is not permission",
        )
    missing = required - credentials
    if missing:
        names = ", ".join(sorted(missing))
        return "needs_onboarding", f"missing required credentials: {names}"
    if gate.status is GateStatus.GATED:
        if not required:
            # A gate exists and we have not established what satisfies it. An empty requirement
            # set is missing knowledge, not an absence of requirements, and reading it as
            # permission would clear a wallet through a gate nobody has characterised.
            return (
                "needs_onboarding",
                "gated, but the credentials this gate requires have not been established",
            )
        return "holdable_now", "wallet holds every required credential for the measured gate"
    return "holdable_now", "measured transfer path reached the token balance check"


def assess_gate(rpc: str, chain_id: int, token: str) -> GateProbe:
    """Both signals, registry first — the only combination correct on every token tested.

    A transfer simulation alone is not sufficient, and the counter-example is the largest
    instrument in the segment. Measured 2026-08-22, BlackRock's BUIDL reverts a plain transfer
    from an empty wallet with "Not enough tokens": DS Protocol checks the balance before it
    checks compliance, so the probe never reaches the gate and surfaces a balance-shaped failure
    on a token that is unambiguously permissioned. `classify_revert` returns UNKNOWN there rather
    than guessing, which is safe but says nothing.

    Reading the registry the token defers to settles it: a token that names a compliance registry
    is gated whatever a simulation happens to surface first.
    """
    registry = detect_registry(rpc, token)
    if registry.get("system") in GATING_SYSTEMS:
        return GateProbe(
            chain_id=chain_id,
            token=token,
            status=GateStatus.GATED,
            revert_reason=(
                f"defers to a {registry['system']} identity registry at {registry['registry']}"
            ),
            probed_at=datetime.now(timezone.utc),
        )
    return probe_transfer_gate(rpc, chain_id, token)
