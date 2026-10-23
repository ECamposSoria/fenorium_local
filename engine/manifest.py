"""Canonical evidence manifest for a Par decision.

The manifest is the object a third party needs in order to re-derive a verdict without trusting
Par. It is serialized to canonical bytes; the keccak256 of those bytes is the ``decisionHash``
recorded in the ``DecisionPassport`` contract on Base, and the CIDv1 of the same bytes is the
``evidenceCid`` recorded beside it.

Verification is therefore a closed loop: fetch the CID, recompute both hashes locally, compare
against the chain. Nothing in the loop requires Par to be online or honest.

Two rules make the hash reproducible and are load-bearing:

* **No floats.** Rates are integer basis points and sizes are raw integer strings. A float would
  make the hash depend on the platform's repr.
* **Identity is a contract address**, never a ticker. ``symbol_reported`` is carried only as
  verification of what the contract itself says, and is explicitly not the key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from .keccak import keccak256

MANIFEST_SCHEMA = "par/decision-manifest/1"
# Current calculations use the size/depth impact model. Historical DecisionPassport manifests
# remain valid under the method_version already serialized into each record.
CURRENT_METHOD_VERSION = "par-engine/2026.09.02-size-impact-v1"

VERDICTS = ("BID", "NO BID", "NO QUOTE")

# The engine's seven gates, in evaluation order. Recorded in full on every verdict: a decision is
# only auditable if the gates that did *not* decide it are visible too.
GATES = ("CREDIT", "LIQUIDITY", "MATURITY", "REDEMPTION", "EVIDENCE", "PRICE", "SIZE")

# Evidence gaps that make an executable price impossible to substantiate. Keep this explicit: a
# BID must never be admitted by fuzzy matching against human-readable gap descriptions.
EXECUTION_BLOCKING_MISSING_FIELDS = frozenset({"amount_specific_execution"})

# Must match engine.composer.ROUTE_SCHEMA. Duplicated rather than imported to keep the
# manifest free of an import cycle with the composer; a test pins the two together.
ROUTE_BINDING_SCHEMA = "par/route-binding/1"


class ManifestError(ValueError):
    """Raised when a manifest cannot be built honestly."""


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's outcome.

    ``passed=None`` means the gate could not be evaluated — distinct from a measured failure, and
    the difference is the whole point of NO QUOTE.
    """

    gate: str
    passed: bool | None
    detail: str
    measured: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        if self.gate not in GATES:
            raise ManifestError(f"unknown gate {self.gate!r}; expected one of {GATES}")
        return {
            "gate": self.gate,
            "passed": self.passed,
            "detail": self.detail,
            "measured": {str(k): str(v) for k, v in sorted(self.measured.items())},
        }


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where an observation came from, and when it was pulled."""

    field_name: str
    endpoint: str
    pulled_at: int
    content_sha256: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "endpoint": self.endpoint,
            "pulled_at": int(self.pulled_at),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class DecisionManifest:
    instrument_chain_id: int
    instrument: str
    denomination_asset: str
    verdict: str
    method_version: str
    engine_commit: str
    generated_at: int
    maturity: int = 0
    quote_expiry: int = 0
    intended_size: int = 0
    size_decimals: int = 0
    executable_apy_bps: int | None = None
    indicative_apy_bps: int | None = None
    symbol_reported: str = ""
    venue: str = ""
    gates: tuple[GateResult, ...] = ()
    missing_fields: tuple[str, ...] = ()
    sources: tuple[SourceRef, ...] = ()
    route: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ManifestError(f"verdict must be one of {VERDICTS}, got {self.verdict!r}")
        if not _is_address(self.instrument):
            raise ManifestError(f"instrument must be a 0x-prefixed 20-byte address, got {self.instrument!r}")
        if self.instrument_chain_id <= 0:
            raise ManifestError("instrument_chain_id must be positive")
        if not self.method_version:
            raise ManifestError("method_version is required: a verdict without a method is not reproducible")
        if self.route is not None:
            if not isinstance(self.route, dict):
                raise ManifestError("route must be a RouteBinding.to_json() dictionary or None")
            _reject_floats(self.route, path="route")
        # A BID asserts an executable price at a size. Refuse to manufacture one that says nothing.
        if self.verdict == "BID":
            if self.executable_apy_bps is None:
                raise ManifestError("a BID must carry executable_apy_bps")
            if self.intended_size <= 0:
                raise ManifestError("a BID must carry a positive intended_size")
            if self.quote_expiry <= 0:
                raise ManifestError("a BID must carry a quote_expiry")
            failed_gates = sorted(gate.gate for gate in self.gates if gate.passed is False)
            if failed_gates:
                raise ManifestError(
                    f"BID has failed gate(s) {', '.join(failed_gates)}; the evidence supports NO BID"
                )

            unresolved_gates = sorted(
                gate.gate for gate in self.gates if gate.passed is not True and gate.passed is not False
            )
            if unresolved_gates:
                raise ManifestError(
                    f"BID has unresolved gate(s) {', '.join(unresolved_gates)}; "
                    "the evidence supports NO QUOTE"
                )

            blocking_fields = sorted(
                set(self.missing_fields) & EXECUTION_BLOCKING_MISSING_FIELDS
            )
            if blocking_fields:
                raise ManifestError(
                    f"BID is missing execution-blocking field(s) {', '.join(blocking_fields)}; "
                    "the evidence supports NO QUOTE"
                )
            if not self.route:
                raise ManifestError("BID has no bound route; the evidence supports NO QUOTE")
            _require_route_matches_decision(self.route, self)

            # Only inspecting the gates that were supplied lets a caller omit the inconvenient
            # ones entirely: a lone passing SIZE gate would sail through while CREDIT, EVIDENCE
            # and PRICE were simply absent. An absent gate is not a passing gate.
            present = [gate.gate for gate in self.gates]
            duplicates = sorted({name for name in present if present.count(name) > 1})
            if duplicates:
                raise ManifestError(f"BID has duplicate gate result(s) {', '.join(duplicates)}")
            absent = sorted(set(GATES) - set(present))
            if absent:
                raise ManifestError(
                    f"BID is missing gate result(s) {', '.join(absent)}; a gate that was never "
                    "evaluated is not a gate that passed, so the evidence supports NO QUOTE"
                )

    def to_json(self) -> dict[str, Any]:
        if self.route is not None:
            _reject_floats(self.route, path="route")
        return {
            "schema": MANIFEST_SCHEMA,
            "identity": {
                "chain_id": int(self.instrument_chain_id),
                "instrument": self.instrument.lower(),
                "denomination_asset": self.denomination_asset.lower(),
                # Carried as verification of what the contract reports, never as the key.
                "symbol_reported": self.symbol_reported,
                "venue": self.venue,
            },
            "decision": {
                "verdict": self.verdict,
                "maturity": int(self.maturity),
                "quote_expiry": int(self.quote_expiry),
                "intended_size": str(int(self.intended_size)),
                "size_decimals": int(self.size_decimals),
                "executable_apy_bps": self.executable_apy_bps,
                "indicative_apy_bps": self.indicative_apy_bps,
            },
            "method": {
                "method_version": self.method_version,
                "engine_commit": self.engine_commit,
                "generated_at": int(self.generated_at),
            },
            "gates": [gate.to_json() for gate in self.gates],
            "missing_fields": sorted(self.missing_fields),
            "route": self.route,
            "sources": sorted(
                (source.to_json() for source in self.sources),
                key=lambda item: (item["field"], item["endpoint"]),
            ),
        }


def _reject_floats(value: Any, *, path: str) -> None:
    """Keep caller-supplied route JSON inside the manifest's integer-only hash contract."""
    if isinstance(value, float):
        raise ManifestError(f"float found at {path}; canonical manifests require integers or strings")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_floats(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_floats(child, path=f"{path}[{index}]")


def _require_route_matches_decision(route: dict[str, Any], manifest: "DecisionManifest") -> None:
    """A BID's route must be *this* decision's route.

    Checking only that a route exists lets any dictionary satisfy the binding, including a
    perfectly valid route for a different chain, token, size or expiry. That would let a manifest
    claim amount-specific execution while committing to an unrelated trade.
    """
    schema = route.get("schema")
    if schema != ROUTE_BINDING_SCHEMA:
        raise ManifestError(f"BID route has schema {schema!r}, expected {ROUTE_BINDING_SCHEMA!r}")

    expected_chain = int(manifest.instrument_chain_id)
    route_chain = route.get("to_chain")
    if route_chain is None or int(route_chain) != expected_chain:
        raise ManifestError(
            f"BID route settles on chain {route_chain}, but the instrument is on chain {expected_chain}"
        )

    route_size = route.get("from_amount")
    if route_size is None or int(route_size) != int(manifest.intended_size):
        raise ManifestError(
            f"BID route is for size {route_size}, but the decision states {manifest.intended_size}"
        )

    route_expiry = route.get("expires_at")
    if route_expiry is None or int(route_expiry) != int(manifest.quote_expiry):
        raise ManifestError(
            f"BID route expires at {route_expiry}, but the decision states {manifest.quote_expiry}"
        )

    to_token = route.get("to_token")
    if not isinstance(to_token, str) or to_token.lower() != manifest.instrument.lower():
        raise ManifestError(
            f"BID route acquires {to_token}, but the instrument is {manifest.instrument.lower()}"
        )


def _is_address(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def canonical_bytes(manifest: DecisionManifest) -> bytes:
    """Serialize to the exact bytes that get hashed, pinned and published.

    Sorted keys and separator-tight JSON make the encoding independent of dict ordering and of the
    machine that produced it.
    """
    return json.dumps(
        manifest.to_json(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def decision_hash(manifest: DecisionManifest) -> str:
    """keccak256 of the canonical bytes — the value stored on Base."""
    return "0x" + keccak256(canonical_bytes(manifest)).hex()


def evidence_cid(manifest: DecisionManifest) -> str:
    """CIDv1 (raw codec, sha2-256, base32) of the canonical bytes.

    Computed locally so the CID is known before pinning and can be checked against whatever an
    IPFS node returns.
    """
    return cid_v1_raw(canonical_bytes(manifest))


def cid_v1_raw(payload: bytes) -> str:
    digest = sha256(payload).digest()
    multihash = bytes([0x12, 0x20]) + digest  # sha2-256, 32 bytes
    cid = bytes([0x01, 0x55]) + multihash  # CIDv1, raw codec
    return "b" + _base32_lower_nopad(cid)  # multibase prefix 'b'


_B32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def _base32_lower_nopad(data: bytes) -> str:
    bits = 0
    value = 0
    out: list[str] = []
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_B32_ALPHABET[(value >> bits) & 0x1F])
    if bits:
        out.append(_B32_ALPHABET[(value << (5 - bits)) & 0x1F])
    return "".join(out)


def require_cid_v1_raw(cid: str) -> str:
    """Reject any content address that is not CIDv1 with the raw codec.

    The manifest's address is the bare block: CIDv1, raw codec, sha2-256, base32, which always
    renders with a ``bafkrei`` prefix. The trap this guards is real and measured — ``ipfs add``
    without ``--cid-version=1`` returns a CIDv0 (``Qm…``) naming a *dag-pb-wrapped* block, a
    different object entirely. Recording one of those on chain produces a pointer that resolves to
    nothing while looking perfectly valid.
    """
    if not isinstance(cid, str) or not cid:
        raise ManifestError("evidence CID is empty")
    if cid.startswith("Qm"):
        raise ManifestError(
            f"{cid} is a CIDv0 (dag-pb wrapped). The manifest is addressed as CIDv1 raw, which is "
            "a different block and would not resolve. Pin with `--cid-version=1 --raw-leaves`."
        )
    if cid.startswith("bafybei"):
        raise ManifestError(
            f"{cid} is CIDv1 dag-pb, not raw. Pin the bare block with `--raw-leaves`."
        )
    if not cid.startswith("bafkrei"):
        raise ManifestError(
            f"{cid} is not a CIDv1 raw sha2-256 content address (expected a 'bafkrei' prefix)."
        )
    return cid


def verify(payload: bytes, expected_decision_hash: str, expected_cid: str) -> tuple[bool, str]:
    """Re-derive both anchors from raw manifest bytes.

    This is what the dashboard's *verify* button runs against whatever IPFS returns, and what any
    third party can run without Par.
    """
    require_cid_v1_raw(expected_cid)
    actual_hash = "0x" + keccak256(payload).hex()
    actual_cid = cid_v1_raw(payload)
    if actual_hash.lower() != expected_decision_hash.lower():
        return False, f"decision hash mismatch: recomputed {actual_hash}, chain says {expected_decision_hash}"
    if actual_cid != expected_cid:
        return False, f"CID mismatch: recomputed {actual_cid}, chain says {expected_cid}"
    return True, f"verified: {actual_hash} matches the manifest at {actual_cid}"
