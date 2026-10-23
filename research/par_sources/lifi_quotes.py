"""Live LI.FI route measurement without turning transport failures into market findings.

LI.FI's quote endpoint returns useful negative evidence as an HTTP 404 JSON body, while throttles
and network failures say nothing about liquidity.  This adapter preserves that distinction,
paces repeated ladder requests, and hands raw payloads to the engine's existing Composer parser
so guaranteed output and exact requested size retain their established meaning.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from engine.composer import RouteBinding, RouteError, parse_quote, size_ladder
from engine.registry import registry
from engine.obtainable import (MARKET_REFUSAL_PREFIX, Obtainability,
                               market_answered as _market_answered, summarise_ladder)
from engine.acquire.fetch import retry_after_seconds
from engine.portfolio.contracts import safe_detail


_ADDRESS = __import__("re").compile(r"0x[0-9a-fA-F]{40}")
DEFAULT_SIZES_USD = (10_000, 100_000, 250_000, 1_000_000, 5_000_000)
QUOTE_URL = "https://li.quest/v1/quote"
# Stated on every request. LI.FI's own default rose on 2026-10-12 from 0.5% to 1% for same-chain swaps and about
# 2.5% for cross-chain routes with a destination swap; the minimum a buyer is guaranteed, and the route cost
# guards measured against it, were calibrated on 0.5%.
SLIPPAGE = "0.005"
# The USDC an instrument's obtainability is measured in, on every chain an instrument may be bought
# on (the chain registry's `buy` role). BSC uses Binance-pegged USDC, not Circle-native USDC. The
# spend token's precision determines fromAmount; the destination PT/share precision must never
# scale the spend.
_BUY_CHAINS = tuple(chain for chain in registry().with_role("buy") if chain.usdc is not None)
USDC_BY_CHAIN = {chain.chain_id: chain.usdc.address for chain in _BUY_CHAINS}
USDC_DECIMALS_BY_CHAIN = {chain.chain_id: chain.usdc.decimals for chain in _BUY_CHAINS}
BUDGET_REFUSAL = "LI.FI measurement budget reached (wall-clock timeout)"
DEFAULT_FROM_ADDRESS = "0x000000000000000000000000000000000000bEEF"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 par-obtainability/1.0"
PACE_SECONDS = 1.2


class LifiQuoteError(RouteError):
    """Raised when LI.FI did not return a usable route observation."""

    retry_after: float | None = None


def _retry_after_seconds(headers) -> float | None:
    return None if headers is None else retry_after_seconds(headers.get("Retry-After"))


def _error_message(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message) if message else None


@dataclass(frozen=True)
class PayAsset:
    """What the buyer actually holds and intends to pay with.

    The depth ladder keeps paying in the chain's USDC, and deliberately: its rungs are dollar
    sizes, so quoting them in a token whose dollar value moves would need a price for that token
    before a single rung could be built, and the figure it produces is named `max_quoted_usd`.
    A buyer holding WETH is a different question -- an exact amount they already have, in units
    they already hold -- and that question needs no dollar price to ask.
    """

    address: str
    decimals: int
    symbol: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.address, str) or not _ADDRESS.fullmatch(self.address):
            raise LifiQuoteError("pay_with address must be a 20-byte hex address")
        if isinstance(self.decimals, bool) or not isinstance(self.decimals, int) or self.decimals < 0:
            raise LifiQuoteError("pay_with decimals must be a non-negative integer")


def usdc_for(chain_id: int) -> PayAsset:
    """The chain's USDC, which is what a dollar-denominated ladder is priced in."""
    quote_asset = USDC_BY_CHAIN.get(chain_id)
    decimals = USDC_DECIMALS_BY_CHAIN.get(chain_id)
    if quote_asset is None or decimals is None:
        raise LifiQuoteError(f"LI.FI USDC quote asset is not configured for chain {chain_id}")
    return PayAsset(address=quote_asset, decimals=decimals, symbol="USDC")


def quote_fn_for(
    chain_id: int,
    token: str,
    *,
    from_address: str,
    api_key: str | None = None,
    decimals: int,
    deadline: float | None = None,
    pay_with: PayAsset | None = None,
    integrator: str | None = None,
    fee: str | None = None,
    from_chain: int | None = None,
) -> Callable[[int], dict]:
    """Build the raw-payload callback consumed by :func:`composer.size_ladder`.

    `chain_id` is where the instrument lives; `from_chain`, when given, is where the payment
    starts. Left out, the route stays on one chain.
    """
    source_chain = chain_id if from_chain is None else from_chain
    pay = pay_with if pay_with is not None else usdc_for(source_chain)
    quote_asset = pay.address
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise LifiQuoteError("decimals must be a non-negative integer")

    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["x-lifi-api-key"] = api_key
    last_request_at: float | None = None

    def remaining_budget() -> float:
        remaining = 40 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise LifiQuoteError(BUDGET_REFUSAL)
        return remaining

    def quote(raw_amount: int) -> dict:
        nonlocal last_request_at
        if raw_amount <= 0:
            raise LifiQuoteError("quote amount must be positive")
        remaining_budget()
        if last_request_at is not None:
            remaining = PACE_SECONDS - (time.monotonic() - last_request_at)
            if remaining > 0:
                time.sleep(min(remaining, remaining_budget()))

        timeout = min(40, remaining_budget())

        query = {
            "fromChain": source_chain,
            "toChain": chain_id,
            "fromToken": quote_asset,
            "toToken": token,
            "fromAmount": raw_amount,
            "fromAddress": from_address,
            "slippage": SLIPPAGE,
        }
        # Without it LI.FI files the route under its own default, `lifi-api`, even when the
        # request carries our key. Measurement quotes leave it out: they are not routes anyone
        # takes, and attributing them would count research as volume.
        if integrator:
            query["integrator"] = integrator
            # A fee is paid to an integration; without one there is nobody to pay it to, and LI.FI
            # refuses a fee for an integration whose fee wallet is not configured.
            if fee:
                query["fee"] = fee
        params = urllib.parse.urlencode(query)
        request = urllib.request.Request(f"{QUOTE_URL}?{params}", headers=headers)
        try:
            last_request_at = time.monotonic()
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response, parse_float=Decimal)
        except urllib.error.HTTPError as exc:
            message = _error_message(exc.read())
            if exc.code == 404:
                # The one status that carries a market's answer. Saying so here is what lets the
                # engine tell a refusal apart from a rejected key without parsing vendor prose.
                raise LifiQuoteError(
                    f"{MARKET_REFUSAL_PREFIX}: "
                    f"{message or 'LI.FI returned HTTP 404 without a refusal message'}"
                ) from exc
            if exc.code == 429:
                detail = f": {message}" if message else ""
                refusal = LifiQuoteError(f"LI.FI rate limited the quote (HTTP 429){detail}")
                # How long LI.FI asked us to wait, so a caller can stop asking for exactly that long.
                refusal.retry_after = _retry_after_seconds(exc.headers)
                raise refusal from exc
            detail = f": {message}" if message else ""
            raise LifiQuoteError(
                f"LI.FI quote request failed with HTTP {exc.code}{detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise LifiQuoteError(f"LI.FI quote connection/transport failed: {safe_detail(exc)}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LifiQuoteError("LI.FI connection returned a response that was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise LifiQuoteError("LI.FI connection returned a response that was not a JSON object")
        remaining_budget()
        action = payload.get("action")
        if not isinstance(action, Mapping):
            raise LifiQuoteError("LI.FI response action was not a JSON object")
        returned_decimals = (action.get("toToken") or {}).get("decimals")
        if type(returned_decimals) is not int or returned_decimals != decimals:
            raise LifiQuoteError("LI.FI response decimals mismatch; connection returned invalid evidence")
        return payload

    return quote


@dataclass(frozen=True, slots=True)
class FetchedRoute:
    """One route this process asked for and received, beside the payload it was bound from.

    The binding alone cannot produce what a buyer would sign -- that lives in the payload's
    ``transactionRequest`` -- and the payload alone proves nothing, because only the binding
    carries the hash that covers it. Whoever fetched the route holds both or neither.
    """

    payload: dict
    binding: RouteBinding
    fetched_at: datetime


def fetch_exact(
    chain_id: int,
    token: str,
    *,
    pay_with: PayAsset,
    amount_raw: int,
    decimals: int,
    from_address: str = DEFAULT_FROM_ADDRESS,
    api_key: str | None = None,
    deadline: float | None = None,
    integrator: str | None = None,
    fee: str | None = None,
    from_chain: int | None = None,
) -> FetchedRoute:
    """Fetch and bind one route for an exact amount the buyer already holds.

    The depth ladder answers "how much of this instrument could anyone acquire", in dollars,
    which is why it pays in USDC. This answers the buyer's own question -- "I hold this many of
    this token; what do I get" -- and needs no dollar price to ask it, because the amount is
    already denominated in the thing they hold.

    Returns the binding with its guaranteed output, so what reaches a decision is the amount the
    route commits to rather than the amount it hopes for.
    """
    if isinstance(amount_raw, bool) or not isinstance(amount_raw, int) or amount_raw <= 0:
        raise LifiQuoteError("amount_raw must be a positive integer in the pay asset's own units")
    source_chain = chain_id if from_chain is None else from_chain
    if source_chain == chain_id and pay_with.address.lower() == str(token).lower():
        raise LifiQuoteError("the asset being bought cannot also be the asset paid with")
    observed_at = datetime.now(timezone.utc)
    quote = quote_fn_for(
        chain_id, token, from_address=from_address, api_key=api_key,
        decimals=decimals, deadline=deadline, pay_with=pay_with, integrator=integrator, fee=fee,
        from_chain=source_chain,
    )
    payload = quote(amount_raw)
    binding = parse_quote(payload, observed_at=int(observed_at.timestamp()))
    # The answer has to be to the question that was asked. `size_ladder` checks the amount for
    # its own rungs; this path had no check at all, so a well-formed response describing a
    # different trade -- another amount, another token, another chain -- would have been bound
    # and handed on as though it were this one.
    mismatches = []
    if binding.from_amount != amount_raw:
        mismatches.append(f"amount {binding.from_amount} for a request of {amount_raw}")
    if binding.from_token.lower() != pay_with.address.lower():
        mismatches.append(f"paid with {binding.from_token}, not {pay_with.address}")
    if binding.to_token.lower() != str(token).lower():
        mismatches.append(f"bought {binding.to_token}, not {token}")
    if binding.from_chain != source_chain or binding.to_chain != chain_id:
        mismatches.append(
            f"chains {binding.from_chain}->{binding.to_chain}, not {source_chain}->{chain_id}")
    if binding.from_decimals != pay_with.decimals:
        mismatches.append(f"pay decimals {binding.from_decimals}, not {pay_with.decimals}")
    if binding.to_decimals != decimals:
        mismatches.append(f"asset decimals {binding.to_decimals}, not {decimals}")
    if mismatches:
        raise LifiQuoteError(
            "LI.FI answered a different trade than the one requested: " + "; ".join(mismatches))
    return FetchedRoute(payload=payload, binding=binding, fetched_at=observed_at)


def fetch_exact_sale(
    chain_id: int,
    token: str,
    *,
    receive_with: PayAsset,
    amount_raw: int,
    decimals: int,
    from_address: str = DEFAULT_FROM_ADDRESS,
    api_key: str | None = None,
    deadline: float | None = None,
    integrator: str | None = None,
    fee: str | None = None,
    to_chain: int | None = None,
) -> FetchedRoute:
    """Fetch and bind an exact sale of the instrument for a settlement asset."""
    if isinstance(amount_raw, bool) or not isinstance(amount_raw, int) or amount_raw <= 0:
        raise LifiQuoteError("amount_raw must be a positive integer in the instrument's own units")
    destination_chain = chain_id if to_chain is None else to_chain
    if destination_chain == chain_id and receive_with.address.lower() == str(token).lower():
        raise LifiQuoteError("the asset being sold cannot also be the asset received")
    observed_at = datetime.now(timezone.utc)
    instrument = PayAsset(address=token, decimals=decimals)
    quote = quote_fn_for(
        destination_chain, receive_with.address, from_address=from_address, api_key=api_key,
        decimals=receive_with.decimals, deadline=deadline, pay_with=instrument, integrator=integrator,
        fee=fee, from_chain=chain_id,
    )
    payload = quote(amount_raw)
    action = payload.get("action")
    for key, expected in (("fromToken", decimals), ("toToken", receive_with.decimals)):
        returned = action.get(key) if isinstance(action, Mapping) else None
        precision = returned.get("decimals") if isinstance(returned, Mapping) else None
        if type(precision) is not int or precision != expected:
            raise LifiQuoteError(f"LI.FI response {key} decimals mismatch; invalid sale evidence")
    binding = parse_quote(payload, observed_at=int(observed_at.timestamp()))
    mismatches = []
    if binding.from_amount != amount_raw:
        mismatches.append(f"amount {binding.from_amount} for a request of {amount_raw}")
    if binding.from_token.lower() != str(token).lower():
        mismatches.append(f"sold {binding.from_token}, not {token}")
    if binding.to_token.lower() != receive_with.address.lower():
        mismatches.append(f"received {binding.to_token}, not {receive_with.address}")
    if binding.from_chain != chain_id or binding.to_chain != destination_chain:
        mismatches.append(f"chains {binding.from_chain}->{binding.to_chain}, not {chain_id}->{destination_chain}")
    if binding.from_decimals != decimals:
        mismatches.append(f"instrument decimals {binding.from_decimals}, not {decimals}")
    if binding.to_decimals != receive_with.decimals:
        mismatches.append(f"receive decimals {binding.to_decimals}, not {receive_with.decimals}")
    if mismatches:
        raise LifiQuoteError("LI.FI answered a different trade than the one requested: " + "; ".join(mismatches))
    return FetchedRoute(payload=payload, binding=binding, fetched_at=observed_at)


def quote_exact(
    chain_id: int,
    token: str,
    *,
    pay_with: PayAsset,
    amount_raw: int,
    decimals: int,
    from_address: str = DEFAULT_FROM_ADDRESS,
    api_key: str | None = None,
    deadline: float | None = None,
) -> RouteBinding:
    """The binding alone, for callers measuring what a holding would buy rather than buying it."""
    return fetch_exact(
        chain_id, token, pay_with=pay_with, amount_raw=amount_raw, decimals=decimals,
        from_address=from_address, api_key=api_key, deadline=deadline,
    ).binding


def measure(
    chain_id: int,
    token: str,
    *,
    sizes_usd: tuple[int, ...] = DEFAULT_SIZES_USD,
    from_address: str = DEFAULT_FROM_ADDRESS,
    api_key: str | None = None,
    decimals: int,
    deadline: float | None = None,
) -> Obtainability:
    """Measure same-chain USDC acquisition capacity at the requested whole-USD sizes."""
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise LifiQuoteError("decimals must be a non-negative integer")
    if any(
        isinstance(size, bool) or not isinstance(size, int) or size <= 0
        for size in sizes_usd
    ):
        raise LifiQuoteError("sizes_usd entries must all be positive whole-dollar integers")
    measured_at = datetime.now(timezone.utc)
    if chain_id not in USDC_BY_CHAIN or chain_id not in USDC_DECIMALS_BY_CHAIN:
        raise LifiQuoteError(f"LI.FI USDC quote asset is not configured for chain {chain_id}")
    raw_sizes = tuple(size * 10 ** USDC_DECIMALS_BY_CHAIN[chain_id] for size in sizes_usd)
    ladder = size_ladder(
        quote_fn_for(
            chain_id,
            token,
            from_address=from_address,
            api_key=api_key,
            decimals=decimals,
            deadline=deadline,
        ),
        sizes=raw_sizes,
        observed_at=int(measured_at.timestamp()),
    )
    # A truncated ladder is not a capacity cliff. Preserve the caller's previous reading
    # through the existing measured=False/stale path instead of publishing a partial maximum.
    if (deadline is not None and time.monotonic() >= deadline) or any(
        rung.error == BUDGET_REFUSAL for rung in ladder
    ):
        return Obtainability(
            chain_id=chain_id, token=token, quote_asset=USDC_BY_CHAIN[chain_id],
            max_quoted_usd=None, impact_bps_at={}, tool_at={}, refusal=BUDGET_REFUSAL,
            measured_at=measured_at,
            # Rungs asked before the budget ran out may still have reached a market, and a
            # refusal is as much an answer as a quote. Counting only bindings left an instrument
            # that had been answered looking unanswered, so rotation kept asking it.
            market_answered=_market_answered(ladder),
        )
    return summarise_ladder(
        ladder,
        chain_id=chain_id,
        token=token,
        quote_asset=USDC_BY_CHAIN[chain_id],
        sizes_usd=sizes_usd,
        measured_at=measured_at,
    )
