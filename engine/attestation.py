"""Facts that come from a document, and the line between them and facts we measured.

Everything else in Par is checkable against something: a chain read, an API response, a
contract call. Redemption terms are not. "Settlement is T+1 with an 11:00 New York cutoff"
exists only in the issuer's prose — there is no second source to reconcile it against, and no
amount of care changes that.

So it does not enter the evidence pool as a measured fact. It enters as an *attestation*, which
is a weaker and differently-shaped thing:

* a **measured** fact says "we observed this";
* an **attested** fact says "this document, at this hash, said this, in these exact words".

The second is provenance, not verification, and the gates treat it as strictly weaker. Mixing
the two would let a sentence scraped off a marketing page carry the same weight as a contract
read, which is the failure this project exists to catch.

What *is* deterministically checkable is the citation. An extractor must return the exact span
it read the fact from, and that span must appear verbatim in the document we hashed. That does
not make the claim true — the issuer could be wrong — but it makes fabrication detectable, and
fabricated citations are the dominant failure mode of extraction. A model that invents a
redemption window will almost always invent the sentence it came from too.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MEASURED = "measured"
ATTESTED = "attested"

# Gates whose evidence must be observed, never read off a page. A redemption *window* may be
# attested; a redemption *ratio* may not — that one is on chain and there is no excuse.
MEASURED_ONLY_GATES = frozenset({"PRICE", "SIZE", "LIQUIDITY", "REDEMPTION"})

MIN_QUOTE_CHARS = 12


class AttestationError(ValueError):
    """Raised when a claimed citation cannot be found in the document it cites."""


def _normalize(text: str) -> str:
    """Collapse whitespace so a quote survives PDF extraction without becoming fuzzy matching.

    Only whitespace is touched. Characters, ordering and casing must still match exactly: the
    point is to tolerate line wrapping, not to tolerate paraphrase.
    """
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True, slots=True)
class Document:
    """A retrieved document, pinned by content hash so a citation can be re-checked later."""

    url: str
    text: str
    retrieved_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def contains(self, quote: str) -> bool:
        return _normalize(quote) in _normalize(self.text)


@dataclass(frozen=True, slots=True)
class Attestation:
    """One field, the value read for it, and the exact words it was read from."""

    field: str
    value: Any
    quote: str
    document_url: str
    document_sha256: str
    retrieved_at: datetime
    extractor: str

    evidence_class: str = ATTESTED

    def to_json(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "quote": self.quote,
            "document_url": self.document_url,
            "document_sha256": self.document_sha256,
            "retrieved_at": self.retrieved_at.astimezone(timezone.utc).isoformat(),
            "extractor": self.extractor,
            "evidence_class": self.evidence_class,
        }


def attest(document: Document, *, field: str, value: Any, quote: str, extractor: str) -> Attestation:
    """Record a fact read from `document`, refusing any citation the document does not contain."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AttestationError(f"{field}: an attestation with no value is not a fact")
    if len(_normalize(quote)) < MIN_QUOTE_CHARS:
        # A two-word "quote" matches half the document and cites nothing.
        raise AttestationError(
            f"{field}: quote is too short to identify a source span ({MIN_QUOTE_CHARS} chars minimum)"
        )
    if not document.contains(quote):
        raise AttestationError(
            f"{field}: quoted span does not appear in {document.url} — citation fabricated"
        )
    return Attestation(
        field=field, value=value, quote=quote, document_url=document.url,
        document_sha256=document.sha256, retrieved_at=document.retrieved_at, extractor=extractor,
    )


def verify(attestation: Attestation, document: Document) -> tuple[bool, str]:
    """Re-check an attestation against a freshly retrieved document.

    Used to detect the case a hash alone cannot distinguish: the issuer quietly changed the
    terms. A failure here is a finding, not an error — it means the fact is now unsupported.
    """
    if document.sha256 != attestation.document_sha256:
        if not document.contains(attestation.quote):
            return False, (
                f"{attestation.field}: document changed and no longer contains the cited span "
                f"— the attested value is now unsupported"
            )
        return True, f"{attestation.field}: document changed but the cited span survives"
    return True, f"{attestation.field}: document unchanged"


def admissible(field_gate: str, evidence_class: str) -> tuple[bool, str]:
    """Whether a fact of this class may satisfy this gate."""
    if evidence_class == MEASURED:
        return True, "measured"
    if field_gate in MEASURED_ONLY_GATES:
        return False, (
            f"{field_gate} requires a measured fact: a document saying so is not an observation"
        )
    return True, f"{field_gate} accepts an attested fact, scored below a measured one"
