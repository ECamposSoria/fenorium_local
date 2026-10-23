from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

from .credit import CreditDecision


@dataclass(frozen=True)
class EvidenceRow:
    instrument: str
    decision: CreditDecision
    executable_capacity_usd: float | None


@dataclass(frozen=True)
class EvidenceSummary:
    total: int
    credit_rejections: tuple[str, ...]
    evidence_rejections: tuple[str, ...]
    mixed_rejections: tuple[str, ...]
    missing_field_counts: tuple[tuple[str, int], ...]
    evaluable_capacity_usd: float
    evaluable_capacity_missing: tuple[str, ...]


SOURCE_FIELDS_AND_MAX_POINTS = {
    "DefiLlama coin price history": ({"peg_history"}, 30.0),
    "DefiLlama protocol TVL time series": ({"protocol_tvl", "protocol_tvl_trend"}, 30.0),
    "Oracle model classification": ({"oracle_model_risk"}, 10.0),
    "Venue executable quote/orderbook": ({"market_liquidity_usd"}, 15.0),
    "On-chain holder indexer": ({"holder_concentration"}, 5.0),
}


def summarize_evidence(rows: Iterable[EvidenceRow]) -> EvidenceSummary:
    materialized = tuple(rows)
    credit = tuple(row.instrument for row in materialized
                   if row.decision.credit_blockers and not row.decision.evidence_blockers)
    evidence = tuple(row.instrument for row in materialized
                     if row.decision.evidence_blockers and not row.decision.credit_blockers)
    mixed = tuple(row.instrument for row in materialized
                  if row.decision.credit_blockers and row.decision.evidence_blockers)
    counts = Counter(field for row in materialized if row.decision.evidence_blockers
                     for field in row.decision.missing_fields)
    capacity = 0.0
    capacity_missing: list[str] = []
    for row in materialized:
        if row.decision.evidence_blockers:
            continue
        if row.executable_capacity_usd is None:
            capacity_missing.append(row.instrument)
        elif row.executable_capacity_usd > 0:
            capacity += row.executable_capacity_usd
    return EvidenceSummary(
        len(materialized), credit, evidence, mixed,
        tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        capacity, tuple(capacity_missing),
    )


def top_unlock_sources(rows: Iterable[EvidenceRow], limit: int = 3) -> tuple[tuple[str, int], ...]:
    """Potential evidence-gate unlocks if the named source returns threshold-quality evidence.

    This does not assume the missing value is good. It asks only whether that source can supply
    enough points to close the observed score deficit; the report must retain this condition.
    """
    source_counts: Counter[str] = Counter()
    for row in rows:
        if not row.decision.evidence_blockers:
            continue
        missing = set(row.decision.missing_fields)
        for source, (fields, max_points) in SOURCE_FIELDS_AND_MAX_POINTS.items():
            if missing.intersection(fields) and row.decision.score + max_points >= 60.0:
                source_counts[source] += 1
    return tuple(sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))[:limit])
