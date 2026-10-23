(function (root) {
  "use strict";

  const FRESHNESS_STATUSES = ["FRESH", "STALE", "EXPIRED"];
  const NOT_COMPUTABLE = "NOT COMPUTABLE";
  const P = root.FenoriumPortfolio || {};
  const PURCHASE_STAGES = P.PURCHASE_STAGES;
  const ICONS = root.FenoriumIconCatalog || {};

  const $ = (id) => {
    if (typeof document === "undefined") return null;
    return document.getElementById(id);
  };

  const esc = (value) => String(value == null ? "UNKNOWN" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

  const textOr = (value, fallback = NOT_COMPUTABLE) => value == null || value === "" ? fallback : value;

  function asBigInt(value) {
    if (typeof P.asBigInt === "function") return P.asBigInt(value);
    try {
      return value == null ? null : BigInt(String(value));
    } catch (_) {
      return null;
    }
  }

  function usd(micros) {
    const value = asBigInt(micros);
    if (value === null) return NOT_COMPUTABLE;
    const sign = value < 0n ? "-" : "";
    const absolute = value < 0n ? -value : value;
    const whole = (absolute / 1000000n).toLocaleString("en-US");
    const fraction = (absolute % 1000000n).toString().padStart(6, "0").slice(0, 2);
    return `${sign}$${whole}.${fraction}`;
  }

  function usdN(value) {
    if (value == null || value === "") return NOT_COMPUTABLE;
    const number = Number(value);
    return Number.isFinite(number) ? `$${Math.round(number).toLocaleString("en-US")}` : NOT_COMPUTABLE;
  }

  function pct(bps) {
    if (bps == null || bps === "") return NOT_COMPUTABLE;
    const number = Number(bps);
    return Number.isFinite(number) ? `${(number / 100).toFixed(2)}%` : NOT_COMPUTABLE;
  }

  function verdict(row) {
    if (typeof P.marketVerdict === "function") return P.marketVerdict(row) || "N/A";
    return String(row?.verdict || "UNRATED").toUpperCase();
  }

  function slug(value) {
    return String(value || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  // Presentation only: codes, numerical values and eligibility remain owned by the engine.
  const SCORE_TOOLTIP = "Fenorium Score: an evidence score from 0 to 100; 60 or more passes every check. It is not a guarantee or a credit rating.";
  const STATE_COPY = Object.freeze({
    "ALIGNED": ["Priced", "The available price evidence agrees."],
    "UNPRICED": ["Price unavailable", "Refresh on-chain reads; the balance is known but its value is UNKNOWN."],
    "UNRESOLVED": ["Balance unavailable", "Refresh on-chain reads; this position remains excluded from priced value."],
    "MISSING_PRICE": ["Price unavailable", "Refresh the data to check for a supported price."],
    "STALE_INPUT": ["Price evidence out of date", "Refresh the data before using this value."],
    "INSUFFICIENT_SOURCES": ["No usable price", "No current price was available for this holding."],
    "SOURCE_CONFLICT": ["Price unavailable", "No price could be confirmed for this holding."],
    "UNRESOLVED_POSITION": ["Position unreadable", "Refresh on-chain reads to check this position."],
    "ZERO_BALANCE": ["Zero balance confirmed", "The balance read returned zero."],
    "VALUE_COVERAGE_NOT_COMPUTABLE": ["Value coverage unavailable", "Review unresolved positions; total portfolio value is UNKNOWN."],
    "MODELLED": ["Modelled estimate", "This is a model estimate, not a measurement or a live executable quote."],
    "NOT_MEASURED": ["UNKNOWN · Not measured", "This applies, but no measurement is available. Check the evidence."],
    "UNKNOWN": ["UNKNOWN · Not measured", "This applies, but has not been established. Check the evidence."],
    "N/A": ["N/A · Not applicable", "This does not apply to this instrument."],
    "NOT_APPLICABLE": ["N/A · Not applicable", "This does not apply to this instrument."],
    "NOT COMPUTABLE": ["UNKNOWN · Cannot calculate", "Required evidence is missing. Review the evidence."],
    "NOT_COMPUTABLE": ["UNKNOWN · Cannot calculate", "Required evidence is missing. Review the evidence."],
    "UNANCHORED": ["Not recorded on-chain", "This evidence preview has no on-chain proof. Inspect the evidence before relying on it."],
    "FRESH": ["Up to date", "The snapshot is within its freshness limit."],
    "STALE": ["Needs refresh", "Refresh the market data before using these values."],
    "EXPIRED": ["Too old to use", "Refresh the market data; do not use these curve values for a decision."],
    "UNSUPPORTED": ["Not supported", "This analysis is unavailable. Review the supported evidence."],
    "PARTIALLY_SUPPORTED": ["Partly supported", "Only some analyses are available. Review the evidence coverage."],
    "PARTIAL": ["Partial evidence", "Only the displayed evidence is available. Review its coverage."],
    "MISSING": ["Evidence missing", "Review the evidence to identify the missing input."],
    "PASS": ["Evidence present", "The required fields are present; this is not execution approval."],
    "SUPPORTED": ["Supported", "This position type can be analysed using the available evidence."],
    "COMPUTED": ["Calculated", "The calculation uses the supplied evidence."],
    "COMPUTABLE": ["Ready to calculate", "The required inputs are available."],
    "MEASURED": ["Measured", "A recorded observation is available."],
    "PRICED": ["Priced", "A supported price is available."],
    "PRICED_NAV_LOWER_BOUND": ["Priced value · lower bound", "Unpriced positions are excluded; this is not total portfolio value."],
    // "Authoritative" means every DISCOVERED position carries a price, and discovery is
    // REGISTERED_ASSETS_ONLY. Calling that a complete portfolio value presents a registry scan
    // as a whole-wallet scan, which the discovery scope explicitly is not.
    "AUTHORITATIVE_NAV": ["Every discovered position priced", "All registered assets found in this wallet carry a price. Assets outside the supported registry are not included."],
    "REGISTERED_ASSETS_ONLY": ["Registered assets only", "Only assets in the supported registry were checked."],
    "LOCAL_DEMO_FIXTURE": ["Illustrative sample", "These are sample data, not your wallet or market observations."],
    "CONNECTED_WALLET": ["Connected wallet", "Balances refer to the connected wallet."],
    "PERMISSIONLESS": ["No transfer gate observed", "A transfer probe reached the balance check. Issuer onboarding may still apply."],
    "VERIFIED": ["Wallet verified", "The instrument confirmed this wallet is verified."],
    "UNVERIFIED": ["Wallet not verified", "Request verification from the demo operator."],
    "GATED": ["Issuer verification required", "Check wallet eligibility before proceeding."],
    "RESTRICTED": ["Access restricted", "Review the issuer eligibility requirements."],
    "ELIGIBLE": ["Eligible", "The checked requirements pass; this is not transaction confirmation."],
    "UNRATED": ["Not rated", SCORE_TOOLTIP],
    "BID": ["Score 60+", SCORE_TOOLTIP],
    "NO BID": ["Below 60", `${SCORE_TOOLTIP} See the reason.`],
    "NO QUOTE": ["Not scored", SCORE_TOOLTIP],
    "NO_WALLET": ["Connect a wallet", "Connect your wallet to read balances on the Base Sepolia testnet."],
    "WRONG_CHAIN": ["Switch to Base Sepolia testnet", "Use the wallet network control to switch to the demo testnet."],
    "UNCONFIGURED_DEPLOYMENT": ["Demo unavailable", "Contact the demo operator to configure the testnet deployment."],
    "INSUFFICIENT_MUSDC": ["Not enough testnet mUSDC", "Get mUSDC from the faucet to cover the displayed quote."],
    "INSUFFICIENT_INSTRUMENT": ["Not enough of the instrument", "Reduce the amount to the instrument balance this wallet holds."],
    "UNVERIFIED_GATED": ["Wallet verification required", "Request verification from the demo operator for this testnet instrument."],
    "USER_REJECTED": ["Wallet request cancelled", "Refresh on-chain reads to re-enable the action, then try again when you are ready to confirm it in your wallet."],
    "PENDING": ["Waiting for confirmation", "Check your wallet for a request; nothing is submitted until you confirm it."],
    "AWAITING_RECEIPT": ["Waiting for confirmation", "The transaction was submitted and is awaiting its receipt. Do not submit it again."],
    "INDETERMINATE": ["Transaction outcome UNKNOWN", "Check the transaction link for its outcome. Do not submit a duplicate."],
    // Covers a failed read AND a transaction that never left the wallet (rpcFailurePatch), so it
    // must not assert either one, nor prescribe a refresh that cannot resolve a missing-gas send.
    "RPC_ERROR": ["Request failed", "No on-chain state was assumed. Review the reported cause, then try again or refresh on-chain reads."],
    "CONFIRMED_STATE_MISMATCH": ["Refresh on-chain reads", "The transaction confirmed, but the expected updated state is not yet visible. Refresh on-chain reads before continuing."],
    "REVERT": ["Transaction failed", "Refresh on-chain reads before trying again; the transaction did not complete its action."],
    "SUCCESS": ["Testnet action succeeded", "The transaction was confirmed on the Base Sepolia testnet."],
    // READY means two things: deploymentStatus() derives it from configuration alone, and the
    // read path sets it once balances and quote come back. It is also the initial state before
    // anything is read. The label must therefore claim only the weaker of the two -- the enabled
    // action button is what tells a user a purchase is actually available.
    "READY": ["Testnet demo configured", "Review the amount and exact mUSDC quote before continuing."],
    "INPUT_ERROR": ["Correct the amount", "Enter a positive amount with no more than six decimal places."],
    "CONFIRMED": ["Confirmed on testnet", "The transaction receipt confirms the action completed."],
    "REVERTED": ["Transaction failed", "Refresh on-chain reads before trying again."],
    "REQUIRED": ["Approval needed", "Approve the exact testnet mUSDC amount to continue."],
    "NOT REQUIRED": ["Approval already sufficient", "The current allowance covers the quote."],
    "NOT STARTED": ["Not started", "No purchase confirmation has been recorded."],
    "SALE_NOT_STARTED": ["Not started", "No sale confirmation has been recorded."],
    "INSTRUMENT_APPROVAL_REQUIRED": ["Approval needed", "Approve the instrument amount for the desk to continue."],
    "INSTRUMENT_APPROVAL_NOT_REQUIRED": ["Approval already sufficient", "The current instrument allowance covers the amount."],
    "DISABLED": ["Disabled", "Mainnet execution and fees are disabled."],
    "LEGAL_REVIEW_REQUIRED": ["Legal review pending", "Mainnet execution stays disabled pending legal review."],
    "MISSING_EXECUTION_QUOTE": ["Execution quote missing", "Inspect the evidence; no live execution quote is available."],
    "DESTINATION_FAILURE_EXPERIMENT": ["Destination recovery unverified", "Cross-chain routing stays disabled until failure and refund behavior is verified."],
    "ENABLED": ["Enabled", "Same-chain purchases through a route this server fetches for your wallet."],
    "INSUFFICIENT_HISTORY": ["Not enough history", "Review the history coverage; 250 aligned daily observations are required."],
    "INSUFFICIENT_COVERAGE": ["Not enough coverage", "Review excluded positions before using portfolio risk results."],
    "BACKEND_METRIC_REQUIRED": ["Risk result unavailable", "Refresh portfolio analytics to request the measured result."],
    "NOT_RANKABLE": ["Cannot rank", "Review the missing evidence; no ranking is supplied."],
    "ANALYTICAL_ONLY": ["Analysis only", "Review the missing costs; this scenario cannot be executed."],
    "CONTEXT_ONLY": ["Context only", "Use this information as portfolio context, not an execution instruction."],
    "NOT_MEASURED_PER_INSTRUMENT": ["Instrument freshness UNKNOWN", "Check the source timestamp; freshness has not been measured per instrument."],
    "EMULATED": ["Emulated", "This is a simulation, not confidential or attested computation."],
    "ADAPTER_ERROR": ["Balance reader failed", "Refresh on-chain reads to retry this position."],
    "UNSUPPORTED_CHAIN": ["Network not supported", "Select a supported network to read balances."],
    "DEMO_CHAIN_NOT_ENABLED": ["Testnet reads disabled", "Contact the demo operator to enable testnet reads."],
    "INCOMPATIBLE_MARKING_FREQUENCY": ["Price histories do not align", "Review the history sources; matching daily observations are required."],
    "MISSING_LIQUIDITY": ["Reported depth UNKNOWN", "Check the depth evidence before choosing an amount."],
    "INSUFFICIENT_LIQUIDITY": ["Not enough reported depth", "Reported depth is below the amount or the liquidity floor, whichever is higher. Reduce the amount, or lower the floor if it alone exceeds the depth."],
    "METHODOLOGY_NOT_APPLICABLE": ["N/A · Method does not apply", "This method cannot evaluate this instrument."],
    "PERMISSION_UNKNOWN": ["Eligibility UNKNOWN", "Check issuer eligibility requirements before proceeding."],
    "MISSING_COST": ["Cost UNKNOWN", "Review the cost evidence before comparing net outcomes."],
    "EXPLICIT_SOURCE_REQUIRED": ["Choose a source", "Select the holding to use for this scenario."],
    "AMOUNT_REQUIRED": ["Amount required", "Enter the amount to analyse."],
    "VARIABLE_ASSET_OPT_IN_REQUIRED": ["Variable-asset consent required", "Opt in explicitly to use a variable-income asset as the source."],
    "VARIABLE_ASSET_LIMIT_REQUIRED": ["Variable-asset limit required", "Set a maximum source amount."],
    "VARIABLE_ASSET_LIMIT_EXCEEDED": ["Source amount exceeds limit", "Reduce the source amount to the stated limit."],
    "SOURCE_LIMIT_EXCEEDED": ["Source amount exceeds limit", "Reduce the source amount to the available limit."],
    "SOURCE_CAPABILITY_REQUIRED": ["Source not supported", "Select a supported source holding."],
    "IDLE_STABLECOIN_SOURCE_REQUIRED": ["Idle stablecoin source required", "Select an idle supported stablecoin holding."],
    "ISSUER_NOT_ALLOWED": ["Issuer outside mandate", "Review the mandate restriction for this issuer."],
    "PROTOCOL_NOT_ALLOWED": ["Protocol outside mandate", "Review the mandate restriction for this protocol."],
    "CHAIN_NOT_ALLOWED": ["Network outside mandate", "Review the mandate restriction for this network."],
    "LIQUIDITY_FLOOR_FAIL": ["Liquidity below mandate minimum", "Review the mandate liquidity requirement."],
    "MATURITY_LIMIT_FAIL": ["Maturity exceeds mandate limit", "Review the mandate maturity requirement."],
    "CREDENTIAL_REQUIREMENT_FAIL": ["Required credentials missing", "Request the required issuer credentials."],
    "PROTOCOL_CONCENTRATION_FAIL": ["Protocol concentration too high", "Reduce the amount assigned to this protocol."],
    "MANDATE_PASS": ["Mandate checks passed", "The supplied mandate checks pass. This is an analysis, not an instruction to buy."],
    "MANDATE_LIMIT": ["Mandate limit reached", "Reduce the scenario amount."],
    "MANDATE_UNKNOWN": ["Mandate result UNKNOWN", "Review the missing mandate inputs."],
    "ES_NOT_COMPARABLE": ["Risk change cannot be compared", "Review the matching history and coverage requirements."],
    "COMPLETE": ["Complete", "The required records are available."],
    "REJECTED": ["Not accepted", "Review the refusal reason before proceeding."],
    "QUOTED": ["Quote available", "Review the quote; it is not a completed transaction."],
    "QUOTE_CHANGED": ["Quote changed", "Review the new quote before confirming."],
    "QUOTE_REVIEW_REQUIRED": ["Review the new quote", "Confirm the updated amount before submitting a testnet transaction."],
    "QUOTE_EXPIRED": ["Quote expired", "Refresh the quote before proceeding."],
    "EVIDENCE_STALE": ["Evidence out of date", "Refresh the evidence before proceeding."],
    "VELA_UNAVAILABLE": ["Confidential computation unavailable", "Use the standard analytical evidence; no attested result is available."],
    "UNIVERSE_REQUEST_FAILED": ["Market data unavailable", "Reload the market to retry the data request."],
    "CONFIRMED \u00b7 READ REFRESH FAILED": ["Approval confirmed · reads need refresh", "Refresh on-chain reads before continuing."],
    "NOT CURRENT \u00b7 refresh failed": ["Eligibility UNKNOWN · reads need refresh", "Refresh on-chain reads to check eligibility."],
    "UNKNOWN \u00b7 NOT READ": ["UNKNOWN · Not read", "Refresh on-chain reads to check wallet eligibility."],
    "MODELLED_SIZE_TO_REPORTED_DEPTH": ["Modelled size-impact estimate", "This estimate uses selected size and reported depth — what was shown exitable or acquirable; it is not measured or a live executable quote."],
    "TIER_A": ["Tier A", "This is a classification, not a guarantee of repayment."],
    "TIER_B": ["Tier B", "This is a classification, not a guarantee of repayment."],
    "TIER_C": ["Tier C", "This is a classification, not a guarantee of repayment."],
    "declared_demo": ["Declared testnet rate", "This is a synthetic demo assumption, not a market observation."],
    "issuer_quoted": ["Issuer-quoted rate", "This is an issuer quote, not a realized return."],
    "indicative": ["Indicative rate", "This rate has not been shown executable at a stated size."],
    "realized": ["Historical realized rate", "This is a past observation, not a future offered rate."],
    "executable": ["Quoted executable rate", "The recorded quote is not a completed transaction."],
    "NO_BID": ["Below 60", `${SCORE_TOOLTIP} See the reason.`],
    "NO_QUOTE": ["Not scored", SCORE_TOOLTIP],
    "VARIABLE_INCOME_OPT_IN_REQUIRED": ["Variable-asset consent required", "Opt in explicitly to use a variable-income asset as the source."],
    "VARIABLE_INCOME_LIMIT_EXCEEDED": ["Source amount exceeds limit", "Reduce the source amount to the stated limit."],
    "CONNECTED": ["Wallet connected", "The wallet is connected; balances require a successful read."],
    "RECORDED": ["Recorded evidence", "Inspect the source and timestamp for this record."],
    "NOT CONFIGURED": ["Not configured", "Contact the demo operator to configure this testnet address."],
    "INSUFFICIENT CASHFLOW DATA": ["Cashflow data missing", "Review the cashflow evidence; yield alone cannot establish duration."],
    "INSUFFICIENT COVERAGE": ["Not enough coverage", "Review the missing evidence before relying on this metric."]
  });

  // What the deployment allows, set by the app once the server and this build's configuration
  // agree. Both default to off, so a page that never learns the answer describes nothing as
  // purchasable.
  const deployment = { execution: false, fees: false };

  function setDeploymentState(next) {
    deployment.execution = next?.execution === true;
    deployment.fees = next?.fees === true;
  }

  function stateLabel(value) {
    const raw = String(value == null ? "UNKNOWN" : value);
    return STATE_COPY[raw]?.[0] || raw.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
  }

  function stateText(value) {
    const raw = String(value == null ? "UNKNOWN" : value);
    const copy = STATE_COPY[raw];
    return copy ? `${copy[0]}. ${copy[1]}` : `${stateLabel(raw)}. Review the evidence before proceeding.`;
  }

  // Longest multiword codes first, then bare tokens, in ONE pass. Translating tokens alone
  // rewrote the BID inside "NO BID (insufficient evidence)" and produced "NO Criteria passed",
  // which states the opposite of the refusal it was describing. A single pass also stops a
  // substituted label from being re-translated by a later one.
  const MULTIWORD_STATE_CODES = Object.keys(STATE_COPY)
    .filter((code) => code.includes(" "))
    .sort((left, right) => right.length - left.length)
    .map((code) => code.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const REASON_PREFIX = /^(NO BID \(credit \+ insufficient evidence\)|NO BID \(insufficient evidence\)|NO BID \(credit\)|NO QUOTE|BID) — /;
  const EXPLAIN_PATTERN = new RegExp(
    `(?:${REASON_PREFIX.source}|${MULTIWORD_STATE_CODES.join("|")}|\\b[A-Z][A-Z_]{2,}\\b)`,
    "g",
  );

  function scoreLabel(row) {
    const code = verdict(row);
    if (!["BID", "NO BID", "NO_BID"].includes(code)) return stateLabel(code);
    const score = row?.fenorium_score;
    return Number.isInteger(score) && score >= 0 && score <= 100
      ? `Score ${score}/100` : stateLabel(code);
  }

  // `compact`: the score alone, one line, for a table cell; the cap reason stays in the tooltip.
  function scoreChip(row, {compact = false} = {}) {
    const code = verdict(row);
    const capped = ["NO BID", "NO_BID"].includes(code) && row?.score_cap;
    const label = scoreLabel(row) + (capped && !compact ? ` · capped: ${row.score_cap}` : "");
    const title = SCORE_TOOLTIP + (["NO BID", "NO_BID"].includes(code)
      ? ` ${capped ? row.score_cap : "See the reason"}.` : "");
    return chip(code, label, title);
  }

  // Why a row is below 60, in the same two words everywhere: a credit finding is a "measured issue",
  // a gap in the evidence is "missing evidence".
  function reasonBasis(reason) {
    const prefix = String(reason || "").match(REASON_PREFIX)?.[1] || "";
    return prefix.includes("credit + insufficient evidence") ? "measured issue + missing evidence"
      : prefix.includes("insufficient evidence") ? "missing evidence"
      : prefix.includes("credit") ? "measured issue" : "";
  }

  // The evidence codes a reason carries, in words. Display only: the stored reason keeps its codes
  // and every address, so nothing a reader could verify is lost, only the spelling.
  const EVIDENCE_FIELDS = Object.freeze({
    peg_history: "peg history", protocol_tvl: "protocol TVL", protocol_tvl_trend: "protocol TVL trend",
    holder_concentration: "holder concentration", peg_drawdown_limit: "peg drawdown limit",
    market_liquidity_usd: "market liquidity (USD)", oracle_model_risk: "oracle model risk",
    collateral_oracle: "collateral oracle", collateral_oracle_feed: "collateral oracle feed",
    collateral_liquidation_depth: "collateral liquidation depth", collateral_price_history: "collateral price history",
  });
  const FEED_MODELS = Object.freeze({
    genuine_feed: "genuine feed", onchain_adapter: "on-chain adapter", heartbeat_undocumented: "heartbeat undocumented",
    documented_heartbeat: "documented heartbeat", hardcoded_1to1: "fixed 1:1", variable_price: "variable price",
    exchange_rate_adapter: "exchange-rate adapter", updated_at_zero: "no update time",
    mixed_or_incomplete_feeds: "mixed or incomplete feeds", unverified: "unverified",
  });
  const snakeToWords = (token) => FEED_MODELS[token] || EVIDENCE_FIELDS[token] || token.replace(/_/g, " ");

  function humanizeEvidence(text) {
    return String(text)
      .replace(/COULD NOT VERIFY oracle_model_risk:/g, "oracle model could not be verified:")
      .replace(/missing field: ([a-z0-9_]+)/g, (_, field) => `not available: ${snakeToWords(field)}`)
      .replace(/returned updatedAt == 0/g, "reported no update time")
      // A quoted feed list, ['a', 'b'], becomes "a; b"; an entry 0x…=genuine_feed:heartbeat_undocumented
      // reads as "0x… genuine feed (heartbeat undocumented)".
      .replace(/\['([^\]]*)'\]/g, (_, inner) => inner.split("', '").join("; "))
      .replace(/(0x[0-9a-fA-F]{40})=([a-z_]+):([a-z0-9_]+)/g, (_, address, model, behaviour) =>
        `${address} ${snakeToWords(model)} (${snakeToWords(behaviour)})`)
      .replace(/\b([a-z]+(?:_[a-z0-9]+)+)\b/g, (token) => snakeToWords(token));
  }

  function explainReason(reason, row) {
    return humanizeEvidence(explain(reason, row));
  }

  function explain(value, row) {
    const raw = String(value == null ? "UNKNOWN" : value);
    if (STATE_COPY[raw]) return stateText(raw);
    return raw.replace(EXPLAIN_PATTERN, (code) => {
      const prefix = code.match(REASON_PREFIX)?.[1];
      if (!prefix) return STATE_COPY[code] ? stateLabel(code) : code;
      const context = row || {verdict: prefix.startsWith("NO BID") ? "NO BID" : prefix};
      const label = scoreLabel(context).replace(/^Score (\d+\/100)$/, "Fenorium Score $1");
      const tag = reasonBasis(code);
      return `${label}${tag ? ` (${tag})` : ""} — `;
    });
  }

  // `title` is overridable so a chip can carry phase-specific wording while keeping the class
  // its status implies: PENDING covers both pre-submission and receipt polling, and the tooltip
  // otherwise told the reader nothing was submitted beside a hash that already existed.
  function chip(value, label = stateLabel(value), title = stateText(value)) {
    const safe = textOr(value, "UNKNOWN");
    return `<span class="chip ${slug(safe)}" title="${esc(title)}"><span class="chip-marker" aria-hidden="true"></span><span>${esc(label)}</span></span>`;
  }

  function monogram(value) {
    const words = String(value || "")
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .split(/[^A-Za-z0-9]+/)
      .filter(Boolean);
    if (!words.length) return "?";
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return `${words[0][0]}${words[1][0]}`.toUpperCase();
  }

  // A mark is an identity claim. `alternates` may only carry other identities the row itself
  // asserts -- its issuer, its venue -- so a missing token mark falls back to the brand that
  // issued it and never to a lookalike ticker.
  function markFor(kind, rawValue) {
    const raw = textOr(rawValue, "");
    if (!raw) return null;
    const group = ICONS[`${kind}s`] || {};
    const key = kind === "token" && /(?:^|[^A-Za-z0-9])USDC(?:$|[^A-Za-z0-9])/i.test(raw) ? "USDC" : raw;
    return group[key] || null;
  }

  function identity(kind, rawValue, suppliedLabel = null, alternates = []) {
    const raw = textOr(rawValue, "UNKNOWN");
    let icon = markFor(kind, raw);
    // An alternate supplies a picture, never a name: a row labelled OUSG keeps that label even
    // when the mark it borrows is its issuer's.
    const ownLabel = icon?.label || null;
    for (const [altKind, altValue] of alternates) {
      if (icon) break;
      icon = markFor(altKind, altValue);
    }
    const presentationLabel = suppliedLabel !== null
      ? suppliedLabel
      : (kind === "venue" ? ICONS.venueLabels?.[raw] : null) || ownLabel || raw;
    const rawDisclosure = presentationLabel !== raw
      ? `<span class="sr-only">Raw ${esc(kind)}: ${esc(raw)}.</span>`
      : "";
    const iconMarkup = icon
      ? `<span class="identity-icon identity-logo" aria-hidden="true"><img src="${esc(icon.src)}" alt="" width="20" height="20"></span>`
      : `<span class="identity-icon identity-monogram" aria-hidden="true">${esc(monogram(raw))}</span>`;
    const labelMarkup = presentationLabel
      ? `<span class="identity-label">${esc(presentationLabel)}</span>`
      : "";
    return `<span class="identity-cell" title="Raw ${esc(kind)}: ${esc(raw)}">${iconMarkup}${labelMarkup}${rawDisclosure}</span>`;
  }

  function availability(state) {
    const notApplicable = state?.status === "N/A";
    const status = notApplicable ? "N/A" : "UNKNOWN";
    const detail = notApplicable ? "Not applicable" : "Not measured";
    const className = notApplicable ? "not-applicable" : "not-measured";
    return `<span class="availability ${className}" title="${esc(stateText(status))}"><b>${status}</b><small>${detail}</small></span>`;
  }

  function typeIndicator(category) {
    const normalized = String(category || "unknown").toLowerCase();
    const label = P.MARKET_CATEGORY_LABELS?.[normalized] || "Unknown type";
    return `<span class="type-indicator type-${slug(normalized)}"><span aria-hidden="true">${esc(monogram(label).slice(0, 1))}</span>${esc(label)}</span>`;
  }

  function meta(label, value, extraClass = "") {
    return `<div class="kv ${extraClass}"><span>${esc(label)}</span><b>${esc(explain(textOr(value)))}</b></div>`;
  }

  // Detail panels show the short form of a state and keep its sentence as the tooltip; a value the
  // engine did not establish reads "Not available", with the reason one hover away.
  function fact(label, value, extraClass = "") {
    const raw = String(textOr(value));
    const missing = raw === NOT_COMPUTABLE || raw === "UNKNOWN";
    const copy = STATE_COPY[raw];
    const text = missing ? "Not available" : copy ? copy[0] : explain(raw);
    const title = missing || copy ? stateText(raw) : "";
    const classes = ["kv", extraClass, missing ? "kv-missing" : ""].filter(Boolean).join(" ");
    return `<div class="${classes}"><span>${esc(label)}</span><b${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</b></div>`;
  }

  // One provenance line per panel: the sources that were recorded, then the valued fields whose
  // source was not, instead of a "source · UNKNOWN" caption under every row.
  function sourcesNote(fields) {
    const valued = fields.filter((field) => field[2] !== false);
    const recorded = valued.filter((field) => field[1] != null && field[1] !== "");
    const unrecorded = valued.filter((field) => field[1] == null || field[1] === "");
    const parts = recorded.map((field) => `${field[0]} source · ${explain(field[1])}`);
    if (unrecorded.length) parts.push(`No source recorded for ${unrecorded.map((field) => field[0].toLowerCase()).join(", ")}`);
    return parts.length ? `<p class="cell-sub panel-sources">${esc(parts.join(". "))}.</p>` : "";
  }

  function categoryLabel(category) {
    if (category == null || category === "") return category;
    return P.MARKET_CATEGORY_LABELS?.[String(category).toLowerCase()] || category;
  }

  function utcStamp(value) {
    const stamp = value == null || value === "" ? NaN : Date.parse(value);
    return Number.isNaN(stamp) ? value : new Date(stamp).toISOString().replace("T", " ").slice(0, 16) + " UTC";
  }

  function valueOf(position) {
    if (!position) return null;
    const direct = asBigInt(position.value_usd_micros);
    if (direct !== null) return direct.toString();
    if (typeof P.usdMicrosFromRaw === "function") {
      return P.usdMicrosFromRaw(position.raw_units, position.decimals, position.price_micros);
    }
    return null;
  }

  function renderMetrics(snapshot, nav, coverage, capital) {
    const element = $("portfolio-metrics");
    if (!element) return;
    const positions = snapshot.positions || [];
    const allocation = Array.isArray(snapshot.allocation?.buckets)
      ? snapshot.allocation.buckets.map((bucket) => ({
          label: bucket.category,
          value_usd_micros: bucket.value_usd_micros,
          share_bps: bucket.weight_bps,
          unknown: bucket.value_usd_micros == null,
        }))
      : (typeof P.allocation === "function" ? P.allocation(positions) : []);
    const priced = allocation.filter((row) => !row.unknown).reduce((sum, row) => {
      const amount = asBigInt(row.value_usd_micros);
      return amount === null ? sum : sum + amount;
    }, 0n);
    const fixedIncome = allocation.find((row) => String(row.label).toLowerCase() === "fixed_income");
    const fixedIncomeShare = fixedIncome && priced
      ? Number(asBigInt(fixedIncome.value_usd_micros) * 10000n / priced)
      : null;
    const unresolvedCount = Number(coverage?.unresolved_positions || 0);
    const navDisplay = unresolvedCount > 0 && asBigInt(nav?.value_usd_micros) === 0n
      ? NOT_COMPUTABLE
      : usd(nav?.value_usd_micros);
    const capitalDisplay = capital?.status === "COMPUTABLE"
      ? usd(capital.value_usd_micros)
      : NOT_COMPUTABLE;
    const metrics = [
      ["Priced NAV (Lower Bound)", navDisplay, "Unresolved positions remain visible."],
      ["Valuation Coverage", coverage?.value_coverage_bps == null ? NOT_COMPUTABLE : pct(coverage.value_coverage_bps), `${coverage?.priced_positions || 0} / ${coverage?.position_count || 0} positions aligned`],
      ["Fixed-Income Allocation", fixedIncomeShare == null ? NOT_COMPUTABLE : pct(fixedIncomeShare), "of priced portfolio"],
      ["Deployable Stablecoins", capitalDisplay, `Across ${capital?.position_ids?.length || 0} assets`],
      ["Unresolved Positions", String(coverage?.unresolved_positions || 0),
        coverage?.discovery_complete === false
          // A scan that stopped early and one that finished look identical in a count, and only
          // one of them is a statement about the wallet.
          ? "The search for holdings outside the registry did not finish"
          : coverage?.value_coverage_bps == null ? "Share of total portfolio is NOT COMPUTABLE" : "Share of total portfolio"],
    ];
    element.innerHTML = metrics.map((metric) => `
      <div class="metric-card">
        <div class="label">${esc(metric[0])}</div>
        <div class="value ${metric[1] === NOT_COMPUTABLE ? "value-unavailable" : ""}">${esc(metric[1])}</div>
        <div class="detail">${esc(metric[2])}</div>
        ${metric[0] === "Valuation Coverage" && coverage?.value_coverage_bps != null
          ? `<meter min="0" max="10000" value="${Number(coverage.value_coverage_bps)}" aria-label="Valuation coverage">${esc(pct(coverage.value_coverage_bps))}</meter>`
          : ""}
      </div>`).join("");

    const snapshotStatus = $("snapshot-status");
    if (snapshotStatus) snapshotStatus.textContent = `As of ${snapshot.provenance?.as_of || "UNKNOWN"}`;
    const fixtureLabel = $("fixture-label");
    if (fixtureLabel) fixtureLabel.textContent = explain(snapshot.provenance?.label || "LOCAL_DEMO_FIXTURE / illustrative only");
    const provenance = $("snapshot-provenance");
    if (provenance) {
      provenance.innerHTML = meta("Data mode", snapshot.provenance?.label || "UNKNOWN")
        + meta("Discovery scope", snapshot.scope || "REGISTERED_ASSETS_ONLY")
        + (snapshot.discovery?.widened
          ? meta("Search outside the registry",
                 snapshot.discovery.complete ? "finished" : "did not finish")
          : "")
        + meta("Source", snapshot.provenance?.source || "UNKNOWN");
    }
  }

  function renderAllocation(positions, analysis = null) {
    const element = $("allocation-panel");
    if (!element || typeof P.allocation !== "function") return;
    const rows = Array.isArray(analysis?.buckets)
      ? analysis.buckets.map((bucket) => ({
          label: bucket.category,
          value_usd_micros: bucket.value_usd_micros,
          share_bps: bucket.weight_bps,
          position_count: bucket.position_ids?.length || 0,
          unknown: bucket.value_usd_micros == null,
        }))
      : P.allocation(positions);
    element.innerHTML = rows.length
      ? `<div class="allocation-list">${rows.map((row) => `
          <div class="allocation-row">
            <div class="allocation-label"><b>${esc(row.unknown ? "Other / unresolved value" : stateLabel(row.label))}</b><small>${esc(usd(row.value_usd_micros))}</small></div>
            <div class="allocation-track" role="presentation"><span class="${row.unknown ? "unknown" : ""}" style="width:${row.share_bps == null ? 0 : Math.min(100, Math.max(0, Number(row.share_bps) / 100))}%"></span></div>
            <strong>${row.share_bps == null ? NOT_COMPUTABLE : esc(pct(row.share_bps))}</strong>
          </div>`).join("")}</div>
        <p class="cell-sub allocation-note">Unknown value remains visible and is excluded from the priced share denominator.</p>`
      : `<div class="empty">No positions detected.</div>`;
  }

  function chainName(chainId) {
    const match = (P.REGISTRY_CHAINS || P.CHAINS || []).find((chain) => chain.chain_id === chainId);
    return match?.name || (chainId == null ? NOT_COMPUTABLE : `Chain ${chainId}`);
  }


  // Every rate carries a kind in the universe; the terminal must not present them as one thing.
  // The compact table shares its row with the P&L card, and the full sentence pushed Maturity
  // out of the panel. A yield still never appears without its kind -- the word stays, and the
  // sentence it abbreviates is on the cell, so nothing about the distinction is lost.
  function rateKindWord(row) {
    if (row?.chain_id === 84532) return "declared";
    return {
      executable: "executable", indicative: "indicative", realized: "realized",
      issuer_quoted: "issuer quoted", declared_demo: "declared",
    }[String(row?.rate_kind || "")] || "kind UNKNOWN";
  }

  function rateKindNote(row) {
    if (row?.chain_id === 84532) return "Declared testnet yield";
    return {
      executable: "Executable quote",
      indicative: "Indicative · not shown obtainable",
      realized: "Realized · trailing observation, not an offer",
      issuer_quoted: "Issuer quoted · not verified by Fenorium",
      declared_demo: "Declared testnet yield",
    }[String(row?.rate_kind || "")] || "Rate kind UNKNOWN";
  }

  function tokenQuantity(rawUnits, decimals) {
    const raw = asBigInt(rawUnits);
    const places = Number(decimals);
    if (raw === null || !Number.isSafeInteger(places) || places < 0 || places > 255) {
      return NOT_COMPUTABLE;
    }
    const sign = raw < 0n ? "-" : "";
    const absolute = raw < 0n ? -raw : raw;
    const scale = 10n ** BigInt(places);
    const whole = (absolute / scale).toLocaleString("en-US");
    const fraction = places
      ? (absolute % scale).toString().padStart(places, "0").replace(/0+$/, "")
      : "";
    return `${sign}${whole}${fraction ? `.${fraction}` : ""}`;
  }

  function renderDeployable(capital, positions = []) {
    const element = $("deployable-panel");
    if (!element) return;
    const ids = new Set(capital?.position_ids || []);
    const rows = positions.filter((position) => ids.has(position.id || position.symbol));
    element.innerHTML = `<div class="asset-list">${rows.map((position) => `
      <div class="asset-row">
        <span><b>${esc(position.symbol || position.name)}</b><small class="cell-sub"> · ${esc(chainName(position.chain_id))}</small></span>
        <b>${esc(usd(valueOf(position)))}</b>
      </div>`).join("") || `<div class="empty">No supported deployable assets.</div>`}</div>
      <p class="cell-sub">${esc(capital?.assumption || NOT_COMPUTABLE)}</p>`;
  }

  function renderChains(snapshot) {
    const element = $("chain-statuses");
    if (!element) return;
    element.innerHTML = (snapshot.chains || []).map((chain) => `
      <div class="chain-item">
        <b>${esc(chainName(chain.chain_id))}</b> · ${esc(stateText(chain.rpc_status || "PENDING"))}
        <div class="cell-sub mono">${esc(chain.finality || "finalized")} ${esc(chain.block_ref || chain.finalized_block_ref || "not queried")} · ${esc(chain.source_timestamp || "timestamp unavailable")}</div>
      </div>`).join("");
  }

  function renderCoverage(universe, positions) {
    const element = $("coverage-panel");
    if (!element) return;
    const fixedIncome = positions.filter((position) => position.asset_type === "fixed_income");
    const dated = fixedIncome.filter((position) => position.maturity);
    const value = fixedIncome.reduce((sum, position) => {
      const amount = asBigInt(valueOf(position));
      return amount === null ? sum : sum + amount;
    }, 0n);
    element.innerHTML = meta("Market Value Lower Bound", usd(value.toString()))
      + meta("Weighted Avg. Maturity", dated.length ? NOT_COMPUTABLE : "INSUFFICIENT COVERAGE")
      + meta("Valuation Coverage", fixedIncome.length && fixedIncome.every((position) => valueOf(position) != null) ? "100.00%" : "INSUFFICIENT COVERAGE")
      + meta("Positions", String(fixedIncome.length))
      + `<div class="cell-sub coverage-note">Maturity distribution: ${dated.length ? "verified dates shown in asset detail." : "INSUFFICIENT CASHFLOW DATA"}</div>`;
  }

  function relative(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return "age unavailable";
    const minutes = Math.floor(Number(seconds) / 60);
    const hours = Math.floor(Number(seconds) / 3600);
    return hours < 1 ? `${minutes}m ago` : hours < 48 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
  }

  function freshness(metadata, error) {
    const element = $("universe-status");
    if (!element) return;
    if (error) {
      element.className = "freshness-banner error";
      element.innerHTML = `<span class="status-symbol" aria-hidden="true">!</span><strong>Fixed Income universe unavailable</strong><span class="meta">${esc(stateText(error.reason_code || "UNIVERSE_REQUEST_FAILED"))} · ${esc(error.message || "endpoint failed")} No static or empty-market fallback was used.</span>`;
      return;
    }
    if (!metadata) {
      element.className = "freshness-banner error";
      element.innerHTML = `<span class="status-symbol" aria-hidden="true">!</span><strong>Fixed Income universe unavailable</strong><span class="meta">Reload the market to request freshness metadata; it was not supplied. No static or empty-market fallback was used.</span>`;
      return;
    }
    const status = String(metadata.freshness?.status || metadata.freshness || "UNKNOWN").toUpperCase();
    const warning = status === "EXPIRED"
      ? stateText("EXPIRED") + " "
      : status === "STALE"
        ? stateText("STALE") + " "
        : stateText(status) + " ";
    const inputAge = metadata.input_freshness?.age_seconds;
    const inputDetail = inputAge == null
      ? ""
      : ` · Newest consumed cache input ${relative(inputAge)}`;
    // A settled universe is not news. It stays one quiet line carrying the same facts, and only
    // becomes a banner when the data is stale or expired -- which is the case worth interrupting
    // for, and the only case where these curve values must not be used for a decision.
    const unsettled = status === "EXPIRED" || status === "STALE";
    element.className = `freshness-banner ${unsettled ? "stale" : "settled"}`;
    element.innerHTML = unsettled
      ? `<span class="status-symbol" aria-hidden="true">●</span><strong>Fixed-Income Universe is ${esc(stateLabel(status))}</strong><span class="meta">${esc(warning)}Cached snapshot · Generated ${esc(metadata.generated_at || metadata.as_of)} (${esc(relative(metadata.freshness?.age_seconds))})${esc(inputDetail)}</span><button class="method-link ghost" type="button" data-utility="methodology">Universe status &amp; methodology ↗</button>`
      : `<span class="status-symbol" aria-hidden="true">●</span><span class="meta">Universe ${esc(stateLabel(status))} · ${esc(warning)}Generated ${esc(metadata.generated_at || metadata.as_of)} (${esc(relative(metadata.freshness?.age_seconds))})${esc(inputDetail)}</span><button class="method-link ghost" type="button" data-utility="methodology">Universe status &amp; methodology ↗</button>`;
  }

  function renderFixedIncome(universe, metadata, error) {
    freshness(metadata, error);
    const body = $("fixed-income-table");
    if (!body) return;
    if (error) {
      body.innerHTML = "";
      return;
    }
    body.innerHTML = (universe || []).map((row) => `<tr>
      <td>${scoreChip(row)}</td>
      <td><b>${esc(row.name)}</b><span class="sr-only">source: ${esc(row.source || metadata?.source)} · as_of: ${esc(row.as_of || metadata?.as_of || metadata?.generated_at)}</span></td>
      <td>${esc(row.rate_bps == null ? NOT_COMPUTABLE : pct(row.rate_bps))}</td>
      <td>${esc(row.rating?.computable === true ? pct(row.rating.net_bps) : NOT_COMPUTABLE)}${row.rating?.computable === true ? costFigureContext(row) : ""}</td>
    </tr>`).join("");
  }

  function numericMetric(row, kind) {
    const computable = row?.rating?.computable === true;
    if (kind === "headline") return row?.rate_bps == null ? NOT_COMPUTABLE : pct(row.rate_bps);
    if (kind === "net") return computable && row.rating.net_bps != null ? pct(row.rating.net_bps) : NOT_COMPUTABLE;
    if (kind === "execution") return computable && row.rating.execution_bps != null ? pct(row.rating.execution_bps) : NOT_COMPUTABLE;
    if (kind === "base_fee") return computable && row.rating.base_fee_annualized_bps != null ? pct(row.rating.base_fee_annualized_bps) : NOT_COMPUTABLE;
    if (kind === "modelled_impact") return computable && row.rating.modelled_impact_annualized_bps != null ? pct(row.rating.modelled_impact_annualized_bps) : NOT_COMPUTABLE;
    if (kind === "credit") return computable && row.rating.credit_haircut_bps != null ? pct(row.rating.credit_haircut_bps) : NOT_COMPUTABLE;
    if (kind === "depth") return row?.depth_usd == null ? NOT_COMPUTABLE : usdN(row.depth_usd);
    if (kind === "days") return P.rowMatured(row) ? "Matured" : row?.days_to_maturity == null ? NOT_COMPUTABLE : Number(row.days_to_maturity).toFixed(1);
    return NOT_COMPUTABLE;
  }

  function recordedSource(value) {
    return value == null || value === "" ? "UNKNOWN · Not measured" : explain(value);
  }

  function sourceContext(label, value) {
    return `<small class="cell-sub">${esc(label)} · ${esc(recordedSource(value))}</small>`;
  }

  function holdingOf(row) {
    return typeof P.holdingPeriod === "function" ? P.holdingPeriod(row) : null;
  }

  function costFigureContext(row) {
    const model = recordedSource(row?.rating?.impact_model);
    return `<small class="cell-sub">${esc(model)} · not a live executable quote<br>Depth source · ${esc(recordedSource(row?.depth_source))}</small>`;
  }

  function notionalLabel(sizeUsd) {
    const size = Number.isInteger(Number(sizeUsd)) ? Number(sizeUsd) : 250_000;
    return `$${size.toLocaleString("en-US")}`;
  }

  function executionMode(row) {
    if (row?.simulation === true) return "Simulated · nothing to execute";
    if (row?.is_demo === true) return "Base Sepolia testnet demo purchase";
    return deployment.execution ? "Mainnet · route on request" : "Mainnet execution disabled";
  }

  function demoEconomicsNotice(row) {
    if (row?.is_demo !== true || row?.rate_kind !== "declared_demo") return "";
    const priceSources = (Array.isArray(row.price_observations) ? row.price_observations : [])
      .map((source) => `${source.source_id || "UNKNOWN"} · ${source.method_version || "UNKNOWN"}`)
      .join("; ") || "UNKNOWN · Not declared";
    return `<div class="inspector-demo-note demo-economics-notice">
      <b>Declared synthetic economics · Base Sepolia testnet only</b>
      <span>Rate, maturity, tier, withdrawable-depth scenario, and price inputs are demo declarations. They are not market observations, executable quotes, issuer publications, realized returns, or your money.</span>
      <small>Price validity · ${esc(recordedSource(row.price_valid_until))} · ${esc(recordedSource(row.price_validity_source))}</small>
      <small>Price declarations · ${esc(priceSources)}</small>
    </div>`;
  }

  function inspector(row, metadata, sizeUsd) {
    const size = notionalLabel(sizeUsd);
    const reason = row?.reason || row?.rejection || NOT_COMPUTABLE;
    const demoNotice = row?.simulation === true
      ? `<div class="inspector-demo-note"><b>Simulated · preview catalog</b><span>A synthetic row for the screens: not deployed on any chain, nothing to buy or sell.</span></div>`
      : row?.is_demo === true
      ? `<div class="inspector-demo-note"><b>Base Sepolia · testnet · demo instrument</b><span>A Base Sepolia testnet instrument: bought with faucet mUSDC and sold back through its desk.</span></div>`
      : deployment.execution
        ? `<div class="inspector-demo-note"><b>Mainnet · route on request</b><span>Connect a wallet and request a route from a token you hold on a supported chain. The server checks the instrument and the route first, and says why when it cannot offer one. Every fee is shown before you sign.</span></div>`
        : `<div class="inspector-demo-note muted"><b>Mainnet execution disabled</b><span>This universe row is read-only; no transaction action is available.</span></div>`;
    return `<header class="inspector-header">
      <div><h2>${esc(row.name)}</h2><p class="cell-sub">${esc(row.venue)} · ${esc(row.category)} · ${esc(row.chain)}</p></div>
      <button class="icon-button" id="inspector-close" type="button" aria-label="Close instrument inspector">×</button>
    </header>
    <p class="inspector-verdict">${scoreChip(row)}</p>
    ${demoNotice}
    ${demoEconomicsNotice(row)}
    <button class="ghost inspector-summary-action" type="button" data-open="${esc(row._index)}">${row?.is_demo === true ? "Open full instrument &amp; demo purchase →" : "Open full instrument view →"}</button>
    <div class="metric-mini-grid">
      <div class="metric-mini"><span>Headline Yield</span><b>${esc(numericMetric(row, "headline"))}</b>${sourceContext("Rate source", row.rate_source)}</div>
      <div class="metric-mini"><span>Net @ ${esc(size)}${holdingOf(row) ? " · to maturity" : ""}</span><b>${esc(holdingOf(row) ? pct(holdingOf(row).net_bps) : numericMetric(row, "net"))}</b>${holdingOf(row) ? `<small class="cell-sub">${esc(Number(holdingOf(row).days).toFixed(1))} days remain · annualized ${esc(numericMetric(row, "net"))}</small>` : ""}${costFigureContext(row)}</div>
      <div class="metric-mini"><span>Reported depth · exitable or acquirable</span><b>${esc(numericMetric(row, "depth"))}</b>${sourceContext("Depth source", row.depth_source)}</div>
      <div class="metric-mini"><span>Tier</span><b>${esc(textOr(row.tier))}</b>${sourceContext("Tier source", row.tier_source)}</div>
    </div>
    ${meta("Rate Kind", textOr(row.rate_kind))}
    ${meta("Maturity", textOr(row.maturity))}
    ${sourceContext("Maturity source", row.maturity_source)}
    ${meta("Days to Maturity", numericMetric(row, "days"))}
    ${sourceContext("Days source", row.days_to_maturity_source)}
    ${meta("Permission", textOr(row.gate?.status))}
    ${meta("Execution", executionMode(row))}
    <p class="cell-sub">Execution cost includes a stated flat 30 bp base fee plus <b>${esc(recordedSource(row.rating?.impact_model))}</b> impact. It is not measured slippage or a live executable quote.<br>Depth source · ${esc(recordedSource(row.depth_source))}</p>
    <h2 class="section-label">Why this score</h2>
    <p class="full-reason">${esc(explainReason(reason, row))}</p>
    ${row?.rating?.computable === false && row.rating.refusal ? `<p class="cell-sub rating-refusal">Rating refusal: ${esc(ratingRefusal(row.rating.refusal))}</p>` : ""}
    <h2 class="section-label">Evidence &amp; Freshness</h2>
    <p class="cell-sub">Universe source: ${esc(metadata?.source)}<br>Universe as of: ${esc(metadata?.generated_at)}<br>Per-instrument freshness: UNKNOWN · Not measured. Check the source timestamp.</p>`;
  }

  function ratingRefusal(value) {
    const raw = String(value || "Required evidence is missing");
    const action = raw.includes("below the intended") ? "Reduce the selected amount."
      : raw.includes("holding period") ? "Review the holding-period assumption."
      : "Review the instrument evidence.";
    return `${action} ${explain(raw)}`;
  }

  function support(cards) {
    return `<div class="support-grid">${cards.map((card) => `
      <div class="support-card"><b>${esc(card[0])}</b><div>${chip(card[1])}</div><p class="cell-sub">${esc(explain(card[2]))}</p></div>`).join("")}</div>`;
  }

  function demoPurchaseCard(row, purchase) {
    const current = purchase || {};
    const readValue = (value) => current.status === "NO_WALLET" || !current.account
      ? "Not read · connect a wallet on Base Sepolia" : value || NOT_COMPUTABLE;
    const status = current.status || "UNCONFIGURED_DEPLOYMENT";
    // Receipt history survives later actions; only describe the current confirmed action as success.
    const confirmedStatus = status === "SUCCESS" || status === "CONFIRMED_STATE_MISMATCH";
    const confirmedKind = current.confirmedOutcome?.kind;
    // Require the CURRENT confirmed action, never a surviving receipt flag. Falling back to
    // purchaseReceiptOutcome when confirmedOutcome is null announced "Your testnet purchase
    // succeeded" during a later approval whose allowance was still short of the quote, with no
    // purchase submitted. app.js sets confirmedOutcome to {kind, hash} on every real success and
    // nulls it otherwise, so the kind is the only trustworthy signal here.
    const confirmedPurchase = confirmedStatus && confirmedKind === "purchase"
      && current.purchaseReceiptOutcome === "CONFIRMED";
    const confirmedFaucet = confirmedStatus && confirmedKind === "faucet"
      && current.faucetReceiptOutcome === "CONFIRMED" && !confirmedPurchase;
    const confirmedSale = confirmedStatus && confirmedKind === "sale"
      && current.saleReceiptOutcome === "CONFIRMED";
    const confirmedAction = confirmedPurchase || confirmedFaucet || confirmedSale;
    const successLabel = confirmedPurchase
      ? "Testnet purchase succeeded"
      : confirmedSale ? "Testnet sale succeeded"
      : confirmedFaucet ? "Testnet mUSDC received"
      : stateLabel(status);
    const successMessage = confirmedPurchase
      ? "Your testnet purchase succeeded. The purchase transaction is confirmed on Base Sepolia."
      : confirmedSale ? "Your testnet sale succeeded. The sale transaction is confirmed on Base Sepolia; the desk paid in mUSDC."
      : confirmedFaucet ? "Your testnet mUSDC request succeeded. The faucet transaction is confirmed on Base Sepolia."
      : status === "INSUFFICIENT_MUSDC" && current.faucetConfigured !== true
        ? "Not enough testnet mUSDC. Contact the demo operator to obtain testnet mUSDC for this quote."
        : stateText(status);
    // Only say a transaction is awaiting confirmation once one actually exists: PENDING also
    // covers the preflight quote refresh and the wait for the wallet, when nothing was submitted.
    // A confirmed faucet or approval leaves its hash in the record, so the hashes cannot answer
    // "is something in flight now". The *_RECEIPT stages are set only after a transaction is
    // submitted and before its receipt resolves, which is exactly this claim.
    const submittedAwaitingReceipt = status === "PENDING"
      && ["APPROVAL_RECEIPT", "PURCHASE_RECEIPT", "FAUCET_RECEIPT", "SALE_APPROVAL_RECEIPT", "SALE_RECEIPT"].includes(current.stage);
    const phaseMessage = submittedAwaitingReceipt ? stateText("AWAITING_RECEIPT") : successMessage;
    const statusDetails = `${phaseMessage} ${confirmedAction && status !== "SUCCESS" ? stateText(status) : ""} ${current.message ? explain(current.message) : ""}`.trim();
    const refreshPrimary = ["CONFIRMED_STATE_MISMATCH", "RPC_ERROR", "REVERT", "USER_REJECTED"].includes(status) || (status === "SUCCESS" && current.readsStale === true);
    const purchaseDisabled = current.matured === true || current.canPurchase !== true;
    const faucetConfigured = current.faucetConfigured === true;
    const faucetDisabled = current.matured === true || !faucetConfigured || current.canFaucet !== true;
    const faucetPrimary = status === "INSUFFICIENT_MUSDC" && !faucetDisabled;
    let needsApproval = true;
    try {
      needsApproval = current.quoteRaw == null || current.allowanceRaw == null || BigInt(current.allowanceRaw) < BigInt(current.quoteRaw);
    } catch (_) {
      needsApproval = true;
    }
    const actionLabel = current.matured === true ? "Buying closed · matured" : needsApproval ? "Approve exact mUSDC" : "Purchase demo instrument";
    const approvalState = current.approvalReceiptOutcome === "REVERTED"
      ? "REVERTED"
      : current.approvalReceiptOutcome === "CONFIRMED"
      ? current.stage === PURCHASE_STAGES.APPROVAL_REFRESH_FAILED && current.readsStale
        ? "CONFIRMED · READ REFRESH FAILED"
        : "CONFIRMED"
      : current.stage === PURCHASE_STAGES.APPROVAL_INDETERMINATE
      ? "UNKNOWN"
      : current.stage === PURCHASE_STAGES.APPROVAL_RECEIPT
      ? "AWAITING_RECEIPT"
      : current.stage === PURCHASE_STAGES.APPROVAL
      ? "PENDING"
      : needsApproval ? "REQUIRED" : "NOT REQUIRED";
    const purchaseState = current.purchaseReceiptOutcome === "REVERTED"
      ? "REVERTED"
      : current.purchaseReceiptOutcome === "CONFIRMED"
      ? "CONFIRMED"
      : current.stage === PURCHASE_STAGES.PURCHASE_INDETERMINATE
      ? "UNKNOWN"
      : current.stage === PURCHASE_STAGES.PURCHASE_RECEIPT
      ? "AWAITING_RECEIPT"
      : current.stage === PURCHASE_STAGES.PURCHASE
      ? "PENDING"
      : "NOT STARTED";
    // The sell-back block exists only where the deployment declares a desk that takes the
    // instrument back; a buy-only sale renders the card exactly as before.
    const sellConfigured = current.sellConfigured === true;
    const sellDisabled = current.canSell !== true;
    let needsInstrumentApproval = true;
    try {
      needsInstrumentApproval = current.amountRaw == null || current.instrumentAllowanceRaw == null
        || BigInt(current.instrumentAllowanceRaw) < BigInt(current.amountRaw);
    } catch (_) {
      needsInstrumentApproval = true;
    }
    const sellLabel = needsInstrumentApproval ? "Approve instrument for sell-back" : "Sell back to the desk";
    const sellPrimary = !sellDisabled && purchaseDisabled && !refreshPrimary && !faucetPrimary;
    const instrumentApprovalState = current.instrumentApprovalReceiptOutcome === "REVERTED"
      ? "REVERTED"
      : current.instrumentApprovalReceiptOutcome === "CONFIRMED"
      ? current.stage === PURCHASE_STAGES.SALE_APPROVAL_REFRESH_FAILED && current.readsStale
        ? "CONFIRMED · READ REFRESH FAILED"
        : "CONFIRMED"
      : current.stage === PURCHASE_STAGES.SALE_APPROVAL_INDETERMINATE
      ? "UNKNOWN"
      : current.stage === PURCHASE_STAGES.SALE_APPROVAL_RECEIPT
      ? "AWAITING_RECEIPT"
      : current.stage === PURCHASE_STAGES.SALE_APPROVAL
      ? "PENDING"
      : needsInstrumentApproval ? "INSTRUMENT_APPROVAL_REQUIRED" : "INSTRUMENT_APPROVAL_NOT_REQUIRED";
    const saleState = current.saleReceiptOutcome === "REVERTED"
      ? "REVERTED"
      : current.saleReceiptOutcome === "CONFIRMED"
      ? "CONFIRMED"
      : current.stage === PURCHASE_STAGES.SALE_INDETERMINATE
      ? "UNKNOWN"
      : current.stage === PURCHASE_STAGES.SALE_RECEIPT
      ? "AWAITING_RECEIPT"
      : current.stage === PURCHASE_STAGES.SALE
      ? "PENDING"
      : "SALE_NOT_STARTED";
    const explorer = current.explorerUrl === null ? null : current.explorerUrl || "https://sepolia.basescan.org";
    const txLink = (label, hash) => hash && explorer
      ? `<a class="tx-link" href="${esc(explorer)}/tx/${esc(hash)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`
      : "";
    return `<section class="purchase-card" aria-labelledby="purchase-title">
      ${current.matured === true ? `<p class="detail-note">Matured on ${esc(current.maturity)}. Buying is closed, as on mainnet, where a matured instrument leaves the market. The desk still buys it back at its quoted price — a sale at the desk's fixed price, not a redemption at an accrued value.</p>` : ""}
      <header class="purchase-header">
        <div><span class="network-badge">Base Sepolia · testnet · demo instrument</span><h2 id="purchase-title">${sellConfigured ? "Buy or sell back" : "Purchase"} ${esc(row.symbol || row.name?.split(" ")[0] || "demo instrument")}</h2></div>
        ${chip(status, status === "CONFIRMED_STATE_MISMATCH" ? stateLabel(status) : successLabel, submittedAwaitingReceipt ? stateText("AWAITING_RECEIPT") : stateText(status))}
      </header>
      <p class="purchase-disclaimer">Connect your wallet on Base Sepolia. ETH on Base Sepolia pays transaction gas; these contracts accept mUSDC for purchases. Get free test mUSDC from the faucet below, approve the quoted amount, then purchase.${sellConfigured ? " Holdings of this instrument can be sold back to the desk at its sell-back quote, paid in mUSDC." : ""} A testnet purchase is not evidence of mainnet price, liquidity, or investment eligibility.</p>
      <div class="purchase-addresses">
        ${meta("Instrument", row.address || NOT_COMPUTABLE, "mono-value")}
        ${meta("Payment token", current.paymentTokenAddress || "NOT CONFIGURED", "mono-value")}
        ${meta(sellConfigured ? "Desk" : "Sale", current.saleAddress || "NOT CONFIGURED", "mono-value")}
      </div>
      <div class="purchase-form">
        <label>Instrument amount · up to six decimal places
          <input id="purchase-amount" data-purchase-amount type="text" inputmode="decimal" value="${esc(current.amountInput || "1")}" aria-describedby="purchase-help">
        </label>
        <div class="purchase-read-grid">
          ${meta("Exact quote · mUSDC", readValue(current.quoteHuman))}
          ${meta("mUSDC balance", readValue(current.paymentBalanceHuman))}
          ${meta("Allowance", readValue(current.allowanceHuman))}
          ${meta("Instrument balance", readValue(current.instrumentBalanceHuman))}
          ${sellConfigured ? meta("Sell-back quote · mUSDC", readValue(current.sellQuoteHuman)) : ""}
          ${sellConfigured ? meta("Instrument allowance", readValue(current.instrumentAllowanceHuman)) : ""}
          ${meta("Eligibility", current.readsStale === true
            ? "NOT CURRENT · refresh failed"
            : current.gated
              ? current.verified === true
                ? "VERIFIED"
                : current.verified === false
                  ? "UNVERIFIED"
                  : "UNKNOWN · NOT READ"
              : "PERMISSIONLESS")}
        </div>
      </div>
      <p class="purchase-help" id="purchase-help">${esc(statusDetails)}</p>
      ${current.readsStale === true ? `<p class="purchase-error" role="status">On-chain reads are not current. Balance, allowance, quote, and eligibility values are hidden until refresh succeeds.</p>` : ""}
      <p class="purchase-error" role="alert">${current.errorMessage ? esc(explain(current.errorMessage)) : ""}</p>
      <ol class="purchase-steps" aria-label="Purchase signing steps">
        <li><b>1 · Allowance</b><span>${esc(explain(approvalState))}</span><small>One wallet signature; receipt and allowance are confirmed before continuing.</small></li>
        <li><b>2 · Purchase</b><span>${esc(explain(purchaseState))}</span><small>A separate wallet signature; balance refresh waits for the purchase receipt.</small></li>
        ${sellConfigured ? `<li><b>3 · Instrument allowance</b><span>${esc(explain(instrumentApprovalState))}</span><small>One wallet signature lets the desk take the instrument amount; receipt and allowance are confirmed before continuing.</small></li>
        <li><b>4 · Sale</b><span>${esc(explain(saleState))}</span><small>A separate wallet signature; the desk pays at least the displayed sell-back quote in mUSDC.</small></li>` : ""}
      </ol>
      <div class="purchase-actions">
        ${current.needsChainSwitch ? `<button class="primary-action" type="button" data-purchase-action="switch">Switch to Base Sepolia</button>` : ""}
        <button class="${refreshPrimary || faucetPrimary || sellPrimary ? "ghost" : "primary-action"}" type="button" data-purchase-action="purchase" ${purchaseDisabled ? "disabled" : ""}>${esc(actionLabel)}</button>
        ${sellConfigured ? `<button class="${sellPrimary ? "primary-action" : "ghost"}" type="button" data-purchase-action="sell" ${sellDisabled ? "disabled" : ""}>${esc(sellLabel)}</button>` : ""}
        <span class="action-note"><b>Base Sepolia · testnet · demo instrument</b> · never mainnet execution.</span>
        <button class="${refreshPrimary ? "primary-action" : "ghost"}" type="button" data-purchase-action="refresh">Refresh on-chain reads</button>
        ${faucetConfigured
          ? `<button class="${faucetPrimary ? "primary-action" : "ghost"}" type="button" data-purchase-action="faucet" ${faucetDisabled ? "disabled" : ""}>Get mUSDC from faucet</button>`
          : `<span class="action-note">Testnet mUSDC faucet unavailable. Contact the demo operator.</span>`}
      </div>
      <p class="faucet-note">The faucet supplies testnet mUSDC only; keep ETH on Base Sepolia in your wallet to pay gas for the faucet, approval and purchase transactions. ETH on Ethereum Sepolia is on a different network.</p>
      <div class="transaction-links">${txLink("Approval transaction", current.approvalTx)} ${txLink("Purchase transaction", current.purchaseTx)} ${txLink("Faucet transaction", current.faucetTx)} ${txLink("Instrument approval transaction", current.instrumentApprovalTx)} ${txLink("Sale transaction", current.saleTx)}</div>
    </section>`;
  }

  // A row of the testnet preview catalog: synthetic figures for the screens, deployed nowhere.
  function simulatedInstrumentNotice(row) {
    return `<section class="panel execution-notice simulated-notice">
      <header><div><p class="eyebrow">Preview catalog</p><h2>Simulated instrument</h2></div>${chip("SIMULATED", "Simulated", "A synthetic preview row, not a deployed contract.")}</header>
      <p>${esc(row?.name || "This instrument")} is a synthetic entry of the testnet preview catalog. Its rate, depth, score and price are fixtures for the screens; it is not deployed on any chain, has no contract address, and cannot be bought or sold. Only the Base Sepolia instruments are on-chain.</p>
    </section>`;
  }
  function readOnlyExecutionNotice(row) {
    if (deployment.execution) {
      return `<section class="panel execution-notice">
      <header><div><p class="eyebrow">Execution boundary</p><h2>Mainnet · route on request</h2></div>${chip("ENABLED")}</header>
      <p>${esc(row?.is_demo === true ? "This is a Base Sepolia testnet demo instrument; it has no mainnet route. Its demo purchase is on Summary." : "Purchases go through a route this server fetches and checks for your connected wallet, delivering on this instrument's chain. Approvals are for the exact amount, and the route is refused, with its reason, whenever it cannot be offered.")}</p>
      <div class="blocker-list"><span>${esc(stateText("DESTINATION_FAILURE_EXPERIMENT"))}</span></div>
      <p class="cell-sub">${esc(deployment.fees ? "Every fee, LI.FI's and Fenorium's routing fee, is shown with the route before you sign." : "Every fee is shown with the route before you sign.")} A route is not a completed transaction.</p>
    </section>`;
    }
    return `<section class="panel execution-notice">
      <header><div><p class="eyebrow">Execution boundary</p><h2>Mainnet execution disabled</h2></div>${chip("DISABLED")}</header>
      <p>${esc(row?.is_demo === true ? "This instrument is bought and sold on Base Sepolia through its desk; mainnet execution stays disabled." : "This universe instrument is read-only. No mainnet transaction action is available.")}</p>
      <div class="blocker-list">
        ${["LEGAL_REVIEW_REQUIRED", "MISSING_EXECUTION_QUOTE", "DESTINATION_FAILURE_EXPERIMENT"].map((code) => `<span>${esc(stateText(code))}</span>`).join("")}
      </div>
      <p class="cell-sub">Mainnet execution and fees are disabled. A quote is not a completed transaction.</p>
    </section>`;
  }

  function summary(row, metadata, purchase, sizeUsd) {
    const computable = row.rating?.computable === true;
    const hp = holdingOf(row);
    const daysLabel = hp ? `${Number(hp.days).toFixed(1)} days` : "";
    const net = hp ? pct(hp.net_bps) : numericMetric(row, "net");
    const size = notionalLabel(sizeUsd);
    const model = recordedSource(row.rating?.impact_model);
    const modelNote = `${model} · not a live executable quote`;
    const depthSource = recordedSource(row.depth_source);
    const tier = String(textOr(row.tier));
    const metrics = [
      ["Fenorium Score", scoreChip(row), "", ""],
      ["Headline Yield", numericMetric(row, "headline"), "gross rate", ""],
      [`Net Yield @ ${size}`, net, hp ? `to maturity · ${daysLabel}` : "after modelled cost",
        hp ? `Annualized ${numericMetric(row, "net")} · ${modelNote}` : modelNote],
      ["Execution Cost", hp ? pct(hp.cost_bps) : numericMetric(row, "execution"), hp ? "to maturity · entry cost + impact" : "modelled · annualized",
        hp ? `Annualized ${numericMetric(row, "execution")} · ${modelNote}` : modelNote],
      ["Reported depth", numericMetric(row, "depth"), "exitable or acquirable", `Depth source · ${depthSource}`],
      ["Tier", tier === NOT_COMPUTABLE ? NOT_COMPUTABLE : esc(stateLabel(tier)), "classification, not a guarantee", stateText(tier)],
      ["Days to Maturity", numericMetric(row, "days"), row.maturity ? `matures ${row.maturity}` : "", ""],
    ];
    const tiles = metrics.map(([label, value, note, title]) => {
      const missing = value === NOT_COMPUTABLE;
      const tooltip = missing ? stateText(NOT_COMPUTABLE) : title;
      return `<div class="metric-card"${tooltip ? ` title="${esc(tooltip)}"` : ""}><div class="label">${esc(label)}</div><div class="value${missing ? " value-unavailable" : ""}">${missing ? "Not available" : value}</div>${note ? `<div class="detail">${esc(note)}</div>` : ""}</div>`;
    }).join("");
    const parameters = computable ? `<details class="impact-parameters"><summary>Model parameters</summary><p>${esc(row.rating.impact_coefficient_bps)} bp × (size / reported depth)<sup>${esc(row.rating.impact_exponent)}</sup> at ${esc(size)} · impact ${esc(row.rating.impact_rounding)} · annualization ${esc(row.rating.annualization_rounding)} · not measured slippage or a live executable quote</p></details>` : "";
    const thesis = computable && hp
      ? `<div class="thesis">
          <div class="thesis-box"><span>YIELD TO MATURITY</span><b>${esc(pct(hp.gross_bps))}</b><small>${esc(pct(row.rating.headline_bps))} − ${esc(numericMetric(row, "credit"))} credit · ${esc(daysLabel)}</small></div>
          <span class="operator" aria-hidden="true">−</span>
          <div class="thesis-box"><span>BASE FEE</span><b>${esc(pct(row.rating.base_fee_bps))}</b><small>${esc(row.rating.base_fee_bps)} bp entry cost</small></div>
          <span class="operator" aria-hidden="true">−</span>
          <div class="thesis-box"><span>MODELLED IMPACT</span><b>${esc(pct(row.rating.modelled_impact_bps))}</b><small>${esc(row.rating.modelled_impact_bps)} bp at this size</small></div>
          <span class="operator" aria-hidden="true">=</span>
          <div class="thesis-box"><span>NET TO MATURITY</span><b>${esc(net)}</b><small>annualized ${esc(numericMetric(row, "net"))}</small></div>
        </div>
        <div class="cell-sub thesis-note"><p>Within ${esc(P.HOLDING_PERIOD_DAYS)} days of maturity the figures are for the ${esc(daysLabel)} that remain, not annualized: a flat entry cost annualized over days is a rate nobody pays. ${esc(model)} Depth source · ${esc(depthSource)}.</p>${parameters}</div>`
      : computable
      ? `<div class="thesis">
          <div class="thesis-box"><span>HEADLINE YIELD</span><b>${esc(pct(row.rating.headline_bps))}</b><small>gross rate at maturity</small></div>
          <span class="operator" aria-hidden="true">−</span>
          <div class="thesis-box"><span>CREDIT HAIRCUT</span><b>${esc(numericMetric(row, "credit"))}</b><small>protocol and credit adjustments</small></div>
          <span class="operator" aria-hidden="true">−</span>
          <div class="thesis-box"><span>BASE FEE</span><b>${esc(numericMetric(row, "base_fee"))}</b><small>${esc(row.rating.base_fee_bps)} bp entry cost · annualized</small></div>
          <span class="operator" aria-hidden="true">−</span>
          <div class="thesis-box"><span>MODELLED IMPACT</span><b>${esc(numericMetric(row, "modelled_impact"))}</b><small>${esc(row.rating.modelled_impact_bps)} bp at this size · annualized</small></div>
          <span class="operator" aria-hidden="true">=</span>
          <div class="thesis-box"><span>NET YIELD @ ${esc(size)}</span><b>${esc(net)}</b><small>modelled · not a live quote</small></div>
        </div>
        <div class="cell-sub thesis-note"><p>${esc(model)} Depth source · ${esc(depthSource)}.</p>${parameters}</div>`
      : `<div class="empty"><b>NET YIELD NOT COMPUTABLE</b><p>${esc(ratingRefusal(row.rating?.refusal || net))}</p><small>A refusal never becomes a number; missing impact is UNKNOWN, not 0.</small></div>`;
    const purchaseMarkup = row.simulation === true ? simulatedInstrumentNotice(row)
      : row.is_demo === true ? demoPurchaseCard(row, purchase) : readOnlyExecutionNotice(row);
    const chainLabel = row.chain && row.chain_id != null ? `${row.chain} · ${row.chain_id}` : row.chain ?? row.chain_id;
    const overview = `<section class="panel"><header><h2>Instrument Overview</h2></header>${fact("Venue", row.venue)}${fact("Chain", chainLabel)}${row.address ? `<div class="kv mono-value"><span>Contract Address</span><b>${esc(row.address)}</b></div>` : fact("Contract Address", "No token address")}${fact("Category", categoryLabel(row.category))}${fact("Rate Kind", row.rate_kind)}${fact("Universe As Of", utcStamp(metadata?.generated_at))}${sourcesNote([["Rate", row.rate_source, row.rate_bps != null]])}</section>`;
    const keyMetrics = `<section class="panel"><header><h2>Key Metrics</h2></header>${fact("Maturity", row.maturity)}${fact("Days to Maturity", numericMetric(row, "days"))}${fact("Tier", row.tier)}${fact("Reported depth · exitable or acquirable", numericMetric(row, "depth"))}${fact("Fund size (TVL)", row.tvl_usd == null ? NOT_COMPUTABLE : usdN(row.tvl_usd))}${fact("Permission", row.gate?.status)}${fact("Execution", executionMode(row))}${sourcesNote([
      ["Maturity", row.maturity_source, row.maturity != null],
      ["Days to maturity", row.days_to_maturity_source, row.days_to_maturity != null],
      ["Tier", row.tier_source, row.tier != null],
      ["Depth", row.depth_source, row.depth_usd != null],
      ["TVL", row.tvl_source, row.tvl_usd != null],
    ])}</section>`;
    return `${demoEconomicsNotice(row)}<div class="kpi-grid asset-metrics">${tiles}</div>
      <section class="panel thesis-panel"><header><h2>Fenorium Thesis</h2><span>Same headline rate is not the same as executable economics.</span></header>${thesis}</section>
      <div class="grid-2 asset-facts">
        ${overview}
        ${keyMetrics}
      </div>
      <section class="panel"><header><h2>Reason</h2></header><p class="full-reason">${esc(explainReason(row.reason || row.rejection || NOT_COMPUTABLE, row))}</p></section>
      ${purchaseMarkup}`;
  }

  function riskCoverage(row) {
    const measured = row.instrument_risk;
    const hasRisk = measured && [measured.volatility_bps, measured.max_drawdown_bps,
      measured.expected_shortfall_95_bps, measured.observations].every((value) => typeof value === "number" && Number.isFinite(value));
    const source = hasRisk ? ` (${measured.source})` : "";
    const duration = row.rate_kind === "declared_demo" && row.maturity && row.days_to_maturity > 0 && row.rate_bps != null
      ? ["Duration", "COMPUTED", `Modified duration ${(row.days_to_maturity / 365 / (1 + row.rate_bps / 10000)).toFixed(2)} years · single redemption at maturity`]
      // Open-ended only where a maturity does not apply to the type; a fixed-term row whose
      // maturity is missing keeps the unsupported reading.
      : !row.maturity && P.marketFieldApplies?.(row, "maturity") === false ? ["Duration", "N/A", "Not applicable: open-ended, no maturity date"]
      : ["Duration", "UNSUPPORTED", "APY cannot create duration."];
    return `<section class="panel"><header><div><p class="eyebrow">Evidence coverage</p><h2>Risk Coverage / Support Matrix</h2></div></header>${support([
      ["Credit", "PARTIAL", row.rating?.computable === true ? "Credit haircut available; no issuer analytics." : "Rating refused."],
      ["Liquidity", row.depth_usd == null ? "MISSING" : "PARTIAL", row.depth_usd == null ? "Reported depth is UNKNOWN. Review the depth evidence." : "Reported depth only — what was shown exitable or acquirable, never fund size."],
      hasRisk ? ["Market", "COMPUTED", `Volatility ${(measured.volatility_bps / 100).toFixed(2)}% annualized · maximum drawdown ${(measured.max_drawdown_bps / 100).toFixed(2)}% over ${measured.observations} daily observations${source}`]
        : ["Market", NOT_COMPUTABLE, "Not enough price history. Review the history requirements."],
      hasRisk ? ["Expected Shortfall", "COMPUTED", `ES95 ${(measured.expected_shortfall_95_bps / 100).toFixed(2)}% one day · ${measured.observations} / 250 aligned observations${source}`]
        : ["Expected Shortfall", NOT_COMPUTABLE, "0 / 250 aligned observations"],
      duration,
      ["Operational", "PARTIAL", row.gate?.reason || "Gate evidence unavailable."],
    ])}</section>`;
  }

  function provenancePreview(row, metadata) {
    const categories = [
      ["Identity", row.name && row.venue ? "PASS" : "MISSING"],
      ["Pricing", row.rate_bps == null ? "MISSING" : "PARTIAL"],
      ["Liquidity", row.depth_usd == null ? "MISSING" : "PARTIAL"],
      ["Credit", row.rating?.computable === true ? "PARTIAL" : "MISSING"],
      ["Eligibility", row.gate?.status ? "PARTIAL" : "MISSING"],
      ["Execution", verdict(row) === "BID" ? "PARTIAL" : "MISSING"],
    ];
    return `<section class="panel"><header><div><p class="eyebrow">Provenance</p><h2>DecisionBundle Preview</h2></div><span>${chip("UNANCHORED")}</span></header>${fact("Fenorium Score", scoreLabel(row))}${fact("Decision Hash", row.decision_hash ? `${row.decision_hash} · preview, not anchored` : NOT_COMPUTABLE, row.decision_hash ? "mono-value" : "")}${fact("Universe Source", metadata?.source, "mono-value")}${fact("Universe Hash", metadata?.hash, "mono-value")}${fact("Universe As Of", utcStamp(metadata?.generated_at))}<p class="cell-sub">This evidence preview has not been recorded on-chain. ${row.evidence_as_of ? `Evidence as of ${esc(utcStamp(row.evidence_as_of))}.` : "Per-instrument freshness is UNKNOWN · Not measured. Check the source timestamp."}</p></section>
      <section class="panel"><header><h2>Evidence Categories</h2></header>${support(categories.map((category) => [category[0], category[1], category[1] === "PASS" ? "Required identity fields present." : "Only the displayed source fields are available."]))}</section>
      <section class="panel"><header><h2>Deterministic Reason</h2></header><p class="full-reason">${esc(explainReason(row.reason || row.rejection || NOT_COMPUTABLE, row))}</p><p class="no-quote detail-note">${deployment.execution ? "No realized outcome recorded for this instrument" : "No realized outcome — execution disabled"}</p></section>`;
  }

  function costLedger(candidate) {
    const ledger = candidate?.cost_ledger;
    if (!ledger) return "";
    const fields = [
      ["Source swap", ledger?.source_swap],
      ["Price impact", ledger?.price_impact],
      ["Bridge", ledger?.bridge],
      ["LI.FI / integrator fee", ledger?.integrator_fee],
      ["Venue", ledger?.destination_venue_cost],
      ["Gas", ledger?.gas],
      ["Entry cost", ledger?.entry_cost],
    ];
    // A charged fee is never shown as $0.
    const feeRow = meta("Fenorium fee", deployment.fees ? "Quoted with each route" : usd("0"));
    const ledgerBody = `${fields.map((field) => fact(field[0], field[1] == null ? NOT_COMPUTABLE : usd(field[1]))).join("")}${feeRow}<p class="cell-sub">Total drag and the resulting net yield stay unknown while any cost is missing.${deployment.fees ? " Fenorium's routing fee is charged per route and shown with each route before you sign." : ""}</p>`;
    return `<section class="panel"><header><div><p class="eyebrow">Analytical preview</p><h2>Cost Ledger</h2></div></header>${ledgerBody}</section>`;
  }

  function analyticsTable(label, headers, rows) {
    return `<details><summary>${esc(label)}</summary><div class="table-wrap"><table><thead><tr>${headers.map(value => `<th scope="col">${esc(value)}</th>`).join("")}</tr></thead><tbody>${rows.map(values => `<tr>${values.map(value => `<td>${esc(value)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
  }

  function instrumentYield(row, data, sizeUsd, charts) {
    const curve = data.size_curve || {};
    const holding = rating => {
      const days = curve.days_to_maturity ?? row.days_to_maturity;
      const windowDays = curve.holding_period_days ?? P.HOLDING_PERIOD_DAYS;
      return Number(days) < Number(windowDays) ? holdingOf({...row, days_to_maturity: days, rating}) : null;
    };
    const points = (curve.points || []).map(point => {
      const hp = holding(point.rating);
      return {...point, headline_bps: point.rating?.computable === true ? (hp ? hp.gross_bps : point.rating.headline_bps) : null,
        net_bps: point.rating?.computable === true ? (hp ? hp.net_bps : point.rating.net_bps) : null,
        refusal: point.rating?.computable === true ? null : point.rating?.refusal || NOT_COMPUTABLE};
    });
    // The waterfall reads the same snapshot as the curve: the universe row may predate a UTC day
    // the platform has since restated (days, ratings, a maturity passing).
    const selectedPoint = Number(data.selected?.size_usd) === Number(sizeUsd) ? data.selected
      : (curve.points || []).find(point => Number(point.size_usd) === Number(sizeUsd));
    const rating = (selectedPoint ? selectedPoint.rating : row.rating) || {};
    const hp = holding(rating);
    const toMaturity = !!hp || points.some(point => holding(point.rating));
    const contribution = (value, factor = 1) => value == null || value === "" || !Number.isFinite(Number(value)) ? null : Number(value) * factor;
    const waterfall = rating.computable === true ? [
      {label: "Headline", value_bps: contribution(rating.headline_bps, hp ? hp.fraction : 1)},
      {label: "Credit haircut", value_bps: contribution(rating.credit_haircut_bps, hp ? -hp.fraction : -1)},
      {label: "Entry fee", value_bps: contribution(hp ? rating.base_fee_bps : rating.base_fee_annualized_bps, -1)},
      {label: "Modelled impact", value_bps: contribution(hp ? rating.modelled_impact_bps : rating.modelled_impact_annualized_bps, -1)},
      {label: "Net", value_bps: hp ? hp.net_bps : rating.net_bps, total: true},
    ] : [];
    return `<section class="panel"><header><h2>Yield at size</h2><span>${toMaturity ? "to maturity, not annualized" : "Annualized yield"}</span></header><p class="cell-sub">Platform size curve · ${esc(data.source_sha256?.slice(0, 12))} · ${esc(utcStamp(data.generated_at))} · modelled costs, not an executable quote</p><div class="grid-2">${charts.instrumentSizeCurve?.(points, {sizeUsd, depthUsd: curve.reported_depth_usd, toMaturity}) || ""}<div><h3>Yield waterfall at ${esc(notionalLabel(sizeUsd))}</h3>${waterfall.length ? charts.instrumentWaterfall?.(waterfall, toMaturity ? "Yield to maturity" : "Annualized yield") || "" : `<p>${esc(ratingRefusal(rating.refusal || NOT_COMPUTABLE))}</p>`}</div></div>${analyticsTable("Yield at size data", ["Position size", "Headline", "Net", "Refusal", "Fenorium Score"], points.map(point => [usdN(point.size_usd), pct(point.headline_bps), pct(point.net_bps), point.refusal || "—", `${point.fenorium_score ?? NOT_COMPUTABLE}${Number(point.fenorium_score) < 60 && point.fenorium_score != null ? ` · below 60 · score cap ${point.score_cap ?? NOT_COMPUTABLE}` : ""}`]))}${analyticsTable("Waterfall data", ["Component", "Yield contribution"], waterfall.map(item => [item.label, pct(item.value_bps)]))}</section>`;
  }

  function instrumentHistory(row, data, charts) {
    const declared = row.rate_kind === "declared_demo";
    const history = data.price_history;
    let prices = Array.isArray(history?.prices) ? history.prices : [];
    if (declared) {
      prices = [];
      const start = Date.parse(row.price_accrual?.accrual_start);
      const today = Date.now();
      const scale = Number(row.price_accrual?.price_scale);
      if (Number.isFinite(start) && Number.isFinite(scale) && scale > 0 && typeof P.historicalTransferPrice === "function") {
        for (let at = start; at <= today; at += 86400000) {
          const price = P.historicalTransferPrice(row, String(Math.floor(at / 1000)));
          if (price != null) prices.push([new Date(at).toISOString().slice(0, 10), String(Number(price) / scale)]);
        }
        const day = new Date(today).toISOString().slice(0, 10);
        if (start <= today && prices[prices.length - 1]?.[0] !== day) {
          const price = P.historicalTransferPrice(row, String(Math.floor(today / 1000)));
          if (price != null) prices.push([day, String(Number(price) / scale)]);
        }
      }
    }
    const stale = history?.statistics_reason?.startsWith("the series ends on ");
    const provenance = declared ? "Declared price path · not a market observation"
      : history ? `${history.provenance} · ${history.source || "UNKNOWN"}${history.provenance === "modelled" ? ` · ${history.method || "UNKNOWN"}` : ""}${stale && prices.length ? ` · last price ${prices[prices.length - 1][0]}` : ""}` : "Platform price history";
    let body;
    if (!declared && !history) body = `<p>${esc(data.price_history_reason || "Price history unavailable")}</p>`;
    else {
      const paths = charts.instrumentPriceHistory?.(prices);
      body = `${paths?.price || ""}${prices.length ? "" : `<p>${declared ? "Declared price path unavailable" : "Price history unavailable"}</p>`}`;
      if (!declared) {
        const stats = history.statistics;
        const labels = [["Observations · trailing year", "observations"], ["Annualized volatility", "annualized_volatility_bps"], ["Max drawdown", "max_drawdown_bps"], ["ES95", "expected_shortfall_95_bps"], ["30-day return", "return_30d_bps"], ["90-day return", "return_90d_bps"]];
        body += `<div class="grid-2"><div><h3>Drawdown</h3>${paths?.drawdown || ""}</div><div><h3>Daily return distribution · trailing year</h3><div class="instrument-return-histogram">${charts.returnHistogram?.((history.daily_returns || []).filter(item => !stats?.risk_window_start || item.day >= stats.risk_window_start), stats?.expected_shortfall_95_bps, {instrument: true}) || ""}</div></div></div><div class="kpi-grid">${labels.map(([label, key]) => `<div class="metric-card"><div class="label">${label}</div><div class="value">${esc(stats?.[key] == null ? NOT_COMPUTABLE : key === "observations" ? stats[key] : pct(stats[key]))}</div>${stats?.[key] == null ? `<p class="detail">${esc(history.statistics_reason || "Insufficient price history")}</p>` : ""}</div>`).join("")}</div>`;
        body += analyticsTable("Daily return data", ["Day", "Daily return"], (history.daily_returns || []).map(item => [item.day, pct(item.return_bps)]));
      }
      let peak = 0;
      body += analyticsTable("Price history data", declared ? ["Day", "Declared price"] : ["Day", "Price", "Drawdown"], prices.map(([day, value]) => {
        peak = Math.max(peak, Number(value));
        return declared ? [day, value] : [day, value, peak > 0 ? pct((Number(value) / peak - 1) * 10000) : NOT_COMPUTABLE];
      }));
    }
    return `<section class="panel"><header><h2>Price history</h2></header><p class="cell-sub">${esc(provenance)}</p>${body}</section>`;
  }

  function instrumentCashflow(row, terms, charts, replyDays = null) {
    // The reply's own terms and day count come first: the page's row can be from an older snapshot.
    row = {...row, maturity: terms.maturity || row.maturity,
      maturity_timestamp: terms.maturity ? terms.maturity_timestamp : row.maturity_timestamp,
      rate_bps: terms.quoted_yield_bps ?? row.rate_bps};
    const matured = P.rowMatured(row);
    const maturity = P.maturityDateMs(row.maturity);
    const remaining = replyDays ?? (terms.years_to_maturity == null
      ? row.days_to_maturity ?? (maturity == null ? null : Math.ceil((maturity - P.clockNow()) / 86400000))
      : Number(terms.years_to_maturity) * 365);
    row = {...row, days_to_maturity: remaining == null ? null : Math.max(0, remaining)};
    const open = !row.maturity && (["OPEN_ENDED", "FUND_SHARE"].includes(terms.instrument_type) || P.marketFieldApplies?.(row, "maturity") === false);
    const datedFund = terms.instrument_type === "FUND_SHARE" && !open;
    const declared = row.rate_kind === "declared_demo";
    const names = {ZERO_COUPON: "Zero-coupon", TERM_POSITION: "Term position", OPEN_ENDED: "Open-ended", FUND_SHARE: "Fund share", NOT_FIXED_INCOME: "Not fixed income"};
    const flows = Array.isArray(terms.cashflows) ? terms.cashflows : [];
    const duration = matured ? "Not applicable: matured" : open ? "Not applicable: open-ended" : declared && remaining != null ? `${(remaining / 365).toFixed(2)} years` : datedFund ? "Not established" : terms.macaulay_duration_years == null ? "UNSUPPORTED" : `${Number(terms.macaulay_duration_years).toFixed(2)} years`;
    const cashflow = matured ? "Matured: the redemption date has passed" : open ? "Open-ended: no scheduled redemption" : declared && row.maturity ? "Single redemption at maturity (zero-coupon)" : datedFund ? "Maturity stated; redemption structure not established" : flows.length ? terms.instrument_type === "ZERO_COUPON" ? "Single redemption at maturity (zero-coupon)" : "Scheduled cashflows" : "INSUFFICIENT CASHFLOW DATA";
    let sensitivity = "";
    if (row.rate_kind === "declared_demo" && !matured && row.days_to_maturity > 0 && row.rate_bps != null) {
      const years = row.days_to_maturity / 365, rate = row.rate_bps / 10000;
      const price = (1 + rate) ** -years, modified = years / (1 + rate);
      const points = [-200, -100, -50, 0, 50, 100, 200].map(shift => ({shift, yield_bps: Number(row.rate_bps) + shift, price: 1 + rate + shift / 10000 > 0 ? (1 + rate + shift / 10000) ** -years : null, current: shift === 0}));
      sensitivity = `<h3>Price versus yield</h3><p class="cell-sub">computed from the declared rate</p><p class="cell-sub">Computed from the declared rate and maturity with annual compounding, as the demo terms declare; one payment at maturity.</p>${fact("Duration / DV01", `Macaulay ${years.toFixed(2)} y · modified ${modified.toFixed(2)} y · DV01 $${(modified * price * 1000000 * 0.0001).toFixed(0)} per $1,000,000 face`)}${fact("YTM / Convexity", `${(rate * 100).toFixed(2)}% · ${(years * (years + 1) / (1 + rate) ** 2).toFixed(2)}`)}${charts.instrumentYieldCurve?.(points) || ""}${analyticsTable("Declared rate sensitivity", ["Yield change", "Yield", "Price per unit face", "Value change"], points.map(point => [`${point.shift > 0 ? "+" : point.shift < 0 ? "−" : ""}${Math.abs(point.shift)} bp`, pct(point.yield_bps), point.price == null ? NOT_COMPUTABLE : point.price.toFixed(6), point.price == null ? NOT_COMPUTABLE : pct((point.price / price - 1) * 10000)]))}`;
    }
    return `<section class="panel"><header><h2>Cashflow and duration</h2></header><p class="cell-sub">Platform instrument terms · ${esc(row.rate_kind === "declared_demo" ? "declared demo terms" : "scheduled cashflows; no yield-dependent measure inferred from quoted APY")}</p>${fact("Instrument type", names[terms.instrument_type] || NOT_COMPUTABLE)}${fact("Maturity Date", terms.maturity || row.maturity || (open ? "Open-ended · no maturity date" : NOT_COMPUTABLE))}${fact("Days to Maturity", matured ? "Matured" : open ? "Not applicable" : numericMetric(row, "days"))}${fact("Cashflow Modeling", cashflow)}${fact("Redemption basis", terms.redemption?.basis)}${fact("Macaulay duration", duration)}${charts.instrumentCashflows?.(flows) || ""}${analyticsTable("Scheduled cashflow data", ["Date", "Asset", "Per unit"], flows.map(flow => [flow.at, flow.asset_id, flow.per_unit]))}${sensitivity}</section>`;
  }

  function instrumentPeers(row, population, sizeUsd, charts) {
    const size = Number(sizeUsd) || 250000;
    const sizeLabel = size >= 1000000 && size % 1000000 === 0 ? `$${size / 1000000}m` : size >= 1000 && size % 1000 === 0 ? `$${size / 1000}k` : notionalLabel(size);
    const dated = row.days_to_maturity != null;
    const peers = population.filter(peer => String(peer.category).toLowerCase() === String(row.category).toLowerCase() && peer.rating?.computable === true && peer.rating.net_bps != null && Number.isFinite(Number(peer.rating.net_bps)));
    const ranked = peers.slice().sort((a, b) => Number(b.rating.net_bps) - Number(a.rating.net_bps));
    const id = instrumentAnalyticsId(row);
    const rank = ranked.findIndex(peer => id !== null && instrumentAnalyticsId(peer) === id);
    const points = peers.filter(peer => dated ? peer.days_to_maturity != null && Number.isFinite(Number(peer.days_to_maturity)) : peer.days_to_maturity == null && Number.isFinite(Number(peer.depth_usd)) && Number(peer.depth_usd) > 0).map(peer => ({name: peer.name, identity: instrumentAnalyticsId(peer), x: Number(dated ? peer.days_to_maturity : peer.depth_usd), net_bps: peer.rating.net_bps, selected: id !== null && instrumentAnalyticsId(peer) === id}));
    const sentence = rank < 0 ? `Net yield at ${sizeLabel} is ${NOT_COMPUTABLE} for this instrument; ${peers.length} computable ${categoryLabel(row.category)} instruments`
      : `Net yield at ${sizeLabel} ranks ${rank + 1} of ${peers.length} ${categoryLabel(row.category)} instruments`;
    return `<section class="panel"><header><h2>Against its peers</h2></header><p class="cell-sub">Market population · same category · selected size · annualized net yield</p><p>${esc(sentence)}${charts.instrumentPeerScale?.(points).beyond ? ` · ${charts.instrumentPeerScale(points).beyond} peers beyond the scale` : ""}</p>${charts.instrumentPeers?.(points, {logX: !dated, xLabel: dated ? "Days to maturity" : "Reported depth (USD, log scale)"}) || ""}${analyticsTable("Peer data", ["Instrument", dated ? "Days to maturity" : "Reported depth", "Net yield (annualized)"], points.map(point => [point.name, dated ? point.x : usdN(point.x), pct(point.net_bps)]))}</section>`;
  }

  function instrumentAnalyticsId(row) {
    return P.instrumentId(row);
  }

  function instrumentAnalytics(row, state, peers, sizeUsd) {
    if (row.simulation === true) return `<p>Analytics come from the platform; a simulated instrument has none.</p>`;
    if (state?.status === "loading") return `<p role="status">Loading analytics…</p>`;
    if (state?.status !== "ready" || !state.data) return `<p>Analytics unavailable · ${esc(state?.error || "No platform analytics available")}</p>${state?.retryable ? '<button type="button" class="ghost" data-analytics-retry>Retry</button>' : ""}`;
    const charts = root.FenoriumCharts || {}, data = state.data;
    const snapshot = state.otherSnapshot ? `<p class="portfolio-scope">These analytics come from the market snapshot of ${esc(utcStamp(data.generated_at))} (${esc(String(data.source_sha256 || "").slice(0, 12))}), newer than the one the rest of this page shows. Refresh the market data to see one snapshot everywhere.</p>`
      : state.otherDay ? `<p class="portfolio-scope">These analytics were rated today; the rest of this page was rated on ${esc(state.otherDay)}. Refresh the market data to see one rating everywhere.</p>` : "";
    return `<div class="instrument-analytics">${snapshot}${instrumentYield(row, data, sizeUsd, charts)}${instrumentHistory(row, data, charts)}${instrumentCashflow(row, data.terms || {}, charts, data.size_curve?.days_to_maturity ?? null)}${state.otherSnapshot || state.otherDay
      ? `<section class="panel"><header><h2>Against its peers</h2></header><p>Peers are ranked within one market snapshot, and this page still shows an older one. Refresh the market data to compare this instrument with its peers.</p></section>`
      : instrumentPeers(row, peers, sizeUsd, charts)}</div>`;
  }

  function asset(row, tab, metadata, candidate, purchase, sizeUsd, analytics, peers) {
    if (!row) return;
    tab = tab === "analytics" || tab === "maturity" ? "analytics" : "summary";
    const copy = $("copy-address");
    if (copy) {
      // A venue position has no token address; the control is withdrawn rather than disabled.
      copy.hidden = !row.address;
      copy.disabled = !row.address;
      copy.textContent = "Copy address";
    }
    const title = $("asset-title");
    if (title) title.textContent = row.name || "Instrument";
    const subtitle = $("asset-subtitle");
    if (subtitle) subtitle.textContent = `${row.simulation === true ? "Simulated instrument · " : ""}${row.venue || NOT_COMPUTABLE} · ${row.chain || NOT_COMPUTABLE} · ${categoryLabel(row.category) || NOT_COMPUTABLE}`;
    const breadcrumb = $("asset-breadcrumb");
    if (breadcrumb) breadcrumb.textContent = `Market / ${row.name || "Instrument"}`;
    if (typeof document !== "undefined") {
      document.querySelectorAll("[data-asset-tab]").forEach((button) => button.setAttribute("aria-current", button.dataset.assetTab === tab ? "page" : "false"));
    }
    const content = $("asset-content");
    if (!content) return;
    content.innerHTML = tab === "summary"
      ? `${summary(row, metadata, purchase, sizeUsd)}${costLedger(candidate)}${row.is_demo === true || row.simulation === true ? readOnlyExecutionNotice(row) : ""}<details id="asset-provenance" class="panel"><summary>Evidence and provenance</summary>${riskCoverage(row)}${provenancePreview(row, metadata)}<button class="ghost" type="button" data-execute-action="export">Export Decision Bundle</button></details>`
      : instrumentAnalytics(row, analytics, peers || [], sizeUsd);
  }

  function renderRisk(positions) {
    const metrics = $("risk-metrics");
    if (metrics) metrics.textContent = NOT_COMPUTABLE;
    const concentration = $("concentration-panel");
    if (concentration && typeof P.concentration === "function") {
      concentration.textContent = P.concentration(positions, "issuer").unknown_visible ? "UNKNOWN remains visible" : "Point-in-time only";
    }
  }

  function renderMaturity() {
    const element = $("maturity-table");
    if (element) element.innerHTML = "";
  }

  function renderEvidence(snapshot, positions, nav, opportunity) {
    const element = $("evidence-preview");
    if (!element) return;
    element.textContent = stateText("UNANCHORED") + "\n\nTechnical record:\n" + JSON.stringify({
      schema_version: "DecisionBundle/v2",
      status: "UNANCHORED",
      as_of: snapshot.provenance?.as_of || null,
      nav_status: nav.status,
      opportunity_status: opportunity?.status || null,
    }, null, 2);
  }

  function renderExecution() {
    const element = $("execution-blockers");
    if (element) element.textContent = ["LEGAL_REVIEW_REQUIRED", "MISSING_EXECUTION_QUOTE", "DESTINATION_FAILURE_EXPERIMENT"].map(stateText).join(" ");
  }

  function compareFields(sizeUsd) {
    return [
      ["Fenorium Score", "verdict"],
      ["Headline Yield", "headline"],
      [`Net @ ${notionalLabel(sizeUsd)}`, "net"],
      ["Execution Cost", "execution"],
      ["Credit Haircut", "credit"],
      ["Reported depth · exitable or acquirable", "depth"],
      ["Tier", "tier"],
      ["Maturity", "maturity"],
      ["Days to Maturity", "days"],
      ["Venue", "venue"],
      ["Chain", "chain"],
      ["Permission", "permission"],
      ["Reason", "reason"],
    ];
  }

  function comparisonValue(row, field) {
    if (field === "verdict") return verdict(row);
    if (field === "net" || field === "execution") {
      const hp = holdingOf(row);
      return hp ? pct(field === "net" ? hp.net_bps : hp.cost_bps) : numericMetric(row, field);
    }
    if (["headline", "credit", "depth", "days"].includes(field)) return numericMetric(row, field);
    if (field === "maturity") return textOr(row.maturity);
    if (field === "tier") return textOr(row.tier);
    if (field === "venue") return textOr(row.venue);
    if (field === "chain") return textOr(row.chain);
    if (field === "permission") return textOr(row.gate?.status);
    if (field === "reason") return explain(textOr(row.reason || row.rejection), row);
    return NOT_COMPUTABLE;
  }

  function comparisonMarkup(row, field, value) {
    const rendered = esc(field === "permission" ? stateText(value) : value);
    if (field === "net" || field === "execution") {
      const hp = holdingOf(row);
      const basis = hp ? `<small class="cell-sub">to maturity · ${esc(Number(hp.days).toFixed(1))} days · annualized ${esc(numericMetric(row, field))}</small>` : "";
      return rendered + basis + costFigureContext(row);
    }
    if (field === "depth") return rendered + sourceContext("Depth source", row.depth_source);
    return rendered;
  }

  function renderCompare(result, sizeUsd) {
    const element = $("compare-content");
    if (!element) return;
    if (!result?.ok) {
      element.innerHTML = `<div class="compare-empty"><strong>Select 2–5 instruments to compare.</strong><p>${esc(result?.message || "The comparison stays neutral and does not rank or choose a winner.")}</p></div>`;
      return;
    }
    const rows = result.rows;
    element.innerHTML = `<p class="compare-note">Selection order is neutral and not ranked. Execution cost uses a stated flat 30 bp base fee plus <b>MODELLED</b> size/depth impact; it is not measured slippage or a live executable quote.</p><div class="comparison-scroll"><table class="comparison-table"><thead><tr><th scope="col">Field</th>${rows.map((row) => `<th scope="col"><span class="compare-instrument">${esc(row.name)}</span><small>${esc(row.chain || NOT_COMPUTABLE)} · ${esc(executionMode(row))}</small></th>`).join("")}</tr></thead><tbody>${compareFields(sizeUsd).map((field) => `<tr><th scope="row">${esc(field[0])}</th>${rows.map((row) => {
      const value = comparisonValue(row, field[1]);
      return `<td class="${value === NOT_COMPUTABLE ? "not-computable" : ""}">${field[1] === "verdict" ? scoreChip(row) : comparisonMarkup(row, field[1], value)}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  function renderAssistant(facts, answer, status, guidance) {
    const factsElement = $("assistant-facts");
    if (factsElement) {
      factsElement.innerHTML = (facts || []).length
        ? `<dl class="facts-list">${facts.map((fact) => `<div><dt>${esc(fact.field_ref)}</dt><dd><b>${esc(stateText(fact.status))}</b><span>${esc(fact.value == null ? "No value" : `${fact.value} ${fact.unit || ""}`)}</span><small>${esc(explain(fact.reason_code || "Recorded evidence"))} · ${esc(fact.source_ref || "local")}</small></dd></div>`).join("")}</dl>`
        : `<p class="cell-sub">No deterministic facts are currently selected.</p>`;
    }
    const answerElement = $("assistant-answer");
    if (answerElement) answerElement.textContent = answer || "No explanation returned. Deterministic output is unchanged.";
    const statusElement = $("assistant-status");
    if (statusElement) statusElement.innerHTML = guidance ? `<span>${esc(status || "Assistant unavailable. Deterministic output remains unchanged.")}</span><code>${esc(guidance)}</code>` : esc(status || "Explanation grounded in the displayed facts.");
  }

  // Risk (owner 2026-10-20, moved from the rail): the dashboard measures this portfolio, so it
  // is a tab of it, beside Performance.
  const PORTFOLIO_TABS = ["summary", "holdings", "exposure", "performance", "risk", "transactions"];
  const portfolioMoney = (value) => value == null ? "UNKNOWN" : usd(value);
  // The KPI row shows round figures, as the mockup does: $500,021 and 100%, not $500,021.34
  // and 100.00%. Cents and trailing zeroes stay everywhere they carry information -- the
  // holdings table, the exposure breakdown, the CSV -- and are dropped only in the headline.
  const portfolioMoneyWhole = (value) => {
    const text = portfolioMoney(value);
    return text === "UNKNOWN" ? text : text.replace(/\.\d{2}$/, "");
  };
  const portfolioPercentTrim = (value) => {
    const text = portfolioPercent(value);
    return text === "UNKNOWN" ? text : text.replace(/\.00%$/, "%");
  };
  const portfolioPercent = (value) => value == null ? "UNKNOWN" : pct(value);
  // The Type column names what the holding is, at the same grain the exposure chart groups by:
  // a tokenized treasury fund and a DeFi principal token both read "Fixed income" otherwise.
  const holdingClassLabel = (row) => {
    const klass = P.assetClass ? P.assetClass(row) : row?.asset_type;
    return P.PORTFOLIO_CLASS_LABELS_ONE?.[klass] || portfolioLabel(klass);
  };
  const portfolioLabel = (value) => value === "UNKNOWN" || value === "N/A" ? value : (P.PORTFOLIO_CLASS_LABELS?.[value] || P.MARKET_CATEGORY_LABELS[value] || stateLabel(value || "UNKNOWN"));
  const historyReason = (tab) => tab === "performance"
    ? "Portfolio history is not collected yet, so returns over time cannot be shown"
    : "Transaction history requires transfer-log discovery, which is not enabled";
  function portfolioUnavailable(tab, history, options = {}) {
    if (tab === "transactions") return transactionScreen(history, options);
    return `<section class="panel portfolio-unavailable"><h2>UNKNOWN · Data not collected</h2><p>${historyReason(tab)}</p></section>`;
  }
  // A scan that takes half a minute looked exactly like one that never started: both showed
  // "scan finalized transfers first". Since the scan now begins on its own, nobody is waiting
  // for it on purpose, and a screen with no sign of work in progress reads as a screen that has
  // given up.
  function scanProgress(options = {}) {
    const history = options.transferHistory;
    if (history?.status !== "LOADING") return null;
    const total = Number(history.scanning) || 0;
    const done = Number(history.scanned) || 0;
    return total > 0
      ? `Reading finalized transfers · ${done} of ${total} block ranges`
      : "Reading finalized transfers…";
  }
  function performanceRecord(snapshot, options = {}) {
    if (snapshot?.provenance?.kind === 'LOCAL_DEMO_FIXTURE') {
      const series = snapshot.nav_series;
      let points = Array.isArray(series?.points) ? series.points : [];
      if (!points.length) return {status:'UNKNOWN', declared:true, points:[], reason:'Derived portfolio history is unavailable; no declared sample series'};
      // A fixture may declare that it has no external flows. Where it does, the change in
      // declared NAV *is* the P&L and there is nothing to exclude; where it does not, the
      // flow-adjusted figure stays UNKNOWN because a deposit would be indistinguishable from a
      // gain. Either way the card says which it is looking at.
      const flowless = series?.external_flows === 'none';
      const first = asBigInt(points[0]?.nav_usd_micros), last = asBigInt(points.at(-1)?.nav_usd_micros);
      // With no external flows the change in declared NAV since the opening mark is the P&L, so
      // the points carry it under the same name a derived record uses. Everything reading a
      // point -- the chart, the callout, the period figure -- then works on either without
      // knowing which it has.
      if (flowless && first !== null) {
        points = points.map((point) => {
          const nav = asBigInt(point.nav_usd_micros);
          if (nav === null) return point;
          // "No external flows" is a declaration that nothing crossed the boundary, which is a
          // measured nought rather than an absence -- so the flow series is zero, not UNKNOWN.
          return {...point, pnl_usd_micros: (nav - first).toString(),
            net_external_flows_usd_micros: '0', external_inflows_usd_micros: '0', external_outflows_usd_micros: '0',
            return_bps: first > 0n ? ((nav - first) * 10000n / first).toString() : null};
        });
      }
      const change = flowless && first !== null && last !== null ? last - first : null;
      const reason = flowless
        ? 'Declared sample series with no external flows; change in declared NAV'
        : 'Declared sample NAV only; external flows and flow-adjusted P&L are UNKNOWN';
      const periods = {};
      if (change !== null && first > 0n) {
        for (const [label, days] of [['7D', 7], ['30D', 30]]) {
          const window = points.slice(-(days + 1));
          const open = asBigInt(window[0]?.nav_usd_micros);
          if (window.length > days && open !== null && open > 0n) {
            // Same four fields the derived record publishes, meaning the same things: with no
            // external flows the change in declared NAV is both the money made and the return.
            periods[label] = {status: 'COMPLETE', pnl_usd_micros: (last - open).toString(),
              change_bps: ((last - open) * 10000n / open).toString(), covered: true, reason: null};
          } else {
            periods[label] = {status: 'UNKNOWN', pnl_usd_micros: null, change_bps: null, covered: false,
              reason: `History covers ${points.length - 1} days; ${label} needs ${days}`};
          }
        }
      }
      // With no external flows the change in declared NAV is the return, so the field means the
      // same thing here as the linked factor does on a derived record. Leaving it out was why
      // the panel could state a return for a real wallet and not for the sample.
      const totalReturn = flowless && first !== null && first > 0n && last !== null
        ? ((last - first) * 10000n / first).toString()
        : null;
      return {status: points.length ? 'COMPLETE' : 'UNKNOWN', declared: true, reason,
        source: `Declared sample series · ${series?.source || 'LOCAL_DEMO_FIXTURE'}`, points,
        total_return_bps: totalReturn,
        cumulative_pnl_usd_micros: change === null ? null : change.toString(),
        net_external_flows_usd_micros: flowless ? '0' : null,
        external_inflows_usd_micros: flowless ? '0' : null, external_outflows_usd_micros: flowless ? '0' : null,
        nav_usd_micros: points.at(-1)?.nav_usd_micros ?? null, tracking_since: points[0]?.as_of,
        annualized_reason: reason, periods};
    }
    return options.performance || {status:'UNKNOWN', points:[], reason:'Derived portfolio history is unavailable; scan finalized transfers first'};
  }
  const performanceMoney = value => value === 'N/A' ? 'N/A' : value == null ? 'UNKNOWN' : portfolioMoney(value);
  const performancePct = value => {
    if (value === 'N/A') return value;
    const bps = asBigInt(value);
    if (bps === null) return 'UNKNOWN';
    const magnitude = bps < 0n ? -bps : bps;
    return `${bps < 0n ? '−' : ''}${magnitude / 100n}.${String(magnitude % 100n).padStart(2,'0')}%`;
  };
  const performanceDelta = performance => {
    const period = performance.periods?.['30D'];
    return period?.status === 'COMPLETE' ? `30D P&L ${performanceMoney(period.pnl_usd_micros)} · declared demo accrual`
      : period?.reason || performance.reason || '30D P&L UNKNOWN';
  };
  // Opening balance + inflows - outflows + P&L = ending balance. Every figure on this screen is
  // a term of that identity, and each is a difference of two cumulative ones the record already
  // publishes -- so the terms reconcile by construction and a period the history does not cover
  // is absent rather than nought.
  function overviewBuckets(performance, options = {}) {
    if (performance.status !== 'COMPLETE' || !Array.isArray(performance.points) || performance.points.length < 2) {
      return {status: 'UNKNOWN', reason: performance.reason || 'Derived portfolio history is unavailable', buckets: []};
    }
    const window = pnlWindow(performance.points, options);
    return P.portfolioCashFlowBuckets(window.points, {baseline: window.before});
  }
  // A breakdown table has five columns in a side panel: $120,000 wraps there and reads as two
  // numbers. Compact where it is read against its neighbours, exact where it is read alone.
  const overviewCompact = (value, signed = false) => {
    const amount = asBigInt(value);
    if (amount === null) return 'UNKNOWN';
    const magnitude = amount < 0n ? -amount : amount;
    const dollars = Number(magnitude) / 1e6;
    let text = `$${dollars.toFixed(dollars === Math.trunc(dollars) ? 0 : 2)}`;
    for (const [size, suffix] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'k']]) {
      const rounded = Number((dollars / size).toFixed(1));
      if (rounded >= 1 && rounded < 1000) {
        text = `$${rounded === Math.trunc(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}${suffix}`;
        break;
      }
    }
    return amount < 0n ? `−${text}` : signed && amount > 0n ? `+${text}` : text;
  };
  const overviewMoney = (value, signed = false) => {
    const amount = asBigInt(value);
    if (amount === null) return 'UNKNOWN';
    const text = portfolioMoneyWhole(amount < 0n ? (-amount).toString() : amount.toString());
    if (text === 'UNKNOWN') return text;
    return amount < 0n ? `−${text}` : signed && amount > 0n ? `+${text}` : text;
  };
  // The Balance Summary is the identity written out as a column, so its terms have to add up to
  // the total under them. Rounded to whole dollars they did not: $735,663 + $1,441 printed
  // beside an ending balance of $737,105, and a reader adding the column got a different book.
  // The cards above keep whole dollars -- they are read one at a time, not summed.
  const overviewExact = (value, signed = false) => {
    const amount = asBigInt(value);
    if (amount === null) return 'UNKNOWN';
    const text = portfolioMoney(amount < 0n ? (-amount).toString() : amount.toString());
    if (text === 'UNKNOWN') return text;
    return amount < 0n ? `−${text}` : signed && amount > 0n ? `+${text}` : text;
  };
  // A movement is drawn as one -- sign, colour, nothing else. A measured nought is neither a
  // gain nor a loss and stays uncoloured.
  const overviewTone = value => {
    const amount = asBigInt(value);
    return amount === null || amount === 0n ? '' : amount < 0n ? ' portfolio-negative' : ' portfolio-positive';
  };
  const overviewUnit = flows => ({day: 'Daily', week: 'Weekly', month: 'Monthly'}[flows.unit] || 'Period');
  // Best and worst period are P&L, never cash flow: the largest month a book ever had is the
  // month it earned the most, not the month the most money was wired into it.
  function overviewExtremes(flows) {
    const earned = (flows.buckets || []).map(bucket => ({label: bucket.label, amount: asBigInt(bucket.pnl_usd_micros)}))
      .filter(entry => entry.amount !== null);
    return {
      best: earned.reduce((a, b) => a === null || b.amount > a.amount ? b : a, null),
      worst: earned.reduce((a, b) => a === null || b.amount < a.amount ? b : a, null),
    };
  }
  function overviewCards(performance, flows, options = {}) {
    const complete = flows.status === 'COMPLETE';
    const missing = complete ? '' : (scanProgress(options) || flows.reason);
    const total = asBigInt(performance.total_return_bps);
    const returned = total === null ? null : `${total < 0n ? '−' : '+'}${(Number(total < 0n ? -total : total) / 100).toFixed(2)}%`;
    const ending = complete ? flows.ending_equity_usd_micros : performance.nav_usd_micros;
    const opening = complete ? flows.starting_equity_usd_micros : null;
    const pnl = complete ? flows.pnl_usd_micros : performance.cumulative_pnl_usd_micros;
    // The gross figures are what crossed the wallet's boundary, which is not the same question
    // as what was deposited: an exchange of one tracked asset for another moves value both ways
    // and nets to nothing. The card says so rather than calling a swap a deposit.
    const cards = [
      ['Ending Balance', overviewMoney(ending), 'Closing account balance', true, '', complete ? returned : null],
      ['Opening Balance', overviewMoney(opening), 'Start of selected period', false, '', null],
      ['Total Inflows', overviewMoney(complete ? flows.inflows_usd_micros : null), 'Gross value transferred in, valued when it moved', false, '', null],
      ['Total Outflows', overviewMoney(complete ? flows.outflows_usd_micros : null), 'Gross value transferred out, valued when it moved', false, '', null],
      ['Net Trading P&L', overviewMoney(pnl, true), 'Balance change less every deposit and withdrawal', false, overviewTone(pnl), null],
    ];
    return `<div class="kpi-grid portfolio-kpis overview-kpis">${cards.map(([label, amount, detail, featured, tone, change]) =>
      `<section class="metric-card${featured ? ' metric-card-featured' : ''}"><div class="label">${esc(label)}</div>`
      + `<div class="value${tone}">${esc(amount)}</div>`
      + `<div class="detail">${change ? `<span class="portfolio-positive">${esc(change)}</span> · ` : ''}${esc(complete ? detail : missing)}</div></section>`).join('')}</div>`;
  }
  function overviewPanel(performance, flows, options = {}) {
    const series = ['all', 'inflows', 'outflows', 'pnl', 'balance'].includes(String(options.pnlSeries || '').toLowerCase())
      ? String(options.pnlSeries).toLowerCase() : 'all';
    const range = String(options.pnlRange || 'ALL').toUpperCase();
    const controls = `<div class="pnl-controls"><div class="pnl-series">`
      + [['all', 'All'], ['inflows', 'Inflows'], ['outflows', 'Outflows'], ['pnl', 'P&L'], ['balance', 'Balance']].map(([key, label]) =>
        `<button type="button" data-pnl-series="${key}" aria-pressed="${series === key}">${esc(label)}</button>`).join('')
      + `</div><div class="pnl-range">${['1W', '1M', '3M', 'ALL'].map(key =>
        `<button type="button" data-pnl-range="${key}" aria-pressed="${range === key}">${key}</button>`).join('')}</div></div>`;
    const unitWord = {day: 'day', week: 'week', month: 'month'}[flows.unit] || 'period';
    const body = flows.status === 'COMPLETE'
      ? root.FenoriumCharts.portfolioOverview(flows.buckets, {series})
      : `<div class="value">UNKNOWN</div><p>${esc(scanProgress(options) || flows.reason)}</p>`;
    // The panel's own mark, so the screen's lead chart is recognisable before it is read.
    const mark = `<span class="movement-mark" aria-hidden="true">`
      + `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">`
      + `<path d="M3 17l6-6 4 4 7-7"></path><path d="M20 8V4h-4"></path></svg></span>`;
    return `<header class="pnl-header"><h2 class="movement-title">${mark}Account Movement</h2>${controls}</header>`
      + `<p class="overview-caption">${esc(flows.status === 'COMPLETE'
        ? `Inflows, outflows, P&L and the resulting ending balance, by ${unitWord}. Flows are gross movement across the wallet boundary; an exchange of one tracked asset for another appears on both sides.`
        : 'Inflows, outflows, P&L, and resulting ending balance.')}</p>${body}`;
  }
  function overviewBreakdown(flows) {
    if (flows.status !== 'COMPLETE') {
      return `<header class="pnl-header"><h2>Breakdown</h2></header><div class="value">UNKNOWN</div><p>${esc(flows.reason)}</p>`;
    }
    const heading = overviewUnit(flows);
    const columns = [['Inflows', 'inflows_usd_micros'], ['Outflows', 'outflows_usd_micros'],
      ['P&L', 'pnl_usd_micros'], ['Ending Balance', 'equity_usd_micros']];
    return `<header class="pnl-header"><h2>${esc(heading)} Breakdown</h2><span class="cell-sub">USD</span></header>`
      + `<div class="table-scroll"><table class="portfolio-table overview-breakdown">`
      + `<thead><tr><th scope="col">${esc(heading === 'Period' ? 'Period' : heading.replace(/ly$/, ''))}</th>`
      + columns.map(([label]) => `<th scope="col" class="num">${esc(label)}</th>`).join('')
      + `</tr></thead><tbody>`
      + flows.buckets.map(bucket => `<tr><th scope="row">${esc(bucket.label)}</th>`
        + columns.map(([label, field]) => `<td class="num${label === 'Ending Balance' ? '' : overviewTone(bucket[field])}" title="${esc(overviewMoney(bucket[field]))}">${esc(overviewCompact(bucket[field], label === 'P&L'))}</td>`).join('')
        + `</tr>`).join('')
      + `</tbody></table></div>`;
  }
  function overviewSummary(performance, flows) {
    if (flows.status !== 'COMPLETE') {
      return `<header class="pnl-header"><h2>Balance Summary</h2></header><div class="value">UNKNOWN</div><p>${esc(flows.reason)}</p>`;
    }
    const unitWord = overviewUnit(flows);
    const span = `${flows.buckets[0].opened_at.slice(0, 10)} – ${flows.buckets.at(-1).as_of.slice(0, 10)}`;
    const {best, worst} = overviewExtremes(flows);
    const annualized = performancePct(performance.annualized_return_bps);
    const total = asBigInt(performance.total_return_bps);
    const rows = [
      ['Opening Balance', overviewExact(flows.starting_equity_usd_micros), ''],
      ['Total Inflows', overviewExact(flows.inflows_usd_micros), overviewTone(flows.inflows_usd_micros)],
      ['Total Outflows', overviewExact(flows.outflows_usd_micros), overviewTone(flows.outflows_usd_micros)],
      ['Net Trading P&L', overviewExact(flows.pnl_usd_micros, true), overviewTone(flows.pnl_usd_micros)],
      ['Ending Balance', overviewExact(flows.ending_equity_usd_micros), ''],
      [`Highest ${unitWord.replace(/ly$/, 'ly')} P&L`, best ? `${overviewExact(best.amount.toString(), true)} (${best.label})` : 'UNKNOWN', best ? overviewTone(best.amount.toString()) : ''],
      [`Lowest ${unitWord.replace(/ly$/, 'ly')} P&L`, worst ? `${overviewExact(worst.amount.toString(), true)} (${worst.label})` : 'UNKNOWN', worst ? overviewTone(worst.amount.toString()) : ''],
      // Not a term of the balance identity, but the two figures a reader asks for next, and the
      // only place on this screen that states what the book returned rather than what it made.
      ['Total Return (time-weighted)', total === null ? 'UNKNOWN' : `${total < 0n ? '−' : '+'}${(Number(total < 0n ? -total : total) / 100).toFixed(2)}%`, total === null ? '' : overviewTone(total.toString())],
      ['Annualized Return', annualized, annualized === 'UNKNOWN' ? '' : overviewTone(performance.annualized_return_bps)],
    ];
    return `<header class="pnl-header"><h2>Balance Summary</h2><span class="cell-sub">${esc(span)}</span></header>`
      + `<div class="table-scroll"><table class="portfolio-table overview-performance"><tbody>`
      + rows.map(([label, value, tone]) => `<tr><th scope="row">${esc(label)}</th><td class="num${tone}">${esc(value)}</td></tr>`).join('')
      + `</tbody></table></div>`;
  }
  function overviewStrip(flows) {
    if (flows.status !== 'COMPLETE') return '';
    const plural = {day: 'Days', week: 'Weeks', month: 'Months'}[flows.unit] || 'Periods';
    const one = {day: 'day', week: 'week', month: 'month'}[flows.unit] || 'period';
    const {best, worst} = overviewExtremes(flows);
    const cells = [
      [String(flows.buckets.length), plural, '', ''],
      [overviewCompact(flows.net_cash_flow_usd_micros, true), 'Net Cash Contribution', overviewTone(flows.net_cash_flow_usd_micros), '(Inflows − outflows)'],
      [overviewCompact(flows.pnl_usd_micros, true), 'Total P&L', overviewTone(flows.pnl_usd_micros), '(Balance change less flows)'],
      [best ? overviewCompact(best.amount.toString(), true) : 'UNKNOWN', `Best P&L ${one}`, best ? overviewTone(best.amount.toString()) : '', best ? `(${best.label})` : ''],
      [worst ? overviewCompact(worst.amount.toString(), true) : 'UNKNOWN', `Worst P&L ${one}`, worst ? overviewTone(worst.amount.toString()) : '', worst ? `(${worst.label})` : ''],
    ];
    // The identity the screen is built on, stated once in the figures it was built from.
    const formula = `Opening ${overviewCompact(flows.starting_equity_usd_micros)}`
      + ` + net cash ${overviewCompact(flows.net_cash_flow_usd_micros, true)}`
      + ` + P&L ${overviewCompact(flows.pnl_usd_micros, true)}`
      + ` = ending ${overviewCompact(flows.ending_equity_usd_micros)}`;
    return `<section class="panel overview-strip">${cells.map(([value, label, tone, note]) =>
      `<div><b class="${tone.trim()}">${esc(value)}</b><small>${esc(label)}</small>${note ? `<small class="cell-sub">${esc(note)}</small>` : ''}</div>`).join('')}</section>`
      + `<section class="panel overview-formula"><b>Balance formula</b><span>${esc(formula)}</span></section>`;
  }
  // The platform's P&L per asset: transfers it verified against their receipts, valued at the
  // stored daily prices, against the balance read at the same block. Drawn only when the platform
  // answered, and its copy says how complete the history behind each figure is: a reconciled
  // balance does not establish coverage, so a round trip that nets to zero can be missing.
  function serverPerformancePanel(performance) {
    if (!performance || typeof performance !== "object" || !Array.isArray(performance.assets)) return "";
    const totals = performance.totals || {};
    const money = (micros) => micros == null ? "UNKNOWN" : usd(micros);
    const counted = `${totals.computed ?? 0} of ${totals.assets ?? performance.assets.length} assets computed`;
    const coverage = totals.complete
      ? `Complete: ${counted}, over histories whose coverage is established.`
      : totals.history_complete
        ? `Partial: ${counted}.`
        : `Partial: ${counted}, over the transfers supplied; history coverage is not established, so earlier buys and sells may be missing.`;
    const short = (address) => typeof address === "string" && address.length > 14 ? `${address.slice(0, 8)}…${address.slice(-6)}` : String(address || "UNKNOWN");
    const status = (asset) => asset.status === "COMPUTED"
      ? (asset.history_complete ? "Computed" : "Computed · history coverage not established")
      : `Not computable · ${asset.reason || "UNKNOWN"}`;
    const rows = performance.assets.map((asset) => `<tr><td>${esc(chainName(asset.chain_id))}</td><td class="mono">${esc(short(asset.token))}</td><td class="mono">${esc(String(asset.transfers ?? "UNKNOWN"))}</td><td>${esc(money(asset.inflow_usd_micros))}</td><td>${esc(money(asset.outflow_usd_micros))}</td><td>${esc(money(asset.current_value_usd_micros))}</td><td>${esc(money(asset.pnl_usd_micros))}</td><td>${esc(status(asset))}</td></tr>`).join("");
    return `<section class="panel verified-pnl"><header><div><h2>Verified P&amp;L</h2><p>${esc(performance.method || "")}</p></div></header><div class="portfolio-scope"><b>Total P&amp;L ${esc(money(totals.pnl_usd_micros))}</b> · current value ${esc(money(totals.current_value_usd_micros))} · net flows ${esc(money(totals.net_flows_usd_micros))}<br>${esc(coverage)}</div><div class="table-scroll"><table class="portfolio-table"><thead><tr><th scope="col">Chain</th><th scope="col">Token</th><th scope="col">Transfers</th><th scope="col">In (USD)</th><th scope="col">Out (USD)</th><th scope="col">Now (USD)</th><th scope="col">P&amp;L (USD)</th><th scope="col">Status</th></tr></thead><tbody>${rows || `<tr><td colspan="8">No assets</td></tr>`}</tbody></table></div></section>`;
  }

  function performanceScreen(snapshot, options = {}) {
    const performance = performanceRecord(snapshot, options), complete = performance.status === 'COMPLETE';
    const value = key => complete ? performanceMoney(performance[key]) : 'UNKNOWN';
    const reason = performance.reason || performance.source;
    const rows = [['Total return (time-weighted)',complete ? performancePct(performance.total_return_bps) : 'UNKNOWN'],['Cumulative P&L',value('cumulative_pnl_usd_micros')],['Best day (UTC P&L)',value('best_day_usd_micros')],['Worst day (UTC P&L)',value('worst_day_usd_micros')],['Tracking since',complete ? performance.tracking_since || 'UNKNOWN' : 'UNKNOWN']];
    const periods = ['7D','30D'].map(key=> {const period = performance.periods?.[key];return `<tr><th scope="row">${key} P&amp;L</th><td>${esc(complete && period?.status === 'COMPLETE' ? performanceMoney(period.pnl_usd_micros) : 'UNKNOWN')}<small class="cell-sub">${esc(period?.reason || (performance.declared ? performance.reason : complete ? 'Declared demo accrual' : reason))}</small></td></tr>`;}).join('');
    const scanning = options.transferHistory?.status === 'LOADING';
    // One button, because there is no longer a shallow scan to choose between: connecting runs
    // the full one, and this repeats it.
    const scan = options.connected && !performance.declared ? `<button type="button" class="ghost" id="scan-transfers"${scanning ? ' disabled' : ''}>${scanning ? 'Scanning…' : 'Rescan transfers'}</button>` : '';
    // The capital-flow dashboard leads, because what came in and what was made are the two
    // questions this screen exists to keep apart. Beneath it, the same P&L panel the Summary
    // draws -- one implementation, because two of them is how they drifted into disagreeing
    // about what a period is and which figure belongs under the amount.
    const flows = overviewBuckets(performance, options);
    const overview = `<p class="overview-identity">Opening balance + inflows − outflows + P&amp;L = ending balance</p>`
      + overviewCards(performance, flows, options)
      + `<div class="portfolio-grid overview-grid-main">`
      + `<section class="panel flow-panel overview-panel">${overviewPanel(performance, flows, options)}</section>`
      + `<div class="overview-side"><section class="panel flow-panel">${overviewBreakdown(flows)}</section>`
      + `<section class="panel flow-panel">${overviewSummary(performance, flows)}</section></div></div>`
      + overviewStrip(flows);
    return `${overview}${serverPerformancePanel(options.serverPerformance)}<div class="portfolio-grid"><section class="panel portfolio-pnl">${portfolioPnl(snapshot, performance, options)}</section><section class="panel"><h2>Performance Summary</h2><div class="table-scroll"><table class="portfolio-table"><tbody>${rows.map(([label,amount])=>`<tr><th scope="row">${esc(label)}</th><td>${esc(amount)}</td></tr>`).join('')}${periods}</tbody></table></div></section><section class="panel"><h2>Method</h2><p>Registered assets on the connected chain only. Transfer balances must reconcile exactly with balanceOf at the same finalized block before any history is emitted. Each historical NAV values every asset at that timestamp. P&amp;L equals NAV less cumulative net external flows, with deposits and withdrawals valued when they happened.</p><p>Total return links portfolio returns between flows. Annualized return compounds that time-weighted return only after at least 365 days of funded history. Best and worst day use complete UTC days; period deltas require the entire window.</p><p>Demo prices use declared_demo synthetic accrual, not measured prices or market returns. The sample book retains its declared NAV series; absent sample flows remain UNKNOWN. All derivation stays in this browser.</p>${scan}</section></div>`;
  }
  function portfolioLink(tab, label, dimension = "") {
    return `<button type="button" class="ghost" data-portfolio-tab="${esc(tab)}"${dimension ? ` data-exposure-dimension="${esc(dimension)}"` : ""}>${esc(label)} →</button>`;
  }
  function portfolioTable(rows, compact = false) {
    const headers = compact ? ["Asset", "Type", "Chain", "Value", "Yield (APY)", "Maturity"]
      : ["Asset", "Type", "Chain", "Amount", "Value (USD)", "Yield (APY)", "Maturity", "Coverage", "Status", "Actions"];
    return `<div class="table-scroll"><table class="portfolio-table${compact ? " portfolio-table-compact" : ""}"><thead><tr>${headers.map(h => `<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>${rows.map(row => {
      // Suppress only what is actually unknown. A position can have a read balance and no
      // authoritative price (reason MISSING_PRICE), and hiding the quantity there states that
      // the balance was not read, which is a different and untrue thing.
      const amount = tokenQuantity(row.raw_units, row.decimals);
      const fields = [
        (compact
          ? `<div class="identity-cell">${identity("token", row.symbol || row.name || row.id, row.symbol || row.name || row.id, [["issuer", row.issuer], ["venue", row.venue]])}</div>`
          : `<div class="identity-cell">${identity("token", row.symbol || row.name || row.id, row.symbol || row.name || row.id, [["issuer", row.issuer], ["venue", row.venue]])}<div><b>${esc(row.name || row.symbol || row.id)}</b><small class="cell-sub mono">${esc(row.symbol || row.id)}${row.chain_id === 84532 ? " · Base Sepolia testnet" : ""}</small></div></div>`),
        esc(holdingClassLabel(row)), esc(row.chain || "UNKNOWN"),
        ...(!compact ? [esc(amount === NOT_COMPUTABLE ? "UNKNOWN" : amount)] : []),
        esc(compact ? portfolioMoneyWhole(row.value_usd_micros) : portfolioMoney(row.value_usd_micros)),
        (compact
          ? `<span title="${esc(rateKindNote(row))}">${esc(portfolioPercent(row.yield_bps))}</span>`
          : `${esc(portfolioPercent(row.yield_bps))}${row.yield_bps == null ? "" : `<small class="cell-sub">${esc(rateKindNote(row))}</small>`}`),
        esc(["N/A", "UNKNOWN"].includes(row.maturity_bucket) ? row.maturity_bucket : row.maturity || (row.days_to_maturity == null ? "UNKNOWN" : `${row.days_to_maturity} days`)),
        ...(!compact ? [row.value_usd_micros == null ? `UNKNOWN · ${esc(stateText(row.reason || "MISSING_PRICE"))}${row.adapter_error?.detail ? ` · ${esc(row.adapter_error.detail)}` : ""}` : "Priced evidence", chip(row.status), row.market_index == null ? "UNKNOWN · Instrument unavailable" : `<button type="button" class="ghost" data-open-chain="${esc(String(row.chain_id))}" data-open-address="${esc(String(row.address || "").toLowerCase())}" data-open-identity="${esc(String(row.instrument_identity || ""))}">Open instrument</button>`] : [])
      ];
      return `<tr data-holding-identity="${esc(row.identity)}">${fields.map(f => `<td>${f}</td>`).join("")}</tr>`;
    }).join("") || `<tr><td colspan="${headers.length}">No assets match these filters.</td></tr>`}</tbody></table></div>`;
  }
  function portfolioKpis(snapshot, rows, tab, options = {}) {
    // Server venue claims are rendered and valued separately from token holdings.
    if (tab === "summary" && options.serverPortfolio) {
      rows = rows.filter(row => row.position_type !== "position");
    }
    const exposure = P.portfolioExposure(rows, "asset_type", snapshot.provenance?.as_of);
    const priced = rows.filter(row => row.value_usd_micros != null).length;
    const allPriced = priced === rows.length && rows.length > 0;
    let coverage = snapshot.valuation ? snapshot.valuation.value_coverage_bps : allPriced ? 10000 : null;
    let nav = allPriced ? portfolioMoneyWhole(exposure.total_usd_micros) : "UNKNOWN";
    let navDetail = allPriced ? "Value of discovered assets · REGISTERED_ASSETS_ONLY" : `Total value unavailable. Priced lower bound: ${portfolioMoney(exposure.total_usd_micros)}`;
    let coverageNote = `${priced} / ${rows.length} assets priced`;
    let navHasMovement = allPriced;
    // Venue claims contribute to NAV, but never become token holdings or routing capital.
    if (tab === "summary" && options.serverPortfolio) {
      const venues = options.serverPortfolio.positions || [];
      const values = venues.map(position => asBigInt(position.present_value_usd_micros));
      const venuePriced = values.filter(value => value !== null).length;
      const accounted = (asBigInt(exposure.total_usd_micros) ?? 0n)
        + values.reduce((sum, value) => sum + (value ?? 0n), 0n);
      const totals = options.serverPortfolio.totals;
      const serverTotal = asBigInt(totals?.value_usd_micros);
      const mismatch = totals?.value_usd_micros != null && serverTotal !== accounted;
      const partial = priced !== rows.length || venuePriced !== venues.length || mismatch || totals?.complete === false;
      const total = serverTotal !== null && !mismatch ? serverTotal : accounted;
      const knownValue = priced + venuePriced > 0 || rows.length + venues.length === 0;
      nav = `${knownValue ? portfolioMoneyWhole(total.toString()) : "UNKNOWN"}${partial ? " · Partial" : ""}`;
      navDetail = `Value of discovered tokens and fixed-rate positions${partial ? " · Partial: only priced values included" : ""}${mismatch ? " · Server total does not match the displayed positions" : ""}`;
      if (venues.length) {
        coverage = Math.floor((priced + venuePriced) * 10000 / (rows.length + venues.length));
        coverageNote = `${priced} / ${rows.length} tokens priced · ${venuePriced} / ${venues.length} fixed-rate positions priced`;
      }
      // Token history cannot establish the movement of a NAV that also includes venue claims.
      navHasMovement = !partial && allPriced && !venues.length;
    }
    const yieldValue = portfolioPercent(P.weightedPortfolioYield(rows));
    const kinds = [...new Set(rows.filter(row => row.yield_bps != null).map(row => rateKindNote(row)))].sort();
    const yieldDetail = yieldValue === "UNKNOWN" ? "Complete valuation and yield evidence required"
      : `Value weighted headline APY · ${kinds.length ? kinds.join(" · ") : "rate kind UNKNOWN"}`;
    const performance = performanceRecord(snapshot, options);
    // A card's third line is either a statement about scope or a movement, and a movement is
    // drawn as one: an arrow, a sign, and green or red. A measured zero is neither a gain nor a
    // loss and is drawn as neither -- flat and grey, so it reads as "this did not move" rather
    // than as a colourless gain. It is only ever shown for a window the record actually covers;
    // an uncovered window is UNKNOWN, and printing a nought for it would be the exact mistake
    // of stating an unmeasured thing as zero.
    //
    // The longest covered window wins, so a book too young for thirty days still states the
    // seven-day change rather than only the sentence explaining the absence of the other.
    const sign = (negative, flat) => flat ? "" : negative ? "▼ −" : "▲ +";
    const drawn = (negative, zero, text, window) => zero
      ? {text: `${text} (${window})`, flat: true}
      : {text: `${sign(negative)}${text} (${window})`, positive: !negative};
    // The change in what the portfolio is worth, which includes money paid in and taken out.
    // The card's tooltip says so, because a deposit is not a gain.
    const navMovement = () => {
      if (performance.status !== "COMPLETE") return null;
      for (const window of ["30D", "7D"]) {
        const period = performance.periods?.[window];
        if (period?.covered !== true || period?.change_bps == null) continue;
        const bps = Number(period.change_bps);
        if (!Number.isFinite(bps)) continue;
        return drawn(bps < 0, bps === 0, `${Math.abs(bps / 100).toFixed(2)}%`, window);
      }
      return null;
    };
    // The money actually made over the window. This is deliberately not the NAV change: with
    // $735,663 paid into this book in a day, the two differ by two orders of magnitude, and
    // showing the NAV's percentage here read as a 47% return on a $1,160 profit. A P&L has no
    // percentage of its own to state -- the base it would divide by is the thing the other card
    // already shows -- so it states the amount.
    // A weighted yield's change is in percentage points, not percent: it is the difference
    // between two percentages, and calling that "%" invites reading it as a relative change.
    const yieldMovement = () => {
      if (performance.status !== "COMPLETE") return null;
      for (const window of ["30D", "7D"]) {
        const period = performance.periods?.[window];
        if (period?.covered !== true || period?.yield_change_bps == null) continue;
        const bps = Number(period.yield_change_bps);
        if (!Number.isFinite(bps)) continue;
        return drawn(bps < 0, bps === 0, `${Math.abs(bps / 100).toFixed(2)}pp`, window);
      }
      return null;
    };
    const pnlMovement = () => {
      if (performance.status !== "COMPLETE") return null;
      for (const window of ["30D", "7D"]) {
        const period = performance.periods?.[window];
        if (period?.status !== "COMPLETE" || period?.pnl_usd_micros == null) continue;
        const amount = asBigInt(period.pnl_usd_micros);
        if (amount === null) continue;
        const magnitude = performanceMoney((amount < 0n ? -amount : amount).toString());
        if (magnitude === "UNKNOWN") continue;
        // Not trimmed to whole dollars the way the headline is: a period figure can be small,
        // and dropping the cents from thirty of them prints a profit as nothing.
        return drawn(amount < 0n, amount === 0n, magnitude, window);
      }
      return null;
    };
    // A P&L is a movement, so it is signed, and the cents on a figure this size are noise the
    // headline does not need -- the Performance screen still prints it exactly.
    const rawPnl = performance.status === "COMPLETE" ? performanceMoney(performance.cumulative_pnl_usd_micros) : "UNKNOWN";
    const pnlPositive = rawPnl !== "UNKNOWN" && (performance.cumulative_pnl_usd_micros ?? "0").toString()[0] !== "-";
    const pnl = rawPnl === "UNKNOWN" || rawPnl === "N/A"
      ? rawPnl
      : `${pnlPositive ? "+" : ""}${rawPnl.replace(/\.\d{2}$/, "")}`;
    const scanning = scanProgress(options);
    const metrics = tab === "holdings"
      ? [{label: "Total Value", value: nav, detail: navDetail},
         {label: "Assets", value: String(rows.length), detail: "Discovered assets only"},
         {label: "Valuation Coverage", value: portfolioPercentTrim(coverage), detail: `${priced} / ${rows.length} assets priced · discovered assets only`},
         {label: "Weighted Yield", value: yieldValue, detail: yieldDetail}]
      : [{label: "Portfolio Value (NAV)", value: nav, detail: navHasMovement && navMovement() ? `${navDetail} · The change includes deposits and withdrawals, so it is not a return` : navDetail, delta: navHasMovement ? navMovement() : null},
         {label: "Valuation Coverage", value: portfolioPercentTrim(coverage), detail: `${coverageNote} · discovered assets only`, note: coverageNote},
         {label: "Weighted Portfolio Yield", value: yieldValue, detail: yieldDetail, delta: yieldMovement()},
         // With no external flows the change in NAV is the P&L, which is why the same movement
         // belongs on both cards. Where flows exist the record leaves periods empty and this
         // falls back to naming the window's P&L instead.
         {label: "Cumulative P&L", value: pnl, detail: scanning && pnl === "UNKNOWN" ? scanning : performanceDelta(performance),
          positive: pnlPositive, delta: pnlMovement()}];
    const info = (label, detail) => `<svg class="portfolio-info" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" role="img" aria-label="${esc(label)} information"><title>${esc(detail)}</title><circle cx="12" cy="12" r="9"></circle><path d="M12 11v6 M12 7v1"></path></svg>`;
    return `<div class="kpi-grid portfolio-kpis">${metrics.map((metric) => {
      const summary = tab === "summary";
      const delta = summary ? metric.delta : null;
      const line = delta ? delta.text : (summary && metric.note) || metric.detail;
      const lineClass = delta
        ? ` portfolio-delta ${delta.flat ? "portfolio-flat" : delta.positive ? "portfolio-positive" : "portfolio-negative"}`
        : "";
      const valueClass = summary && metric.positive ? " portfolio-positive" : "";
      return `<section class="metric-card"><div class="label">${metric.label}${summary ? info(metric.label, metric.detail) : ""}</div><div class="value${valueClass}">${esc(metric.value)}</div><div class="detail${lineClass}">${esc(line)}</div></section>`;
    }).join("")}</div>`;
  }
  function valuedRiskPanel(snapshot) {
    const scope = snapshot?.risk_scope;
    if (!scope || scope.nav_scope !== "VALUED_HOLDINGS") return "";
    const exclusions = Array.isArray(scope.excluded) ? scope.excluded : [];
    const excludedCount = exclusions.length;
    const metricRows = [
      ["Expected Shortfall 95% (one day)", snapshot.analytics?.expected_shortfall_95],
      ["Volatility (annualized)", snapshot.analytics?.volatility],
      ["Maximum Drawdown", snapshot.analytics?.drawdown],
    ].map(([label, metric]) => {
      const value = metric?.status === "COMPUTED" && metric.value != null
        ? (String(metric.unit || "").startsWith("BPS") ? pct(metric.value) : textOr(metric.value))
        : explain(metric?.reason_code || metric?.status || NOT_COMPUTABLE);
      return `<div class="metric-card"><div class="label">${label}</div><div class="value">${esc(value)}</div></div>`;
    }).join("");
    const excluded = exclusions.map(item => {
      const identity = textOr(item?.symbol || item?.identity, "Unknown holding");
      const reason = item?.reason ? ` · ${explain(item.reason)}` : "";
      return `<li>${esc(identity)}${esc(reason)}</li>`;
    }).join("");
    const modelledCount = Number.isSafeInteger(scope.modelled_positions) && scope.modelled_positions > 0
      ? scope.modelled_positions : 0;
    // Valued but without usable daily history: the engine leaves these out of the risk itself, which a
    // reader of "risk over valued holdings" would otherwise assume covers every valued position.
    const coverage = snapshot.analytics?.risk_coverage_bps;
    const valuedIds = new Set(exclusions.map(item => item?.identity));
    const symbols = new Map((snapshot.valuation?.positions || []).map(row => [row?.identity, row?.position?.raw_position?.asset?.symbol]));
    const withoutHistory = (Array.isArray(snapshot.analytics?.excluded_positions) ? snapshot.analytics.excluded_positions : [])
      .filter(identity => !valuedIds.has(identity))
      .map(identity => `<li>${esc(textOr(symbols.get(identity) || identity, "Unknown holding"))}</li>`).join("");
    const coverageLine = Number.isSafeInteger(coverage)
      ? `<p>Daily price history covers ${esc(pct(coverage))} of valued holdings${withoutHistory ? "; left out of the risk for lack of usable history:" : "."}</p>${withoutHistory ? `<ul>${withoutHistory}</ul>` : ""}`
      : "";
    return `<section class="panel valued-risk-panel"><header><h2>Risk over valued holdings · ${esc(usd(scope.valued_usd_micros))}</h2></header>
      <div class="kpi-grid asset-metrics">${metricRows}</div>
      ${coverageLine}
      ${excludedCount
        ? `<p>${excludedCount} ${excludedCount === 1 ? "holding could not be valued and is" : "holdings could not be valued and are"} excluded:</p><ul>${excluded}</ul>`
        : "<p>Every holding found could be valued.</p>"}
      ${modelledCount ? `<p>Modelled daily prices for ${modelledCount} ${modelledCount === 1 ? "position" : "positions"}: rate moves only, excluding default risk.</p>` : ""}</section>`;
  }
  // The risk dashboard (owner 2026-10-20): the metric cards, then four charts drawn from the same
  // aligned daily series the metrics come from, so the curves and the figures above them agree.
  function analyticsScreen(snapshot, universe = []) {
    const cards = valuedRiskPanel(snapshot);
    if (!cards) {
      // Two honest cases: no book at all, or a book the browser read itself, which carries no
      // daily history for the platform's risk metrics.
      return snapshot
        ? `<section class="panel"><h2>Risk over time</h2><p>UNKNOWN · This book was read by the browser, not the platform, so no daily history is attached. Risk metrics need 250 aligned daily observations and at least 90% coverage of the valued holdings; refresh the portfolio to read it through the platform.</p></section>`
        : `<section class="panel"><h2>Risk over time</h2><p>Connect a wallet, or watch one, to see the risk of its valued holdings: the one-day Expected Shortfall, volatility and drawdown, and the daily series they are computed from.</p></section>`;
    }
    const charts = root.FenoriumCharts || {};
    const returns = Array.isArray(snapshot.analytics?.daily_returns) ? snapshot.analytics.daily_returns : [];
    const valued = snapshot.risk_scope?.valued_usd_micros;
    const es95 = snapshot.analytics?.expected_shortfall_95?.status === "COMPUTED" && Number.isSafeInteger(snapshot.analytics.expected_shortfall_95.value)
      ? snapshot.analytics.expected_shortfall_95.value : null;
    const curves = typeof charts.riskCurves === "function" ? charts.riskCurves(returns, {valuedMicros: valued == null ? null : String(valued)}) : null;
    const histogram = typeof charts.returnHistogram === "function" ? charts.returnHistogram(returns, es95) : "";
    const asOf = snapshot.provenance?.as_of;
    const rows = typeof P.portfolioRows === "function" ? P.portfolioRows(snapshot.positions || [], universe || [], asOf) : [];
    const ladder = typeof charts.portfolioMaturity === "function" && typeof P.portfolioExposure === "function"
      ? charts.portfolioMaturity(P.portfolioExposure(rows, "maturity", asOf), {summary: true}) : "";
    const modelled = Number.isSafeInteger(snapshot.risk_scope?.modelled_positions) ? snapshot.risk_scope.modelled_positions : 0;
    const window = curves ? `${curves.observations} aligned days · ${returns[0].day} to ${returns[returns.length - 1].day}` : "No aligned daily series";
    const modelledNote = modelled ? ` ${modelled} position${modelled === 1 ? " is" : "s are"} on modelled daily prices (rate moves only; excludes default risk).` : "";
    const empty = `<div class="empty">UNKNOWN · No aligned daily series</div>`;
    return `${cards}<div class="chart-grid analytics-charts">
      <section class="panel"><header><h2>Valued holdings over the window</h2><span>${esc(window)}</span></header>${curves ? curves.nav : empty}<p class="cell-sub">Today's valued holdings walked back through each day's aligned return.${esc(modelledNote)} Not the account's balance history: deposits and withdrawals are not in it.</p></section>
      <section class="panel"><header><h2>Drawdown</h2><span>${curves ? `at most ${esc(pct(curves.max_drawdown_bps))}` : ""}</span></header>${curves ? curves.drawdown : empty}<p class="cell-sub">Distance below the running peak of that path, day by day.</p></section>
      <section class="panel"><header><h2>Daily return distribution</h2><span>${es95 != null ? `ES95 ${esc(pct(es95))} per day` : ""}</span></header>${histogram || empty}<p class="cell-sub">One bar per band of daily return. The coral bars hold the worst 5% of days; the marker is their average loss, the Expected Shortfall above.</p></section>
      <section class="panel"><header><h2>Maturity ladder</h2></header>${ladder || empty}<p class="cell-sub">Fixed-rate positions and principal tokens by time to maturity, at today's value.</p></section>
    </div>`;
  }
  function portfolioControls(rows, options) {
    // Keep an active restriction visible even when the snapshot no longer contains its value:
    // rendering "All" while still filtering shows an empty table under apparently no filter.
    const select = (key, label, values) => {
      const available = [...new Set(values)].sort();
      const active = options[key];
      const shown = active && !available.includes(active) ? [...available, active] : available;
      return `<label>${label}<select data-holdings-control="${key}"><option value="">All ${label.toLowerCase()}</option>${shown.map(v => `<option value="${esc(v)}"${active === v ? " selected" : ""}>${esc(portfolioLabel(v))}${active === v && !available.includes(v) ? " · not in this snapshot" : ""}</option>`).join("")}</select></label>`;
    };
    return `<div class="portfolio-controls"><label>Search holdings<input type="search" id="holdings-search" value="${esc(options.search || "")}" placeholder="Search assets…"></label>${select("asset_type", "Asset Type", rows.map(r => r.asset_type || "UNKNOWN"))}${select("chain", "Chain", rows.map(r => r.chain || "UNKNOWN"))}${select("status", "Status", rows.map(r => r.status))}<label>Sort<select data-holdings-control="sort">${[["value-desc", "Value: high to low"], ["value-asc", "Value: low to high"], ["name", "Asset name"], ["yield-desc", "Yield: high to low"], ["maturity", "Maturity"]].map(([v,l]) => `<option value="${v}"${(options.sort || "value-desc") === v ? " selected" : ""}>${l}</option>`).join("")}</select></label></div>`;
  }

  // Owner 2026-10-21: no Source column; what a row came from stays in its notes and status.
  const TRANSFER_HEADERS = ["Date (UTC)", "Action", "Asset", "Amount", "Value (USD)", "Chain", "Tx Hash", "Status", "Notes"];
  const TRANSFER_EXPLORERS = Object.freeze(Object.fromEntries((P.REGISTRY_CHAINS || [])
    .filter((chain) => chain.explorer).map((chain) => [chain.chain_id, chain.explorer])));

  function transferDate(value) {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime())
      ? date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC")
      : "UNKNOWN";
  }

  function transferAmount(row) {
    if (row?.decimals == null) return "UNKNOWN";
    const amount = tokenQuantity(row?.raw_amount, row?.decimals);
    if (amount === NOT_COMPUTABLE) return "UNKNOWN";
    const signed = amount.startsWith("-") ? amount : `+${amount}`;
    return `${signed}${row?.symbol ? ` ${row.symbol}` : ""}`;
  }

  function transferExplorer(row) {
    const base = TRANSFER_EXPLORERS[Number(row?.chain_id)];
    const hash = typeof row?.tx_hash === "string" ? row.tx_hash.trim() : "";
    if (!base || !/^0x[0-9a-fA-F]{64}$/.test(hash)) return null;
    return `${base}/tx/${encodeURIComponent(hash)}`;
  }

  function transferHash(row) {
    const hash = typeof row?.tx_hash === "string" ? row.tx_hash.trim() : "";
    if (!hash) return "UNKNOWN";
    const label = hash.length > 18 ? `${hash.slice(0, 10)}…${hash.slice(-8)}` : hash;
    const url = transferExplorer(row);
    const value = url ? `<a class="tx-link mono" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : `<span class="mono">${esc(label)}</span>`;
    return `${value} <button type="button" class="ghost" data-copy-transfer="${esc(hash)}" aria-label="Copy transaction hash">Copy</button>`;
  }

  function transferCellRecord(row) {
    return {
      date: transferDate(row?.timestamp),
      action: row?.action || "UNKNOWN",
      asset: row?.symbol || row?.name || row?.asset || "UNKNOWN",
      amount: row?.action === "Self" ? transferAmount(row).replace(/^\+/, "") : transferAmount(row),
      value_usd: row?.value_usd_micros == null ? "UNKNOWN" : usd(row.value_usd_micros),
      chain: row?.chain || chainName(row?.chain_id),
      source: row?.source || "UNKNOWN",
      tx_hash: row?.tx_hash || "UNKNOWN",
      tx_hash_display: transferHash(row),
      status: row?.status || "UNKNOWN",
      notes: row?.notes || "UNKNOWN",
    };
  }

  function transferDisplayCells(row) {
    const cells = transferCellRecord(row);
    return [cells.date, cells.action, cells.asset, cells.amount, cells.value_usd, cells.chain, cells.tx_hash, cells.status, cells.notes];
  }

  // A transfer from this wallet to itself is one log that the data keeps as two legs (so the
  // P&L nets them). On screen it is one row: the demo sale pays its proceeds to the deployer,
  // so the owner's own purchases printed "+3 mUSDC" beside "-3 mUSDC" (owner 2026-10-21).
  function collapseSelfTransfers(rows) {
    const legs = new Map();
    for (const row of rows) {
      if (!row?.from || row.from !== row.to) continue;
      const key = `${row.chain_id}:${String(row.tx_hash || "").toLowerCase()}:${row.log_index}:${String(row.asset_address || "").toLowerCase()}`;
      legs.set(key, (legs.get(key) || new Set()).add(row.action));
    }
    const out = [];
    for (const row of rows) {
      if (!row?.from || row.from !== row.to) { out.push(row); continue; }
      const key = `${row.chain_id}:${String(row.tx_hash || "").toLowerCase()}:${row.log_index}:${String(row.asset_address || "").toLowerCase()}`;
      const actions = legs.get(key);
      if (!actions?.has("Outgoing") || !actions.has("Incoming")) { out.push(row); continue; }
      if (row.action === "Outgoing") continue;
      const value = row.value_usd_micros == null ? null : String(row.value_usd_micros).replace(/^-/, "");
      out.push({...row, id: `${row.id}`.replace(/:incoming$/, ":self"), action: "Self",
        raw_amount: String(row.raw_amount).replace(/^-/, ""), value_usd_micros: value,
        notes: `Sent by this wallet to itself, so its balance did not change. ${row.notes || ""}`.trim()});
    }
    return out;
  }

  function transferCoverage(history, indexed, {loadingIndex = false, historyError = ""} = {}) {
    const assets = Array.isArray(history?.assets) ? history.assets : [];
    const indexedLines = (Array.isArray(indexed) ? indexed : []).map((entry) => {
      const chain = chainName(entry.chain_id);
      let text;
      if (entry.status === "INDEXED") {
        const coverage = (history.scanned_chains || []).find((row) => row.chain_id === entry.chain_id);
        text = `${chain}: indexed by Blockscout; ${coverage?.event_count ?? "UNKNOWN"} transfers, ${coverage?.verified_count ?? "UNKNOWN"} confirmed against the chain; ${coverage?.complete === true ? "the index reported the whole history" : "coverage not established"}`;
      } else if (entry.status === "UNAVAILABLE") {
        text = `${chain}: indexed history unavailable${entry.detail ? ` · ${entry.detail}` : ""}`;
      } else if (entry.status === "UNSUPPORTED") {
        text = `${chain}: no indexed history`;
      } else return "";
      return `<li>${esc(text)}</li>`;
    }).join("");
    const indexedList = indexedLines ? `<b>Indexed history</b><ul>${indexedLines}</ul>` : "";
    if (!assets.length) {
      // The index placeholder before the platform has answered: a read in progress, not a scan
      // that found nothing (owner 2026-10-21: it read as broken).
      if (loadingIndex) {
        return `<div class="portfolio-scope"><b>Reading this wallet's transfer history from the platform's index…</b> No block scan has run in this browser yet.</div>`;
      }
      if (historyError && history?.status === "UNAVAILABLE") {
        return `<div class="portfolio-scope"><b>Transfer history unavailable</b> · ${esc(historyError)} No block scan has run in this browser; Refresh asks the platform's index again.</div>`;
      }
      return `<div class="portfolio-scope"><b>Coverage UNKNOWN</b> · 0 blocks scanned before a transfer scan.${indexedList || " Other chains are unscanned."}</div>`;
    }
    const lines = assets.map((asset) => {
      const label = asset.asset?.symbol || asset.asset?.name || asset.asset?.address || "UNKNOWN asset";
      const chain = asset.asset?.chain || chainName(asset.asset?.chain_id);
      const scanned = Number.isFinite(Number(asset.scanned_blocks)) ? Number(asset.scanned_blocks) : 0;
      const from = asset.from_block == null ? "UNKNOWN" : asset.from_block;
      const to = asset.to_block == null ? "UNKNOWN" : asset.to_block;
      const range = scanned > 0 ? `${scanned.toLocaleString("en-US")} blocks (${from}–${to})` : "0 blocks";
      const status = asset.status === "COMPLETE" ? `Verified scan: ${range}; transfers before the oldest scanned block are UNKNOWN` : `UNKNOWN coverage: ${range}; transfers before the oldest scanned block are UNKNOWN`;
      const reason = asset.reason ? ` · ${esc(asset.reason)}` : "";
      return `<li><b>${esc(label)}</b> · ${esc(chain)}: ${esc(status)}${reason}</li>`;
    }).join("");
    return `<div class="portfolio-scope"><b>Transfer coverage</b><p>Declared scan depth: up to ${esc(history?.declared_depth ?? 90000)} blocks per asset, in windows of at most 9,000 blocks.</p><ul>${lines}</ul>${indexedList}<p>Older unscanned blocks and blocks after the scan endpoint are UNKNOWN. Other chains are unscanned unless listed above.</p></div>`;
  }

  function transferControls(rows, options = {}) {
    const values = (key) => [...new Set(rows.map((row) => row?.[key]).filter(Boolean))].sort();
    const select = (key, label) => {
      const available = values(key);
      const active = options[key] || "";
      const shown = active && !available.includes(active) ? [...available, active] : available;
      return `<label>${label}<select data-transfer-control="${key}"><option value="">All ${label.toLowerCase()}</option>${shown.map((value) => `<option value="${esc(value)}"${active === value ? " selected" : ""}>${esc(value)}${active === value && !available.includes(value) ? " · not in this snapshot" : ""}</option>`).join("")}</select></label>`;
    };
    const assetValues = [...new Map(rows.map((row) => [row?.asset_address || "UNKNOWN", row?.symbol || row?.name || row?.asset || row?.asset_address || "UNKNOWN"])).entries()];
    const activeAsset = options.asset || options.asset_address || "";
    const assetShown = activeAsset && !assetValues.some(([value]) => value === activeAsset) ? [[activeAsset, activeAsset], ...assetValues] : assetValues;
    const assetSelect = `<label>Asset<select data-transfer-control="asset"><option value="">All asset</option>${assetShown.map(([value, label]) => `<option value="${esc(value)}"${activeAsset === value ? " selected" : ""}>${esc(label)}${activeAsset === value && !assetValues.some(([known]) => known === value) ? " · not in this snapshot" : ""}</option>`).join("")}</select></label>`;
    return `<div class="portfolio-controls transfer-controls">${assetSelect}${select("action", "Action")}${select("chain", "Chain")}<label>From<input type="date" data-transfer-control="from" value="${esc(options.from || "")}"></label><label>To<input type="date" data-transfer-control="to" value="${esc(options.to || "")}"></label></div>`;
  }

  function transactionTable(rows, {loadingIndex = false, historyError = ""} = {}) {
    const body = collapseSelfTransfers(rows).map((row) => {
      const cells = transferCellRecord(row);
      return `<tr><td>${esc(cells.date)}</td><td>${esc(cells.action)}</td><td>${esc(cells.asset)}</td><td class="mono">${esc(cells.amount)}</td><td>${esc(cells.value_usd)}</td><td>${esc(cells.chain)}</td><td>${cells.tx_hash_display}</td><td>${esc(cells.status)}</td><td>${esc(cells.notes)}</td></tr>`;
    }).join("");
    const empty = loadingIndex
      ? "Reading this wallet's transfer history from the platform's index…"
      : historyError
        ? "Transfer history unavailable; see the reason above."
        : "No matching verified rows in this scope. Review transfer coverage above; activity outside the declared interval is UNKNOWN.";
    return `<div class="table-scroll"><table class="portfolio-table transactions-table"><thead><tr>${TRANSFER_HEADERS.map((header) => `<th scope="col">${header}</th>`).join("")}</tr></thead><tbody>${body || `<tr><td colspan="${TRANSFER_HEADERS.length}">${empty}</td></tr>`}</tbody></table></div>`;
  }

  function valuedTransferRows(rows, transferValues) {
    const values = transferValues && typeof transferValues === "object" ? transferValues : null;
    return values ? rows.map((row) => {
      const key = `${Number(row?.chain_id)}:${String(row?.tx_hash || "").toLowerCase()}:${Number(row?.log_index)}`;
      const value = values[key]?.value_usd_micros;
      return value == null ? row : {...row, value_usd_micros: String(row.raw_amount).startsWith("-") ? "-" + value : value};
    }) : rows;
  }

  function allTransferRows(scanned, indexedRows, transferValues) {
    const rows = Array.isArray(scanned) ? scanned.slice() : [];
    const ids = new Set(rows.map((row) => row.id));
    for (const row of Array.isArray(indexedRows) ? indexedRows : []) {
      if (ids.has(row.id)) continue;
      ids.add(row.id);
      rows.push(row);
    }
    return valuedTransferRows(rows, transferValues);
  }

  function transactionScreen(history, options = {}) {
    if (Array.isArray(options.simulatedActivity)) {
      const events = options.simulatedActivity;
      const summary = events.length
        ? `${events.length} simulated activity entries · no transactions were submitted.`
        : "No simulated activity is included in this scenario.";
      const headers = ["Date", "Event", "Amount", "Asset", "Chain", "Status"];
      const rows = events.map((event) => `<tr>${[event.day, event.kind, event.amount, event.symbol, `Chain ${event.chain_id}`, "Simulated"].map((value) => `<td>${esc(value ?? "UNKNOWN")}</td>`).join("")}</tr>`).join("");
      return `<section class="panel transactions-panel"><header><div><h2>Transfer history</h2><p>${esc(summary)}</p></div></header><p class="portfolio-scope">Synthetic scenario events. They have no transaction hashes and are not verified against a chain.</p><div class="table-scroll"><table class="portfolio-table transactions-table"><thead><tr>${headers.map((label) => `<th scope="col">${label}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div></section>`;
    }
    const snapshot = history || { status: "UNKNOWN", assets: [], rows: [] };
    const scanned = Array.isArray(snapshot.rows) ? snapshot.rows : [];
    const allRows = allTransferRows(scanned, options.indexedRows, options.transferValues);
    // What the screen presents: one row per self-transfer, folded before any filter, control or
    // export sees the rows, so a filter cannot turn a Self row back into a signed leg (review dq).
    const presented = collapseSelfTransfers(allRows);
    const queryOptions = {...options, asset_address: options.asset || options.asset_address};
    let rows = presented;
    if (typeof P.queryTransfers === "function") {
      try { rows = P.queryTransfers(presented, queryOptions) || []; } catch (_) { rows = []; }
    }
    const loading = snapshot.status === "LOADING";
    // The index placeholder with nothing back yet is a read in progress; with an error recorded
    // it is a failure, and says so (review dq).
    const historyError = options.historyError ? String(options.historyError) : "";
    const indexPending = snapshot.status === "UNAVAILABLE" && !Array.isArray(options.indexedRows) && !allRows.length;
    const loadingIndex = indexPending && !historyError;
    const errorValue = snapshot.error && typeof snapshot.error === "object"
      ? [snapshot.error.code, snapshot.error.message].filter(Boolean).join(" · ")
      : snapshot.error;
    const error = errorValue ? `<p class="portfolio-scope">Transfer scan failed: ${esc(errorValue)}</p>` : "";
    // With browser-scanned rows on screen the failed index read is said here: its rows are gone
    // from the table, and nothing else would say why.
    const historyFailure = historyError && !indexPending
      ? `<p class="portfolio-scope">Indexed transfer history unavailable · ${esc(historyError)}</p>` : "";
    const emptyReason = snapshot.status === "UNKNOWN" && !allRows.length
      ? "Transfer history is UNKNOWN. Failed or unscanned ranges cannot establish no activity; see coverage above."
      : "";
    const controlRows = presented.concat((snapshot.assets || []).map((entry) => ({
      asset_address: entry.asset?.address,
      symbol: entry.asset?.symbol,
      name: entry.asset?.name,
      chain: entry.asset?.chain,
      chain_id: entry.asset?.chain_id,
    })));
    const content = options.transferScanSuppressed
      ? `<p class="portfolio-scope">This browser's transfer history was forgotten. Refresh the portfolio to read it again.</p>`
      : `${options.historyStatus ? `<p class="portfolio-scope">${esc(options.historyStatus.summary)}</p>${options.historyStatus.coverage ? `<p class="cell-sub">${esc(options.historyStatus.coverage)}</p>` : ""}` : ""}${transferCoverage({...snapshot, scanned_chains: options.scannedChains}, options.indexed, {loadingIndex, historyError: indexPending ? historyError : ""})}${error}${historyFailure}${emptyReason ? `<p class="portfolio-scope">${esc(emptyReason)}</p>` : ""}${transferControls(controlRows, options)}${transactionTable(rows, {loadingIndex, historyError: indexPending ? historyError : ""})}`;
    return `<section class="panel transactions-panel"><header><div><h2>Transfer history</h2><p>Transfers this browser scanned on the connected chain, plus indexed history where the platform could read it.</p></div><div><button type="button" class="ghost" id="scan-transfers"${loading ? " disabled" : ""}>${loading ? "Scanning…" : "Scan full history"}</button> <button type="button" class="ghost" id="download-transactions"${rows.length ? "" : " disabled"}>Download CSV</button></div></header>${content}</section>`;
  }

  function transactionsCsv(rows) {
    const quote = (value) => `"${String(value == null ? "UNKNOWN" : value).replaceAll('"', '""')}"`;
    return [TRANSFER_HEADERS, ...collapseSelfTransfers(rows).map((row) => {
      return transferDisplayCells(row);
    })].map((line) => line.map(quote).join(",")).join("\r\n");
  }
  function portfolioExposureScreen(rows, options, asOf) {
    const dimensions = [["asset_type", "By Asset Type"], ["chain", "By Chain"], ["issuer", "By Issuer"], ["venue", "By Protocol / Venue"], ["maturity", "By Maturity"]];
    const dimension = options.dimension || "asset_type";
    const exposure = P.portfolioExposure(rows, dimension, asOf);
    const share = row => portfolioPercent(row.share_bps);
    const bars = exposure.rows.map(row => `<div class="portfolio-share-row"><div><b>${esc(portfolioLabel(row.label))}</b><span>${esc(options.unit === "usd" ? portfolioMoney(row.value_usd_micros) : share(row))}</span></div>${row.share_bps == null ? '<p>UNKNOWN · No priced value</p>' : `<svg viewBox="0 0 100 3" role="img" aria-label="${esc(portfolioLabel(row.label))}: ${esc(share(row))} of priced value"><rect width="100" height="3" fill="#1c2d3c"></rect><rect width="${row.share_bps / 100}" height="3" fill="#27c7d8"></rect></svg>`}</div>`).join("");
    return `<div class="portfolio-dimensions" aria-label="Exposure dimension">${dimensions.map(([key,label]) => `<button type="button" data-exposure-dimension="${key}" aria-pressed="${dimension === key}">${label}</button>`).join("")}</div><div class="portfolio-grid"><section class="panel"><h2>Portfolio Exposure</h2>${root.FenoriumCharts.portfolioDonut(exposure)}</section><section class="panel"><header><h2>Share of priced value</h2><div><button type="button" data-exposure-unit="percent" aria-pressed="${options.unit !== "usd"}">%</button><button type="button" data-exposure-unit="usd" aria-pressed="${options.unit === "usd"}">$</button></div></header>${bars}</section><section class="panel"><h2>Exposure Breakdown</h2><div class="table-scroll"><table><thead><tr><th>Category</th><th>Assets</th><th>Value (USD)</th><th>Share</th></tr></thead><tbody>${exposure.rows.map(row => `<tr><td>${esc(portfolioLabel(row.label))}</td><td>${row.assets.length}<small class="cell-sub">${esc(row.assets.map(a => a.name || a.symbol || a.id).join(", "))}</small></td><td>${esc(portfolioMoney(row.value_usd_micros))}${row.unresolved_count ? `<small class="cell-sub">${row.unresolved_count} unpriced · Priced lower bound ${esc(portfolioMoney(row.priced_value_usd_micros))}</small>` : ""}</td><td>${esc(share(row))}</td></tr>`).join("")}</tbody></table></div></section><section class="panel"><h2>About Exposure</h2><p>Shares use priced discovered assets only. Discovery is REGISTERED_ASSETS_ONLY, not the whole wallet. Unknown values are excluded from the priced denominator and remain visible; category totals with missing values are UNKNOWN. Missing category metadata is UNKNOWN; a dimension that does not apply is N/A.</p></section></div>`;
  }
  // The reference titles this card with the date its history starts, which is exactly the
  // caveat the panel needs to carry: a curve is only as old as the record behind it.
  // Prefers the record the panel is actually drawing. Rebuilding one from the snapshot alone
  // finds nothing for a connected wallet, whose history comes from the transfer scan, so the
  // period the chart covers went unstated on every real book.
  function trackingSince(snapshot, performance = null) {
    const points = (performance || performanceRecord(snapshot))?.points;
    const first = Array.isArray(points) && points.length ? points[0].as_of : null;
    return first ? ` <span class="panel-since">(Since ${esc(String(first).slice(0, 10))})</span>` : "";
  }

  // The panel's own header: title, the period it covers, and the controls on one row, with the
  // provenance behind the same info affordance the cards use rather than two paragraphs of it
  // under the chart.
  // What a range button selects, for every chart that has one. Filtered by time, not by how
  // many marks happen to fall in it: counting marks made "1W" mean "the last eight
  // observations", and a day with several transfers in it produces several marks -- so a week's
  // button was showing two days of a book that had just been funded, and the figure beside it
  // was that of two days.
  const PNL_WINDOWS = {"1W": 7, "1M": 30, "3M": 90};
  function pnlWindow(all, options = {}) {
    const range = String(options.pnlRange || "ALL").toUpperCase();
    const points = Array.isArray(all) ? all : [];
    const cutoff = PNL_WINDOWS[range] && points.length
      ? Date.parse(points.at(-1).as_of) - PNL_WINDOWS[range] * 86400000
      : null;
    const within = cutoff === null || Number.isNaN(cutoff)
      ? points
      : points.filter((point) => Date.parse(point.as_of) >= cutoff);
    // A window needs two ends to be a window. One that catches fewer marks than that keeps the
    // last two rather than drawing a single dot and calling it a period.
    const selected = within.length >= 2 ? within : points.slice(-2);
    // The mark before the window opens, which is what the period's opening balance and its
    // first period's flows are measured from. Without it a filtered range charged every deposit
    // the book ever took to its first bar. A window that opens at the start of history has no
    // such mark, and its opening balance is nought -- the wallet held none of these assets.
    const first = points.indexOf(selected[0]);
    return {range, points: selected, before: first > 0 ? points[first - 1] : null};
  }
  function pnlHeader(snapshot, performance, controls = "", caveat = "") {
    const provenance = [performance.source
      || 'Derived from finalized ERC-20 transfers; declared_demo synthetic accrual, not measured market returns',
      caveat].filter(Boolean).join(' · ');
    const info = `<svg class="portfolio-info" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" role="img" aria-label="Portfolio P&amp;L information"><title>${esc(provenance)}</title><circle cx="12" cy="12" r="9"></circle><path d="M12 11v6 M12 7v1"></path></svg>`;
    return `<header class="pnl-header"><h2>Portfolio P&amp;L${trackingSince(snapshot, performance)}${info}</h2>${controls}</header>`;
  }
  function portfolioPnl(snapshot, performance = performanceRecord(snapshot), options = {}) {
    if (performance.status !== 'COMPLETE' || !performance.points?.length) {
      const scanning = scanProgress(options);
      return pnlHeader(snapshot, performance)
        + `<div class="value">UNKNOWN</div><p>${esc(scanning || performance.reason || 'Derived portfolio history is unavailable')}</p>`;
    }
    let detail = performanceDelta(performance), amount = performance.cumulative_pnl_usd_micros;
    if (performance.declared) {
      const first = asBigInt(performance.points[0].nav_usd_micros), last = asBigInt(performance.points.at(-1).nav_usd_micros);
      const change = first === null || last === null ? null : last - first;
      detail = `Declared NAV change ${change !== null && change >= 0n ? '+' : ''}${performanceMoney(change?.toString())}; flow-adjusted P&L UNKNOWN`;
    }
    const unit = String(options.pnlUnit || "usd").toLowerCase();
    const {range, points} = pnlWindow(performance.points, options);
    // Percent needs a return at every point to plot. Where the record has none -- a declared
    // series that never said what was paid in -- the button is disabled rather than left to
    // look live and change nothing, which is the state it was in before it did anything at all.
    // The same rule the chart applies: the opening mark has no return yet -- there was no
    // capital to return on -- and it is the base the rest are measured against, so it is
    // allowed to be absent. Requiring one there disabled the button on every real book.
    const returnable = points.length > 1 && points.slice(1).every((point) => point.return_bps != null);
    const controls = `<div class="pnl-controls">`
      + `<div class="pnl-unit">${[["usd", "$"], ["pct", "%"]].map(([key, label]) =>
          `<button type="button" data-pnl-unit="${key}" aria-pressed="${unit === key && (key === "usd" || returnable)}"${key === "pct" && !returnable ? ' disabled title="No return series: this book never declared what was paid in or taken out"' : ""}>${label}</button>`).join("")}</div>`
      + `<div class="pnl-range">${["1W", "1M", "3M", "ALL"].map((key) =>
          `<button type="button" data-pnl-range="${key}" aria-pressed="${range === key}">${key}</button>`).join("")}</div></div>`;
    // The figure for the period the chart is showing, which is what the range buttons change.
    // The callout is not pinned to the last point any more, so without this the panel would
    // state no number at all until the pointer entered it -- and the all-time total would be
    // the wrong one to state beside a chart filtered to a week.
    // The return since the chart began, linked across every flow. This used to be a window's
    // NAV change, which is a return only where nothing was paid in or taken out -- on a book
    // that took $735,663 in a day the NAV's +47% said nothing whatever about a $1,236 profit.
    // The time-weighted figure is honest either way, and it is the same one each point states
    // as the pointer moves along the curve.
    const total = asBigInt(performance.total_return_bps);
    const change = total === null
      ? null
      : `${total < 0n ? "−" : "+"}${(Number(total < 0n ? -total : total) / 100).toFixed(2)}%`;
    const opening = asBigInt(points[0]?.pnl_usd_micros);
    const closing = asBigInt(points.at(-1)?.pnl_usd_micros);
    const moved = opening === null || closing === null ? null : closing - opening;
    const period = moved === null
      ? performanceMoney(amount)
      : `${moved < 0n ? "−" : "+"}${performanceMoney((moved < 0n ? -moved : moved).toString())}`;
    // The return across the visible window, not the difference of two cumulative ones: returns
    // compound, so a window's return is the ratio of the two factors rather than their gap.
    const openingReturn = Number(points[0]?.return_bps ?? 0);
    const closingReturn = Number(points.at(-1)?.return_bps ?? NaN);
    const movedBps = Number.isFinite(closingReturn) && Number.isFinite(openingReturn) && openingReturn > -10000
      ? Math.round(((10000 + closingReturn) / (10000 + openingReturn) - 1) * 10000)
      : null;
    const periodChange = movedBps === null
      ? change
      : `${movedBps < 0 ? "−" : "+"}${Math.abs(movedBps / 100).toFixed(2)}%`;
    const span = points.length > 1
      ? `${String(points[0].as_of).slice(0, 10)} to ${String(points.at(-1).as_of).slice(0, 10)}`
      : String(points[0].as_of).slice(0, 10);
    return pnlHeader(snapshot, performance, controls, detail)
      + `<div class="pnl-period"><span class="value${moved === null ? "" : moved < 0n ? " portfolio-negative" : " portfolio-positive"}">${esc(period)}</span>`
      + `<small class="cell-sub">${esc(periodChange ? `${periodChange} · ${span}` : span)}</small></div>`
      + root.FenoriumCharts.portfolioArea(points, {unit: returnable ? unit : "usd",
          expect: performance.declared ? null : "pnl"});
  }

  function portfolioScreen(snapshot, universe, tab = "summary", options = {}) {
    if (tab === "transactions") return transactionScreen(options.transferHistory, {...options, ...options.transferOptions});
    if (tab === "performance") return performanceScreen(snapshot, options);
    if (!snapshot) return "";
    const asOf = snapshot.provenance?.as_of;
    if (tab === "risk") return analyticsScreen(snapshot, universe);
    const rows = P.portfolioRows(snapshot.positions || [], universe || [], asOf);
    if (tab === "exposure") return portfolioExposureScreen(rows, options, asOf);
    if (tab === "holdings") {
      const result = P.queryHoldings(rows, {...options, asOf});
      return portfolioKpis(snapshot, rows, tab, options) + `<section class="panel">${portfolioControls(rows, options)}${portfolioTable(result.rows)}<div class="portfolio-pagination"><span>Showing ${result.start}–${result.end} of ${result.total} assets</span><button type="button" data-holdings-nav="prev" data-holdings-page="${result.page - 1}"${result.page <= 1 ? " disabled" : ""}>Previous</button><span>Page ${result.page} of ${result.pages}</span><button type="button" data-holdings-nav="next" data-holdings-page="${result.page + 1}"${result.page >= result.pages ? " disabled" : ""}>Next</button><label>Rows per page <select data-holdings-control="pageSize">${[10,25,50].map(n => `<option value="${n}"${result.pageSize === n ? " selected" : ""}>${n}</option>`).join("")}</select></label></div></section>`;
    }
    return `<div class="portfolio-summary">${portfolioKpis(snapshot, rows, tab, options)}<div class="portfolio-grid portfolio-summary-grid"><section class="panel"><header><h2>Portfolio Exposure (by Asset Type)</h2></header>${root.FenoriumCharts.portfolioDonut(P.portfolioExposure(rows, "asset_type", asOf), {summary: true})}<div class="portfolio-panel-link">${portfolioLink("exposure", "View exposure")}</div></section><section class="panel"><header><h2>Maturity Profile</h2></header>${root.FenoriumCharts.portfolioMaturity(P.portfolioExposure(rows, "maturity", asOf), {summary: true})}<div class="portfolio-panel-link">${portfolioLink("exposure", "View maturity", "maturity")}</div></section><section class="panel"><header><h2>Largest Holdings</h2></header>${portfolioTable(P.queryHoldings(rows, {sort:"value-desc", pageSize:5}).rows, true)}<div class="portfolio-panel-link">${portfolioLink("holdings", "View all holdings")}</div></section><section class="panel portfolio-pnl">${portfolioPnl(snapshot, performanceRecord(snapshot, options), options)}<div class="portfolio-panel-link">${portfolioLink("performance", "View performance")}</div></section></div></div>`;
  }
  function renderPortfolioScreen(snapshot, universe, tab, options = {}) {
    $("portfolio-header")?.closest?.('.view')?.classList.toggle("portfolio-summary-active", tab === "summary");
    const title = tab[0].toUpperCase() + tab.slice(1);
    const descriptions = {summary:"Valuation, yield and allocation of discovered assets.", holdings:"Explore the assets discovered in the registered-asset scan.", exposure:"Understand the distribution of priced discovered assets.", performance:"Reconciled portfolio history, external flows and declared demo returns.", risk:"Expected Shortfall, volatility and drawdown of the valued holdings, and the daily series they come from.", transactions:"Transfers require enabled transaction-history discovery."};
    const tabs = $("portfolio-tabs");
    if (tabs) tabs.innerHTML = PORTFOLIO_TABS.map(key => `<button type="button" data-portfolio-tab="${key}" aria-current="${tab === key ? "page" : "false"}">${key[0].toUpperCase()+key.slice(1)}</button>`).join("");
    const header = $("portfolio-header");
    const timestamp = snapshot?.provenance?.as_of;
    const parsed = timestamp ? new Date(timestamp) : null;
    const updated = parsed && Number.isFinite(parsed.getTime()) ? parsed.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC") : "UNKNOWN · No snapshot";
    if (header) header.innerHTML = `<div class="page-title"><div><h1>${tab === "summary" ? "Portfolio" : `Portfolio ${title}`}</h1><p>${tab === "summary" ? "Your on-chain fixed-income portfolio." : descriptions[tab]}</p></div><div class="portfolio-header-tools">${tab === "holdings" && snapshot ? '<button type="button" class="ghost" id="download-holdings">Download CSV</button>' : ""}${tab === "summary" ? `<span class="portfolio-updated">Last updated<span>${esc(updated)}</span></span>` : `<span>Last updated ${esc(updated)}</span>`} <button type="button" class="ghost" id="refresh-portfolio" title="${options.loading ? "Refreshing…" : "Refresh"}" aria-label="${options.loading ? "Refreshing…" : "Refresh"}"${options.loading ? " disabled" : ""}><svg class="inline-icon" width="16" height="16" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path d="M20 7v5h-5 M4 17v-5h5 M5 8a8 8 0 0 1 13-3l2 3 M4 16l2 3a8 8 0 0 0 13-3"></path></svg></button></div></div>`;
    // The scope sentence said what the Valuation Coverage card already says on every screen
    // ("N / M assets priced · discovered assets only"), so it cost a line and told nobody
    // anything new. A refresh that failed or is in flight is news, and still gets its line.
    const refreshNotice = options.error
      ? `Refresh failed: ${esc(options.error.code)} · ${esc(options.error.message)}. Showing the previous snapshot at the timestamp above.`
      : options.loading ? "Refresh in progress; showing the previous snapshot." : "";
    if (header && snapshot && refreshNotice) header.innerHTML += `<p class="portfolio-scope">${refreshNotice}</p>`;
    const provenance = $("snapshot-provenance");
    if (provenance && snapshot) provenance.innerHTML = meta("Data mode", snapshot.provenance?.label || "UNKNOWN") + meta("Discovery scope", snapshot.scope || "REGISTERED_ASSETS_ONLY") + meta("Source", snapshot.provenance?.source || "UNKNOWN");
    const deferred = ["performance", "transactions"].includes(tab);
    const missing = $("portfolio-deferred");
    if (missing) { missing.hidden = !deferred; missing.innerHTML = deferred ? portfolioScreen(snapshot, universe, tab, options) : ""; }
    const screen = $("portfolio-screen");
    if (screen) screen.innerHTML = deferred ? "" : portfolioScreen(snapshot, universe, tab, options);
  }
  function holdingsCsv(rows) {
    const quote = value => `"${String(value).replace(/^[=+@\-]/, "'$&").replaceAll('"', '""')}"`;
    return [["Identity", "Asset", "Type", "Chain", "Amount", "Value (USD)", "Yield (APY)", "Yield basis", "Maturity", "Coverage", "Status"], ...rows.map(r => [r.identity, r.name || r.symbol || r.id, r.asset_type || "UNKNOWN", r.chain || "UNKNOWN", tokenQuantity(r.raw_units,r.decimals).replace(NOT_COMPUTABLE,"UNKNOWN"), portfolioMoney(r.value_usd_micros), portfolioPercent(r.yield_bps), r.yield_bps == null ? "UNKNOWN" : rateKindNote(r), ["N/A", "UNKNOWN"].includes(r.maturity_bucket) ? r.maturity_bucket : r.maturity || (r.days_to_maturity == null ? "UNKNOWN" : `${r.days_to_maturity} days`), r.value_usd_micros == null ? "UNKNOWN" : "Priced evidence", r.status])].map(row => row.map(quote).join(",")).join("\r\n");
  }

  root.FenoriumUI = {
    setDeploymentState,
    executionMode,
    portfolioScreen, renderPortfolioScreen, analyticsScreen, portfolioUnavailable, holdingsCsv, transactionsCsv, transferDisplayCells, transactionScreen, collapseSelfTransfers, valuedTransferRows, allTransferRows, portfolioLabel,
    $,
    esc,
    stateLabel,
    stateText,
    explain,
    usd,
    pct,
    verdict,
    chip,
    scoreLabel,
    scoreChip,
    identity,
    availability,
    sourceContext,
    costFigureContext,
    reasonBasis,
    explainReason,
    typeIndicator,
    meta,
    renderMetrics,
    renderAllocation,
    renderDeployable,
    renderChains,
    renderCoverage,
    renderFixedIncome,
    freshness,
    inspector,
    asset,
    instrumentAnalyticsId,
    demoPurchaseCard,
    renderRisk,
    renderMaturity,
    renderEvidence,
    renderExecution,
    renderCompare,
    renderAssistant,
    compareFields,
    comparisonValue,
    numericMetric,
    FRESHNESS_STATUSES,
    NOT_COMPUTABLE,
  };
}(typeof globalThis !== "undefined" ? globalThis : this));
