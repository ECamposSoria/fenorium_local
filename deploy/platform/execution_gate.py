"""Whether this deployment may hand a caller something to execute, and what is still missing.

Execution was never one switch. The 501 it returns names three preconditions, and they are not
the same kind of thing: a legal opinion is a document, a security review is an engagement, and
the destination-failure behaviour is an observation nobody can make without sending real money
through a route designed to fail. Collapsing them into a boolean loses which one is outstanding,
and the answer a caller most needs when execution is unavailable is *why*.

So the gate is a set. It opens only when every precondition is cleared, it reports the ones that
are not, and it defaults to closed everywhere -- a deployment that says nothing gets nothing.
Clearing is configuration rather than code so that closing it again is an environment change and
a restart, not a rebuild, a redeploy and an outage in the middle of whatever went wrong.

Not every precondition applies to every instruction. The destination-failure experiment asks what
happens when a bridged route's destination leg fails after the source leg settled. A route that
settles in one transaction on one chain has no such leg: it completes or reverts as a whole. So
same-chain execution needs the legal review and the security audit, and cross-chain execution
needs all three.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


ENVIRONMENT_VARIABLE = "FENORIUM_EXECUTION_CLEARED"

# Each name is a precondition for offering an instruction a wallet could sign.
LEGAL_REVIEW = "LEGAL_REVIEW"
SECURITY_AUDIT = "SECURITY_AUDIT"
DESTINATION_FAILURE_EXPERIMENT = "DESTINATION_FAILURE_EXPERIMENT"

REQUIRED_CLEARANCES: tuple[str, ...] = (
    LEGAL_REVIEW,
    SECURITY_AUDIT,
    DESTINATION_FAILURE_EXPERIMENT,
)
SAME_CHAIN_CLEARANCES: tuple[str, ...] = (
    LEGAL_REVIEW,
    SECURITY_AUDIT,
)

# What a caller is told when a clearance is missing, in the vocabulary the API already uses.
_REASON_BY_CLEARANCE = {
    LEGAL_REVIEW: "LEGAL_REVIEW_REQUIRED",
    SECURITY_AUDIT: "SECURITY_AUDIT_REQUIRED",
    DESTINATION_FAILURE_EXPERIMENT: "DESTINATION_FAILURE_EXPERIMENT",
}


class ExecutionGateError(ValueError):
    """The configuration asked for something this gate will not do."""


@dataclass(frozen=True, slots=True)
class ExecutionGate:
    """The clearances a deployment claims. Absent means missing; there is no partial credit."""

    cleared: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = sorted(self.cleared - set(REQUIRED_CLEARANCES))
        if unknown:
            # A typo must not read as a clearance. Failing here makes a misspelled name a
            # startup error rather than a silently unopened -- or silently opened -- gate.
            raise ExecutionGateError(
                f"unknown execution clearance(s): {', '.join(unknown)}; "
                f"known names are {', '.join(REQUIRED_CLEARANCES)}"
            )

    def missing_for(self, *, cross_chain: bool) -> tuple[str, ...]:
        required = REQUIRED_CLEARANCES if cross_chain else SAME_CHAIN_CLEARANCES
        return tuple(name for name in required if name not in self.cleared)

    def is_open_for(self, *, cross_chain: bool) -> bool:
        return not self.missing_for(cross_chain=cross_chain)

    def reason_codes_for(self, *, cross_chain: bool) -> tuple[str, ...]:
        """Why this kind of execution is unavailable, sorted, empty when it is available."""

        return tuple(sorted(
            _REASON_BY_CLEARANCE[name] for name in self.missing_for(cross_chain=cross_chain)
        ))

    # Without a scope, the strictest one: every precondition, as before the scopes existed.
    @property
    def missing(self) -> tuple[str, ...]:
        return self.missing_for(cross_chain=True)

    @property
    def is_open(self) -> bool:
        return self.is_open_for(cross_chain=True)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Why execution is unavailable, sorted, empty when it is available."""

        return self.reason_codes_for(cross_chain=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_enabled": self.is_open,
            "cleared": sorted(self.cleared),
            "missing": list(self.missing),
            "reason_codes": list(self.reason_codes),
        }


CLOSED = ExecutionGate()


def from_environment(environ: dict[str, str] | None = None) -> ExecutionGate:
    """Read the clearances a deployment declares. Nothing declared is nothing cleared.

    Names are comma-separated and case-insensitive. An empty or absent variable is the normal
    case and yields a closed gate, so forgetting to configure a deployment cannot open one.
    """

    source = os.environ if environ is None else environ
    raw = (source.get(ENVIRONMENT_VARIABLE) or "").strip()
    if not raw:
        return CLOSED
    names = frozenset(part.strip().upper() for part in raw.split(",") if part.strip())
    return ExecutionGate(cleared=names)
