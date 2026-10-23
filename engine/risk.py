from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


MIN_CVAR_RETURNS = 100
CONSERVATIVE_CVAR95_FALLBACK = 0.02


@dataclass(frozen=True)
class CvarEstimate:
    value: float
    kind: str
    observations: int
    source: str
    detail: str

    @property
    def measured(self) -> bool:
        return self.kind == "measured"


def empirical_cvar95(
    daily_returns: Iterable[float],
    *,
    source: str,
    min_returns: int = MIN_CVAR_RETURNS,
    periods_per_year: int = 365,
) -> CvarEstimate:
    values = tuple(float(value) for value in daily_returns)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("daily returns must be finite")
    if len(values) < min_returns:
        return conservative_cvar_fallback(
            source=source,
            observations=len(values),
            missing=f"daily_return_history: need {min_returns}, obtained {len(values)}",
        )
    tail_count = max(1, math.ceil(len(values) * 0.05))
    losses = sorted((-value for value in values), reverse=True)[:tail_count]
    daily_tail_loss = max(0.0, sum(losses) / tail_count)
    annualized = daily_tail_loss * math.sqrt(periods_per_year)
    return CvarEstimate(
        annualized,
        "measured",
        len(values),
        source,
        f"empirical historical CVaR95 from worst {tail_count}/{len(values)} daily returns; "
        f"annualized by sqrt({periods_per_year})",
    )


def conservative_cvar_fallback(*, source: str, observations: int, missing: str) -> CvarEstimate:
    return CvarEstimate(
        CONSERVATIVE_CVAR95_FALLBACK,
        "fallback",
        observations,
        source,
        f"FALLBACK, NOT A MEASUREMENT — explicit conservative policy constant "
        f"{CONSERVATIVE_CVAR95_FALLBACK:.2%}; COULD NOT VERIFY {missing}",
    )
