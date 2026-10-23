from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from engine.registry import registry
from research.par_sources.http import cache_fetched_at


DAYS_PER_YEAR = 365.0
DEFAULT_SCALAR_ROOT = 50.0
ROOT = Path(__file__).resolve().parents[1]


def year_fraction(start: datetime, maturity: datetime) -> float:
    """ACT/365 year fraction; both timestamps must be timezone-aware."""
    if start.tzinfo is None or maturity.tzinfo is None:
        raise ValueError("ACT/365 inputs must be timezone-aware")
    return max(0.0, (maturity - start).total_seconds() / (DAYS_PER_YEAR * 86400.0))


def pt_price(face: float, annual_rate: float, years: float) -> float:
    """Zero-coupon PT price under annual effective compounding."""
    if face <= 0 or years < 0 or annual_rate <= -1:
        raise ValueError("face must be positive, years non-negative, and rate greater than -100%")
    return face / ((1.0 + annual_rate) ** years)


def implied_rate(face: float, market_price: float, years: float) -> float:
    if face <= 0 or market_price <= 0 or years <= 0:
        raise ValueError("face, market_price, and years must be positive")
    return (face / market_price) ** (1.0 / years) - 1.0


def target_bid(face: float, target_rate: float, years: float) -> float:
    return pt_price(face, target_rate, years)


@dataclass(frozen=True)
class PendleCurve:
    """Auditable approximation of Pendle V2's logit curve, excluding fees/index conversion.

    `total_asset` and `total_pt` are pool units. `anchor` is the market exchange-rate
    anchor. The implementation mirrors MarketMathCore's logit and rateScalar terms.
    """

    total_pt: float
    total_asset: float
    scalar_root: float
    years_to_maturity: float
    anchor: float

    def __post_init__(self) -> None:
        if min(self.total_pt, self.total_asset, self.scalar_root, self.years_to_maturity) <= 0:
            raise ValueError("Pendle curve reserves, scalar_root, and time must be positive")

    @property
    def rate_scalar(self) -> float:
        return self.scalar_root / self.years_to_maturity

    def exchange_rate(self, pt_out: float = 0.0) -> float:
        numerator = self.total_pt - pt_out
        if numerator <= 0:
            raise ValueError("trade exhausts PT reserve")
        proportion = numerator / (self.total_pt + self.total_asset)
        if not 0 < proportion <= 0.96:
            raise ValueError("post-trade Pendle market proportion is outside (0, 0.96]")
        return self.anchor + math.log(proportion / (1.0 - proportion)) / self.rate_scalar

    def marginal_pt_price(self, pt_out: float = 0.0) -> float:
        exchange = self.exchange_rate(pt_out)
        if exchange < 1.0:
            raise ValueError("Pendle exchange rate fell below one")
        return 1.0 / exchange

    def asset_required(self, pt_out: float, *, steps: int = 256) -> float:
        """Deterministic midpoint integration of the marginal curve."""
        if pt_out < 0 or steps <= 0:
            raise ValueError("pt_out must be non-negative and steps positive")
        if pt_out == 0:
            return 0.0
        width = pt_out / steps
        return sum(self.marginal_pt_price((index + 0.5) * width) * width for index in range(steps))

    def max_pt_out(self) -> float:
        """Largest PT output that preserves the protocol's exchange-rate floor."""
        low, high = 0.0, self.total_pt * (1.0 - 1e-12)
        for _ in range(100):
            midpoint = (low + high) / 2.0
            try:
                valid = self.exchange_rate(midpoint) >= 1.0
            except ValueError:
                valid = False
            if valid:
                low = midpoint
            else:
                high = midpoint
        return low

    def pt_out_for_asset(self, asset_in: float) -> float:
        if asset_in < 0:
            raise ValueError("asset_in must be non-negative")
        if asset_in == 0:
            return 0.0
        low, high = 0.0, self.max_pt_out() * (1.0 - 1e-12)
        if self.asset_required(high) < asset_in:
            raise ValueError("trade exceeds executable curve capacity")
        for _ in range(80):
            midpoint = (low + high) / 2.0
            if self.asset_required(midpoint) < asset_in:
                low = midpoint
            else:
                high = midpoint
        return (low + high) / 2.0

    def price_impact(self, asset_in: float) -> float:
        pt_out = self.pt_out_for_asset(asset_in)
        average_price = asset_in / pt_out
        return average_price / self.marginal_pt_price(0.0) - 1.0


def curve_from_spot(*, liquidity_usd: float, spot_price: float, scalar_root: float,
                    years_to_maturity: float) -> PendleCurve:
    """Seed the curve from reported TVL using an explicit 50/50 value split assumption."""
    if liquidity_usd <= 0 or not 0 < spot_price <= 1.0:
        raise ValueError("liquidity must be positive and PT spot price in (0, 1]")
    total_pt = liquidity_usd / (2.0 * spot_price)
    total_asset = liquidity_usd / 2.0
    rate_scalar = scalar_root / years_to_maturity
    proportion = total_pt / (total_pt + total_asset)
    anchor = (1.0 / spot_price) - math.log(proportion / (1.0 - proportion)) / rate_scalar
    return PendleCurve(total_pt, total_asset, scalar_root, years_to_maturity, anchor)


@dataclass(frozen=True)
class QuoteResidual:
    chain: str
    instrument: str
    maturity: str
    size_usd: float
    quote_impact_bp: float
    model_impact_bp: float
    residual_bp: float


def validate_cached_convert_quotes(cache_dir: Path = ROOT / "research" / "data" / "latest",
                                   *, scalar_root: float = DEFAULT_SCALAR_ROOT) -> list[QuoteResidual]:
    """Compare the transparent curve approximation with cached executable quote impact.

    The active-market endpoint does not expose on-chain reserves, scalarRoot, fees, or SY
    index. The model therefore uses fetched liquidity, the fetched implied APY spot price,
    and a disclosed 50/50 value split. Residuals are evidence, not calibration targets.
    """
    markets: dict[tuple[int, str], tuple[str, str, datetime, float, float]] = {}
    market_paths = list(cache_dir.glob("pendle_markets_active_*.json"))
    if not market_paths:
        return []
    as_of = max(cache_fetched_at(path) for path in market_paths)
    for path in market_paths:
        try:
            chain_id = int(path.stem.rsplit("_", 1)[-1])
        except ValueError as exc:
            raise ValueError(f"{path}: cannot derive chain id from cache name") from exc
        payload = json.loads(path.read_text(encoding="utf-8"))
        for market in payload.get("markets", []):
            details = market.get("details", {})
            pt = str(market.get("pt", "")).split("-")[-1].lower()
            try:
                maturity = datetime.fromisoformat(str(market["expiry"]).replace("Z", "+00:00"))
                apy = float(details["impliedApy"])
                liquidity = float(details["liquidity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}: active market lacks expiry/details.impliedApy/details.liquidity") from exc
            known = registry().get(chain_id)
            chain = known.label if known is not None else str(chain_id)
            markets[(chain_id, pt)] = (chain, str(market.get("name", pt)), maturity, apy, liquidity)
    rows: list[QuoteResidual] = []
    pattern = re.compile(r"pendle_convert_(\d+)_(0x[a-f0-9]+)_(\d+)usd\.json$")
    for path in sorted(cache_dir.glob("pendle_convert_*usd.json")):
        match = pattern.match(path.name)
        key = (int(match.group(1)), match.group(2)) if match else None
        if not match or key not in markets:
            continue
        chain, instrument, maturity, apy, liquidity = markets[key]
        years = year_fraction(as_of, maturity)
        if years <= 0:
            continue
        size = float(match.group(3))
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            quote_impact = abs(float(payload["routes"][0]["data"]["priceImpact"]))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: Convert response lost routes[0].data.priceImpact") from exc
        spot = pt_price(1.0, apy, years)
        curve = curve_from_spot(liquidity_usd=liquidity, spot_price=spot,
                                scalar_root=scalar_root, years_to_maturity=years)
        model_impact = curve.price_impact(size)
        rows.append(QuoteResidual(chain, instrument, maturity.date().isoformat(), size,
                                  quote_impact * 10_000, model_impact * 10_000,
                                  (model_impact - quote_impact) * 10_000))
    return rows


def render_quote_table(rows: Iterable[QuoteResidual]) -> str:
    lines = ["chain | instrument | maturity | size_usd | quote_bp | model_bp | residual_bp",
             "--- | --- | --- | ---: | ---: | ---: | ---:"]
    lines.extend(f"{r.chain} | {r.instrument} | {r.maturity} | {r.size_usd:.0f} | {r.quote_impact_bp:.2f} | "
                 f"{r.model_impact_bp:.2f} | {r.residual_bp:+.2f}" for r in rows)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Par's Pendle curve against cached Convert quotes")
    parser.add_argument("--scalar-root", type=float, default=DEFAULT_SCALAR_ROOT)
    args = parser.parse_args()
    rows = validate_cached_convert_quotes(scalar_root=args.scalar_root)
    print("Convention: ACT/365; annual effective zero-coupon compounding.")
    print(render_quote_table(rows))
    if rows:
        print(f"max_abs_residual_bp={max(abs(row.residual_bp) for row in rows):.2f}")
    else:
        print("COULD NOT VERIFY — no cached successful Convert quotes matched active markets")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
