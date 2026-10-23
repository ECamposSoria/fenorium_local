/* Fenorium portfolio kernel: deterministic, read-only, integer-valued browser contracts. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.FenoriumPortfolio = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function clockNow() { return Date.now(); }

  // The mGT/mPT pair's declared maturity (DEMO_MATURITY in scripts/build_universe.py); catalog
  // instruments take theirs from the static catalog file.
  const DEMO_PAIR_MATURITY = "2030-12-31";

  function maturityDateMs(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const ms = Date.parse(`${value}T00:00:00+00:00`);
    return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === value ? ms : null;
  }

  // Midnight venues may publish a calendar date without an hour and stay live through that UTC
  // day (fixed_income_terms in engine/registry/instruments.py); other date-only rows mature at
  // its start.
  const END_OF_DAY_MATURITY_VENUES = new Set(["Tenor", "Morpho Midnight"]);

  function rowMatured(row, nowMs = clockNow()) {
    if (Number.isInteger(row?.maturity_timestamp) && row.maturity_timestamp >= 0) return row.maturity_timestamp * 1000 <= nowMs;
    const ms = maturityDateMs(row?.maturity);
    if (ms === null) return false;
    return ms + (END_OF_DAY_MATURITY_VENUES.has(row?.venue) ? 86399000 : 0) <= nowMs;
  }

  const STATUS = Object.freeze({
    SUPPORTED: "SUPPORTED", PARTIALLY_SUPPORTED: "PARTIALLY_SUPPORTED",
    CONTEXT_ONLY: "CONTEXT_ONLY", UNRESOLVED: "UNRESOLVED",
  });
  const PURCHASE_STAGES = Object.freeze({
    APPROVAL: "APPROVAL",
    APPROVAL_RECEIPT: "APPROVAL_RECEIPT",
    APPROVAL_INDETERMINATE: "APPROVAL_INDETERMINATE",
    APPROVAL_COMPLETE: "APPROVAL_COMPLETE",
    APPROVAL_REFRESH_FAILED: "APPROVAL_REFRESH_FAILED",
    PURCHASE: "PURCHASE",
    PURCHASE_RECEIPT: "PURCHASE_RECEIPT",
    PURCHASE_INDETERMINATE: "PURCHASE_INDETERMINATE",
    FAUCET: "FAUCET",
    FAUCET_RECEIPT: "FAUCET_RECEIPT",
    FAUCET_INDETERMINATE: "FAUCET_INDETERMINATE",
    FAUCET_COMPLETE: "FAUCET_COMPLETE",
    FAUCET_REFRESH_FAILED: "FAUCET_REFRESH_FAILED",
    COMPLETE: "COMPLETE",
    COMPLETE_REFRESH_FAILED: "COMPLETE_REFRESH_FAILED",
    QUOTE_REVIEW_REQUIRED: "QUOTE_REVIEW_REQUIRED",
    QUOTE_CHANGED: "QUOTE_CHANGED",
    SALE_APPROVAL: "SALE_APPROVAL",
    SALE_APPROVAL_RECEIPT: "SALE_APPROVAL_RECEIPT",
    SALE_APPROVAL_INDETERMINATE: "SALE_APPROVAL_INDETERMINATE",
    SALE_APPROVAL_COMPLETE: "SALE_APPROVAL_COMPLETE",
    SALE_APPROVAL_REFRESH_FAILED: "SALE_APPROVAL_REFRESH_FAILED",
    SALE: "SALE",
    SALE_RECEIPT: "SALE_RECEIPT",
    SALE_INDETERMINATE: "SALE_INDETERMINATE",
    SALE_COMPLETE: "SALE_COMPLETE",
    SALE_COMPLETE_REFRESH_FAILED: "SALE_COMPLETE_REFRESH_FAILED",
    SALE_QUOTE_REVIEW_REQUIRED: "SALE_QUOTE_REVIEW_REQUIRED",
    SALE_QUOTE_CHANGED: "SALE_QUOTE_CHANGED",
  });
  const REASONS = Object.freeze({
    MISSING_PRICE: "MISSING_PRICE", UNRESOLVED_POSITION: "UNRESOLVED_POSITION",
    VALUE_COVERAGE_NOT_COMPUTABLE: "VALUE_COVERAGE_NOT_COMPUTABLE",
    INSUFFICIENT_HISTORY: "INSUFFICIENT_HISTORY", INSUFFICIENT_COVERAGE: "INSUFFICIENT_COVERAGE",
    INCOMPATIBLE_MARKING_FREQUENCY: "INCOMPATIBLE_MARKING_FREQUENCY",
    BACKEND_METRIC_REQUIRED: "BACKEND_METRIC_REQUIRED", MISSING_COST: "MISSING_COST",
    EXPLICIT_SOURCE_REQUIRED: "EXPLICIT_SOURCE_REQUIRED",
    VARIABLE_ASSET_OPT_IN_REQUIRED: "VARIABLE_ASSET_OPT_IN_REQUIRED",
    VARIABLE_ASSET_LIMIT_EXCEEDED: "VARIABLE_ASSET_LIMIT_EXCEEDED",
    NO_BID: "NO_BID", NO_QUOTE: "NO_QUOTE", MISSING_EXECUTION_QUOTE: "MISSING_EXECUTION_QUOTE",
    LEGAL_REVIEW_REQUIRED: "LEGAL_REVIEW_REQUIRED", MANDATE_LIMIT: "MANDATE_LIMIT",
    PROTOCOL_CONCENTRATION_FAIL: "PROTOCOL_CONCENTRATION_FAIL", MANDATE_UNKNOWN: "MANDATE_UNKNOWN",
    VARIABLE_ASSET_LIMIT_REQUIRED: "VARIABLE_ASSET_LIMIT_REQUIRED",
    IDLE_STABLECOIN_SOURCE_REQUIRED: "IDLE_STABLECOIN_SOURCE_REQUIRED",
    SOURCE_CAPABILITY_REQUIRED: "SOURCE_CAPABILITY_REQUIRED", SOURCE_LIMIT_EXCEEDED: "SOURCE_LIMIT_EXCEEDED",
    MISSING_LIQUIDITY: "MISSING_LIQUIDITY", INSUFFICIENT_LIQUIDITY: "INSUFFICIENT_LIQUIDITY",
  });
  // Every chain Fenorium knows, from engine/registry/chains.json. Generated, never edited here.
  // BEGIN GENERATED CHAIN REGISTRY -- `python -m engine.registry.web` writes this; do not edit.
  const REGISTRY_CHAINS = Object.freeze([
    {"chain_id":1,"key":"ethereum","name":"Ethereum","short":"ETH","testnet":false,"block_seconds":12,"history_index":true,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://ethereum-rpc.publicnode.com","explorer":"https://etherscan.io","usdc":{"symbol":"USDC","address":"0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48","decimals":6},"offer_native":true,"roles":["buy","collect","pay","portfolio"]},
    {"chain_id":10,"key":"optimism","name":"OP Mainnet","short":"OP","testnet":false,"block_seconds":2,"history_index":true,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://optimism-rpc.publicnode.com","explorer":"https://optimistic.etherscan.io","usdc":{"symbol":"USDC","address":"0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85","decimals":6},"offer_native":true,"roles":["buy","collect","pay","portfolio"]},
    {"chain_id":56,"key":"bsc","name":"BNB Chain","short":"BSC","testnet":false,"block_seconds":0.45,"history_index":false,"native":{"symbol":"BNB","decimals":18},"rpc_url":"https://bsc-rpc.publicnode.com","explorer":"https://bscscan.com","usdc":{"symbol":"USDC","address":"0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d","decimals":18},"offer_native":true,"roles":["buy","collect","pay","portfolio"]},
    {"chain_id":100,"key":"gnosis","name":"Gnosis","short":"GNO","testnet":false,"block_seconds":5,"history_index":false,"native":{"symbol":"xDAI","decimals":18},"rpc_url":"https://rpc.gnosischain.com","explorer":"https://gnosis.blockscout.com","usdc":{"symbol":"USDC","address":"0xDDAfbb505ad214D7b80b1f830fcCc89B60fb7A83","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":130,"key":"unichain","name":"Unichain","short":"UNI","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://mainnet.unichain.org","explorer":"https://unichain.blockscout.com","usdc":{"symbol":"USDC","address":"0x078D782b760474a361dDA0AF3839290b0EF57AD6","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":137,"key":"polygon","name":"Polygon","short":"POL","testnet":false,"block_seconds":1.5,"history_index":false,"native":{"symbol":"POL","decimals":18},"rpc_url":"https://polygon-bor-rpc.publicnode.com","explorer":"https://polygonscan.com","usdc":{"symbol":"USDC","address":"0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":143,"key":"monad","name":"Monad","short":"MON","testnet":false,"block_seconds":0.3,"history_index":false,"native":{"symbol":"MON","decimals":18},"rpc_url":"https://rpc.monad.xyz","explorer":"https://monadscan.com","usdc":{"symbol":"USDC","address":"0x754704Bc059F8C67012fEd69BC8A327a5aafb603","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":146,"key":"sonic","name":"Sonic","short":"S","testnet":false,"block_seconds":1.25,"history_index":false,"native":{"symbol":"S","decimals":18},"rpc_url":"https://rpc.soniclabs.com","explorer":"https://explorer.soniclabs.com","usdc":{"symbol":"USDC","address":"0x29219dd400f2Bf60E5a23d13Be72B486D4038894","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":196,"key":"xlayer","name":"XLayer","short":"XLAYER","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"OKB","decimals":18},"rpc_url":"https://rpc.xlayer.tech","explorer":"https://www.oklink.com/xlayer","usdc":{"symbol":"USDC","address":"0x74b7F16337b8972027F6196A17a631aC6dE26d22","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":324,"key":"zksync","name":"zkSync","short":"ZKSYNC","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://mainnet.era.zksync.io","explorer":"https://explorer.zksync.io","usdc":{"symbol":"USDC","address":"0x1d17CBcF0D6D143135aE902365D2E5e2A16538D4","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":480,"key":"worldchain","name":"World Chain","short":"WLD","testnet":false,"block_seconds":2,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://worldchain-mainnet.g.alchemy.com/public","explorer":"https://worldscan.org","usdc":{"symbol":"USDC","address":"0x79A02482A880bCE3F13e09Da970dC34db4CD24d1","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":999,"key":"hyperevm","name":"HyperEVM","short":"HYPE","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"HYPE","decimals":18},"rpc_url":"https://rpc.hyperliquid.xyz/evm","explorer":"https://hyperevmscan.io","usdc":{"symbol":"USDC","address":"0xb88339CB7199b77E23DB6E890353E22632Ba630f","decimals":6},"offer_native":true,"roles":["buy","collect","pay"]},
    {"chain_id":1672,"key":"pharos","name":"Pharos","short":"PROS","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"PROS","decimals":18},"rpc_url":"https://rpc.pharos.xyz","explorer":"https://www.pharosscan.xyz","usdc":{"symbol":"USDC","address":"0xC879C018dB60520F4355C26eD1a6D572cdAC1815","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":2818,"key":"morph","name":"Morph","short":"MORPH","testnet":false,"block_seconds":1.8,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://rpc.morph.network","explorer":"https://explorer.morph.network","usdc":{"symbol":"USDC","address":"0xCfb1186F4e93D60E60a8bDd997427D1F33bc372B","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":4326,"key":"megaeth","name":"MegaETH","short":"MEGA","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://mainnet.megaeth.com/rpc","explorer":"https://mega.etherscan.io","usdc":{"symbol":"USDm","address":"0xFAfDdbb3FC7688494971a79cc65DCa3EF82079E7","decimals":18},"offer_native":true,"roles":["buy","collect","pay"]},
    {"chain_id":5000,"key":"mantle","name":"Mantle","short":"MNT","testnet":false,"block_seconds":2,"history_index":false,"native":{"symbol":"MNT","decimals":18},"rpc_url":"https://rpc.mantle.xyz","explorer":"https://explorer.mantle.xyz","usdc":{"symbol":"USDC","address":"0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":5042,"key":"arc","name":"Arc","short":"ARC","testnet":false,"block_seconds":0.5,"history_index":false,"native":{"symbol":"USDC","decimals":18},"rpc_url":"https://rpc.drpc.mainnet.arc.io","explorer":"https://explorer.arc.io","usdc":{"symbol":"USDC","address":"0x3600000000000000000000000000000000000000","decimals":6},"offer_native":false,"roles":["buy","collect","pay"]},
    {"chain_id":8453,"key":"base","name":"Base","short":"BASE","testnet":false,"block_seconds":2,"history_index":true,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://base-rpc.publicnode.com","explorer":"https://basescan.org","usdc":{"symbol":"USDC","address":"0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913","decimals":6},"offer_native":true,"roles":["buy","collect","pay","portfolio"]},
    {"chain_id":42161,"key":"arbitrum","name":"Arbitrum","short":"ARB","testnet":false,"block_seconds":0.25,"history_index":true,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://arbitrum-one-rpc.publicnode.com","explorer":"https://arbiscan.io","usdc":{"symbol":"USDC","address":"0xaf88d065e77c8cC2239327C5EDb3A432268e5831","decimals":6},"offer_native":true,"roles":["buy","collect","pay","portfolio"]},
    {"chain_id":42220,"key":"celo","name":"Celo","short":"CELO","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"CELO","decimals":18},"rpc_url":"https://forno.celo.org","explorer":"https://celoscan.io","usdc":{"symbol":"USDC","address":"0xcebA9300f2b948710d2653dD7B07f33A8B32118C","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":43114,"key":"avalanche","name":"Avalanche","short":"AVAX","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"AVAX","decimals":18},"rpc_url":"https://api.avax.network/ext/bc/C/rpc","explorer":"https://snowtrace.io","usdc":{"symbol":"USDC","address":"0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":57073,"key":"ink","name":"Ink","short":"INK","testnet":false,"block_seconds":1,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://rpc-gel.inkonchain.com","explorer":"https://explorer.inkonchain.com","usdc":{"symbol":"USDC","address":"0x2D270e6886d130D724215A266106e6832161EAEd","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":59144,"key":"linea","name":"Linea","short":"LINEA","testnet":false,"block_seconds":2,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://rpc.linea.build","explorer":"https://lineascan.build","usdc":{"symbol":"USDC","address":"0x176211869cA2b568f2A7D4EE941E073a821EE1ff","decimals":6},"offer_native":true,"roles":["pay"]},
    {"chain_id":84532,"key":"base-sepolia","name":"Base Sepolia","short":"BASE-SEPOLIA","testnet":true,"block_seconds":2,"history_index":true,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://sepolia.base.org","explorer":"https://sepolia.basescan.org","usdc":null,"offer_native":true,"roles":["demo","portfolio"]},
    {"chain_id":534352,"key":"scroll","name":"Scroll","short":"SCROLL","testnet":false,"block_seconds":3,"history_index":false,"native":{"symbol":"ETH","decimals":18},"rpc_url":"https://rpc.scroll.io","explorer":"https://scrollscan.com","usdc":{"symbol":"USDC","address":"0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4","decimals":6},"offer_native":true,"roles":["pay"]},
  ].map((chain) => Object.freeze(chain)));
  const FEE_RECIPIENT = "0xC84ECe4538Ef02521383b0DdfF48F4Bd1bE171aE";
  // END GENERATED CHAIN REGISTRY
  const chainRole = (role) => REGISTRY_CHAINS.filter((chain) => chain.roles.includes(role));
  // The chains a wallet is read on. `block_seconds` is each chain's measured block time, and it is
  // what turns a scan depth from a block count into a length of history. 900,000 blocks was 125
  // days of Ethereum, 21 of Base and under 3 of Arbitrum -- one number that meant a different
  // thing on every chain, and on the fast ones far too little to reach the day a position was opened.
  const CHAINS = Object.freeze(chainRole("portfolio"));
  // How far back a full scan reaches, and why it stopped there. Two bounds, because they pull
  // against each other: the history worth having is measured in years, and the cost of reading
  // it is measured in windows -- and a provider caps a window at a few thousand blocks, so a
  // chain producing a block every quarter second cannot be read back a year at any price.
  // Whichever bound bites is named, so a scan that fell short says so rather than implying it
  // looked at everything.
  const SCAN_SPAN_SECONDS = 730 * 86400;
  const SCAN_WINDOW_BUDGET = 1000;
  function transferScanDepth(chainId, options = {}) {
    const windowSize = Number.isInteger(options.windowSize) && options.windowSize > 0 ? options.windowSize : 9000;
    const spanSeconds = Number.isFinite(options.spanSeconds) && options.spanSeconds > 0 ? options.spanSeconds : SCAN_SPAN_SECONDS;
    const budget = Number.isInteger(options.budget) && options.budget > 0 ? options.budget : SCAN_WINDOW_BUDGET;
    const chain = CHAINS.find((row) => row.chain_id === Number(chainId));
    const affordable = budget * windowSize;
    // An unknown chain has no block time to convert with, so it gets the budget and nothing is
    // claimed about how long that is.
    if (!chain?.block_seconds) return {blocks: affordable, bound: "budget", days: null, block_seconds: null};
    const wanted = Math.ceil(spanSeconds / chain.block_seconds);
    const blocks = Math.min(wanted, affordable);
    return {blocks, bound: blocks < wanted ? "budget" : "span",
      days: Math.floor(blocks * chain.block_seconds / 86400), block_seconds: chain.block_seconds};
  }
  const MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11";
  const MULTICALL_CHUNK_SIZE = 75;
  const AGGREGATE3_SELECTOR = "0x82ad56cb";
  const BALANCE_OF_SELECTOR = "0x70a08231";
  const TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
  // The client holds each demo instrument's declared rate itself and refuses any artifact that
  // disagrees, so a tampered universe cannot revalue a position by rewriting its own schedule.
  // A demo instrument absent from this map has no historical price here, by design.
  const DEFAULT_TRANSFER_RATES = Object.freeze({
    "0xe72a18adf4fdb9df991ad4218b7199d324214f54": 525n,
    "0x5c8ea45363e98e8647ee3759ac888e12817e86a3": 475n,
    "0x3e3cb7d30181e1df2db97fce867adcc5a0c08f1e": 440n,
    "0xfd9c6631d34d23df4a3e27f9c760cea4cdefc906": 310n,
  });
  // The demo's unit of account has no accrual schedule because it does not accrete: it is one
  // dollar at every moment, which is the whole reason everything else can be priced in it. The
  // price is held here rather than read from the artifact for the same reason the rates are --
  // a tampered universe must not be able to revalue a position.
  const DEFAULT_UNIT_PRICES = Object.freeze({
    "0xfecb2eff5603b44dad440561ca3cd889f29239ac": 100000000n,
  });
  let DECLARED_TRANSFER_RATES = DEFAULT_TRANSFER_RATES;
  let DECLARED_UNIT_PRICES = DEFAULT_UNIT_PRICES;
  let DECLARED_TRANSFER_ASSETS = new Set([
    ...Object.keys(DECLARED_TRANSFER_RATES),
    ...Object.keys(DECLARED_UNIT_PRICES),
  ]);
  function registerDemoDeclarations(declared) {
    const rates = {}, units = {};
    const valid = declared && typeof declared === "object" && !Array.isArray(declared)
      && Object.keys(declared).length > 0 && Object.entries(declared).every(([token, entry]) => {
        if (!/^0x[0-9a-f]{40}$/.test(token) || !entry || typeof entry !== "object" || Array.isArray(entry)) return false;
        const rate = Object.hasOwn(entry, "rate_bps"), unit = Object.hasOwn(entry, "unit_price_usd_fixed");
        if (rate === unit) return false;
        if (rate) {
          if (!Number.isInteger(entry.rate_bps) || entry.rate_bps < 0 || entry.rate_bps >= 10000) return false;
          rates[token] = BigInt(entry.rate_bps);
        } else {
          if (typeof entry.unit_price_usd_fixed !== "string" || !/^\d+$/.test(entry.unit_price_usd_fixed)) return false;
          units[token] = BigInt(entry.unit_price_usd_fixed);
        }
        return true;
      });
    DECLARED_TRANSFER_RATES = valid ? Object.freeze(rates) : DEFAULT_TRANSFER_RATES;
    DECLARED_UNIT_PRICES = valid ? Object.freeze(units) : DEFAULT_UNIT_PRICES;
    DECLARED_TRANSFER_ASSETS = new Set([...Object.keys(DECLARED_TRANSFER_RATES), ...Object.keys(DECLARED_UNIT_PRICES)]);
  }
  const DECLARED_PRICE_SCALE = 100000000n;
  const declaredSchedules = new Map();
  function registerDeclaredSchedules(instruments) {
    declaredSchedules.clear();
    for (const item of Array.isArray(instruments) ? instruments : []) {
      const token = address(item?.instrument);
      const base = nonNegativeBigInt(item?.base_price_usd_fixed), rate = nonNegativeBigInt(item?.rate_bps);
      const start = isoSeconds(item?.accrual_start), end = isoSeconds(item?.accrual_end);
      if (!token || base === null || rate === null || rate >= 10000n || start === null || end === null || end <= start) continue;
      declaredSchedules.set(token, {base, rate, start, end});
    }
  }
  const SECONDS_PER_YEAR = 31536000n;
  const FIXED_MATH_SCALE = 10n ** 60n;
  const REGISTERED_CHAIN_IDS = new Set(CHAINS.map((chain) => chain.chain_id));
  const MARKET_CATEGORY_LABELS = Object.freeze({
    native: "Fixed maturity",
    rwa: "Tokenized fund",
    vault: "Vault",
  });
  // Portfolio asset classes, kept apart from the market's instrument categories: these name
  // exposures in a book, not rows in the universe, and marketCategory() must not accept them.
  // A chart names a class of holdings and a table names one holding, so each has its own word.
  const PORTFOLIO_CLASS_LABELS = Object.freeze({
    defi_fixed_income: "DeFi fixed income",
    stablecoin: "Stablecoins",
    tokenized_treasuries: "Tokenized treasuries",
  });
  const PORTFOLIO_CLASS_LABELS_ONE = Object.freeze({
    defi_fixed_income: "DeFi fixed income",
    stablecoin: "Stablecoin",
    tokenized_treasuries: "Tokenized treasury",
  });
  const MARKET_DEFAULT_COLUMNS = Object.freeze({
    mixed: Object.freeze([
      "verdict", "instrument", "maturity", "headline", "net", "execution", "depth", "tier",
    ]),
    native: Object.freeze([
      "verdict", "instrument", "maturity", "headline", "net", "execution", "depth", "tier",
    ]),
    rwa: Object.freeze([
      "instrument", "headline", "net", "execution", "tvl", "rate_kind", "permission",
    ]),
    vault: Object.freeze([
      "instrument", "headline", "net", "execution", "depth", "tvl", "rate_kind",
    ]),
  });

  function marketCategory(row) {
    const category = String(row?.category || "").toLowerCase();
    return Object.hasOwn(MARKET_CATEGORY_LABELS, category) ? category : "unknown";
  }

  function marketFieldValue(row, key) {
    const values = {
      verdict: row?.verdict,
      instrument: row?.name,
      venue: row?.venue,
      chain: row?.chain,
      maturity: row?.maturity,
      headline: row?.rate_bps,
      net: row?.rating?.computable === true ? row.rating.net_bps : null,
      execution: row?.rating?.computable === true ? row.rating.execution_bps : null,
      depth: row?.depth_usd,
      tier: row?.tier,
      category: row?.category,
      rate_kind: row?.rate_kind,
      tvl: row?.tvl_usd,
      days: row?.days_to_maturity,
      rejection: row?.rejection || (String(row?.verdict || "").toUpperCase() === "NO BID" ? "size" : "none"),
      permission: row?.gate?.status,
      address: row?.address,
      chain_id: row?.chain_id,
      execution_mode: row?.is_demo === true ? "Base Sepolia demo purchase" : "Mainnet execution disabled",
    };
    return values[key];
  }

  function marketFieldApplies(row, key) {
    const category = marketCategory(row);
    if (["maturity", "days", "tier", "verdict"].includes(key)) {
      // A stablecoin is the unit of account, not an instrument to score or to hold to maturity.
      if (String(row?.category || "").toLowerCase() === "stablecoin") return false;
      return category === "unknown" || category === "native";
    }
    if (key === "tvl") return category === "unknown" || category === "rwa" || category === "vault";
    if (key === "depth" && category === "rwa") return row?.depth_usd != null;
    return true;
  }

  function marketFieldState(row, key) {
    if (!marketFieldApplies(row, key)) {
      return { status: "N/A", reason: "NOT_APPLICABLE", value: null };
    }
    const value = marketFieldValue(row, key);
    if (value === null || value === undefined || value === "") {
      return { status: "UNKNOWN", reason: "NOT_MEASURED", value: null };
    }
    return { status: "MEASURED", reason: null, value };
  }

  function marketVerdict(row) {
    if (!marketFieldApplies(row, "verdict")) return null;
    return String(row?.verdict || "UNRATED").toUpperCase();
  }

  function asBigInt(value) {
    if (typeof value === "bigint") return value;
    if (value === null || value === undefined || value === "") return null;
    try { return BigInt(String(value)); } catch (_) { return null; }
  }
  function nonNegativeBigInt(value) {
    const parsed = asBigInt(value);
    return parsed !== null && parsed >= 0n ? parsed : null;
  }
  function usdMicrosFromRaw(rawUnits, decimals, priceMicros) {
    if (decimals === null || decimals === undefined || decimals === "") return null;
    const raw = nonNegativeBigInt(rawUnits), price = nonNegativeBigInt(priceMicros), places = Number(decimals);
    if (raw === null || price === null || !Number.isInteger(places) || places < 0 || places > 255) return null;
    return (raw * price) / (10n ** BigInt(places));
  }
  function numberString(value) { return value === null || value === undefined ? null : String(value); }
  function metricFailure(reason_code, extra) {
    return Object.assign({ status: "NOT_COMPUTABLE", value: null, unit: null,
      method_version: "portfolio-browser-v1", as_of: null, window: null, frequency: null,
      observations: 0, coverage_bps: 0, reason_code, excluded_positions: [], sources: [],
      assumptions: [], limitations: [] }, extra || {});
  }
  function metricPass(value, options) {
    return Object.assign({ status: "COMPUTED", value: numberString(value), unit: "USD_MICROS",
      method_version: "portfolio-browser-v1", as_of: null, window: null, frequency: null,
      observations: 0, coverage_bps: 10000, reason_code: null, excluded_positions: [],
      sources: [], assumptions: [], limitations: [] }, options || {});
  }

  function classifyPosition(position) {
    const p = position || {}, category = String(p.asset_type || p.category || "unknown").toLowerCase();
    const variable = ["variable_income", "risk_asset", "crypto", "equity"].includes(category);
    const stable = p.is_stablecoin === true || category === "stablecoin" || category === "cash";
    const hasPrice = nonNegativeBigInt(p.price_micros) !== null || nonNegativeBigInt(p.value_usd_micros) !== null;
    let status = p.classification_status || p.status;
    if (!Object.values(STATUS).includes(status)) status = hasPrice ? STATUS.SUPPORTED : STATUS.PARTIALLY_SUPPORTED;
    if (p.unresolved === true || (!p.address && p.position_type !== "native") || category === "unknown") status = STATUS.UNRESOLVED;
    if (!hasPrice && status === STATUS.SUPPORTED) status = STATUS.PARTIALLY_SUPPORTED;
    return { id: p.id || p.address || p.symbol || "unknown-position", category, variable_income: variable,
      stablecoin: stable, status, price_status: hasPrice ? "PRICED" : "UNPRICED",
      identity: p.identity_evidence || (p.address ? "registered universe address" : "explicit native asset"),
      exclusion_reason: status === STATUS.UNRESOLVED ? (p.reason || REASONS.UNRESOLVED_POSITION) : (hasPrice ? null : REASONS.MISSING_PRICE),
      supports: { valuation: hasPrice, risk: p.risk_supported === true,
        fixed_income: p.fixed_income_supported === true || category === "fixed_income", execution: p.execution_supported === true } };
  }
  function valueOf(position) {
    const direct = nonNegativeBigInt(position && position.value_usd_micros);
    return direct !== null ? direct : usdMicrosFromRaw(position && position.raw_units, position && position.decimals, position && position.price_micros);
  }
  function coverageSummary(positions) {
    const rows = Array.isArray(positions) ? positions : [], classified = rows.map(classifyPosition);
    const priced = classified.filter((c, i) => valueOf(rows[i]) !== null);
    const unknown = classified.filter(c => c.status === STATUS.UNRESOLVED || c.price_status === "UNPRICED");
    const valueKnown = rows.reduce((sum, p) => sum + (valueOf(p) || 0n), 0n);
    const supportedValue = rows.reduce((sum, p, i) => sum + (classified[i].status === STATUS.SUPPORTED ? (valueOf(p) || 0n) : 0n), 0n);
    const valueDenominatorKnown = unknown.length === 0 && valueKnown > 0n;
    return { position_count: rows.length, priced_positions: priced.length, unresolved_positions: unknown.length,
      supported_positions: classified.filter(c => c.status === STATUS.SUPPORTED).length,
      position_coverage_bps: rows.length ? Math.floor(priced.length * 10000 / rows.length) : 0,
      value_known_usd_micros: valueKnown.toString(), supported_value_usd_micros: supportedValue.toString(),
      value_coverage_bps: valueDenominatorKnown ? Number(supportedValue * 10000n / valueKnown) : null,
      value_coverage_status: valueDenominatorKnown ? "COMPUTED" : REASONS.VALUE_COVERAGE_NOT_COMPUTABLE,
      unresolved: classified.map((c, i) => ({ id: c.id, reason: c.exclusion_reason, value_usd_micros: numberString(valueOf(rows[i])) }))
        .filter(row => row.reason !== null) };
  }
  function navLowerBound(positions) {
    const rows = Array.isArray(positions) ? positions : [], coverage = coverageSummary(rows);
    const priced = rows.reduce((sum, p) => sum + (valueOf(p) || 0n), 0n);
    const unresolved = rows.filter(p => { const c = classifyPosition(p); return c.status === STATUS.UNRESOLVED || valueOf(p) === null; });
    const authoritative = unresolved.length === 0 && rows.length > 0;
    return { status: authoritative ? "AUTHORITATIVE_NAV" : "PRICED_NAV_LOWER_BOUND", value_usd_micros: priced.toString(),
      lower_bound: !authoritative, currency: "USD", coverage_bps: coverage.value_coverage_bps,
      unresolved_positions: unresolved.map(p => p.id || p.address || p.symbol || "unknown-position"),
      reason: authoritative ? null : REASONS.MISSING_PRICE,
      source_timestamps: rows.map(p => p.source_timestamp || p.as_of).filter(Boolean).sort() };
  }
  function groupedValue(positions, key) {
    const groups = new Map();
    (positions || []).forEach(p => { const c = classifyPosition(p), value = valueOf(p);
      const isUnknown = c.status === STATUS.UNRESOLVED || value === null;
      const label = isUnknown ? "UNKNOWN" : (p[key] || (key === "issuer" ? p.issuer_name : null) || "Unspecified");
      const prior = groups.get(label) || { label, value_usd_micros: 0n, position_count: 0, unknown: isUnknown };
      prior.position_count += 1; if (value !== null) prior.value_usd_micros += value; else prior.unknown = true; groups.set(label, prior); });
    return [...groups.values()].sort((a, b) => a.label.localeCompare(b.label)).map(g => ({ label: g.label,
      value_usd_micros: g.value_usd_micros.toString(), position_count: g.position_count, unknown: g.unknown }));
  }
  function allocation(positions) {
    const rows = groupedValue(positions, "asset_type"), total = rows.reduce((sum, row) => sum + BigInt(row.value_usd_micros), 0n);
    return rows.map(row => Object.assign({}, row, { share_bps: total && !row.unknown ? Number(BigInt(row.value_usd_micros) * 10000n / total) : null }));
  }
  function concentration(positions, groupBy) {
    const rows = groupedValue(positions, groupBy || "issuer"), total = rows.reduce((sum, row) => sum + BigInt(row.value_usd_micros), 0n);
    const sorted = rows.map(row => Object.assign({}, row, { share_bps: total && !row.unknown ? Number(BigInt(row.value_usd_micros) * 10000n / total) : null }))
      .sort((a, b) => (b.share_bps || -1) - (a.share_bps || -1) || a.label.localeCompare(b.label));
    return { group_by: groupBy || "issuer", rows: sorted, top_share_bps: sorted[0] ? sorted[0].share_bps : null,
      unknown_visible: sorted.some(row => row.unknown || row.label === "UNKNOWN") };
  }
  function deployableStablecoinCapital(positions) {
    const rows = (positions || []).filter(p => { const c = classifyPosition(p);
      return c.stablecoin && c.status === STATUS.SUPPORTED && c.supports.valuation && valueOf(p) !== null && p.deployable !== false; });
    const value = rows.reduce((sum, p) => sum + valueOf(p), 0n);
    return { status: rows.length ? "COMPUTABLE" : "NOT COMPUTABLE", value_usd_micros: value.toString(),
      position_ids: rows.map(p => p.id || p.symbol), reason: rows.length ? null : REASONS.MISSING_PRICE,
      assumption: "Stablecoin status and price are evidence fields; no $1 peg is assumed." };
  }
  const COST_FIELDS = Object.freeze(["source_swap", "price_impact", "bridge", "integrator_fee", "fenorium_fee", "destination_venue_cost", "gas", "entry_cost"]);
  function validateCostLedger(ledger) {
    const source = ledger || {}, missing = COST_FIELDS.filter(key => nonNegativeBigInt(source[key]) === null);
    const total = missing.length ? null : COST_FIELDS.reduce((sum, key) => sum + nonNegativeBigInt(source[key]), 0n);
    return { status: missing.length ? "ANALYTICAL_ONLY" : "COMPLETE", complete: missing.length === 0, missing,
      total_usd_micros: total === null ? null : total.toString(),
      fee_usd_micros: missing.length ? null : (nonNegativeBigInt(source.integrator_fee) + nonNegativeBigInt(source.fenorium_fee)).toString(),
      reason_code: missing.length ? REASONS.MISSING_COST : null, fields: COST_FIELDS.slice() };
  }
  function mandateGates(input) {
    const request = input || {}, candidate = request.candidate || {}, mandate = request.mandate || {}, amount = nonNegativeBigInt(request.amount_usd_micros) || 0n, gates = [];
    if (candidate.protocol && mandate.max_protocol_share_bps !== undefined) {
      const knownShare = mandate.current_protocol_share_bps !== undefined && mandate.projected_protocol_share_bps !== undefined;
      const share = Number(mandate.current_protocol_share_bps) + Number(mandate.projected_protocol_share_bps), maxShare = Number(mandate.max_protocol_share_bps);
      gates.push({ code: !knownShare || !Number.isFinite(share) || !Number.isFinite(maxShare) ? REASONS.MANDATE_UNKNOWN : (share > maxShare ? REASONS.PROTOCOL_CONCENTRATION_FAIL : "MANDATE_PASS"), pass: knownShare && Number.isFinite(share) && Number.isFinite(maxShare) && share <= maxShare });
    }
    if (mandate.max_amount_usd_micros !== undefined) { const max = nonNegativeBigInt(mandate.max_amount_usd_micros);
      gates.push({ code: max === null ? REASONS.MANDATE_UNKNOWN : (amount > max ? REASONS.MANDATE_LIMIT : "MANDATE_PASS"), pass: max !== null && amount <= max }); }
    if (mandate.require_legal_review) gates.push({ code: REASONS.LEGAL_REVIEW_REQUIRED, pass: false });
    return { pass: gates.every(g => g.pass), gates };
  }
  function expectedShortfall(input) {
    const metric = input && (input.backend_metric_result || input);
    if (!metric || metric.status !== "COMPUTED") return metricFailure(REASONS.BACKEND_METRIC_REQUIRED, { unit: "PERCENT_BPS" });
    const observations = Number(metric.observations || 0), coverage = Number(metric.coverage_bps || 0);
    if (observations < 250) return metricFailure(REASONS.INSUFFICIENT_HISTORY, { observations, coverage_bps: coverage, unit: "PERCENT_BPS" });
    if (coverage < 9000) return metricFailure(REASONS.INSUFFICIENT_COVERAGE, { observations, coverage_bps: coverage, unit: "PERCENT_BPS" });
    return Object.assign({}, metric, { unit: "PERCENT_BPS", method_version: metric.method_version || "backend-provided" });
  }
  function explicitSourceScenario(input) {
    const request = input || {}, candidate = request.candidate || {}, source = request.source_position, amount = nonNegativeBigInt(request.amount_usd_micros), objective = request.objective || "deploy_idle_stablecoins", reasons = [];
    if (!source) reasons.push(REASONS.EXPLICIT_SOURCE_REQUIRED); if (amount === null || amount === 0n) reasons.push("AMOUNT_REQUIRED");
    if (["NO BID", "NO QUOTE"].includes(String(candidate.verdict || "").toUpperCase())) reasons.push(String(candidate.verdict).toUpperCase().replace(" ", "_"));
    const sourceClass = source ? classifyPosition(source) : null;
    if (sourceClass && (sourceClass.status !== STATUS.SUPPORTED || !sourceClass.supports.valuation)) reasons.push(REASONS.SOURCE_CAPABILITY_REQUIRED);
    if (objective === "deploy_idle_stablecoins" && sourceClass && !sourceClass.stablecoin) reasons.push(REASONS.IDLE_STABLECOIN_SOURCE_REQUIRED);
    const sourceValue = source ? valueOf(source) : null;
    if (source && sourceValue === null) reasons.push(REASONS.MISSING_PRICE);
    else if (amount !== null && sourceValue !== null && amount > sourceValue) reasons.push(REASONS.SOURCE_LIMIT_EXCEEDED);
    if (sourceClass && sourceClass.variable_income) { if (request.variable_income_opt_in !== true) reasons.push(REASONS.VARIABLE_ASSET_OPT_IN_REQUIRED);
      const max = nonNegativeBigInt(request.variable_income_max_amount_usd_micros); if (max === null) reasons.push(REASONS.VARIABLE_ASSET_LIMIT_REQUIRED); else if (amount !== null && amount > max) reasons.push(REASONS.VARIABLE_ASSET_LIMIT_EXCEEDED); }
    const liquidityFloor = nonNegativeBigInt(request.liquidity_floor_usd_micros), depth = nonNegativeBigInt(candidate.depth_usd_micros);
    const requiredDepth = amount === null ? liquidityFloor : (liquidityFloor === null || amount > liquidityFloor ? amount : liquidityFloor);
    if (depth === null) reasons.push(REASONS.MISSING_LIQUIDITY);
    else if (requiredDepth !== null && depth < requiredDepth) reasons.push(REASONS.INSUFFICIENT_LIQUIDITY);
    const cost = validateCostLedger(request.cost_ledger), quoteMissing = candidate.execution_quote !== true && candidate.quote_status !== "QUOTED";
    if (quoteMissing) reasons.push(REASONS.MISSING_EXECUTION_QUOTE);
    if (!cost.complete) reasons.push(REASONS.MISSING_COST);
    const mandate = mandateGates({ candidate, amount_usd_micros: amount && amount.toString(), mandate: request.mandate });
    if (!mandate.pass) reasons.push(...mandate.gates.filter(g => !g.pass).map(g => g.code));
    const afterEs = expectedShortfall({ backend_metric_result: request.after_es }), beforeEs = expectedShortfall({ backend_metric_result: request.before_es });
    if (objective === "reduce_risk" && (beforeEs.status !== "COMPUTED" || afterEs.status !== "COMPUTED")) reasons.push("ES_NOT_COMPARABLE");
    const uniqueReasons = [...new Set(reasons)], analyticalReasons = new Set([REASONS.MISSING_EXECUTION_QUOTE, REASONS.MISSING_COST]);
    const hardReasons = uniqueReasons.filter(reason => !analyticalReasons.has(reason));
    let status = hardReasons.length ? "REJECTED" : (cost.complete && !quoteMissing ? "ELIGIBLE" : "ANALYTICAL_ONLY");
    if (objective === "reduce_risk" && uniqueReasons.includes("ES_NOT_COMPARABLE")) status = "NOT_RANKABLE";
    return { status, objective, source_id: source ? source.id || source.symbol || null : null,
      candidate_id: candidate.id || candidate.instrument_id || candidate.name || null, amount_usd_micros: amount === null ? null : amount.toString(),
      before: request.before || { nav_usd_micros: null, es: beforeEs }, after: request.after || { nav_usd_micros: null, es: afterEs },
      delta: request.delta || { income_bps: numberString(candidate.rate_bps), es_bps: null }, cost_ledger: cost, mandate,
      reasons: uniqueReasons, rank_tuple: [status === "ELIGIBLE" ? 0 : 1, Number(candidate.rate_bps || 0), cost.complete ? 1 : 0, String(candidate.id || candidate.name || "")] };
  }
  function orderOpportunities(opportunities) {
    return (opportunities || []).slice().sort((a, b) => { const ar = a.rank_tuple || [1, 0, 0, a.candidate_id || ""], br = b.rank_tuple || [1, 0, 0, b.candidate_id || ""];
      for (let i = 0; i < ar.length; i++) { if (ar[i] === br[i]) continue; if (typeof ar[i] === "string" || typeof br[i] === "string") return String(ar[i]).localeCompare(String(br[i])); return i === 0 ? Number(ar[i]) - Number(br[i]) : Number(br[i]) - Number(ar[i]); } return 0; });
  }
  function createAssistantEnvelope(input) {
    const source = input || {}, facts = Array.isArray(source.facts) ? source.facts : [];
    return { schema_version: "fenorium/assistant-envelope/1", method_version: source.method_version || "portfolio-browser-v1",
      as_of: source.as_of || null,
      facts: JSON.parse(JSON.stringify(facts)).sort((a, b) => String(a.field_ref).localeCompare(String(b.field_ref))) };
  }
  function address(value) {
    return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value)
      ? value.toLowerCase()
      : null;
  }
  function uintWord(value) {
    const parsed = asBigInt(value);
    if (parsed === null || parsed < 0n || parsed >= 1n << 256n) throw new Error("ABI uint256 is out of range");
    return parsed.toString(16).padStart(64, "0");
  }
  function hexBody(value, field = "hex data") {
    if (typeof value !== "string" || !/^0x(?:[0-9a-fA-F]{2})*$/.test(value)) throw new Error(`${field} must be byte-aligned hex`);
    return value.slice(2).toLowerCase();
  }
  function encodeBalanceOfCall(wallet) {
    const normalized = address(wallet);
    if (!normalized) throw new Error("wallet must be a 20-byte EVM address");
    return BALANCE_OF_SELECTOR + normalized.slice(2).padStart(64, "0");
  }
  function encodeAggregate3(calls) {
    if (!Array.isArray(calls) || !calls.length) throw new Error("aggregate3 requires at least one call");
    const tuples = calls.map((call) => {
      const target = address(call?.target);
      if (!target) throw new Error("aggregate3 target must be a 20-byte EVM address");
      const data = hexBody(call.callData, "aggregate3 callData");
      const paddedData = data.padEnd(Math.ceil(data.length / 64) * 64, "0");
      return target.slice(2).padStart(64, "0")
        + uintWord(1)
        + uintWord(96)
        + uintWord(data.length / 2)
        + paddedData;
    });
    let cursorBytes = calls.length * 32;
    const offsets = tuples.map((tuple) => {
      const offset = uintWord(cursorBytes);
      cursorBytes += tuple.length / 2;
      return offset;
    });
    const array = uintWord(calls.length) + offsets.join("") + tuples.join("");
    return AGGREGATE3_SELECTOR + uintWord(32) + array;
  }
  function readWord(body, byteOffset, field) {
    const start = byteOffset * 2;
    const word = body.slice(start, start + 64);
    if (word.length !== 64) throw new Error(`aggregate3 result is truncated at ${field}`);
    return BigInt(`0x${word}`);
  }
  function safeOffset(value, bodyBytes, field) {
    if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error(`aggregate3 ${field} offset is too large`);
    const offset = Number(value);
    if (offset % 32 || offset < 0 || offset + 32 > bodyBytes) throw new Error(`aggregate3 ${field} offset is invalid`);
    return offset;
  }
  function decodeAggregate3(value, expectedCount = null) {
    const body = hexBody(value, "aggregate3 result");
    const bodyBytes = body.length / 2;
    if (bodyBytes < 64 || bodyBytes % 32) throw new Error("aggregate3 result has invalid length");
    const arrayStart = safeOffset(readWord(body, 0, "array"), bodyBytes, "array");
    const countBig = readWord(body, arrayStart, "count");
    if (countBig > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("aggregate3 result count is too large");
    const count = Number(countBig);
    if (expectedCount !== null && count !== expectedCount) throw new Error(`aggregate3 returned ${count} results for ${expectedCount} calls`);
    const offsetsStart = arrayStart + 32;
    if (offsetsStart + count * 32 > bodyBytes) throw new Error("aggregate3 result offsets are truncated");
    return Array.from({ length: count }, (_, index) => {
      const tupleRelative = safeOffset(readWord(body, offsetsStart + index * 32, `result ${index}`), bodyBytes - offsetsStart, `result ${index}`);
      const tupleStart = offsetsStart + tupleRelative;
      const successWord = readWord(body, tupleStart, `result ${index} success`);
      if (successWord !== 0n && successWord !== 1n) throw new Error(`aggregate3 result ${index} success flag is invalid`);
      const dataRelative = safeOffset(readWord(body, tupleStart + 32, `result ${index} data`), bodyBytes - tupleStart, `result ${index} data`);
      const dataStart = tupleStart + dataRelative;
      const lengthBig = readWord(body, dataStart, `result ${index} length`);
      if (lengthBig > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error(`aggregate3 result ${index} length is too large`);
      const length = Number(lengthBig);
      const dataBodyStart = dataStart + 32;
      if (dataBodyStart + length > bodyBytes) throw new Error(`aggregate3 result ${index} data is truncated`);
      return { success: successWord === 1n, returnData: `0x${body.slice(dataBodyStart * 2, (dataBodyStart + length) * 2)}` };
    });
  }
  function registeredKind(row) {
    const category = String(row?.category || "").toLowerCase();
    if (category === "vault") return "VAULT";
    if (category === "native") return "PT";
    return "ERC20";
  }
  function portfolioCategory(category) {
    const normalized = String(category || "UNKNOWN").toLowerCase();
    if (normalized === "native" || normalized === "rwa") return "fixed_income";
    if (normalized === "vault") return "variable_income";
    return normalized;
  }
  function stableAssetIdentity(asset) {
    const normalized = address(asset?.address);
    if (!normalized || !REGISTERED_CHAIN_IDS.has(Number(asset?.chain_id))) return null;
    return `eip155:${Number(asset.chain_id)}/${String(asset.kind).toLowerCase()}:${normalized}`;
  }
  // The identity a universe row will have once it becomes a registered asset. Anything that
  // needs to match a row to an asset must go through this: the kind is derived from the row's
  // category, so reconstructing an identity by assuming "erc20" silently misses notes and
  // vaults. The universe builder mirrors this mapping when it stamps price observations.
  function registeredAssetIdentity(row) {
    return stableAssetIdentity({chain_id: row?.chain_id, address: row?.address, kind: registeredKind(row)});
  }
  function registeredAssetsFromUniverse(universe, chainId) {
    const selectedChain = Number(chainId);
    if (!REGISTERED_CHAIN_IDS.has(selectedChain)) return [];
    const byAddress = new Map();
    for (const row of Array.isArray(universe) ? universe : []) {
      const token = address(row?.address);
      const decimals = Number(row?.decimals);
      if (Number(row?.chain_id) !== selectedChain || !token || !Number.isInteger(decimals) || decimals < 0 || decimals > 255) continue;
      const asset = {
        chain_id: selectedChain,
        kind: registeredKind(row),
        address: token,
        decimals,
        symbol: String(row.symbol || row.name || ""),
        category: portfolioCategory(row.category),
        protocol: String(row.venue || "UNKNOWN"),
        is_stablecoin: row.is_stablecoin === true,
        is_variable_income: row.is_variable_income === true || String(row.category).toLowerCase() === "vault",
      };
      asset.identity = stableAssetIdentity(asset);
      const addressKey = `${selectedChain}:${token}`;
      if (asset.identity && !byAddress.has(addressKey)) byAddress.set(addressKey, asset);
    }
    return [...byAddress.values()].sort((left, right) => left.identity.localeCompare(right.identity));
  }
  function priceObservationsFromUniverse(universe, assets) {
    const allowed = new Set((assets || []).map((asset) => asset.identity));
    const seen = new Set();
    const observations = [];
    for (const row of Array.isArray(universe) ? universe : []) {
      const kind = registeredKind(row);
      const identity = stableAssetIdentity({ chain_id: row?.chain_id, address: row?.address, kind });
      if (!identity || !allowed.has(identity) || !Array.isArray(row.price_observations)) continue;
      for (const source of row.price_observations) {
        const price = source?.price_usd_fixed;
        const exactPrice = nonNegativeBigInt(price);
        const observedAt = source?.observed_at;
        if (source?.identity !== identity
          || typeof source.source_id !== "string" || !source.source_id
          || typeof source.independence_key !== "string" || !source.independence_key
          || exactPrice === null || exactPrice <= 0n
          || typeof observedAt !== "string" || Number.isNaN(Date.parse(observedAt))
          || typeof source.method_version !== "string" || !source.method_version) continue;
        const priceText = exactPrice.toString();
        const key = `${identity}:${source.independence_key}:${source.source_id}:${observedAt}:${priceText}`;
        if (seen.has(key)) continue;
        seen.add(key);
        observations.push({
          schema_version: "fenorium/price-observation/v1",
          identity,
          source_id: source.source_id,
          independence_key: source.independence_key,
          price_usd_fixed: priceText,
          observed_at: observedAt,
          method_version: source.method_version,
        });
      }
    }
    return observations.sort((left, right) => (
      left.identity.localeCompare(right.identity)
      || left.independence_key.localeCompare(right.independence_key)
      || left.source_id.localeCompare(right.source_id)
    ));
  }
  function portfolioAnalyticsView(data, requestedWallet = null) {
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("portfolio analytics data must be an object");
    const walletSnapshot = data.wallet_snapshot;
    const valuation = data.valuation;
    if (!walletSnapshot || !valuation || !data.allocation || !data.analytics || !Array.isArray(valuation.positions)) {
      throw new Error("portfolio analytics response is missing wallet, valuation, allocation, or analytics data");
    }
    // Either scope is a contract this kernel understands. Refusing the wider one would reject
    // exactly the snapshot that carries the holdings the coverage figure depends on.
    const SCOPES = ["REGISTERED_ASSETS_ONLY", "REGISTERED_PLUS_ENUMERATED"];
    if (!address(walletSnapshot.wallet) || !SCOPES.includes(walletSnapshot.discovery_scope) || !Array.isArray(walletSnapshot.scanned_chains)) {
      throw new Error("portfolio analytics response has an invalid wallet snapshot contract");
    }
    if (requestedWallet != null && (!address(requestedWallet)
      || walletSnapshot.wallet.toLowerCase() !== requestedWallet.toLowerCase())) {
      throw new Error("portfolio analytics wallet snapshot does not match the requested wallet");
    }
    const widened = walletSnapshot.discovery_scope === "REGISTERED_PLUS_ENUMERATED";
    if (widened && (!Array.isArray(walletSnapshot.enumerated) || typeof walletSnapshot.enumeration_complete !== "boolean")) {
      throw new Error("a widened wallet snapshot must carry its enumerated holdings and completeness");
    }
    // A holding the registry cannot price is why a total is withheld, so the view carries the
    // identities rather than leaving a reader to wonder what the refusal was about.
    const unvalued = Array.isArray(valuation.unvalued_identities) ? valuation.unvalued_identities : [];
    if (unvalued.length && valuation.schema_version !== "fenorium/portfolio-valuation/v2") {
      throw new Error("a valuation naming unpriced holdings must declare v2");
    }
    if (nonNegativeBigInt(valuation.priced_nav_lower_bound_usd_micros) === null) {
      throw new Error("portfolio valuation priced_nav_lower_bound_usd_micros is invalid");
    }
    if (!Number.isSafeInteger(valuation.unresolved_position_count) || valuation.unresolved_position_count < 0) {
      throw new Error("portfolio valuation unresolved_position_count is invalid");
    }
    for (const field of ["position_count_coverage_bps", "value_coverage_bps"]) {
      if (valuation[field] !== null && (!Number.isSafeInteger(valuation[field]) || valuation[field] < 0 || valuation[field] > 10000)) {
        throw new Error(`portfolio valuation ${field} is invalid`);
      }
    }
    if (!Array.isArray(data.allocation.buckets) || !valuation.positions.every((priced) => (
      priced && typeof priced === "object" && !Array.isArray(priced)
      && typeof priced.identity === "string"
      && priced.position && priced.position.raw_position && priced.position.raw_position.asset
      && (priced.value_usd_micros === null || nonNegativeBigInt(priced.value_usd_micros) !== null)
    ))) {
      throw new Error("portfolio analytics response contains malformed positions or allocation buckets");
    }
    const positions = valuation.positions.map((priced) => {
      const classified = priced?.position || {};
      const raw = classified.raw_position || {};
      const asset = raw.asset || {};
      const adapterError = raw.adapter_error || null;
      return {
        id: priced.identity || raw.identity,
        identity: priced.identity || raw.identity,
        position_type: String(asset.kind || "").toLowerCase(),
        address: asset.address,
        chain_id: asset.chain_id,
        symbol: asset.symbol,
        name: asset.symbol || priced.identity,
        asset_type: String(asset.category || "unknown").toLowerCase(),
        protocol: asset.protocol,
        raw_units: raw.raw_amount == null ? null : String(raw.raw_amount),
        decimals: asset.decimals,
        value_usd_micros: priced.value_usd_micros == null ? null : String(priced.value_usd_micros),
        is_stablecoin: asset.is_stablecoin === true,
        is_variable_income: asset.is_variable_income === true,
        status: classified.overall_status || (raw.unresolved ? "UNRESOLVED" : "PARTIALLY_SUPPORTED"),
        unresolved: raw.unresolved === true,
        zero_balance: raw.zero_balance === true,
        adapter_error: adapterError,
        reason: adapterError?.reason_code || priced.reason_code || priced.price?.reason_code || null,
        source_timestamp: valuation.as_of || walletSnapshot.as_of,
      };
    }).filter((position) => position.unresolved || position.raw_units !== "0");
    return {
      schema_version: walletSnapshot.schema_version,
      method_version: walletSnapshot.method_version,
      account: walletSnapshot.wallet,
      scope: walletSnapshot.discovery_scope,
      positions,
      chains: (walletSnapshot.scanned_chains || []).map((chain) => ({
        chain_id: chain.chain_id,
        finalized_block_ref: chain.block?.block_number == null ? null : `0x${Number(chain.block.block_number).toString(16)}`,
        rpc_status: chain.complete ? "COMPLETE" : "PARTIAL",
        source_timestamp: walletSnapshot.as_of,
      })),
      provenance: {
        kind: "CONNECTED_WALLET",
        // The label states the scope that was actually used, so a reader is never told the scan
        // was registry-only when it was widened, or the reverse.
        label: `CONNECTED_WALLET / ${walletSnapshot.discovery_scope}`,
        as_of: walletSnapshot.as_of,
        source: "injected provider observations; deterministic platform classification",
      },
      discovery: {
        widened,
        complete: widened ? walletSnapshot.enumeration_complete : true,
        unvalued_identities: unvalued,
      },
      valuation,
      allocation: data.allocation,
      analytics: data.analytics,
      risk_scope: data.risk_scope || null,
    };
  }

  function portfolioAddress(position) {
    const direct = address(position?.address);
    if (direct) return direct;
    const text = String(position?.identity || position?.id || "");
    const match = text.match(/(?:erc20|token|vault):((?:0x)?[0-9a-fA-F]{40})(?:$|[/?])/);
    return address(match ? match[1] : null);
  }
  function portfolioChain(position) {
    const direct = Number(position?.chain_id);
    if (Number.isInteger(direct)) return direct;
    const text = String(position?.identity || position?.id || "");
    const match = text.match(/(?:eip155:|chain[-/:])([0-9]+)/i);
    return match ? Number(match[1]) : null;
  }
  function portfolioUniverseIndex(row, index) {
    return Number.isInteger(row?._index) ? row._index : index;
  }
  function portfolioNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  function portfolioDateDays(maturity, asOf) {
    if (typeof maturity !== "string" || !maturity.trim()) return null;
    const dateOnly = maturity.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (dateOnly) {
      const year = Number(dateOnly[1]), month = Number(dateOnly[2]), day = Number(dateOnly[3]);
      const checked = new Date(Date.UTC(year, month - 1, day));
      if (checked.getUTCFullYear() !== year || checked.getUTCMonth() !== month - 1 || checked.getUTCDate() !== day) return null;
    }
    const maturityTime = Date.parse(maturity);
    const asOfTime = Date.parse(asOf);
    if (!Number.isFinite(maturityTime) || !Number.isFinite(asOfTime)) return null;
    return (maturityTime - asOfTime) / 86400000;
  }
  // engine/registry/instruments.py::instrument_id for a row without a token address. Python's
  // quote() passes letters, digits, "_.-~" and the safe set "@._-"; encodeURIComponent also leaves
  // !'()* unescaped and escapes "@", so both are corrected to keep the two ids identical.
  function quoteLikePython(text) {
    return encodeURIComponent(text)
      .replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
      .replace(/%40/g, "@");
  }
  // Within 30 days of maturity the annualized cost is a figure nobody pays: a flat 30 bp entry fee
  // over 2.4 days annualizes to 46%. What a buyer pays is the yield and the cost over the days that
  // remain, so the screens show those (owner 2026-10-20) and keep the annualized ones as the tooltip.
  // Components are the engine's own: (headline − credit haircut) × days/365 − entry fee − impact.
  const HOLDING_PERIOD_DAYS = 30;
  function holdingPeriod(row) {
    const days = Number(row?.days_to_maturity), rating = row?.rating;
    if (!Number.isFinite(days) || days <= 0 || days >= HOLDING_PERIOD_DAYS || rating?.computable !== true) return null;
    const headline = Number(rating.headline_bps ?? row.rate_bps), credit = Number(rating.credit_haircut_bps ?? 0);
    const baseFee = Number(rating.base_fee_bps), impact = Number(rating.modelled_impact_bps);
    if (![headline, credit, baseFee, impact].every(Number.isFinite)) return null;
    const fraction = days / 365;
    const gross = (headline - credit) * fraction;
    const cost = baseFee + impact;
    return {days, fraction, gross_bps: gross, cost_bps: cost, net_bps: gross - cost,
      annualized_net_bps: rating.net_bps ?? null, annualized_execution_bps: rating.execution_bps ?? null};
  }

  // The universe row's id as engine.registry.instruments.instrument_id names it: a token's asset id,
  // a position's chain, venue and name. Fixture rows may need the fallback derivation.
  function instrumentId(row) {
    if (typeof row?.instrument_id === "string" && row.instrument_id.length > 0) return row.instrument_id;
    if (!Number.isInteger(row?.chain_id) || row.chain_id <= 0) return null;
    if (typeof row.address === "string" && /^0x[0-9a-fA-F]{40}$/.test(row.address)) {
      return `eip155:${row.chain_id}/erc20:${row.address.toLowerCase()}`;
    }
    return row.address == null ? positionIdentity(row) : null;
  }

  function positionIdentity(row) {
    const chain = Number(row?.chain_id);
    if (!Number.isInteger(chain) || chain <= 0 || address(row?.address)) return null;
    const venue = quoteLikePython(String(row?.venue ?? "").trim().toLowerCase().replace(/\s+/g, "-") || "unknown");
    return `eip155:${chain}/position:${venue}:${quoteLikePython(String(row?.name ?? "").trim())}`;
  }
  function portfolioRows(positions, universe, asOf) {
    const market = Array.isArray(universe) ? universe : [];
    const byKey = new Map();
    // A venue position (an Exactly deposit, Midnight credit) has no token address; the server names
    // it by its instrument identity, which is rebuilt here for every addressless universe row.
    const byIdentity = new Map();
    market.forEach((row, index) => {
      const chain = Number(row?.chain_id), token = address(row?.address);
      if (!Number.isInteger(chain)) return;
      const lookup = token ? `${chain}:${token}` : positionIdentity(row);
      if (!lookup) return;
      const index_ = token ? byKey : byIdentity;
      const prior = index_.get(lookup) || [];
      prior.push({ row, index });
      index_.set(lookup, prior);
    });
    return (Array.isArray(positions) ? positions : []).map((position, originalIndex) => {
      const source = position && typeof position === "object" ? position : {};
      const chainId = portfolioChain(source), token = portfolioAddress(source);
      const venuePosition = !token && String(source.position_type || "").toLowerCase() === "position";
      const matches = chainId !== null && token ? byKey.get(`${chainId}:${token}`) || []
        : venuePosition && typeof source.identity === "string" ? byIdentity.get(source.identity) || []
        : [];
      const match = matches.length === 1 ? matches[0] : null;
      const metadata = match?.row || null;
      const adapterError = source.adapter_error != null;
      const hasExplicitValue = Object.prototype.hasOwnProperty.call(source, "value_usd_micros");
      const value = adapterError ? null : (hasExplicitValue ? nonNegativeBigInt(source.value_usd_micros) : valueOf(source));
      const unresolved = adapterError || source.unresolved === true || String(source.status || "").toUpperCase() === "UNRESOLVED";
      const status = unresolved ? "UNRESOLVED" : value === null ? "UNPRICED" : "ALIGNED";
      const stableIdentity = `position:${chainId == null ? "unknown-chain" : chainId}:${token || String(source.id || source.identity || "unknown")}#${originalIndex}`;
      const chainLabel = metadata?.chain || (chainId == null ? null : CHAINS.find((row) => row.chain_id === chainId)?.name) || null;
      return Object.assign({}, source, {
        identity: stableIdentity,
        // The server's instrument id of a venue position, kept so the Holdings table can open the
        // universe row it was joined to; a token position opens by chain and address instead.
        instrument_identity: !token && typeof source.identity === "string" ? source.identity : null,
        asset_type: metadata?.asset_type ?? metadata?.category ?? source.asset_type ?? source.category ?? null,
        chain: chainLabel,
        category: metadata?.category ?? source.category ?? null,
        unresolved,
        issuer: metadata?.issuer ?? source.issuer ?? null,
        venue: metadata?.venue ?? source.venue ?? null,
        maturity: metadata?.maturity ?? source.maturity ?? null,
        days_to_maturity: metadata?.days_to_maturity ?? source.days_to_maturity ?? null,
        // Only a computable rating may contribute a headline. A refusal keeps headline_bps at
        // its 0 default -- 238 checked-in instruments look like that -- so reading it here
        // published 0.00% where the engine had explicitly declined to price, and carried that
        // zero into the weighted portfolio yield.
        yield_bps: portfolioNumber(metadata?.ytm_bps ?? metadata?.apy_bps ?? metadata?.headline_bps ?? metadata?.rate_bps ?? (metadata?.rating?.computable === true ? metadata?.rating?.headline_bps : null) ?? source.yield_bps),
        // A yield without its kind is not a yield: 'realized' is a trailing observation and
        // 'issuer_quoted' is unverified, and neither is an executable rate. Carrying the number
        // without the qualifier made a Morpho trailing rate render as a plain APY.
        rate_kind: metadata?.rate_kind ?? source.rate_kind ?? null,
        value_usd_micros: unresolved || value === null ? null : value.toString(),
        status,
        market_index: match ? portfolioUniverseIndex(metadata, match.index) : null,
        maturity_bucket: maturityBucket(Object.assign({}, source, metadata || {}, {
          asset_type: metadata?.asset_type ?? metadata?.category ?? source.asset_type ?? source.category,
          category: metadata?.category ?? source.category,
          maturity: metadata?.maturity ?? source.maturity,
          days_to_maturity: metadata?.days_to_maturity ?? source.days_to_maturity,
        }), asOf),
        _original_index: originalIndex,
      });
    });
  }
  function maturityBucket(position, asOf) {
    const assetType = String(position?.asset_type ?? position?.category ?? "").toLowerCase();
    const category = String(position?.category || "").toLowerCase();
    // Dated RWA instruments include the declared testnet treasuries. Open-ended
    // funds without maturity evidence remain N/A.
    if ((category === "rwa" || assetType === "rwa") && position?.maturity == null && position?.days_to_maturity == null) return "N/A";
    // Settlement assets join the other non-maturing kinds. Cash does not mature, so N/A is the
    // honest answer; UNKNOWN would report missing maturity evidence for something that has no
    // maturity to be missing.
    if (category === "vault" || assetType === "vault" || category === "settlement"
      || ["variable_income", "stablecoin", "settlement", "cash", "crypto", "equity"].includes(assetType)) return "N/A";
    if (!assetType || assetType === "unknown") return "UNKNOWN";
    let days;
    if (asOf !== undefined && asOf !== null && position?.maturity !== null && position?.maturity !== undefined) {
      days = portfolioDateDays(position.maturity, asOf);
    } else {
      days = portfolioNumber(position?.days_to_maturity);
    }
    if (days === null) return "UNKNOWN";
    if (days < 0) return "Matured";
    // Match the terminal's existing 180-day six-month convention.
    if (days < 180) return "< 6M";
    if (days < 365) return "6M–1Y";
    if (days < 1095) return "1–3Y";
    if (days < 1825) return "3–5Y";
    return "5Y+";
  }
  // A tokenized treasury fund and a Pendle principal token are both fixed income, and they are
  // not the same exposure: one is an issuer's claim on treasuries, the other a discount struck
  // in a DeFi market. A chart that merges them hides the distinction this platform exists to
  // draw. Only the chart groups this way -- the holdings filter still selects on the raw
  // asset_type -- and a row whose category we never resolved stays plain fixed income rather
  // than being assigned to either side.
  function assetClass(row) {
    const type = String(row?.asset_type || "").toLowerCase();
    const category = String(row?.category || "").toLowerCase();
    // A holding matched to the universe carries that row's category as its asset type, so the
    // same instrument arrives here as "fixed_income" from a fixture and as "rwa" or "native"
    // from a wallet scan. Both name the same exposure and must land in the same class.
    const marks = new Set([type, category]);
    if (!marks.has("fixed_income") && !marks.has("rwa") && !marks.has("native")) {
      return type || "UNKNOWN";
    }
    if (marks.has("rwa")) return "tokenized_treasuries";
    if (marks.has("native") || marks.has("vault")) return "defi_fixed_income";
    return type;
  }
  function exposureLabel(row, dimension, asOf) {
    if (dimension === "maturity") return row?.maturity_bucket || maturityBucket(row, asOf);
    if (dimension === "chain") return row?.chain || (row?.chain_id == null ? "UNKNOWN" : String(row.chain_id));
    if (dimension === "asset_type") return assetClass(row);
    const value = row?.[dimension];
    return value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);
  }
  function portfolioExposure(rows, dimension, asOf) {
    const source = Array.isArray(rows) ? rows : [], groups = new Map();
    for (const row of source) {
      const label = exposureLabel(row, dimension, asOf);
      if (!groups.has(label)) groups.set(label, { label, assets: [], known: 0n, priced: 0n, hasUnknown: false, unresolved_count: 0 });
      const group = groups.get(label);
      group.assets.push(row);
      const value = nonNegativeBigInt(row?.value_usd_micros);
      if (value === null) group.hasUnknown = true;
      else { group.known += value; group.priced += value; }
      if (value === null) group.unresolved_count += 1;
    }
    const grouped = [...groups.values()], pricedTotal = grouped.reduce((sum, group) => sum + group.priced, 0n);
    const total = source.some(row => nonNegativeBigInt(row?.value_usd_micros) !== null) ? pricedTotal : null;
    const shares = new Map();
    if (pricedTotal > 0n) {
      let allocated = 0;
      const remainders = grouped.map((group, index) => {
        if (group.priced === 0n) return { index, remainder: -1n };
        const numerator = group.priced * 10000n, base = numerator / pricedTotal;
        shares.set(index, Number(base)); allocated += Number(base);
        return { index, remainder: numerator % pricedTotal };
      }).filter((item) => item.remainder >= 0n).sort((left, right) => right.remainder > left.remainder ? 1 : right.remainder < left.remainder ? -1 : left.index - right.index);
      for (let index = 0; index < 10000 - allocated; index += 1) shares.set(remainders[index % remainders.length].index, shares.get(remainders[index % remainders.length].index) + 1);
    }
    return {
      rows: grouped.map((group, index) => ({ label: group.label, assets: group.assets,
        value_usd_micros: group.hasUnknown ? null : group.known.toString(),
        share_bps: shares.has(index) ? shares.get(index) : pricedTotal > 0n && group.assets.some(row => nonNegativeBigInt(row?.value_usd_micros) !== null) ? 0 : null,
        priced_value_usd_micros: group.assets.some(row => nonNegativeBigInt(row?.value_usd_micros) !== null) ? group.priced.toString() : null, unresolved_count: group.unresolved_count })),
      total_usd_micros: total === null ? null : total.toString(),
      unresolved_count: source.filter((row) => nonNegativeBigInt(row?.value_usd_micros) === null).length,
    };
  }
  function queryHoldings(rows, options) {
    const opts = options || {}, source = Array.isArray(rows) ? rows : [], search = String(opts.search || "").trim().toLowerCase();
    let filtered = source.filter((row) => {
      const text = [row?.name, row?.symbol, row?.id, row?.identity, row?.issuer, row?.venue, row?.chain, row?.asset_type].filter(Boolean).join(" ").toLowerCase();
      return (!search || text.includes(search))
        && (!opts.asset_type || String(row?.asset_type || "UNKNOWN") === String(opts.asset_type))
        && (!opts.chain || String(row?.chain || "UNKNOWN") === String(opts.chain) || String(row?.chain_id) === String(opts.chain))
        && (!opts.status || String(row?.status) === String(opts.status));
    });
    const sort = String(opts.sort || "value-desc");
    const name = (row) => String(row?.name || row?.symbol || row?.identity || "").toLowerCase();
    const numeric = (row, key) => key === "value" ? nonNegativeBigInt(row?.value_usd_micros) : portfolioNumber(row?.[key]);
    filtered.sort((left, right) => {
      let comparison = 0;
      if (sort === "name") comparison = name(left).localeCompare(name(right));
      else if (sort === "yield-desc") comparison = compareSort(numeric(left, "yield_bps"), numeric(right, "yield_bps"), -1);
      else if (sort === "maturity") comparison = compareSort(portfolioSortDays(left, opts.asOf), portfolioSortDays(right, opts.asOf), 1);
      else if (sort === "value-asc") comparison = compareSort(numeric(left, "value"), numeric(right, "value"), 1);
      else comparison = compareSort(numeric(left, "value"), numeric(right, "value"), -1);
      return comparison || Number(left?._original_index || 0) - Number(right?._original_index || 0);
    });
    const pageSize = Math.max(1, Number.isInteger(Number(opts.pageSize)) ? Number(opts.pageSize) : 10), total = filtered.length;
    const pages = Math.max(1, Math.ceil(total / pageSize)), page = Math.min(Math.max(1, Number.isInteger(Number(opts.page)) ? Number(opts.page) : 1), pages);
    const start = total ? (page - 1) * pageSize + 1 : 0, end = total ? Math.min(start + pageSize - 1, total) : 0;
    return { rows: filtered.slice(start ? start - 1 : 0, end), filtered, total, page, pageSize, pages, start, end };
  }
  function decimalSeconds(value) {
    if (typeof value !== "string" || !/^(?:0|[1-9][0-9]*)$/.test(value)) return null;
    return asBigInt(value);
  }
  function rpcSeconds(value) {
    if (typeof value !== "string") return null;
    if (/^0x[0-9a-fA-F]+$/.test(value)) return asBigInt(value);
    return decimalSeconds(value);
  }
  function isoFromSeconds(value) {
    const seconds = rpcSeconds(value), maxDateSeconds = 8640000000000n;
    if (seconds === null || seconds > maxDateSeconds) return null;
    const iso = new Date(Number(seconds) * 1000).toISOString();
    return Number.isFinite(Date.parse(iso)) ? iso : null;
  }
  function isoSeconds(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    const parsed = Date.parse(value);
    if (!Number.isFinite(parsed) || parsed % 1000 !== 0) return null;
    return BigInt(parsed / 1000);
  }
  function fixedMul(left, right) { return (left * right) / FIXED_MATH_SCALE; }
  function fixedLogOnePlus(rateFixed) {
    let term = rateFixed, total = 0n;
    for (let n = 1n; n <= 160n; n += 1n) {
      const add = term / n;
      if (add === 0n) break;
      total += n % 2n ? add : -add;
      term = fixedMul(term, rateFixed);
    }
    return total;
  }
  function fixedExp(value) {
    let term = FIXED_MATH_SCALE, total = FIXED_MATH_SCALE;
    for (let n = 1n; n <= 160n; n += 1n) {
      term = fixedMul(term, value) / n;
      if (term === 0n) break;
      total += term;
    }
    return total;
  }
  function declaredAccrual(asset) {
    return asset?.price_accrual || null;
  }
  function historicalTransferPrice(asset, timestamp) {
    const token = address(asset?.address), accrual = declaredAccrual(asset);
    const schedule = DECLARED_TRANSFER_ASSETS.has(token) ? null : declaredSchedules.get(token);
    if (!token || (!DECLARED_TRANSFER_ASSETS.has(token) && !schedule) || Number(asset?.chain_id) !== 84532 || asset?.rate_kind !== "declared_demo") return null;
    // A flat unit is only honoured where the row agrees it has no schedule. A universe that
    // grew one for this token would be claiming it moves, and the two statements cannot both
    // be trusted, so neither is used.
    if (Object.hasOwn(DECLARED_UNIT_PRICES, token)) {
      return accrual === null && rpcSeconds(timestamp) !== null ? DECLARED_UNIT_PRICES[token].toString() : null;
    }
    if (accrual?.model !== "declared_compound_interest_v1" || (schedule
      ? nonNegativeBigInt(accrual?.price_scale) !== DECLARED_PRICE_SCALE
      : String(accrual?.price_scale) !== DECLARED_PRICE_SCALE.toString())) return null;
    const base = nonNegativeBigInt(accrual.base_price_usd_fixed), rate = nonNegativeBigInt(accrual.rate_bps);
    const moment = rpcSeconds(timestamp), start = isoSeconds(accrual.accrual_start), end = isoSeconds(accrual.maturity);
    if (base === null || rate === null || moment === null || start === null || end === null || end < start
      || end - start > 315360000n || rate >= 10000n || rate !== (schedule ? schedule.rate : DECLARED_TRANSFER_RATES[token])) return null;
    if (schedule && (base !== schedule.base || start !== schedule.start || end !== schedule.end)) return null;
    const clamped = moment < start ? start : moment > end ? end : moment;
    const elapsed = clamped - start;
    const rateFixed = (rate * FIXED_MATH_SCALE) / 10000n;
    const exponent = (fixedLogOnePlus(rateFixed) * elapsed) / SECONDS_PER_YEAR;
    const factor = fixedExp(exponent);
    return ((base * factor + FIXED_MATH_SCALE / 2n) / FIXED_MATH_SCALE).toString();
  }
  // Client-side reconstruction. COMPLETE scan coverage alone never establishes an opening balance.
  function derivePortfolioPerformance(history, finalized) {
    const source = 'Derived from finalized ERC-20 transfers; declared_demo synthetic accrual, not measured market returns';
    const unknown = reason => ({status: 'UNKNOWN', reason, source, points: [], balance_timeline: [],
      nav_usd_micros: null, cumulative_pnl_usd_micros: null, net_external_flows_usd_micros: null,
      total_return_bps: null, annualized_return_bps: null, best_day_usd_micros: null, worst_day_usd_micros: null,
      tracking_since: null, periods: Object.fromEntries(['7D', '30D'].map(key => [key, {status: 'UNKNOWN', pnl_usd_micros: null, reason}]))});
    const key = item => `${item?.chain_id}:${address(item?.address || item?.asset_address)}`;
    const rawInteger = value => typeof value === 'bigint' ? value : typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : null;
    try {
      if (!history || history.status !== 'COMPLETE' || !history.assets?.length) {
        // A scan that ran and failed said exactly what a scan that never ran says, because the
        // per-asset reasons were collected and then never looked at. That made a wallet whose
        // provider refuses eth_getLogs indistinguishable from one nobody had scanned yet, and
        // there was no way to tell the two apart from the screen.
        const failed = (history?.assets || [])
          .filter(record => record?.reason)
          .map(record => `${record.asset?.symbol || record.asset?.name || record.asset?.address}: ${record.reason}`);
        // Named in one request, a refusal belongs to every instrument in it -- which on a chain
        // carrying a hundred of them is a hundred copies of one sentence. The reader needs the
        // refusal and an idea of its reach, not the list.
        if (failed.length) {
          const shown = failed.slice(0, 3).join(' · ');
          const rest = failed.length - 3;
          return unknown(`Transfer scan did not complete · ${shown}${rest > 0 ? ` · and ${rest} more` : ''}`);
        }
        return unknown(history?.error || 'Derived portfolio history is unavailable; scan finalized transfers first');
      }
      const end = isoSeconds(finalized?.as_of), block = nonNegativeBigInt(finalized?.block_number);
      if (end === null || block === null || block !== nonNegativeBigInt(history.to_block)) return unknown('Transfer history and finalized balances must use the same block');
      const observations = new Map();
      for (const observation of finalized.observations || []) {
        if (observations.has(key(observation))) return unknown('Duplicate finalized balance observation');
        observations.set(key(observation), observation);
      }
      const assets = new Map(), events = new Map();
      for (const scanned of history.assets) {
        const asset = scanned.asset, identity = key(asset), name = asset.name || asset.symbol || asset.address;
        // A block count is not something a reader can weigh a position against. The chain's own
        // block time turns it into the thing they can: how far back the scan actually looked,
        // which is what a position opened before it fails to reconcile with.
        const reach = (blocks) => {
          const days = blocks == null ? null : transferScanDepth(asset.chain_id, {budget: 1, windowSize: Math.max(1, Math.ceil(Number(blocks) || 1))}).days;
          return days ? `${blocks} blocks (about ${days} days)` : `${blocks} blocks`;
        };
        const fail = detail => unknown(`${name}: ${detail}; scanned back ${reach(scanned.scanned_blocks ?? history.declared_depth)}, of a declared ${reach(history.declared_depth)}`);
        if (assets.has(identity)) return fail('duplicate asset history');
        const observed = observations.get(identity), balance = rawInteger(observed?.raw_amount);
        if (scanned.status !== 'COMPLETE' || observed?.error || balance === null || balance < 0n || nonNegativeBigInt(scanned.to_block) !== block) return fail('history does not reconcile with a verified finalized balance');
        const decimals = asset.decimals;
        if (!Number.isInteger(decimals) || decimals < 0 || decimals > 255) return fail('invalid decimals');
        const entry = {asset, balance: 0n, final: balance, scale: 10n ** BigInt(decimals)};
        assets.set(identity, entry);
        const seen = new Set();
        for (const row of scanned.rows || []) {
          const at = isoSeconds(row.timestamp), amount = rawInteger(row.raw_amount), rowBlock = nonNegativeBigInt(row.block_number);
          if (key(row) !== identity || at === null || at > end || amount === null || rowBlock === null
              || rowBlock > block || rowBlock < nonNegativeBigInt(scanned.from_block) || !row.id || seen.has(row.id)) return fail('invalid or duplicate transfer history');
          seen.add(row.id);
          // Both legs of a self-transfer and every event at the same timestamp are folded together.
          if (!events.has(at)) events.set(at, new Map());
          const changes = events.get(at);
          changes.set(identity, (changes.get(identity) || 0n) + amount);
          entry.balance += amount;
        }
        if (entry.balance !== balance) return fail(`history does not reconcile: transfers sum to ${entry.balance} raw units, finalized balanceOf is ${balance}`);
        entry.balance = 0n;
      }
      if (observations.size !== assets.size || [...observations.keys()].some(identity => !assets.has(identity))) return unknown('History does not cover every finalized balance observation');
      const times = [...events.keys()].sort((a,b) => a < b ? -1 : a > b ? 1 : 0);
      if (!times.length) return unknown('No dated transfer history exists; tracking start is UNKNOWN');
      const start = times[0], tracking = isoFromSeconds(start.toString());
      const marks = new Set([...times, end]);
      // UTC daily boundaries support full-day best/worst P&L. Exact period boundaries avoid interpolation.
      for (let at = (start / 86400n + 1n) * 86400n; at < end; at += 86400n) {
        if (marks.size > 20000) return unknown('History exceeds the client-side derivation limit');
        marks.add(at);
      }
      for (const days of [7n, 30n]) if (end - days * 86400n >= start) marks.add(end - days * 86400n);
      let flows = 0n, inflows = 0n, outflows = 0n, previousNav = 0n, returnFactor = FIXED_MATH_SCALE, hasCapital = false, fundedAt = null;
      const points = [], timeline = [];
      // The declared rate the client holds itself, on the same terms as the price: an artifact
      // that disagrees does not get to move the number. An asset with no client-held rate makes
      // the weighted yield UNKNOWN for that moment rather than being counted as zero.
      const declaredRate = (asset) => {
        const token = address(asset?.address);
        if (token === null) return null;
        if (Object.hasOwn(DECLARED_TRANSFER_RATES, token)) return DECLARED_TRANSFER_RATES[token];
        return Object.hasOwn(DECLARED_UNIT_PRICES, token) ? 0n : declaredSchedules.get(token)?.rate ?? null;
      };
      for (const at of [...marks].sort((a,b) => a < b ? -1 : a > b ? 1 : 0)) {
        let nav = 0n, before = 0n, flow = 0n, moveIn = 0n, moveOut = 0n, weighted = 0n, rated = 0n, rateKnown = true;
        for (const [identity, entry] of assets) {
          const change = events.get(at)?.get(identity) || 0n;
          const price = historicalTransferPrice(entry.asset, at.toString());
          if (price === null) return unknown(`${entry.asset.name || entry.asset.symbol || entry.asset.address}: no derivable historical price at ${isoFromSeconds(at.toString())}`);
          const value = raw => raw * BigInt(price) * 1000000n / (entry.scale * DECLARED_PRICE_SCALE);
          before += value(entry.balance);
          // Gross value across the wallet's boundary, kept apart from the net. They are two
          // different questions -- how much moved, and how much stayed -- and a book that swaps
          // one tracked asset for another moves a great deal while netting to nothing.
          const moved = value(change);
          flow += moved;
          if (moved > 0n) moveIn += moved;
          else if (moved < 0n) moveOut += moved;
          entry.balance += change;
          if (entry.balance < 0n) return unknown(`${entry.asset.name || entry.asset.address}: history does not reconcile (negative intermediate balance); scanned back ${history.declared_depth} blocks`);
          const held = value(entry.balance);
          nav += held;
          const rate = declaredRate(entry.asset);
          if (rate === null) rateKnown = false;
          else { weighted += held * rate; rated += held; }
          if (events.has(at)) timeline.push({asset_address: entry.asset.address, chain_id: entry.asset.chain_id, timestamp: isoFromSeconds(at.toString()), raw_balance: entry.balance.toString()});
        }
        if (previousNav > 0n) { returnFactor = returnFactor * before / previousNav; hasCapital = true; }
        flows += flow;
        inflows += moveIn;
        outflows += moveOut;
        points.push({as_of: isoFromSeconds(at.toString()), nav_usd_micros: nav.toString(), pnl_usd_micros: (nav - flows).toString(),
          net_external_flows_usd_micros: flows.toString(),
          external_inflows_usd_micros: inflows.toString(),
          external_outflows_usd_micros: outflows.toString(),
          yield_bps: rateKnown && rated > 0n ? (weighted / rated).toString() : null,
          // The return from the first point to this one, linked across every flow rather than
          // measured off the NAV: money paid in raises the value and is not a gain, and this
          // book took in more in a day than it has ever earned.
          return_bps: hasCapital ? ((returnFactor - FIXED_MATH_SCALE) * 10000n / FIXED_MATH_SCALE).toString() : null});
        if (nav > 0n && fundedAt === null) fundedAt = at;
        previousNav = nav;
      }
      const last = points[points.length - 1];
      // Both shapes of this record are read by the same cards, so a period means the same thing
      // in each: `pnl_usd_micros` is the money made over the window and `change_bps` the NAV
      // change across it. The declared branch published the second and this one did not, so a
      // connected wallet could never show a movement -- only the sentence explaining its absence.
      const periods = Object.fromEntries([7,30].map(days => {
        const boundary = end - BigInt(days) * 86400n;
        const point = points.find(p => isoSeconds(p.as_of) === boundary);
        const open = point === undefined ? null : BigInt(point.nav_usd_micros);
        // A weighted yield moves when the rates or the weights move, and both ends of the
        // window are needed to say by how much. In percentage points, which is what a change in
        // a percentage is.
        const openYield = point?.yield_bps ?? null, lastYield = last.yield_bps ?? null;
        // A window that opens at nothing has no percentage to state, however much was made in it.
        const covered = open !== null && open > 0n;
        return [`${days}D`, point ? {status:'COMPLETE', pnl_usd_micros:(BigInt(last.pnl_usd_micros)-BigInt(point.pnl_usd_micros)).toString(),
            covered, change_bps: covered ? ((BigInt(last.nav_usd_micros) - open) * 10000n / open).toString() : null,
            yield_change_bps: openYield !== null && lastYield !== null ? (BigInt(lastYield) - BigInt(openYield)).toString() : null,
            reason: covered ? null : `${days}D change is UNKNOWN: the window opens at a zero balance`}
          : {status:'UNKNOWN', pnl_usd_micros:null, covered:false, change_bps:null, yield_change_bps:null,
            reason:`History available since ${tracking.slice(0,10)}; ${days}D window is not covered`}];
      }));
      const daily = points.filter(point => isoSeconds(point.as_of) % 86400n === 0n);
      const changes = daily.slice(1).map((point,index) => BigInt(point.pnl_usd_micros) - BigInt(daily[index].pnl_usd_micros));
      const totalReturn = hasCapital ? (returnFactor - FIXED_MATH_SCALE) * 10000n / FIXED_MATH_SCALE : null;
      // Geometric annualization uses the existing fixed-point logarithm and exponential.
      // Use fixed log only after reducing its argument into [0.75, 1.5) for convergence.
      let annualized = null;
      if (hasCapital && fundedAt !== null && end - fundedAt >= SECONDS_PER_YEAR && returnFactor > 0n) {
        let factor = returnFactor, shifts = 0n;
        while (factor >= 3n * FIXED_MATH_SCALE / 2n) { factor /= 2n; shifts += 1n; }
        while (factor < 3n * FIXED_MATH_SCALE / 4n) { factor *= 2n; shifts -= 1n; }
        // log(2) via log(1.5) + log(4/3), avoiding the series endpoint at 1.
        const logTwo = fixedLogOnePlus(FIXED_MATH_SCALE / 2n) + fixedLogOnePlus(FIXED_MATH_SCALE / 3n);
        const logarithm = fixedLogOnePlus(factor - FIXED_MATH_SCALE) + shifts * logTwo;
        annualized = (fixedExp(logarithm * SECONDS_PER_YEAR / (end - fundedAt)) - FIXED_MATH_SCALE) * 10000n / FIXED_MATH_SCALE;
      }
      return {status:'COMPLETE', reason:null, source, points, balance_timeline:timeline, tracking_since:tracking,
        nav_usd_micros:last.nav_usd_micros, cumulative_pnl_usd_micros:last.pnl_usd_micros,
        net_external_flows_usd_micros:last.net_external_flows_usd_micros,
        external_inflows_usd_micros:last.external_inflows_usd_micros, external_outflows_usd_micros:last.external_outflows_usd_micros,
        total_return_bps:totalReturn?.toString() ?? null,
        annualized_return_bps:annualized?.toString() ?? null,
        annualized_reason:annualized === null ? `Annualized return requires at least 365 days of funded history. History available since ${tracking.slice(0,10)}` : null,
        best_day_usd_micros:changes.length ? changes.reduce((a,b)=>a>b?a:b).toString() : null,
        worst_day_usd_micros:changes.length ? changes.reduce((a,b)=>a<b?a:b).toString() : null, periods};
    } catch (_) { return unknown('Invalid history or finalized balance input; reconstruction is UNKNOWN'); }
  }

  function transferTopicAddress(topic) {
    if (typeof topic !== "string" || !/^0x0{24}[0-9a-fA-F]{40}$/.test(topic)) return null;
    return `0x${topic.slice(-40).toLowerCase()}`;
  }
  function transferUnsigned(value) {
    return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value) ? BigInt(value) : null;
  }
  function parseTransferLog(log, wallet, asset, chainId, timestamp) {
    const owner = address(wallet), token = address(asset?.address), chain = Number(chainId);
    const topics = log?.topics, from = Array.isArray(topics) ? transferTopicAddress(topics[1]) : null;
    const to = Array.isArray(topics) ? transferTopicAddress(topics[2]) : null;
    const amount = transferUnsigned(log?.data), block = rpcSeconds(log?.blockNumber), logIndex = rpcSeconds(log?.logIndex);
    const txHash = typeof log?.transactionHash === "string" && /^0x[0-9a-fA-F]{64}$/.test(log.transactionHash) ? log.transactionHash.toLowerCase() : null;
    const blockHash = typeof log?.blockHash === "string" && /^0x[0-9a-fA-F]{64}$/.test(log.blockHash) ? log.blockHash.toLowerCase() : null;
    const at = isoFromSeconds(timestamp);
    if (!owner || !token || !REGISTERED_CHAIN_IDS.has(chain) || Number(asset?.chain_id) !== chain || (log?.removed !== undefined && log.removed !== false) || address(log?.address) !== token
      || !Array.isArray(topics) || topics.length !== 3 || String(topics[0]).toLowerCase() !== TRANSFER_TOPIC
      || !from || !to || amount === null || block === null || logIndex === null || !txHash || !blockHash || !at) throw new Error("invalid ERC-20 Transfer log");
    if (from !== owner && to !== owner) throw new Error("ERC-20 Transfer log does not involve wallet");
    const decimals = Number(asset?.decimals), price = historicalTransferPrice(asset, timestamp);
    if (asset?.decimals == null || !Number.isInteger(decimals) || decimals < 0 || decimals > 255) throw new Error("asset decimals are invalid");
    const value = price === null ? null : (amount * BigInt(price) * 1000000n) / ((10n ** BigInt(decimals)) * DECLARED_PRICE_SCALE);
    const valueFor = (sign) => value === null ? null : `${sign < 0 ? "-" : ""}${value}`;
    const rawFor = (sign) => `${sign < 0 ? "-" : ""}${amount}`;
    const source = price === null
      ? "ERC-20 Transfer log; value unavailable under the historical price policy"
      : "ERC-20 Transfer log; value uses declared_demo synthetic terms, not a market observation";
    const notes = price === null ? "UNKNOWN · Historical price policy is not available for this asset" : "Declared demo value; not a market observation";
    const makeRow = (action, sign) => ({ id: `${chain}:${txHash}:${logIndex}:${owner}:${action.toLowerCase()}`, asset_address: token, from, to,
      asset: asset?.name || asset?.symbol || token, name: asset?.name || null, symbol: asset?.symbol || null,
      chain_id: chain, chain: CHAINS.find((row) => row.chain_id === chain)?.name || null, action, raw_amount: rawFor(sign), decimals,
      timestamp: at, value_usd_micros: valueFor(sign), source, tx_hash: txHash, status: "Finalized", notes,
      block_number: block.toString(), block_hash: blockHash, log_index: logIndex.toString() });
    if (from === owner && to === owner) return [makeRow("Outgoing", -1), makeRow("Incoming", 1)];
    return from === owner ? [makeRow("Outgoing", -1)] : [makeRow("Incoming", 1)];
  }
  function rowsFromHistoryEvents(events, wallet) {
    const owner = address(wallet);
    if (!owner || !Array.isArray(events)) return [];
    return events.flatMap((event) => {
      const chain = REGISTRY_CHAINS.find((row) => row.chain_id === event?.chain_id);
      const token = address(event?.token), counterparty = address(event?.counterparty);
      const hash = event?.transaction_hash;
      const block = nonNegativeBigInt(event?.block_number), index = nonNegativeBigInt(event?.log_index);
      if (!chain || !token || typeof hash !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(hash)
        || typeof event?.raw_amount !== "string" || !/^[0-9]+$/.test(event.raw_amount)
        || block === null || index === null || !["IN", "OUT", "SELF"].includes(event.direction)
        || (event.direction !== "SELF" && !counterparty)) return [];
      const txHash = hash.toLowerCase(), symbol = event.symbol || token;
      const from = event.direction === "IN" ? counterparty : owner;
      const to = event.direction === "OUT" ? counterparty : owner;
      const makeRow = (action) => ({
        id: `${chain.chain_id}:${txHash}:${index}:${owner}:${action.toLowerCase()}`, asset_address: token, from, to,
        asset: symbol, symbol, name: symbol, chain_id: chain.chain_id, chain: chain.name, action,
        raw_amount: `${action === "Outgoing" ? "-" : ""}${event.raw_amount}`, decimals: event.decimals ?? null,
        timestamp: event.occurred_at, value_usd_micros: null,
        source: event.verified === true ? "Blockscout index · confirmed against the chain" : "Blockscout index · not confirmed against the chain",
        tx_hash: txHash, status: event.verified === true ? "Verified" : "Unverified",
        notes: "Indexed history; not from this browser's scan", block_number: block.toString(), log_index: index.toString(), block_hash: null,
      });
      return event.direction === "SELF" ? [makeRow("Outgoing"), makeRow("Incoming")]
        : [makeRow(event.direction === "OUT" ? "Outgoing" : "Incoming")];
    });
  }
  function queryTransfers(rows, options) {
    const opts = options || {}, source = Array.isArray(rows) ? rows : [];
    const requestedAsset = opts.asset ?? opts.asset_address;
    const assetFilter = requestedAsset ? address(requestedAsset) : null;
    const from = opts.from ? isoSeconds(`${opts.from}T00:00:00Z`) : null;
    const to = opts.to ? isoSeconds(`${opts.to}T23:59:59Z`) : null;
    return source.filter((row) => {
      const timestamp = typeof row?.timestamp === "string" ? Date.parse(row.timestamp) : NaN;
      return (!requestedAsset || (assetFilter !== null && address(row?.asset_address) === assetFilter))
        && (!opts.action || String(row?.action) === String(opts.action))
        && (!opts.chain || String(row?.chain) === String(opts.chain) || String(row?.chain_id) === String(opts.chain))
        && (from === null || (Number.isFinite(timestamp) && BigInt(timestamp / 1000) >= from))
        && (to === null || (Number.isFinite(timestamp) && BigInt(timestamp / 1000) <= to));
    }).slice().sort((left, right) => {
      const parsedTimeLeft = typeof left?.timestamp === "string" ? Date.parse(left.timestamp) : NaN;
      const parsedTimeRight = typeof right?.timestamp === "string" ? Date.parse(right.timestamp) : NaN;
      const timeLeft = Number.isFinite(parsedTimeLeft) ? parsedTimeLeft : -Infinity;
      const timeRight = Number.isFinite(parsedTimeRight) ? parsedTimeRight : -Infinity;
      if (timeLeft !== timeRight) return timeLeft > timeRight ? -1 : 1;
      const parsedBlockLeft = nonNegativeBigInt(left?.block_number), parsedBlockRight = nonNegativeBigInt(right?.block_number);
      const parsedIndexLeft = nonNegativeBigInt(left?.log_index), parsedIndexRight = nonNegativeBigInt(right?.log_index);
      const blockLeft = parsedBlockLeft === null ? -1n : parsedBlockLeft, blockRight = parsedBlockRight === null ? -1n : parsedBlockRight;
      const indexLeft = parsedIndexLeft === null ? -1n : parsedIndexLeft, indexRight = parsedIndexRight === null ? -1n : parsedIndexRight;
      return blockLeft === blockRight ? (indexLeft === indexRight ? String(left?.id || "").localeCompare(String(right?.id || "")) : indexLeft > indexRight ? -1 : 1) : blockLeft > blockRight ? -1 : 1;
    });
  }
  function portfolioSortDays(row, asOf) {
    if (asOf !== undefined && asOf !== null && row?.maturity != null) return portfolioDateDays(row.maturity, asOf);
    return portfolioNumber(row?.days_to_maturity);
  }
  function compareNullable(left, right) {
    const leftUnknown = left === null || left === undefined, rightUnknown = right === null || right === undefined;
    if (leftUnknown || rightUnknown) return leftUnknown === rightUnknown ? 0 : leftUnknown ? 1 : -1;
    if (typeof left === "bigint" || typeof right === "bigint") return left < right ? -1 : left > right ? 1 : 0;
    return left < right ? -1 : left > right ? 1 : 0;
  }
  function compareSort(left, right, direction) {
    const leftUnknown = left === null || left === undefined, rightUnknown = right === null || right === undefined;
    if (leftUnknown || rightUnknown) return leftUnknown === rightUnknown ? 0 : leftUnknown ? 1 : -1;
    return compareNullable(left, right) * direction;
  }
  function weightedPortfolioYield(rows) {
    const source = Array.isArray(rows) ? rows : [];
    let total = 0n, weighted = 0;
    if (!source.length) return null;
    for (const row of source) {
      if (String(row?.status || "").toUpperCase() !== "ALIGNED") return null;
      const value = nonNegativeBigInt(row?.value_usd_micros), yieldBps = portfolioNumber(row?.yield_bps);
      if (value === null || yieldBps === null) return null;
      total += value; weighted += Number(value) * yieldBps;
    }
    return total > 0n ? weighted / Number(total) : null;
  }
  // Capital flows read as periods, not as marks: a wallet that took four transfers on one
  // afternoon produced four observations, and a bar apiece said four things about one
  // afternoon. Buckets are calendar units so the label names something a reader already has --
  // "Jun", not "observation 31" -- and the unit is the smallest one that keeps the bars
  // countable.
  //
  // Every figure here is a difference of two cumulative ones the record already carries, so the
  // buckets sum to the totals by construction. Nothing is apportioned and nothing is modelled:
  // a period the record does not cover is absent, never zero.
  const BUCKET_UNITS = ["day", "week", "month"];
  const MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function bucketKey(unit, iso) {
    const at = Date.parse(iso);
    if (!Number.isFinite(at)) return null;
    if (unit === "month") return iso.slice(0, 7);
    if (unit === "day") return iso.slice(0, 10);
    // Whole weeks counted back from the epoch, so the same instant always lands in the same
    // week whatever slice of history is being drawn.
    return `W${Math.floor(at / 604800000)}`;
  }
  function bucketLabel(unit, iso, withYear) {
    if (unit === "month") return `${MONTH_NAMES[Number(iso.slice(5, 7)) - 1] || iso.slice(5, 7)}${withYear ? ` '${iso.slice(2, 4)}` : ""}`;
    return iso.slice(5, 10);
  }
  function portfolioCashFlowBuckets(points, options = {}) {
    const baseline = options.baseline || null;
    const fail = reason => ({status: "UNKNOWN", reason, unit: null, buckets: []});
    if (!Array.isArray(points) || points.length < 2) return fail("At least two observations are needed to state a period");
    const fields = ["nav_usd_micros", "pnl_usd_micros", "net_external_flows_usd_micros",
      "external_inflows_usd_micros", "external_outflows_usd_micros"];
    const read = [];
    for (const point of points) {
      const at = String(point?.as_of || "");
      if (!Number.isFinite(Date.parse(at)) || at.length < 10) return fail("Observation timestamps are invalid");
      const values = {};
      for (const field of fields) {
        const value = asBigInt(point?.[field]);
        // A gross flow series the record never published is UNKNOWN, and an unknown inflow is
        // not a nought: drawing one would state that no money moved.
        if (value === null) return fail("Capital flows are UNKNOWN for this history");
        values[field] = value;
      }
      read.push({at, values});
    }
    if (read.some((point, index) => index && Date.parse(point.at) < Date.parse(read[index - 1].at))) return fail("Observations are out of order");
    const limit = Number.isInteger(options.maxBuckets) && options.maxBuckets > 0 ? options.maxBuckets : 14;
    const counted = unit => new Set(read.map(point => bucketKey(unit, point.at))).size;
    const unit = BUCKET_UNITS.find(candidate => counted(candidate) <= limit) || "month";
    const years = new Set(read.map(point => point.at.slice(0, 4)));
    const order = [];
    const groups = new Map();
    for (const point of read) {
      const key = bucketKey(unit, point.at);
      if (!groups.has(key)) { groups.set(key, []); order.push(key); }
      groups.get(key).push(point);
    }
    // Each bucket is what changed across it: the cumulative figure at its close less the one at
    // the close of the bucket before. The bucket before the first is the caller's baseline --
    // the mark the window opens on -- so a filtered range measures its first period from where
    // that range began. With no baseline the window opens at the start of history, where every
    // cumulative figure is nought and the wallet held none of these assets.
    const opening = {};
    for (const field of fields) opening[field] = baseline ? asBigInt(baseline[field]) : 0n;
    if (Object.values(opening).some(value => value === null)) return fail("The window's opening observation is incomplete");
    let previous = {values: opening};
    const buckets = order.map(key => {
      const members = groups.get(key);
      const close = members[members.length - 1];
      const since = field => close.values[field] - previous.values[field];
      const bucket = {
        key, unit, as_of: close.at, opened_at: members[0].at,
        opening_equity_usd_micros: previous.values.nav_usd_micros.toString(),
        label: bucketLabel(unit, members[0].at, years.size > 1),
        observations: members.length,
        inflows_usd_micros: since("external_inflows_usd_micros").toString(),
        outflows_usd_micros: since("external_outflows_usd_micros").toString(),
        net_cash_flow_usd_micros: since("net_external_flows_usd_micros").toString(),
        pnl_usd_micros: since("pnl_usd_micros").toString(),
        equity_usd_micros: close.values.nav_usd_micros.toString(),
      };
      previous = close;
      return bucket;
    });
    const last = read[read.length - 1].values;
    return {status: "COMPLETE", reason: null, unit, buckets,
      starting_equity_usd_micros: opening.nav_usd_micros.toString(),
      ending_equity_usd_micros: last.nav_usd_micros.toString(),
      inflows_usd_micros: (last.external_inflows_usd_micros - opening.external_inflows_usd_micros).toString(),
      outflows_usd_micros: (last.external_outflows_usd_micros - opening.external_outflows_usd_micros).toString(),
      net_cash_flow_usd_micros: (last.net_external_flows_usd_micros - opening.net_external_flows_usd_micros).toString(),
      pnl_usd_micros: (last.pnl_usd_micros - opening.pnl_usd_micros).toString()};
  }

  // What a wallet did, shaped for a table. Deliberately no value: the payload carries none, and
  // deriving one here would invent the very number the engine refuses to state.
  function walletHistoryView(data, requestedWallet = null) {
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("wallet history data must be an object");
    if (data.schema_version !== "fenorium/wallet-history/v1") throw new Error("wallet history schema mismatch");
    if (!address(data.wallet) || !Array.isArray(data.events) || !Array.isArray(data.errors)) {
      throw new Error("wallet history response has an invalid contract");
    }
    if (requestedWallet != null && (!address(requestedWallet) || data.wallet.toLowerCase() !== requestedWallet.toLowerCase())) {
      throw new Error("wallet history wallet does not match the requested wallet");
    }
    if (typeof data.complete !== "boolean") throw new Error("wallet history must state whether it is complete");
    const events = data.events.map((event, index) => {
      if (!event || typeof event !== "object" || Array.isArray(event)) throw new Error(`wallet history event ${index} is invalid`);
      if (!["IN", "OUT", "SELF"].includes(event.direction)) throw new Error(`wallet history event ${index} has no direction`);
      if (asBigInt(event.raw_amount) === null) throw new Error(`wallet history event ${index} has an invalid amount`);
      if (event.valued !== false) throw new Error("a wallet history event must not carry a value");
      return {
        identity: event.identity,
        occurred_at: event.occurred_at,
        direction: event.direction,
        symbol: typeof event.symbol === "string" && event.symbol ? event.symbol : null,
        token: event.token,
        raw_amount: String(event.raw_amount),
        decimals: event.decimals,
        block_number: event.block_number,
        log_index: event.log_index,
        amount: formatRawAmount(event.raw_amount, event.decimals),
        counterparty: event.counterparty,
        transaction_hash: event.transaction_hash,
        chain_id: event.chain_id,
        registered: typeof event.registered_identity === "string" ? event.registered_identity : null,
        verified: event.verified === true,
      };
    });
    const verified = events.filter((event) => event.verified).length;
    const recognised = events.filter((event) => event.registered).length;
    return {
      wallet: data.wallet,
      complete: data.complete,
      events,
      counts: { total: events.length, verified, recognised },
      // Every reason the history is short, in the engine's own words, so the table is never
      // read as the whole past when it is not.
      gaps: data.errors.map((error) => ({ chain_id: error.chain_id, reason: error.reason_code, detail: error.detail })),
    };
  }

  // Raw units into something readable, without a currency and without a float. Unknown decimals
  // stay raw rather than being guessed at, because a wrong scale is worse than an ugly one.
  function formatRawAmount(raw, decimals) {
    const value = asBigInt(raw);
    if (value === null) return String(raw);
    if (!Number.isSafeInteger(decimals) || decimals < 0 || decimals > 255) return value.toString();
    if (decimals === 0) return value.toString();
    const scale = 10n ** BigInt(decimals);
    const whole = value / scale;
    const fraction = (value % scale).toString().padStart(decimals, "0").replace(/0+$/, "");
    return fraction ? `${whole}.${fraction.slice(0, 6)}` : whole.toString();
  }


  return { STATUS, REASONS, CHAINS, REGISTRY_CHAINS, FEE_RECIPIENT, chainRole, PURCHASE_STAGES, COST_FIELDS, MARKET_CATEGORY_LABELS, PORTFOLIO_CLASS_LABELS, PORTFOLIO_CLASS_LABELS_ONE, assetClass, MARKET_DEFAULT_COLUMNS, positionIdentity, instrumentId, holdingPeriod, HOLDING_PERIOD_DAYS,
    MULTICALL3_ADDRESS, MULTICALL_CHUNK_SIZE, TRANSFER_TOPIC, clockNow, maturityDateMs, rowMatured, DEMO_PAIR_MATURITY,
    asBigInt, usdMicrosFromRaw, metricFailure, metricPass, marketCategory, marketFieldApplies, marketFieldState, marketVerdict,
    classifyPosition, coverageSummary, navLowerBound, allocation, concentration, deployableStablecoinCapital,
    validateCostLedger, mandateGates, expectedShortfall, portfolioExpectedShortfall: expectedShortfall,
    explicitSourceScenario, scenarioFromExplicitSource: explicitSourceScenario, orderOpportunities, rankOpportunities: orderOpportunities,
    createAssistantEnvelope, encodeBalanceOfCall, encodeAggregate3, decodeAggregate3,
    stableAssetIdentity, registeredAssetIdentity, registeredAssetsFromUniverse, priceObservationsFromUniverse,
    portfolioAnalyticsView, portfolioRows, maturityBucket, portfolioExposure, queryHoldings, weightedPortfolioYield,
    walletHistoryView, rowsFromHistoryEvents, formatRawAmount,
    derivePortfolioPerformance, portfolioCashFlowBuckets, transferScanDepth, historicalTransferPrice, registerDemoDeclarations, registerDeclaredSchedules, parseTransferLog, queryTransfers };
});
