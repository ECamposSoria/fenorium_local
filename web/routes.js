/* Deterministic quote-display kernel: no transport, DOM, or transaction material. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.FenoriumRoutes = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const address = value => typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value);
  const decimals = value => Number.isSafeInteger(value) && value >= 0 && value <= 255;
  const amount = value => typeof value === "string" && /^[0-9]+$/.test(value) ? BigInt(value) : null;
  const chain = value => Number.isSafeInteger(value) && value > 0;
  const VALIDITY_MS = 120000;

  function buildRequest(chainId, instrument, pay, rawAmount, fromAddress) {
    const fromChain = pay?.chain_id === undefined ? chainId : pay.chain_id;
    const raw = typeof rawAmount === "bigint" ? rawAmount.toString() : rawAmount;
    if (!chain(chainId) || !chain(fromChain) || !address(instrument?.address) || !decimals(instrument?.decimals)
      || !address(pay?.address) || !decimals(pay?.decimals) || !address(fromAddress)
      || amount(raw) === null || amount(raw) <= 0n
      || (fromChain === chainId && pay.address.toLowerCase() === instrument.address.toLowerCase())) return null;
    return {
      query: {fromChain: String(fromChain), toChain: String(chainId), fromToken: pay.address,
        toToken: instrument.address, fromAmount: BigInt(raw).toString(), fromAddress,
        // The server states the same tolerance on every LI.FI request (research/par_sources/lifi_quotes.py).
        slippage: "0.005"},
      from_decimals: pay.decimals, to_decimals: instrument.decimals,
    };
  }

  function bindQuote(request, payload, observedAt, now) {
    try {
      const q = request.query, a = payload.action, e = payload.estimate;
      if (!Number.isSafeInteger(observedAt) || !Number.isSafeInteger(now)
        || observedAt < 0 || now < observedAt || now >= observedAt + VALIDITY_MS) return null;
      // parse_quote binds estimate.fromAmount. Check action.fromAmount too when supplied:
      // contradictory copies are not evidence for the requested trade.
      const from = amount(e.fromAmount), minimum = amount(e.toAmountMin), expected = amount(e.toAmount);
      if (from === null || from <= 0n || minimum === null || expected === null || minimum > expected
        || from !== amount(q.fromAmount)
        || (a.fromAmount !== undefined && amount(a.fromAmount) !== from)
        || !address(a.fromToken.address) || !address(a.toToken.address)
        || a.fromToken.address.toLowerCase() !== q.fromToken.toLowerCase()
        || a.toToken.address.toLowerCase() !== q.toToken.toLowerCase()
        || !chain(a.fromChainId) || !chain(a.toChainId)
        || String(a.fromChainId) !== q.fromChain || String(a.toChainId) !== q.toChain
        || !decimals(a.fromToken.decimals) || !decimals(a.toToken.decimals)
        || a.fromToken.decimals !== request.from_decimals || a.toToken.decimals !== request.to_decimals) return null;
      return {from_chain: a.fromChainId, to_chain: a.toChainId,
        from_token: q.fromToken, to_token: q.toToken, from_amount: from.toString(),
        to_amount_min: minimum.toString(), from_decimals: request.from_decimals,
        to_decimals: request.to_decimals, observed_at: observedAt, expires_at: observedAt + VALIDITY_MS};
    } catch (_) { return null; }
  }

  function formatAmount(raw, places) {
    const value = amount(raw);
    if (value === null || !decimals(places)) return null;
    const scale = 10n ** BigInt(places);
    const fraction = places ? (value % scale).toString().padStart(places, "0").replace(/0+$/, "") : "";
    return (value / scale).toString() + (fraction ? "." + fraction : "");
  }

  function displayQuote(binding, now) {
    if (!binding || !Number.isSafeInteger(now) || now < binding.observed_at || now >= binding.expires_at) return null;
    const pay = formatAmount(binding.from_amount, binding.from_decimals);
    const minimum = formatAmount(binding.to_amount_min, binding.to_decimals);
    return pay === null || minimum === null ? null : {pay_amount: pay, minimum_amount: minimum};
  }
  // What /v1/execution-routes says, in words. An unknown code is shown as itself rather than
  // hidden: a reason the client cannot name is still a reason the purchase did not proceed.
  const ROUTE_STATUS = Object.freeze({
    ROUTE_READY: "Route ready",
    DEPOSIT_READY: "Deposit ready",
    ANALYTICAL_ONLY: "Quoted, but not executable as it stands",
    REJECTED: "Fenorium will not route this purchase",
    ROUTE_UNAVAILABLE: "No route could be obtained right now",
  });
  const REASON_TEXT = Object.freeze({
    SIGNATURE_UNVERIFIED: "The ratifier did not accept this signature; nothing was published.",
    ORDER_CANCELED: "This order's root is cancelled on chain.",
    ORDER_RECORD_INVALID: "The stored order no longer verifies; prepare a new one.",
    ONCHAIN_UNAVAILABLE: "The chain could not be asked; try again.",
    NO_RATE: "Set the rate to lend at.",
    MARKET_KEY_MISMATCH: "Tenor's market terms could not be verified.",
    UNSUPPORTED_VENUE: "Lending orders are not available for this venue.",
    MEMPOOL_POLICY: "No public order book would show this order.",
    POLICY_UNAVAILABLE: "The public order book could not check this order; try again.",
    ORDERS_UNAVAILABLE: "Lending orders are not enabled on this deployment.",
    EXPIRED: "This order has expired; prepare a new one.",
    ORDER_NOT_FOUND: "This order could not be found for this wallet and chain.",
    INSUFFICIENT_BALANCE: "This wallet does not hold enough of it for the selected size.",
    PREVIEW_DEADLINE: "Not quoted in time; the quoted options are shown.",
    PREVIEW_EXPIRED: "This preview had already expired when it arrived.",
    NO_BID: "Its Fenorium Score is below 60.",
    NO_QUOTE: "The engine could not price this instrument.",
    TRANSFER_GATE_UNKNOWN: "Whether this token can be transferred to you was never established.",
    TRANSFER_GATE_RESTRICTED: "This token only moves between approved holders.",
    PAY_ASSET_IS_THE_INSTRUMENT: "A token cannot be paid for with itself.",
    NOT_AN_INSTRUMENT: "This is a settlement asset, used to pay with, not an instrument.",
    INSTRUMENT_NOT_IN_UNIVERSE: "This instrument is not in Fenorium's universe.",
    INSTRUMENT_AMBIGUOUS: "More than one universe entry claims this token.",
    INSTRUMENT_DECIMALS_UNKNOWN: "The instrument's decimals are not known.",
    INSTRUMENT_IS_TESTNET: "This is a testnet mock.",
    UNIVERSE_UNAVAILABLE: "The instrument universe could not be read.",
    UNIVERSE_EXPIRED: "The instrument universe is more than 48 hours old.",
    ROUTER_UNAVAILABLE: "This deployment could not obtain a route.",
    ROUTER_BUSY: "Another route is being fetched; try again in a moment.",
    NO_ROUTE_AT_SIZE: "No market will fill this amount.",
    QUOTE_EXPIRED: "The route expired.",
    MISSING_COST: "The route did not state its gas cost.",
    VALUATION_CONFLICT: "The route's valuation of this instrument disagrees with Fenorium's; nothing is offered for signing.",
    UNGUARANTEED_OUTPUT: "The route guarantees no minimum output.",
    MISSING_EXECUTION_QUOTE: "The route came without a transaction to sign.",
    MISSING_APPROVAL_TARGET: "The route did not name the contract to approve.",
    APPROVAL_TARGET_MISMATCH: "The route asked to approve a different contract from the one it calls.",
    LEGAL_REVIEW_REQUIRED: "Execution awaits legal review.",
    SECURITY_AUDIT_REQUIRED: "Execution awaits a security audit.",
    DESTINATION_FAILURE_EXPERIMENT: "Cross-chain purchases await the LI.FI destination-failure experiment.",
    DESTINATION_NOT_WALLET: "The route would deliver to an address other than your wallet.",
    PAY_CHAIN_UNSUPPORTED: "Fenorium does not route payments from that chain.",
    SALE_MATURED_REDEEM: "This principal token has matured; redeem it rather than selling it.",
    SALE_NOT_A_TOKEN: "This position is not a token; it cannot be sold through a route.",
    SALE_COST_NEEDS_ACCEPTANCE: "This sale costs more than Fenorium's usual limit; it is offered only after you accept the cost.",
    PURCHASE_COST_NEEDS_ACCEPTANCE: "This purchase costs more than Fenorium's usual limit; accept the cost only while its yield to maturity stays positive.",
    ROUTE_COST_TOO_HIGH: "The route cost is above the permitted limit.",
    NET_YIELD_NOT_POSITIVE: "This route costs more than the instrument earns to maturity.",
    MARKET_NOT_PUBLIC: "Fenorium only places lending orders in markets shown on Morpho's public order book; this market isn't, so borrowers would not see your order.",
    RATIFIER_AUTHORIZATION_REQUIRED: "Morpho needs a one-time permission before your orders can be filled.",
    RATIFIER_NOT_AUTHORIZED: "This order cannot be filled until the maker authorizes Morpho's order checker.",
    BALANCE_UNAVAILABLE: "The balance could not be read; try again.",
    INSUFFICIENT_NATIVE_VALUE: "This wallet does not hold the native value this transaction sends.",
    INSUFFICIENT_GAS: "This wallet does not hold enough native coin to send this transaction.",
    LOW_GAS_BALANCE: "This wallet may not hold enough native coin for the wallet's gas reservation.",
    GAS_CHECK_UNAVAILABLE: "The native balance and gas requirement could not be checked; the wallet will check before signing.",
  });
  const code = value => typeof value === "string" && /^[A-Z][A-Z0-9_]{0,63}$/.test(value);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const plainUsd = value => typeof value === "string" && /^[0-9]{1,18}(?:\.[0-9]{1,18})?$/.test(value);

  // Raw native amounts stay decimal strings across JSON. Bind the check to the chain of the
  // transaction the wallet would sign so a cross-chain quote cannot check its destination.
  function gasCheckView(data, transactionChainId) {
    if (!Object.prototype.hasOwnProperty.call(data, "gas_check")) return undefined;
    if (data.gas_check === null) return null;
    const gas = data.gas_check;
    if (!object(gas) || !chain(gas.chain_id) || gas.chain_id !== transactionChainId
      || typeof gas.native_symbol !== "string" || !gas.native_symbol.trim() || gas.native_symbol.length > 32
      || !["wallet", "external"].includes(gas.payer)
      || !Number.isSafeInteger(gas.approvals_counted) || gas.approvals_counted < 0 || gas.approvals_counted > 2
      || !decimals(gas.native_decimals)
      || !["native_balance_raw_amount", "required_raw_amount", "reservation_raw_amount"].every(key => amount(gas[key]) !== null)
      || !Number.isSafeInteger(gas.gas_units) || gas.gas_units <= 0
      || !Number.isSafeInteger(gas.base_fee_wei) || gas.base_fee_wei < 0
      || !Number.isSafeInteger(gas.priority_fee_wei) || gas.priority_fee_wei < 0
      || !["router gas limit", "estimate", "default"].includes(gas.source)) return undefined;
    return {payer: gas.payer, approvals_counted: gas.approvals_counted,
      chain_id: gas.chain_id, native_symbol: gas.native_symbol, native_decimals: gas.native_decimals,
      native_balance_raw_amount: gas.native_balance_raw_amount, required_raw_amount: gas.required_raw_amount,
      reservation_raw_amount: gas.reservation_raw_amount, gas_units: gas.gas_units,
      base_fee_wei: gas.base_fee_wei, priority_fee_wei: gas.priority_fee_wei, source: gas.source};
  }

  function transactionChain(data, expected) {
    const tx = data.transaction_request || data.unsigned_transaction_request;
    return tx?.chainId ?? data.pay_chain_id ?? data.chain_id ?? expected;
  }

  function gasWarning(data, transactionChainId) {
    const gas = gasCheckView(data, transactionChainId);
    if (gas === undefined) return undefined;
    if (!gas) return null;
    const balance = amount(gas.native_balance_raw_amount), required = amount(gas.required_raw_amount);
    const reservation = amount(gas.reservation_raw_amount);
    const unavailable = data.reason_codes.includes("GAS_CHECK_UNAVAILABLE");
    const walletPaid = gas.payer === "wallet";
    const tx = data.transaction_request || data.unsigned_transaction_request;
    const rawValue = tx?.value ?? "0";
    if (typeof rawValue !== "string" || !/^(?:[0-9]+|0x[0-9a-fA-F]+)$/.test(rawValue)) return undefined;
    const value = BigInt(rawValue);
    const nativeInsufficient = !unavailable && !walletPaid && balance < value;
    const insufficient = !unavailable && walletPaid && balance < required;
    const low = !unavailable && walletPaid && !insufficient && balance < reservation;
    if (reservation < required || data.reason_codes.includes("INSUFFICIENT_GAS") !== insufficient
      || data.reason_codes.includes("INSUFFICIENT_NATIVE_VALUE") !== nativeInsufficient
      || data.reason_codes.includes("LOW_GAS_BALANCE") !== low
      || unavailable && !walletPaid) return undefined;
    return {gas_check: {...gas, native_value_raw_amount: value.toString()}, insufficient, nativeInsufficient, low};
  }

  // A decimal the user typed, as raw units of a token with `places` decimals. Never through a
  // float: 1.1 USDC is 1100000, not 1100000.0000000001 rounded by whoever reads it next.
  function parseAmount(text, places) {
    if (typeof text !== "string" || !decimals(places)) return null;
    const match = /^([0-9]{1,78})(?:\.([0-9]+))?$/.exec(text.trim());
    if (!match) return null;
    const fraction = match[2] || "";
    if (fraction.length > places) return null;
    const raw = BigInt(match[1] + fraction.padEnd(places, "0"));
    return raw > 0n ? raw.toString() : null;
  }

  // Validate the server's answer before a word of it is shown. A response whose parts disagree
  // -- authorised with nothing to sign, ready with reasons that forbid it -- is refused whole.
  function executionRouteView(data, now) {
    if (!object(data) || !Object.prototype.hasOwnProperty.call(ROUTE_STATUS, data.status)
      || !["SERVER_FETCHED", "SERVER_BUILT"].includes(data.route_provenance) || !Array.isArray(data.reason_codes)
      || !data.reason_codes.every(code) || typeof data.broadcast_enabled !== "boolean"
      || !Number.isSafeInteger(now) || (data.cost_accepted_bps != null
        && (!Number.isSafeInteger(data.cost_accepted_bps) || data.cost_accepted_bps < 0 || data.cost_accepted_bps > 10000))) return null;
    // Null when the server could not resolve what was asked about; only a stated chain is checked.
    if (data.pay_chain_id != null && !chain(data.pay_chain_id)) return null;
    const signable = data.broadcast_enabled === true;
    const ready = ["ROUTE_READY", "DEPOSIT_READY"].includes(data.status);
    if (signable && (!ready || !object(data.transaction_request))) return null;
    if (!signable && (data.transaction_request !== null || data.approval_request != null
      || data.approval_reset_request != null)) return null;
    const gas = gasWarning(data, transactionChain(data));
    if (gas === undefined || (gas?.insufficient || gas?.nativeInsufficient) && signable) return null;
    if (["INSUFFICIENT_GAS", "INSUFFICIENT_NATIVE_VALUE", "LOW_GAS_BALANCE"].some(code => data.reason_codes.includes(code)) && !gas) return null;
    if (["INSUFFICIENT_GAS", "INSUFFICIENT_NATIVE_VALUE"].some(code => data.reason_codes.includes(code)) && (signable || data.transaction_request !== null
      || data.approval_request != null || data.approval_reset_request != null)) return null;
    if (data.approval_reset_request != null
      && (!object(data.approval_reset_request) || !object(data.approval_request))) return null;
    const usdMicros = value => value === null ? null
      : typeof value === "string" && /^[0-9]+$/.test(value) ? formatAmount(value, 6)
      : Number.isSafeInteger(value) && value >= 0 ? formatAmount(String(value), 6) : undefined;
    const totals = [data.lifi_fee_usd_micros, data.integrator_fee_usd_micros, data.fenorium_fee_usd_micros].map(usdMicros);
    if (totals.some(value => value === undefined)) return null;
    if (data.fees !== null && !Array.isArray(data.fees)) return null;
    // A route that would not state its fees is a legitimate answer -- the server says MISSING_COST
    // and offers nothing to sign -- so only a ready route must carry the list.
    if (data.fees === null && (ready || signable)) return null;
    const fees = [];
    let sizeCheck = null;
    if (data.size_check != null) {
      const s = data.size_check;
      const bp = v => Number.isSafeInteger(v);
      const micros = v => typeof v === "string" && /^[0-9]+$/.test(v);
      if (!object(s) || !(s.route_cost_bps === null || bp(s.route_cost_bps))) return null;
      sizeCheck = {...s};
      for (const key of ["yield_to_maturity_bps", "net_yield_to_maturity_bps"]) {
        if (s[key] != null && !bp(s[key])) return null;
      }
      for (const key of ["outlay_mark_usd_micros", "guaranteed_output_usd_micros"]) {
        if (s[key] != null && !micros(s[key])) return null;
      }
      if (s.yield_to_maturity_bps == null && s.net_yield_to_maturity_bps != null
        || s.yield_to_maturity_bps != null && s.route_cost_bps === null && s.net_yield_to_maturity_bps != null
        || s.yield_to_maturity_bps != null && s.route_cost_bps !== null
          && s.net_yield_to_maturity_bps !== Math.floor(((10000 - s.route_cost_bps) * (10000 + s.yield_to_maturity_bps)
            - 1e8) / 10000)) return null;
    }
    for (const fee of data.fees || []) {
      if (!object(fee) || typeof fee.name !== "string" || !fee.name.trim() || fee.name.length > 64
        || (fee.token_chain_id != null && !chain(fee.token_chain_id))
        || !address(fee.token) || !decimals(fee.token_decimals) || amount(fee.amount) === null
        || (fee.amount_usd !== null && !plainUsd(fee.amount_usd)) || typeof fee.included !== "boolean"
        || amount(fee.lifi_fee) === null || amount(fee.integrator_fee) === null
        || amount(fee.lifi_fee) + amount(fee.integrator_fee) !== amount(fee.amount)) return null;
      fees.push({name: fee.name, token: fee.token, amount: formatAmount(fee.amount, fee.token_decimals),
        amount_usd: fee.amount_usd, included: fee.included, token_chain_id: fee.token_chain_id ?? null,
        lifi_fee: formatAmount(fee.lifi_fee, fee.token_decimals),
        integrator_fee: formatAmount(fee.integrator_fee, fee.token_decimals)});
    }
    let route = null;
    if (data.route !== null) {
      const r = data.route;
      if (!object(r) || !chain(r.from_chain) || !chain(r.to_chain) || !decimals(r.from_decimals) || !decimals(r.to_decimals)
        || amount(r.from_amount) === null || amount(r.to_amount_min) === null
        || !Number.isSafeInteger(r.expires_at) || typeof r.tool !== "string"
        || (r.gas_cost_usd !== null && !plainUsd(r.gas_cost_usd))) return null;
      if (data.pay_chain_id != null && data.pay_chain_id !== r.from_chain) return null;
      if (signable && r.from_chain !== r.to_chain
        && (data.pay_chain_id !== r.from_chain || [data.transaction_request,
          data.approval_request, data.approval_reset_request].some(tx => tx != null && tx.chainId !== r.from_chain))) return null;
      const pay = formatAmount(r.from_amount, r.from_decimals);
      const minimum = formatAmount(r.to_amount_min, r.to_decimals);
      if (pay === null || minimum === null) return null;
      route = {cross_chain: r.from_chain !== r.to_chain, from_chain: r.from_chain, to_chain: r.to_chain,
        execution_duration_s: Number.isSafeInteger(r.execution_duration_s) && r.execution_duration_s >= 0 ? r.execution_duration_s : null,
        pay_amount: pay, minimum_amount: minimum, gas_cost_usd: r.gas_cost_usd,
        tool: r.tool, expires_in_seconds: Math.max(0, r.expires_at - Math.floor(now / 1000))};
    }
    if (ready && route === null) return null;
    const view = {
      cross_chain: route?.cross_chain === true, from_chain: route?.from_chain ?? null,
      to_chain: route?.to_chain ?? null, execution_duration_s: route?.execution_duration_s ?? null,
      fees: data.fees === null ? null : fees,
      lifi_fee_usd: totals[0], integrator_fee_usd: totals[1], fenorium_fee_usd: totals[2],
      status: data.status,
      status_text: ROUTE_STATUS[data.status],
      reasons: data.reason_codes.map(item => ({code: item, text: REASON_TEXT[item] || item})),
      gas_check: gas?.gas_check ?? null,
      gas_warning: gas?.nativeInsufficient ? "native_value" : gas?.low ? "low" : gas?.insufficient ? "insufficient" : null,
      route,
      size_check: sizeCheck,
      cost_accepted_bps: data.cost_accepted_bps ?? null,
      can_sign: signable && route !== null && route.expires_in_seconds > 0,
    };
    if (data.reason_codes.includes("SALE_COST_NEEDS_ACCEPTANCE") && data.direction !== "SELL") return null;
    if (data.reason_codes.includes("PURCHASE_COST_NEEDS_ACCEPTANCE")) {
      const acceptance = data.cost_acceptance, size = data.size_check;
      const micros = value => typeof value === "string" && /^[0-9]+$/.test(value) ? BigInt(value) : null;
      const outlay = micros(acceptance?.outlay_mark_usd_micros), output = micros(acceptance?.guaranteed_output_usd_micros);
      if (data.status !== "ANALYTICAL_ONLY" || data.reason_codes.filter(item => !["INSUFFICIENT_GAS", "INSUFFICIENT_NATIVE_VALUE", "LOW_GAS_BALANCE", "GAS_CHECK_UNAVAILABLE"].includes(item)).length !== 1 || !object(acceptance) || !object(size)
        || !Number.isSafeInteger(acceptance.route_cost_bps) || acceptance.route_cost_bps <= 300
        || acceptance.max_acceptable_bps !== 1000 || acceptance.route_cost_bps > 1000
        || acceptance.route_cost_bps !== size.route_cost_bps
        || !Number.isSafeInteger(acceptance.yield_to_maturity_bps) || acceptance.yield_to_maturity_bps < 0
        || !Number.isSafeInteger(acceptance.net_yield_to_maturity_bps) || acceptance.net_yield_to_maturity_bps <= 0
        || acceptance.yield_to_maturity_bps !== size.yield_to_maturity_bps
        || acceptance.net_yield_to_maturity_bps !== size.net_yield_to_maturity_bps
        || outlay === null || outlay <= 0n || output === null || output <= 0n
        || outlay !== micros(size.outlay_mark_usd_micros)
        || output !== micros(size.guaranteed_output_usd_micros)
        || !route || route.expires_in_seconds <= 0) return null;
      view.cost_acceptance = acceptance;
    } else if (data.cost_acceptance != null && !data.reason_codes.includes("SALE_COST_NEEDS_ACCEPTANCE")) return null;
    if (data.cost_acceptance != null && !data.reason_codes.includes("PURCHASE_COST_NEEDS_ACCEPTANCE")
      && !data.reason_codes.includes("SALE_COST_NEEDS_ACCEPTANCE")) return null;
    if (data.cost_accepted_bps != null && (!ready || !view.size_check
      || view.size_check.route_cost_bps == null || data.cost_accepted_bps < view.size_check.route_cost_bps
      || data.direction !== "SELL" && view.size_check.cost_accepted_bps !== data.cost_accepted_bps)) return null;
    if (data.direction !== "SELL" && data.cost_accepted_bps == null
      && view.size_check?.cost_accepted_bps != null) return null;
    if (data.direction !== "SELL" && ready && view.size_check?.route_cost_bps > 300
      && data.cost_accepted_bps == null) return null;
    if (data.direction !== "SELL" && data.cost_accepted_bps != null
      && (view.size_check.route_cost_bps <= 300 || view.size_check.route_cost_bps > 1000
        || !Number.isSafeInteger(view.size_check.net_yield_to_maturity_bps)
        || view.size_check.net_yield_to_maturity_bps <= 0)) return null;
    return view;
  }

  // Sale responses reverse the route roles: the held instrument is the input and the
  // settlement asset is the guaranteed output. Keep the shared shape and freshness checks,
  // then bind the response to the selected wallet, instrument and receive asset.
  function saleRouteView(data, expected, now) {
    if (!object(expected) || !address(expected.wallet) || !address(expected.instrument_address)
      || !address(expected.receive_address) || !chain(expected.instrument_chain_id)
      || !chain(expected.receive_chain_id) || !decimals(expected.instrument_decimals)
      || !decimals(expected.receive_decimals) || amount(expected.amount_raw) === null) return null;
    const view = executionRouteView(data, now), r = data?.route;
    if (!view || data.direction !== "SELL"
      || (r === null && (view.can_sign || !["REJECTED", "ROUTE_UNAVAILABLE", "ANALYTICAL_ONLY"].includes(data.status)))) return null;
    if (!sameAddress(data.wallet, expected.wallet) || !sameAddress(data.pay_address, expected.instrument_address)
      || data.pay_chain_id !== expected.instrument_chain_id
      || !sameAddress(data.receive_address, expected.receive_address)
      || data.receive_chain_id !== expected.receive_chain_id
      || (data.instrument != null && (!object(data.instrument)
        || !sameAddress(data.instrument.address, expected.instrument_address)
        || data.instrument.chain_id !== expected.instrument_chain_id
        || data.instrument.decimals !== expected.instrument_decimals))
      || amount(data.amount_raw) !== amount(expected.amount_raw)) return null;
    if (r === null) return view;
    const recipient = data.recipient ?? r.to_address ?? data.to_address;
    if (!object(r) || !sameAddress(r.from_token, expected.instrument_address)
      || !sameAddress(r.to_token, expected.receive_address)
      || r.from_chain !== expected.instrument_chain_id || r.to_chain !== expected.receive_chain_id
      || r.from_decimals !== expected.instrument_decimals || r.to_decimals !== expected.receive_decimals
      || amount(r.from_amount) !== amount(expected.amount_raw)
      || !sameAddress(recipient, expected.wallet)) return null;
    if (data.reason_codes.includes("SALE_COST_NEEDS_ACCEPTANCE")) {
      const acceptance = data.cost_acceptance, size = data.size_check;
      // USD micros travel as decimal strings (json_bytes), like every raw amount.
      const outlay = amount(acceptance?.outlay_mark_usd_micros), proceeds = amount(acceptance?.proceeds_usd_micros);
      if (data.status !== "ANALYTICAL_ONLY" || data.reason_codes.filter(item => !["INSUFFICIENT_GAS", "INSUFFICIENT_NATIVE_VALUE", "LOW_GAS_BALANCE", "GAS_CHECK_UNAVAILABLE"].includes(item)).length !== 1
        || !object(acceptance) || !object(size)
        || !Number.isSafeInteger(acceptance.route_cost_bps) || acceptance.route_cost_bps <= 300
        || acceptance.max_acceptable_bps !== 1000 || acceptance.route_cost_bps > acceptance.max_acceptable_bps
        || acceptance.route_cost_bps !== size.route_cost_bps
        || outlay === null || outlay <= 0n || proceeds === null || proceeds <= 0n
        || outlay !== amount(size.outlay_mark_usd_micros)
        || proceeds !== amount(size.proceeds_usd_micros)) return null;
      view.cost_acceptance = acceptance;
    }
    return view;
  }

  const EXIT_ACTIONS = Object.freeze({
    EXACTLY_EXIT: new Set(["EXACTLY_WITHDRAW_AT_MATURITY", "EXACTLY_WITHDRAW_EARLY"]),
    TENOR_EXIT: new Set(["TENOR_WITHDRAW", "TENOR_SELL"]),
  });
  const EXIT_STATUS = Object.freeze({
    EXIT_READY: "Exit ready", ANALYTICAL_ONLY: "Exit needs cost acceptance",
    REJECTED: "Exit unavailable at this price", EXIT_UNAVAILABLE: "Exit could not be checked",
  });
  const EXIT_REASONS = Object.freeze({
    UNKNOWN_MARKET: "This market is not known to Fenorium.", NO_POSITION: "This wallet has no position here.",
    INSUFFICIENT_PROTOCOL_LIQUIDITY: "Exactly cannot fund an early withdrawal right now.",
    DISAGREEMENT: "The withdrawal payout changed below the required floor.",
    NO_TAKEABLE_BID: "No verified lender bid can buy this credit right now.",
    NO_EXECUTABLE_BID: "No verified lender bid can buy this credit right now.",
    AWAITING_REPAYMENT: "No repaid credit is available yet; wait for market repayments.",
    EXIT_COST_TOO_HIGH: "The sale cost is above the maximum Fenorium allows.",
    MARK_UNAVAILABLE: "Fenorium could not value this exit against a positive market mark.",
    NO_REDEMPTION_OUTPUT: "This balance is too small to redeem for anything.",
    SALE_COST_NEEDS_ACCEPTANCE: "Accept this cost to request a fresh exit quote.",
    ONCHAIN_UNAVAILABLE: "The chain could not be asked; try again.",
    PROVIDER_UNAVAILABLE: "The market data source is unavailable; try again.",
    EXITS_UNAVAILABLE: "Tenor exits are not available in this deployment.",
    ORDERS_UNAVAILABLE: "Tenor exits are not available in this deployment.",
    MARKET_KEY_MISMATCH: "Tenor’s market identity could not be verified.",
    TENOR_INVALID_MARKET: "Tenor returned invalid market terms.",
    EXACTLY_PAUSED: "Exactly has paused withdrawals for this market.",
    GATE_CLOSED: "The deployment gate is closed; this quote cannot be signed.",
    EXECUTION_DISABLED: "Signing is disabled in this deployment.",
    SIMULATION_REVERTED: "The transaction simulation failed; nothing was sent.",
    SELF_TAKE: "You cannot take your own bid.", OFFER_EXPIRED: "That bid has expired.",
    OFFER_NOT_STARTED: "That bid is not active yet.", CONSUMED_ASSETS: "That bid has already been used.",
    CONSUMED_UNITS: "That bid has already been used.",
    INSUFFICIENT_NATIVE_VALUE: "This wallet does not hold the native value this transaction sends.",
    INSUFFICIENT_GAS: "This wallet does not hold enough native coin to send this transaction.",
    LOW_GAS_BALANCE: "This wallet may not hold enough native coin for the wallet's gas reservation.",
    GAS_CHECK_UNAVAILABLE: "The native balance and gas requirement could not be checked; the wallet will check before signing.",
  });
  const MIDNIGHT_ADDRESSES = Object.freeze({
    1: "0x471686c42792f93528b000bef54bc10e3aa2045f",
    8453: "0xadedd8ab6de832766fedf0fac4992e5c4d3ea18a",
    5042: "0x208786922be56fde2d1fa60e6b9ec5d723e8d7b0",
  });

  // Position exits are wallet-bound instructions. Reject a response as a whole when its
  // displayed amounts, market identity, or unsigned evidence do not agree with the instruction.
  function positionExitView(data, expected) {
    if (!object(data) || !object(expected) || !address(expected.wallet) || !chain(expected.chain_id)
      || !["EXACTLY_EXIT", "TENOR_EXIT", "PT_REDEEM"].includes(expected.kind)
      || data.kind !== expected.kind || !sameAddress(data.wallet, expected.wallet)
      || data.chain_id !== expected.chain_id || !Object.prototype.hasOwnProperty.call(EXIT_STATUS, data.status)
      || !Array.isArray(data.reason_codes) || !data.reason_codes.every(code)
      || typeof data.broadcast_enabled !== "boolean") return null;
    const gas = gasWarning(data, transactionChain(data, expected.chain_id));
    if (gas === undefined || ["INSUFFICIENT_GAS", "INSUFFICIENT_NATIVE_VALUE"].some(code => data.reason_codes.includes(code)) && (data.broadcast_enabled
      || data.transaction_request != null || data.approval_request != null || data.approval_reset_request != null)
      || ["INSUFFICIENT_GAS", "INSUFFICIENT_NATIVE_VALUE", "LOW_GAS_BALANCE"].some(code => data.reason_codes.includes(code) && !gas)
      ) return null;
    const gasFields = {gas_check: gas?.gas_check ?? null,
      gas_warning: gas?.nativeInsufficient ? "native_value" : gas?.low ? "low" : gas?.insufficient ? "insufficient" : null};
    if (expected.kind === "PT_REDEEM") return principalTokenRedeemView(data, expected, gasFields);
    const signableStatus = data.status === "EXIT_READY";
    const position = data.position;
    if (data.position != null) {
      if (!object(data.position)) return null;
      if (!(expected.kind === "EXACTLY_EXIT" ? ["Exactly"] : ["Tenor", "Morpho Midnight"]).includes(position.venue)
        || !Number.isSafeInteger(position.maturity) || position.maturity < 0
        || Number.isSafeInteger(expected.maturity) && position.maturity !== expected.maturity) return null;
      if (expected.kind === "EXACTLY_EXIT") {
        if (!sameAddress(position.market_address, expected.market_address) || position.market_id != null
          || !address(position.market_address)) return null;
      } else if (typeof position.market_id !== "string" || !/^0x[0-9a-f]{64}$/i.test(position.market_id)
        || position.market_id.toLowerCase() !== String(expected.market_id).toLowerCase()
        || !address(position.midnight_address) || !sameAddress(position.midnight_address, MIDNIGHT_ADDRESSES[expected.chain_id])) return null;
    } else if (signableStatus) return null;
    if (data.receive != null) {
      if (!object(data.receive) || !address(data.receive.address) || !decimals(data.receive.decimals)
        || typeof data.receive.symbol !== "string" || !data.receive.symbol.trim()
        || expected.receive_address && !sameAddress(data.receive.address, expected.receive_address)) return null;
    } else if (signableStatus) return null;
    if (data.block != null && (!object(data.block) || !Number.isSafeInteger(data.block.number) || data.block.number < 0
      || typeof data.block.hash !== "string" || !/^0x[0-9a-f]{64}$/i.test(data.block.hash))) return null;
    if (signableStatus && data.block == null) return null;
    const allowed = EXIT_ACTIONS[expected.kind];
    if (data.action !== null && !allowed.has(data.action)) return null;
    if (data.status === "EXIT_READY" && !data.action
      || data.status === "ANALYTICAL_ONLY" && (!data.action || !data.reason_codes.includes("SALE_COST_NEEDS_ACCEPTANCE"))) return null;
    if (expected.kind === "EXACTLY_EXIT" && data.action
      && !["EXACTLY_WITHDRAW_AT_MATURITY", "EXACTLY_WITHDRAW_EARLY"].includes(data.action)) return null;
    const rawKeys = Object.keys(data).filter(key => key.endsWith("_raw_amount"));
    if (!["proceeds_raw_amount", "min_proceeds_raw_amount", "face_raw_amount", "mark_raw_amount"].every(key => rawKeys.includes(key))
      || rawKeys.some(key => data[key] !== null && (typeof data[key] !== "string" || amount(data[key]) === null))) return null;
    const hasQuote = ["EXIT_READY", "ANALYTICAL_ONLY"].includes(data.status);
    if (hasQuote && (!position || !data.receive || !data.block)) return null;
    for (const key of ["proceeds_raw_amount", "min_proceeds_raw_amount", "face_raw_amount", "mark_raw_amount"]) {
      if (hasQuote && amount(data[key]) === null) return null;
    }
    if ((data.mark_basis != null && (typeof data.mark_basis !== "string" || !data.mark_basis.trim()))
      || (data.route_cost_bps != null && !Number.isSafeInteger(data.route_cost_bps))
      || hasQuote && (typeof data.mark_basis !== "string" || !Number.isSafeInteger(data.route_cost_bps))) return null;
    if (expected.kind === "TENOR_EXIT") {
      if (!["TENOR_WITHDRAW", "TENOR_SELL"].includes(data.action)) {
        if (data.status === "EXIT_READY" || data.status === "ANALYTICAL_ONLY") return null;
      } else if (hasQuote && ["units_raw_amount", "remaining_units_raw_amount"].some(key => typeof data[key] !== "string" || amount(data[key]) === null)) return null;
      if (data.action === "TENOR_SELL" && hasQuote && (typeof data.settlement_fee_raw_amount !== "string"
        || amount(data.settlement_fee_raw_amount) === null)) return null;
    }
    if (data.cost_acceptance != null) {
      const acceptance = data.cost_acceptance;
      if (!object(acceptance) || data.status !== "ANALYTICAL_ONLY" || acceptance.route_cost_bps !== data.route_cost_bps
        || acceptance.max_acceptable_bps !== 1000 || !Number.isSafeInteger(acceptance.route_cost_bps)
        || acceptance.route_cost_bps <= 300 || acceptance.route_cost_bps > 1000
        || typeof acceptance.mark_raw_amount !== "string" || typeof acceptance.proceeds_raw_amount !== "string"
        || amount(acceptance.mark_raw_amount) !== amount(data.mark_raw_amount)
        || amount(acceptance.proceeds_raw_amount) !== amount(data.proceeds_raw_amount)) return null;
    } else if (data.status === "ANALYTICAL_ONLY") return null;
    if (data.cost_accepted_bps != null && (!Number.isSafeInteger(data.cost_accepted_bps)
      || data.cost_accepted_bps < data.route_cost_bps || data.cost_accepted_bps > 10000 || data.status !== "EXIT_READY"
      || data.cost_accepted_bps !== expected.accept_cost_bps)) return null;
    if (data.status === "EXIT_READY" && data.route_cost_bps > 300
      && (data.route_cost_bps > 1000 || !Number.isSafeInteger(data.cost_accepted_bps)
        || data.cost_accepted_bps < data.route_cost_bps || data.cost_accepted_bps > 10000
        || data.cost_accepted_bps !== expected.accept_cost_bps)) return null;
    const selector = data.action === "EXACTLY_WITHDRAW_AT_MATURITY" || data.action === "EXACTLY_WITHDRAW_EARLY" ? "0xa05a091a"
      : data.action === "TENOR_WITHDRAW" ? "0xa6db1750" : data.action === "TENOR_SELL" ? "0x6a14c9ef" : null;
    const unsigned = data.unsigned_transaction_request, tx = data.transaction_request;
    if (data.broadcast_enabled && (!signableStatus || !object(tx) || !object(unsigned))) return null;
    if (!data.broadcast_enabled && tx !== null) return null;
    if (unsigned !== null && unsigned !== undefined) {
      if (!position) return null;
      const target = expected.kind === "EXACTLY_EXIT" ? position.market_address : position.midnight_address;
      for (const instruction of [unsigned, ...(tx ? [tx] : [])]) {
        if (!object(instruction) || instruction.chainId !== expected.chain_id || !sameAddress(instruction.from, expected.wallet)
          || !sameAddress(instruction.to, target) || typeof instruction.data !== "string"
          || !/^0x(?:[0-9a-f]{2})+$/i.test(instruction.data) || (selector && !instruction.data.toLowerCase().startsWith(selector))
          || instruction.value !== "0") return null;
      }
      if (tx && (tx.chainId !== unsigned.chainId || !sameAddress(tx.from, unsigned.from) || !sameAddress(tx.to, unsigned.to)
        || tx.data.toLowerCase() !== unsigned.data.toLowerCase() || tx.value !== unsigned.value)) return null;
    } else if (data.status === "EXIT_READY") return null;
    return {status: data.status, status_text: EXIT_STATUS[data.status], action: data.action,
      reasons: data.reason_codes.map(item => ({code: item, text: EXIT_REASONS[item] || REASON_TEXT[item]
        || item.toLowerCase().replace(/_/g, " ").replace(/^./, first => first.toUpperCase())})),
      can_sign: data.status === "EXIT_READY" && data.broadcast_enabled && !!tx, ...gasFields};
  }

  function principalTokenRedeemView(data, expected, gasFields = {}) {
    const router = "0x888888888889758f76e7103c6cbf23abbf58f946";
    const pendle = expected.venue === "Pendle";
    const token = expected.instrument_address;
    const signable = data.status === "EXIT_READY";
    const floorCostBps = (mark, minimum) => minimum >= mark ? 0
      : Number((mark - minimum) * 10000n / mark + ((mark - minimum) * 10000n % mark ? 1n : 0n));
    if (!address(token) || !["Pendle", "Spectra"].includes(expected.venue)
      || data.position != null && (!object(data.position) || !sameAddress(data.position.address, token)
        || data.position.venue !== expected.venue || !Number.isSafeInteger(data.position.maturity) || data.position.maturity < 0
        || data.position.maturity_timestamp !== data.position.maturity)
      || data.receive != null && (!object(data.receive) || !address(data.receive.address) || !decimals(data.receive.decimals)
        || typeof data.receive.symbol !== "string" || !data.receive.symbol.trim())
      || data.block != null && (!object(data.block) || !Number.isSafeInteger(data.block.number) || data.block.number < 0
        || typeof data.block.hash !== "string" || !/^0x[0-9a-f]{64}$/i.test(data.block.hash))
      || Object.keys(data).filter(key => key.endsWith("_raw_amount")).some(key =>
        data[key] != null && (typeof data[key] !== "string" || !/^[0-9]{1,78}$/.test(data[key])
          || amount(data[key]) >= (1n << 256n)))
      || ["EXIT_READY", "ANALYTICAL_ONLY"].includes(data.status)
        && (data.face_raw_amount !== data.proceeds_raw_amount
          || data.mark_raw_amount !== data.proceeds_raw_amount)
      || data.action != null && data.action !== (pendle ? "PENDLE_REDEEM" : "SPECTRA_REDEEM")
      || pendle && data.simulated != null && typeof data.simulated !== "boolean"
      || !pendle && data.simulated != null) return null;
    if (data.status !== "EXIT_READY" && data.status !== "ANALYTICAL_ONLY") {
      if (data.transaction_request != null || data.approval_request != null || data.approval_reset_request != null
        || data.broadcast_enabled) return null;
      return {status: data.status, status_text: EXIT_STATUS[data.status] || "Redemption unavailable",
        action: data.action, reasons: data.reason_codes.map(item => ({code: item, text: EXIT_REASONS[item] || REASON_TEXT[item] || item})), can_sign: false, ...gasFields};
    }
    if (!data.position || !data.receive || !data.block || data.action !== (pendle ? "PENDLE_REDEEM" : "SPECTRA_REDEEM")
      || pendle && typeof data.simulated !== "boolean"
      || !["proceeds_raw_amount", "min_proceeds_raw_amount", "face_raw_amount", "mark_raw_amount", "units_raw_amount"].every(key =>
        typeof data[key] === "string" && amount(data[key]) !== null)
      || amount(data.units_raw_amount) === 0n || amount(data.min_proceeds_raw_amount) > amount(data.proceeds_raw_amount)
      || typeof data.mark_basis !== "string" || !data.mark_basis.trim()) return null;
    const mark = amount(data.mark_raw_amount), minimum = amount(data.min_proceeds_raw_amount);
    if (mark === 0n) return null;
    const expectedCost = floorCostBps(mark, minimum);
    if (!Number.isSafeInteger(data.route_cost_bps) || data.route_cost_bps !== expectedCost) return null;
    if (data.cost_acceptance != null) {
      const acceptance = data.cost_acceptance;
      if (!object(acceptance) || data.status !== "ANALYTICAL_ONLY"
        || acceptance.route_cost_bps !== expectedCost || acceptance.max_acceptable_bps !== 1000
        || acceptance.route_cost_bps <= 300 || acceptance.route_cost_bps > 1000
        || acceptance.mark_raw_amount !== data.mark_raw_amount
        || acceptance.proceeds_raw_amount !== data.proceeds_raw_amount) return null;
    }
    if (data.status === "ANALYTICAL_ONLY") {
      if (data.cost_acceptance == null || expectedCost <= 300
        || !data.reason_codes.includes("SALE_COST_NEEDS_ACCEPTANCE")
        || data.cost_accepted_bps != null
        || data.transaction_request != null || data.approval_request != null
        || data.approval_reset_request != null || data.broadcast_enabled) return null;
      return {status: data.status, status_text: EXIT_STATUS[data.status], action: data.action,
        reasons: data.reason_codes.map(item => ({code: item, text: EXIT_REASONS[item] || REASON_TEXT[item] || item})), can_sign: false, ...gasFields};
    }
    if (data.cost_accepted_bps != null && (!Number.isSafeInteger(data.cost_accepted_bps)
      || data.cost_accepted_bps < expectedCost || data.cost_accepted_bps > 10000 || data.status !== "EXIT_READY"
      || data.cost_accepted_bps !== expected.accept_cost_bps)) return null;
    if (signable && expectedCost > 300 && (expectedCost > 1000
      || !Number.isSafeInteger(data.cost_accepted_bps) || data.cost_accepted_bps < expectedCost
      || data.cost_accepted_bps !== expected.accept_cost_bps)) return null;
    const tx = data.transaction_request, unsigned = data.unsigned_transaction_request;
    const target = pendle ? router : token, selector = pendle ? "0x47f1de22" : "0x9f40a7b3";
    const validTx = instruction => object(instruction) && instruction.chainId === expected.chain_id
      && sameAddress(instruction.from, expected.wallet) && sameAddress(instruction.to, target)
      && typeof instruction.data === "string" && /^0x(?:[0-9a-f]{2})+$/i.test(instruction.data)
      && instruction.data.toLowerCase().startsWith(selector)
      && instruction.value === "0";
    if (!data.broadcast_enabled && (tx != null || data.approval_request != null || data.approval_reset_request != null)) return null;
    const word = (instruction, index) => instruction.data.slice(10 + index * 64, 10 + (index + 1) * 64).toLowerCase();
    const addressWord = value => value.slice(2).toLowerCase().padStart(64, "0");
    const amountWord = value => BigInt(value).toString(16).padStart(64, "0");
    const validRedeemAmounts = instruction => pendle
      ? instruction.data.length === 10 + 14 * 64 && word(instruction, 0) === addressWord(expected.wallet)
        && /^0{24}[0-9a-f]{40}$/.test(word(instruction, 1)) && word(instruction, 1) !== "0".repeat(64)
        && word(instruction, 2) === amountWord(data.units_raw_amount)
        && word(instruction, 3) === amountWord("128") && word(instruction, 4) === addressWord(data.receive.address)
        && word(instruction, 5) === amountWord(data.min_proceeds_raw_amount)
        && word(instruction, 6) === addressWord(data.receive.address) && /^0{64}$/.test(word(instruction, 7))
        && word(instruction, 8) === amountWord("160") && /^0{64}$/.test(word(instruction, 9))
        && /^0{64}$/.test(word(instruction, 10)) && word(instruction, 11) === amountWord("128")
        && /^0{64}$/.test(word(instruction, 12)) && /^0{64}$/.test(word(instruction, 13))
      : instruction.data.length === 10 + 4 * 64 && word(instruction, 0) === amountWord(data.units_raw_amount)
        && word(instruction, 1) === addressWord(expected.wallet)
        && word(instruction, 2) === addressWord(expected.wallet) && word(instruction, 3) === amountWord(data.min_proceeds_raw_amount);
    if (!validTx(unsigned) || !validRedeemAmounts(unsigned)
      || data.broadcast_enabled && (!validTx(tx) || unsigned.data.toLowerCase() !== tx.data.toLowerCase())) return null;
    const approval = data.approval_request, unsignedApproval = data.unsigned_approval_request;
    const reset = data.approval_reset_request, unsignedReset = data.unsigned_approval_reset_request;
    if (pendle) {
      const amountRaw = data.units_raw_amount;
      const validApproval = (instruction, requiredAmount) => object(instruction)
        && instruction.chainId === expected.chain_id && sameAddress(instruction.from, expected.wallet)
        && sameAddress(instruction.to, token) && sameAddress(instruction.spender, router)
        && instruction.value === "0" && instruction.amount === requiredAmount
        && typeof instruction.data === "string" && instruction.data.toLowerCase() === "0x095ea7b3"
          + router.slice(2).toLowerCase().padStart(64, "0") + BigInt(requiredAmount).toString(16).padStart(64, "0");
      if (!validApproval(unsignedApproval, amountRaw) || !validApproval(unsignedReset, "0")
        || data.broadcast_enabled && (!validApproval(approval, amountRaw) || !validApproval(reset, "0"))) return null;
    } else if (approval != null || unsignedApproval != null || reset != null || unsignedReset != null) return null;
    return {status: data.status, status_text: EXIT_STATUS[data.status], action: data.action,
      reasons: data.reason_codes.map(item => ({code: item, text: EXIT_REASONS[item] || REASON_TEXT[item]
        || item.toLowerCase().replace(/_/g, " ")})), can_sign: signable && data.broadcast_enabled, ...gasFields};
  }

  function sameAddress(left, right) {
    return address(left) && address(right) && left.toLowerCase() === right.toLowerCase();
  }

  return {buildRequest, bindQuote, formatAmount, displayQuote, parseAmount, executionRouteView, saleRouteView, positionExitView,
    REASON_TEXT, ROUTE_STATUS, VALIDITY_MS};
});
