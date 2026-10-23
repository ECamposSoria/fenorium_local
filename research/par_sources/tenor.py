from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .http import HttpClient, SourceError, require_key, require_list, require_mapping


MORPHO_FIXED_QUERY = """{ morphoFixedMarkets(first: 60) { items { id fixedMarketIdentifier maturity state { totalBorrowedAssetsUsd totalBorrowedAssets totalCollateralAssetsUsd supplyRateApr borrowRateApr withdrawable totalBadDebt lossIndex } tenorMarket { chainId loanToken { symbol address decimals } collaterals { token oracle } } } } }"""


class TenorClient:
    URL = "https://api.tenor.finance/graphql"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def morpho_fixed_markets(self) -> list[dict[str, Any]]:
        payload = require_mapping(self.http.request_json("tenor", "morpho_fixed_markets", "POST", self.URL,
            json_body={"query": MORPHO_FIXED_QUERY}), self.URL)
        if payload.get("errors"):
            raise SourceError(f"{self.URL}: GraphQL errors: {payload['errors']}")
        data = require_mapping(require_key(payload, "data", self.URL), self.URL)
        wrapper = require_mapping(require_key(data, "morphoFixedMarkets", self.URL), self.URL)
        return [require_mapping(item, self.URL) for item in require_list(require_key(wrapper, "items", self.URL), self.URL)]

    def sell_offers(self, chain_id: int) -> list[dict[str, Any]]:
        now = int(datetime.now(UTC).timestamp())
        offers, skip = [], 0
        for page in range(25):
            query = """{ offersList(first: 200, skip: %d, where: { isBuy: false, chainId_in: [%d], maturityGte: %d }) {
            items { id maker buy availableAssets consumed tick price rate maturity start expiry
                enterGate ratifier invalidated reduceOnly maxAssets maxUnits fixedMarketIdentifier
                loanToken { symbol address decimals } }
            pageInfo { countTotal count limit skip }
        } }""" % (skip, chain_id, now)
            name = f"sell_offers_{chain_id}" + (f"_p{page}" if page else "")
            payload = require_mapping(self.http.request_json("tenor", name, "POST", self.URL,
                json_body={"query": query}), self.URL)
            if payload.get("errors"):
                raise SourceError(f"{self.URL}: GraphQL errors: {payload['errors']}")
            data = require_mapping(require_key(payload, "data", self.URL), self.URL)
            wrapper = require_mapping(require_key(data, "offersList", self.URL), self.URL)
            items = [require_mapping(item, self.URL) for item in require_list(
                require_key(wrapper, "items", self.URL), self.URL)]
            info = require_mapping(require_key(wrapper, "pageInfo", self.URL), self.URL)
            total, count, limit, offset = (
                require_key(info, key, self.URL) for key in ("countTotal", "count", "limit", "skip"))
            if (any(type(value) is not int or value < 0 for value in (total, count, limit, offset))
                    or offset != skip or not 0 < limit <= 200 or count != len(items)
                    or count > limit or skip + count > total or (count == 0 and skip < total)):
                raise SourceError(f"{self.URL}: invalid book pagination; not read")
            offers.extend(items)
            skip += count
            if skip >= total:
                return offers
        raise SourceError(f"{self.URL}: book larger than {len(offers)} offers; not read")
