"""LI.FI Composer binding.

LI.FI finds and builds the route. Par decides whether that route should exist. This module is the
seam between the two: it turns a raw LI.FI quote into a *binding* — a route pinned to an exact
amount, an exact expected-worst-case output, and an expiry — so that the thing the engine decided
on is provably the same thing that would execute.

Three deliberate choices:

* **Price off ``toAmountMin``, never ``toAmount``.** The optimistic figure is a forecast; the
  minimum is the only number the route guarantees. A BID priced off the optimistic amount is a
  quote dressed up as a fill.
* **Par imposes the expiry.** A LI.FI quote carries an execution *duration* estimate, not a
  guarantee of price validity. Rather than infer one, Par states its own validity window and
  records it, so "this quote was considered live for 120 seconds" is an auditable claim.
* **The route is hashed.** ``route_hash`` covers the binding, so a route that changes between
  decision and execution no longer matches the passport.
"""

from __future__ import annotations

import json
import re
from dataclasses import InitVar, dataclass
from fractions import Fraction
from typing import Any

from .keccak import keccak256

ROUTE_SCHEMA = "par/route-binding/1"
# Bumped to /2 when `value` began committing to the quantity rather than the router's
# lexeme: the document's definition changed, so its hashes are not comparable across it.
TRANSACTION_REQUEST_SCHEMA = "par/transaction-request/2"

# How long Par treats a fetched quote as decision-grade. Not a LI.FI guarantee — Par's own rule.
DEFAULT_VALIDITY_SECONDS = 120

BPS = 10_000


class RouteError(ValueError):
    """Raised when a quote cannot be bound honestly."""


@dataclass(frozen=True, slots=True)
class RouteBinding:
    from_chain: int
    to_chain: int
    from_token: str
    to_token: str
    from_amount: int
    to_amount: int
    to_amount_min: int
    tool: str
    observed_at: int
    expires_at: int
    from_decimals: int | None = None
    to_decimals: int | None = None
    from_amount_usd: str | None = None
    to_amount_usd: str | None = None
    gas_cost_usd: str | None = None
    execution_duration_s: int | None = None
    approval_address: str | None = None
    transaction_request_hash: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": ROUTE_SCHEMA,
            "from_chain": int(self.from_chain),
            "to_chain": int(self.to_chain),
            "from_token": self.from_token.lower(),
            "to_token": self.to_token.lower(),
            # Raw integer strings: a float here would make the route hash platform-dependent.
            "from_amount": str(int(self.from_amount)),
            "to_amount": str(int(self.to_amount)),
            "to_amount_min": str(int(self.to_amount_min)),
            "tool": self.tool,
            "from_decimals": self.from_decimals,
            "to_decimals": self.to_decimals,
            "observed_at": int(self.observed_at),
            "expires_at": int(self.expires_at),
            "from_amount_usd": self.from_amount_usd,
            "to_amount_usd": self.to_amount_usd,
            "gas_cost_usd": self.gas_cost_usd,
            "execution_duration_s": self.execution_duration_s,
            "approval_address": (self.approval_address or "").lower() or None,
            "transaction_request_hash": self.transaction_request_hash,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

    @property
    def route_hash(self) -> str:
        """Identity of this route at this size. Changes if anything material changes."""
        return "0x" + keccak256(self.canonical_bytes()).hex()

    @property
    def slippage_tolerance_bps(self) -> int:
        """Width of the tolerance band the router was configured with, in basis points.

        This is ``toAmount`` versus ``toAmountMin``. Measured live on Base 2026-08-21 it reads a
        flat ~50bp at every size, because it reflects LI.FI's default 0.5% slippage *setting* —
        not depth. It is a configuration readout, not a market measurement, and must never be
        presented as price impact. Use :attr:`price_impact_bps` for that.
        """
        if self.to_amount <= 0:
            return 0
        return ((self.to_amount - self.to_amount_min) * BPS) // self.to_amount

    @property
    def price_impact_bps(self) -> int | None:
        """What the route actually costs at this size, in basis points of notional.

        Derived from the router's own USD valuations of both legs, so it captures real impact:
        the same USDC->WETH route on Base measured 50bp at $10k and 127bp at $10M. ``None`` when
        the router did not price both legs, because an unpriced leg is an evidence gap rather
        than a zero.
        """
        if self.from_amount_usd is None or self.to_amount_usd is None:
            return None
        try:
            spent = Fraction(self.from_amount_usd)
            received = Fraction(self.to_amount_usd)
        except ValueError:
            return None
        if spent <= 0:
            return None
        return round((spent - received) / spent * BPS)

    def is_live_at(self, now: int) -> bool:
        return now < self.expires_at


NATIVE_TOKEN_SENTINELS = frozenset({
    "0x0000000000000000000000000000000000000000",
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
})


def _request_address(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
        raise RouteError(f"transactionRequest.{name} must be a 20-byte address")
    return value.lower()


def _request_quantity(value: Any, name: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str):
        if re.fullmatch(r"0x[0-9a-fA-F]+", value):
            return int(value[2:], 16)
        if re.fullmatch(r"[0-9]+", value):
            return int(value, 10)
    raise RouteError(f"transactionRequest.{name} must be an integer quantity")


@dataclass(frozen=True, slots=True)
class UnsignedTransactionRequest:
    """Checked evidence of what a bound route would ask a wallet to sign."""

    chain_id: int
    from_address: str
    to: str
    data: str
    value: int
    gas_limit: int
    gas_price: int
    binding: InitVar[RouteBinding]
    requested_from: InitVar[str]

    def __post_init__(self, binding: RouteBinding, requested_from: str) -> None:
        for name in ("chain_id", "value", "gas_limit", "gas_price"):
            if type(getattr(self, name)) is not int:
                raise RouteError(f"{name} must be an integer")
        if self.chain_id <= 0 or self.chain_id != binding.from_chain:
            raise RouteError("transactionRequest.chainId does not match the route binding")
        sender = _request_address(self.from_address, "from")
        if sender != _request_address(requested_from, "requested_from"):
            raise RouteError("transactionRequest.from does not match the requested sender")
        _request_address(self.to, "to")
        if not isinstance(self.data, str) or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", self.data) is None:
            raise RouteError("transactionRequest.data must be non-empty, even-length hex bytes")
        if self.gas_limit <= 0 or self.gas_price <= 0:
            raise RouteError("transactionRequest gasLimit and gasPrice must be positive")
        if type(binding.from_amount) is not int or binding.from_amount <= 0:
            raise RouteError("route binding must have a positive integer from_amount")
        expected_value = binding.from_amount if binding.from_token.lower() in NATIVE_TOKEN_SENTINELS else 0
        if self.value != expected_value:
            raise RouteError("transactionRequest.value does not match the bound asset spend")

    def to_json(self) -> dict[str, Any]:
        # Decimal strings preserve arbitrary-size quantities in JSON consumers too.
        return {
            "chainId": self.chain_id,
            "from": self.from_address.lower(),
            "to": self.to.lower(),
            "data": self.data.lower(),
            "value": str(self.value),
            "gasLimit": str(self.gas_limit),
            "gasPrice": str(self.gas_price),
        }


def parse_unsigned_transaction_request(
    raw: dict[str, Any], *, binding: RouteBinding, requested_from: str,
) -> UnsignedTransactionRequest:
    """Validate a supplied router object without signing, RPC, or other I/O."""
    if not isinstance(raw, dict):
        raise RouteError("transactionRequest must be an object")
    result = UnsignedTransactionRequest(
        chain_id=_request_quantity(raw.get("chainId"), "chainId"),
        from_address=_request_address(raw.get("from"), "from"),
        to=_request_address(raw.get("to"), "to"),
        data=raw.get("data"),
        value=_request_quantity(raw.get("value"), "value"),
        gas_limit=_request_quantity(raw.get("gasLimit"), "gasLimit"),
        gas_price=_request_quantity(raw.get("gasPrice"), "gasPrice"),
        binding=binding,
        requested_from=requested_from,
    )
    if binding.transaction_request_hash != _transaction_request_hash(
        {"transactionRequest": raw}, fallback_chain_id=binding.from_chain,
    ):
        raise RouteError("transactionRequest does not match the bound payload hash")
    return result


_PLAIN_USD = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,18})?")


def _plain_decimal(value: Fraction) -> str:
    """A sum of plain decimal figures, written back as one: its denominator divides 10**18."""
    scaled = value * 10**18
    whole, fraction = divmod(int(scaled), 10**18)
    return f"{whole}.{fraction:018d}".rstrip("0").rstrip(".")


def parse_quote(
    raw: dict[str, Any],
    *,
    observed_at: int,
    validity_seconds: int = DEFAULT_VALIDITY_SECONDS,
) -> RouteBinding:
    """Bind a raw ``GET /v1/quote`` payload. Pure: does no network I/O."""
    if validity_seconds <= 0:
        raise RouteError("validity_seconds must be positive")

    action = _mapping(raw, "action")
    estimate = _mapping(raw, "estimate")

    from_amount = _integer(estimate, "fromAmount")
    to_amount = _integer(estimate, "toAmount")
    to_amount_min = _integer(estimate, "toAmountMin")

    if from_amount <= 0:
        raise RouteError("quote has no positive fromAmount; nothing was actually priced")
    if to_amount_min > to_amount:
        raise RouteError(f"toAmountMin {to_amount_min} exceeds toAmount {to_amount}: quote is incoherent")

    from_token = _token_address(action, "fromToken")
    to_token = _token_address(action, "toToken")
    from_decimals = _token_decimals(action, "fromToken")
    to_decimals = _token_decimals(action, "toToken")
    from_chain = int(_require(action, "fromChainId"))
    to_chain = int(_require(action, "toChainId"))

    gas_cost_usd = None
    gas_costs = estimate.get("gasCosts")
    if isinstance(gas_costs, list) and gas_costs:
        # Every gas component the route states, summed: the first alone understates a multi-step
        # route. One that is not a plain non-negative USD figure makes the total unknown.
        amounts = [cost.get("amountUSD") if isinstance(cost, dict) else None for cost in gas_costs]
        if all(isinstance(amount, str) and _PLAIN_USD.fullmatch(amount) for amount in amounts):
            total = sum((Fraction(amount) for amount in amounts), Fraction(0))
            gas_cost_usd = _plain_decimal(total)

    duration = estimate.get("executionDuration")
    execution_duration = int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
    transaction_request_hash = _transaction_request_hash(raw, fallback_chain_id=from_chain)

    return RouteBinding(
        from_chain=from_chain,
        to_chain=to_chain,
        from_token=from_token,
        to_token=to_token,
        from_amount=from_amount,
        to_amount=to_amount,
        to_amount_min=to_amount_min,
        tool=str(raw.get("tool") or "unknown"),
        from_decimals=from_decimals,
        to_decimals=to_decimals,
        observed_at=int(observed_at),
        expires_at=int(observed_at) + int(validity_seconds),
        from_amount_usd=_optional_str(estimate, "fromAmountUSD"),
        to_amount_usd=_optional_str(estimate, "toAmountUSD"),
        gas_cost_usd=gas_cost_usd,
        execution_duration_s=execution_duration,
        approval_address=_optional_str(estimate, "approvalAddress"),
        transaction_request_hash=transaction_request_hash,
    )


@dataclass(frozen=True, slots=True)
class SizeQuote:
    """One rung of the rate-versus-size ladder."""

    size: int
    binding: RouteBinding | None
    error: str | None = None

    @property
    def executable(self) -> bool:
        return self.binding is not None


def effective_rate_bps(
    binding: RouteBinding,
    *,
    days_to_maturity: int,
    redeems_into_input_asset: bool = False,
) -> int:
    """Annualized rate implied by the *guaranteed* output, in basis points.

    Uses ``to_amount_min`` and simple annualization. Returns a negative value when the route
    guarantees less than it consumes — that is a real outcome and is reported, not clamped.

    A rate only means something when the thing you get back is a claim on the thing you put in.
    A USDC→WETH swap has no rate: subtracting 18-decimal WETH from 6-decimal USDC produces a
    number, and that number is meaningless. So a route between two different assets raises unless
    the caller states that the output token redeems into the input asset (a principal token, say),
    and decimals are normalized before any subtraction.
    """
    if days_to_maturity <= 0:
        raise RouteError("days_to_maturity must be positive to annualize a rate")
    if binding.from_amount <= 0:
        raise RouteError("cannot compute a rate from a zero-size route")

    same_asset = binding.from_token.lower() == binding.to_token.lower()
    if not same_asset and not redeems_into_input_asset:
        raise RouteError(
            f"cannot annualize a route from {binding.from_token} to {binding.to_token}: they are "
            "different assets, so the difference is a swap result, not a yield. Pass "
            "redeems_into_input_asset=True only when the output token is a claim that redeems "
            "into the input asset."
        )

    scale_from, scale_to = _common_scale(binding.from_decimals, binding.to_decimals)
    principal = binding.from_amount * scale_from
    guaranteed = binding.to_amount_min * scale_to
    if principal <= 0:
        raise RouteError("cannot compute a rate from a zero-size route")

    # Integer arithmetic throughout: this number reaches the manifest and must be reproducible.
    # Annualize before dividing to basis points. Rounding to bps first collapses any total return
    # smaller than one basis point to zero, so a one-day gain of 0.99999bp would report 0bp rather
    # than roughly 364bp annualized, and a small loss would be exaggerated to -1bp.
    return ((guaranteed - principal) * BPS * 365) // (principal * days_to_maturity)


def _common_scale(from_decimals: int | None, to_decimals: int | None) -> tuple[int, int]:
    """Multipliers that put two token amounts on one scale.

    Unknown decimals are refused rather than assumed: guessing 18 is how a 6-decimal balance
    becomes a trillion-dollar position.
    """
    if from_decimals is None or to_decimals is None:
        raise RouteError(
            "token decimals are unknown, so the two legs cannot be compared; refusing to guess"
        )
    highest = max(from_decimals, to_decimals)
    return 10 ** (highest - from_decimals), 10 ** (highest - to_decimals)


def size_ladder(
    quote_fn,
    *,
    sizes: tuple[int, ...],
    observed_at: int,
    validity_seconds: int = DEFAULT_VALIDITY_SECONDS,
) -> tuple[SizeQuote, ...]:
    """Probe the same route at several sizes to find where it stops being executable.

    ``quote_fn`` takes a raw integer amount and returns a raw LI.FI quote payload. A rung that
    fails is recorded with its error rather than dropped: the size at which a venue stops quoting
    is the most informative point on the curve.
    """
    rungs: list[SizeQuote] = []
    for size in sizes:
        try:
            raw = quote_fn(size)
            binding = parse_quote(raw, observed_at=observed_at, validity_seconds=validity_seconds)
        except Exception as exc:  # noqa: BLE001 - the failure text is the evidence
            rungs.append(SizeQuote(size=size, binding=None, error=str(exc)))
            continue
        if binding.from_amount != size:
            rungs.append(
                SizeQuote(
                    size=size,
                    binding=None,
                    error=f"route returned fromAmount {binding.from_amount}, asked for {size}",
                )
            )
            continue
        rungs.append(SizeQuote(size=size, binding=binding))
    return tuple(rungs)


def capacity_cliff(ladder: tuple[SizeQuote, ...]) -> int | None:
    """Largest size that still quoted, or ``None`` when nothing did."""
    executable = [rung.size for rung in ladder if rung.executable]
    return max(executable) if executable else None


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise RouteError(f"quote is missing a usable {key!r} object")
    return value


def _require(node: dict[str, Any], key: str) -> Any:
    if key not in node or node[key] is None:
        raise RouteError(f"quote is missing required field {key!r}")
    return node[key]


def _integer(node: dict[str, Any], key: str) -> int:
    value = _require(node, key)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise RouteError(f"field {key!r} is not an integer amount: {value!r}") from exc


def _token_address(action: dict[str, Any], key: str) -> str:
    token = action.get(key)
    if not isinstance(token, dict) or not token.get("address"):
        raise RouteError(f"quote is missing {key}.address; a route without an exact token is not identity")
    return str(token["address"])


def _token_decimals(action: dict[str, Any], key: str) -> int | None:
    token = action.get(key)
    if not isinstance(token, dict):
        return None
    value = token.get("decimals")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_str(node: dict[str, Any], key: str) -> str | None:
    value = node.get(key)
    return None if value is None else str(value)


def _transaction_request_hash(
    raw: dict[str, Any], *, fallback_chain_id: int
) -> str | None:
    """Hash the executable transaction without embedding potentially large calldata.

    An absent LI.FI ``transactionRequest`` stays absent. A present object, including an empty
    object, hashes an explicit four-field document so it cannot collide with absence.
    """
    if "transactionRequest" not in raw or raw["transactionRequest"] is None:
        return None
    request = raw["transactionRequest"]
    if not isinstance(request, dict):
        raise RouteError("quote has a transactionRequest that is not an object")

    bound_request = {
        "schema": TRANSACTION_REQUEST_SCHEMA,
        "chain_id": _transaction_chain_id(request, fallback=fallback_chain_id),
        "to": _optional_transaction_text(request, "to", lowercase=True),
        "data": _optional_transaction_text(request, "data", lowercase=True),
        "value": _optional_transaction_value(request),
    }
    payload = json.dumps(
        bound_request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return "0x" + keccak256(payload).hex()


def _transaction_chain_id(request: dict[str, Any], *, fallback: int) -> int:
    value = request.get("chainId")
    if value is None:
        return int(fallback)
    if isinstance(value, bool):
        raise RouteError("transactionRequest.chainId is not an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise RouteError(f"transactionRequest.chainId is not an integer: {value!r}") from exc


def _optional_transaction_text(
    request: dict[str, Any], key: str, *, lowercase: bool = False
) -> str | None:
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RouteError(f"transactionRequest.{key} is not a string")
    return value.lower() if lowercase else value


def _optional_transaction_value(request: dict[str, Any]) -> str | None:
    """Commit to the quantity, not to the way the router happened to spell it.

    LI.FI writes `value` as hex ("0x0") while `UnsignedTransactionRequest.to_json` publishes a
    decimal string, so hashing the lexeme meant the evidence returned could not reproduce the
    commitment returned beside it. Hashing the quantity also stops "0x0", "0x00", "0" and 0 --
    one transaction, four spellings -- committing to four different things.
    """
    value = request.get("value")
    if value is None:
        return None
    return str(_request_quantity(value, "value"))
