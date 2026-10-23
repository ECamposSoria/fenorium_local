/* Deterministic, RPC-free live price observation kernel. */
(function (root, factory) {
  const portfolio = typeof module !== "undefined" && module.exports
    ? require("./portfolio.js")
    : root.FenoriumPortfolio;
  const api = factory(portfolio);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.FenoriumPrices = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (P) {
  "use strict";

  const WORD_BYTES = 32;
  const WORD_BITS = 256n;
  const TWO256 = 1n << WORD_BITS;
  const TWO255 = 1n << 255n;
  const MAX_UINT256 = TWO256 - 1n;
  const MAX_INT256 = TWO255 - 1n;
  const MAX_UINT8 = 255n;
  const MAX_UINT24 = (1n << 24n) - 1n;
  const MAX_UINT32 = (1n << 32n) - 1n;
  const MAX_UINT80 = (1n << 80n) - 1n;
  const MAX_UINT160 = (1n << 160n) - 1n;
  const MIN_INT56 = -(1n << 55n);
  const MAX_INT56 = (1n << 55n) - 1n;
  const MIN_TICK = -887272;
  const MAX_TICK = 887272;
  const Q192 = 1n << 192n;
  const PRICE_SCALE = 100000000n;
  const SOURCE_CHAINLINK = "chainlink-aggregator";
  const SOURCE_UNISWAP = "uniswap-v3-twap";
  const CHAINLINK_METHOD = "chainlink-aggregator/v1";
  const UNISWAP_METHOD = "uniswap-v3-twap/v1";

  function asBigInt(value) {
    if (typeof value === "bigint") return value;
    if (typeof value === "number") return Number.isSafeInteger(value) ? BigInt(value) : null;
    if (typeof value !== "string" || !/^-?[0-9]+$/.test(value)) return null;
    try { return BigInt(value); } catch (_) { return null; }
  }

  function hexBody(value) {
    if (typeof value !== "string" || !/^0x(?:[0-9a-fA-F]{2})*$/.test(value)) return null;
    return value.slice(2).toLowerCase();
  }

  function readWord(body, byteOffset) {
    if (body === null || !Number.isSafeInteger(byteOffset) || byteOffset < 0
      || byteOffset + WORD_BYTES > body.length / 2) return null;
    return BigInt(`0x${body.slice(byteOffset * 2, (byteOffset + WORD_BYTES) * 2)}`);
  }

  function signed256(value) {
    return value >= TWO255 ? value - TWO256 : value;
  }

  function readExactWords(value, count) {
    const body = hexBody(value);
    return body !== null && body.length === count * WORD_BYTES * 2 ? body : null;
  }

  function decodeLatestRoundData(value) {
    const body = readExactWords(value, 5);
    if (body === null) return null;
    const roundId = readWord(body, 0), answer = readWord(body, 32);
    const startedAt = readWord(body, 64), updatedAt = readWord(body, 96);
    const answeredInRound = readWord(body, 128);
    if (roundId === null || answer === null || startedAt === null || updatedAt === null || answeredInRound === null
      || roundId > MAX_UINT80 || answeredInRound > MAX_UINT80) return null;
    return { roundId, answer: signed256(answer), startedAt, updatedAt, answeredInRound };
  }

  function decodeDecimals(value) {
    const body = readExactWords(value, 1), word = body === null ? null : readWord(body, 0);
    return word !== null && word <= MAX_UINT8 ? word : null;
  }

  function bytesToUtf8(body) {
    const bytes = new Uint8Array(body.length / 2);
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = parseInt(body.slice(index * 2, index * 2 + 2), 16);
    try {
      if (typeof TextDecoder !== "undefined") return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      return decodeURIComponent(Array.from(bytes, byte => `%${byte.toString(16).padStart(2, "0")}`).join(""));
    } catch (_) { return null; }
  }

  function dynamicBounds(body, offset, elementBytes = 1) {
    const bodyBytes = body.length / 2;
    if (offset === null || offset % 32n !== 0n || offset > BigInt(Number.MAX_SAFE_INTEGER)) return null;
    const start = Number(offset);
    if (start < 0 || start + 32 > bodyBytes) return null;
    const length = readWord(body, start);
    if (length === null || length > BigInt(Number.MAX_SAFE_INTEGER)
      || !Number.isInteger(elementBytes) || elementBytes <= 0) return null;
    const sizeBig = length * BigInt(elementBytes);
    if (sizeBig > BigInt(Number.MAX_SAFE_INTEGER)) return null;
    const size = Number(sizeBig), dataStart = start + 32, dataEnd = dataStart + size;
    if (dataEnd < dataStart || dataEnd > bodyBytes) return null;
    return { dataStart, dataEnd, length: Number(length) };
  }

  function decodeDescription(value) {
    const body = hexBody(value);
    if (body === null || body.length < 128 || body.length % 64 !== 0) return null;
    const offset = readWord(body, 0), bounds = dynamicBounds(body, offset);
    if (bounds === null) return null;
    const paddedEnd = bounds.dataStart + Math.ceil(bounds.length / WORD_BYTES) * WORD_BYTES;
    if (offset !== 32n || paddedEnd !== body.length / 2) return null;
    for (let index = bounds.dataEnd; index < paddedEnd; index += 1) {
      if (body.slice(index * 2, index * 2 + 2) !== "00") return null;
    }
    return bytesToUtf8(body.slice(bounds.dataStart * 2, bounds.dataEnd * 2));
  }

  function decodeGetPool(value) {
    const body = readExactWords(value, 1), word = body === null ? null : readWord(body, 0);
    if (word === null || word > MAX_UINT160) return null;
    return `0x${word.toString(16).padStart(40, "0")}`;
  }

  function decodeObserve(value) {
    const body = hexBody(value);
    if (body === null || body.length < 128 || body.length % 64 !== 0) return null;
    const ticksOffset = readWord(body, 0), liquidityOffset = readWord(body, 32);
    const ticksBounds = dynamicBounds(body, ticksOffset, WORD_BYTES), liquidityBounds = dynamicBounds(body, liquidityOffset, WORD_BYTES);
    if (ticksBounds === null || liquidityBounds === null) return null;
    const ticksLength = ticksBounds.length, liquidityLength = liquidityBounds.length;
    if (ticksLength !== liquidityLength || ticksLength === 0 || ticksOffset !== 64n
      || liquidityOffset !== BigInt(ticksBounds.dataEnd) || liquidityBounds.dataEnd !== body.length / 2) return null;
    const tickCumulatives = [], secondsPerLiquidityCumulativeX128 = [];
    for (let index = 0; index < ticksLength; index += 1) {
      const tickWord = readWord(body, ticksBounds.dataStart + index * 32);
      const liquidityWord = readWord(body, liquidityBounds.dataStart + index * 32);
      if (tickWord === null || liquidityWord === null) return null;
      const tick = signed256(tickWord);
      if (tick < MIN_INT56 || tick > MAX_INT56 || liquidityWord > MAX_UINT160) return null;
      tickCumulatives.push(tick);
      secondsPerLiquidityCumulativeX128.push(liquidityWord);
    }
    return { tickCumulatives, secondsPerLiquidityCumulativeX128 };
  }

  function address(value) {
    return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value) ? value.toLowerCase() : null;
  }

  function identityFor(asset) {
    if (!P || typeof P.stableAssetIdentity !== "function") return null;
    if (typeof asset?.kind === "string" && asset.kind) {
      const direct = P.stableAssetIdentity(asset);
      if (direct) return direct;
    }
    return typeof P.registeredAssetIdentity === "function" ? P.registeredAssetIdentity(asset) : null;
  }

  function isoFromMilliseconds(milliseconds) {
    if (!Number.isFinite(milliseconds) || Math.abs(milliseconds) > 8640000000000000) return null;
    try { return new Date(milliseconds).toISOString(); } catch (_) { return null; }
  }

  function asOfSeconds(value) {
    const milliseconds = value instanceof Date ? value.getTime() : Date.parse(String(value ?? ""));
    if (!Number.isFinite(milliseconds) || !isoFromMilliseconds(milliseconds)) return null;
    const seconds = BigInt(Math.floor(milliseconds / 1000));
    return { milliseconds, seconds };
  }

  function boundedDecimals(value) {
    const decimals = asBigInt(value);
    return decimals !== null && decimals >= 0n && decimals <= MAX_UINT8 ? decimals : null;
  }

  function boundedHeartbeat(value) {
    const heartbeat = asBigInt(value);
    return heartbeat !== null && heartbeat > 0n && heartbeat <= MAX_UINT32 ? heartbeat : null;
  }

  function chainlinkObservation({ asset, feed, round, decimals, description, asOf } = {}) {
    const identity = identityFor(asset), feedAddress = address(feed?.address);
    const when = asOfSeconds(asOf), places = boundedDecimals(decimals);
    const heartbeat = boundedHeartbeat(feed?.heartbeat_seconds);
    const answer = asBigInt(round?.answer), updatedAt = asBigInt(round?.updatedAt);
    if (!identity || !feedAddress || feedAddress === "0x0000000000000000000000000000000000000000" || !when || places === null || heartbeat === null
      || typeof feed?.expected_description !== "string" || !feed.expected_description || description !== feed.expected_description
      || answer === null || answer <= 0n || answer > MAX_INT256 || updatedAt === null || updatedAt <= 0n || updatedAt > MAX_UINT256) return null;
    const observedSeconds = updatedAt > when.seconds ? when.seconds : updatedAt;
    // The heartbeat IS the staleness gate, and it is a better-informed one than any clock the
    // caller could apply: a push feed undertakes to publish within it, and to publish sooner if
    // the price moves past its deviation threshold. Inside that undertaking the on-chain value
    // is the oracle's current answer, so the observation is as of the read -- the same reasoning
    // the demo book already uses for declared prices, whose terms are good until they expire.
    // Dating it to the round instead would retire every feed with a heartbeat longer than the
    // caller's window: eight of these twelve feeds, including all four on Ethereum, were older
    // than 300 seconds when measured while being exactly what the oracle currently reports.
    // When a feed genuinely stops, the heartbeat catches it, which is the check that refused a
    // 40.1-hour-old oracle here on 2026-09-16. The round's own time stays in the provenance.
    if (when.seconds - observedSeconds > heartbeat) return null;
    const price = answer * PRICE_SCALE / (10n ** places);
    if (price <= 0n) return null;
    const observedAt = isoFromMilliseconds(when.milliseconds);
    // The round's own time, not the clamped one. Clamping exists so the observation is never
    // dated ahead of the request; borrowing that value here would file a round published at
    // 12:00:02 under 12:00:00 and quietly lose the only record of when the oracle last spoke.
    const roundAt = isoFromMilliseconds(Number(updatedAt) * 1000);
    if (!observedAt || !roundAt) return null;
    return {
      schema_version: "fenorium/price-observation/v1",
      identity: String(identity),
      source_id: `${SOURCE_CHAINLINK}:${feedAddress}:round-${roundAt}:heartbeat-${heartbeat.toString()}s`,
      independence_key: SOURCE_CHAINLINK,
      price_usd_fixed: price.toString(),
      observed_at: observedAt,
      method_version: CHAINLINK_METHOD,
    };
  }

  function floorDiv(numerator, denominator) {
    if (denominator <= 0n) return null;
    const quotient = numerator / denominator;
    return numerator < 0n && numerator % denominator !== 0n ? quotient - 1n : quotient;
  }

  function meanTick(oldCumulative, newCumulative, window) {
    const oldValue = asBigInt(oldCumulative), newValue = asBigInt(newCumulative), duration = asBigInt(window);
    if (oldValue === null || newValue === null || duration === null || duration <= 0n
      || oldValue < MIN_INT56 || oldValue > MAX_INT56 || newValue < MIN_INT56 || newValue > MAX_INT56
      || duration > MAX_UINT32) return null;
    const tick = floorDiv(newValue - oldValue, duration);
    return tick !== null && tick >= BigInt(MIN_TICK) && tick <= BigInt(MAX_TICK) ? tick : null;
  }

  /*
   * Ported from Uniswap v3-core contracts/libraries/TickMath.sol.
   * Copyright Uniswap authors; SPDX-License-Identifier: GPL-2.0-or-later.
   * Source: https://github.com/Uniswap/v3-core/blob/d0831dc6b8a318df3872b6d68f6de135c9f3ec29/contracts/libraries/TickMath.sol
   */
  const TICK_RATIOS = Object.freeze([
    "fffcb933bd6fad37aa2d162d1a594001", "fff97272373d413259a46990580e213a",
    "fff2e50f5f656932ef12357cf3c7fdcc", "ffe5caca7e10e4e61c3624eaa0941cd0",
    "ffcb9843d60f6159c9db58835c926644", "ff973b41fa98c081472e6896dfb254c0",
    "ff2ea16466c96a3843ec78b326b52861", "fe5dee046a99a2a811c461f1969c3053",
    "fcbe86c7900a88aedcffc83b479aa3a4", "f987a7253ac413176f2b074cf7815e54",
    "f3392b0822b70005940c7a398e4b70f3", "e7159475a2c29b7443b29c7fa6e889d9",
    "d097f3bdfd2022b8845ad8f792aa5825", "a9f746462d870fdf8a65dc1f90e061e5",
    "70d869a156d2a1b890bb3df62baf32f7", "31be135f97d08fd981231505542fcfa6",
    "9aa508b5b7a84e1c677de54f3e99bc9", "5d6af8dedb81196699c329225ee604",
    "2216e584f5fa1ea926041bedfe98", "48a170391f7dc42444e8fa2",
  ].map(value => BigInt(`0x${value}`)));

  function getSqrtRatioAtTick(tickInput) {
    const tick = asBigInt(tickInput);
    if (tick === null || tick < BigInt(MIN_TICK) || tick > BigInt(MAX_TICK)) return null;
    const positive = tick > 0n;
    let absolute = positive ? tick : -tick;
    let ratio = absolute & 1n ? TICK_RATIOS[0] : 1n << 128n;
    for (let index = 1; index < TICK_RATIOS.length; index += 1) {
      if ((absolute & (1n << BigInt(index))) !== 0n) ratio = (ratio * TICK_RATIOS[index]) >> 128n;
    }
    if (positive) ratio = (TWO256 - 1n) / ratio;
    return (ratio >> 32n) + ((ratio & ((1n << 32n) - 1n)) === 0n ? 0n : 1n);
  }

  function poolTokens(pool, assetAddress, quoteAddress) {
    const poolAddress = address(typeof pool === "string" ? pool : pool?.address || pool?.pool);
    if (!poolAddress || poolAddress === "0x0000000000000000000000000000000000000000") return null;
    const configured0 = address(pool?.token0), configured1 = address(pool?.token1);
    if (typeof pool !== "string" && (Object.hasOwn(pool || {}, "token0") || Object.hasOwn(pool || {}, "token1"))
      && (!configured0 || !configured1)) return null;
    if (configured0 && configured1) {
      return configured0 < configured1 ? { token0: configured0, token1: configured1, poolAddress } : null;
    }
    if (!assetAddress || !quoteAddress || assetAddress === quoteAddress) return null;
    return assetAddress < quoteAddress
      ? { token0: assetAddress, token1: quoteAddress, poolAddress }
      : { token0: quoteAddress, token1: assetAddress, poolAddress };
  }

  /*
   * Harmonic mean liquidity over the window, from Uniswap's own seconds-per-liquidity
   * accumulator: (window << 128) / delta. An initialised pool with no liquidity still answers
   * `observe` -- the oracle divides by max(1, liquidity), so the accumulator races away and this
   * collapses to about 1. That is how an unused 0.01% pool can answer before a liquid 0.05% one
   * and speak for a market it has never traded.
   */
  function timeWeightedLiquidity(first, second, window) {
    const start = asBigInt(first), end = asBigInt(second), duration = asBigInt(window);
    if (start === null || end === null || duration === null || duration <= 0n
      || start < 0n || end < 0n || start > MAX_UINT160 || end > MAX_UINT160) return null;
    const delta = end >= start ? end - start : end + (1n << 160n) - start;   // the accumulator wraps
    if (delta <= 0n) return null;
    return (duration << 128n) / delta;
  }

  function twapObservation({ asset, quote, pool, cumulatives, window, asOf,
                             quoteUsdFixed, quoteSource, minimumLiquidity } = {}) {
    const identity = identityFor(asset), assetAddress = address(asset?.address), quoteAddress = address(quote?.address);
    const tokens = poolTokens(pool, assetAddress, quoteAddress), when = asOfSeconds(asOf);
    const assetDecimals = boundedDecimals(asset?.decimals), quoteDecimals = boundedDecimals(quote?.decimals);
    const duration = asBigInt(window);
    const ticks = Array.isArray(cumulatives)
      ? cumulatives
      : (Array.isArray(cumulatives?.tickCumulatives) ? cumulatives.tickCumulatives : null);
    // The pool answers in its quote token, not in dollars, and `price_usd_fixed` is read as
    // dollars. Publishing the raw ratio asserts a dollar price that is only true while the quote
    // token is worth exactly one. With DAI at $1.005 a USDC/DAI ratio of 0.995 would be taken as
    // USDC's dollar price and understate the position by 50bp. So the quote token's own measured
    // dollar price converts it, and without one there is no observation.
    const quoteUsd = asBigInt(quoteUsdFixed);
    const liquidity = timeWeightedLiquidity(
      cumulatives?.secondsPerLiquidityCumulativeX128?.[0],
      cumulatives?.secondsPerLiquidityCumulativeX128?.[1], duration);
    const floor = minimumLiquidity === undefined || minimumLiquidity === null
      ? 0n : asBigInt(minimumLiquidity);
    if (!identity || !tokens || !when || !assetAddress || !quoteAddress || assetAddress === quoteAddress
      || assetDecimals === null || quoteDecimals === null || duration === null || duration <= 0n || duration > MAX_UINT32
      || !ticks || ticks.length !== 2
      || quoteUsd === null || quoteUsd <= 0n || quoteUsd > MAX_INT256
      // An empty pool is not silent. Uniswap divides by max(1, liquidity), so a pool with none
      // accumulates exactly `window << 128` and this arithmetic returns 1 -- a number, from a
      // market that has never traded, at whatever tick the pool was initialised with.
      || liquidity === null || liquidity <= 1n || floor === null || liquidity < floor
      || typeof quoteSource !== "string" || !quoteSource) return null;
    const tick = meanTick(ticks[0], ticks[1], duration), sqrtRatio = getSqrtRatioAtTick(tick);
    if (tick === null || sqrtRatio === null) return null;
    const square = sqrtRatio * sqrtRatio;
    const assetScale = 10n ** assetDecimals, quoteScale = 10n ** quoteDecimals;
    let numerator, denominator;
    if (tokens.token0 === assetAddress && tokens.token1 === quoteAddress) {
      numerator = square * assetScale * quoteUsd;
      denominator = Q192 * quoteScale;
    } else if (tokens.token0 === quoteAddress && tokens.token1 === assetAddress) {
      numerator = Q192 * assetScale * quoteUsd;
      denominator = square * quoteScale;
    } else return null;
    if (denominator <= 0n) return null;
    const price = numerator / denominator;
    if (price <= 0n) return null;
    return {
      schema_version: "fenorium/price-observation/v1",
      identity: String(identity),
      // The quote asset is named, never assumed. This read is a price in whatever token the
      // pool quotes against, and labelling a DAI-quoted pool as USDC would describe a
      // measurement nobody made.
      source_id: `${SOURCE_UNISWAP}:quote-${quoteAddress}@${quoteUsd.toString()}`
        + `:pool-${tokens.poolAddress}:window-${duration.toString()}:liquidity-${liquidity.toString()}`,
      // The dollar leg came from somewhere, and the evidence records where: this observation
      // leans on the source that told it what a dollar is worth.
      depends_on: [quoteSource],
      independence_key: SOURCE_UNISWAP,
      price_usd_fixed: price.toString(),
      observed_at: isoFromMilliseconds(when.milliseconds),
      method_version: UNISWAP_METHOD,
    };
  }

  function encodeWord(value) { return value.toString(16).padStart(64, "0"); }

  function encodeGetPool(token, quote, fee) {
    const tokenAddress = address(token), quoteAddress = address(quote), feeValue = asBigInt(fee);
    if (!tokenAddress || !quoteAddress || feeValue === null || feeValue < 0n || feeValue > MAX_UINT24) return null;
    return `0x1698ee82${encodeWord(BigInt(tokenAddress))}${encodeWord(BigInt(quoteAddress))}${encodeWord(feeValue)}`;
  }

  function encodeObserve(window) {
    const duration = asBigInt(window);
    if (duration === null || duration <= 0n || duration > MAX_UINT32) return null;
    return `0x883bdbfd${encodeWord(32n)}${encodeWord(2n)}${encodeWord(duration)}${encodeWord(0n)}`;
  }

  return {
    decodeLatestRoundData, decodeDecimals, decodeDescription, decodeGetPool, decodeObserve,
    chainlinkObservation, twapObservation, meanTick, getSqrtRatioAtTick, timeWeightedLiquidity,
    encodeGetPool, encodeObserve,
    SOURCE_CHAINLINK, SOURCE_UNISWAP, CHAINLINK_METHOD, UNISWAP_METHOD,
  };
});
