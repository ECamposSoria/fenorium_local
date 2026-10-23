"""Grounded, local explanations of a verdict — and the guard that makes them safe to publish.

Par's whole claim is that it does not assert more than it measured. Handing a verdict to a
language model and printing whatever comes back would abandon that claim in the one place a
reader is most likely to trust it.

So the model is never the authority here. It is a rephraser working from facts this module hands
it, and everything it returns is checked before it is shown:

* **every number in the output must appear in the input facts.** A model that invents "rejected
  because liquidity was $50,000" on an instrument whose liquidity was never measured has produced
  a plausible sentence and a false statement, and plausible-and-false is worse than nothing.
* **the verdict word must match.** An explanation that says "approved" about a NO BID is rejected
  outright.
* **the raw reason strings are always available beside the prose**, so the generated text is a
  convenience, never the record.

Measured 2026-08-22 on the 1B model this ships with: asked to reply with exactly "PAR QVAC OK", it
replied "NO". That is the reliability floor these checks exist for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

VERDICT_WORDS = {
    "BID": {"bid", "approved", "accept", "accepted", "passes", "passed"},
    "NO BID": {"no bid", "reject", "rejected", "declined", "fails", "failed"},
    "NO QUOTE": {"no quote", "cannot price", "unpriceable", "insufficient", "unquoted"},
}

# Numbers a model may legitimately use to structure prose ("all seven gates", "the first of two").
STRUCTURAL_NUMBERS = {"0", "1", "2", "3", "4", "5", "6", "7", "100"}


class GroundingError(ValueError):
    """Raised when generated prose asserts something the facts do not support."""


@dataclass(frozen=True, slots=True)
class Facts:
    """Exactly what the model is allowed to know. Nothing is fetched at explain time."""

    instrument: str
    venue: str
    chain_id: int
    verdict: str
    tier: str | None
    days_to_maturity: float | None
    rate_percent: float | None
    rate_kind: str | None
    depth_usd: float | None
    missing: tuple[str, ...]
    reason: str

    def numbers(self) -> set[str]:
        """Every number the prose is permitted to contain."""
        allowed: set[str] = set(STRUCTURAL_NUMBERS)
        for value in (self.chain_id, self.days_to_maturity, self.rate_percent, self.depth_usd):
            if value is None:
                continue
            allowed.add(_normalize_number(str(value)))
            if isinstance(value, float):
                allowed.add(_normalize_number(f"{value:.0f}"))
                allowed.add(_normalize_number(f"{value:.1f}"))
                allowed.add(_normalize_number(f"{value:.2f}"))
        for token in re.findall(r"\d[\d,]*\.?\d*", self.reason):
            allowed.add(_normalize_number(token))
        # The maturity lives in the instrument name (USDC@2027-02-11). Rejecting the model for
        # writing "2027" treated a real fact as an invention — measured 2026-08-22, 2 of 6
        # rejections were this guard being wrong rather than the model.
        for token in re.findall(r"\d+", self.instrument):
            allowed.add(_normalize_number(token))
        return allowed

    def to_prompt(self) -> str:
        lines = [
            "Rewrite these facts as two short sentences a fixed-income analyst would read.",
            "Use ONLY these facts. Do not add numbers. Do not speculate about causes.",
            "",
            f"instrument: {self.instrument}",
            f"venue: {self.venue}",
            f"chain id: {self.chain_id}",
            f"verdict: {self.verdict}",
        ]
        if self.tier:
            lines.append(f"credit tier: {self.tier}")
        if self.days_to_maturity is not None:
            lines.append(f"days to maturity: {self.days_to_maturity:.0f}")
        if self.rate_percent is not None:
            lines.append(f"published rate: {self.rate_percent:.2f} percent ({self.rate_kind})")
        if self.depth_usd is not None:
            lines.append(f"reported depth in dollars: {self.depth_usd:.0f}")
        if self.missing:
            lines.append(f"evidence not obtained: {', '.join(self.missing)}")
        lines += ["", f"engine reason string: {self.reason}", "",
                  "Two sentences. State the verdict and why. No numbers beyond those above."]
        return "\n".join(lines)


def _normalize_number(token: str) -> str:
    token = token.replace(",", "").rstrip(".")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token or "0"


def facts_from_point(point: dict[str, Any]) -> Facts:
    rate = point.get("executable_apy")
    kind = "executable"
    if rate is None:
        rate = point.get("indicative_apy")
        kind = "indicative"
    reason = point.get("reason") or ""
    return Facts(
        instrument=str(point.get("instrument") or point.get("identity") or "unknown"),
        venue=str(point.get("venue") or "unknown"),
        chain_id=int(point.get("chain_id") or 0) if str(point.get("chain_id") or "").isdigit() else 0,
        verdict=str(point.get("status") or ""),
        tier=point.get("tier"),
        days_to_maturity=point.get("days_to_maturity"),
        rate_percent=None if rate is None else float(rate) * 100,
        rate_kind=None if rate is None else kind,
        depth_usd=point.get("liquidity_usd"),
        missing=tuple(sorted(set(re.findall(r"missing field: (\w+)", reason)))),
        reason=reason,
    )


def check_grounding(text: str, facts: Facts) -> tuple[bool, str]:
    """Return (ok, detail). The caller shows the raw reason string when this fails."""
    if not text or not text.strip():
        return False, "model returned nothing"

    allowed = facts.numbers()
    for token in re.findall(r"\d[\d,]*\.?\d*", text):
        if _normalize_number(token) not in allowed:
            return False, f"invented number {token!r}: not present in the facts"

    lowered = text.lower()
    expected = VERDICT_WORDS.get(facts.verdict, set())
    if expected and not any(word in lowered for word in expected):
        return False, f"prose does not state the {facts.verdict} verdict"

    for other, words in VERDICT_WORDS.items():
        if other == facts.verdict:
            continue
        # "no bid" contains "bid", so only flag a competing verdict named explicitly.
        if other.lower() in lowered and facts.verdict.lower() not in lowered:
            return False, f"prose asserts {other} but the verdict is {facts.verdict}"

    denial = _denied_fact(lowered, facts)
    if denial:
        return False, denial

    return True, "grounded: numbers, verdict and stated facts all match"


# Phrases that deny something we actually hold. Numeric grounding cannot catch these: measured
# 2026-08-22, the model wrote "with no credit tier assigned" about an instrument whose tier is
# TIER_A, invented no number, said "bid", and was published. A check on numbers alone is
# necessary and not sufficient.
_DENIALS = (
    ("tier", ("no credit tier", "no tier", "tier is not", "tier not assigned", "untiered",
              "without a credit tier", "lacks a credit tier")),
    ("depth", ("no depth", "depth is not reported", "no reported depth", "without reported depth")),
    ("rate", ("no published rate", "no rate is published", "without a published rate")),
)


def _denied_fact(lowered: str, facts: Facts) -> str | None:
    held = {"tier": facts.tier, "depth": facts.depth_usd, "rate": facts.rate_percent}
    for name, phrases in _DENIALS:
        if held.get(name) is None:
            continue
        for phrase in phrases:
            if phrase in lowered:
                return f"prose denies a fact we hold ({name}={held[name]!r}): {phrase!r}"
    return None


def explain(point: dict[str, Any], generate) -> dict[str, Any]:
    """Build facts, call `generate(prompt) -> str`, and refuse ungrounded output.

    `generate` is injected so this module never imports an inference runtime and stays testable
    without one.
    """
    facts = facts_from_point(point)
    try:
        text = generate(facts.to_prompt())
    except Exception as exc:  # noqa: BLE001 - a runtime failure is not an explanation
        return {"instrument": facts.instrument, "ok": False,
                "detail": f"generation failed: {exc}", "text": None, "reason": facts.reason}

    ok, detail = check_grounding(text or "", facts)
    return {
        "instrument": facts.instrument, "verdict": facts.verdict, "ok": ok, "detail": detail,
        "text": text.strip() if ok and text else None, "reason": facts.reason,
    }
