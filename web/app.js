(function (root) {
  "use strict";

  const P = root.FenoriumPortfolio || {};
  const Prices = root.FenoriumPrices || {};
  const Routes = root.FenoriumRoutes || {};
  const U = root.FenoriumUI || {};
  const $ = U.$ || ((id) => typeof document === "undefined" ? null : document.getElementById(id));
  const PAGE_SIZE = 50;
  const SCHEMA = "fenorium/platform-response/1";
  const FRESHNESS = new Set(["FRESH", "STALE", "EXPIRED"]);
  const DEFAULT_NOTIONAL_USD = 250_000;
  const MIN_NOTIONAL_USD = 1;
  const MAX_NOTIONAL_USD = 1_000_000_000;
  const NOTIONAL_DEBOUNCE_MS = 450;
  // The server's wallet-read cache lifetime plus one second.
  const DEPOSIT_REFRESH_DELAY_MS = 61_000;
  const DEPOSIT_REFRESH_LIMIT = 20;
  // Idempotency namespace for this browsing session. The portfolio read generation counter
  // restarts from zero on every page load, so without this two tabs on the same wallet derive
  // identical keys for requests whose bodies genuinely differ -- they read at different blocks
  // and stamp different `as_of` -- and the server correctly refuses that as IDEMPOTENCY_CONFLICT.
  // A retry of the same logical request keeps this id and its generation, so it still dedupes;
  // only genuinely separate sessions diverge.
  const PORTFOLIO_SESSION_ID = (() => {
    const source = root.crypto;
    if (source && typeof source.randomUUID === "function") return source.randomUUID();
    if (source && typeof source.getRandomValues === "function") {
      return Array.from(source.getRandomValues(new Uint8Array(16)),
        (byte) => byte.toString(16).padStart(2, "0")).join("");
    }
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 14)}`;
  })();
  const FIXED_INCOME_FILTER = "fixed-income";
  const EXECUTION_COST_MODEL = "STATED_BASE_FEE_PLUS_SIZE_COST_PLUS_ROUTING_FEE";
  const RATING_METHOD_VERSION = "par-engine/2026.10.25-net-after-costs-v1";
  const NET_AFTER_COSTS = "NET_AFTER_COSTS";
  const WALLET_INTENT_KEY = "fenorium.wallet.connect-intent";
  // A finished scan is worth keeping between page loads: without it every reload re-read seven
  // hundred thousand blocks to reach the same answer, which is the half minute of UNKNOWN. It
  // stays in this browser -- it is this wallet's own public transfer history, derived here and
  // sent nowhere -- and is only ever a starting point: the blocks after it are always re-read,
  // and it is discarded outright if it does not match the wallet, the chain, or a block this
  // scan can extend.
  const TRANSFER_CACHE_KEY = "fenorium.transfers.v1";
  const TRANSFER_CACHE_LIMIT = 400000;
  const ASSISTANT_KEY = "fenorium.assistant.access-key";
  const chat = {status: null, key: safeStorageGet(ASSISTANT_KEY), turns: [], controller: null, serial: 0, paint: null};
  const PURCHASE_STAGES = P.PURCHASE_STAGES;
  const DISCONNECTED_ACCOUNT = "__disconnected__";
  const FALLBACK_DEFAULT_COLUMNS = Object.freeze({
    mixed: Object.freeze([
      "verdict", "instrument", "maturity", "headline", "net", "net_to_maturity", "execution", "depth", "tier",
    ]),
    native: Object.freeze([
      "verdict", "instrument", "maturity", "headline", "net", "net_to_maturity", "execution", "depth", "tier",
    ]),
    rwa: Object.freeze([
      "instrument", "headline", "net", "net_to_maturity", "execution", "tvl", "rate_kind", "permission",
    ]),
    vault: Object.freeze([
      "instrument", "headline", "net", "net_to_maturity", "execution", "depth", "tvl", "rate_kind",
    ]),
  });

  function defaultColumnKeys(category = "") {
    const defaults = P.MARKET_DEFAULT_COLUMNS || FALLBACK_DEFAULT_COLUMNS;
    const key = ["native", "rwa", "vault"].includes(String(category).toLowerCase())
      ? String(category).toLowerCase()
      : "mixed";
    return [...defaults[key]];
  }

  const BASE_SEPOLIA = Object.freeze({
    chainId: 84532,
    chainIdHex: "0x14a34",
    chainName: "Base Sepolia",
    nativeCurrency: Object.freeze({ name: "Ether", symbol: "ETH", decimals: 18 }),
    rpcUrl: "https://sepolia.base.org",
    explorerUrl: "https://sepolia.basescan.org",
  });

  const DEMO_INSTRUMENTS = Object.freeze({
    mGT: "0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54",
    mPT: "0x5C8Ea45363e98e8647eE3759ac888E12817E86a3",
  });

  function demoInstruments(config = state.config) {
    const catalog = isTestnetReady() ? testnetWorkspace()?.catalog?.instruments || [] : [];
    const pair = {...DEMO_INSTRUMENTS};
    for (const key of Object.keys(pair)) {
      const sale = config?.deployment?.sales?.[key] || config?.deployment?.sales?.[key.toLowerCase()];
      if (configuredAddress(sale?.instrument_address)) pair[key] = sale.instrument_address;
    }
    return {...Object.fromEntries(catalog.map((row) => [row.symbol, row.instrument])), ...pair};
  }

  function deploymentEndpoints(config = state.config) {
    const deployment = config?.deployment || {};
    return {
      rpcUrl: deployment.rpc_url || BASE_SEPOLIA.rpcUrl,
      explorerUrl: deployment.explorer_url === null ? null
        : deployment.explorer_url || deployment.explorerUrl || BASE_SEPOLIA.explorerUrl,
    };
  }

  function localRpcRefusal() {
    return {status: STATUS.WRONG_NETWORK_RPC, canPurchase: false, canSell: false, canFaucet: false,
      errorMessage: `Your wallet's Base Sepolia network points at the public testnet. Edit that network's RPC URL to ${deploymentEndpoints().rpcUrl} (chain id 84532) and reconnect.`};
  }

  // Until a local workspace's wallet has proved it reads the local chain, nothing is read through
  // it: the public testnet answers under the same chain id, and its empty history would be cached
  // as this chain's.
  function localRpcUnverified() {
    if (!testnetWorkspace()?.local || !state.provider || state.wallet.chainId !== BASE_SEPOLIA.chainId) return false;
    if (state.wallet.status === STATUS.WRONG_NETWORK_RPC) return true;
    // The answer belongs to the provider, not the account: a signer that disconnects leaves its
    // provider behind for a watched book, still on whichever network it was last found on.
    const verified = state.localRpcVerified;
    return !(verified && verified.provider === state.provider && verified.valid === true);
  }

  function verifyLocalWalletRpc(provider = state.provider) {
    if (!testnetWorkspace()?.local) return Promise.resolve(true);
    // One check per wallet and provider: the purchase panel asks beside the wallet change, and a
    // second check superseding the first would hand the first caller a false answer.
    const pending = state.localRpcPending;
    if (pending && pending.generation === state.walletGeneration && pending.provider === provider) return pending.promise;
    const entry = {generation: state.walletGeneration, provider, promise: null};
    entry.promise = readLocalWalletRpc(provider, entry.generation).finally(() => {
      if (state.localRpcPending === entry) state.localRpcPending = null;
    });
    state.localRpcPending = entry;
    return entry.promise;
  }

  async function readLocalWalletRpc(provider, generation) {
    const sequence = state.localRpcSequence = (state.localRpcSequence || 0) + 1;
    // Only an address the deployment data names: the built-in fallback is the public testnet's
    // own mock, whose code would pass this check on the very network it exists to refuse.
    const sale = state.config?.deployment?.sales?.mGT || state.config?.deployment?.sales?.mgt;
    let valid = false;
    if (configuredAddress(sale?.instrument_address)) {
      try {
        const code = await provider.request({method: "eth_getCode", params: [sale.instrument_address, "latest"]});
        valid = typeof code === "string" && /^0x[0-9a-fA-F]+$/.test(code);
      } catch (_) { /* Keep local actions blocked until the provider can prove the deployment. */ }
    }
    if (generation !== state.walletGeneration || sequence !== state.localRpcSequence) return false;
    if (state.wallet.chainId !== BASE_SEPOLIA.chainId) return false;
    state.localRpcVerified = {provider, valid};
    if (!state.wallet.account) return valid;
    state.wallet.status = valid ? STATUS.CONNECTED : STATUS.WRONG_NETWORK_RPC;
    state.wallet.message = valid ? `${walletChainLabel(state.wallet.chainId)} · connected` : localRpcRefusal().errorMessage;
    renderWallet();
    if (state.selected?.is_demo) renderSelectedAsset();
    return valid;
  }

  function registryUniverse() {
    const addresses = new Set((testnetWorkspace()?.catalog?.instruments || [])
      .map((row) => String(row.instrument).toLowerCase()));
    return state.universe.filter((row) => !(row.catalog === true || row.deployed === true)
      || addresses.has(String(row.address || "").toLowerCase()));
  }

  // A universe row without a symbol makes registeredAssetsFromUniverse fall back to the row's
  // name, and a name is not a ticker: the terminal then prints thirty characters of prose where
  // it wants four. demoInstruments() holds each demo contract's own symbol() and already supplies
  // it on the transfer path; the position path reads the same map so both agree. The universe
  // builder now emits the field, so this only covers an artifact published before that.
  function demoTicker(address) {
    const wanted = String(address || "").toLowerCase();
    return Object.entries(demoInstruments())
      .find(([, known]) => known.toLowerCase() === wanted)?.[0] || null;
  }

  function withDemoTicker(asset) {
    const ticker = demoTicker(asset?.address);
    return ticker && asset.symbol !== ticker ? {...asset, symbol: ticker} : asset;
  }

  const STATUS = Object.freeze({
    CONNECTED: "CONNECTED",
    NO_WALLET: "NO_WALLET",
    WRONG_CHAIN: "WRONG_CHAIN",
    WRONG_NETWORK_RPC: "WRONG_NETWORK_RPC",
    UNCONFIGURED_DEPLOYMENT: "UNCONFIGURED_DEPLOYMENT",
    INSUFFICIENT_MUSDC: "INSUFFICIENT_MUSDC",
    INSUFFICIENT_INSTRUMENT: "INSUFFICIENT_INSTRUMENT",
    UNVERIFIED_GATED: "UNVERIFIED_GATED",
    USER_REJECTED: "USER_REJECTED",
    PENDING: "PENDING",
    INDETERMINATE: "INDETERMINATE",
    RPC_ERROR: "RPC_ERROR",
    CONFIRMED_STATE_MISMATCH: "CONFIRMED_STATE_MISMATCH",
    REVERT: "REVERT",
    SUCCESS: "SUCCESS",
    INPUT_ERROR: "INPUT_ERROR",
    READY: "READY",
  });

  function amountInputErrorMessage(error) {
    if (error?.code === "EXCESS_PRECISION") {
      return "Enter an amount with fewer decimal places for this instrument.";
    }
    if (error?.code === "INVALID_AMOUNT") {
      return "Enter a valid instrument amount, such as 1 or 1.5.";
    }
    return "Enter a valid instrument amount.";
  }

  function insufficientFundsMessage(deployment) {
    return deployment?.faucetConfigured
      ? "Use the Base Sepolia testnet mUSDC faucet to cover the exact quote."
      : "Add enough testnet mUSDC to cover the exact quote.";
  }

  function insufficientInstrumentMessage(row) {
    return `Reduce the amount to at most the ${row?.symbol || "instrument"} balance this wallet holds.`;
  }

  function confirmedActionLabel(kind) {
    if (kind === "purchase") return "Purchase";
    if (kind === "sale") return "Sale";
    if (kind === "faucet") return "Testnet mUSDC funding";
    if (kind === "instrumentApproval") return "Instrument approval";
    return "Approval";
  }

  // Purchases, sales and faucet mints change balances; an approval only permits one of them.
  function isExecutionKind(kind) {
    return kind === "purchase" || kind === "faucet" || kind === "sale";
  }

  const TRANSACTION_STAGES = Object.freeze({
    approval: Object.freeze({
      pending: PURCHASE_STAGES.APPROVAL,
      receipt: PURCHASE_STAGES.APPROVAL_RECEIPT,
      indeterminate: PURCHASE_STAGES.APPROVAL_INDETERMINATE,
      complete: PURCHASE_STAGES.APPROVAL_COMPLETE,
      refreshFailed: PURCHASE_STAGES.APPROVAL_REFRESH_FAILED,
      reverted: PURCHASE_STAGES.APPROVAL,
    }),
    purchase: Object.freeze({
      pending: PURCHASE_STAGES.PURCHASE,
      receipt: PURCHASE_STAGES.PURCHASE_RECEIPT,
      indeterminate: PURCHASE_STAGES.PURCHASE_INDETERMINATE,
      complete: PURCHASE_STAGES.COMPLETE,
      refreshFailed: PURCHASE_STAGES.COMPLETE_REFRESH_FAILED,
      reverted: PURCHASE_STAGES.PURCHASE,
    }),
    faucet: Object.freeze({
      pending: PURCHASE_STAGES.FAUCET,
      receipt: PURCHASE_STAGES.FAUCET_RECEIPT,
      indeterminate: PURCHASE_STAGES.FAUCET_INDETERMINATE,
      complete: PURCHASE_STAGES.FAUCET_COMPLETE,
      refreshFailed: PURCHASE_STAGES.FAUCET_REFRESH_FAILED,
      reverted: PURCHASE_STAGES.FAUCET,
    }),
    instrumentApproval: Object.freeze({
      pending: PURCHASE_STAGES.SALE_APPROVAL,
      receipt: PURCHASE_STAGES.SALE_APPROVAL_RECEIPT,
      indeterminate: PURCHASE_STAGES.SALE_APPROVAL_INDETERMINATE,
      complete: PURCHASE_STAGES.SALE_APPROVAL_COMPLETE,
      refreshFailed: PURCHASE_STAGES.SALE_APPROVAL_REFRESH_FAILED,
      reverted: PURCHASE_STAGES.SALE_APPROVAL,
    }),
    sale: Object.freeze({
      pending: PURCHASE_STAGES.SALE,
      receipt: PURCHASE_STAGES.SALE_RECEIPT,
      indeterminate: PURCHASE_STAGES.SALE_INDETERMINATE,
      complete: PURCHASE_STAGES.SALE_COMPLETE,
      refreshFailed: PURCHASE_STAGES.SALE_COMPLETE_REFRESH_FAILED,
      reverted: PURCHASE_STAGES.SALE,
    }),
  });

  const SELECTORS = Object.freeze({
    "balanceOf(address)": "0x70a08231",
    "allowance(address,address)": "0xdd62ed3e",
    "approve(address,uint256)": "0x095ea7b3",
    "quote(uint256)": "0xed1bd76c",
    "purchase(uint256,uint256)": "0x70876c98",
    "sellQuote(uint256)": "0xd7a2e4c9",
    "sell(uint256,uint256)": "0xd79875eb",
    "isVerified(address)": "0xb9209e33",
    "mint(address,uint256)": "0x40c10f19",
    "decimals()": "0x313ce567",
  });

  const ERROR_DEFINITIONS = Object.freeze({
    "0xb12c8f91": { name: "NotVerified", types: ["address"], labels: ["wallet"] },
    "0x959c24e1": { name: "ClaimTooLarge", types: ["uint256", "uint256"], labels: ["amount", "cap"] },
    "0xcb883321": { name: "WrongSchema", types: ["bytes32", "bytes32"], labels: ["got", "want"] },
    "0xd68dc619": { name: "UntrustedAttester", types: ["address", "address"], labels: ["got", "want"] },
    "0x44e29e54": { name: "NotTheRecipient", types: ["address", "address"], labels: ["got", "caller"] },
    "0x671336ab": { name: "CredentialRevoked", types: ["uint64"], labels: ["revokedAt"] },
    "0x6ed5f9ff": { name: "CredentialExpired", types: ["uint64"], labels: ["expiredAt"] },
    "0xe6c4247b": { name: "InvalidAddress", types: [], labels: [] },
    "0xc251ac7c": { name: "InvalidTokenDecimals", types: ["uint8", "uint8"], labels: ["paymentDecimals", "instrumentDecimals"] },
    "0x1f2a2005": { name: "ZeroAmount", types: [], labels: [] },
    "0x27515afa": { name: "PriceNotSet", types: [], labels: [] },
    "0x4be126f4": { name: "PriceMovedAgainstBuyer", types: ["uint256", "uint256"], labels: ["quoted", "maxAccepted"] },
    "0x31078702": { name: "InsufficientPaymentBalance", types: ["uint256", "uint256"], labels: ["available", "required"] },
    "0xb2df58df": { name: "InsufficientPaymentAllowance", types: ["uint256", "uint256"], labels: ["available", "required"] },
    "0x1b2e0d06": { name: "InsufficientInstrumentBalance", types: ["uint256", "uint256"], labels: ["available", "required"] },
    "0xc995d35b": { name: "InsufficientInstrumentAllowance", types: ["uint256", "uint256"], labels: ["available", "required"] },
    "0x30dae5b7": { name: "PriceMovedAgainstSeller", types: ["uint256", "uint256"], labels: ["quoted", "minAccepted"] },
    "0xd9c80951": { name: "SettlementFailed", types: [], labels: [] },
    "0xa2ec2b64": { name: "FaucetFailed", types: [], labels: [] },
    "0x5f40c4c9": { name: "BuyerNotVerified", types: ["address"], labels: ["buyer"] },
    "0xb991d4a7": { name: "EligibilityCheckFailed", types: [], labels: [] },
    "0xf499da20": { name: "PaymentFailed", types: [], labels: [] },
    "0xc6137ab9": { name: "DeliveryFailed", types: [], labels: [] },
    "0xe450d38c": { name: "ERC20InsufficientBalance", types: ["address", "uint256", "uint256"], labels: ["sender", "balance", "needed"] },
    "0xfb8f41b2": { name: "ERC20InsufficientAllowance", types: ["address", "uint256", "uint256"], labels: ["spender", "allowance", "needed"] },
    "0xe2517d3f": { name: "AccessControlUnauthorizedAccount", types: ["address", "bytes32"], labels: ["account", "neededRole"] },
  });

  const state = {
    snapshot: null,
    history: null,
    historyError: null,
    historyGeneration: 0,
    sampleSnapshot: null,
    sampleVisible: false,
    sampleLoading: false,
    sampleError: null,
    universe: [],
    metadata: null,
    universeError: null,
    config: null,
    serverExecution: false,
    serverFees: false,
    // The testnet workspace (testnet.js, testnet image only) and the simulated portfolio chosen
    // on the Portfolio screen; the Market and the wallet never depend on that choice.
    testnetWorkspace: null,
    testnetScenarioId: null,
    testnetActivity: [],
    candidate: null,
    view: "market",
    selected: null,
    assetTab: "summary",
    page: 1,
    // The Score column opens sorted from the highest score down (owner 2026-10-20).
    sort: { key: "verdict", dir: -1 },
    filters: { category: FIXED_INCOME_FILTER },
    columns: new Set(defaultColumnKeys()),
    compareSelection: new Set(),
    compareOpen: false,
    compareMessage: "",
    notionalSize: DEFAULT_NOTIONAL_USD,
    requestedNotionalSize: DEFAULT_NOTIONAL_USD,
    notionalPending: false,
    notionalError: null,
    notionalRequestGeneration: 0,
    notionalDebounceTimer: null,
    provider: null,
    providerEventsBound: false,
    walletGeneration: 0,
    walletAccountGeneration: 0,
    walletChainGeneration: 0,
    portfolioTab: "summary",
    holdingsOptions: {search: "", sort: "value-desc", page: 1, pageSize: 10},
    exposureOptions: {dimension: "asset_type", unit: "percent"},
    portfolioReadGeneration: 0,
    connectedReadGeneration: 0,
    // One counter for the analytics idempotency key across both read scopes; separate counters
    // reused a key for two different requests and the platform refused the second (review cu).
    portfolioRequestSequence: 0,
    portfolioByAccount: Object.create(null),
    watchedWallets: [],
    viewedAccount: null,
    viewGeneration: 0,
    walletNotice: null,
    walletScreenError: null,
    walletScreenDraft: null,
    settings: {notional_usd: null, score_only: false},
    settingsError: null,
    // An amount typed in Settings and not yet saved; cleared only by a save or by forgetting.
    settingsDraft: null,
    // Set by "Forget this browser's data": no transfer scan starts on its own until the user asks
    // for one or the wallet changes, or the cache just forgotten would be written straight back.
    transferScanSuppressed: false,
    transferHistory: null,
    transferScanRequestedFor: null,
    transferOptions: {},
    transferGeneration: 0,
    wallet: {
      status: STATUS.NO_WALLET,
      account: null,
      chainId: null,
      chainName: null,
      connectIntent: false,
      message: "No injected wallet detected.",
    },
    purchaseByKey: Object.create(null),
    eventsBound: false,
  };

  function object(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  // The testnet workspace: the server's Base Sepolia instruments plus, from testnet.js, a
  // deployed catalog and simulated portfolios. The simulation is a choice on the
  // Portfolio screen, never a mode of the whole app: the Market always shows the complete
  // catalog, and the wallet connects whatever portfolio is on screen. A workspace whose
  // configuration is missing or invalid is blocked: nothing wallet-backed starts.
  function testnetWorkspace() {
    return state.testnetWorkspace?.enabled === true ? state.testnetWorkspace : null;
  }

  function isTestnetBlocked() {
    return testnetWorkspace()?.mode === "blocked";
  }

  function isTestnetReady() {
    const workspace = testnetWorkspace();
    return Boolean(workspace) && workspace.mode !== "blocked";
  }

  function testnetScenarios() {
    return testnetWorkspace()?.data?.scenarios || [];
  }

  function testnetScenario(id = state.testnetScenarioId) {
    return id ? testnetScenarios().find((row) => row.id === id) || null : null;
  }

  // A simulated portfolio is on screen. It rides the sample-book path, so every reader of
  // sampleVisible (scans, sales, exits) already treats it as a fixture and not as a wallet.
  function scenarioShowing() {
    return state.sampleVisible === true && testnetScenario() !== null;
  }

  function isSimulatedRow(row) {
    if (row?.deployed === true || row?.catalog === true
      || (isTestnetReady() && testnetWorkspace()?.catalog?.instruments?.some((entry) => sameAddress(row?.address, entry.instrument)))) return false;
    return row?.simulation === true;
  }

  function filterTestnetInstruments(rows) {
    return Array.isArray(rows) ? rows.filter((row) => Number(row?.chain_id) === BASE_SEPOLIA.chainId) : [];
  }

  // The Market's rows in the workspace: the on-chain Base Sepolia instruments first, then the
  // missing fixture catalog rows. Positions are reassigned because both sources
  // number their rows from zero and the table finds a row by its position.
  function testnetUniverse(serverRows, sizeUsd) {
    const catalog = testnetWorkspace()?.data?.universe?.instruments || [];
    const server = filterTestnetInstruments(serverRows);
    const addresses = new Set(server.map((row) => String(row.address || "").toLowerCase()).filter(Boolean));
    return [...server, ...catalog.filter((row) => !row.address || !addresses.has(row.address.toLowerCase()))
      .map((row) => fixtureRatingAt({...row, fixture: true, simulation: isSimulatedRow({...row, simulation: true})}, sizeUsd))]
      .map((row, index) => ({...row, _index: index}));
  }

  // Prefer the engine rating for the selected preset. Older fixtures still refuse a size
  // they were not rated at rather than showing figures computed for another notional.
  function fixtureRatingAt(row, sizeUsd) {
    const bySize = row.ratings_by_size?.[String(sizeUsd)];
    if (object(bySize)) return {...row, rating: bySize,
      decision_hash: row.decision_hashes_by_size?.[String(sizeUsd)] ?? null};
    const rating = row.rating;
    const ratedAt = object(rating) ? parseNotional(rating.size_usd) : null;
    if (ratedAt == null || ratedAt === sizeUsd) return row;
    return {...row, decision_hash: null, rating: {...rating, computable: false, net_bps: null, size_cost_bps: null,
      size_cost_annualized_bps: null, size_cost_basis: null, size_cost_rung_usd: null,
      size_cost_measured_at: null, base_fee_annualized_bps: null, routing_fee_annualized_bps: null,
      refusal: `preview fixture rated at ${formatNotional(ratedAt)} only; it is not re-rated at ${formatNotional(sizeUsd)}`}};
  }

  // The one place the universe arrives (first load and a notional change), with the size it
  // was rated at, so the venue positions are re-read against it every time.
  function applyUniverseRows(instruments, sizeUsd = state.notionalSize) {
    state.universe = isTestnetReady() ? testnetUniverse(instruments, sizeUsd) : instruments;
    void refreshVenuePositions();
  }

  // What a synthetic row shows: its fixture address is a contract nowhere, so the asset page
  // and the inspector get the row without one, and without an explorer to link to.
  function displayRow(row) {
    if (isSimulatedRow(row)) return {...row, address: null, explorer: null, explorer_url: null, is_demo: false};
    if (Number(row?.chain_id) === BASE_SEPOLIA.chainId && (row?.is_demo || isDeclaredDemoRow(row) || isExactDemoInstrument(row))) {
      const explorer = deploymentEndpoints().explorerUrl;
      return {...row, simulation: false, explorer, explorer_url: explorer};
    }
    return {...row, simulation: false};
  }

  // The provenance a synthetic row's page and inspector print: the fixture's own date and
  // source, no hash. The server universe's hash and timestamp belong to a document the row is
  // not in (review df).
  function fixtureMetadata() {
    const data = testnetWorkspace()?.data;
    const universe = object(data?.universe) ? data.universe : {};
    const generated = validTime(universe.generated_at) ? universe.generated_at
      : typeof data?.as_of === "string" ? `${data.as_of}T00:00:00Z` : null;
    return {
      ...(object(universe.metadata) ? universe.metadata : {}),
      generated_at: generated,
      as_of: generated,
      source: `testnet preview catalog · ${universe.source || "deterministic synthetic scenario universe"} · not a published universe`,
      hash: null,
      source_sha256: null,
      freshness: {status: "FIXTURE", age_seconds: null},
      input_freshness: null,
    };
  }

  // A row the page took from the fixture cites the fixture, even when it names a deployed token:
  // the platform's universe hash vouches only for the rows it served.
  function rowMetadata(row) {
    return isSimulatedRow(row) || row?.fixture === true ? fixtureMetadata() : state.metadata;
  }

  function selectTestnetScenario(scenarioId) {
    const scenario = testnetScenario(scenarioId) || testnetScenarios()[0] || null;
    if (!scenario) return false;
    state.testnetScenarioId = scenario.id;
    state.testnetActivity = Array.isArray(scenario.activity) ? scenario.activity : [];
    state.sampleSnapshot = {
      ...scenario.snapshot,
      provenance: {
        ...scenario.snapshot.provenance,
        kind: "LOCAL_DEMO_FIXTURE",
        scenario_kind: "SYNTHETIC_TESTNET_SCENARIO",
      },
    };
    state.sampleVisible = true;
    state.sampleLoading = false;
    state.sampleError = null;
    renderWalletSwitcher({screens: false});
    renderOverview();
    return true;
  }

  function clearTestnetScenario() {
    if (!state.testnetScenarioId) return false;
    state.testnetScenarioId = null;
    state.testnetActivity = [];
    state.sampleVisible = false;
    state.sampleSnapshot = null;
    return true;
  }

  // What the rail's Portfolio entry says it needs; painted by the wallet header and by the
  // Portfolio screen, since either a wallet change or a simulation choice can change it.
  function overviewNavText() {
    return isTestnetBlocked() ? "Workspace unavailable"
      : state.wallet.account ? "Wallet connected" : scenarioShowing() ? "Simulated portfolio" : "Needs wallet";
  }

  function mountTestnetToolbar() {
    const workspace = testnetWorkspace();
    if (!workspace) return;
    const onChain = (state.universe || []).filter((row) => !isSimulatedRow(row)).length;
    root.FenoriumTestnet?.mountToolbar?.(workspace, {onChain: isTestnetReady() && onChain ? onChain : null});
  }

  function validTime(value) {
    return typeof value === "string" && !Number.isNaN(Date.parse(value));
  }

  function makeCodeError(code, message, details = {}) {
    const error = new Error(message);
    error.code = code;
    Object.assign(error, details);
    return error;
  }

  function configuredAddress(value) {
    return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value) && !/^0x0{40}$/i.test(value);
  }

  function sameAddress(left, right) {
    return configuredAddress(left) && configuredAddress(right) && left.toLowerCase() === right.toLowerCase();
  }

  function normalizeChainId(value) {
    try {
      if (typeof value === "number" && Number.isInteger(value) && value >= 0) return value;
      if (typeof value === "bigint" && value >= 0n) return Number(value);
      if (typeof value === "string" && /^(0x[0-9a-f]+|[0-9]+)$/i.test(value.trim())) return Number(BigInt(value));
    } catch (_) {
      return null;
    }
    return null;
  }

  // The chain registry (engine/registry/chains.json), carried by portfolio.js.
  const REGISTRY_CHAINS = Array.isArray(P.REGISTRY_CHAINS) ? P.REGISTRY_CHAINS : [];
  const chainIds = (role) => REGISTRY_CHAINS.filter((chain) => chain.roles.includes(role)).map((chain) => chain.chain_id);
  // The platform's index of chain 84532 is the public testnet's. In a local workspace that chain id
  // names the local node, whose history only the browser scan can read.
  function historyIndexed(chainId) {
    if (testnetWorkspace()?.local && chainId === BASE_SEPOLIA.chainId) return false;
    return (Array.isArray(P.REGISTRY_CHAINS) ? P.REGISTRY_CHAINS : []).some((chain) => chain.chain_id === chainId && chain.history_index);
  }
  // Chains whose registered instruments this terminal reads in a wallet. There it is connected for
  // reading; only the testnet demo's own purchase asks for Base Sepolia. A purchase can be quoted to
  // any chain the platform can buy on, which includes chains this terminal never reads a wallet on;
  // every other paying chain is pay-only here.
  const MAINNET_CHAIN_IDS = new Set(REGISTRY_CHAINS
    .filter((chain) => chain.roles.includes("portfolio") && !chain.testnet).map((chain) => chain.chain_id));
  const BUY_CHAIN_IDS = new Set(chainIds("buy")
    .filter((id) => !REGISTRY_CHAINS.find((chain) => chain.chain_id === id).testnet));

  function onSupportedMainnet() {
    return Boolean(state.wallet.account) && (MAINNET_CHAIN_IDS.has(state.wallet.chainId)
      || Boolean(routeChainConfig(state.config)?.[state.wallet.chainId]));
  }

  function onPayOnlyChain() {
    return Boolean(state.wallet.account) && !MAINNET_CHAIN_IDS.has(state.wallet.chainId)
      && Boolean(routeChainConfig(state.config)?.[state.wallet.chainId]);
  }

  // A switch to the testnet that the wallet refused is a switch the user asked for. Only then does
  // the wallet button retry it; a mainnet wallet that simply connected is left where it is.
  function switchOutstanding() {
    return state.wallet.errorCode != null || Boolean(state.wallet.errorMessage);
  }

  // A chain the wallet can be on and Fenorium can work with: the registry's portfolio chains, the
  // served config's pay-only chains, and the Base Sepolia demo.
  function knownWalletChain(chainId) {
    if (isTestnetReady()) return chainId === BASE_SEPOLIA.chainId;
    return chainId === BASE_SEPOLIA.chainId || MAINNET_CHAIN_IDS.has(chainId)
      || Boolean(routeChainConfig(state.config)?.[chainId]);
  }

  function supportedWalletChains() {
    if (isTestnetReady()) return [{chainId: BASE_SEPOLIA.chainId, name: walletChainLabel(BASE_SEPOLIA.chainId), testnet: true}];
    const ids = new Set([...MAINNET_CHAIN_IDS, ...Object.keys(routeChainConfig(state.config) || {}).map(Number), BASE_SEPOLIA.chainId]);
    return [...ids].filter((id) => Number.isInteger(id) && id > 0)
      .map((id) => ({chainId: id, name: walletChainLabel(id), testnet: id === BASE_SEPOLIA.chainId}))
      .sort((a, b) => (a.testnet - b.testnet) || a.name.localeCompare(b.name));
  }

  function walletChainLabel(chainId) {
    return chainId === BASE_SEPOLIA.chainId ? testnetWorkspace()?.local ? "the local chain" : "Base Sepolia testnet" : routeChainName(chainId);
  }

  // What wallet_switchEthereumChain / wallet_addEthereumChain need for a chain, and how to name it in
  // a sentence; null when Fenorium knows nothing about the chain.
  function walletChainDetails(chainId) {
    if (chainId === BASE_SEPOLIA.chainId) {
      const endpoints = deploymentEndpoints();
      return {chainId, hex: BASE_SEPOLIA.chainIdHex, name: BASE_SEPOLIA.chainName,
        spoken: testnetWorkspace()?.local ? "the local chain" : "the Base Sepolia testnet",
        native: BASE_SEPOLIA.nativeCurrency, rpc: endpoints.rpcUrl, explorer: endpoints.explorerUrl};
    }
    const configured = routeChainConfig(state.config)?.[chainId];
    const registered = REGISTRY_CHAINS.find((chain) => chain.chain_id === chainId);
    if (!configured && !registered) return null;
    const native = configured?.native || registered?.native || null;
    return {chainId, hex: `0x${chainId.toString(16)}`, name: routeChainName(chainId), spoken: routeChainName(chainId),
      native: native ? {name: native.symbol, symbol: native.symbol, decimals: native.decimals} : null,
      rpc: configured?.rpc_url || registered?.rpc_url || null, explorer: registered?.explorer || null};
  }

  function chainName(chainId) {
    if (chainId === BASE_SEPOLIA.chainId) return BASE_SEPOLIA.chainName;
    return REGISTRY_CHAINS.find((chain) => chain.chain_id === chainId)?.name
      || (chainId == null ? null : `Chain ${chainId}`);
  }

  function truncateAddress(address) {
    return configuredAddress(address) ? `${address.slice(0, 6)}…${address.slice(-4)}` : "Connect";
  }

  // Watched wallets (owner 2026-10-19): any address can be added to the list and viewed in the
  // Portfolio screen. Buying, selling and exits always use the account the browser
  // extension has active, so a watched wallet's book is read-only until it is that account.
  const WATCHED_WALLETS_KEY = "fenorium.watched-wallets.v1";
  const WATCHED_WALLETS_MAX = 20;
  const WATCHED_LABEL_MAX = 32;
  // A watched wallet is read through the platform on every chain; this chain only names the block
  // its P&L would anchor to while no mainnet wallet is connected to take the chain from.
  const VIEW_CHAIN_ID = 8453;

  function watchedLabel(label) {
    return String(label == null ? "" : label).replace(/\s+/g, " ").trim().slice(0, WATCHED_LABEL_MAX);
  }

  function loadWatchedWallets() {
    let parsed = [];
    try { parsed = JSON.parse(safeStorageGet(WATCHED_WALLETS_KEY) || "[]"); } catch (_) { parsed = []; }
    const seen = new Set();
    state.watchedWallets = (Array.isArray(parsed) ? parsed : []).flatMap((entry) => {
      const address = String(entry?.address || "").toLowerCase();
      if (!configuredAddress(address) || seen.has(address)) return [];
      seen.add(address);
      return [{address, label: watchedLabel(entry.label)}];
    }).slice(0, WATCHED_WALLETS_MAX);
    if (state.viewedAccount && !state.watchedWallets.some((wallet) => wallet.address === state.viewedAccount)) state.viewedAccount = null;
    return state.watchedWallets;
  }

  function saveWatchedWallets() {
    if (state.watchedWallets.length) safeStorageSet(WATCHED_WALLETS_KEY, JSON.stringify(state.watchedWallets));
    else safeStorageRemove(WATCHED_WALLETS_KEY);
  }

  function addWatchedWallet(address, label = "") {
    const normalized = String(address || "").trim().toLowerCase();
    if (!configuredAddress(normalized)) return {ok: false, error: "Enter a 0x address of 40 hexadecimal characters."};
    if (state.watchedWallets.some((wallet) => wallet.address === normalized)) return {ok: false, error: "That wallet is already on the list."};
    if (state.watchedWallets.length >= WATCHED_WALLETS_MAX) return {ok: false, error: `The list holds at most ${WATCHED_WALLETS_MAX} wallets.`};
    state.watchedWallets.push({address: normalized, label: watchedLabel(label)});
    saveWatchedWallets();
    return {ok: true, address: normalized};
  }

  function removeWatchedWallet(address) {
    const normalized = String(address || "").toLowerCase();
    const before = state.watchedWallets.length;
    state.watchedWallets = state.watchedWallets.filter((wallet) => wallet.address !== normalized);
    if (state.watchedWallets.length === before) return false;
    saveWatchedWallets();
    if (state.viewedAccount === normalized) setViewedAccount(null);
    else renderWalletSwitcher();
    return true;
  }

  // The wallet whose book the Portfolio screen shows: the connected account unless a
  // watched wallet is chosen, and choosing the connected account itself is the connected view.
  function viewedWallet() {
    const connected = String(state.wallet.account || "").toLowerCase();
    const chosen = state.viewedAccount && state.viewedAccount !== connected
      ? state.watchedWallets.find((wallet) => wallet.address === state.viewedAccount) || null : null;
    if (!chosen) return {account: state.wallet.account, chainId: state.wallet.chainId, connected: true, watched: null};
    // A watched book is read on the connected mainnet, else on Base; in the testnet workspace the
    // only chain there is Base Sepolia (owner 2026-10-21: a watched entry there pointed at mainnet).
    const chainId = isTestnetReady() ? BASE_SEPOLIA.chainId
      : MAINNET_CHAIN_IDS.has(state.wallet.chainId) ? state.wallet.chainId : VIEW_CHAIN_ID;
    return {account: chosen.address, chainId, connected: false, watched: chosen};
  }

  function viewedRecord() {
    const viewed = viewedWallet();
    return portfolioRecord(viewed.account, viewed.chainId);
  }

  function setViewedAccount(address) {
    const normalized = address ? String(address).toLowerCase() : null;
    const next = normalized && state.watchedWallets.some((wallet) => wallet.address === normalized) ? normalized : null;
    if (next === state.viewedAccount) { renderWalletSwitcher(); return; }
    state.viewedAccount = next;
    state.viewGeneration += 1;
    // The scan and the history belong to the wallet that was being viewed.
    state.transferGeneration += 1;
    state.transferHistory = null;
    state.transferScanRequestedFor = null;
    state.historyGeneration += 1;
    state.history = null;
    state.historyError = null;
    renderWalletSwitcher();
    // Repainted here as well as by the read: with nothing to read for the new view (no connected
    // account after the viewed wallet was removed) the read returns at once and would leave the
    // old book on screen.
    renderOverview();
    void refreshVenuePositions();
    void refreshLendingOrders();
    if (state.universe.length) void refreshPortfolio();
  }

  async function requestExtensionAccounts() {
    const provider = state.provider;
    if (!provider?.request) return;
    try {
      await provider.request({method: "wallet_requestPermissions", params: [{eth_accounts: {}}]});
      const accounts = await provider.request({method: "eth_accounts"});
      const listed = Array.isArray(accounts) ? accounts : [];
      const active = String(listed[0] || "").toLowerCase();
      let added = 0;
      for (const account of listed) {
        const address = String(account || "").toLowerCase();
        if (!configuredAddress(address) || address === active) continue;
        if (addWatchedWallet(address, "Extension account").ok) added += 1;
      }
      state.walletNotice = added
        ? `${added} account${added === 1 ? "" : "s"} from the extension added to the list.`
        : "The extension shares no account beyond the active one.";
    } catch (error) {
      state.walletNotice = error?.code === 4001
        ? "The wallet did not grant access to more accounts."
        : `The wallet could not share its accounts: ${boundedAdapterError(error)}`;
    }
    renderWalletSwitcher();
  }

  function renderWalletSwitcher({screens = true, keepDraft = true} = {}) {
    // Owner 2026-10-20: the wallet chips and the add controls left the Portfolio
    // screen; the header names the connected wallet and the Wallet screen holds the list, the add
    // form and "Open book". What stays is what neither says: that a watched book or a simulation is
    // on screen, and (testnet) the simulations to choose from.
    const hosts = ["wallet-switcher"].map((id) => $(id)).filter(Boolean);
    if (!hosts.length) return;
    const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
    const viewed = viewedWallet();
    const connected = state.wallet.account;
    const simulation = scenarioShowing() ? testnetScenario() : null;
    const simulations = testnetScenarios();
    const simulationGroup = simulations.length
      ? `<div class="wallet-switcher-row" role="group" aria-label="Simulated portfolio to view"><span class="wallet-switcher-label">Simulations</span>${simulations.map((scenario) => `<button type="button" class="wallet-chip" data-view-scenario="${escape(scenario.id)}" aria-pressed="${simulation?.id === scenario.id}" title="${escape(scenario.description || "")}">${escape(scenario.label || scenario.id)}</button>`).join("")}</div>`
      : "";
    const note = simulation
      ? `<p class="wallet-switcher-note">Viewing the <b>${escape(simulation.label || simulation.id)}</b> simulation: synthetic positions and history, not a wallet. ${connected
        ? `Your connected wallet (${escape(truncateAddress(connected))}) is untouched.`
        : "Connect a wallet to see real holdings."} <button type="button" class="ghost" data-leave-simulation>Back to my wallet</button></p>`
      : !viewed.connected
      ? `<p class="wallet-switcher-note">Viewing <b>${escape(viewed.watched.label || truncateAddress(viewed.account))}</b> read-only. ${connected
        ? `Buying, selling and exits use the wallet active in your extension (${escape(truncateAddress(connected))}); switch it to this account to act. <button type="button" class="ghost" data-view-wallet="">Back to the connected wallet</button>`
        : "Connect a wallet to buy, sell or exit."} <button type="button" class="ghost" data-tab="wallet">Wallet screen →</button></p>`
      : "";
    for (const host of hosts) host.innerHTML = `${simulationGroup}${note}`;
    if (screens) {
      renderWalletScreen();
      renderSettingsScreen({keepDraft});
    }
  }

  function relabelWatchedWallet(address, label) {
    const normalized = String(address || "").toLowerCase();
    const entry = state.watchedWallets.find((wallet) => wallet.address === normalized);
    if (!entry) return false;
    entry.label = watchedLabel(label);
    saveWatchedWallets();
    // The chips elsewhere take the new label; the Wallet screen's own row is left standing, because
    // the label's change event fires on blur, just before a click on the row's Open or Remove
    // button lands — a rebuilt row would swallow that click.
    renderWalletSwitcher({screens: false});
    return true;
  }

  // The Wallet screen (owner 2026-10-20): the account that signs, and the watched list's home.
  function renderWalletScreen() {
    const host = $("wallet-screen");
    if (!host) return;
    if (isTestnetBlocked()) {
      host.innerHTML = `<section class="panel testnet-wallet-state"><header><h2>Workspace unavailable</h2><span class="chip">BLOCKED</span></header><p>${U.esc(state.testnetWorkspace?.error || "No wallet or network requests were started.")}</p></section>`;
      return;
    }
    const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
    const connected = state.wallet.account;
    const rows = state.watchedWallets.map((wallet) => {
      const active = String(connected || "").toLowerCase() === wallet.address;
      const viewing = !active && state.viewedAccount === wallet.address;
      return `<tr data-watched="${escape(wallet.address)}"><td><input type="text" class="wallet-label" data-wallet-label="${escape(wallet.address)}" value="${escape(wallet.label)}" maxlength="${WATCHED_LABEL_MAX}" placeholder="Label" aria-label="Label for ${escape(wallet.address)}"></td>`
        + `<td class="mono">${escape(wallet.address)}</td><td>${active ? "Active in your extension" : viewing ? "Viewing" : "Watched"}</td>`
        + `<td><button type="button" class="ghost" data-wallet-open="${escape(wallet.address)}">Open book</button> <button type="button" class="ghost" data-wallet-remove="${escape(wallet.address)}">Remove</button></td></tr>`;
    }).join("");
    const draft = state.walletScreenDraft || {address: "", label: ""};
    host.innerHTML = `<section class="panel"><header><h2>Connected wallet</h2></header>
        <div class="kv"><span>Account</span><b>${connected ? `${escape(truncateAddress(connected))} · ${escape(walletChainLabel(state.wallet.chainId))}` : "No wallet connected"}</b></div>
        <div class="kv"><span>Status</span><b>${escape(state.wallet.message || "")}</b></div>
        <p class="cell-sub">Buying, selling and exits always use this account. ${connected ? "Change it in your extension and the page follows." : "Connect through the button in the header or below."}</p>
        ${connected ? "" : '<button type="button" class="primary-action" id="wallet-screen-connect">Connect wallet</button>'}
      </section>
      <section class="panel"><header><h2>Watched wallets</h2><span>${escape(String(state.watchedWallets.length))} of ${WATCHED_WALLETS_MAX}</span></header>
        <p class="cell-sub">Any address can be watched: the platform reads its book, shown read-only in Portfolio, with transfers in its Transactions tab. The list stays in this browser.</p>
        ${rows ? `<div class="table-scroll"><table class="wallet-table"><thead><tr><th>Label</th><th>Address</th><th>State</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="cell-sub">No watched wallets yet.</p>'}
        <div class="wallet-add-form wallet-screen-add">
          <label>Address <input id="wallet-screen-address" type="text" autocomplete="off" spellcheck="false" placeholder="0x…" value="${escape(draft.address)}"></label>
          <label>Label <input id="wallet-screen-label" type="text" maxlength="${WATCHED_LABEL_MAX}" placeholder="Treasury, cold wallet…" value="${escape(draft.label)}"></label>
          <button type="button" class="button" data-wallet-screen-add>Add</button>
          ${state.provider?.request ? '<button type="button" class="ghost" data-wallet-request>Add from extension</button>' : ""}
          ${state.walletScreenError ? `<p class="route-error" role="alert">${escape(state.walletScreenError)}</p>` : ""}
        </div>
        ${state.walletNotice ? `<p class="wallet-switcher-note" role="status">${escape(state.walletNotice)}</p>` : ""}
      </section>`;
  }

  function submitWalletScreenAdd() {
    const address = $("wallet-screen-address")?.value || "", label = $("wallet-screen-label")?.value || "";
    const added = addWatchedWallet(address, label);
    state.walletScreenError = added.ok ? null : added.error;
    // A rejected address stays in the form to be corrected; a successful add clears it.
    state.walletScreenDraft = added.ok ? null : {address, label};
    renderWalletSwitcher();
  }

  // Settings (owner 2026-10-20): browser-local preferences. Forgetting these does not remove the
  // platform's route and order evidence.
  const SETTINGS_KEY = "fenorium.settings.v1";

  function loadSettings() {
    let parsed = {};
    try { parsed = JSON.parse(safeStorageGet(SETTINGS_KEY) || "{}"); } catch (_) { parsed = {}; }
    const notional = parsed && parsed.notional_usd != null ? parseNotional(parsed.notional_usd) : null;
    state.settings = {notional_usd: notional ?? null, score_only: parsed?.score_only === true};
    return state.settings;
  }

  function saveSettings() {
    const stored = {notional_usd: state.settings.notional_usd, score_only: state.settings.score_only};
    if (stored.notional_usd == null && !stored.score_only) safeStorageRemove(SETTINGS_KEY);
    else safeStorageSet(SETTINGS_KEY, JSON.stringify(stored));
  }

  function updateSettings(patch) {
    state.settings = {...state.settings, ...patch};
    saveSettings();
    renderSettingsScreen();
  }

  // The market's verdict select is not rebuilt by renderMarket, so a preference that sets the
  // filter must set the control too, or the dropdown says one thing while the table shows another.
  function syncVerdictSelect() {
    const select = $("filter-verdict");
    if (select) select.value = state.filters.verdict || "";
  }

  function browserDataSummary() {
    let transferCaches = 0;
    try {
      const storage = root.localStorage;
      for (let index = 0; index < (storage?.length || 0); index += 1) {
        if (String(storage.key(index) || "").startsWith(`${TRANSFER_CACHE_KEY}:`)) transferCaches += 1;
      }
    } catch (_) { transferCaches = 0; }
    return {watched: state.watchedWallets.length, transferCaches,
      settings: state.settings.notional_usd != null || state.settings.score_only};
  }

  function forgetBrowserData() {
    try {
      const storage = root.localStorage;
      const keys = [];
      for (let index = 0; index < (storage?.length || 0); index += 1) keys.push(storage.key(index));
      for (const key of keys) if (typeof key === "string" && key.startsWith("fenorium.")) safeStorageRemove(key);
    } catch (_) { /* no storage: nothing was remembered */ }
    chat.key = null; stopAssistantChat(); chat.turns = [];
    if ($("assistant-question")) $("assistant-question").value = "";
    renderAssistantChat(); updateAssistantVisibility();
    state.watchedWallets = [];
    state.settings = {notional_usd: null, score_only: false};
    state.settingsError = null;
    state.walletNotice = null; state.walletScreenError = null; state.walletScreenDraft = null;
    // A scan in flight would write the cache it just lost when its RPC answers; it is superseded,
    // and the retained history goes with it.
    state.transferGeneration += 1;
    state.transferHistory = null;
    state.transferScanRequestedFor = null;
    state.historyGeneration += 1;
    state.history = null;
    state.historyError = null;
    state.transferScanSuppressed = true;
    if (state.viewedAccount) setViewedAccount(null);
    else {
      renderWalletSwitcher({keepDraft: false});
      renderOverview();
    }
    // Whichever branch repainted, the Settings form shows the cleared preference, not a draft.
    renderSettingsScreen({keepDraft: false});
  }

  function renderSettingsScreen({keepDraft = true} = {}) {
    const host = $("settings-screen");
    if (!host) return;
    const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
    const data = browserDataSummary();
    const notional = state.settings.notional_usd;
    // An amount typed but not yet saved survives any number of rebuilds (the score toggle rebuilds
    // the form): the draft is recorded as the user types, never inferred from the input, so an
    // untouched input is never mistaken for one and a preference loaded after the first paint shows.
    if (!keepDraft) state.settingsDraft = null;
    const shown = state.settingsDraft != null ? state.settingsDraft : notional == null ? "" : String(notional);
    const remembered = [`${data.watched} watched wallet${data.watched === 1 ? "" : "s"}`,
      `${data.transferCaches} transfer cache${data.transferCaches === 1 ? "" : "s"}`, ...(data.settings ? ["these settings"] : [])];
    host.innerHTML = `<section class="panel"><header><h2>Market defaults</h2></header>
        <div class="settings-row">
          <label>Default notional (USD)<input id="settings-notional" type="text" inputmode="numeric" value="${escape(shown)}" placeholder="${escape(String(state.notionalSize || DEFAULT_NOTIONAL_USD))}"></label>
          <button type="button" class="button secondary" id="settings-notional-apply">Save</button>
          <span class="cell-sub">${notional == null ? "Using the size the universe ships with; leave empty to keep that." : `The market opens at ${escape(formatNotional(notional))}; empty the field to use the shipped size.`}</span>
          ${state.settingsError ? `<p class="route-error" role="alert">${escape(state.settingsError)}</p>` : ""}
        </div>
        <label class="settings-check"><input id="settings-score-only" type="checkbox"${state.settings.score_only ? " checked" : ""}> Open the market showing only instruments scoring 60 or more</label>
      </section>
      <section class="panel"><header><h2>This browser's data</h2></header>
        <p class="cell-sub">This browser remembers ${escape(remembered.join(", "))}. Fenorium keeps no account or profile of you; routes you request and orders you sign are recorded on the platform as transaction evidence, and forgetting here removes only what this browser holds.</p>
        <button type="button" class="button secondary" id="forget-browser-data">Forget this browser's data</button>
      </section>
      ${chat.status?.access === "key" ? `<section class="panel"><header><h2>Assistant access key</h2></header><div class="settings-row"><label>Assistant access key<input id="assistant-access-key" type="password" autocomplete="off" value="${escape(chat.key || "")}"></label><button type="button" class="button secondary" id="assistant-key-save">Save</button><button type="button" class="button secondary" id="assistant-key-remove">Remove</button></div></section>` : ""}
      <section class="panel"><header><h2>About</h2></header>
        <div class="kv"><span>Build</span><b>${escape($("build-id")?.textContent || "Local build")}</b></div>
        <div class="kv"><span>Universe as of</span><b>${escape(validTime(state.metadata?.generated_at) ? new Date(state.metadata.generated_at).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "Not loaded")}</b></div>
        <div class="kv"><span>Chains the wallet can switch to</span><b>${escape(String(supportedWalletChains().length))}</b></div>
        <button type="button" class="ghost" data-utility="methodology">Universe status &amp; methodology</button>
      </section>`;
  }

  function safeStorageGet(key) {
    try {
      return root.localStorage?.getItem(key) || null;
    } catch (_) {
      return null;
    }
  }

  function safeStorageSet(key, value) {
    if (!value) return false;
    try {
      root.localStorage?.setItem(key, value);
      return true;
    } catch (_) {
      return false;
    }
  }

  function safeStorageRemove(key) {
    try {
      root.localStorage?.removeItem(key);
    } catch (_) {
      return false;
    }
    return true;
  }

  function validateUniverseEnvelope(envelope, requestedSize = null) {
    if (!object(envelope)) throw Error("universe response must be an object");
    if (!validPlatformEnvelope(envelope)) throw Error("universe response failed schema and execution/fee consistency checks");
    const data = envelope.data;
    if (!object(data)) throw Error("universe response data is missing or invalid");
    if (!Array.isArray(data.instruments) || !data.instruments.length) throw Error("universe response has no instruments");
    if (!data.instruments.every(object)) throw Error("universe instruments must be objects");
    if (!Number.isInteger(data.instrument_count) || data.instrument_count !== data.instruments.length) {
      throw Error("universe instrument_count does not match instruments");
    }
    if (!FRESHNESS.has(String(data.freshness).toUpperCase())) throw Error("universe freshness status is invalid");
    if (!validTime(data.generated_at)) throw Error("universe generated_at is missing or invalid");
    if (!Number.isInteger(data.age_seconds) || data.age_seconds < 0) throw Error("universe age_seconds is missing or invalid");
    if (!object(data.freshness_thresholds)) throw Error("universe freshness thresholds are missing or invalid");
    if (typeof data.source !== "string" || !/^[a-f0-9]{64}$/i.test(data.source_sha256 || "")) {
      throw Error("universe source metadata is invalid");
    }
    if (requestedSize != null) {
      if (data.size_usd !== requestedSize) throw Error("universe response size_usd does not match the requested size");
      if (!object(data.assumptions)
        || data.assumptions.size_usd !== requestedSize
        || data.assumptions.execution_cost_bps !== 30
        || !Number.isInteger(data.assumptions.routing_fee_bps)
        || data.assumptions.routing_fee_bps < 0
        || data.assumptions.cost_model !== NET_AFTER_COSTS
        || data.assumptions.impact_coefficient_bps !== 100
        || data.assumptions.impact_exponent !== "1/2"
        || data.assumptions.execution_cost_model !== EXECUTION_COST_MODEL) {
        throw Error("universe response did not echo the applied base-fee, size-cost and routing-fee assumptions");
      }
      if (!object(data.rating_method)
        || data.rating_method.method_version !== RATING_METHOD_VERSION
        || data.rating_method.model !== NET_AFTER_COSTS
        || data.rating_method.mode !== "FULL_RECOMPUTE"
        || !object(data.source_rating_method)) {
        throw Error("sized universe response does not identify one consistent rating method");
      }
      if (!data.instruments.every((row) => object(row.rating)
        && row.rating.method_version === RATING_METHOD_VERSION
        && row.rating.cost_model === NET_AFTER_COSTS)) {
        throw Error("sized universe response mixed rating methods");
      }
      if (!Number.isInteger(data.priceable_count) || data.priceable_count < 0 || data.priceable_count > data.instruments.length) {
        throw Error("universe response priceable_count is invalid");
      }
      if (!object(data.priceability_reference)
        || !Number.isInteger(data.priceability_reference.size_usd)
        || !Number.isInteger(data.priceability_reference.priceable_count)
        || !Number.isInteger(data.priceability_reference.delta)) {
        throw Error("universe response priceability reference is invalid");
      }
    }
    return {
      instruments: data.instruments,
      execution_enabled: envelope.execution_enabled === true,
      fees_enabled: envelope.fees_enabled === true,
      metadata: {
        ...data,
        freshness: { status: String(data.freshness).toUpperCase(), age_seconds: data.age_seconds },
        as_of: data.generated_at,
        hash: data.source_sha256,
        // Declared rows are restated per UTC day without a new hash; the day the ratings arrived
        // says whether they still match analytics read today.
        received_on: new Date(P.clockNow()).toISOString().slice(0, 10),
      },
    };
  }

  async function loadUniverse(sizeUsd = null) {
    // Init stops on a blocked workspace, but the notional controls stay bound; a preset must not
    // repopulate the market behind the block (review df).
    if (isTestnetBlocked()) {
      throw makeCodeError("TESTNET_WORKSPACE_UNAVAILABLE", "The testnet workspace is unavailable; no universe request was made.",
        {reason_code: "TESTNET_WORKSPACE_UNAVAILABLE"});
    }
    const path = sizeUsd == null ? "/v1/universe" : `/v1/universe?size_usd=${encodeURIComponent(sizeUsd)}`;
    const response = await root.fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
    let body = null;
    try {
      body = await response.json();
    } catch (_) {
      body = null;
    }
    if (!response.ok) {
      const error = Error(body?.message || `universe request failed (HTTP ${response.status})`);
      error.reason_code = body?.reason_code || body?.error || "UNIVERSE_REQUEST_FAILED";
      throw error;
    }
    if (!body) throw Error("universe response is not valid JSON");
    return validateUniverseEnvelope(body, sizeUsd);
  }

  // What this build was configured to accept. A server saying execution is on when the client
  // was not built for it is a mismatch, not a feature: the two have to be changed together.
  function expectsExecution() {
    return object(state.config) && state.config.execution_enabled === true;
  }

  function expectsFees() {
    return object(state.config) && state.config.fees_enabled === true;
  }

  // Mainnet purchases are described as available only when the server says it executes and this
  // build was configured for it: the same two keys signing needs.
  function mainnetExecutionOpen() {
    return state.serverExecution === true && expectsExecution();
  }

  function executionBoundary() {
    return mainnetExecutionOpen() ? "Mainnet purchases by route" : "Mainnet execution disabled";
  }

  function applyDeploymentState(result) {
    state.serverExecution = result?.execution_enabled === true;
    state.serverFees = result?.fees_enabled === true;
    U.setDeploymentState?.({ execution: mainnetExecutionOpen(), fees: mainnetExecutionOpen() && state.serverFees });
    const boundary = $("market-boundary");
    if (boundary) boundary.textContent = `Market remains usable without a wallet · ${executionBoundary()}`;
    const spoken = $("execution-boundary-sr");
    if (spoken) spoken.textContent = executionBoundary();
  }

  function validPlatformEnvelope(envelope) {
    return object(envelope) && typeof envelope.execution_enabled === "boolean"
      && (!envelope.execution_enabled || expectsExecution())
      && ((envelope.schema_version === SCHEMA && envelope.fees_enabled === false)
        || (envelope.schema_version === "fenorium/platform-response/2" && expectsFees()
          && envelope.fees_enabled === true));
  }

  function routeChainConfig(config) {
    const chains = config?.route_chains;
    if (!object(chains) || !Object.keys(chains).length) return null;
    const allowed = new Set(chainIds("pay").map(String));
    const symbol = value => typeof value === "string" && value.trim().length > 0 && value.length <= 12;
    const decimals = value => Number.isInteger(value) && value >= 0 && value <= 36;
    const https = value => {
      if (typeof value !== "string" || !/^https:\/\/[^\s/?#]+(?:[/?#][^\s]*)?$/.test(value)) return false;
      try { return !root.URL || new root.URL(value).protocol === "https:"; }
      catch (_) { return false; }
    };
    for (const [id, entry] of Object.entries(chains)) {
      if (!allowed.has(id) || !object(entry) || !https(entry.rpc_url)
        || typeof entry.name !== "string" || !entry.name.trim()
        || (entry.native != null && (!object(entry.native) || !symbol(entry.native.symbol) || !decimals(entry.native.decimals)))
        || (entry.explorer !== undefined && !https(entry.explorer))
        || (entry.tokens !== undefined && (!Array.isArray(entry.tokens) || entry.tokens.some(token =>
          !object(token) || !symbol(token.symbol) || typeof token.address !== "string"
          || !/^0x[0-9a-f]{40}$/i.test(token.address) || !decimals(token.decimals))))
        || (entry.pay_only !== undefined && typeof entry.pay_only !== "boolean")
        || (entry.pay_only === true && MAINNET_CHAIN_IDS.has(Number(id)))
        || (entry.offer_native !== undefined && typeof entry.offer_native !== "boolean")) return null;
    }
    return chains;
  }

  function routeChainName(id) {
    return routeChainConfig(state.config)?.[id]?.name || chainName(id);
  }

  // Deliberately read-only: these public endpoints never receive signing requests.
  function routeRpc(entry, fetcher, signal) {
    let id = 0;
    return {request: async ({method, params = []}) => {
      if (!["eth_chainId", "eth_getCode", "eth_getBlockByNumber", "eth_call", "eth_getBalance", "eth_getTransactionReceipt"].includes(method)) {
        throw new Error("Unsupported read-only RPC method.");
      }
      const requestId = ++id;
      const response = await fetcher(entry.rpc_url, {method: "POST", cache: "no-store", credentials: "omit",
        headers: {"Content-Type": "application/json"}, signal,
        body: JSON.stringify({jsonrpc: "2.0", id: requestId, method, params})});
      if (!response.ok) throw new Error("Balance RPC unavailable.");
      const body = await response.json();
      if (body?.error || body?.jsonrpc !== "2.0" || body.id !== requestId || !("result" in body)) throw new Error("Invalid RPC response.");
      return body.result;
    }};
  }

  function validateConfig(config) {
    if (!object(config)) throw Error("config response must be an object");
    if (typeof config.execution_enabled !== "boolean" || typeof config.fees_enabled !== "boolean") {
      throw Error("config must state execution and fees explicitly as booleans");
    }
    const deployment = object(config.deployment) ? config.deployment : {};
    if (deployment.chain_id != null && normalizeChainId(deployment.chain_id) !== BASE_SEPOLIA.chainId) {
      throw Error("demo deployment must target Base Sepolia");
    }
    if (deployment.rpc_url != null && (typeof deployment.rpc_url !== "string"
      || !/^(?:https:\/\/[^\s/?#]+|http:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?)(?:[/?#][^\s]*)?$/.test(deployment.rpc_url))) {
      throw Error("demo deployment RPC must use HTTPS or local HTTP");
    }
    return config.route_chains === undefined ? config : {...config, route_chains: routeChainConfig(config)};
  }

  async function loadConfig() {
    if (isTestnetBlocked()) throw makeCodeError("TESTNET_WORKSPACE_UNAVAILABLE", "The testnet workspace is unavailable; no config request was made.");
    const response = await root.fetch("config.json", { cache: "no-store", headers: { Accept: "application/json" } });
    let body = null;
    try {
      body = await response.json();
    } catch (_) {
      body = null;
    }
    if (!response.ok) throw makeCodeError("CONFIG_REQUEST_FAILED", `config request failed (HTTP ${response.status})`);
    state.config = validateConfig(body);
    P.registerDemoDeclarations(state.config.deployment?.declared);
    return state.config;
  }

  function demoKeyForAddress(address, config = state.config) {
    if (!configuredAddress(address)) return null;
    for (const [key, known] of Object.entries(demoInstruments(config))) {
      if (sameAddress(address, known)) return key;
    }
    return null;
  }

  // Purchasable: the configured pair plus the loaded testnet catalog, keyed by address.
  function isExactDemoInstrument(row) {
    return Number(row?.chain_id) === BASE_SEPOLIA.chainId && demoKeyForAddress(row?.address) !== null;
  }

  // Declared-demo priced: every Base Sepolia row whose price is a declaration rather than a
  // market observation, which is a wider set than the purchasable instruments and the right one for
  // any question about how those declared prices are allowed to age.
  function isDeclaredDemoRow(row) {
    return Number(row?.chain_id) === BASE_SEPOLIA.chainId && row?.rate_kind === "declared_demo"
      && configuredAddress(row?.address);
  }

  function deploymentInfo(row, config = state.config) {
    const key = demoKeyForAddress(row?.address, config);
    if (!key || Number(row?.chain_id) !== BASE_SEPOLIA.chainId) return null;
    const deployment = object(config?.deployment) ? config.deployment : {};
    const payment = deployment.payment_token || deployment.paymentToken || {};
    const sales = deployment.sales || {};
    const catalog = isTestnetReady() ? testnetWorkspace()?.catalog : null;
    const entry = !Object.hasOwn(DEMO_INSTRUMENTS, key)
      ? catalog?.instruments.find((item) => sameAddress(item.instrument, row.address)) : null;
    const sale = entry ? {...catalog, instrument_address: entry.instrument, sale_address: entry.desk, gated: entry.gated}
      : sales[key] || sales[key.toLowerCase()] || {};
    const paymentAddress = configuredAddress(payment.address) ? payment.address : null;
    const saleAddress = configuredAddress(sale.sale_address || sale.saleAddress) ? (sale.sale_address || sale.saleAddress) : null;
    const instrumentAddress = sale.instrument_address || sale.instrumentAddress;
    const instrumentMatches = sameAddress(instrumentAddress, demoInstruments(config)[key]);
    const paymentDecimals = Number(payment.decimals == null ? 6 : payment.decimals);
    const instrumentDecimals = entry ? 6 : Number(row.decimals == null ? 6 : row.decimals);
    const faucetAddress = configuredAddress(payment.faucet_address || payment.faucetAddress)
      ? (payment.faucet_address || payment.faucetAddress)
      : null;
    const faucetMethod = payment.faucet_method || payment.faucetMethod || null;
    const faucetConfigured = Boolean(faucetAddress && faucetMethod === "mint(address,uint256)");
    const configured = Boolean(paymentAddress && saleAddress && instrumentMatches && paymentDecimals === 6 && instrumentDecimals === 6
      && (!entry || sameAddress(paymentAddress, catalog.payment_token)));
    // A desk quotes and takes the instrument back; a buy-only sale declares neither signature.
    const sellQuoteSignature = sale.sell_quote_signature || sale.sellQuoteSignature || null;
    const sellSignature = sale.sell_signature || sale.sellSignature || null;
    const declaredMaturity = Object.hasOwn(DEMO_INSTRUMENTS, key) ? P.DEMO_PAIR_MATURITY : entry?.maturity ?? null;
    return {
      key,
      maturity: declaredMaturity,
      matured: P.rowMatured({maturity: declaredMaturity}),
      instrumentAddress: demoInstruments(config)[key],
      paymentAddress,
      saleAddress,
      faucetAddress,
      faucetMethod,
      faucetConfigured,
      paymentDecimals,
      instrumentDecimals,
      gated: sale.gated === true || key === "mGT",
      quoteSignature: sale.quote_signature || sale.quoteSignature || "quote(uint256)",
      purchaseSignature: sale.purchase_signature || sale.purchaseSignature || "purchase(uint256,uint256)",
      sellQuoteSignature,
      sellSignature,
      sellConfigured: Boolean(configured && sellQuoteSignature && sellSignature),
      explorerUrl: deploymentEndpoints(config).explorerUrl,
      configured,
      instrumentMatches,
    };
  }

  function deploymentStatus(row, config = state.config) {
    const info = deploymentInfo(row, config);
    return info && info.configured ? STATUS.READY : STATUS.UNCONFIGURED_DEPLOYMENT;
  }

  function stripHex(value) {
    return String(value || "").replace(/^0x/i, "").toLowerCase();
  }

  function assertHex(value, label = "hex data") {
    const raw = stripHex(value);
    if (!raw || raw.length % 2 || !/^[0-9a-f]+$/.test(raw)) throw makeCodeError("INVALID_HEX", `${label} is invalid`);
    return raw;
  }

  function encodeUint256(value) {
    let amount;
    try {
      amount = BigInt(value);
    } catch (_) {
      throw makeCodeError("INVALID_UINT256", "uint256 value is invalid");
    }
    if (amount < 0n || amount > ((1n << 256n) - 1n)) throw makeCodeError("INVALID_UINT256", "uint256 value is out of range");
    return amount.toString(16).padStart(64, "0");
  }

  function encodeAddress(address) {
    if (!configuredAddress(address)) throw makeCodeError("INVALID_ADDRESS", "address is invalid");
    return `${"0".repeat(24)}${stripHex(address)}`;
  }

  function encodeBytes32(value) {
    const raw = assertHex(value, "bytes32 value");
    if (raw.length !== 64) throw makeCodeError("INVALID_BYTES32", "bytes32 value must be 32 bytes");
    return raw;
  }

  function selectorFor(signatureOrSelector) {
    const value = String(signatureOrSelector || "");
    if (/^0x[0-9a-fA-F]{8}$/.test(value)) return value.toLowerCase();
    if (SELECTORS[value]) return SELECTORS[value];
    throw makeCodeError("UNSUPPORTED_ABI", `ABI signature is not configured: ${value}`);
  }

  function signatureTypes(signature) {
    const match = String(signature || "").match(/^[^(]+\((.*)\)$/);
    if (!match) throw makeCodeError("UNSUPPORTED_ABI", `ABI signature is invalid: ${signature}`);
    return match[1].trim() ? match[1].split(",").map((type) => type.trim()) : [];
  }

  function encodeArgument(type, value) {
    if (type === "address") return encodeAddress(value);
    if (type === "bytes32") return encodeBytes32(value);
    if (/^uint(8|16|32|64|128|256)?$/.test(type)) return encodeUint256(value);
    if (type === "bool") return BigInt(value ? 1 : 0).toString(16).padStart(64, "0");
    throw makeCodeError("UNSUPPORTED_ABI", `ABI argument type is not supported: ${type}`);
  }

  function encodeFunctionCall(signatureOrSelector, args = []) {
    const selector = selectorFor(signatureOrSelector);
    const types = /^0x/.test(String(signatureOrSelector || "")) ? args.map((value) => configuredAddress(value) ? "address" : "uint256") : signatureTypes(signatureOrSelector);
    if (types.length !== args.length) throw makeCodeError("ABI_ARGUMENT_COUNT", "ABI argument count does not match signature");
    return `${selector}${types.map((type, index) => encodeArgument(type, args[index])).join("")}`;
  }

  function encodeBalanceOf(address) {
    return encodeFunctionCall("balanceOf(address)", [address]);
  }

  function encodeAllowance(owner, spender) {
    return encodeFunctionCall("allowance(address,address)", [owner, spender]);
  }

  function encodeApprove(spender, amount) {
    return encodeFunctionCall("approve(address,uint256)", [spender, amount]);
  }

  function encodeQuote(amount, signature = "quote(uint256)") {
    return encodeFunctionCall(signature, [amount]);
  }

  function encodePurchase(instrumentAmount, maxPaymentAmount, signature = "purchase(uint256,uint256)") {
    if (maxPaymentAmount == null) throw makeCodeError("MAX_PAYMENT_REQUIRED", "purchase requires the displayed maximum payment amount");
    if (!/^0x[0-9a-fA-F]{8}$/.test(String(signature)) && signatureTypes(signature).length !== 2) {
      throw makeCodeError("UNSUPPORTED_PURCHASE_ABI", "purchase ABI must accept instrument amount and maximum payment amount");
    }
    return encodeFunctionCall(signature, [instrumentAmount, maxPaymentAmount]);
  }

  function encodeSell(instrumentAmount, minPaymentAmount, signature = "sell(uint256,uint256)") {
    if (minPaymentAmount == null) throw makeCodeError("MIN_PAYMENT_REQUIRED", "sell requires the displayed minimum payment amount");
    if (!/^0x[0-9a-fA-F]{8}$/.test(String(signature)) && signatureTypes(signature).length !== 2) {
      throw makeCodeError("UNSUPPORTED_SELL_ABI", "sell ABI must accept instrument amount and minimum payment amount");
    }
    return encodeFunctionCall(signature, [instrumentAmount, minPaymentAmount]);
  }

  function encodeIsVerified(address) {
    return encodeFunctionCall("isVerified(address)", [address]);
  }

  function encodeMint(address, amount) {
    return encodeFunctionCall("mint(address,uint256)", [address, amount]);
  }

  function decodeWord(data, index = 0) {
    const raw = assertHex(data, "ABI response");
    const start = index * 64;
    const word = raw.slice(start, start + 64);
    if (word.length !== 64) throw makeCodeError("ABI_DECODE", "ABI response does not contain the requested word");
    return word;
  }

  function decodeUint256(data, index = 0) {
    return BigInt(`0x${decodeWord(data, index)}`);
  }

  function decodeAddress(data, index = 0) {
    return `0x${decodeWord(data, index).slice(-40)}`;
  }

  function decodeBool(data, index = 0) {
    return decodeUint256(data, index) !== 0n;
  }

  function decodeUtf8(hex) {
    const raw = assertHex(hex, "UTF-8 data");
    const bytes = [];
    for (let index = 0; index < raw.length; index += 2) bytes.push(parseInt(raw.slice(index, index + 2), 16));
    try {
      return new TextDecoder().decode(new Uint8Array(bytes));
    } catch (_) {
      return bytes.map((byte) => String.fromCharCode(byte)).join("");
    }
  }

  function extractErrorData(error, depth = 0) {
    if (!error || depth > 4) return null;
    if (typeof error === "string" && /^0x[0-9a-f]+$/i.test(error)) return error;
    if (typeof error === "object") {
      for (const key of ["data", "originalError", "cause", "error"]) {
        const value = error[key];
        const found = typeof value === "string" && /^0x[0-9a-f]+$/i.test(value)
          ? value
          : extractErrorData(value, depth + 1);
        if (found) return found;
      }
    }
    return null;
  }

  function decodeError(error, customDefinitions = {}) {
    const data = extractErrorData(error);
    if (!data || stripHex(data).length < 8) {
      return { kind: "UNKNOWN", name: "UnknownError", selector: null, args: [], message: error?.message || String(error || "Unknown RPC error") };
    }
    const raw = stripHex(data);
    const selector = `0x${raw.slice(0, 8)}`;
    const payload = raw.slice(8);
    if (selector === "0x08c379a0") {
      try {
        const offsetBytes = BigInt(`0x${payload.slice(0, 64)}`);
        if (offsetBytes > BigInt(Number.MAX_SAFE_INTEGER)) throw makeCodeError("ABI_DECODE", "Error(string) offset is too large");
        const offset = Number(offsetBytes) * 2;
        const length = Number(BigInt(`0x${payload.slice(offset, offset + 64)}`));
        if (!Number.isSafeInteger(length) || length < 0) throw makeCodeError("ABI_DECODE", "Error(string) length is invalid");
        const message = decodeUtf8(payload.slice(offset + 64, offset + 64 + length * 2));
        return { kind: "STANDARD_ERROR", name: "Error", selector, args: [message], message: `Error(string): ${message}` };
      } catch (_) {
        return { kind: "STANDARD_ERROR", name: "Error", selector, args: [], message: "Error(string)" };
      }
    }
    if (selector === "0x4e487b71") {
      const code = payload.length >= 64 ? BigInt(`0x${payload.slice(0, 64)}`).toString() : "unknown";
      return { kind: "STANDARD_PANIC", name: "Panic", selector, args: [code], message: `Panic(uint256): ${code}` };
    }
    const supplied = customDefinitions[selector] || customDefinitions[selector.toLowerCase()];
    const definition = supplied || ERROR_DEFINITIONS[selector];
    if (!definition) return { kind: "CUSTOM_ERROR", name: "UnknownCustomError", selector, args: [], message: `Custom error ${selector}` };
    const args = (definition.types || []).map((type, index) => {
      try {
        if (type === "address") return decodeAddress(payload, index);
        if (type === "bytes32") return `0x${decodeWord(payload, index)}`;
        return decodeUint256(payload, index).toString();
      } catch (_) {
        return "<decode failed>";
      }
    });
    const named = args.map((value, index) => `${definition.labels?.[index] || `arg${index}`}: ${value}`).join(", ");
    return {
      kind: "CUSTOM_ERROR",
      name: definition.name,
      selector,
      args,
      message: `${definition.name}${named ? ` (${named})` : ""}`,
    };
  }

  function parseUnits(value, decimals = 6) {
    const input = String(value ?? "").trim();
    const places = Number(decimals);
    if (!Number.isInteger(places) || places < 0 || places > 77) throw makeCodeError("INVALID_DECIMALS", "decimal places are invalid");
    if (!/^\+?(?:\d+)(?:\.\d*)?$/.test(input)) throw makeCodeError("INVALID_AMOUNT", "amount must be a decimal human-unit value");
    const unsigned = input.replace(/^\+/, "");
    const [wholePart, fractionPart = ""] = unsigned.split(".");
    if (fractionPart.length > places) throw makeCodeError("EXCESS_PRECISION", `amount has more than ${places} decimal places`);
    const whole = BigInt(wholePart || "0");
    const fraction = BigInt((fractionPart || "").padEnd(places, "0") || "0");
    const scale = 10n ** BigInt(places);
    return (whole * scale + fraction).toString();
  }

  function formatUnits(value, decimals = 6) {
    const amount = BigInt(value);
    const places = Number(decimals);
    const negative = amount < 0n;
    const absolute = negative ? -amount : amount;
    const scale = 10n ** BigInt(places);
    const whole = absolute / scale;
    const fraction = absolute % scale;
    if (!places) return `${negative ? "-" : ""}${whole}`;
    return `${negative ? "-" : ""}${whole}.${fraction.toString().padStart(places, "0")}`;
  }

  function transactionExplorerUrl(hash, explorerUrl = BASE_SEPOLIA.explorerUrl) {
    return hash && explorerUrl ? `${String(explorerUrl).replace(/\/$/, "")}/tx/${hash}` : null;
  }

  function receiptSucceeded(receipt) {
    return receipt?.status === "0x1" || receipt?.status === "0x01" || receipt?.status === 1 || receipt?.status === true;
  }

  function receiptReverted(receipt) {
    return receipt?.status === "0x0" || receipt?.status === "0x00" || receipt?.status === 0 || receipt?.status === false;
  }

  function wait(delayMs) {
    const timer = root.setTimeout || setTimeout;
    return new Promise((resolve) => timer(resolve, Math.max(0, Number(delayMs) || 0)));
  }

  async function waitForReceipt(provider, transactionHash, options = {}) {
    if (!provider?.request) throw makeCodeError("NO_PROVIDER", "injected provider is unavailable");
    if (!transactionHash) throw makeCodeError("NO_TRANSACTION_HASH", "transaction hash is missing");
    const intervalMs = options.intervalMs == null ? 1000 : Number(options.intervalMs);
    const maxPolls = options.maxPolls == null ? 60 : Number(options.maxPolls);
    for (let poll = 0; poll < maxPolls; poll += 1) {
      const receipt = await provider.request({ method: "eth_getTransactionReceipt", params: [transactionHash] });
      if (receipt) return receipt;
      if (poll + 1 < maxPolls) await wait(intervalMs);
    }
    throw makeCodeError("RECEIPT_TIMEOUT", "transaction receipt was not observed within the bounded polling window");
  }

  async function ethCall(provider, to, data) {
    if (!provider?.request) throw makeCodeError("NO_PROVIDER", "injected provider is unavailable");
    return provider.request({ method: "eth_call", params: [{ to, data }, "latest"] });
  }

  async function readUint(provider, to, data) {
    return decodeUint256(await ethCall(provider, to, data));
  }

  async function readDemoOnChain(provider, row, config = state.config, account = state.wallet.account, amountInput = "1") {
    const deployment = deploymentInfo(row, config);
    if (!deployment || !deployment.configured) return { status: STATUS.UNCONFIGURED_DEPLOYMENT, deployment };
    if (!configuredAddress(account)) return { status: STATUS.NO_WALLET, deployment };
    if (testnetWorkspace()?.local && !await verifyLocalWalletRpc(provider)) return {...localRpcRefusal(), deployment};
    let amountRaw;
    try {
      amountRaw = parseUnits(amountInput, deployment.instrumentDecimals);
    } catch (error) {
      return { status: STATUS.INPUT_ERROR, deployment, errorMessage: amountInputErrorMessage(error), errorCode: error.code };
    }
    try {
      const calls = [
        readUint(provider, deployment.paymentAddress, encodeBalanceOf(account)),
        readUint(provider, deployment.paymentAddress, encodeAllowance(account, deployment.saleAddress)),
        readUint(provider, deployment.instrumentAddress, encodeBalanceOf(account)),
        readUint(provider, deployment.saleAddress, encodeQuote(amountRaw, deployment.quoteSignature)),
      ];
      if (deployment.gated) calls.push(ethCall(provider, deployment.instrumentAddress, encodeIsVerified(account)).then(decodeBool));
      const sellOffset = calls.length;
      if (deployment.sellConfigured) {
        calls.push(
          readUint(provider, deployment.instrumentAddress, encodeAllowance(account, deployment.saleAddress)),
          readUint(provider, deployment.saleAddress, encodeQuote(amountRaw, deployment.sellQuoteSignature)),
        );
      }
      const values = await Promise.all(calls);
      return {
        status: STATUS.READY,
        deployment,
        amountRaw,
        paymentBalanceRaw: values[0].toString(),
        allowanceRaw: values[1].toString(),
        instrumentBalanceRaw: values[2].toString(),
        quoteRaw: values[3].toString(),
        verified: deployment.gated ? values[4] === true : null,
        instrumentAllowanceRaw: deployment.sellConfigured ? values[sellOffset].toString() : null,
        sellQuoteRaw: deployment.sellConfigured ? values[sellOffset + 1].toString() : null,
      };
    } catch (error) {
      const decoded = decodeError(error);
      return {
        status: STATUS.RPC_ERROR,
        deployment,
        errorCode: error.code || decoded.name,
        // quote() reverts ZeroAmount for an amount of 0, which parseUnits() accepts, and
        // PriceNotSet when the operator disables sales. Refreshing unchanged inputs resolves
        // neither, so the decoded cause has to survive.
        errorMessage: `On-chain reads could not be completed.${decoded.message ? ` The contract reported: ${decoded.message}` : ""}`,
      };
    }
  }

  function compareKey(row, index = 0) {
    const stableIndex = Number.isInteger(row?._index) ? row._index : index;
    const chainId = normalizeChainId(row?.chain_id);
    return `row-${chainId == null ? "unknown-chain" : chainId}-${stableIndex}`;
  }

  function normalized(row, index) {
    const value = row || {};
    return {
      ...value,
      _index: index,
      _key: compareKey(value, index),
      is_demo: isExactDemoInstrument(value),
      verdict: typeof P.marketVerdict === "function"
        ? P.marketVerdict(value)
        : (P.marketFieldApplies?.(value, "verdict") === false ? null : String(value.verdict || "UNRATED").toUpperCase()),
      _text: [value.name, value.venue, value.issuer, value.chain, value.category, value.rate_kind, value.tier, value.address, value.reason, value.rejection]
        .filter(Boolean)
        .join(" ")
        .toLowerCase(),
    };
  }

  function maturityBucket(row) {
    const days = row.days_to_maturity;
    if (days == null) return "Open / N/A";
    if (days < 90) return "<3M";
    if (days < 180) return "3–6M";
    if (days < 365) return "6–12M";
    if (days < 730) return "1–2Y";
    if (days < 1095) return "2–3Y";
    if (days < 1825) return "3–5Y";
    if (days < 3650) return "5–10Y";
    return "10Y+";
  }

  function sortValue(row, key) {
    // A column with its own basis sorts by that even when it is null: falling back to the row's
    // field turned an unscored row's verdict word into a value that sorted above every number.
    const values = {
      // The Score column sorts by the number it shows, not by the verdict word behind it: by the
      // word every BID row tied and the table kept its source order under a "Score" header
      // (owner 2026-10-20). A row whose cell reads N/A (the score does not apply to its
      // category) or UNKNOWN sorts last whichever way the column points (review dl).
      verdict: P.marketFieldApplies?.(row, "verdict") !== false && Number.isFinite(row.fenorium_score) ? row.fenorium_score : null,
      headline: row.rewards_only === true ? null : row.rate_bps,
      net: row.rating?.computable === true ? row.rating.net_bps : null,
      net_to_maturity: P.holdingPeriod?.(row)?.net_bps ?? null,
      execution: P.entryCost?.(row) ?? null,
      depth: row.depth_usd,
      tvl: row.tvl_usd,
      days: row.days_to_maturity,
      chain_id: row.chain_id,
    };
    return Object.hasOwn(values, key) ? values[key] : row[key];
  }

  function marketPopulation(universe, category = FIXED_INCOME_FILTER) {
    // The universe is every registered asset the terminal can scan, which now includes the
    // demo's own unit of account. The market is the investable subset: cash is what you buy
    // instruments with, not an instrument, and counting it broke the population arithmetic
    // these labels state -- fixed income plus vaults stopped summing to the total.
    // Settlement assets join the demo's unit of account in being registered but not investable:
    // WETH is not a stablecoin, so the stablecoin test alone would let it through and count cash
    // as an instrument -- the same arithmetic break, by a different door.
    const all = (universe || []).flatMap((row, index) => P.rowMatured(row) ? [] : [normalized(row, index)]).filter((row) => {
      const category = String(row.category).toLowerCase();
      return row.is_stablecoin !== true && category !== "stablecoin" && category !== "settlement";
    });
    const normalizedCategory = String(category || "").toLowerCase();
    const fixedIncome = all.filter((row) => ["native", "rwa"].includes(String(row.category).toLowerCase()));
    const vaults = all.filter((row) => String(row.category).toLowerCase() === "vault");
    let rows = all;
    let summary = `${all.length} instruments · ${fixedIncome.length} fixed-income instruments · ${vaults.length} variable-yield vaults`;
    let populationLabel = "total-universe instruments";
    let metricLabel = "Instruments";

    if (normalizedCategory === FIXED_INCOME_FILTER) {
      rows = fixedIncome;
      summary = `${fixedIncome.length} fixed-income instruments · ${vaults.length} variable-yield vaults available · ${all.length} total universe`;
      populationLabel = "fixed-income instruments";
      metricLabel = "Fixed-Income Instruments";
    } else if (normalizedCategory === "vault") {
      rows = vaults;
      summary = `${vaults.length} variable-yield vaults · ${fixedIncome.length} fixed-income instruments available · ${all.length} total universe`;
      populationLabel = "variable-yield vaults";
      metricLabel = "Variable-Yield Vaults";
    } else if (normalizedCategory === "native") {
      rows = all.filter((row) => String(row.category).toLowerCase() === "native");
      summary = `${rows.length} native fixed-maturity instruments · ${all.length} total universe`;
      populationLabel = "native fixed-maturity instruments";
      metricLabel = "Native Instruments";
    } else if (normalizedCategory === "rwa") {
      rows = all.filter((row) => String(row.category).toLowerCase() === "rwa");
      summary = `${rows.length} tokenized treasury and bond funds · ${all.length} total universe`;
      populationLabel = "tokenized treasury and bond funds";
      metricLabel = "RWA Fixed Income";
    }

    return {
      rows,
      total: all.length,
      fixedIncome: fixedIncome.length,
      vaults: vaults.length,
      summary,
      populationLabel,
      metricLabel,
    };
  }

  function filteredRows() {
    let rows = marketPopulation(state.universe, state.filters.category).rows;
    const filters = state.filters;
    if (filters.search) rows = rows.filter((row) => row._text.includes(filters.search));
    for (const key of ["verdict", "chain", "venue", "tier"]) {
      if (filters[key]) rows = rows.filter((row) => String(row[key] || "UNKNOWN") === filters[key]);
    }
    if (filters.maturity) rows = rows.filter((row) => maturityBucket(row) === filters.maturity);
    const direction = state.sort.dir;
    const key = state.sort.key;
    rows.sort((left, right) => {
      const a = sortValue(left, key);
      const b = sortValue(right, key);
      if (a == null && b == null) return left._index - right._index;
      if (a == null) return 1;
      if (b == null) return -1;
      const result = typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b));
      return result * direction || left._index - right._index;
    });
    return rows;
  }

  const columns = [
    ["verdict", "Fenorium Score"],
    ["instrument", "Instrument"],
    ["category", "Type"],
    ["venue", "Venue"],
    ["chain", "Chain"],
    ["maturity", "Maturity"],
    ["headline", "Headline Yield"],
    ["net", "Net APY @ selected size"],
    ["net_to_maturity", "Net to maturity @ selected size"],
    ["execution", "Entry cost @ selected size"],
    ["depth", "Reported depth · exitable or acquirable"],
    ["tvl", "Fund size (TVL) · reported"],
    ["tier", "Tier"],
    ["rate_kind", "Rate Kind"],
    ["days", "Days to Maturity"],
    ["rejection", "Rejection"],
    ["permission", "Permission"],
    ["address", "Contract Address"],
    ["chain_id", "Chain ID"],
    ["execution_mode", "Execution"],
  ];

  function formatNotional(value) {
    return `$${Number(value).toLocaleString("en-US")}`;
  }

  function columnLabel(key, label) {
    const sized = {net: "Net APY", net_to_maturity: "Net to maturity", execution: "Entry cost"};
    return sized[key] ? `${sized[key]} @ ${formatNotional(state.notionalSize)}` : label;
  }

  // Column headers: the short word, with the full qualifier on hover; the Columns menu and the
  // CSV keep the qualified names.
  const HEADER_LABELS = Object.freeze({
    verdict: "Score", category: "Type", headline: "Headline", net: "Net APY", net_to_maturity: "To maturity", execution: "Entry cost", depth: "Depth",
    tvl: "TVL", rate_kind: "Rate kind", days: "Days", execution_mode: "Execution", chain_id: "Chain ID",
    address: "Address",
  });

  function headerLabel(key, label) {
    return HEADER_LABELS[key] || columnLabel(key, label);
  }

  function compactUsd(value) {
    const number = Number(value);
    const abs = Math.abs(number);
    const fixed = (scaled, unit) => `$${scaled.toFixed(scaled >= 100 ? 0 : 1)}${unit}`;
    if (abs >= 1e9) return fixed(number / 1e9, "B");
    if (abs >= 1e6) return fixed(number / 1e6, "M");
    if (abs >= 1e4) return fixed(number / 1e3, "k");
    return `$${Math.round(number).toLocaleString("en-US")}`;
  }

  function parseNotional(value) {
    const text = String(value ?? "").trim().replaceAll(",", "");
    if (!/^[0-9]+$/.test(text)) return null;
    const parsed = Number(text);
    return Number.isSafeInteger(parsed) && parsed >= MIN_NOTIONAL_USD && parsed <= MAX_NOTIONAL_USD
      ? parsed
      : null;
  }

  function renderNotionalControl() {
    const input = $("market-notional");
    const status = $("notional-status");
    if (input && typeof document !== "undefined" && document.activeElement !== input) {
      input.value = String(state.requestedNotionalSize);
    }
    if (typeof document !== "undefined") {
      document.querySelectorAll("[data-notional]").forEach((button) => {
        const selected = Number(button.dataset.notional) === state.notionalSize && !state.notionalPending;
        button.setAttribute("aria-pressed", String(selected));
        button.disabled = state.notionalPending;
      });
    }
    if ($("apply-notional")) $("apply-notional").disabled = state.notionalPending;
    if (input) input.setAttribute("aria-busy", String(state.notionalPending));
    if (!status) return;
    status.classList.toggle("error", Boolean(state.notionalError));
    if (state.notionalPending) {
      status.textContent = `Recomputing at ${formatNotional(state.requestedNotionalSize)}… Counts and rows are pending.`;
      return;
    }
    if (state.notionalError) {
      const reasonCode = state.notionalError.reason_code || "UNKNOWN";
      const explained = typeof U.explain === "function" ? U.explain(reasonCode) : "";
      const reason = explained && explained !== reasonCode
        ? explained
        : state.notionalError.message || "The selected size could not be applied. Check the amount.";
      status.textContent = `${reason} Still showing the last confirmed ${formatNotional(state.notionalSize)} result.`;
      return;
    }
    const population = marketPopulation(state.universe, state.filters.category);
    const priceable = population.rows.filter((row) => row?.rating?.computable === true).length;
    status.textContent = `${priceable} of ${population.rows.length} ${population.populationLabel} priceable at ${formatNotional(state.notionalSize)}.`;
  }

  function beginNotionalIntent() {
    if (state.notionalDebounceTimer != null) {
      const clear = root.clearTimeout || (typeof clearTimeout === "function" ? clearTimeout : null);
      clear?.(state.notionalDebounceTimer);
    }
    state.notionalDebounceTimer = null;
    return ++state.notionalRequestGeneration;
  }

  async function applyNotional(value, scheduledGeneration = null) {
    const generation = scheduledGeneration == null ? beginNotionalIntent() : scheduledGeneration;
    if (generation !== state.notionalRequestGeneration) return null;
    const size = parseNotional(value);
    if (size == null) {
      state.notionalError = {
        reason_code: "INVALID_SIZE_USD",
        message: `Enter a whole-dollar amount from ${formatNotional(MIN_NOTIONAL_USD)} to ${formatNotional(MAX_NOTIONAL_USD)}.`,
      };
      state.notionalPending = false;
      renderNotionalControl();
      return null;
    }
    state.requestedNotionalSize = size;
    state.notionalError = null;
    if (size === state.notionalSize && state.metadata?.size_usd === size) {
      state.notionalPending = false;
      renderNotionalControl();
      return { instruments: state.universe, metadata: state.metadata };
    }
    state.notionalPending = true;
    renderMarket();
    try {
      const result = await loadUniverse(size);
      if (generation !== state.notionalRequestGeneration) return null;
      applyUniverseRows(result.instruments, size);
      applyDeploymentState(result);
      state.metadata = result.metadata;
      state.notionalSize = size;
      state.notionalPending = false;
      state.notionalError = null;
      state.universeError = null;
      state.page = 1;
      if (state.selected?._index != null && state.universe[state.selected._index]) {
        state.selected = normalized(state.universe[state.selected._index], state.selected._index);
        void refreshRouteQuotes();
      }
      renderMarket();
      if (state.view === "asset") renderSelectedAsset();
      return result;
    } catch (error) {
      if (generation !== state.notionalRequestGeneration) return null;
      state.notionalPending = false;
      state.notionalError = {
        reason_code: error?.reason_code || "UNIVERSE_REQUEST_FAILED",
        message: error?.message || String(error),
      };
      renderMarket();
      return null;
    }
  }

  function scheduleNotional(value) {
    const schedule = root.setTimeout || setTimeout;
    const generation = beginNotionalIntent();
    state.notionalDebounceTimer = schedule(() => {
      state.notionalDebounceTimer = null;
      if (generation !== state.notionalRequestGeneration) return;
      void applyNotional(value, generation);
    }, NOTIONAL_DEBOUNCE_MS);
  }

  function marketFieldState(row, key) {
    if (typeof P.marketFieldState === "function") return P.marketFieldState(row, key);
    const value = row?.[key];
    return value === null || value === undefined || value === ""
      ? { status: "UNKNOWN", reason: "NOT_MEASURED", value: null }
      : { status: "MEASURED", reason: null, value };
  }

  function columnCoverage(rows, key) {
    const selection = Array.isArray(rows) ? rows : [];
    const states = selection.map((row) => marketFieldState(row, key));
    const applicable = states.filter((field) => field.status !== "N/A").length;
    const measured = states.filter((field) => field.status === "MEASURED").length;
    const categories = [...new Set(selection.map((row) => String(row.category || "unknown").toLowerCase()))];
    let label;
    if (categories.length === 1) {
      label = applicable === 0
        ? `N/A for ${selection.length} ${categories[0]}`
        : `${measured} of ${applicable} ${categories[0]}`;
    } else {
      label = `${measured} measured · ${applicable} applicable of ${selection.length}`;
    }
    return { measured, applicable, total: selection.length, label };
  }

  function renderColumnsMenu(rows) {
    const menu = $("columns-menu");
    if (!menu) return;
    const allSelected = columns.every(([key]) => state.columns.has(key));
    menu.innerHTML = `<label class="columns-all"><input type="checkbox" data-columns-all ${allSelected ? "checked" : ""}> Select all</label><b>Columns · current selection coverage</b>${columns.map(([key, label]) => {
      const coverage = columnCoverage(rows, key);
      return `<label class="column-option"><input type="checkbox" data-column="${key}" ${state.columns.has(key) ? "checked" : ""}><span>${U.esc(columnLabel(key, label))}<small>${U.esc(coverage.label)}</small></span></label>`;
    }).join("")}<p class="cell-sub">Compare selection is always visible; unselecting all keeps Instrument. N/A means not applicable; UNKNOWN means applicable but not measured.</p>`;
  }

  function numericCell(row, key) {
    const field = marketFieldState(row, key);
    if (field.status !== "MEASURED") return field.status;
    if (["headline", "net", "net_to_maturity", "execution"].includes(key)) return U.pct(field.value);
    if (key === "depth" || key === "tvl") return `$${Math.round(Number(field.value)).toLocaleString("en-US")}`;
    if (key === "days") return Number(field.value).toFixed(1);
    return null;
  }

  function cell(row, key) {
    if (key === "headline" && U.rewardQualifier(row)) {
      const headline = U.rewardHeadline(row);
      if (headline) return headline;
      const field = marketFieldState(row, key);
      return (U.availability?.(field) || U.esc(field.status))
        + `<small class="cell-sub">${U.esc(U.rewardQualifier(row))}</small>`;
    }
    if (key === "net_to_maturity" && !P.marketFieldApplies(row, "maturity")) return '<span title="no maturity: held open-ended">—</span>';
    const field = marketFieldState(row, key);
    if (field.status !== "MEASURED") return U.availability?.(field) || U.esc(field.status);
    if (key === "verdict") return U.scoreChip(row, {compact: true});
    if (key === "instrument") {
      // One line of context under the name; the full reason and every source stay in the
      // inspector, one click away, instead of being repeated on every row.
      const category = String(row.category || "").toLowerCase();
      const type = category && category !== "native" ? P.MARKET_CATEGORY_LABELS?.[category] : null;
      const tag = U.reasonBasis?.(row.reason || row.rejection) || "";
      const context = [row.venue, row.chain, type].filter(Boolean).map((part) => U.esc(part)).join(" · ");
      return `<div class="instrument-cell">${U.identity?.("token", row.name, "") || ""}<div><div class="cell-title">${U.esc(row.name)}</div><small class="cell-sub">${context}${tag ? `<span class="reason-tag">${U.esc(tag)}</span>` : ""}</small></div></div>`;
    }
    if (key === "venue") return U.identity?.("venue", row.venue) || U.esc(row.venue);
    if (key === "chain") return U.identity?.("chain", row.chain) || U.esc(row.chain);
    if (key === "category") return U.typeIndicator?.(row.category) || U.esc(row.category);
    if (["headline", "net", "net_to_maturity", "execution", "depth", "tvl", "days"].includes(key)) {
      if (key === "depth" || key === "tvl") {
        const source = key === "depth" ? row.depth_source : row.tvl_source;
        return `<span title="${U.esc(`${numericCell(row, key)} · ${key === "depth" ? "Depth" : "TVL"} source: ${source || "not recorded"}`)}">${U.esc(compactUsd(field.value))}</span>`;
      }
      const value = U.esc(numericCell(row, key));
      if (["net", "net_to_maturity", "execution"].includes(key)) {
        const horizon = key === "net" ? "Annualized net" : key === "net_to_maturity"
          ? `To maturity · ${Number(P.holdingPeriod(row).days).toFixed(1)} days`
          : `One-off entry cost; annualized ${U.pct(row.rating.execution_bps)}`;
        return `<span title="${U.esc(`${horizon} · Entry costs · ${U.sizeCostContext?.(row) || "UNKNOWN · Not measured"}, not a live executable quote · depth source: ${row.depth_source || "not recorded"}`)}">${value}</span>`;
      }
      return value;
    }
    if (key === "maturity") return U.esc(row.maturity) + (row.days_to_maturity != null && Number.isFinite(Number(row.days_to_maturity)) ? `<small class="cell-sub">${Number(row.days_to_maturity).toFixed(1)} days</small>` : "");
    if (key === "rejection") {
      const code = marketFieldState(row, key).value;
      const label = code ? ({credit: "Credit", evidence: "Evidence", market_data: "Market data", size: "Size", none: "None"}[code] || String(code).replace(/_/g, " ")) : "None";
      return `<span title="${U.esc(row.reason || "")}">${U.esc(label)}</span>`;
    }
    if (key === "rate_kind") return U.esc(U.stateLabel(row.rate_kind));
    if (key === "tier") return U.esc(U.stateLabel?.(row.tier) || row.tier);
    if (key === "permission") return U.permissionCell(row.gate.status);
    if (key === "address") return U.esc(row.address);
    if (key === "execution_mode") return `<span class="execution-cell ${row.is_demo ? "demo" : mainnetExecutionOpen() ? "routable" : "read-only"}">${U.esc(row.is_demo ? "Base Sepolia demo purchase" : mainnetExecutionOpen() ? "Mainnet · route on request" : "Mainnet execution disabled")}</span>`;
    return U.esc(row[key] || "NOT COMPUTABLE");
  }

  function compareResult() {
    const rows = state.universe.map(normalized).filter((row) => state.compareSelection.has(row._key));
    rows.sort((left, right) => left._key.localeCompare(right._key));
    if (rows.length < 2) return { ok: false, rows: [], message: "Select 2–5 instruments. The comparison does not rank or choose a winner." };
    if (rows.length > 5) return { ok: false, rows: [], message: "Select no more than five instruments for a side-by-side comparison." };
    return { ok: true, rows, message: "" };
  }

  function renderCompareState() {
    const panel = $("compare-panel");
    if (panel) panel.hidden = !state.compareOpen;
    const count = $("compare-count");
    if (count) count.textContent = String(state.compareSelection.size);
    const label = $("compare-selection-label");
    if (label) label.textContent = state.compareMessage || (state.compareSelection.size >= 2 ? `${state.compareSelection.size} selected · selection persists across pages.` : "Select 2–5 instruments to compare. Selection persists across pages.");
    if (state.compareOpen && typeof U.renderCompare === "function") U.renderCompare(compareResult(), state.notionalSize);
  }

  function renderMarket() {
    if (typeof U.freshness === "function") U.freshness(state.metadata, state.universeError);
    renderNotionalControl();
    if (state.notionalPending) {
      const subtitle = $("market-subtitle");
      if (subtitle) subtitle.textContent = `Recomputing at ${formatNotional(state.requestedNotionalSize)} · ${executionBoundary()}`;
      const metrics = $("market-metrics");
      if (metrics) {
        metrics.setAttribute("aria-busy", "true");
        metrics.classList.add("market-recomputing");
        metrics.innerHTML = ["Instruments", "BID", "NO BID", "NO QUOTE", "UNRATED"]
          .map((label) => `<div class="metric-card"><div class="label">${U.esc(U.stateLabel?.(label) || label)}</div><div class="value">Calculating…</div><div class="detail">Re-rating every row</div></div>`)
          .join("");
      }
      for (const id of ["yield-chart", "verdict-chart", "depth-chart"]) {
        if ($(id)) $(id).innerHTML = '<div class="empty">RECOMPUTING FOR SELECTED SIZE…</div>';
      }
      if ($("fixed-income-table")) $("fixed-income-table").innerHTML = '<tr><td colspan="2" class="empty">Rows pending for the selected size.</td></tr>';
      if ($("market-range")) $("market-range").textContent = "Recomputing…";
      if ($("market-inspector")) $("market-inspector").hidden = true;
      if (state.compareOpen && $("compare-content")) $("compare-content").innerHTML = '<div class="empty">Comparison pending for the selected size.</div>';
      return;
    }
    if (state.universeError) {
      const metrics = $("market-metrics");
      const body = $("fixed-income-table");
      if (metrics) metrics.innerHTML = "";
      if (body) body.innerHTML = `<tr><td colspan="2" class="empty">${U.esc(U.stateText?.(state.universeError.reason_code || "UNIVERSE_REQUEST_FAILED") || state.universeError.message || "Market data unavailable.")}</td></tr>`;
      const range = $("market-range");
      if (range) range.textContent = U.stateLabel?.(state.universeError.reason_code || "UNIVERSE_REQUEST_FAILED") || "Market data unavailable";
      renderCompareState();
      return;
    }
    const population = marketPopulation(state.universe, state.filters.category);
    const all = population.rows;
    const total = all.length;
    const verdicts = ["BID", "NO BID", "NO QUOTE", "UNRATED"];
    const verdictTotal = all.filter((row) => row.verdict != null).length;
    const counts = Object.fromEntries(verdicts.map((value) => [value, all.filter((row) => row.verdict === value).length]));
    const subtitle = $("market-subtitle");
    if (subtitle) subtitle.textContent = population.summary;
    const metrics = $("market-metrics");
    if (metrics) {
      metrics.setAttribute("aria-busy", "false");
      metrics.classList.remove("market-recomputing");
      metrics.innerHTML = [
        ["Instruments", total, `${population.populationLabel} shown · ${population.total} total universe`],
        ...verdicts.map((value) => [U.stateLabel?.(value) || value, counts[value], `${verdictTotal ? (counts[value] / verdictTotal * 100).toFixed(1) : "0.0"}% of ${verdictTotal} applicable ${population.populationLabel}`]),
      ].map((metric) => `<div class="metric-card" title="${U.esc(metric[2])}"><div class="label">${U.esc(metric[0])}</div><div class="value">${U.esc(metric[1])}</div><div class="detail">${U.esc(metric[2])}</div></div>`).join("");
    }
    const charts = root.FenoriumCharts;
    if ($("yield-chart")) $("yield-chart").innerHTML = charts?.yieldByMaturity ? charts.yieldByMaturity(all) : "";
    if ($("verdict-chart")) $("verdict-chart").innerHTML = charts?.verdictDonut ? charts.verdictDonut(all) : "";
    if ($("depth-chart")) $("depth-chart").innerHTML = charts?.depthByChain ? charts.depthByChain(all) : "";

    const rows = filteredRows();
    renderColumnsMenu(rows);
    const visible = columns.filter(([key]) => state.columns.has(key));
    const head = $("market-head");
    if (head) {
      const defaults = defaultColumnKeys(state.filters.category);
      head.closest?.("table")?.classList.toggle("market-default-columns", visible.length === defaults.length && visible.every(([key]) => defaults.includes(key)));
      head.innerHTML = `<tr><th scope="col" class="compare-col"><span class="sr-only">Compare</span></th>${visible.map(([key, label]) => `<th scope="col" data-column="${key}" title="${U.esc(columnLabel(key, label))}"><button class="sort-action" type="button" data-sort="${key}">${U.esc(headerLabel(key, label))}${state.sort.key === key ? (state.sort.dir === 1 ? " ↑" : " ↓") : ""}</button></th>`).join("")}</tr>`;
    }
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    state.page = Math.min(state.page, pages);
    const start = (state.page - 1) * PAGE_SIZE;
    const page = rows.slice(start, start + PAGE_SIZE);
    const body = $("fixed-income-table");
    if (body) {
      body.innerHTML = page.map((row) => `<tr data-row="${row._index}" tabindex="0" aria-current="${state.selected?._index === row._index ? "true" : "false"}" class="${state.selected?._index === row._index ? "selected" : ""}">
        <td class="compare-cell"><input type="checkbox" data-compare="${U.esc(row._key)}" aria-label="Select ${U.esc(row.name)} for comparison" ${state.compareSelection.has(row._key) ? "checked" : ""}></td>
        ${visible.map(([key]) => `<td data-column="${key}" class="${["headline", "net", "net_to_maturity", "execution", "depth", "tvl", "days", "chain_id"].includes(key) ? "num" : ""}">${cell(row, key)}</td>`).join("")}
      </tr>`).join("") || `<tr><td colspan="${visible.length + 1}" class="empty">No instruments match these filters.</td></tr>`;
      // A keyboard move re-renders the table; focus follows the row that was moved to.
      if (state.focusRow != null) {
        body.querySelector?.(`tr[data-row="${state.focusRow}"]`)?.focus?.();
        state.focusRow = null;
      }
    }
    const range = $("market-range");
    if (range) range.textContent = rows.length ? `Showing ${start + 1}–${Math.min(start + PAGE_SIZE, rows.length)} of ${rows.length}` : "Showing 0 of 0";
    const pageElement = $("market-pages");
    if (pageElement) {
      const numbers = [...new Set([1, 2, 3, state.page - 1, state.page, state.page + 1, pages])]
        .filter((number) => number >= 1 && number <= pages)
        .sort((a, b) => a - b);
      let previous = 0;
      pageElement.innerHTML = numbers.map((number) => `${previous && number - previous > 1 ? "<span>…</span>" : ""}<button type="button" data-page="${number}" aria-current="${number === state.page ? "page" : "false"}">${previous = number}</button>`).join("");
    }
    if (state.selected && $("market-inspector") && typeof U.inspector === "function") {
      $("market-inspector").hidden = false;
      $("market-inspector").innerHTML = `${isSimulatedRow(state.selected) ? '<p class="chip testnet-asset-badge">SIMULATED · preview catalog</p>' : ""}${U.inspector(displayRow(state.selected), rowMetadata(state.selected), state.notionalSize)}`;
    }
    syncMarketControls();
    renderCompareState();
  }

  // The controls show their state: the active category tab, which filters are narrowing the
  // table (a chip lights up), and a way out of all of them at once.
  const NARROWING_FILTERS = ["verdict", "chain", "venue", "tier", "maturity"];

  function syncMarketControls() {
    if (typeof document === "undefined") return;
    document.querySelectorAll("#market-tabs [data-category]").forEach((tab) => {
      tab.setAttribute("aria-selected", String((tab.dataset.category || "") === String(state.filters.category || "")));
    });
    const toggle = $("score-toggle");
    if (toggle) toggle.setAttribute("aria-pressed", String(state.filters.verdict === "BID"));
    let narrowing = Boolean(state.filters.search);
    for (const key of NARROWING_FILTERS) {
      const select = $(`filter-${key}`);
      const chip = select?.closest?.(".filter-chip");
      if (chip) {
        chip.classList.toggle("active", Boolean(state.filters[key]));
        // A long value is clipped in the chip; the full one is on hover.
        chip.title = state.filters[key] ? (select.options?.[select.selectedIndex]?.text || state.filters[key]) : "";
      }
      if (state.filters[key]) narrowing = true;
    }
    const clear = $("clear-filters");
    if (clear) clear.hidden = !narrowing;
  }

  function clearMarketFilters() {
    for (const key of [...NARROWING_FILTERS, "search"]) state.filters[key] = "";
    for (const key of NARROWING_FILTERS) {
      const select = $(`filter-${key}`);
      if (select) select.value = "";
    }
    const search = $("market-search");
    if (search) search.value = "";
    state.page = 1;
    renderMarket();
  }

  function fillSelect(id, values) {
    const element = $(id);
    if (!element) return;
    const label = element.options?.[0]?.textContent || "Select";
    element.innerHTML = `<option value="">${U.esc(label)}</option>${[...new Set(values.filter((value) => value != null).map(String))].sort().map((value) => `<option value="${U.esc(value)}">${U.esc(["filter-verdict", "filter-tier"].includes(id) ? (U.stateLabel?.(value) || value) : value)}</option>`).join("")}`;
  }

  function setupMarket() {
    const rows = marketPopulation(state.universe, "").rows;
    fillSelect("filter-verdict", ["BID", "NO BID", "NO QUOTE", "UNRATED"]);
    fillSelect("filter-chain", rows.map((row) => row.chain));
    fillSelect("filter-venue", rows.map((row) => row.venue));
    fillSelect("filter-tier", rows.map((row) => row.tier || "UNKNOWN"));
    const tabs = $("market-tabs");
    if (tabs) {
      tabs.innerHTML = [
        [FIXED_INCOME_FILTER, "Fixed income"],
        ["native", "Native"],
        ["rwa", "RWA"],
        ["vault", "Vaults"],
        ["", "All"],
      ].map(([value, label]) => `<button type="button" role="tab" data-category="${U.esc(value)}" aria-selected="${String(value === state.filters.category)}">${U.esc(label)}</button>`).join("");
    }
    fillSelect("filter-maturity", rows.map(maturityBucket));
    // The top-bar selector describes the provider chain; market filtering has its own control.
    renderMarket();
  }

  function activate(view) {
    if (view === "activity") {
      state.portfolioTab = "transactions";
      view = "overview";
      renderOverview();
    }
    state.view = view;
    if (view === "wallet") renderWalletScreen();
    if (view === "settings") renderSettingsScreen();
    if (typeof document !== "undefined") {
      document.querySelectorAll("[data-view]").forEach((element) => element.classList.toggle("active", element.dataset.view === view));
      document.querySelectorAll("[data-tab]").forEach((button) => button.setAttribute("aria-current", button.dataset.tab === view ? "page" : "false"));
    }
    if (["overview", "market", "wallet", "settings"].includes(view)) root.history?.replaceState?.(null, "", `#${view}`);
    const main = $("app-main");
    main?.focus?.({ preventScroll: true });
  }

  function purchaseKey(row) {
    return demoKeyForAddress(row?.address);
  }

  function accountKey(account) {
    return configuredAddress(account) ? account.toLowerCase() : null;
  }

  function purchaseAccountKey(account) {
    return accountKey(account) || DISCONNECTED_ACCOUNT;
  }

  function ensurePurchaseState(row, account = state.wallet.account) {
    const key = purchaseKey(row);
    if (!key) return null;
    if (!state.purchaseByKey[key]) {
      state.purchaseByKey[key] = {
        key,
        accounts: Object.create(null),
      };
    }
    const scopedAccount = purchaseAccountKey(account);
    if (!state.purchaseByKey[key].accounts[scopedAccount]) {
      state.purchaseByKey[key].accounts[scopedAccount] = {
        key,
        account: accountKey(account),
        status: deploymentStatus(row),
        amountInput: "1",
        approvalTx: null,
        purchaseTx: null,
        faucetTx: null,
        instrumentApprovalTx: null,
        saleTx: null,
        unresolvedTransaction: null,
        requestGeneration: 0,
        actionSerial: 0,
        inFlightAction: null,
        confirmedOutcome: null,
        approvalReceiptOutcome: null,
        purchaseReceiptOutcome: null,
        faucetReceiptOutcome: null,
        instrumentApprovalReceiptOutcome: null,
        saleReceiptOutcome: null,
        errorMessage: "",
      };
    }
    return state.purchaseByKey[key].accounts[scopedAccount];
  }

  function purchaseView(row) {
    const current = ensurePurchaseState(row) || {};
    const deployment = deploymentInfo(row);
    const format = (value, decimals = 6) => value == null ? null : formatUnits(value, decimals);
    const readsCurrent = current.readsStale !== true;
    const wrongRpc = localRpcUnverified();
    const actionLocked = wrongRpc || current.inFlightAction != null || current.unresolvedTransaction != null;
    return {
      ...current,
      ...(wrongRpc ? localRpcRefusal() : {}),
      maturity: deployment?.maturity ?? null,
      matured: deployment?.matured === true,
      gated: deployment?.gated ?? row.gate?.status === "GATED",
      paymentTokenAddress: deployment?.paymentAddress || null,
      saleAddress: deployment?.saleAddress || null,
      faucetConfigured: deployment?.faucetConfigured === true,
      sellConfigured: deployment?.sellConfigured === true,
      needsChainSwitch: Boolean(state.wallet.account && state.wallet.chainId !== BASE_SEPOLIA.chainId),
      canPurchase: Boolean(current.canPurchase === true && !actionLocked && !deployment?.matured),
      canFaucet: Boolean(current.canFaucet === true && !actionLocked && !deployment?.matured && deployment?.faucetConfigured && state.wallet.account && state.wallet.chainId === BASE_SEPOLIA.chainId),
      canSell: Boolean(current.canSell === true && !actionLocked && deployment?.sellConfigured),
      explorerUrl: deployment ? deployment.explorerUrl : deploymentEndpoints().explorerUrl,
      quoteHuman: readsCurrent ? format(current.quoteRaw) : null,
      paymentBalanceHuman: readsCurrent ? format(current.paymentBalanceRaw) : null,
      allowanceHuman: readsCurrent ? format(current.allowanceRaw) : null,
      instrumentBalanceHuman: readsCurrent ? format(current.instrumentBalanceRaw) : null,
      sellQuoteHuman: readsCurrent ? format(current.sellQuoteRaw) : null,
      instrumentAllowanceHuman: readsCurrent ? format(current.instrumentAllowanceRaw) : null,
    };
  }

  function setPurchaseState(row, patch, account = state.wallet.account) {
    const current = ensurePurchaseState(row, account);
    if (!current) return null;
    Object.assign(current, patch);
    const activeAccount = purchaseAccountKey(state.wallet.account);
    if (state.selected
      && demoKeyForAddress(state.selected.address) === current.key
      && purchaseAccountKey(account) === activeAccount
      && typeof U.asset === "function") {
      // The same render as opening the page: a fixture fallback row keeps the fixture's provenance.
      renderSelectedAsset();
    }
    return { ...current };
  }

  function retainUnresolvedTransaction(row, transaction) {
    const account = accountKey(transaction?.account);
    if (!account) return null;
    return setPurchaseState(row, {
      unresolvedTransaction: { ...transaction, account },
      inFlightAction: null,
    }, account);
  }

  function ownsUnresolvedTransaction(row, account, hash) {
    return ensurePurchaseState(row, accountKey(account))?.unresolvedTransaction?.hash === hash;
  }

  function isLatestAction(row, capture) {
    return ensurePurchaseState(row, capture.account)?.actionSerial === capture.actionSerial;
  }

  function clearUnresolvedTransaction(row, account) {
    const current = ensurePurchaseState(row, account);
    if (!current?.unresolvedTransaction) return current ? { ...current } : null;
    return setPurchaseState(row, { unresolvedTransaction: null }, account);
  }

  const instrumentAnalyticsCache = new Map();
  let instrumentAnalyticsRequest = 0;
  let instrumentAnalyticsSelection = null;

  function normalizeAssetTab(tab) {
    return tab === "analytics" || tab === "maturity" ? "analytics" : "summary";
  }

  function instrumentAnalyticsKey(row) {
    return JSON.stringify([P.instrumentId(row), state.notionalSize, state.metadata?.source_sha256 || state.metadata?.hash || null,
      new Date(P.clockNow()).toISOString().slice(0, 10)]);
  }

  async function ensureInstrumentAnalytics(row = state.selected) {
    const id = P.instrumentId(row);
    if (!id || isSimulatedRow(row) || state.assetTab !== "analytics") return null;
    const key = instrumentAnalyticsKey(row);
    const cached = instrumentAnalyticsCache.get(key);
    // Price history is published after the universe and changes no hash, so a read older than this
    // is asked again at the next paint; nothing re-reads on a timer.
    if (cached && !(cached.status === "ready" && P.clockNow() - cached.fetchedAt > 10 * 60 * 1000)) return cached;
    const request = ++instrumentAnalyticsRequest;
    const sourceHash = state.metadata?.source_sha256 || state.metadata?.hash || null;
    const current = () => request === instrumentAnalyticsRequest
      && state.assetTab === "analytics" && P.instrumentId(state.selected) === id
      && instrumentAnalyticsKey(state.selected) === key && !isSimulatedRow(state.selected);
    const pending = {status: "loading"};
    instrumentAnalyticsCache.set(key, pending);
    try {
      const response = await root.fetch(`/v1/instrument-analytics?instrument_id=${encodeURIComponent(id)}&size_usd=${encodeURIComponent(state.notionalSize)}`, {
        cache: "no-store", headers: {Accept: "application/json"},
      });
      let envelope = null;
      try { envelope = await response.json(); } catch (_) { /* A missing endpoint may serve HTML. */ }
      if (!response.ok) throw Error(envelope?.message || `HTTP ${response.status}`);
      const data = envelope?.data;
      if (!object(data) || data.schema_version !== "fenorium/instrument-analytics/1"
        || data.instrument_id !== id
        || !object(data.size_curve) || !Array.isArray(data.size_curve.points) || !object(data.terms)) {
        throw Error("response does not match this instrument and universe");
      }
      // A universe published after this page loaded: every figure on this tab still comes from one
      // snapshot, the reply's own, and the tab says it is not the one the rest of the page shows.
      if (current()) instrumentAnalyticsCache.set(key, {status: "ready", data, fetchedAt: P.clockNow(),
        otherSnapshot: Boolean(sourceHash && data.source_sha256 !== sourceHash)});
    } catch (error) {
      if (current()) instrumentAnalyticsCache.set(key, {status: "error", error: boundedAdapterError(error), retryable: true});
    } finally {
      if (current()) renderSelectedAsset();
      else if (instrumentAnalyticsCache.get(key) === pending) instrumentAnalyticsCache.delete(key);
    }
    return current() ? instrumentAnalyticsCache.get(key) : null;
  }

  // Judged at each paint against the market the page shows now: a refresh of the same-hash universe
  // brings today's ratings without changing the cached analytics.
  function withRatingDay(entry) {
    if (entry?.status !== "ready") return entry;
    const ratedOn = state.metadata?.received_on;
    return {...entry, otherDay: !entry.otherSnapshot && ratedOn && ratedOn !== new Date(P.clockNow()).toISOString().slice(0, 10) ? ratedOn : null};
  }

  function renderSelectedAsset() {
    if (!state.selected || typeof U.asset !== "function") return;
    state.assetTab = normalizeAssetTab(state.assetTab);
    const key = instrumentAnalyticsKey(state.selected);
    const selection = `${state.assetTab}:${key}`;
    if (selection !== instrumentAnalyticsSelection) {
      instrumentAnalyticsSelection = selection;
      instrumentAnalyticsRequest += 1;
      // Pending entries belong to the previous selection; their replies cannot populate a new page.
      // A failed read is forgotten too, so coming back to the instrument asks again; a repaint of
      // the same selection does not, which keeps a failing platform from being polled.
      for (const [cachedKey, value] of instrumentAnalyticsCache) {
        if (value.status === "loading" || value.status === "error") instrumentAnalyticsCache.delete(cachedKey);
      }
    }
    if (state.assetTab === "analytics") void ensureInstrumentAnalytics();
    U.asset(displayRow(state.selected), state.assetTab, rowMetadata(state.selected), state.candidate,
      isSimulatedRow(state.selected) ? null : purchaseView(state.selected), state.notionalSize,
      withRatingDay(instrumentAnalyticsCache.get(key)) || (P.instrumentId(state.selected) === null
        ? {status: "error", error: "this instrument has no platform id"} : {status: "loading"}),
      marketPopulation(state.universe, "").rows.map(displayRow));
    root.FenoriumCharts?.bindTooltips?.(root.document);
  }

  function setPurchaseAmount(row, value, account = state.wallet.account) {
    const current = ensurePurchaseState(row, account);
    if (!current) return null;
    const activeInput = typeof document !== "undefined" && document.activeElement?.id === "purchase-amount"
      ? document.activeElement
      : null;
    const selection = activeInput
      ? {
          start: activeInput.selectionStart,
          end: activeInput.selectionEnd,
          direction: activeInput.selectionDirection,
        }
      : null;
    const updated = setPurchaseState(row, {
      amountInput: String(value),
      requestGeneration: (current.requestGeneration || 0) + 1,
      readsStale: true,
      canPurchase: false,
      canSell: false,
      canFaucet: false,
    }, account);
    if (activeInput) {
      const replacement = $("purchase-amount");
      if (replacement && replacement !== activeInput) {
        replacement.focus?.({ preventScroll: true });
        if (typeof replacement.setSelectionRange === "function"
          && Number.isInteger(selection.start)
          && Number.isInteger(selection.end)) {
          const length = String(replacement.value || "").length;
          replacement.setSelectionRange(
            Math.min(selection.start, length),
            Math.min(selection.end, length),
            selection.direction || "none",
          );
        }
      }
    }
    return updated;
  }

  function beginPurchaseRequest(row, account, action = null, requireActiveAccount = true) {
    const current = ensurePurchaseState(row, account);
    if (!current) return null;
    const generation = (current.requestGeneration || 0) + 1;
    const capture = {
      account: accountKey(account),
      amountInput: current.amountInput || "1",
      generation,
      action,
      requireActiveAccount,
    };
    const patch = {
      requestGeneration: generation,
      readsStale: true,
      canPurchase: false,
      canSell: false,
      canFaucet: false,
    };
    if (action) {
      patch.actionSerial = (current.actionSerial || 0) + 1;
      capture.actionSerial = patch.actionSerial;
      patch.inFlightAction = { ...capture };
      patch.status = STATUS.PENDING;
      patch.message = `Refreshing the exact quote before the ${action} request.`;
      patch.errorMessage = "";
    }
    setPurchaseState(row, patch, account);
    return capture;
  }

  function purchaseRequestIsCurrent(row, capture) {
    if (!capture) return false;
    const current = ensurePurchaseState(row, capture.account);
    return Boolean(current
      && current.requestGeneration === capture.generation
      && (current.amountInput || "1") === capture.amountInput
      && (!capture.requireActiveAccount || accountKey(state.wallet.account) === capture.account)
      && state.wallet.chainId === BASE_SEPOLIA.chainId
      && (!capture.action
        || (current.inFlightAction?.generation === capture.generation
          && current.inFlightAction?.action === capture.action)));
  }

  function discardStalePurchaseRequest(row, capture) {
    const current = ensurePurchaseState(row, capture?.account);
    if (!current) return null;
    if (capture?.action && current.inFlightAction?.generation === capture.generation) {
      return setPurchaseState(row, {
        inFlightAction: null,
        readsStale: true,
        canPurchase: false,
        canSell: false,
        canFaucet: false,
        message: "Refresh on-chain reads to see the current quote. No transaction was submitted.",
      }, capture.account);
    }
    return { ...current };
  }

  function maturedPurchaseRefusal(deployment) {
    return { canPurchase: false, canFaucet: false,
      message: `Matured on ${deployment.maturity}. Buying is closed; the desk still buys back at its quoted price.` };
  }

  function finishPurchaseAction(row, capture, patch) {
    if (!purchaseRequestIsCurrent(row, capture)) return discardStalePurchaseRequest(row, capture);
    return setPurchaseState(row, { ...patch, inFlightAction: null }, capture.account);
  }

  function invalidatePurchaseReads(row, account, patch = {}) {
    const current = ensurePurchaseState(row, account);
    if (!current) return null;
    return setPurchaseState(row, {
      requestGeneration: (current.requestGeneration || 0) + 1,
      readsStale: true,
      canPurchase: false,
      canSell: false,
      canFaucet: false,
      ...patch,
    }, account);
  }

  async function refreshPurchase(row, options = {}) {
    if (!row || !isExactDemoInstrument(row)) return null;
    const hasExplicitAccount = Object.prototype.hasOwnProperty.call(options, "account");
    const refreshAccount = hasExplicitAccount
      ? options.account
      : state.wallet.account;
    const current = ensurePurchaseState(row, refreshAccount);
    const deployment = deploymentInfo(row);
    if (!deployment || !deployment.configured) {
      return setPurchaseState(row, {
        status: STATUS.UNCONFIGURED_DEPLOYMENT,
        errorMessage: "This Base Sepolia testnet demo is not configured. Ask the administrator to configure it.",
        canPurchase: false,
        canSell: false,
      }, refreshAccount);
    }
    if (!refreshAccount) {
      return setPurchaseState(row, { status: STATUS.NO_WALLET, message: "Connect a wallet to read the Base Sepolia testnet demo state.", canPurchase: false, canSell: false, canFaucet: false });
    }
    if (state.wallet.chainId !== BASE_SEPOLIA.chainId) {
      return invalidatePurchaseReads(row, refreshAccount, {
        status: STATUS.WRONG_CHAIN,
        message: "Switch to the Base Sepolia testnet before reading or using this demo.",
      });
    }
    const unresolved = current.unresolvedTransaction;
    let resolvedKind = null;
    if (unresolved) {
      const receiptAccount = unresolved.account;
      const stages = TRANSACTION_STAGES[unresolved.kind];
      let receipt;
      try {
        receipt = await state.provider.request({ method: "eth_getTransactionReceipt", params: [unresolved.hash] });
      } catch (error) {
        if (!ownsUnresolvedTransaction(row, receiptAccount, unresolved.hash)) return { ...ensurePurchaseState(row, receiptAccount) };
        return setPurchaseState(row, {
          ...indeterminateTransactionPatch(error, unresolved.label, unresolved.hash),
          stage: stages.indeterminate,
        }, receiptAccount);
      }
      // The action's own receipt wait may have resolved this transaction while the request was in
      // flight, and a newer transaction may own the record by now.
      if (!ownsUnresolvedTransaction(row, receiptAccount, unresolved.hash)) return { ...ensurePurchaseState(row, receiptAccount) };
      if (!receipt) {
        return setPurchaseState(row, {
          ...indeterminateTransactionPatch(null, unresolved.label, unresolved.hash),
          stage: stages.indeterminate,
        }, receiptAccount);
      }
      if (receiptReverted(receipt)) {
        return setPurchaseState(row, {
          unresolvedTransaction: null,
          ...receiptOutcomePatch(unresolved.kind, "REVERTED"),
          status: STATUS.REVERT,
          stage: stages.reverted,
          errorMessage: `${confirmedActionLabel(unresolved.kind)} did not go through. Try it again after reviewing the wallet's message.`,
          canPurchase: false,
          canSell: false,
          canFaucet: false,
        }, receiptAccount);
      }
      if (!receiptSucceeded(receipt)) {
        return setPurchaseState(row, {
          ...indeterminateTransactionPatch(null, unresolved.label, unresolved.hash),
          stage: stages.indeterminate,
        }, receiptAccount);
      }
      resolvedKind = unresolved.kind;
      const confirmedExecution = isExecutionKind(resolvedKind);
      setPurchaseState(row, {
        unresolvedTransaction: null,
        ...receiptOutcomePatch(resolvedKind, "CONFIRMED"),
        confirmedOutcome: confirmedExecution
          ? {
              kind: resolvedKind,
              hash: unresolved.hash,
              ...(resolvedKind === "purchase" || resolvedKind === "sale" ? { amountRaw: unresolved.amountRaw } : {}),
            }
          : current.confirmedOutcome,
        status: confirmedExecution ? STATUS.SUCCESS : current.status,
        stage: stages.complete,
        message: `${confirmedActionLabel(resolvedKind)} succeeded on the Base Sepolia testnet. Refreshing current on-chain reads.`,
        errorCode: null,
        errorMessage: "",
        canPurchase: false,
        canSell: false,
        canFaucet: false,
      }, receiptAccount);
    }
    const confirmedKind = resolvedKind || current.confirmedOutcome?.kind || null;
    const confirmedExecution = isExecutionKind(confirmedKind);
    const readRequest = beginPurchaseRequest(row, refreshAccount, null, !hasExplicitAccount);
    const result = await readDemoOnChain(state.provider, row, state.config, refreshAccount, readRequest.amountInput);
    if (!purchaseRequestIsCurrent(row, readRequest)) return discardStalePurchaseRequest(row, readRequest);
    if (result.status === STATUS.WRONG_NETWORK_RPC) return setPurchaseState(row, result, refreshAccount);
    if (result.status === STATUS.INPUT_ERROR) {
      if (confirmedExecution) {
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES[confirmedKind].complete,
          message: `${confirmedActionLabel(confirmedKind)} succeeded on the Base Sepolia testnet. Enter a valid amount to refresh current on-chain reads; no new transaction was submitted.`,
          errorCode: result.errorCode,
          errorMessage: result.errorMessage,
          readsStale: true,
          canPurchase: false,
          canSell: false,
          canFaucet: false,
        }, refreshAccount);
      }
      return setPurchaseState(row, { ...result, canPurchase: false, canSell: false }, refreshAccount);
    }
    if (result.status === STATUS.RPC_ERROR) {
      const confirmedApproval = resolvedKind === "approval" || resolvedKind === "instrumentApproval";
      return setPurchaseState(row, {
        ...result,
        status: confirmedExecution ? STATUS.SUCCESS : STATUS.RPC_ERROR,
        stage: confirmedExecution
          ? TRANSACTION_STAGES[confirmedKind].refreshFailed
          : confirmedApproval ? TRANSACTION_STAGES[resolvedKind].refreshFailed : current.stage,
        ...(confirmedApproval ? receiptConfirmedPatch(resolvedKind) : {}),
        message: confirmedExecution
          ? `${confirmedActionLabel(confirmedKind)} succeeded on the Base Sepolia testnet, but current on-chain reads failed. Refresh on-chain reads; no new transaction was submitted.`
          : resolvedKind === "approval"
            ? "Approval succeeded, but current on-chain reads failed. Refresh on-chain reads; no purchase was submitted."
            : confirmedApproval
              ? "Instrument approval succeeded, but current on-chain reads failed. Refresh on-chain reads; no sale was submitted."
              : undefined,
        readsStale: true,
        canPurchase: false,
        canSell: false,
        canFaucet: false,
      }, refreshAccount);
    }
    const allowance = BigInt(result.allowanceRaw);
    const { insufficient, unverified, canPurchase, canFaucet, canSell } = actionAvailability(deployment, result, repeatBlockedKind(current.confirmedOutcome, result.amountRaw));
    const status = confirmedExecution
      ? STATUS.SUCCESS
      : unverified
      ? STATUS.UNVERIFIED_GATED
      : insufficient ? STATUS.INSUFFICIENT_MUSDC : (options.preserveSuccess ? STATUS.SUCCESS : STATUS.READY);
    const resolvedMessage = confirmedExecution
      ? `${confirmedActionLabel(confirmedKind)} succeeded on the Base Sepolia testnet. Current on-chain reads were refreshed.`
      : null;
    return setPurchaseState(row, {
      ...result,
      status,
      stage: confirmedExecution ? TRANSACTION_STAGES[confirmedKind].complete : current.stage,
      message: resolvedMessage || options.successMessage || undefined,
      canPurchase,
      canFaucet,
      canSell,
      errorMessage: confirmedExecution && insufficient
        ? "The current testnet mUSDC balance is below the exact quote. The confirmed outcome is unchanged."
        : confirmedExecution && unverified ? "This wallet is not verified for another purchase or sale. The confirmed outcome is unchanged." : "",
      readsStale: false,
      allowanceRaw: allowance.toString(),
      quoteAccount: readRequest.account,
      quoteAmountInput: readRequest.amountInput,
    }, refreshAccount);
  }

  function userRejected(error) {
    return Number(error?.code) === 4001 || String(error?.code).toUpperCase() === "ACTION_REJECTED";
  }

  function rpcFailurePatch(error) {
    const decoded = decodeError(error);
    return {
      status: userRejected(error) ? STATUS.USER_REJECTED : STATUS.RPC_ERROR,
      errorCode: error?.code || decoded.name,
      // Keep the wallet's own reason. A send that fails for want of Base Sepolia ETH reports
      // insufficient funds, and dropping that left the user retrying forever against advice to
      // refresh reads -- reads succeed indefinitely without ever supplying the missing gas.
      errorMessage: userRejected(error)
        ? "The wallet declined this action. Refresh on-chain reads to re-enable it, then try again."
        : `This action could not be completed before confirmation.${decoded.message ? ` Your wallet reported: ${decoded.message}` : ""}`,
      canPurchase: false,
      canSell: false,
    };
  }

  function indeterminateTransactionPatch(error, label, transactionHash) {
    const decoded = decodeError(error);
    return {
      status: STATUS.INDETERMINATE,
      errorCode: error?.code || decoded.name,
      errorMessage: `${label} outcome is unknown. The transaction may still confirm; check it in the Base Sepolia testnet explorer before trying again.`,
      message: `The transaction result is still pending. Check this transaction in the Base Sepolia testnet explorer: ${transactionHash}`,
      canPurchase: false,
      canSell: false,
      canFaucet: false,
    };
  }

  function receiptOutcomePatch(kind, outcome) {
    if (kind === "approval") return { approvalReceiptOutcome: outcome };
    if (kind === "purchase") return { purchaseReceiptOutcome: outcome };
    if (kind === "faucet") return { faucetReceiptOutcome: outcome };
    if (kind === "instrumentApproval") return { instrumentApprovalReceiptOutcome: outcome };
    if (kind === "sale") return { saleReceiptOutcome: outcome };
    return {};
  }

  function receiptConfirmedPatch(kind) {
    if (kind === "instrumentApproval") return { instrumentApprovalReceiptConfirmed: true };
    return { approvalReceiptConfirmed: true };
  }

  function repeatBlockedKind(outcome, amountRaw) {
    if (outcome?.kind !== "purchase" && outcome?.kind !== "sale") return null;
    if (outcome.amountRaw == null || amountRaw == null) return outcome.kind;
    return BigInt(outcome.amountRaw) === BigInt(amountRaw) ? outcome.kind : null;
  }

  // What the current reads allow. A confirmed purchase or sale blocks the same raw amount;
  // a different amount and the other side stay available on these reads.
  function actionAvailability(deployment, reads, confirmedKind = null) {
    const insufficient = BigInt(reads.paymentBalanceRaw) < BigInt(reads.quoteRaw);
    const unverified = deployment.gated && reads.verified !== true;
    const sellable = deployment.sellConfigured === true && reads.sellQuoteRaw != null && reads.instrumentBalanceRaw != null && reads.amountRaw != null;
    const insufficientInstrument = sellable && BigInt(reads.instrumentBalanceRaw) < BigInt(reads.amountRaw);
    return {
      insufficient,
      unverified,
      insufficientInstrument,
      canPurchase: !deployment.matured && confirmedKind !== "purchase" && !unverified && !insufficient,
      canFaucet: !deployment.matured && deployment.faucetConfigured && insufficient,
      canSell: confirmedKind !== "sale" && sellable && !unverified && !insufficientInstrument,
    };
  }

  // `current` is the caller's own staleness rule: the local-chain check below awaits the wallet,
  // and an amount edited meanwhile would otherwise still go out with the old calldata.
  async function sendTransaction(to, data, from = state.wallet.account, current = null) {
    if (!configuredAddress(to)) throw makeCodeError("INVALID_TRANSACTION_TARGET", "transaction target is not configured");
    if (testnetWorkspace()?.local) {
      if (!await verifyLocalWalletRpc()) throw makeCodeError(STATUS.WRONG_NETWORK_RPC, localRpcRefusal().errorMessage);
      if (current && !current()) throw makeCodeError("ACTION_SUPERSEDED", "The request changed while the wallet's network was being checked; nothing was sent.");
    }
    const hash = await state.provider.request({
      method: "eth_sendTransaction",
      params: [{ from, to, data }],
    });
    if (typeof hash !== "string" || !hash) throw makeCodeError("INVALID_TRANSACTION_HASH", "wallet did not return a transaction hash");
    return hash;
  }

  async function explainMinedRevert(to, data, receipt, from = state.wallet.account) {
    try {
      await state.provider.request({
        method: "eth_call",
        params: [{ from, to, data }, receipt?.blockNumber || "latest"],
      });
    } catch (error) {
      return decodeError(error).message;
    }
    return "Transaction receipt reported a revert; no revert data was returned by the provider.";
  }

  async function performPurchase(row) {
    if (!row || !isExactDemoInstrument(row)) return null;
    const current = ensurePurchaseState(row, state.wallet.account);
    const unresolved = current.unresolvedTransaction;
    if (unresolved) {
      return setPurchaseState(row, {
        ...indeterminateTransactionPatch(null, unresolved.label, unresolved.hash),
        stage: TRANSACTION_STAGES[unresolved.kind].indeterminate,
      }, unresolved.account);
    }
    if (current.inFlightAction) return { ...current };
    const deployment = deploymentInfo(row);
    if (!deployment || !deployment.configured) return setPurchaseState(row, { status: STATUS.UNCONFIGURED_DEPLOYMENT, canPurchase: false, canSell: false, errorMessage: "This Base Sepolia testnet demo is not configured. Ask the administrator to configure it." });
    if (deployment.matured) return setPurchaseState(row, maturedPurchaseRefusal(deployment));
    if (!state.wallet.account) return setPurchaseState(row, { status: STATUS.NO_WALLET, canPurchase: false, canSell: false, message: "Connect a wallet to use the Base Sepolia testnet demo." });
    if (state.wallet.chainId !== BASE_SEPOLIA.chainId) return setPurchaseState(row, { status: STATUS.WRONG_CHAIN, canPurchase: false, canSell: false, message: "Switch to the Base Sepolia testnet first." });
    const submittingAccount = state.wallet.account;
    const capturedAmountInput = current.amountInput || "1";
    const displayedQuoteRaw = current.readsStale !== true
      && current.quoteAccount === accountKey(submittingAccount)
      && current.quoteAmountInput === capturedAmountInput
      ? current.quoteRaw
      : null;
    const purchaseRequest = beginPurchaseRequest(row, submittingAccount, "purchase");
    let instrumentAmount;
    try {
      instrumentAmount = parseUnits(purchaseRequest.amountInput, deployment.instrumentDecimals);
    } catch (error) {
      return finishPurchaseAction(row, purchaseRequest, { status: STATUS.INPUT_ERROR, canPurchase: false, canSell: false, errorCode: error.code, errorMessage: amountInputErrorMessage(error) });
    }
    const reads = await readDemoOnChain(state.provider, row, state.config, submittingAccount, purchaseRequest.amountInput);
    if (!purchaseRequestIsCurrent(row, purchaseRequest)) return discardStalePurchaseRequest(row, purchaseRequest);
    if (reads.status !== STATUS.READY) return finishPurchaseAction(row, purchaseRequest, { ...reads, canPurchase: false, canSell: false });
    // Nothing else is awaited before the approval or purchase is sent, so this is the last moment
    // a maturity can pass unnoticed.
    if (deploymentInfo(row)?.matured) return finishPurchaseAction(row, purchaseRequest, { ...reads, ...maturedPurchaseRefusal(deployment) });
    const paymentBalance = BigInt(reads.paymentBalanceRaw);
    const freshQuote = BigInt(reads.quoteRaw);
    const allowance = BigInt(reads.allowanceRaw);
    const freshReads = {
      ...reads,
      readsStale: false,
      quoteAccount: purchaseRequest.account,
      quoteAmountInput: purchaseRequest.amountInput,
    };
    if (deployment.gated && reads.verified !== true) return finishPurchaseAction(row, purchaseRequest, { ...freshReads, status: STATUS.UNVERIFIED_GATED, canPurchase: false, canSell: false, errorMessage: "This gated testnet instrument is not available to this wallet. Use an eligible wallet." });
    if (displayedQuoteRaw == null || BigInt(displayedQuoteRaw) !== freshQuote) {
      const previousQuote = displayedQuoteRaw == null ? null : formatUnits(displayedQuoteRaw, deployment.paymentDecimals);
      const nextQuote = formatUnits(freshQuote, deployment.paymentDecimals);
      const insufficient = paymentBalance < freshQuote;
      return finishPurchaseAction(row, purchaseRequest, {
        ...freshReads,
        status: insufficient ? STATUS.INSUFFICIENT_MUSDC : STATUS.READY,
        stage: displayedQuoteRaw == null ? PURCHASE_STAGES.QUOTE_REVIEW_REQUIRED : PURCHASE_STAGES.QUOTE_CHANGED,
        quoteAccount: accountKey(submittingAccount),
        quoteAmountInput: purchaseRequest.amountInput,
        canPurchase: !insufficient,
        canFaucet: deployment.faucetConfigured && insufficient,
        canSell: actionAvailability(deployment, reads, repeatBlockedKind(current.confirmedOutcome, reads.amountRaw)).canSell,
        message: displayedQuoteRaw == null
          ? `Exact quote loaded: ${nextQuote} mUSDC. This is a Base Sepolia testnet quote. Review it, then click again to confirm this ceiling.`
          : `Quote changed from ${previousQuote} to ${nextQuote} mUSDC. This is a Base Sepolia testnet quote. No transaction was submitted. Review it, then click again to confirm this ceiling.`,
        errorMessage: insufficient ? insufficientFundsMessage(deployment) : "",
      });
    }
    const displayedQuote = BigInt(displayedQuoteRaw);
    if (paymentBalance < displayedQuote) return finishPurchaseAction(row, purchaseRequest, { ...freshReads, status: STATUS.INSUFFICIENT_MUSDC, canPurchase: false, canSell: false, canFaucet: deployment.faucetConfigured, errorMessage: insufficientFundsMessage(deployment) });

    let approvalTx = current.approvalTx;
    let attemptApprovalTx = null;
    if (allowance < displayedQuote) {
      setPurchaseState(row, {
        ...freshReads,
        confirmedOutcome: null,
        approvalReceiptOutcome: null,
        status: STATUS.PENDING,
        stage: TRANSACTION_STAGES.approval.pending,
        canPurchase: false,
        canSell: false,
        errorMessage: "Approve the exact quoted testnet mUSDC amount in your wallet to continue.",
      }, submittingAccount);
      let approvalConfirmed = false;
      try {
        const approvalData = encodeApprove(deployment.saleAddress, displayedQuote);
        attemptApprovalTx = await sendTransaction(deployment.paymentAddress, approvalData, submittingAccount, () => purchaseRequestIsCurrent(row, purchaseRequest));
        approvalTx = attemptApprovalTx;
        retainUnresolvedTransaction(row, { kind: "approval", label: "Approval", hash: approvalTx, account: submittingAccount });
        setPurchaseState(row, { approvalTx, approvalUrl: transactionExplorerUrl(approvalTx, deployment.explorerUrl), status: STATUS.PENDING, stage: TRANSACTION_STAGES.approval.receipt, canPurchase: false, canSell: false }, submittingAccount);
        const approvalReceipt = await waitForReceipt(state.provider, approvalTx);
        if (!ownsUnresolvedTransaction(row, submittingAccount, approvalTx)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (receiptReverted(approvalReceipt)) {
          clearUnresolvedTransaction(row, submittingAccount);
          const reason = await explainMinedRevert(deployment.paymentAddress, approvalData, approvalReceipt, submittingAccount);
          if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
          return setPurchaseState(row, {
            approvalReceiptOutcome: "REVERTED",
            status: STATUS.REVERT,
            stage: TRANSACTION_STAGES.approval.reverted,
            errorMessage: `Refresh on-chain reads. Approval did not go through; the wallet reported: ${reason}`,
            canPurchase: false,
            canSell: false,
          }, submittingAccount);
        }
        if (!receiptSucceeded(approvalReceipt)) {
          return setPurchaseState(row, { ...indeterminateTransactionPatch(null, "Approval", approvalTx), approvalTx, stage: TRANSACTION_STAGES.approval.indeterminate }, submittingAccount);
        }
        approvalConfirmed = true;
        clearUnresolvedTransaction(row, submittingAccount);
        setPurchaseState(row, { approvalReceiptOutcome: "CONFIRMED" }, submittingAccount);
        const refreshedAllowance = await readUint(state.provider, deployment.paymentAddress, encodeAllowance(submittingAccount, deployment.saleAddress));
        if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (refreshedAllowance < displayedQuote) {
          return setPurchaseState(row, {
            status: STATUS.CONFIRMED_STATE_MISMATCH,
            stage: TRANSACTION_STAGES.approval.reverted,
            approvalTx,
            message: "Refresh on-chain reads. Approval was confirmed, but the approved amount could not be verified. No purchase was submitted.",
            errorMessage: "Refresh on-chain reads to verify the approved amount before trying the purchase again.",
            canPurchase: false,
            canSell: false,
          }, submittingAccount);
        }
        const refreshed = await refreshPurchase(row, { account: submittingAccount });
        if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (refreshed?.status === STATUS.RPC_ERROR) {
          return setPurchaseState(row, {
            status: STATUS.RPC_ERROR,
            stage: TRANSACTION_STAGES.approval.refreshFailed,
            approvalTx,
            approvalReceiptConfirmed: true,
            canPurchase: false,
            canSell: false,
            canFaucet: false,
            readsStale: true,
            message: "Approval succeeded, but current on-chain reads failed. Refresh on-chain reads; no purchase was submitted.",
            errorMessage: refreshed.errorMessage || "Refresh on-chain reads before trying the purchase again.",
          }, submittingAccount);
        }
        const approvalReady = refreshed.status === STATUS.READY && refreshed.readsStale !== true;
        return setPurchaseState(row, {
          status: refreshed.status,
          stage: TRANSACTION_STAGES.approval.complete,
          approvalTx,
          approvalReceiptConfirmed: true,
          canPurchase: refreshed.canPurchase === true,
          canSell: refreshed.canSell === true,
          ...(approvalReady ? {
            message: "Approval succeeded on the Base Sepolia testnet. Review the exact quote now displayed, then purchase the demo instrument.",
            errorMessage: "",
          } : {}),
        }, submittingAccount);
      } catch (error) {
        if (!approvalConfirmed && attemptApprovalTx && !ownsUnresolvedTransaction(row, submittingAccount, attemptApprovalTx)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (approvalConfirmed) {
          if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
          return setPurchaseState(row, {
            status: STATUS.RPC_ERROR,
            stage: TRANSACTION_STAGES.approval.refreshFailed,
            approvalTx,
            approvalReceiptConfirmed: true,
            readsStale: true,
            canPurchase: false,
            canSell: false,
            canFaucet: false,
            message: "Approval succeeded, but the approved amount could not be refreshed. Refresh on-chain reads; no purchase was submitted.",
            errorCode: error?.code || decodeError(error).name,
            errorMessage: `Refresh on-chain reads to verify the approval before trying the purchase again. ${decodeError(error).message}`,
          }, submittingAccount);
        }
        const failure = attemptApprovalTx ? indeterminateTransactionPatch(error, "Approval", attemptApprovalTx) : rpcFailurePatch(error);
        return setPurchaseState(row, { ...failure, approvalTx, inFlightAction: null, stage: attemptApprovalTx ? TRANSACTION_STAGES.approval.indeterminate : TRANSACTION_STAGES.approval.pending }, submittingAccount);
      }
    }

    setPurchaseState(row, {
      confirmedOutcome: null,
      purchaseReceiptOutcome: null,
      status: STATUS.PENDING,
      stage: TRANSACTION_STAGES.purchase.pending,
      approvalTx,
      canPurchase: false,
      canSell: false,
      errorMessage: "Confirm the purchase on the Base Sepolia testnet in your wallet.",
    }, submittingAccount);
    let purchaseTx = null;
    let purchaseConfirmed = false;
    try {
      const purchaseData = encodePurchase(instrumentAmount, displayedQuote, deployment.purchaseSignature);
      purchaseTx = await sendTransaction(deployment.saleAddress, purchaseData, submittingAccount, () => purchaseRequestIsCurrent(row, purchaseRequest));
      retainUnresolvedTransaction(row, { kind: "purchase", label: "Purchase", hash: purchaseTx, account: submittingAccount, amountRaw: instrumentAmount.toString() });
      setPurchaseState(row, { purchaseTx, purchaseUrl: transactionExplorerUrl(purchaseTx, deployment.explorerUrl), status: STATUS.PENDING, stage: TRANSACTION_STAGES.purchase.receipt, canPurchase: false, canSell: false }, submittingAccount);
      const purchaseReceipt = await waitForReceipt(state.provider, purchaseTx);
      if (!ownsUnresolvedTransaction(row, submittingAccount, purchaseTx)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (receiptReverted(purchaseReceipt)) {
        clearUnresolvedTransaction(row, submittingAccount);
        const reason = await explainMinedRevert(deployment.saleAddress, purchaseData, purchaseReceipt, submittingAccount);
        if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        return setPurchaseState(row, {
          purchaseReceiptOutcome: "REVERTED",
          status: STATUS.REVERT,
          stage: TRANSACTION_STAGES.purchase.reverted,
          errorMessage: `Refresh on-chain reads. Purchase did not go through; the wallet reported: ${reason}`,
          canPurchase: false,
          canSell: false,
        }, submittingAccount);
      }
      if (!receiptSucceeded(purchaseReceipt)) {
        return setPurchaseState(row, { ...indeterminateTransactionPatch(null, "Purchase", purchaseTx), approvalTx, purchaseTx, stage: TRANSACTION_STAGES.purchase.indeterminate }, submittingAccount);
      }
      purchaseConfirmed = true;
      setPurchaseState(row, {
        unresolvedTransaction: null,
        purchaseReceiptOutcome: "CONFIRMED",
        confirmedOutcome: { kind: "purchase", hash: purchaseTx, amountRaw: instrumentAmount.toString() },
        status: STATUS.SUCCESS,
        stage: TRANSACTION_STAGES.purchase.complete,
        canPurchase: false,
        canSell: false,
        canFaucet: false,
        message: "Purchase succeeded on the Base Sepolia testnet. Refreshing current on-chain reads.",
      }, submittingAccount);
      const refreshed = await refreshPurchase(row, {
        account: submittingAccount,
        preserveSuccess: true,
        successMessage: "Purchase succeeded on the Base Sepolia testnet. Balances were refreshed from the chain.",
      });
      if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (refreshed?.status !== STATUS.SUCCESS || refreshed.readsStale === true) {
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES.purchase.refreshFailed,
          approvalTx,
          purchaseTx,
          canPurchase: false,
          canSell: false,
          canFaucet: false,
          readsStale: true,
          message: "Purchase succeeded on the Base Sepolia testnet. Current balances could not be re-read from the chain, so balance values are hidden until a successful refresh.",
          errorMessage: refreshed?.errorMessage || "Refresh on-chain reads to show the current balances.",
        }, submittingAccount);
      }
      return setPurchaseState(row, {
        status: STATUS.SUCCESS,
        stage: TRANSACTION_STAGES.purchase.complete,
        approvalTx,
        purchaseTx,
        confirmedOutcome: { kind: "purchase", hash: purchaseTx, amountRaw: instrumentAmount.toString() },
        message: "Purchase succeeded on the Base Sepolia testnet. Balances were refreshed from the chain.",
      }, submittingAccount);
    } catch (error) {
      if (!purchaseConfirmed && purchaseTx && !ownsUnresolvedTransaction(row, submittingAccount, purchaseTx)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (purchaseConfirmed) {
        if (!isLatestAction(row, purchaseRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES.purchase.refreshFailed,
          approvalTx,
          purchaseTx,
          confirmedOutcome: { kind: "purchase", hash: purchaseTx, amountRaw: instrumentAmount.toString() },
          readsStale: true,
          canPurchase: false,
          canSell: false,
          canFaucet: false,
          message: "Purchase succeeded on the Base Sepolia testnet. Current balances could not be re-read from the chain, so balance values are hidden until a successful refresh.",
          errorCode: error?.code || decodeError(error).name,
          errorMessage: `Refresh on-chain reads to show the current balances. ${decodeError(error).message}`,
        }, submittingAccount);
      }
      const failure = purchaseTx
        ? indeterminateTransactionPatch(error, "Purchase", purchaseTx)
        : rpcFailurePatch(error);
      return setPurchaseState(row, { ...failure, approvalTx, purchaseTx, inFlightAction: null, stage: purchaseTx ? TRANSACTION_STAGES.purchase.indeterminate : TRANSACTION_STAGES.purchase.pending }, submittingAccount);
    }
  }

  async function performSale(row) {
    if (!row || !isExactDemoInstrument(row)) return null;
    const current = ensurePurchaseState(row, state.wallet.account);
    const unresolved = current.unresolvedTransaction;
    if (unresolved) {
      return setPurchaseState(row, {
        ...indeterminateTransactionPatch(null, unresolved.label, unresolved.hash),
        stage: TRANSACTION_STAGES[unresolved.kind].indeterminate,
      }, unresolved.account);
    }
    if (current.inFlightAction) return { ...current };
    const deployment = deploymentInfo(row);
    if (!deployment || !deployment.configured) return setPurchaseState(row, { status: STATUS.UNCONFIGURED_DEPLOYMENT, canPurchase: false, canSell: false, errorMessage: "This Base Sepolia testnet demo is not configured. Ask the administrator to configure it." });
    if (!deployment.sellConfigured) return setPurchaseState(row, { canSell: false, errorMessage: "This Base Sepolia testnet demo has no sell-back desk configured. Ask the administrator to configure it." });
    if (!state.wallet.account) return setPurchaseState(row, { status: STATUS.NO_WALLET, canPurchase: false, canSell: false, message: "Connect a wallet to use the Base Sepolia testnet demo." });
    if (state.wallet.chainId !== BASE_SEPOLIA.chainId) return setPurchaseState(row, { status: STATUS.WRONG_CHAIN, canPurchase: false, canSell: false, message: "Switch to the Base Sepolia testnet first." });
    const submittingAccount = state.wallet.account;
    const capturedAmountInput = current.amountInput || "1";
    const displayedSellQuoteRaw = current.readsStale !== true
      && current.quoteAccount === accountKey(submittingAccount)
      && current.quoteAmountInput === capturedAmountInput
      && current.sellQuoteRaw != null
      ? current.sellQuoteRaw
      : null;
    const saleRequest = beginPurchaseRequest(row, submittingAccount, "sale");
    let instrumentAmount;
    try {
      instrumentAmount = parseUnits(saleRequest.amountInput, deployment.instrumentDecimals);
    } catch (error) {
      return finishPurchaseAction(row, saleRequest, { status: STATUS.INPUT_ERROR, canPurchase: false, canSell: false, errorCode: error.code, errorMessage: amountInputErrorMessage(error) });
    }
    const reads = await readDemoOnChain(state.provider, row, state.config, submittingAccount, saleRequest.amountInput);
    if (!purchaseRequestIsCurrent(row, saleRequest)) return discardStalePurchaseRequest(row, saleRequest);
    if (reads.status !== STATUS.READY) return finishPurchaseAction(row, saleRequest, { ...reads, canPurchase: false, canSell: false });
    const instrumentAllowance = BigInt(reads.instrumentAllowanceRaw);
    const freshSellQuote = BigInt(reads.sellQuoteRaw);
    const available = actionAvailability(deployment, reads, repeatBlockedKind(current.confirmedOutcome, reads.amountRaw));
    const freshReads = {
      ...reads,
      readsStale: false,
      quoteAccount: saleRequest.account,
      quoteAmountInput: saleRequest.amountInput,
      canPurchase: available.canPurchase,
      canFaucet: available.canFaucet,
    };
    if (deployment.gated && reads.verified !== true) return finishPurchaseAction(row, saleRequest, { ...freshReads, status: STATUS.UNVERIFIED_GATED, canPurchase: false, canSell: false, errorMessage: "This gated testnet instrument cannot be sold back from this wallet. Use an eligible wallet." });
    const insufficientInstrument = available.insufficientInstrument;
    if (displayedSellQuoteRaw == null || BigInt(displayedSellQuoteRaw) !== freshSellQuote) {
      const previousQuote = displayedSellQuoteRaw == null ? null : formatUnits(displayedSellQuoteRaw, deployment.paymentDecimals);
      const nextQuote = formatUnits(freshSellQuote, deployment.paymentDecimals);
      return finishPurchaseAction(row, saleRequest, {
        ...freshReads,
        status: insufficientInstrument ? STATUS.INSUFFICIENT_INSTRUMENT : STATUS.READY,
        stage: displayedSellQuoteRaw == null ? PURCHASE_STAGES.SALE_QUOTE_REVIEW_REQUIRED : PURCHASE_STAGES.SALE_QUOTE_CHANGED,
        canSell: !insufficientInstrument,
        message: displayedSellQuoteRaw == null
          ? `Sell-back quote loaded: ${nextQuote} mUSDC. This is a Base Sepolia testnet quote. Review it, then click again to confirm this floor.`
          : `Sell-back quote changed from ${previousQuote} to ${nextQuote} mUSDC. This is a Base Sepolia testnet quote. No transaction was submitted. Review it, then click again to confirm this floor.`,
        errorMessage: insufficientInstrument ? insufficientInstrumentMessage(row) : "",
      });
    }
    const displayedSellQuote = BigInt(displayedSellQuoteRaw);
    if (insufficientInstrument) return finishPurchaseAction(row, saleRequest, { ...freshReads, status: STATUS.INSUFFICIENT_INSTRUMENT, canSell: false, errorMessage: insufficientInstrumentMessage(row) });

    let instrumentApprovalTx = current.instrumentApprovalTx;
    let attemptApprovalTx = null;
    if (instrumentAllowance < BigInt(instrumentAmount)) {
      setPurchaseState(row, {
        ...freshReads,
        confirmedOutcome: null,
        instrumentApprovalReceiptOutcome: null,
        status: STATUS.PENDING,
        stage: TRANSACTION_STAGES.instrumentApproval.pending,
        canPurchase: false,
        canSell: false,
        errorMessage: "Approve the exact instrument amount for the desk in your wallet to continue.",
      }, submittingAccount);
      let approvalConfirmed = false;
      try {
        const approvalData = encodeApprove(deployment.saleAddress, instrumentAmount);
        attemptApprovalTx = await sendTransaction(deployment.instrumentAddress, approvalData, submittingAccount, () => purchaseRequestIsCurrent(row, saleRequest));
        instrumentApprovalTx = attemptApprovalTx;
        retainUnresolvedTransaction(row, { kind: "instrumentApproval", label: "Instrument approval", hash: instrumentApprovalTx, account: submittingAccount });
        setPurchaseState(row, { instrumentApprovalTx, instrumentApprovalUrl: transactionExplorerUrl(instrumentApprovalTx, deployment.explorerUrl), status: STATUS.PENDING, stage: TRANSACTION_STAGES.instrumentApproval.receipt, canPurchase: false, canSell: false }, submittingAccount);
        const approvalReceipt = await waitForReceipt(state.provider, instrumentApprovalTx);
        if (!ownsUnresolvedTransaction(row, submittingAccount, instrumentApprovalTx)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (receiptReverted(approvalReceipt)) {
          clearUnresolvedTransaction(row, submittingAccount);
          const reason = await explainMinedRevert(deployment.instrumentAddress, approvalData, approvalReceipt, submittingAccount);
          if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
          return setPurchaseState(row, {
            instrumentApprovalReceiptOutcome: "REVERTED",
            status: STATUS.REVERT,
            stage: TRANSACTION_STAGES.instrumentApproval.reverted,
            errorMessage: `Refresh on-chain reads. Instrument approval did not go through; the wallet reported: ${reason}`,
            canPurchase: false,
            canSell: false,
          }, submittingAccount);
        }
        if (!receiptSucceeded(approvalReceipt)) {
          return setPurchaseState(row, { ...indeterminateTransactionPatch(null, "Instrument approval", instrumentApprovalTx), instrumentApprovalTx, stage: TRANSACTION_STAGES.instrumentApproval.indeterminate }, submittingAccount);
        }
        approvalConfirmed = true;
        clearUnresolvedTransaction(row, submittingAccount);
        setPurchaseState(row, { instrumentApprovalReceiptOutcome: "CONFIRMED" }, submittingAccount);
        const refreshedAllowance = await readUint(state.provider, deployment.instrumentAddress, encodeAllowance(submittingAccount, deployment.saleAddress));
        if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (refreshedAllowance < BigInt(instrumentAmount)) {
          return setPurchaseState(row, {
            status: STATUS.CONFIRMED_STATE_MISMATCH,
            stage: TRANSACTION_STAGES.instrumentApproval.reverted,
            instrumentApprovalTx,
            message: "Refresh on-chain reads. Instrument approval was confirmed, but the approved amount could not be verified. No sale was submitted.",
            errorMessage: "Refresh on-chain reads to verify the approved amount before trying the sale again.",
            canPurchase: false,
            canSell: false,
          }, submittingAccount);
        }
        const refreshed = await refreshPurchase(row, { account: submittingAccount });
        if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (refreshed?.status === STATUS.RPC_ERROR) {
          return setPurchaseState(row, {
            status: STATUS.RPC_ERROR,
            stage: TRANSACTION_STAGES.instrumentApproval.refreshFailed,
            instrumentApprovalTx,
            instrumentApprovalReceiptConfirmed: true,
            canPurchase: false,
            canFaucet: false,
            canSell: false,
            readsStale: true,
            message: "Instrument approval succeeded, but current on-chain reads failed. Refresh on-chain reads; no sale was submitted.",
            errorMessage: refreshed.errorMessage || "Refresh on-chain reads before trying the sale again.",
          }, submittingAccount);
        }
        const approvalReady = refreshed.status === STATUS.READY && refreshed.readsStale !== true;
        return setPurchaseState(row, {
          status: refreshed.status,
          stage: TRANSACTION_STAGES.instrumentApproval.complete,
          instrumentApprovalTx,
          instrumentApprovalReceiptConfirmed: true,
          canPurchase: refreshed.canPurchase === true,
          canSell: refreshed.canSell === true,
          ...(approvalReady ? {
            message: "Instrument approval succeeded on the Base Sepolia testnet. Review the sell-back quote now displayed, then sell the demo instrument back to the desk.",
            errorMessage: "",
          } : {}),
        }, submittingAccount);
      } catch (error) {
        if (!approvalConfirmed && attemptApprovalTx && !ownsUnresolvedTransaction(row, submittingAccount, attemptApprovalTx)) return { ...ensurePurchaseState(row, submittingAccount) };
        if (approvalConfirmed) {
          if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
          return setPurchaseState(row, {
            status: STATUS.RPC_ERROR,
            stage: TRANSACTION_STAGES.instrumentApproval.refreshFailed,
            instrumentApprovalTx,
            instrumentApprovalReceiptConfirmed: true,
            readsStale: true,
            canPurchase: false,
            canFaucet: false,
            canSell: false,
            message: "Instrument approval succeeded, but the approved amount could not be refreshed. Refresh on-chain reads; no sale was submitted.",
            errorCode: error?.code || decodeError(error).name,
            errorMessage: `Refresh on-chain reads to verify the approval before trying the sale again. ${decodeError(error).message}`,
          }, submittingAccount);
        }
        const failure = attemptApprovalTx ? indeterminateTransactionPatch(error, "Instrument approval", attemptApprovalTx) : rpcFailurePatch(error);
        return setPurchaseState(row, { ...failure, instrumentApprovalTx, inFlightAction: null, stage: attemptApprovalTx ? TRANSACTION_STAGES.instrumentApproval.indeterminate : TRANSACTION_STAGES.instrumentApproval.pending }, submittingAccount);
      }
    }

    setPurchaseState(row, {
      confirmedOutcome: null,
      saleReceiptOutcome: null,
      status: STATUS.PENDING,
      stage: TRANSACTION_STAGES.sale.pending,
      instrumentApprovalTx,
      canPurchase: false,
      canSell: false,
      errorMessage: "Confirm the sale on the Base Sepolia testnet in your wallet.",
    }, submittingAccount);
    let saleTx = null;
    let saleConfirmed = false;
    try {
      const saleData = encodeSell(instrumentAmount, displayedSellQuote, deployment.sellSignature);
      saleTx = await sendTransaction(deployment.saleAddress, saleData, submittingAccount, () => purchaseRequestIsCurrent(row, saleRequest));
      retainUnresolvedTransaction(row, { kind: "sale", label: "Sale", hash: saleTx, account: submittingAccount, amountRaw: instrumentAmount.toString() });
      setPurchaseState(row, { saleTx, saleUrl: transactionExplorerUrl(saleTx, deployment.explorerUrl), status: STATUS.PENDING, stage: TRANSACTION_STAGES.sale.receipt, canPurchase: false, canSell: false }, submittingAccount);
      const saleReceipt = await waitForReceipt(state.provider, saleTx);
      if (!ownsUnresolvedTransaction(row, submittingAccount, saleTx)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (receiptReverted(saleReceipt)) {
        clearUnresolvedTransaction(row, submittingAccount);
        const reason = await explainMinedRevert(deployment.saleAddress, saleData, saleReceipt, submittingAccount);
        if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        return setPurchaseState(row, {
          saleReceiptOutcome: "REVERTED",
          status: STATUS.REVERT,
          stage: TRANSACTION_STAGES.sale.reverted,
          errorMessage: `Refresh on-chain reads. Sale did not go through; the wallet reported: ${reason}`,
          canPurchase: false,
          canSell: false,
        }, submittingAccount);
      }
      if (!receiptSucceeded(saleReceipt)) {
        return setPurchaseState(row, { ...indeterminateTransactionPatch(null, "Sale", saleTx), instrumentApprovalTx, saleTx, stage: TRANSACTION_STAGES.sale.indeterminate }, submittingAccount);
      }
      saleConfirmed = true;
      setPurchaseState(row, {
        unresolvedTransaction: null,
        saleReceiptOutcome: "CONFIRMED",
        confirmedOutcome: { kind: "sale", hash: saleTx, amountRaw: instrumentAmount.toString() },
        status: STATUS.SUCCESS,
        stage: TRANSACTION_STAGES.sale.complete,
        canPurchase: false,
        canFaucet: false,
        canSell: false,
        message: "Sale succeeded on the Base Sepolia testnet. Refreshing current on-chain reads.",
      }, submittingAccount);
      const refreshed = await refreshPurchase(row, {
        account: submittingAccount,
        preserveSuccess: true,
        successMessage: "Sale succeeded on the Base Sepolia testnet. Balances were refreshed from the chain.",
      });
      if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (refreshed?.status !== STATUS.SUCCESS || refreshed.readsStale === true) {
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES.sale.refreshFailed,
          instrumentApprovalTx,
          saleTx,
          canPurchase: false,
          canFaucet: false,
          canSell: false,
          readsStale: true,
          message: "Sale succeeded on the Base Sepolia testnet. Current balances could not be re-read from the chain, so balance values are hidden until a successful refresh.",
          errorMessage: refreshed?.errorMessage || "Refresh on-chain reads to show the current balances.",
        }, submittingAccount);
      }
      return setPurchaseState(row, {
        status: STATUS.SUCCESS,
        stage: TRANSACTION_STAGES.sale.complete,
        instrumentApprovalTx,
        saleTx,
        confirmedOutcome: { kind: "sale", hash: saleTx, amountRaw: instrumentAmount.toString() },
        canSell: false,
        message: "Sale succeeded on the Base Sepolia testnet. Balances were refreshed from the chain.",
      }, submittingAccount);
    } catch (error) {
      if (!saleConfirmed && saleTx && !ownsUnresolvedTransaction(row, submittingAccount, saleTx)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (saleConfirmed) {
        if (!isLatestAction(row, saleRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES.sale.refreshFailed,
          instrumentApprovalTx,
          saleTx,
          confirmedOutcome: { kind: "sale", hash: saleTx, amountRaw: instrumentAmount.toString() },
          readsStale: true,
          canPurchase: false,
          canFaucet: false,
          canSell: false,
          message: "Sale succeeded on the Base Sepolia testnet. Current balances could not be re-read from the chain, so balance values are hidden until a successful refresh.",
          errorCode: error?.code || decodeError(error).name,
          errorMessage: `Refresh on-chain reads to show the current balances. ${decodeError(error).message}`,
        }, submittingAccount);
      }
      const failure = saleTx
        ? indeterminateTransactionPatch(error, "Sale", saleTx)
        : rpcFailurePatch(error);
      return setPurchaseState(row, { ...failure, instrumentApprovalTx, saleTx, inFlightAction: null, stage: saleTx ? TRANSACTION_STAGES.sale.indeterminate : TRANSACTION_STAGES.sale.pending }, submittingAccount);
    }
  }

  async function requestFaucet(row) {
    if (!row || !isExactDemoInstrument(row)) return null;
    const current = ensurePurchaseState(row, state.wallet.account);
    const unresolved = current.unresolvedTransaction;
    if (unresolved) {
      return setPurchaseState(row, {
        ...indeterminateTransactionPatch(null, unresolved.label, unresolved.hash),
        stage: TRANSACTION_STAGES[unresolved.kind].indeterminate,
      }, unresolved.account);
    }
    if (current.inFlightAction) return { ...current };
    const deployment = deploymentInfo(row);
    if (!deployment?.faucetConfigured) return setPurchaseState(row, { status: STATUS.UNCONFIGURED_DEPLOYMENT, canPurchase: false, canSell: false, errorMessage: "The Base Sepolia testnet mUSDC faucet is not configured. Ask the administrator to configure it." });
    if (!state.wallet.account) return setPurchaseState(row, { status: STATUS.NO_WALLET, canPurchase: false, canSell: false, canFaucet: false, message: "Connect a wallet to use the Base Sepolia testnet faucet." });
    if (state.wallet.chainId !== BASE_SEPOLIA.chainId) return setPurchaseState(row, { status: STATUS.WRONG_CHAIN, canPurchase: false, canSell: false, canFaucet: false, message: "Switch to the Base Sepolia testnet first." });
    // The faucet only funds a purchase, so it closes with buying.
    if (deployment.matured) return setPurchaseState(row, maturedPurchaseRefusal(deployment));
    const submittingAccount = state.wallet.account;
    const faucetRequest = beginPurchaseRequest(row, submittingAccount, "faucet");
    const reads = await readDemoOnChain(state.provider, row, state.config, submittingAccount, faucetRequest.amountInput);
    if (!purchaseRequestIsCurrent(row, faucetRequest)) return discardStalePurchaseRequest(row, faucetRequest);
    if (reads.status !== STATUS.READY) return finishPurchaseAction(row, faucetRequest, { ...reads, canPurchase: false, canSell: false, canFaucet: false });
    if (deploymentInfo(row)?.matured) return finishPurchaseAction(row, faucetRequest, { ...reads, ...maturedPurchaseRefusal(deployment) });
    const amount = reads.quoteRaw;
    const paymentBalance = BigInt(reads.paymentBalanceRaw);
    const freshQuote = BigInt(reads.quoteRaw);
    const freshReads = {
      ...reads,
      readsStale: false,
      quoteAccount: faucetRequest.account,
      quoteAmountInput: faucetRequest.amountInput,
    };
    if (paymentBalance >= freshQuote) {
      return finishPurchaseAction(row, faucetRequest, {
        ...freshReads,
        status: STATUS.READY,
        canPurchase: true,
        canFaucet: false,
        message: "The current testnet mUSDC balance already covers the exact quote; no faucet transaction was submitted.",
      });
    }
    setPurchaseState(row, {
      ...freshReads,
      confirmedOutcome: null,
      faucetReceiptOutcome: null,
      status: STATUS.PENDING,
      stage: TRANSACTION_STAGES.faucet.pending,
      canPurchase: false,
      canSell: false,
      canFaucet: false,
      errorMessage: "Confirm the testnet mUSDC funding transaction in your wallet.",
    }, submittingAccount);
    let faucetTx = null;
    let faucetConfirmed = false;
    try {
      faucetTx = await sendTransaction(deployment.faucetAddress, encodeMint(submittingAccount, amount), submittingAccount, () => purchaseRequestIsCurrent(row, faucetRequest));
      retainUnresolvedTransaction(row, { kind: "faucet", label: "Faucet", hash: faucetTx, account: submittingAccount });
      setPurchaseState(row, { faucetTx, faucetUrl: transactionExplorerUrl(faucetTx, deployment.explorerUrl), status: STATUS.PENDING, stage: TRANSACTION_STAGES.faucet.receipt, canPurchase: false, canSell: false, canFaucet: false }, submittingAccount);
      const receipt = await waitForReceipt(state.provider, faucetTx);
      if (!ownsUnresolvedTransaction(row, submittingAccount, faucetTx)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (receiptReverted(receipt)) {
        clearUnresolvedTransaction(row, submittingAccount);
        return setPurchaseState(row, {
          faucetReceiptOutcome: "REVERTED",
          status: STATUS.REVERT,
          stage: TRANSACTION_STAGES.faucet.reverted,
          errorMessage: "Refresh on-chain reads. Testnet mUSDC funding did not go through; review the wallet's message.",
          canPurchase: false,
          canSell: false,
          canFaucet: false,
        }, submittingAccount);
      }
      if (!receiptSucceeded(receipt)) {
        return setPurchaseState(row, { ...indeterminateTransactionPatch(null, "Faucet", faucetTx), faucetTx, stage: TRANSACTION_STAGES.faucet.indeterminate }, submittingAccount);
      }
      faucetConfirmed = true;
      setPurchaseState(row, {
        unresolvedTransaction: null,
        faucetReceiptOutcome: "CONFIRMED",
        confirmedOutcome: { kind: "faucet", hash: faucetTx },
        status: STATUS.SUCCESS,
        stage: TRANSACTION_STAGES.faucet.complete,
        canPurchase: false,
        canSell: false,
        canFaucet: false,
        message: "Testnet mUSDC funding succeeded on the Base Sepolia testnet. Refreshing current on-chain reads.",
      }, submittingAccount);
      const refreshed = await refreshPurchase(row, {
        account: submittingAccount,
        preserveSuccess: true,
        successMessage: "Testnet mUSDC funding succeeded on the Base Sepolia testnet. Balance was refreshed.",
      });
      if (!isLatestAction(row, faucetRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (refreshed?.status !== STATUS.SUCCESS || refreshed.readsStale === true) {
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES.faucet.refreshFailed,
          faucetTx,
          canPurchase: false,
          canSell: false,
          canFaucet: false,
          readsStale: true,
          message: "Testnet mUSDC funding succeeded on the Base Sepolia testnet. The current balance could not be re-read, so it is hidden until a successful refresh.",
          errorMessage: refreshed?.errorMessage || "Refresh on-chain reads to show the current balance.",
        }, submittingAccount);
      }
      return setPurchaseState(row, {
        status: STATUS.SUCCESS,
        stage: TRANSACTION_STAGES.faucet.complete,
        faucetTx,
        confirmedOutcome: { kind: "faucet", hash: faucetTx },
        message: "Testnet mUSDC funding succeeded on the Base Sepolia testnet. Balance was refreshed from the chain.",
      }, submittingAccount);
    } catch (error) {
      if (!faucetConfirmed && faucetTx && !ownsUnresolvedTransaction(row, submittingAccount, faucetTx)) return { ...ensurePurchaseState(row, submittingAccount) };
      if (faucetConfirmed) {
        if (!isLatestAction(row, faucetRequest)) return { ...ensurePurchaseState(row, submittingAccount) };
        return setPurchaseState(row, {
          status: STATUS.SUCCESS,
          stage: TRANSACTION_STAGES.faucet.refreshFailed,
          faucetTx,
          confirmedOutcome: { kind: "faucet", hash: faucetTx },
          readsStale: true,
          canPurchase: false,
          canSell: false,
          canFaucet: false,
          message: "Testnet mUSDC funding succeeded on the Base Sepolia testnet. The current balance could not be re-read, so it is hidden until a successful refresh.",
          errorCode: error?.code || decodeError(error).name,
          errorMessage: `Refresh on-chain reads to show the current balance. ${decodeError(error).message}`,
        }, submittingAccount);
      }
      const failure = faucetTx
        ? indeterminateTransactionPatch(error, "Faucet", faucetTx)
        : rpcFailurePatch(error);
      return setPurchaseState(row, { ...failure, faucetTx, inFlightAction: null, stage: faucetTx ? TRANSACTION_STAGES.faucet.indeterminate : TRANSACTION_STAGES.faucet.pending }, submittingAccount);
    }
  }

  function setWallet(account, chainId, message = "", options = {}) {
    // Only for the wallet's own event announcing a switch this page already applied, and only while
    // nothing is in flight: the same account on the same chain keeps the route built for it.
    const kept = options.keepRoute === true && execution && execution.phase === "IDLE" && !execution.sending
      && String(execution.account || "").toLowerCase() === String(account || "").toLowerCase()
      && execution.chain_id === normalizeChainId(chainId) ? execution : null;
    state.walletGeneration += 1;
    // The viewed wallet's book is keyed to the connected chain, which may just have changed.
    state.viewGeneration += 1;
    state.transferScanSuppressed = false;
    // Unsent quotes expire with the wallet; submittedPositionExits retains unresolved submissions.
    positionExitStates.clear();
    state.transferGeneration += 1;
    state.transferHistory = null;
    state.transferScanRequestedFor = null;
    // Belongs to the wallet that just went away. Leaving it up shows one account's past under
    // another's name, which is worse than showing nothing.
    state.historyGeneration += 1;
    state.history = null;
    state.historyError = null;
    // A route is bound to the wallet that asked for it; under another wallet it is not signable.
    execution = null;
    executionGeneration += 1;
    if (kept) {
      kept.generation = executionGeneration;
      kept.wallet_generation = state.walletGeneration;
      execution = kept;
    }
    const normalizedAccount = configuredAddress(account) ? account : null;
    const normalizedChain = normalizeChainId(chainId);
    // Connected means connected: a wallet on any chain Fenorium knows is not on the wrong chain.
    // Only the demo purchase and faucet need Base Sepolia, and they say so themselves.
    let status = STATUS.NO_WALLET;
    if (normalizedAccount) status = knownWalletChain(normalizedChain) ? STATUS.CONNECTED : STATUS.WRONG_CHAIN;
    state.wallet = {
      status,
      account: normalizedAccount,
      chainId: normalizedChain,
      chainName: routeChainName(normalizedChain),
      connectIntent: state.wallet.connectIntent === true,
      message: message || (status === STATUS.WRONG_CHAIN
        ? "Switch to the Base Sepolia testnet to use the demo."
        : status === STATUS.CONNECTED ? `${walletChainLabel(normalizedChain)} · connected` : "Wallet optional · read-only market"),
    };
    const localChain = Boolean(testnetWorkspace()?.local) && normalizedChain === BASE_SEPOLIA.chainId;
    if (localChain && normalizedAccount) {
      state.wallet.status = STATUS.WRONG_NETWORK_RPC;
      state.wallet.message = localRpcRefusal().errorMessage;
    }
    // Every wallet event retires the last answer: the provider may have moved network while no
    // account was exposed, and a watched book is still read through it.
    state.localRpcVerified = null;
    state.localWalletCheck = localChain && (normalizedAccount || state.provider) ? verifyLocalWalletRpc() : null;
    renderWallet();
    void refreshLendingOrders();
    void refreshRouteQuotes();
    // The viewed book is re-read on every wallet change, a watched one even when the signer has
    // gone (its pending read was invalidated and would otherwise stay "Reading…"); the signer's
    // own book is read beside it when another wallet is on screen, for pay options and receipts.
    const readBook = () => {
      if (state.universe.length && ((normalizedAccount && state.provider) || !viewedWallet().connected)) {
        void refreshPortfolio();
        if (normalizedAccount && state.provider && !viewedWallet().connected) void refreshPortfolio({scope: "connected"});
      } else void refreshVenuePositions();
    };
    if (state.localWalletCheck) {
      const generation = state.walletGeneration;
      void state.localWalletCheck.then(valid => {
        if (generation !== state.walletGeneration) return;
        if (valid) readBook(); else void refreshVenuePositions();
      });
    } else readBook();
    if (state.selected?.is_demo) {
      renderSelectedAsset();
      void refreshPurchase(state.selected);
    }
    return state.wallet;
  }

  function portfolioAccountKey(account = state.wallet.account, chainId = state.wallet.chainId) {
    return configuredAddress(account) && Number.isInteger(chainId)
      ? `${account.toLowerCase()}:${chainId}`
      : null;
  }

  function portfolioRecord(account = state.wallet.account, chainId = state.wallet.chainId) {
    const key = portfolioAccountKey(account, chainId);
    if (!key) return null;
    if (!state.portfolioByAccount[key]) {
      state.portfolioByAccount[key] = { snapshot: null, loading: false, error: null, promise: null, capture: null };
    }
    return state.portfolioByAccount[key];
  }

  function portfolioCaptureCurrent(capture) {
    if (!capture) return false;
    // A read of the signing wallet's own book stays current whatever is being viewed.
    if (capture.scope === "connected") {
      return capture.generation === state.connectedReadGeneration
        && capture.walletGeneration === state.walletGeneration
        && capture.account === String(state.wallet.account || "").toLowerCase()
        && capture.chainId === state.wallet.chainId;
    }
    const viewed = viewedWallet();
    return capture.generation === state.portfolioReadGeneration
      && capture.walletGeneration === state.walletGeneration
      && capture.viewGeneration === state.viewGeneration
      && capture.account === String(viewed.account || "").toLowerCase()
      && capture.chainId === viewed.chainId;
  }

  function boundedAdapterError(error) {
    const text = String(error?.message || error || "unknown provider failure").replace(/[\u0000-\u001f]/g, " ");
    return text.slice(0, 240);
  }

  async function readPortfolioObservations(provider, wallet, chainId, assets) {
    const block = await provider.request({ method: "eth_getBlockByNumber", params: ["finalized", false] });
    const blockNumber = normalizeChainId(block?.number);
    if (!Number.isSafeInteger(blockNumber) || blockNumber < 0 || typeof block?.hash !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(block.hash)) {
      throw makeCodeError("INVALID_FINALIZED_BLOCK", "the injected provider did not return a usable finalized block reference");
    }
    const observations = [];
    const chunkSize = Number(P.MULTICALL_CHUNK_SIZE || 75);
    for (let start = 0; start < assets.length; start += chunkSize) {
      const chunk = assets.slice(start, start + chunkSize);
      const calls = chunk.map((asset) => ({ target: asset.address, callData: P.encodeBalanceOfCall(wallet) }));
      try {
        const result = await provider.request({
          method: "eth_call",
          params: [{ to: P.MULTICALL3_ADDRESS, data: P.encodeAggregate3(calls) }, block.number],
        });
        const decoded = P.decodeAggregate3(result, chunk.length);
        decoded.forEach((entry, index) => {
          const asset = chunk[index];
          if (!entry.success) {
            const detail = entry.returnData && entry.returnData !== "0x"
              ? `Multicall3 balanceOf sub-call reverted: ${entry.returnData.slice(0, 74)}`
              : "Multicall3 balanceOf sub-call failed without return data";
            observations.push({ chain_id: chainId, address: asset.address, error: detail });
            return;
          }
          try {
            observations.push({ chain_id: chainId, address: asset.address, raw_amount: decodeUint256(entry.returnData).toString() });
          } catch (error) {
            observations.push({ chain_id: chainId, address: asset.address, error: `Invalid balanceOf return data: ${boundedAdapterError(error)}` });
          }
        });
      } catch (error) {
        const detail = `Multicall3 chunk failed: ${boundedAdapterError(error)}`;
        chunk.forEach((asset) => observations.push({ chain_id: chainId, address: asset.address, error: detail }));
      }
    }
    return {
      blockReference: { chain_id: chainId, block_number: blockNumber, block_hash: block.hash, finalized: true },
      observations,
      as_of: typeof block.timestamp === "string" && /^0x[0-9a-f]+$/i.test(block.timestamp) && BigInt(block.timestamp) <= 8640000000000n ? new Date(Number(BigInt(block.timestamp)) * 1000).toISOString() : null,
    };
  }

  // Browser-only, bounded discovery. No logs or wallet linkage are posted to a server.
  // The full depth is the default everywhere a scan can be started, and it is a length of
  // history rather than a count of blocks: 900,000 of them was two years of Arbitrum's worth of
  // requests and under three days of its history. `transferScanDepth` converts one into the
  // other per chain and says which bound it hit. Creation-block bounds, one request per window
  // for every contract at once, and concurrent windows are what make it affordable as a default.

  // eth_getLogs range caps vary by provider, so a deep scan is inherently
  // many small requests and running them one at a time made it minutes long. Six in flight is
  // roughly five times faster here and stays well short of what an injected wallet provider
  // will queue.
  const SCAN_CONCURRENCY = 6;

  async function pooled(items, limit, run) {
    const queue = items.slice();
    let failure = null;
    const worker = async () => {
      while (queue.length && !failure) {
        const item = queue.shift();
        try {
          await run(item);
        } catch (error) {
          failure = failure || error;
        }
      }
    };
    await Promise.all(Array.from({length: Math.max(1, Math.min(limit, items.length))}, worker));
    if (failure) throw failure;
  }

  async function readTransferHistory(provider, wallet, chainId, assets, range = {}) {
    const quantity = (value) => {
      if (typeof value === "string" && /^0x[0-9a-f]+$/i.test(value)) value = Number(BigInt(value));
      if (!Number.isSafeInteger(value) || value < 0) throw new Error("Invalid block range");
      return value;
    };
    if (!configuredAddress(wallet)) throw new Error("Invalid wallet address");
    const windowSize = quantity(range.windowSize ?? 9000);
    if (!windowSize || windowSize > 9000) throw new Error("Invalid scan depth or window size");
    const full = P.transferScanDepth(chainId, {windowSize});
    const depth = quantity(range.depth ?? full.blocks);
    if (!depth || depth > full.blocks) throw new Error("Invalid scan depth or window size");
    const current = () => { if (range.isCurrent && !range.isCurrent()) throw new Error("Wallet or chain changed; scan discarded"); };
    // An injected wallet proxies to its own endpoint, which rate-limits and drops requests under
    // the few hundred a full scan makes. One refused window failed its whole asset, and one
    // failed asset takes the entire derived history down with it. A refusal that says the
    // request itself is wrong -- rejected by the user, or a method the wallet does not
    // implement -- is final and is never retried.
    const FINAL_CODES = new Set([4001, 4100, 4200, -32601, -32602]);
    const retryable = (error) => {
      const code = Number(error?.code ?? error?.data?.code);
      if (Number.isFinite(code) && FINAL_CODES.has(code)) return false;
      return !/user (rejected|denied)|unsupported method|not supported/i.test(String(error?.message || ""));
    };
    // Backs off where there is a clock to back off against, and simply retries where there is
    // not. Reaching for a bare setTimeout threw in any context without one, which turned every
    // retryable failure into "setTimeout is not defined" and hid the real refusal.
    const pause = (ms) => {
      const timer = root.setTimeout || (typeof setTimeout === "function" ? setTimeout : null);
      return timer ? new Promise((resolve) => { timer(resolve, ms); }) : Promise.resolve();
    };
    const request = async (method, params) => {
      for (let attempt = 0; ; attempt += 1) {
        current();
        try {
          const value = await provider.request({method, params});
          current();
          return value;
        } catch (error) {
          // Retrying the same oversized range cannot succeed. Let fetchLogs split it
          // immediately; transient transport failures still use the normal backoff.
          if (method === "eth_getLogs" && rangeLimited(error)) throw error;
          if (attempt >= 3 || !retryable(error)) throw error;
          await pause(250 * (attempt + 1) * (attempt + 1));
        }
      }
    };
    const finalized = await request("eth_getBlockByNumber", ["finalized", false]);
    const finalizedNumber = quantity(finalized?.number);
    if (!/^0x[0-9a-f]{64}$/i.test(finalized?.hash || "")) throw new Error("Invalid finalized block");
    const toBlock = range.toBlock == null ? finalizedNumber : quantity(range.toBlock);
    if (toBlock > finalizedNumber) throw new Error("Scan end is not finalized");
    const fromBlock = Math.max(range.fromBlock == null ? 0 : quantity(range.fromBlock), toBlock - depth + 1);
    if (fromBlock > toBlock) throw new Error("Invalid scan interval");
    const hex = (n) => `0x${n.toString(16)}`;
    const walletTopic = `0x${wallet.slice(2).toLowerCase().padStart(64, "0")}`;
    const blocks = new Map();
    if (/^0x[0-9a-f]+$/i.test(finalized.timestamp || "")) blocks.set(finalizedNumber, Promise.resolve(finalized));
    const timestampFor = async (log) => {
      const number = quantity(log.blockNumber);
      if (!blocks.has(number)) blocks.set(number, request("eth_getBlockByNumber", [hex(number), false]));
      const block = await blocks.get(number);
      if (quantity(block?.number) !== number || !/^0x[0-9a-f]{64}$/i.test(block?.hash || "")
          || String(block.hash).toLowerCase() !== String(log.blockHash).toLowerCase()) throw new Error("Block reference mismatch");
      quantity(block.timestamp);
      return block.timestamp;
    };
    // A refresh moves the finalized block by a handful of blocks, and rescanning seven hundred
    // thousand of them to learn that is what turned every refresh into another half minute of
    // UNKNOWN. An earlier scan of the same wallet is extended instead: its range and this one
    // are disjoint by construction, so the rows concatenate without needing to be deduplicated.
    const earlier = new Map();
    if (range.previous?.status === "COMPLETE" && Number.isSafeInteger(Number(range.previous.to_block))) {
      const previousTo = Number(range.previous.to_block);
      // `<=`, not `<`: a scan already at this exact block has nothing new to read, and saying
      // so costs no requests at all.
      if (previousTo <= toBlock && previousTo >= fromBlock) {
        // The earlier scan names the block it ended at. A chain without that block -- a local
        // chain reset and rebuilt at the same addresses -- is not the chain it was read from,
        // and its rows would live on as the history of transactions that no longer exist.
        if (!blocks.has(previousTo)) blocks.set(previousTo, request("eth_getBlockByNumber", [hex(previousTo), false]));
        const ended = await blocks.get(previousTo);
        current();
        if (/^0x[0-9a-f]{64}$/i.test(range.previous.to_block_hash || "")
            && String(ended?.hash || "").toLowerCase() === String(range.previous.to_block_hash).toLowerCase()) {
          for (const record of range.previous.assets || []) {
            const token = String(record?.asset?.address || "").toLowerCase();
            if (record?.status === "COMPLETE" && token) earlier.set(token, {record, previousTo});
          }
        }
      }
    }
    // One request can name many contracts: eth_getLogs takes `address` as an array, and the
    // logs come back exactly as they do asset by asset. Scanning 116 registered Ethereum
    // instruments one at a time was 116 requests per window per direction -- 23,200 for a full
    // depth -- against whatever endpoint the reader's wallet happens to point at. Named
    // together it is two, however many instruments the universe grows to.
    //
    // Assets are grouped by the block their own scan starts at, because that is the only thing
    // that has to match for them to share a window: a token bounded by its creation block, or
    // one being extended from an earlier scan, starts somewhere else and gets its own group.
    let batching = true;
    // How many blocks this provider will answer for at once. The window size is what the scan
    // asks for; the provider is the one who decides. Base Sepolia allows 10,000 and Base mainnet
    // 2,000 -- the same operator, an order of magnitude apart -- so a fixed size means every
    // request of a whole scan fails against the stricter one. The cap is learned from the
    // refusal and remembered, so it is discovered once rather than on every window.
    let maxRange = windowSize;
    const rangeLimited = (error) => {
      const code = Number(error?.code ?? error?.data?.code);
      if (code === -32614) return true;
      return /limited to a [\d,]+ (block )?range|block range is too (large|wide)|range is too large|exceeds? the (maximum|max) (block )?range|too many blocks|more than \d+ results/i
        .test(String(error?.message || ""));
    };
    // Most providers that cap a range say what the cap is -- "limited to a 2,000 range". Taking
    // them at their word settles it in one refusal and on the number they actually allow;
    // halving blindly costs several and lands short of it.
    const statedCap = (error) => {
      const match = /limited to a ([\d,]+)\b|maximum of ([\d,]+) blocks|range of ([\d,]+) blocks/i.exec(String(error?.message || ""));
      if (!match) return null;
      const value = Number(String(match[1] || match[2] || match[3]).replace(/,/g, ""));
      return Number.isSafeInteger(value) && value > 0 ? value : null;
    };
    const fetchLogs = async (filter, from, to) => {
      if (to - from + 1 > maxRange) {
        const mid = from + maxRange - 1;
        const head = await fetchLogs(filter, from, mid);
        return head.concat(await fetchLogs(filter, mid + 1, to));
      }
      try {
        const logs = await request("eth_getLogs", [{...filter, fromBlock: hex(from), toBlock: hex(to)}]);
        if (!Array.isArray(logs)) throw new Error("Invalid transfer log response");
        return logs;
      } catch (error) {
        if (!rangeLimited(error) || to <= from) throw error;
        const stated = statedCap(error);
        maxRange = stated && stated < to - from + 1 ? stated : Math.max(1, Math.floor((to - from + 1) / 2));
        const mid = from + maxRange - 1;
        const head = await fetchLogs(filter, from, mid);
        return head.concat(await fetchLogs(filter, mid + 1, to));
      }
    };
    const readLogs = async (addresses, start, end, topics) => {
      if (batching && addresses.length > 1) return fetchLogs({address: addresses, topics}, start, end);
      const collected = [];
      for (const address of addresses) collected.push(...await fetchLogs({address, topics}, start, end));
      return collected;
    };

    const results = [];
    const groups = new Map();
    for (const asset of assets) {
      if (Number(asset.chain_id) !== chainId || !configuredAddress(asset.address)) throw new Error("Invalid registered asset chain or address");
      // A token has no transfers before it existed, so a scan that reaches further back is
      // asking the chain about blocks it can only answer "none" for. Bounding by the creation
      // block is what makes a full-depth scan affordable: the instruments deployed today need
      // one window, not a hundred.
      const deployedBlock = Number.isSafeInteger(asset.deployed_block) && asset.deployed_block >= 0
        ? asset.deployed_block
        : 0;
      const candidate = earlier.get(String(asset.address).toLowerCase()) || null;
      // A recent-window cache cannot satisfy a later request for deeper history.
      const extending = candidate && Number.isSafeInteger(candidate.record.from_block)
        && candidate.record.from_block <= Math.max(fromBlock, deployedBlock) ? candidate : null;
      const assetFrom = Math.max(fromBlock, deployedBlock, extending ? extending.previousTo + 1 : 0);
      const result = {asset, status: "UNKNOWN", scanned_blocks: 0, from_block: null, to_block: toBlock, rows: [], reason: null};
      results.push(result);
      if (assetFrom > toBlock) {
        // Nothing left to look at: either the token did not exist yet, or an earlier scan
        // already covered every block up to here. Both are complete answers, not failed ones.
        result.status = "COMPLETE";
        result.from_block = extending ? extending.record.from_block : toBlock + 1;
        result.scanned_blocks = extending ? extending.record.scanned_blocks : 0;
        result.rows = extending ? extending.record.rows.slice() : [];
        continue;
      }
      if (!groups.has(assetFrom)) groups.set(assetFrom, []);
      groups.get(assetFrom).push({result, asset, extending, unique: new Map()});
    }

    // Windows are counted, not assets: named together, a hundred instruments finish in the same
    // instant, so a count of them stands still for minutes and then jumps to the end.
    const planned = [...groups].map(([groupFrom, members]) => {
      const windows = [];
      for (let end = toBlock; end >= groupFrom;) {
        const start = Math.max(groupFrom, end - windowSize + 1);
        windows.push({start, end});
        end = start - 1;
      }
      return {members, windows};
    });
    const totalWindows = planned.reduce((total, plan) => total + plan.windows.length, 0);
    let doneWindows = 0;
    const progressed = () => { if (typeof range.onProgress === "function") range.onProgress(doneWindows, totalWindows); };
    progressed();

    for (const {members, windows} of planned) {
      const byToken = new Map(members.map(member => [String(member.asset.address).toLowerCase(), member]));
      for (const member of members) { member.done = []; member.reason = null; member.invalidLog = false; }
      // Read one window for a set of assets. A batched read that fails is retried contract by
      // contract, so a provider that will not answer for one of them -- a token whose logs
      // exceed its result cap, say -- costs that token its history and nothing else. The retry
      // is paid only on failure; the batched read is what the scan normally makes.
      const readWindow = async (targets, start, end) => {
        const both = async (addresses) => {
          const collected = [];
          for (const topics of [[P.TRANSFER_TOPIC, walletTopic], [P.TRANSFER_TOPIC, null, walletTopic]]) {
            collected.push(...await readLogs(addresses, start, end, topics));
          }
          return collected;
        };
        if (targets.length === 1) {
          try { return {logs: await both([targets[0].asset.address]), failed: new Map()}; }
          catch (error) { return {logs: [], failed: new Map([[targets[0], boundedAdapterError(error)]])}; }
        }
        try { return {logs: await both(targets.map(target => target.asset.address)), failed: new Map()}; }
        catch (_) {
          const logs = [], failed = new Map();
          for (const target of targets) {
            try { logs.push(...await both([target.asset.address])); }
            catch (error) { failed.set(target, boundedAdapterError(error)); }
          }
          // Asking one at a time worked where asking together did not, so the array is what this
          // provider refused. That is the only evidence worth acting on: the codes providers use
          // to say it are not their own -- one endpoint answers -32602 to mean an archive
          // request needs a paid token, which has nothing to do with how the filter was written.
          if (failed.size < targets.length) batching = false;
          return {logs, failed};
        }
      };
      const scanWindow = async ({start, end}) => {
        const targets = members.filter(member => !member.reason);
        if (!targets.length) return;
        const {logs, failed} = await readWindow(targets, start, end);
        for (const [member, reason] of failed) member.reason = member.reason || reason;
        const alive = new Set(targets.filter(target => !failed.has(target)));
        for (const log of logs) {
          const number = quantity(log.blockNumber);
          if (number < start || number > end) throw new Error("Transfer outside requested interval");
          // Every log is attributed to the contract that emitted it. A response carrying a
          // contract that was not asked for is not a log this wallet's history can use.
          const member = byToken.get(String(log.address || "").toLowerCase());
          if (!member) throw new Error("Transfer log from an unrequested contract");
          if (!alive.has(member)) continue;
          try {
            const timestamp = await timestampFor(log);
            const parsed = P.parseTransferLog(log, wallet, member.asset, chainId, timestamp);
            const key = `${log.blockHash}:${log.logIndex}`.toLowerCase();
            const serialized = JSON.stringify(parsed);
            if (member.unique.has(key) && member.unique.get(key).serialized !== serialized) throw new Error("Conflicting duplicate transfer log");
            member.unique.set(key, {serialized, rows: parsed, block: number, index: quantity(log.logIndex)});
          } catch (error) {
            // A log that cannot be dated or parsed invalidates the asset it belongs to, not the
            // whole window: the others in it were read from the same response and are intact.
            member.reason = member.reason || boundedAdapterError(error);
            member.invalidLog = true;
            alive.delete(member);
          }
        }
        for (const member of alive) member.done.push({start, end});
        doneWindows += 1;
        progressed();
      };
      // The first window runs on its own, and it is what decides whether this provider will take
      // an array of contracts. Left to the pool, six windows ask at once and all six are already
      // in flight before the first answer comes back -- six refusals where one would do, and no
      // way to say afterwards which of them settled it.
      const [probe, ...rest] = windows;
      if (probe) await scanWindow(probe);
      await pooled(rest, SCAN_CONCURRENCY, scanWindow);
      for (const member of members) {
        // Recorded whether the scan finished or failed: a partial scan still says truthfully how
        // far it got, and the caller needs that to know the interval a refusal covers.
        const scanned = member.done.reduce((total, window) => total + window.end - window.start + 1, 0);
        const reached = member.done.length ? Math.min(...member.done.map(window => window.start)) : null;
        // An extended scan reports the whole interval it now covers, not just today's slice.
        member.result.scanned_blocks = scanned + (member.extending ? member.extending.record.scanned_blocks : 0);
        member.result.from_block = member.extending ? member.extending.record.from_block : reached;
        if (member.reason) {
          // A provider refusing an older range does not invalidate transfers already
          // parsed in successful windows. Keep those events, but leave coverage UNKNOWN
          // so they cannot populate a complete-history cache or authoritative P&L.
          // Conflicting/malformed logs still invalidate this asset's entire scan.
          const partial = member.invalidLog ? [] : Array.from(member.unique.values())
            .filter(entry => member.done.some(window => entry.block >= window.start && entry.block <= window.end))
            .sort((left, right) => left.block - right.block || left.index - right.index)
            .flatMap(entry => entry.rows);
          member.result.rows = member.invalidLog ? [] : (member.extending ? member.extending.record.rows.concat(partial) : partial);
          member.result.reason = member.reason;
          continue;
        }
        // Windows do not complete in order, so neither the row order nor the covered range can
        // be read off the loop. Both are derived from the windows that actually finished.
        const fresh = Array.from(member.unique.values())
          .sort((left, right) => left.block - right.block || left.index - right.index)
          .flatMap(entry => entry.rows);
        member.result.rows = member.extending ? member.extending.record.rows.concat(fresh) : fresh;
        member.result.status = "COMPLETE";
      }
    }
    if (!blocks.has(toBlock)) blocks.set(toBlock, request("eth_getBlockByNumber", [hex(toBlock), false]));
    const endBlock = await blocks.get(toBlock);
    const endHash = /^0x[0-9a-f]{64}$/i.test(endBlock?.hash || "") ? String(endBlock.hash).toLowerCase() : null;
    return {status: results.length && results.every(item => item.status === "COMPLETE") ? "COMPLETE" : "UNKNOWN",
      declared_depth: depth, from_block: fromBlock, to_block: toBlock, to_block_hash: endHash, assets: results,
      rows: results.flatMap(item => item.rows)};
  }

  function transferCacheKey(wallet, chainId) {
    return `${TRANSFER_CACHE_KEY}:${chainId}:${String(wallet).toLowerCase()}`;
  }

  function readTransferCache(wallet, chainId, toBlock) {
    const raw = safeStorageGet(transferCacheKey(wallet, chainId));
    if (!raw) return null;
    try {
      const cached = JSON.parse(raw);
      const cachedTo = Number(cached?.to_block);
      // Older ledger caches discarded the parties or chain in the ID; reread their logs.
      if (cached?.status !== "COMPLETE" || !Array.isArray(cached.assets) || !cached.assets.length) return null;
      if (cached.assets.some(record => !Array.isArray(record.rows)
        || record.rows.some(row => !configuredAddress(row?.from) || !configuredAddress(row?.to)
          || typeof row.id !== "string" || !row.id.startsWith(`${Number(chainId)}:`)))) return null;
      if (!Number.isSafeInteger(cachedTo) || cachedTo < 0) return null;
      // A cache without the hash of its last block cannot be told from another chain instance's.
      if (!/^0x[0-9a-f]{64}$/i.test(cached.to_block_hash || "")) return null;
      // Never ahead of the block being scanned to: a cache from a longer chain is not evidence
      // about this one, and extending from it would leave a hole.
      if (Number.isSafeInteger(toBlock) && cachedTo > toBlock) return null;
      if (String(cached.wallet || "").toLowerCase() !== String(wallet).toLowerCase()) return null;
      if (Number(cached.chain_id) !== Number(chainId)) return null;
      return cached;
    } catch (_) {
      return null;
    }
  }

  function writeTransferCache(wallet, chainId, history) {
    if (localRpcUnverified()) return false;
    if (history?.status !== "COMPLETE" || !/^0x[0-9a-f]{64}$/i.test(history.to_block_hash || "")) return false;
    const payload = JSON.stringify({
      // The scan's own status, not a hardcoded one: stamping COMPLETE on whatever was passed
      // would let an unfinished scan come back as a finished one on the next load.
      status: history.status, wallet: String(wallet).toLowerCase(), chain_id: Number(chainId),
      to_block: history.to_block, to_block_hash: history.to_block_hash, declared_depth: history.declared_depth,
      assets: (history.assets || []).map((record) => ({
        asset: {address: record.asset?.address, chain_id: record.asset?.chain_id},
        status: record.status, scanned_blocks: record.scanned_blocks,
        from_block: record.from_block, to_block: record.to_block, rows: record.rows,
      })),
    });
    // A book large enough to fill the quota is one this cache should decline rather than throw
    // over; the scan still works, it is only slower.
    if (payload.length > TRANSFER_CACHE_LIMIT) return false;
    return safeStorageSet(transferCacheKey(wallet, chainId), payload);
  }

  // How deep a scan goes is decided in one place -- readTransferHistory, from the chain -- so
  // there is no second copy of the policy here to drift from it.
  async function refreshTransfers(depth) {
    if (localRpcUnverified()) return null;
    const viewed = viewedWallet();
    if (state.sampleVisible || !state.provider?.request || !viewed.account || viewed.chainId !== state.wallet.chainId) return null;
    const provider = state.provider, wallet = viewed.account, chainId = viewed.chainId;
    // Public testnet RPCs cap log reads at as few as 200 blocks. Show recent activity
    // without walking years of blocks first; the full-history control opts into that cost.
    if (depth == null && chainId === BASE_SEPOLIA.chainId) depth = 9000;
    const walletGeneration = state.walletGeneration, viewGeneration = state.viewGeneration;
    const record = portfolioRecord(wallet, chainId);
    if (record?.promise) await record.promise;
    if (localRpcUnverified()) return null;
    if (walletGeneration !== state.walletGeneration || viewGeneration !== state.viewGeneration || provider !== state.provider) return null;
    const generation = ++state.transferGeneration;
    const isCurrent = () => generation === state.transferGeneration && walletGeneration === state.walletGeneration
      && viewGeneration === state.viewGeneration && provider === state.provider;
    const assets = P.registeredAssetsFromUniverse(registryUniverse(), chainId).map(asset => {
      const matches = state.universe.filter(row => Number(row.chain_id) === chainId && String(row.address).toLowerCase() === asset.address.toLowerCase());
      const symbol = matches[0]?.symbol || demoTicker(asset.address) || asset.symbol;
      // The creation block bounds this scan and nothing else, so it is attached here rather
      // than on the registered asset itself: that shape is validated field by field by the
      // platform API, which has no use for it.
      const deployedBlock = matches.length === 1 ? matches[0].deployed_block : undefined;
      const scanned = {...asset, symbol, deployed_block: deployedBlock};
      return matches.length === 1 ? {...matches[0], ...scanned} : scanned;
    });
    // Carried into the scan so it can be extended, and kept on the state object so the screen
    // can keep showing the last complete figure while the few new blocks are read.
    const anchorBlock = Number(portfolioRecord(wallet, chainId)?.finalizedRead?.block_number);
    const previous = state.transferHistory?.status === "COMPLETE"
      ? state.transferHistory
      : readTransferCache(wallet, chainId, anchorBlock);
    // Progress is counted in block ranges, not assets: named together in one request, a hundred
    // instruments finish in the same instant, so an asset count stands still and then jumps.
    state.transferHistory = {status: "LOADING", declared_depth: depth ?? null, assets: [], rows: [],
      scanned: 0, scanning: 0, previous};
    renderOverview();
    try {
      const anchor = record?.finalizedRead;
      const onProgress = (scanned, total) => {
        if (!isCurrent() || state.transferHistory?.status !== "LOADING") return;
        state.transferHistory = {...state.transferHistory, scanned, scanning: total};
        renderOverview();
      };
      const history = await readTransferHistory(provider, wallet, chainId, assets, {isCurrent, depth, toBlock: anchor?.block_number, onProgress, previous});
      if (isCurrent()) state.transferHistory = history;
      if (isCurrent()) writeTransferCache(wallet, chainId, history);
      // Transactions reads the same rows, so it is filled here rather than from a second scan.
      if (isCurrent()) void refreshActivity();
      return isCurrent() ? history : null;
    } catch (error) {
      if (isCurrent()) state.transferHistory = {status: "UNKNOWN", declared_depth: depth ?? null, assets: [], rows: [], error: boundedAdapterError(error)};
      if (isCurrent()) void refreshActivity();
      return null;
    } finally {
      if (isCurrent()) renderOverview();
    }
  }

  async function postPortfolioAnalytics(
    request,
    observations,
    generation,
    priceObservations = [],
    maxPriceAgeSeconds = 300,
  ) {
    const operation = {
      schema_version: "fenorium/platform-analytics-request/1",
      method_version: request.method_version,
      as_of: request.as_of,
      request,
      observations,
      price_observations: priceObservations,
      return_series: [],
      max_price_age_seconds: maxPriceAgeSeconds,
    };
    const response = await root.fetch("/v1/portfolio-analytics", {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": `wallet-${PORTFOLIO_SESSION_ID}-${generation}-${request.wallet.toLowerCase()}`,
      },
      body: JSON.stringify(operation),
    });
    let envelope = null;
    try { envelope = await response.json(); } catch (_) { envelope = null; }
    if (!response.ok) throw makeCodeError(envelope?.error || "PORTFOLIO_REQUEST_FAILED", envelope?.message || `portfolio request failed (HTTP ${response.status})`);
    if (!validPlatformEnvelope(envelope) || !object(envelope.data)) {
      throw makeCodeError("INVALID_PORTFOLIO_RESPONSE", "portfolio HTTP 200 response failed its shape and execution-consistency checks");
    }
    return P.portfolioAnalyticsView(envelope.data);
  }

  // How stale a declared demo price may be. It is derived from the row's own declared validity,
  // not from a clock: these prices are terms, and terms are good until they expire.
  //
  // This used to select rows through the purchasable-instrument map, so the moment the chain
  // carried a demo asset that was not for sale, no row matched, every asset fell back to the
  // 300-second limit meant for market data, and a book priced from a several-hour-old artifact
  // read UNKNOWN across the board.
  function portfolioPriceMaxAgeSeconds(universe, assets, asOf) {
    const fallback = 300;
    const requestedAt = Date.parse(asOf);
    if (!Number.isFinite(requestedAt) || !Array.isArray(assets) || !assets.length) return fallback;
    // An identity carries the kind derived from the row's category, so this has to go through
    // the same derivation the assets did. Assuming ERC20 here missed the note and the vault,
    // and one unmatched asset drops every asset to the market fallback.
    const rowsByIdentity = new Map();
    for (const row of Array.isArray(universe) ? universe : []) {
      if (!isDeclaredDemoRow(row)) continue;
      const identity = P.registeredAssetIdentity?.(row);
      if (identity) rowsByIdentity.set(identity, row);
    }
    const selected = assets.map((asset) => rowsByIdentity.get(asset.identity));
    if (selected.some((row) => !row)) return fallback;
    const permittedAges = selected.map((row) => {
      const validUntil = Date.parse(row.price_valid_until);
      const observations = Array.isArray(row.price_observations) ? row.price_observations : [];
      const catalog = isTestnetReady() && testnetWorkspace()?.catalog?.instruments?.some((entry) => sameAddress(row.address, entry.instrument));
      if (!Number.isFinite(validUntil) || requestedAt > validUntil || observations.length < (catalog ? 1 : 2)) return null;
      const declared = observations.filter((source) => (
        String(source?.source_id || "").startsWith(catalog ? "DECLARED_TESTNET_CATALOG_" : "DECLARED_TESTNET_DEMO_")
        && String(source?.method_version || "").startsWith(catalog ? "declared-testnet-catalog-price/" : "declared-testnet-demo-price/")
      ));
      if (declared.length !== observations.length) return null;
      const ages = declared.map((source) => {
        const observedAt = Date.parse(source.observed_at);
        return Number.isFinite(observedAt) && observedAt <= validUntil
          ? Math.floor((validUntil - observedAt) / 1000)
          : null;
      });
      return ages.some((age) => age === null) ? null : Math.min(...ages);
    });
    const maxAge = permittedAges.some((age) => age === null) ? null : Math.min(...permittedAges);
    return Number.isSafeInteger(maxAge) && maxAge >= fallback ? maxAge : fallback;
  }

  // Optional view-time evidence. The deadline bounds providers that never settle; failures
  // are absent observations. No result is cached or reused across a wallet/chain capture.
  async function readLivePriceObservations(provider, chainId, assets, asOf, config) {
    const registry = config?.price_sources?.chains?.[String(chainId)];
    if (!registry || !provider?.request || !Prices.chainlinkObservation) return [];
    let stopped = false, timer;
    const observations = [];
    const call = async (to, data) => {
      if (stopped || !configuredAddress(to) || !data) throw new Error("Unavailable price call");
      return provider.request({method: "eth_call", params: [{to, data}, "latest"]});
    };
    // The quote token's own dollar price, read once per token however many pairings use it. A
    // pool answers in its quote token; without a measured price for that token there is no way
    // to state the result in dollars, and so no observation.
    const quoteUsd = new Map();
    const quoteUsdFixed = (quoteToken, decimals) => {
      const key = String(quoteToken || "").toLowerCase();
      if (!quoteUsd.has(key)) quoteUsd.set(key, (async () => {
        const feed = registry.feeds?.[key];
        if (!feed) return null;
        try {
          const [round, places, description] = await Promise.all([
            call(feed.address, "0xfeaf968c"), call(feed.address, "0x313ce567"), call(feed.address, "0x7284e416"),
          ]);
          const observation = Prices.chainlinkObservation({
            asset: {chain_id: chainId, kind: "erc20", address: quoteToken, decimals}, feed, asOf,
            round: Prices.decodeLatestRoundData(round), decimals: Prices.decodeDecimals(places),
            description: Prices.decodeDescription(description),
          });
          return observation ? observation.price_usd_fixed : null;
        } catch (_) { return null; }
      })());
      return quoteUsd.get(key);
    };
    const work = async () => {
      const jobs = [];
      for (const asset of assets || []) {
        if (String(asset.chain_id) !== String(chainId)) continue;
        if (!configuredAddress(asset.address)) continue;
        const token = asset.address.toLowerCase();
        const feed = registry.feeds?.[token.toLowerCase()];
        if (feed) jobs.push(async () => {
          const [round, decimals, description] = await Promise.all([
            call(feed.address, "0xfeaf968c"), call(feed.address, "0x313ce567"), call(feed.address, "0x7284e416"),
          ]);
          const observation = Prices.chainlinkObservation({asset, feed, asOf,
            round: Prices.decodeLatestRoundData(round), decimals: Prices.decodeDecimals(decimals),
            description: Prices.decodeDescription(description)});
          if (!stopped && observation) observations.push(observation);
        });
        // A chain may carry more than one pairing: an asset that is itself the quote of one pool
        // (USDC against USDC) has no pool of its own, and is quoted against another stablecoin.
        const pairings = Array.isArray(registry.uniswap_v3) ? registry.uniswap_v3
          : (registry.uniswap_v3 ? [registry.uniswap_v3] : []);
        for (const twap of pairings) {
        if (twap?.assets?.some(address => String(address).toLowerCase() === token.toLowerCase())) jobs.push(async () => {
          const quotePrice = await quoteUsdFixed(twap.quote?.address, twap.quote?.decimals);
          if (!quotePrice || stopped) return;
          // Every configured tier is read, not just the first that answers. An initialised pool
          // with no liquidity answers `observe` as readily as a deep one, and on Ethereum the
          // 0.01% tier is tried first, so taking the first answer lets a pool that has never
          // traded speak for the market. The deepest one over the window wins instead.
          let best = null;
          for (const fee of twap.fee_tiers || []) {
            if (stopped) return;
            try {
              const pool = Prices.decodeGetPool(await call(twap.factory, Prices.encodeGetPool(token, twap.quote?.address, fee)));
              if (!pool || /^0x0{40}$/i.test(pool)) continue;
              const decoded = Prices.decodeObserve(await call(pool, Prices.encodeObserve(twap.window_seconds)));
              const liquidity = Prices.timeWeightedLiquidity(
                decoded?.secondsPerLiquidityCumulativeX128?.[0],
                decoded?.secondsPerLiquidityCumulativeX128?.[1], twap.window_seconds);
              const observation = Prices.twapObservation({asset, quote: twap.quote, pool, asOf,
                cumulatives: decoded, window: twap.window_seconds, quoteUsdFixed: quotePrice,
                quoteSource: Prices.SOURCE_CHAINLINK, minimumLiquidity: twap.minimum_liquidity});
              if (observation && liquidity !== null && (best === null || liquidity > best.liquidity)) {
                best = {observation, liquidity};
              }
            } catch (_) { /* Missing pool or insufficient observation history: try next tier. */ }
          }
          if (best && !stopped) observations.push(best.observation);
        });
        }
      }
      if (!jobs.length || stopped) return [];
      if (BigInt(await provider.request({method: "eth_chainId"})) !== BigInt(chainId) || stopped) return [];
      await pooled(jobs, 6, async job => { try { await job(); } catch (_) { /* No evidence. */ } });
      if (stopped || BigInt(await provider.request({method: "eth_chainId"})) !== BigInt(chainId)) return [];
      return observations.sort((a, b) => a.identity.localeCompare(b.identity) || a.independence_key.localeCompare(b.independence_key));
    };
    try {
      // A timed-out scan cannot establish its final chain, so discard its partial evidence.
      return await Promise.race([work().catch(() => []), new Promise(resolve => {
        timer = setTimeout(() => { stopped = true; resolve([]); }, 8000);
      })]);
    } finally { stopped = true; clearTimeout(timer); }
  }

  // Quotes have their own generation: instrument changes invalidate them without affecting NAV.
  let routeGeneration = 0, routeRows = [], routeMessages = [], routeExpiryTimer;
  let depositRefreshTimer = null;
  let depositRefresh = null;
  let routeQuotesServed = false;
  let routeSelectionGeneration = 0, routeSelectionMessage = "", appliedSwitch = null;
  // One executable route at a time, for the preview row the user chose. The preview is quoted from
  // a neutral address and cannot be signed by anyone; this one is fetched by the server for the
  // connected wallet, on request only, and it is the only kind that can become an instruction.
  let execution = null, executionGeneration = 0;
  let depositEntryError = null, depositSelectionGeneration = 0, venuePositionsGeneration = 0;
  const positionExitStates = new Map();
  // Submitted exits belong to the account and chain-qualified position, for this page session.
  const submittedPositionExits = new Map();
  function getPositionExitState(key) {
    const account = String(state.wallet.account || "").toLowerCase();
    const submitted = submittedPositionExits.get(`${account}:${key}`);
    return submitted ? {...submitted, wallet_generation: state.walletGeneration,
      wallet_chain_id: state.wallet.chainId, universe: state.universe} : positionExitStates.get(key);
  }
  function setPositionExitState(key, entry) {
    const identity = `${entry.account}:${key}`;
    if (entry.submission_unresolved || (entry.hash && entry.pending_receipt)) submittedPositionExits.set(identity, entry);
    else submittedPositionExits.delete(identity);
    if (entry.account === String(state.wallet.account || "").toLowerCase()) positionExitStates.set(key, entry);
  }

  let positionExitSequence = 0;
  let portfolioInitializing = false;
  // Submission ownership survives row, instrument and wallet changes.
  const routeSubmissions = [];
  const purchasedRouteInstructions = new Set();
  let salePositions = new Map();
  const instrumentIdentity = row => JSON.stringify([row?.chain_id, String(row?.address || "").toLowerCase(),
    ...(row?.address == null ? [row?.venue, row?.name, row?.maturity, row?.underlying_address, row?.market_address,
      row?.rate_bps, row?.verdict] : [])]);
  const routeScopeMatches = (tx, account = state.wallet.account, chain = state.wallet.chainId) =>
    tx.account === String(account || "").toLowerCase() && tx.chain_id === chain;
  const routeLocked = e => routeSubmissions.some(tx => tx.unresolved &&
    (routeScopeMatches(tx, e.account, e.chain_id) || (tx.account === String(e.account).toLowerCase()
      && tx.instrument_identity === instrumentIdentity(e.instrument))));
  const routeRequestIsCurrent = capture => executionIsCurrent(capture) && execution.request === capture.request;
  const routeInstructionKey = e => {
    const tx = e.data?.transaction_request;
    return tx ? JSON.stringify([String(e.account).toLowerCase(), e.chain_id,
      String(tx.to).toLowerCase(), tx.data, "0x" + BigInt(tx.value || "0").toString(16)]) : null;
  };
  const routeFresh = e => {
    const expiry = e.kind === "lending" ? e.data?.expires_at : e.data?.route?.expires_at;
    return Number.isSafeInteger(expiry) && Math.floor(Date.now() / 1000) < expiry;
  };
  const EXPLORERS = Object.freeze(Object.fromEntries(REGISTRY_CHAINS
    .filter((chain) => chain.roles.includes("portfolio") && chain.explorer).map((chain) => [chain.chain_id, chain.explorer])));

  function positionSettlement(row) {
    return state.universe.find(asset => asset.category === "settlement" && asset.chain_id === row?.chain_id
      && configuredAddress(row?.underlying_address)
      && String(asset.address).toLowerCase() === row.underlying_address.toLowerCase());
  }

  function positionMaturity(row) {
    if (["Tenor", "Morpho Midnight"].includes(row?.venue) && Number.isSafeInteger(row.maturity_timestamp) && row.maturity_timestamp >= 0) return row.maturity_timestamp;
    if (typeof row?.maturity !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(row.maturity)) return null;
    const ms = Date.parse(row.maturity + "T00:00:00Z");
    return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === row.maturity ? ms / 1000 + (["Tenor", "Morpho Midnight"].includes(row.venue) ? 86399 : 0) : null;
  }

  function depositEligibility(row) {
    const pay = positionSettlement(row);
    let error = null;
    if (row?.venue !== "Exactly") error = "the venue is not Exactly";
    else if (row.address !== null) error = "this is not an addressless fixed position";
    else if (row.verdict !== "BID") error = "its Fenorium Score is below 60";
    else if (!configuredAddress(row.underlying_address)) error = "the deposit asset address is missing";
    else if (!configuredAddress(row.market_address)) error = "the market address is missing";
    else if (!pay) error = "the deposit asset has no settlement row on this chain";
    else if (Routes.formatAmount?.("0", pay.decimals) == null || !pay.symbol) error = "the settlement asset precision or symbol is missing";
    else if (positionMaturity(row) === null) error = "the maturity date is missing or invalid";
    else if (!Number.isSafeInteger(row.rate_bps)) error = "the priced fixed rate is missing";
    return {pay, error};
  }

  function depositEntry(escape) {
    const row = state.selected;
    if (["Tenor", "Morpho Midnight"].includes(row?.venue)) return lendingEntry(escape);
    if (!row || (row.address != null && row.venue !== "Exactly")) return "";
    const {error} = depositEligibility(row);
    if (error) return `<p>This position cannot be deposited into from here: ${escape(error)}.</p>`;
    return `<section class="panel"><h3>Exactly fixed deposit</h3><p>Maturity ${escape(row.maturity)} · Priced fixed rate ${escape(row.rate_bps / 100)}%</p>`
      + '<button type="button" class="button" data-position-deposit>Deposit</button>'
      + (depositEntryError ? `<p class="route-error">${escape(depositEntryError)}</p>` : "") + '</section>';
  }

  async function selectDepositPosition() {
    if (isSimulatedRow(state.selected)) return null;
    const instrument = state.selected, {pay, error} = depositEligibility(instrument);
    if (error) return;
    const generation = ++depositSelectionGeneration, walletGeneration = state.walletGeneration;
    const account = state.wallet.account, chainId = state.wallet.chainId, provider = state.provider;
    const current = () => generation === depositSelectionGeneration && walletGeneration === state.walletGeneration
      && state.selected === instrument && state.wallet.account === account && state.wallet.chainId === chainId;
    execution = null; executionGeneration += 1;
    depositEntryError = null;
    try {
      if (!account || !provider?.request) throw new Error("Connect a wallet to deposit.");
      if (chainId !== instrument.chain_id) throw new Error(`Switch your wallet to ${chainName(instrument.chain_id)} to deposit.`);
      const chain = await provider.request({method: "eth_chainId"});
      if (!current()) return;
      if (normalizeChainId(chain) !== chainId) throw new Error(`Switch your wallet to ${chainName(instrument.chain_id)} to deposit.`);
      const raw = await provider.request({method: "eth_call", params: [{to: pay.address, data: encodeBalanceOf(account)}, "latest"]});
      if (!current()) return;
      if (typeof raw !== "string" || !/^0x[0-9a-f]{64}$/i.test(raw)) throw new Error("The deposit asset balance could not be read.");
      const balanceRaw = BigInt(raw).toString(), balance = Routes.formatAmount(balanceRaw, pay.decimals);
      execution = {kind: "deposit", generation: ++executionGeneration, wallet_generation: walletGeneration,
        account, chain_id: chainId, instrument: {...instrument}, instrument_symbol: pay.symbol,
        pay: {...pay}, symbol: pay.symbol, balance_raw: balanceRaw, balance_display: balance,
        amount_input: balance, phase: "IDLE", view: null, data: null, error: null,
        signable: false, needs_approval: false, sending: false};
    } catch (failure) {
      if (current()) depositEntryError = boundedAdapterError(failure);
    } finally { if (current()) renderRouteQuotes(); }
  }

  function lendingEligibility(row) {
    const pay = positionSettlement(row);
    let error = null;
    if (!["Tenor", "Morpho Midnight"].includes(row?.venue) || row.address != null) error = "this is not a Midnight lending market";
    else if (row.verdict === "NO BID") error = "its Fenorium Score is below 60";
    else if (!configuredAddress(row.underlying_address)) error = "the loan token address is missing";
    else if (!pay || !pay.symbol || Routes.formatAmount("0", pay.decimals) == null) error = "the loan token settlement units are missing";
    else if (positionMaturity(row) === null || positionMaturity(row) <= Date.now() / 1000) error = "the maturity must be a valid date in the future";
    return {pay, error};
  }

  function lendingEntry(escape) {
    if (execution?.kind === "lending") return "";
    const row = state.selected, {error} = lendingEligibility(row);
    if (error) return `<p>This lending order is unavailable: ${escape(error)}.</p>`;
    const rateLabel = row.rate_kind === "indicative" ? "indicative — best lender bid"
      : row.rate_kind === "executable" ? "lend now — best borrower ask" : null;
    return `<section class="panel"><h3>${escape(row.name)} · matures ${escape(row.maturity)}</h3>`
      + (Number.isSafeInteger(row.rate_bps) && rateLabel ? `<p>${escape(row.rate_bps / 100)}% · ${rateLabel}</p>`
        : !Number.isSafeInteger(row.rate_bps) ? '<p>No published rate for this market; set the rate you will lend at.</p>' : '')
      + '<button type="button" class="button" data-lending-select>Lend</button>'
      + (depositEntryError ? `<p class="route-error">${escape(depositEntryError)}</p>` : "") + '</section>';
  }

  async function selectLendingPosition() {
    const instrument = state.selected, {pay, error} = lendingEligibility(instrument);
    if (error) return;
    const generation = ++depositSelectionGeneration, walletGeneration = state.walletGeneration;
    const account = state.wallet.account, chainId = state.wallet.chainId, provider = state.provider;
    const current = () => generation === depositSelectionGeneration && walletGeneration === state.walletGeneration
      && state.selected === instrument && state.wallet.account === account && state.wallet.chainId === chainId;
    execution = null; executionGeneration += 1; depositEntryError = null;
    try {
      if (!account || !provider?.request) throw new Error("Connect a wallet to lend.");
      if (chainId !== instrument.chain_id || normalizeChainId(await provider.request({method: "eth_chainId"})) !== chainId) {
        throw new Error(`Switch your wallet to ${chainName(instrument.chain_id)} to lend.`);
      }
      if (!current()) return;
      const raw = await provider.request({method: "eth_call", params: [{to: pay.address, data: encodeBalanceOf(account)}, "latest"]});
      if (!current()) return;
      if (typeof raw !== "string" || !/^0x[0-9a-f]{64}$/i.test(raw)) throw new Error("The loan token balance could not be read.");
      const balance = Routes.formatAmount(BigInt(raw).toString(), pay.decimals);
      execution = {kind: "lending", generation: ++executionGeneration, wallet_generation: walletGeneration,
        account, chain_id: chainId, instrument: {...instrument}, instrument_symbol: pay.symbol,
        pay: {...pay}, symbol: pay.symbol, balance_raw: BigInt(raw).toString(), balance_display: balance,
        amount_input: balance, apr_input: Number.isSafeInteger(instrument.rate_bps) ? (instrument.rate_bps / 100).toFixed(2) : "",
        expiry_seconds: 86400, phase: "IDLE", view: null, data: null, error: null,
        signable: false, needs_approval: false, sending: false};
    } catch (failure) { if (current()) depositEntryError = boundedAdapterError(failure); }
    finally { if (current()) renderRouteQuotes(); }
  }

  const lendingDate = seconds => Number.isSafeInteger(Number(seconds)) && Number(seconds) > 0
    && Number(seconds) < 8640000000000 ? new Date(Number(seconds) * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC") : "Unknown";
  const lendingReasons = data => (data?.reason_codes || []).map(code => ({code, text: Routes.REASON_TEXT[code] || code}));
  const lendingMinimums = new Map();
  const lendingMarketKey = e => JSON.stringify([e.chain_id, e.instrument.name, e.pay.address.toLowerCase()]);
  const policyMinimum = issue => {
    const raw = issue?.details?.min_assets;
    return issue?.rule === "min_offer_assets_usd"
      && ((typeof raw === "string" && /^[0-9]+$/.test(raw)) || (Number.isSafeInteger(raw) && raw >= 0)) ? String(raw) : null;
  };

  function lendingPolicyLine(issue, pay) {
    const rule = issue?.rule, minimum = policyMinimum(issue);
    const formatted = minimum === null ? null : Routes.formatAmount(minimum, pay.decimals);
    if (rule === "min_offer_assets_usd" && formatted !== null) return `The book's minimum is ${formatted} ${pay.symbol}.`;
    if (["maturity", "rcf_threshold", "market_triplet", "loan_token", "collateral_token", "oracle",
      "collateral_lltv", "max_lif", "max_collaterals"].includes(rule)) return "This market is not listed on the public order book.";
    if (["min_tick", "max_tick", "tick_spacing"].includes(rule)) return "This rate is outside what the book accepts.";
    if (rule === "min_duration") return "The order must stay open longer.";
    return `The order does not meet the book's rules (${rule}).`;
  }

  function rememberLendingMinimum(e, data) {
    if (data.status !== "REJECTED" || !data.reason_codes?.includes("MEMPOOL_POLICY")) return;
    for (const issue of data.policy_issues || []) {
      const minimum = policyMinimum(issue);
      if (minimum !== null) lendingMinimums.set(lendingMarketKey(e), minimum);
    }
  }

  function lendingMinimumHint(e) {
    const minimum = lendingMinimums.get(lendingMarketKey(e));
    const raw = /^0+(?:\.0+)?$/.test(e.amount_input.trim()) ? "0" : Routes.parseAmount(e.amount_input, e.pay.decimals);
    if (minimum == null || raw == null || BigInt(raw) >= BigInt(minimum)) return null;
    return lendingPolicyLine({rule: "min_offer_assets_usd", details: {min_assets: minimum}}, e.pay);
  }

  const sameJsonValue = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b)
      && a.length === b.length && a.every((item, i) => sameJsonValue(item, b[i]));
    if (!object(a) || !object(b)) return false;
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every(key => Object.prototype.hasOwnProperty.call(b, key) && sameJsonValue(a[key], b[key]));
  };

  function lendingPanel(escape) {
    const e = execution, locked = e.sending || routeLocked(e), data = e.data;
    const lines = [`<div class="route-execution"><h3>${escape(e.instrument.name)} · matures ${escape(e.instrument.maturity)}</h3>`,
      `<p>Fenorium's server builds the order for this wallet; your wallet signs it; your ${escape(e.symbol)} leaves only when a borrower takes the order.</p>`,
      `<label>Amount of ${escape(e.symbol)} <input type="text" inputmode="decimal" data-route-amount value="${escape(e.amount_input)}"></label> `,
      `<label>APR % <input type="text" inputmode="decimal" data-lending-apr value="${escape(e.apr_input)}"></label> `,
      `<label>Expiry <select data-lending-expiry>${[1, 3, 7].map(days => `<option value="${days * 86400}" ${e.expiry_seconds === days * 86400 ? "selected" : ""}>${days} day${days === 1 ? "" : "s"}</option>`).join("")}</select></label> `,
      `<button type="button" class="button" data-route-request ${locked || e.phase === "LOADING" ? "disabled" : ""}>Prepare lending order</button>`];
    if (!Number.isSafeInteger(e.instrument.rate_bps)) lines.push('<p>No published rate for this market; set the rate you will lend at.</p>');
    if (e.phase === "LOADING") lines.push('<p>Preparing your lending order…</p>');
    const minimumHint = lendingMinimumHint(e);
    if (minimumHint && !(e.publish_data || data)?.reason_codes?.includes("MEMPOOL_POLICY")) {
      lines.push(`<p class="route-error">${escape(minimumHint)}</p>`);
    }
    if (e.error && e.error !== minimumHint) lines.push(`<p class="route-error">${escape(e.error)}</p>`);
    if (data) {
      if (data.status === "LENDING_ORDER_READY") lines.push(`<p>Lend at ${escape(data.apr_bps / 100)}% until ${escape(lendingDate(data.expires_at))} · matures ${escape(e.instrument.maturity)}</p>`);
      const refusal = e.publish_data || data, reasons = lendingReasons(refusal);
      for (const reason of reasons) {
        if (["MEMPOOL_POLICY", "POLICY_UNAVAILABLE"].includes(reason.code)) lines.push(`<p>${escape(reason.text)}</p>`);
        else lines.push(`<p><code>${escape(["NO_BID", "NO_QUOTE"].includes(reason.code) ? U.stateLabel(reason.code) : reason.code)}</code> ${escape(reason.text)}</p>`);
        if (reason.code === "MEMPOOL_POLICY") {
          for (const issue of refusal.policy_issues || []) lines.push(`<p>${escape(lendingPolicyLine(issue, e.pay))}</p>`);
        }
      }
      if (e.signable) {
        if (e.needs_reset) lines.push(`<button type="button" class="button" data-route-reset ${locked ? "disabled" : ""}>Reset allowance to 0</button> `);
        if (e.needs_approval) lines.push(`<button type="button" class="button" data-route-approve ${locked || e.needs_reset ? "disabled" : ""}>Approve ${escape(e.amount_input)} ${escape(e.symbol)}</button> `);
        if (e.needs_authorization) {
          lines.push('<p>One-time permission on Morpho Midnight for Morpho\'s order checker (EcrecoverRatifier). It can only confirm orders you signed; it cannot move your funds. Without it, nobody can fill your order.</p>');
          lines.push(`<button type="button" class="button" data-lending-authorize ${locked || e.needs_reset || e.needs_approval ? "disabled" : ""}>Authorize order checker</button> `);
        }
        if (e.publish_data?.status === "LENDING_ORDER_PUBLISHABLE") {
          lines.push(`<button type="button" class="button" data-route-buy ${locked || e.needs_approval || e.published ? "disabled" : ""}>Publish order</button>`);
        } else lines.push(`<button type="button" class="button" data-lending-sign ${locked || e.needs_approval || e.needs_authorization ? "disabled" : ""}>Sign order</button>`);
      } else if (e.published) lines.push('<p>Lending order submitted. Its receipt is shown below.</p>');
      else lines.push('<p>Signing is not available for this order. The reasons above are why; nothing has been sent.</p>');
    }
    return lines.join("") + '</div>';
  }

  function invalidateLendingTerms() {
    execution.request = (execution.request || 0) + 1;
    execution.amount_changed = true; execution.signable = false; execution.phase = "IDLE";
    execution.publish_data = null; execution.data = null;
    execution.error = "Terms changed; prepare a new lending order before signing.";
  }

  function lendingOperation(account, chainId, fields = {}) {
    return {schema_version: "fenorium/platform-lending-order-request/1", method_version: "lending-order/1",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), wallet: account.toLowerCase(), chain_id: chainId, ...fields};
  }

  let lendingPostAttempt = 0;

  async function lendingPost(path, operation, key) {
    key = key || `${path.slice(1)}-${PORTFOLIO_SESSION_ID}-${operation.root || "list"}-${++lendingPostAttempt}`;
    return boundedRouteRead(async signal => {
      const response = await root.fetch("/v1/lending-orders" + path, {method: "POST", cache: "no-store", signal,
        headers: {Accept: "application/json", "Content-Type": "application/json", "Idempotency-Key": key},
        body: JSON.stringify(operation)});
      const envelope = await response.json();
      if (!response.ok) throw makeCodeError(envelope?.error || "ORDERS_UNAVAILABLE", envelope?.message || "Lending orders could not be read.");
      const data = envelope?.data;
      if (!validPlatformEnvelope(envelope) || !object(data) || data.route_provenance !== "SERVER_BUILT"
        || !sameAddress(data.wallet, operation.wallet) || data.pay_chain_id !== operation.chain_id
        || typeof data.authorised !== "boolean" || data.broadcast_enabled !== data.authorised
        || (data.authorised && !envelope.execution_enabled)
        || !Array.isArray(data.reason_codes) || !data.reason_codes.every(code => typeof code === "string")) {
        throw new Error("The lending response does not match this wallet and chain.");
      }
      applyDeploymentState(envelope);
      return data;
    }, LENDING_READ_MS);
  }

  // The universe exposes a UTC date, while Midnight can mature within that day. Bind the
  // offer to the resolved timestamp and the selected date without inventing a midnight expiry.
  function validateLendingResponse(e, data) {
    const offer = data.offer, market = offer?.market, p = data.instrument, approval = data.unsigned_approval_request;
    const ratifierAuthorizationRequired = data.reason_codes?.includes("RATIFIER_AUTHORIZATION_REQUIRED");
    if (data.route_provenance !== "SERVER_BUILT" || !sameAddress(data.wallet, e.account)
      || data.pay_chain_id !== e.chain_id || !sameAddress(data.pay_address, e.pay.address)
      || !object(offer) || !object(market) || !sameAddress(offer.maker, e.account) || offer.buy !== true
      || !sameAddress(market.loanToken, e.pay.address) || String(market.chainId) !== String(e.chain_id)
      || !configuredAddress(market.midnight) || !object(p) || p.maturity !== e.instrument.maturity
      || p.chain_id !== e.chain_id || p.venue !== e.instrument.venue || p.name !== e.instrument.name
      || (e.instrument.market_id != null && String(data.market_id).toLowerCase() !== String(e.instrument.market_id).toLowerCase())
      || !Number.isSafeInteger(p.maturity_timestamp) || String(market.maturity) !== String(p.maturity_timestamp)
      || new Date(p.maturity_timestamp * 1000).toISOString().slice(0, 10) !== e.instrument.maturity
      || (e.instrument.maturity_timestamp != null && p.maturity_timestamp !== e.instrument.maturity_timestamp)
      || !sameAddress(p.underlying_address, e.pay.address) || p.underlying_decimals !== e.pay.decimals
      || p.underlying_symbol !== e.symbol || !sameAddress(p.market_address, market.midnight)
      || data.decimals !== e.pay.decimals || !sameAddress(data.loan_token, e.pay.address)
      || !Number.isSafeInteger(data.expires_at) || String(offer.expiry) !== String(data.expires_at)
      || data.expires_at > p.maturity_timestamp || BigInt(offer.maxAssets) !== BigInt(data.amount_raw)
      || data.amount_raw !== e.amount_raw || !/^0x[0-9a-f]{64}$/i.test(data.root || "")
      || !configuredAddress(data.mempool) || !Number.isSafeInteger(data.apr_bps) || data.apr_bps < 0
      || !object(data.typed_data) || String(data.typed_data.domain?.chainId) !== String(e.chain_id)
      || (data.signing_typed_data != null && (!object(data.signing_typed_data) || !sameJsonValue(data.signing_typed_data, data.typed_data)))
      || (data.authorised === true && (ratifierAuthorizationRequired
        ? (object(data.signing_typed_data) || data.reason_codes.some(code => code !== "RATIFIER_AUTHORIZATION_REQUIRED"))
        : (!object(data.signing_typed_data) || data.reason_codes.length)))) {
      throw new Error("The lending response does not match this wallet, position and amount.");
    }
    if (!object(approval) || approval.chainId !== e.chain_id || !sameAddress(approval.from, e.account)
      || !sameAddress(approval.to, e.pay.address) || !sameAddress(approval.spender, market.midnight)
      || approval.amount !== data.amount_raw || BigInt(approval.value) !== 0n
      || String(approval.data).toLowerCase() !== "0x095ea7b3" + encodeAddress(market.midnight).toLowerCase() + encodeUint256(data.amount_raw)
      || (data.authorised && !sameJsonValue(data.approval_request, approval))) {
      throw new Error("The lending approval does not approve Midnight for the exact amount.");
    }
    const midnightByChain = {1: {midnight: "0x471686c42792f93528b000bef54bc10e3aa2045f", ratifier: "0xac439c81caa6ef4c7b7e8f0110f8ce63a4b6d43e"},
      8453: {midnight: "0xadedd8ab6de832766fedf0fac4992e5c4d3ea18a", ratifier: "0xd6e70365c8e8dda9a4ca662c07bbe663b017755e"},
      5042: {midnight: "0x208786922be56fde2d1fa60e6b9ec5d723e8d7b0", ratifier: "0xa3b53ade6668b6cec03a9e56a993b47034f43715"}};
    const deployment = midnightByChain[e.chain_id], ratifier = deployment?.ratifier;
    const auth = data.authorization_request, unsignedAuth = data.unsigned_authorization_request;
    const authRequired = data.reason_codes.includes("RATIFIER_AUTHORIZATION_REQUIRED");
    const authData = "0xb78212ab" + encodeAddress(ratifier) + encodeUint256("1") + encodeAddress(e.account);
    if (!deployment || !sameAddress(market.midnight, deployment.midnight) || !sameAddress(offer.ratifier, ratifier)
      || (authRequired !== (unsignedAuth != null))
      || (unsignedAuth != null && (!object(unsignedAuth) || unsignedAuth.chainId !== e.chain_id || !sameAddress(unsignedAuth.from, e.account)
        || !sameAddress(unsignedAuth.to, deployment.midnight) || unsignedAuth.value !== "0" || !sameAddress(unsignedAuth.authorized, ratifier)
        || String(unsignedAuth.data).toLowerCase() !== authData.toLowerCase()))
      || (data.authorised === true && authRequired !== (auth != null))
      || (auth != null && (!sameJsonValue(auth, unsignedAuth) || data.authorised !== true))) {
      throw new Error("The Morpho order checker authorization does not match this wallet and Midnight deployment.");
    }
    const reset = data.unsigned_approval_reset_request;
    if (reset != null) {
      const expected = "0x095ea7b3" + encodeAddress(market.midnight).toLowerCase() + encodeUint256("0");
      if (!object(reset) || reset.chainId !== approval.chainId || !sameAddress(reset.from, approval.from)
        || !sameAddress(reset.to, approval.to) || !sameAddress(reset.spender, market.midnight)
        || reset.amount !== "0" || BigInt(reset.value || "0") !== 0n
        || String(reset.data).toLowerCase() !== expected
        || (data.authorised && !sameJsonValue(data.approval_reset_request, reset))) {
        throw new Error("The lending allowance reset does not target Midnight for zero.");
      }
    } else if (data.approval_reset_request != null) throw new Error("Unexpected lending allowance reset instruction.");
  }

  async function requestLendingOrder(amountText) {
    if (!executionIsCurrent(execution) || execution.sending || lendingOrdersBusy || routeLocked(execution)) return;
    const e = execution, raw = Routes.parseAmount(String(amountText ?? ""), e.pay.decimals);
    e.request = (e.request || 0) + 1;
    e.amount_input = String(amountText ?? "").trim(); e.amount_raw = raw;
    e.data = null; e.view = null; e.publish_data = null; e.signable = false; e.needs_approval = false; e.amount_changed = false; e.published = false;
    const apr = /^([0-9]+)(?:\.([0-9]{1,2}))?$/.exec(String(e.apr_input).trim());
    const bps = apr ? Number(BigInt(apr[1]) * 100n + BigInt((apr[2] || "").padEnd(2, "0"))) : null;
    if (!raw || BigInt(raw) > BigInt(e.balance_raw)) e.error = "Enter a positive amount within your wallet balance.";
    else if (lendingMinimumHint(e)) e.error = lendingMinimumHint(e);
    else if (bps === null || !Number.isSafeInteger(bps)) e.error = Routes.REASON_TEXT.NO_RATE;
    else if (![86400, 259200, 604800].includes(e.expiry_seconds)) e.error = "Choose an expiry of 1, 3 or 7 days.";
    else e.error = null;
    if (e.error) { renderRouteQuotes(); return; }
    e.phase = "LOADING";
    const capture = {...e};
    const terms = {venue: e.instrument.venue, instrument: e.instrument.name, amount_raw: raw, apr_bps: bps, expiry_seconds: e.expiry_seconds};
    if (!e.retry || !sameJsonValue(e.retry.terms, terms)) e.retry = {terms,
      operation: lendingOperation(e.account, e.chain_id, terms), key: `order-${PORTFOLIO_SESSION_ID}-${e.generation}-${e.request}`};
    const retry = e.retry;
    renderRouteQuotes();
    try {
      const data = await lendingPost("", retry.operation, retry.key);
      if (!routeRequestIsCurrent(capture)) return;
      if (!["LENDING_ORDER_READY", "REJECTED", "ROUTE_UNAVAILABLE", "ORDERS_UNAVAILABLE"].includes(data.status)) throw new Error("The server did not return a lending order.");
      if (data.status === "LENDING_ORDER_READY") validateLendingResponse(e, data);
      rememberLendingMinimum(e, data);
      e.data = data; e.retry = null;
      await refreshLendingAllowance(capture);
    } catch (error) { if (routeRequestIsCurrent(capture)) { e.signable = false; e.error = boundedAdapterError(error); } }
    finally { if (routeRequestIsCurrent(capture)) { e.phase = "IDLE"; renderRouteQuotes(); } }
  }

  async function refreshLendingAllowance(capture) {
    if (!routeRequestIsCurrent(capture)) return;
    const e = execution, data = e.data;
    e.signable = false;
    e.needs_approval = false; e.needs_reset = false; e.needs_authorization = false;
    const generation = e.allowance_generation = (e.allowance_generation || 0) + 1;
    const authRequired = data?.reason_codes?.includes("RATIFIER_AUTHORIZATION_REQUIRED");
    if (data?.status !== "LENDING_ORDER_READY" || data.authorised !== true
      || (!authRequired && !object(data.signing_typed_data)) || (authRequired && !object(data.authorization_request))
      || !mainnetExecutionOpen() || !routeFresh(e) || e.amount_changed || e.published
      || Routes.parseAmount(e.amount_input, e.pay.decimals) !== e.amount_raw) return;
    validateLendingResponse(e, data);
    const allowance = await readAllowance(e.account, e.pay.address, data.offer.market.midnight);
    if (!routeRequestIsCurrent(capture) || generation !== e.allowance_generation) return;
    if (allowance === null) throw new Error("Allowance could not be read.");
    e.needs_approval = allowance < BigInt(e.amount_raw);
    e.needs_authorization = data.unsigned_authorization_request != null;
    const reset = data.unsigned_approval_reset_request;
    e.needs_reset = e.needs_approval && allowance > 0n && object(reset);
    e.signable = routeFresh(e) && !e.amount_changed;
  }

  function validateLendingTransaction(e, data, target) {
    const tx = data?.unsigned_transaction_request;
    if (!object(data) || data.authorised !== true || data.reason_codes.length || !sameAddress(data.wallet, e.account)
      || data.pay_chain_id !== e.chain_id || data.root !== e.data.root || !object(tx)
      || !sameAddress(tx.to, target) || !sameAddress(tx.from, e.account) || tx.chainId !== e.chain_id
      || BigInt(tx.value) !== 0n || typeof tx.data !== "string" || !/^0x(?:[0-9a-f]{2})+$/i.test(tx.data)
      || !sameJsonValue(data.transaction_request, tx)) throw new Error("The lending transaction does not match the server's authorised instruction.");
    return tx;
  }

  async function signLendingOrder() {
    const e = execution;
    if (e?.kind !== "lending" || !executionIsCurrent(e) || !e.signable || e.sending || e.needs_approval || e.needs_authorization
      || routeLocked(e) || lendingOrdersBusy || !mainnetExecutionOpen() || !object(e.data?.signing_typed_data) || !routeFresh(e)) return;
    const capture = {...e}, provider = state.provider;
    e.sending = true; e.error = null; e.publish_data = null;
    renderRouteQuotes();
    try {
      validateLendingResponse(e, e.data);
      const chain = await provider.request({method: "eth_chainId"});
      if (!routeRequestIsCurrent(capture) || !e.signable || !mainnetExecutionOpen() || !routeFresh(e)) return;
      if (normalizeChainId(chain) !== e.chain_id) throw new Error("Switch to the order's chain before signing.");
      const signature = await provider.request({method: "eth_signTypedData_v4", params: [e.account, JSON.stringify(e.data.signing_typed_data)]});
      if (!routeRequestIsCurrent(capture) || !routeFresh(e) || e.amount_changed) return;
      if (typeof signature !== "string" || !/^0x[0-9a-f]{130}$/i.test(signature)) throw new Error("The wallet must return a 65-byte signature.");
      let v = parseInt(signature.slice(130), 16);
      if (v === 0 || v === 1) v += 27;
      if (v !== 27 && v !== 28) throw new Error("The wallet returned an invalid signature recovery byte.");
      const signed = {v, r: signature.slice(0, 66).toLowerCase(), s: "0x" + signature.slice(66, 130).toLowerCase()};
      const identity = JSON.stringify([e.data.root, signed.v, signed.r, signed.s]);
      e.publish_attempts ||= new Map();
      if (!e.publish_attempts.has(identity)) {
        let hash = 0xcbf29ce484222325n;
        for (const char of identity) hash = BigInt.asUintN(64, (hash ^ BigInt(char.charCodeAt(0))) * 0x100000001b3n);
        e.publish_attempts.set(identity, {
          key: `publish-${PORTFOLIO_SESSION_ID}-${e.data.root}-${hash.toString(16).padStart(16, "0")}`,
          operation: lendingOperation(e.account, e.chain_id, {root: e.data.root, signature: signed})});
      }
      const attempt = e.publish_attempts.get(identity);
      const data = await lendingPost("/publish", attempt.operation, attempt.key);
      if (!routeRequestIsCurrent(capture)) return;
      rememberLendingMinimum(e, data);
      e.publish_data = data;
      if (data.status === "LENDING_ORDER_PUBLISHABLE") {
        if (!data.authorised) e.signable = false;
        else validateLendingTransaction(e, data, e.data.mempool);
      }
      else if (!["REJECTED", "ROUTE_UNAVAILABLE", "ORDERS_UNAVAILABLE"].includes(data.status)) throw new Error("The server did not return a publishable order.");
      if (data.status !== "LENDING_ORDER_PUBLISHABLE") e.error = data.reason_codes?.some(code => ["MEMPOOL_POLICY", "POLICY_UNAVAILABLE"].includes(code))
        ? null : lendingReasons(data).map(reason => reason.text).join(" ");
      void refreshLendingOrders();
    } catch (error) { if (routeRequestIsCurrent(capture)) { e.publish_data = null; e.error = boundedAdapterError(error); } }
    finally { e.sending = false; renderRouteQuotes(); }
  }

  async function sendLendingInstruction(kind) {
    const e = execution;
    if (!["reset", "approve", "authorize", "buy"].includes(kind) || !executionIsCurrent(e) || !e.signable || e.sending || lendingOrdersBusy || routeLocked(e)
      || !routeFresh(e) || e.amount_changed || (kind === "reset" ? !e.needs_reset : kind === "approve" ? !e.needs_approval || e.needs_reset : kind === "authorize" ? !e.needs_authorization || e.needs_reset || e.needs_approval : e.needs_approval || e.needs_authorization || e.published)) return;
    const capture = {...e};
    e.sending = true;
    try {
      validateLendingResponse(e, e.data);
      const request = kind === "reset" ? e.data.approval_reset_request : kind === "approve" ? e.data.approval_request
        : kind === "authorize" ? e.data.authorization_request : validateLendingTransaction(e, e.publish_data, e.data.mempool);
      if (kind === "authorize" && (!sameJsonValue(request, e.data.unsigned_authorization_request)
        || !object(request) || !sameAddress(request.from, e.account) || request.chainId !== e.chain_id
        || !sameAddress(request.to, e.data.offer.market.midnight) || request.value !== "0")) throw new Error("Invalid Morpho order checker authorization instruction.");
      if (kind === "reset") {
        const expected = "0x095ea7b3" + encodeAddress(e.data.offer.market.midnight).toLowerCase() + encodeUint256("0");
        if (!object(request) || request.amount !== "0" || !sameAddress(request.from, e.account)
          || !sameAddress(request.to, e.pay.address) || !sameAddress(request.spender, e.data.offer.market.midnight)
          || request.chainId !== e.chain_id || BigInt(request.value || "0") !== 0n || String(request.data).toLowerCase() !== expected
          || !sameJsonValue(request, e.data.unsigned_approval_reset_request)) throw new Error("Invalid lending allowance reset instruction.");
      }
      await submitLendingTransaction(e, request, kind === "reset" ? "Allowance reset" : kind === "approve" ? "Approval" : kind === "authorize" ? "Order checker authorization" : "Lending order",
        () => routeRequestIsCurrent(capture) && e.signable && routeFresh(e) && !e.amount_changed,
        () => { if (kind === "buy") e.published = true; },
        kind === "authorize" ? async () => {
          if (routeRequestIsCurrent(capture)) {
            e.sending = false;
            await requestLendingOrder(e.amount_input);
          }
        } : null);
    } catch (error) { if (routeRequestIsCurrent(capture)) e.error = boundedAdapterError(error); }
    finally { e.sending = false; renderRouteQuotes(); }
  }

  async function submitLendingTransaction(scope, request, label, current, onSent = () => {}, onConfirmed = null) {
    const provider = state.provider;
    const tx = {account: scope.account.toLowerCase(), chain_id: scope.chain_id, label,
      kind: label === "Approval" ? "approve" : label === "Allowance reset" ? "reset" : label === "Order checker authorization" ? "authorize" : "buy", status: "CHECKING", unresolved: true, hash: null, polling: false,
      instrument_identity: instrumentIdentity(scope.instrument), cross_chain: false, on_confirmed: onConfirmed};
    routeSubmissions.push(tx);
    let submitted = false;
    renderRouteQuotes();
    try {
      if (normalizeChainId(await provider.request({method: "eth_chainId"})) !== scope.chain_id
        || !current() || !mainnetExecutionOpen()) throw new Error("The wallet or order changed before submission.");
      if (!object(request) || !sameAddress(request.from, scope.account) || request.chainId !== scope.chain_id) throw new Error("The instruction does not match the connected wallet.");
      tx.status = "AWAITING_WALLET";
      submitted = true;
      const hash = await provider.request({method: "eth_sendTransaction", params: [{from: request.from, to: request.to,
        data: request.data, chainId: "0x" + request.chainId.toString(16), value: "0x" + BigInt(request.value).toString(16)}]});
      onSent();
      if (typeof hash !== "string" || !/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error("Wallet did not return a transaction hash; submission outcome is unknown.");
      tx.hash = hash; tx.status = "PENDING";
      void refreshLendingOrders();
      await checkRouteSubmission(hash);
    } catch (error) {
      if (!submitted || Number(error?.code) === 4001) routeSubmissions.splice(routeSubmissions.indexOf(tx), 1);
      else tx.status = "UNKNOWN";
      throw error;
    } finally { renderRouteQuotes(); }
  }

  let lendingOrdersGeneration = 0, lendingOrders = [], lendingOrdersScope = null, lendingOrdersBusy = false;
  async function refreshLendingOrders() {
    if (isTestnetBlocked() || (isTestnetReady() && viewedWallet().chainId !== BASE_SEPOLIA.chainId)) return null;
    const node = $("lending-orders"), generation = ++lendingOrdersGeneration;
    if (!node) return;
    // A watched wallet's orders are listed read-only (the list is by maker and needs no signature);
    // cancelling stays with the signing wallet.
    const viewed = viewedWallet();
    const account = viewed.account, chainId = viewed.chainId;
    const walletGeneration = state.walletGeneration, viewGeneration = state.viewGeneration;
    const current = () => generation === lendingOrdersGeneration && walletGeneration === state.walletGeneration
      && viewGeneration === state.viewGeneration && account === viewedWallet().account && chainId === viewedWallet().chainId;
    const escape = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
    const heading = viewed.connected ? "Your lending orders" : `Lending orders · ${escape(viewed.watched?.label || truncateAddress(account))}`;
    lendingOrders = []; lendingOrdersScope = null;
    node.hidden = !account || state.sampleVisible === true;
    node.innerHTML = account ? `<h2>${heading}</h2><p>Reading lending orders…</p>` : "";
    if (!account || !chainId) return;
    try {
      const data = await lendingPost("/list", lendingOperation(account, chainId));
      if (!current()) return;
      if (data.status !== "LENDING_ORDERS") throw new Error(lendingReasons(data).map(reason => reason.text).join(" ") || "Lending orders could not be read.");
      if (!Array.isArray(data.orders) || data.orders.some(order => !sameAddress(order.wallet, account) || order.chain_id !== chainId)) throw new Error("The orders do not belong to this wallet and chain.");
      lendingOrders = data.orders; lendingOrdersScope = {account, chainId, walletGeneration};
      const shownMarkets = new Set();
      const rows = data.orders.map(order => {
        const symbol = order.instrument?.underlying_symbol || positionSettlement({chain_id: chainId, underlying_address: order.loan_token})?.symbol || "Unknown asset";
        const amount = value => `${escape(P.formatRawAmount(String(value), order.decimals))} ${escape(symbol)}`;
        const unavailable = order.reason_codes?.includes("ONCHAIN_UNAVAILABLE");
        const status = order.canceled === true ? "Cancelled on chain" : order.expired ? "Expired"
          : unavailable ? "Chain state unavailable"
          : order.fillable === false ? "Not fillable"
          : "Signed";
        const fillabilityReason = (order.reason_codes || []).map(code => Routes.REASON_TEXT[code] || code).join(" ");
        const credit = P.asBigInt(order.market_position?.face_value);
        const debt = P.asBigInt(order.market_position?.debt);
        const market = `${chainId}:${String(order.market_id).toLowerCase()}`;
        let marketPosition = "";
        if (!shownMarkets.has(market) && (credit > 0n || debt > 0n)) {
          marketPosition = (credit > 0n ? `<p>${viewed.connected ? "your credit" : "credit"} in this market: ${amount(credit)} at maturity</p>` : "")
            + (debt > 0n ? `<p>${viewed.connected ? "you owe" : "owes"} ${amount(debt)}</p>` : "");
          shownMarkets.add(market);
        }
        const action = viewed.connected && !order.canceled && !order.expired && order.status === "SIGNED"
          ? `<button type="button" class="button" data-lending-cancel="${escape(order.root)}" ${lendingOrdersBusy ? "disabled" : ""}>Cancel on chain</button>` : "";
        return `<li><h3>${escape(symbol)} · ${escape(order.instrument?.maturity || "Unknown maturity")}</h3>`
          + `<p>${escape(order.apr_bps / 100)}% · ${amount(order.amount_raw)} · expires ${escape(lendingDate(order.expires_at))} · ${escape(status)}</p>`
          + (fillabilityReason ? `<p>${escape(fillabilityReason)}</p>` : "")
          + marketPosition + action + '</li>';
      });
      node.innerHTML = `<h2>${heading}</h2>` + (rows.length ? '<ul>' + rows.join("") + '</ul>'
        : `<p>No lending orders from this wallet on ${escape(routeChainName(chainId))}.</p>`);
    } catch (error) { if (current()) node.innerHTML = `<h2>${heading}</h2><p>${escape(boundedAdapterError(error))}</p>`; }
  }

  async function cancelLendingOrder(rootHash) {
    const scope = lendingOrdersScope, order = lendingOrders.find(item => item.root === rootHash);
    if (!scope || !order || lendingOrdersBusy || execution?.sending || order.canceled || order.expired
      || order.status !== "SIGNED") return;
    const current = () => scope.walletGeneration === state.walletGeneration && scope.account === state.wallet.account && scope.chainId === state.wallet.chainId;
    if (!current()) return;
    const e = {account: scope.account, chain_id: scope.chainId, instrument: order.instrument, data: {root: rootHash}};
    if (routeLocked(e)) return;
    lendingOrdersBusy = true;
    try {
      const data = await lendingPost("/cancel", lendingOperation(scope.account, scope.chainId, {root: rootHash}));
      if (!current()) return;
      if (data.status !== "CANCEL_READY") throw new Error(lendingReasons(data).map(reason => reason.text).join(" "));
      const tx = validateLendingTransaction(e, data, order.offer?.ratifier);
      await submitLendingTransaction(e, tx, "Cancel lending order", current);
    } catch (error) {
      if (current()) {
        const node = $("lending-orders");
        if (node) { const message = boundedAdapterError(error); node.textContent = "Your lending orders · " + message; }
      }
      return;
    } finally {
      lendingOrdersBusy = false;
      // The flag is shared: a list drawn for another wallet meanwhile rendered its buttons disabled.
      if (!current()) void refreshLendingOrders();
    }
    if (current()) await refreshLendingOrders();
  }

  async function refreshVenuePositions({preserveExitRequests = false} = {}) {
    if (isTestnetBlocked() || (isTestnetReady() && state.wallet.chainId !== BASE_SEPOLIA.chainId)) return null;
    const node = $("venue-positions"), generation = ++venuePositionsGeneration;
    if (!node) return;
    const viewed = viewedWallet();
    const record = viewedRecord();
    if (viewed.connected && onPayOnlyChain() && record?.source !== "server" && !record?.loading) { node.hidden = true; node.innerHTML = ""; return; }
    const account = viewed.account, chainId = viewed.chainId;
    const walletGeneration = state.walletGeneration, viewGeneration = state.viewGeneration, provider = state.provider, universe = state.universe;
    const current = () => generation === venuePositionsGeneration && walletGeneration === state.walletGeneration
      && viewGeneration === state.viewGeneration && account === viewedWallet().account && chainId === viewedWallet().chainId
      && universe === state.universe;
    node.hidden = !account || state.sampleVisible === true;
    node.innerHTML = account ? '<h2>Fixed-rate positions</h2><p>Reading positions…</p>' : "";
    if (!account || record?.loading || (portfolioInitializing && !record?.source)) return;
    const escape = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
    const assetAddress = position => {
      if (configuredAddress(position?.underlying_address)) return position.underlying_address;
      const match = /^eip155:[0-9]+\/erc20:(0x[0-9a-f]{40})$/i.exec(String(position?.underlying_asset_id || ""));
      return match?.[1] || null;
    };
    const exitMaturity = (position, row) => {
      if (Number.isSafeInteger(position?.maturity_timestamp)) return position.maturity_timestamp;
      if (Number.isSafeInteger(row?.maturity_timestamp)) return row.maturity_timestamp;
      if (position?.venue === "Exactly" && typeof position?.maturity === "string" && /^\d{4}-\d{2}-\d{2}$/.test(position.maturity)) {
        const stamp = Date.parse(`${position.maturity}T00:00:00Z`);
        return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === position.maturity ? stamp / 1000 : null;
      }
      return null;
    };
    try {
      if (record.source === "server") {
        const activeExitKeys = new Set();
        const positions = (record.venuePositions || []).filter(position => ["Exactly", "Tenor", "Morpho Midnight"].includes(position.venue)).map(position => {
          const row = position.instrument_id && universe.find(row => row.instrument_id === position.instrument_id);
          const assetId = position.underlying_asset_id || "Unknown asset";
          const asset = universe.find(row => row.asset_id === assetId
            || (row.chain_id === position.chain_id && row.address
              && `eip155:${row.chain_id}/erc20:${row.address.toLowerCase()}` === assetId.toLowerCase()));
          const registryAsset = REGISTRY_CHAINS.find(chain => chain.chain_id === position.chain_id)?.usdc;
          const registered = registryAsset?.address
            && `eip155:${position.chain_id}/erc20:${registryAsset.address.toLowerCase()}` === assetId.toLowerCase();
          const symbol = row?.underlying_symbol || row?.symbol || asset?.symbol
            || (registered && registryAsset.symbol) || assetId;
          const amount = value => `${escape(Routes.formatAmount(String(value), position.decimals) ?? "Unknown")} ${escape(symbol)}`;
          const maturityTimestamp = exitMaturity(position, row);
          const knownAssetAddress = asset?.address || assetAddress(position);
          const exitKey = position.venue === "Exactly"
            ? `EXACTLY_EXIT:${position.chain_id}:${String(position.market_address || row?.market_address || "").toLowerCase()}:${maturityTimestamp}`
            : `TENOR_EXIT:${position.chain_id}:${String(position.market_id || "").toLowerCase()}`;
          activeExitKeys.add(exitKey);
          const exitButton = !viewed.connected ? "" : position.venue === "Exactly"
            ? `<button type="button" class="button secondary" data-position-exit="${escape(exitKey)}">${position.matured ? "Withdraw" : "Withdraw early"}</button>`
            : `<button type="button" class="button secondary" data-position-exit="${escape(exitKey)}">Exit</button>`;
          // Exit state belongs to the wallet that signs: a watched book neither reads it (the lookup
          // answers for the connected account) nor writes it under the watched address.
          let exitState = viewed.connected ? getPositionExitState(exitKey) : null;
          if (exitState && (exitState.account !== String(account).toLowerCase() || exitState.chain_id !== position.chain_id
            || exitState.wallet_generation !== walletGeneration || exitState.universe !== universe)) {
            positionExitStates.delete(exitKey);
            exitState = null;
          }
          if (exitState?.loading && !preserveExitRequests) {
            exitState = {...exitState, request_id: null, loading: false, data: null, view: null,
              message: "Positions refreshed. Request a fresh exit quote."};
            setPositionExitState(exitKey, exitState);
          }
          const exitPanel = viewed.connected ? renderPositionExit(exitState, escape) : "";
          if (["Tenor", "Morpho Midnight"].includes(position.venue)) {
            const credit = P.asBigInt(position.face_value_raw_amount), debt = P.asBigInt(position.debt_raw_amount);
            const face = position.at_maturity_usd_micros == null ? "" : ` · worth about $${escape(Routes.formatAmount(String(position.at_maturity_usd_micros), 6))} at maturity`;
            const present = position.present_value_usd_micros == null ? "" : ` · ≈ $${escape(Routes.formatAmount(String(position.present_value_usd_micros), 6))} now`;
            const basis = ` (${escape(position.valuation_basis || "Unknown valuation basis")})`;
            const descriptor = {kind: "TENOR_EXIT", key: exitKey, chain_id: position.chain_id, market_id: position.market_id,
              loan_token: position.loan_token || row?.loan_token || row?.address, maturity: position.maturity_timestamp || row?.maturity_timestamp,
              receive_address: knownAssetAddress || position.loan_token || row?.loan_token || row?.address,
              position, symbol, decimals: position.decimals};
            if (viewed.connected) setPositionExitState(exitKey, {...(getPositionExitState(exitKey) || {}), key: exitKey, descriptor,
              account: String(account || "").toLowerCase(), chain_id: position.chain_id,
              wallet_generation: walletGeneration, universe});
            return `<li data-position-key="${escape(exitKey)}"><p>${escape(position.venue)} · ${escape(symbol)} · matures ${escape(position.maturity || "Unknown maturity")} · ${credit === null ? "Unknown credit" : amount(credit) + " at maturity"}${face}${present}${basis}`
              + (debt > 0n ? ` · owes ${amount(debt)}` : "")
              + (position.matured ? ` · matured; redeem on ${escape(position.venue)}` : "") + `</p>${exitButton}<div class="position-exit-host">${exitPanel}</div></li>`;
          }
          const principal = BigInt(position.principal_raw_amount), fee = BigInt(position.fee_raw_amount);
          const usd = position.present_value_usd_micros == null ? ""
            : ` · ≈ $${escape(Routes.formatAmount(String(position.present_value_usd_micros), 6))} now (${escape(position.valuation_basis || "Unknown valuation basis")})`;
          const descriptor = {kind: "EXACTLY_EXIT", key: exitKey, chain_id: position.chain_id,
            market_address: position.market_address || row?.market_address,
            maturity: maturityTimestamp, receive_address: knownAssetAddress || row?.underlying_address,
            position, symbol, decimals: position.decimals};
          if (viewed.connected) setPositionExitState(exitKey, {...(getPositionExitState(exitKey) || {}), key: exitKey, descriptor,
            account: String(account || "").toLowerCase(), chain_id: position.chain_id,
            wallet_generation: walletGeneration, universe});
          return `<li data-position-key="${escape(exitKey)}"><h3>${escape(row?.name || symbol)}</h3><p>${escape(routeChainName(position.chain_id))} · Maturity ${escape(position.maturity)} · ${escape(position.matured ? "Matured" : "Not matured")}</p>`
            + `<p>Principal: ${amount(principal)}${usd} · Fixed fee: ${amount(fee)} · Total at maturity: ${amount(principal + fee)}</p>${exitButton}<div class="position-exit-host">${exitPanel}</div></li>`;
        });
        const unreadChains = Object.entries(record.servedChains || {})
          .filter(([, status]) => status !== "READ").map(([chain]) => escape(routeChainName(Number(chain))));
        const unread = unreadChains.length ? `<p>Positions on ${unreadChains.join(", ")} could not be read.</p>` : "";
        const redeemables = (record.redeemable || []).map(item => {
          const key = `PT_REDEEM:${item.chain_id}:${String(item.address || "").toLowerCase()}`;
          activeExitKeys.add(key);
          const descriptor = {kind: "PT_REDEEM", key, chain_id: item.chain_id, instrument_address: item.address,
            venue: item.venue, maturity: item.maturity_timestamp, maturity_date: item.maturity, name: item.name,
            decimals: item.decimals};
          let prior = viewed.connected ? getPositionExitState(key) || {} : {};
          if (viewed.connected && (prior.account !== String(account || "").toLowerCase() || prior.wallet_generation !== walletGeneration
            || prior.universe !== universe)) {
            positionExitStates.delete(key);
            prior = {};
          }
          if (prior.loading && !preserveExitRequests) prior = {...prior, request_id: null, loading: false, data: null, view: null,
            message: "Positions refreshed. Request a fresh redemption quote."};
          if (viewed.connected) setPositionExitState(key, {...prior, key, descriptor, account: String(account || "").toLowerCase(),
            chain_id: item.chain_id, wallet_generation: walletGeneration, universe});
          return `<li data-position-key="${escape(key)}"><h3>${escape(item.name || item.venue + " principal token")}</h3>`
            + `<p>${escape(routeChainName(item.chain_id))} · ${escape(item.venue)} · matured ${escape(item.maturity || "")}</p>`
            + `<p>Balance: ${escape(Routes.formatAmount(String(item.raw_amount), item.decimals) || "Unknown")} PT</p>`
            + (viewed.connected ? `<button type="button" class="button secondary" data-position-exit="${escape(key)}">Redeem</button>` : "")
            + `<div class="position-exit-host">${viewed.connected ? renderPositionExit(getPositionExitState(key), escape) : ""}</div></li>`;
        });
        // Completed exits are the signing wallet's; the lookup answers for it, so a watched book skips them.
        const completed = !viewed.connected ? "" : [...new Set([...positionExitStates.keys(), ...[...submittedPositionExits.values()]
          .filter(entry => entry.account === String(account).toLowerCase()).map(entry => entry.key)])]
          .map(key => getPositionExitState(key)).filter(entry => (entry.hash || entry.submission_unresolved)
          && !activeExitKeys.has(entry.key) && entry.account === String(account).toLowerCase()
          && entry.wallet_generation === walletGeneration && entry.universe === universe)
          .map(entry => `<div role="status" data-position-key="${escape(entry.key)}"><div class="position-exit-host">${renderPositionExit(entry, escape)}</div></div>`).join("");
        if (current()) node.innerHTML = '<h2>Fixed-rate positions</h2>'
          + (viewed.connected ? "" : '<p class="muted">Read-only: exits are offered when this wallet is the active account in your extension.</p>')
          + completed + (positions.length
          ? '<ul>' + positions.join("") + '</ul>' : redeemables.length || unread ? "" : '<p>No fixed-rate positions on the chains read.</p>')
          + (redeemables.length ? '<h3>Matured tokens</h3><ul>' + redeemables.join("") + '</ul>' : '') + unread;
        return;
      }
      if (localRpcUnverified()) throw new Error(localRpcRefusal().errorMessage);
      if (!provider?.request || normalizeChainId(await provider.request({method: "eth_chainId"})) !== chainId) {
        throw new Error(viewed.connected
          ? "The wallet is not on the selected chain."
          : "The platform could not read this wallet, and a browser read needs a connected wallet on this chain.");
      }
      if (!current()) return;
      const rows = universe.filter(row => row.venue === "Exactly" && row.market_address && row.chain_id === chainId);
      const markets = Array.from(new Set(rows.map(row => row.market_address.toLowerCase())));
      const discovered = new Array(markets.length);
      await pooled(markets.map((market, index) => ({market, index})), 3, async ({market, index}) => {
        if (!current()) return;
        if (!configuredAddress(market)) throw new Error("A position has an invalid market.");
        const raw = await provider.request({method: "eth_call", params: [{to: market,
          data: "0x5e5c06e2" + encodeAddress(account)}, "latest"]});
        if (!current()) return;
        if (typeof raw !== "string" || !/^0x[0-9a-f]{192}$/i.test(raw)) throw new Error("A market returned invalid account data.");
        const packed = BigInt("0x" + raw.slice(2, 66));
        const base = Number(packed & 0xffffffffn), bitmap = packed >> 32n;
        const marketRows = rows.filter(row => row.market_address.toLowerCase() === market);
        discovered[index] = [];
        for (let i = 0; i < 224; i++) {
          if (!(bitmap & (1n << BigInt(i)))) continue;
          const maturity = base + i * 2419200;
          const row = marketRows.find(row => positionMaturity(row) === maturity);
          discovered[index].push({row: row || marketRows[0], maturity, name: row?.name});
        }
      });
      if (!current()) return;
      const jobs = discovered.flat(), positions = new Array(jobs.length);
      await pooled(jobs.map((job, index) => ({...job, index})), 3, async ({row, maturity, name, index}) => {
        if (!current()) return;
        const raw = await provider.request({method: "eth_call", params: [{to: row.market_address,
          data: "0xccfd8b11" + encodeUint256(maturity) + encodeAddress(account)}, "latest"]});
        if (!current()) return;
        if (typeof raw !== "string" || !/^0x[0-9a-f]{128}$/i.test(raw)) throw new Error("A position returned invalid amounts.");
        const principal = BigInt("0x" + raw.slice(2, 66)), fee = BigInt("0x" + raw.slice(66));
        if (principal === 0n && fee === 0n) return;
        const pay = positionSettlement(row);
        if (!pay?.symbol || Routes.formatAmount("0", pay.decimals) === null) throw new Error("The deposit asset settlement units are unavailable.");
        const amount = value => `${escape(Routes.formatAmount(value.toString(), pay.decimals))} ${escape(pay.symbol)}`;
        const date = new Date(maturity * 1000).toISOString().slice(0, 10);
        positions[index] = `<li><h3>${escape(name || `${pay.symbol}@${date}`)}</h3><p>Maturity ${escape(date)} · ${Date.now() >= maturity * 1000 ? "Matured" : "Not matured"}</p>`
          + `<p>Principal: ${amount(principal)} · Fixed fee: ${amount(fee)} · Total at maturity: ${amount(principal + fee)}</p></li>`;
      });
      if (!current()) return;
      node.innerHTML = '<h2>Fixed-rate positions</h2>' + (positions.some(Boolean)
        ? '<ul>' + positions.filter(Boolean).join("") + '</ul>' : '<p>No Exactly fixed deposits on this chain.</p>')
        + '<p>Tenor credit is shown when the server reads the wallet. Morpho Midnight credit is shown there too.</p>';
    } catch (error) {
      if (current()) node.innerHTML = `<h2>Fixed-rate positions</h2><p>Positions could not be read: ${escape(boundedAdapterError(error))}</p>`
        + (record?.source !== "server" ? '<p>Tenor credit is shown when the server reads the wallet. Morpho Midnight credit is shown there too.</p>' : "");
    }
  }

  function renderPositionExit(state, escape) {
    if (!state) return "";
    if (state.submission_unresolved) return `<div class="position-exit"><p>${escape(state.message)}</p>`
      + (!state.sending ? `<button type="button" class="button secondary" data-position-exit-release="${escape(state.key)}">I checked — allow a new request</button>` : "") + "</div>";
    if (state.loading) return '<div class="position-exit"><p>Checking the exit against the current market…</p></div>';
    if (state.error) return `<div class="position-exit"><p>${escape(state.error)}</p></div>`;
    const data = state.data, view = state.view, descriptor = state.descriptor;
    if (!data || !view || !descriptor) return state.message ? `<div class="position-exit"><p>${escape(state.message)}</p></div>` : "";
    const display = raw => data.receive ? Routes.formatAmount(raw, data.receive.decimals) : null;
    const lines = [`<div class="position-exit"><p><b>${escape(view.status_text)}</b>${view.action ? ` · ${escape(view.action.replaceAll("_", " ").toLowerCase())}` : ""}</p>`];
    if (["insufficient", "native_value"].includes(view.gas_warning)) lines.push(renderGasNotice(view.gas_check, view.gas_warning, escape));
    if (data.receive && data.proceeds_raw_amount != null) lines.push(descriptor.kind === "PT_REDEEM"
      ? `<p>Wallet receives ${escape(display(data.proceeds_raw_amount) || "Unknown")} ${escape(data.receive.symbol)}</p>`
      : `<p>Receive now: ${escape(display(data.proceeds_raw_amount) || "Unknown")} ${escape(data.receive.symbol)} · at maturity: ${escape(display(data.face_raw_amount) || "Unknown")} ${escape(data.receive.symbol)}</p>`);
    if (descriptor.kind === "PT_REDEEM" && data.receive) lines.push(`<p>Minimum: ${escape(display(data.min_proceeds_raw_amount) || "Unknown")} ${escape(data.receive.symbol)} · ${descriptor.venue === "Pendle" ? (data.simulated ? "simulated" : "preview") : "simulated"}</p>`);
    if (descriptor.kind !== "PT_REDEEM" && data.route_cost_bps != null) lines.push(`<p>Cost: ${escape(data.route_cost_bps)} bp at Fenorium’s mark · ${escape(data.mark_basis)}</p>`);
    if (data.receive && data.settlement_fee_raw_amount != null) lines.push(`<p>Settlement fee: ${escape(display(data.settlement_fee_raw_amount) || "Unknown")} ${escape(data.receive.symbol)}</p>`);
    if (descriptor.kind !== "PT_REDEEM" && data.units_raw_amount != null) lines.push(`<p>Credit exited: ${escape(display(data.units_raw_amount) || "Unknown")} · left: ${escape(display(data.remaining_units_raw_amount) || "Unknown")}</p>`);
    if (data.block?.number != null) lines.push(`<p>Checked at block ${escape(data.block.number)}</p>`);
    const exitReasons = view.reasons.filter(reason => !["INSUFFICIENT_GAS", "LOW_GAS_BALANCE"].includes(reason.code));
    if (exitReasons.length) lines.push(`<ul>${exitReasons.map(reason => `<li>${escape(reason.text)}</li>`).join("")}</ul>`);
    if (data.cost_acceptance && !data.cost_accepted_bps) {
      lines.push(`<button type="button" class="button" data-position-exit-accept="${escape(state.key)}" data-exit-cost="${escape(data.cost_acceptance.route_cost_bps)}">Accept ${escape((data.cost_acceptance.route_cost_bps / 100).toFixed(2))}% cost and refresh quote</button>`);
    }
    if (view.gas_warning === "low") lines.push(renderGasNotice(view.gas_check, view.gas_warning, escape));
    if (view.can_sign && !state.pending_receipt && state.wallet_chain_id !== descriptor.chain_id) {
      lines.push(`<button type="button" class="button" data-position-exit-switch="${escape(state.key)}">Switch wallet to ${escape(routeChainName(descriptor.chain_id))} for a fresh quote</button>`);
    }
    if (view.can_sign && state.message) lines.push(`<p>${escape(state.message)}${state.hash ? ` · ${escape(state.hash)}` : ""}</p>`);
    if (view.can_sign && state.wallet_chain_id === descriptor.chain_id && !state.message && !state.pending_receipt) lines.push(`<button type="button" class="button" data-position-exit-send="${escape(state.key)}">${descriptor.kind === "PT_REDEEM" ? "Redeem" : "Sign and send"}</button>`);
    if (state.pending_receipt && state.hash) {
      // Receipts are read through the wallet, so they can only be checked on the chain the exit was sent on.
      const elsewhere = state.wallet_chain_id !== state.chain_id;
      if (elsewhere) lines.push(`<p>Switch your wallet back to ${escape(routeChainName(state.chain_id))} to check this transaction.</p>`);
      lines.push(`<button type="button" class="button" data-position-exit-check="${escape(state.key)}"${state.checking || elsewhere ? " disabled" : ""}>Check again</button>`);
    }
    lines.push("</div>");
    return lines.join("");
  }

  function renderGasNotice(gas, level, escape) {
    if (!gas || !["insufficient", "native_value", "low"].includes(level)) return "";
    const balance = Routes.formatAmount(gas.native_balance_raw_amount, gas.native_decimals);
    const required = Routes.formatAmount(gas.required_raw_amount, gas.native_decimals);
    const reservation = Routes.formatAmount(gas.reservation_raw_amount, gas.native_decimals);
    const chain = escape(routeChainName(gas.chain_id)), symbol = escape(gas.native_symbol);
    if (level === "native_value") {
      const value = Routes.formatAmount(gas.native_value_raw_amount, gas.native_decimals);
      return `<p class="route-error">This wallet holds ${escape(balance)} ${symbol} on ${chain}; this transaction sends ${escape(value)}.</p>`;
    }
    return level === "insufficient"
      ? `<p class="route-error">This wallet holds ${escape(balance)} ${symbol} on ${chain}; sending this needs about ${escape(required)} for gas.</p>`
      : `<p class="route-warning">Your wallet may refuse to send: wallets set aside up to ${escape(reservation)} ${symbol} for this transaction's gas limit and this one holds ${escape(balance)}.</p>`;
  }

  async function checkPositionExitReceipt(key) {
    let entry = getPositionExitState(key);
    if (!entry?.pending_receipt || entry.checking || entry.chain_id !== state.wallet.chainId || !state.provider?.request) return;
    const walletGeneration = state.walletGeneration, provider = state.provider;
    const receiptScope = () => walletGeneration === state.walletGeneration && provider === state.provider
      && entry.account === String(state.wallet.account || "").toLowerCase() && entry.chain_id === state.wallet.chainId;
    entry = {...entry, receipt_scope: receiptScope, receipt_provider: provider};
    setPositionExitState(key, {...entry, checking: true});
    renderVenuePositionsIfCurrent(venuePositionsGeneration);
    const receipt = await waitForRouteReceipt(entry.hash, entry.receipt_scope, entry.receipt_provider);
    if (!entry.receipt_scope()) {
      setPositionExitState(key, {...entry, checking: false});
      renderVenuePositionsIfCurrent(venuePositionsGeneration);
      return;
    }
    const safeFailed = safeExecutionFailed(receipt, entry.account);
    if (safeFailed) receipt.status = "REVERTED";
    const pending = !["CONFIRMED", "REVERTED"].includes(receipt.status);
    const message = pending ? "Transaction is still pending" : safeFailed
      ? "The Safe executed the transaction but its inner call failed; nothing was exited."
      : receipt.status === "REVERTED"
      ? "The exit transaction reverted" : entry.approval_pending
        ? "Approval confirmed. Request a fresh redemption quote."
        : entry.descriptor.kind === "PT_REDEEM" ? "Redemption confirmed" : "Exit confirmed";
    setPositionExitState(key, {...entry, checking: false, sending: pending, pending_receipt: pending, receipt_status: receipt.status, message});
    renderVenuePositionsIfCurrent(venuePositionsGeneration);
    if (!pending && !entry.approval_pending) {
      const refreshPositions = () => entry.receipt_scope() ? refreshVenuePositions({preserveExitRequests: true}) : undefined;
      if (receipt.status === "CONFIRMED") {
        await refreshAfterRoute({account: entry.account, chain_id: entry.chain_id,
          receipt_block: Number(receipt.block_number) || 0}, refreshPositions);
      } else {
        await refreshPortfolio({preserveExitRequests: true, scope: "connected"});
        await refreshPositions();
      }
    }
  }

  async function requestPositionExit(key, acceptCostBps) {
    const stateForExit = getPositionExitState(key), descriptor = stateForExit?.descriptor;
    if (!descriptor || stateForExit.loading || stateForExit.sending || stateForExit.submission_unresolved || stateForExit.pending_receipt || !state.wallet.account || !state.provider?.request || !viewedWallet().connected) return;
    if (stateForExit.account !== String(state.wallet.account).toLowerCase()
      || stateForExit.wallet_generation !== state.walletGeneration || stateForExit.universe !== state.universe) return;
    if (acceptCostBps !== undefined && (!Number.isSafeInteger(acceptCostBps)
      || stateForExit.data?.status !== "ANALYTICAL_ONLY"
      || stateForExit.data?.cost_acceptance?.route_cost_bps !== acceptCostBps)) return;
    const generation = venuePositionsGeneration, walletGeneration = state.walletGeneration;
    const account = String(state.wallet.account).toLowerCase(), activeChainId = state.wallet.chainId, chainId = descriptor.chain_id;
    const universe = state.universe, provider = state.provider, requestId = ++positionExitSequence;
    const current = () => walletGeneration === state.walletGeneration
      && account === String(state.wallet.account || "").toLowerCase() && activeChainId === state.wallet.chainId
      && universe === state.universe && provider === state.provider;
    const entry = {...stateForExit, key, loading: true, request_id: requestId, wallet_chain_id: activeChainId,
      error: null, message: null, hash: null, data: null, view: null};
    const requestCurrent = () => current() && getPositionExitState(key)?.request_id === requestId;
    setPositionExitState(key, entry); renderVenuePositionsIfCurrent(venuePositionsGeneration);
    const request = {schema_version: "fenorium/platform-position-exit-request/1", method_version: "exit/1",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), wallet: account, chain_id: chainId, kind: descriptor.kind,
      ...(descriptor.kind === "EXACTLY_EXIT" ? {market_address: descriptor.market_address, maturity: Number(descriptor.maturity)}
        : descriptor.kind === "TENOR_EXIT" ? {market_id: descriptor.market_id} : {instrument_address: descriptor.instrument_address}),
      ...(acceptCostBps === undefined ? {} : {accept_cost_bps: acceptCostBps})};
    if (descriptor.kind === "EXACTLY_EXIT" && (!configuredAddress(descriptor.market_address)
      || !Number.isSafeInteger(descriptor.maturity) || descriptor.maturity < 0)) {
      setPositionExitState(key, {...entry, loading: false, error: "This Exactly position is missing its market or maturity identity."});
      renderVenuePositionsIfCurrent(venuePositionsGeneration);
      return;
    }
    try {
      const response = await root.fetch("/v1/position-exits", {method: "POST", cache: "no-store",
        headers: {Accept: "application/json", "Content-Type": "application/json",
          "Idempotency-Key": `position-exit-${PORTFOLIO_SESSION_ID}-${generation}-${requestId}`}, body: JSON.stringify(request)});
      let envelope = null; try { envelope = await response.json(); } catch (_) { envelope = null; }
      if (!requestCurrent()) return;
      if (!response.ok || !validPlatformEnvelope(envelope) || !object(envelope.data)) {
        throw new Error(envelope?.message || envelope?.error || `Exit request failed (HTTP ${response.status}).`);
      }
      const expected = {wallet: account, chain_id: chainId, kind: descriptor.kind,
        market_address: descriptor.market_address, market_id: descriptor.market_id,
        instrument_address: descriptor.instrument_address, venue: descriptor.venue,
        maturity: descriptor.kind === "EXACTLY_EXIT" ? Number(descriptor.maturity) : undefined,
        receive_address: descriptor.receive_address, accept_cost_bps: acceptCostBps};
      const view = Routes.positionExitView(envelope.data, expected);
      if (!view) throw new Error("The exit response did not match this position and wallet, so it cannot be signed.");
      setPositionExitState(key, {...entry, loading: false, data: envelope.data, view});
      renderVenuePositionsIfCurrent(venuePositionsGeneration);
    } catch (error) {
      if (!requestCurrent()) return;
      setPositionExitState(key, {...entry, loading: false, error: boundedAdapterError(error)});
      renderVenuePositionsIfCurrent(venuePositionsGeneration);
    }
  }

  function renderVenuePositionsIfCurrent(generation) {
    if (generation !== venuePositionsGeneration) return;
    // A watched book carries no exit panel; a quote that lands for the signing wallet stays out of it.
    if (!viewedWallet().connected) return;
    const node = $("venue-positions");
    const holders = node?.querySelectorAll?.(".position-exit-host") || [];
    for (const holder of holders) {
      const row = holder.closest?.("[data-position-key]");
      const key = row?.getAttribute?.("data-position-key");
      if (key && getPositionExitState(key)) holder.innerHTML = renderPositionExit(getPositionExitState(key), value =>
        String(value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char])));
    }
    if (execution?.kind === "sale" && execution.data?.reason_codes?.includes("SALE_MATURED_REDEEM")) renderRouteQuotes();
  }

  // Time for the wallet to sign and the sale to land before the bid's deadline.
  const POSITION_EXIT_SEND_MARGIN_SECONDS = 30;

  async function sendPositionExit(key) {
    const entry = getPositionExitState(key), descriptor = entry?.descriptor, data = entry?.data, view = entry?.view;
    if (!entry || entry.sending || entry.submission_unresolved || entry.hash || !descriptor || !view?.can_sign || !data?.transaction_request
      || !mainnetExecutionOpen() || !viewedWallet().connected
      || entry.account !== String(state.wallet.account || "").toLowerCase()
      || entry.wallet_generation !== state.walletGeneration || entry.universe !== state.universe
      || entry.wallet_chain_id !== state.wallet.chainId || descriptor.chain_id !== state.wallet.chainId) return;
    const generation = venuePositionsGeneration, walletGeneration = state.walletGeneration;
    const account = String(state.wallet.account || "").toLowerCase(), chainId = descriptor.chain_id;
    const accountGeneration = state.walletAccountGeneration, provider = state.provider, universe = state.universe;
    const scopeCurrent = () => walletGeneration === state.walletGeneration
      && accountGeneration === state.walletAccountGeneration && account === String(state.wallet.account || "").toLowerCase()
      && chainId === state.wallet.chainId && provider === state.provider && universe === state.universe;
    const current = () => generation === venuePositionsGeneration && scopeCurrent();
    if (data.transaction_request.chainId !== chainId || String(data.transaction_request.from).toLowerCase() !== account) return;
    // A sale into a Midnight bid is only takeable until the bid's deadline; past it Midnight reverts.
    const bidExpired = () => Number.isSafeInteger(data.expires_at)
      && Date.now() / 1000 >= data.expires_at - POSITION_EXIT_SEND_MARGIN_SECONDS;
    const BID_EXPIRED = "This bid expired before it was signed. Request the exit again for a fresh quote.";
    if (bidExpired()) {
      setPositionExitState(key, {...entry, data: null, view: null, message: BID_EXPIRED});
      renderVenuePositionsIfCurrent(generation);
      return;
    }
    let sendingEntry = {...entry, sending: true, message: "Waiting for your wallet to send the transaction…", hash: null};
    setPositionExitState(key, sendingEntry);
    renderVenuePositionsIfCurrent(generation);
    try {
      if (!current()) return;
      const chainHex = await provider.request({method: "eth_chainId"});
      if (!current()) return;
      if (normalizeChainId(chainHex) !== chainId || state.wallet.chainId !== chainId) throw new Error(`Switch your wallet to ${routeChainName(chainId)} before signing.`);
      if (String(data.transaction_request.from).toLowerCase() !== String(state.wallet.account).toLowerCase()) throw new Error("This exit was built for a different wallet; request it again.");
      if (!current()) return;
      const sendInstruction = async instruction => {
        // The deployment may have closed signing since this exit was prepared; nothing is sent then.
        if (!mainnetExecutionOpen()) throw new Error("Signing is disabled on this deployment; nothing was sent.");
        const transaction = {from: instruction.from, to: instruction.to, data: instruction.data,
          chainId: `0x${chainId.toString(16)}`, value: `0x${BigInt(instruction.value || "0").toString(16)}`};
        // A wallet can broadcast and then reject without returning a hash. Lock before asking it.
        sendingEntry = {...sendingEntry, submission_unresolved: true, hash: null, pending_receipt: false};
        setPositionExitState(key, sendingEntry);
        const hash = await provider.request({method: "eth_sendTransaction", params: [transaction]});
        if (typeof hash !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(hash)) {
          throw new Error("Wallet did not return a transaction hash; submission outcome is unknown.");
        }
        return hash;
      };
      const awaitConfirmed = async approvalHash => {
        sendingEntry = {...sendingEntry, pending_receipt: true, approval_pending: true,
          receipt_scope: scopeCurrent, receipt_provider: provider};
        setPositionExitState(key, sendingEntry);
        await checkPositionExitReceipt(key);
        const stored = getPositionExitState(key);
        if (!scopeCurrent() || stored?.pending_receipt || stored?.receipt_status !== "CONFIRMED") return false;
        sendingEntry = {...stored, sending: true, pending_receipt: false, approval_pending: false};
        setPositionExitState(key, sendingEntry);
        return true;
      };
      if (descriptor.kind === "PT_REDEEM") {
        let allowance = 0n;
        if (descriptor.venue === "Pendle") {
          const allowanceData = "0xdd62ed3e" + account.slice(2).toLowerCase().padStart(64, "0")
            + "888888888889758f76e7103c6cbf23abbf58f946".padStart(64, "0");
          const result = await provider.request({method: "eth_call", params: [{to: descriptor.instrument_address, data: allowanceData}, "latest"]});
          if (!scopeCurrent() || typeof result !== "string" || !/^0x[0-9a-f]{64}$/i.test(result)) throw new Error("Could not verify the principal token allowance.");
          allowance = BigInt(result);
        }
        const balance = BigInt(data.units_raw_amount);
        const approvals = descriptor.venue === "Pendle" && allowance < balance
          ? [...(allowance > 0n && data.approval_reset_request ? [data.approval_reset_request] : []), data.approval_request].filter(Boolean)
          : [];
        for (const instruction of approvals) {
          if (!scopeCurrent()) return;
          const approvalHash = await sendInstruction(instruction);
          sendingEntry = {...sendingEntry, submission_unresolved: false, hash: approvalHash, pending_receipt: true, approval_pending: true,
            message: `Waiting for approval ${approvalHash}…`};
          setPositionExitState(key, sendingEntry);
          if (!scopeCurrent()) return;
          renderVenuePositionsIfCurrent(venuePositionsGeneration);
          if (!await awaitConfirmed(approvalHash)) return;
        }
      }
      if (!scopeCurrent()) return;
      if (bidExpired()) {
        sendingEntry = {...sendingEntry, sending: false, data: null, view: null, message: BID_EXPIRED};
        setPositionExitState(key, sendingEntry);
        renderVenuePositionsIfCurrent(venuePositionsGeneration);
        return;
      }
      const hash = await sendInstruction(data.transaction_request);
      sendingEntry = {...sendingEntry, submission_unresolved: false, hash, pending_receipt: true, approval_pending: false,
        message: `Waiting for transaction ${hash}…`};
      setPositionExitState(key, sendingEntry);
      if (!scopeCurrent()) return;
      renderVenuePositionsIfCurrent(venuePositionsGeneration);
      setPositionExitState(key, {...sendingEntry, pending_receipt: true, approval_pending: false,
        receipt_scope: scopeCurrent, receipt_provider: provider});
      await checkPositionExitReceipt(key);
    } catch (error) {
      if (sendingEntry.submission_unresolved || scopeCurrent()) {
        const rejected = userRejected(error) || /\buser rejected\b/i.test(String(error?.message || ""));
        const unresolved = sendingEntry.submission_unresolved && !rejected;
        setPositionExitState(key, {...sendingEntry, sending: false, submission_unresolved: unresolved,
          message: unresolved
            ? "The wallet did not confirm whether this transaction was sent. Check your wallet's activity before trying again."
            : `Wallet could not send the exit: ${boundedAdapterError(error)}`});
        renderVenuePositionsIfCurrent(venuePositionsGeneration);
      }
    } finally {
      const stored = getPositionExitState(key);
      if (!sendingEntry.hash && !stored?.submission_unresolved && stored?.sending && stored.request_id === entry.request_id && scopeCurrent()) {
        setPositionExitState(key, {...stored, sending: false, data: null, view: null,
          message: "Positions changed. Request a fresh exit quote."});
        renderVenuePositionsIfCurrent(venuePositionsGeneration);
      }
    }
  }

  function releasePositionExit(key) {
    const entry = getPositionExitState(key);
    if (!entry?.submission_unresolved || entry.sending || entry.hash
      || entry.account !== String(state.wallet.account || "").toLowerCase()) return;
    setPositionExitState(key, {...entry, submission_unresolved: false, data: null, view: null,
      approval_pending: false, message: "Request a fresh exit quote."});
    renderVenuePositionsIfCurrent(venuePositionsGeneration);
  }

  async function switchPositionExitChain(key) {
    const entry = getPositionExitState(key), descriptor = entry?.descriptor;
    if (!entry || !descriptor || entry.sending || entry.submission_unresolved || entry.account !== String(state.wallet.account || "").toLowerCase()
      || entry.wallet_generation !== state.walletGeneration || entry.universe !== state.universe || !state.provider?.request) return;
    const account = String(state.wallet.account).toLowerCase(), chainId = descriptor.chain_id;
    const walletGeneration = state.walletGeneration, accountGeneration = state.walletAccountGeneration;
    const provider = state.provider, universe = state.universe, fromChain = state.wallet.chainId;
    const current = () => walletGeneration === state.walletGeneration && accountGeneration === state.walletAccountGeneration
      && account === String(state.wallet.account || "").toLowerCase() && provider === state.provider && universe === state.universe
      && fromChain === state.wallet.chainId;
    try {
      const chainHex = `0x${chainId.toString(16)}`;
      try { await provider.request({method: "wallet_switchEthereumChain", params: [{chainId: chainHex}]}); }
      catch (error) {
        if (Number(error?.code) !== 4902) throw error;
        const target = routeChainConfig(state.config)?.[chainId];
        if (!target?.native) throw error;
        await provider.request({method: "wallet_addEthereumChain", params: [{chainId: chainHex,
          chainName: target.name, nativeCurrency: {name: target.native.symbol, symbol: target.native.symbol, decimals: 18},
          rpcUrls: [target.rpc_url], ...(target.explorer ? {blockExplorerUrls: [target.explorer]} : {})}]});
        if (!current()) return;
        await provider.request({method: "wallet_switchEthereumChain", params: [{chainId: chainHex}]});
      }
      if (!current()) return;
      const observedChain = normalizeChainId(await provider.request({method: "eth_chainId"}));
      if (!current() || observedChain !== chainId) return;
      if (state.wallet.chainId !== chainId) setWallet(account, chainId);
      appliedSwitch = {chainId, at: Date.now()};
      setPositionExitState(key, {...entry, data: null, view: null, loading: false, sending: false,
        error: null, hash: null, message: "Wallet switched. Request a fresh quote before signing.",
        wallet_chain_id: chainId, wallet_generation: state.walletGeneration,
        account, chain_id: descriptor.chain_id, universe});
      void refreshVenuePositions();
    } catch (error) {
      if (!current()) return;
      setPositionExitState(key, {...entry, error: `Could not switch to ${routeChainName(chainId)}: ${boundedAdapterError(error)}`});
      renderVenuePositionsIfCurrent(venuePositionsGeneration);
    }
  }
  function renderRouteQuotes() {
    if (execution && !executionIsCurrent(execution)) {
      execution = null;
      executionGeneration += 1;
    }
    if (execution && !routeFresh(execution)) execution.signable = false;
    const now = Date.now();
    const rows = routeRows.map((row, index) => ({...row, index, display: Routes.displayQuote?.(row.binding, now)}))
      .filter(row => row.display || (row.preview && !row.binding))
      .sort((a, b) => Number(Boolean(b.best)) - Number(Boolean(a.best)));
    const escape = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
    const explanation = rows.some(row => row.preview)
      ? "Quoted by Fenorium's server for this wallet at the selected size; not a purchase. Network costs are not included."
      : "LI.FI quote only · minimum output · not a purchase. Quoted from a neutral address, so it is what this size would obtain and not a simulation of your wallet: allowances, gas and eligibility are not checked. Network costs are not included.";
    const html = rows.length ? '<section class="panel"><h3>What your holdings could obtain</h3><p>' + explanation + '</p>'
      + rows.map(row => {
        if (!row.binding) return `<p>Paying with ${escape(row.symbol)}: ${(row.refused || []).map(code => escape(Routes.REASON_TEXT?.[code] || code)).join(" · ")}</p>`;
        const minimum = `${escape(row.display.minimum_amount)} ${escape(state.selected?.symbol || state.selected?.name || row.binding.to_token)}`;
        const quotedUsd = row.quoted_usd_micros != null ? Number(row.quoted_usd_micros) / 1000000 : state.notionalSize;
        const quote = row.preview
          ? `${row.best ? '<strong>Best price</strong> · ' : ''}Pay with ≈ $${escape(quotedUsd.toLocaleString("en-US"))} of ${escape(row.symbol)}: at least ${minimum}${row.cost_bps != null ? " · cost " + escape(row.cost_bps) + " bp" : ""}${row.yield_to_maturity_bps != null ? ` · yield ${(row.yield_to_maturity_bps / 100).toFixed(2)}% · after cost ${row.net_yield_to_maturity_bps == null ? "unknown" : (row.net_yield_to_maturity_bps / 100).toFixed(2) + "%"}` : ""}${row.acceptance_offer ? " · accept cost on the executable quote" : ""}${row.tool ? " · via " + escape(row.tool) : ""}`
          : `With ${escape(row.display.pay_amount)} ${escape(row.symbol)}, quoted minimum: ${minimum}`;
        return `<p>${quote}${row.binding.from_chain !== row.binding.to_chain ? " · pay on " + escape(routeChainName(row.binding.from_chain)) : ""} <button type="button" class="button${row.preview && row.best ? "" : " secondary"}" data-route-execute="${escape(row.binding.from_chain + ":" + row.binding.from_token)}">Executable route</button></p>`;
      }).join("") + '</section>' : "";
    // Outside the preview on purpose: the preview expires every two minutes, and a purchase that
    // is waiting on its receipt must not vanish with it.
    const panel = execution?.kind === "sale" ? "" : executionPanel(escape) + routeSubmissionPanel(escape);
    const notices = [...routeMessages, routeSelectionMessage].filter(Boolean).map(text => `<p>${escape(text)}</p>`).join("");
    const markup = depositEntry(escape) + html + (notices ? `<section class="panel">${notices}</section>` : "") + (panel ? `<section class="panel">${panel}</section>` : "");
    for (const id of ["market-route-quotes", "asset-route-quotes"]) {
      const node = $(id);
      if (node) { node.innerHTML = markup; node.hidden = !markup; }
    }
    const saleNode = $("portfolio-sale-route");
    if (saleNode) {
      // The sale is the signer's; its controls leave a watched wallet's book and return with the
      // signer's view, the sale itself and its submitted transaction untouched.
      const saleMarkup = execution?.kind === "sale" && viewedWallet().connected
        ? executionPanel(escape) + routeSubmissionPanel(escape) : "";
      saleNode.innerHTML = saleMarkup ? `<section class="panel">${saleMarkup}</section>` : "";
      saleNode.hidden = !saleMarkup;
    }
    return markup;
  }

  function executionPanel(escape) {
    if (!execution) return "";
    if (execution.kind === "lending") return lendingPanel(escape);
    const e = execution, lines = [], locked = e.sending || routeLocked(e), deposit = e.kind === "deposit", sale = e.kind === "sale";
    lines.push(`<div class="route-execution"><h4>${sale ? `Sell ${escape(e.instrument_symbol)}` : deposit ? "Exactly fixed deposit" : "Executable route"}${sale ? ` · you receive ${escape(e.receive?.symbol || "settlement asset")} on ${escape(routeChainName(e.receive?.chain_id || e.chain_id))}` : ` · pay with ${escape(e.symbol)}`}</h4>`);
    lines.push(`<p>${sale ? "Choose an amount to sell. Fenorium checks your available balance before quoting." : deposit ? "Fenorium's server builds the deposit for this wallet." : "Asking for one sends your wallet address to LI.FI through Fenorium's server, so the route is built for this wallet rather than a neutral one."} Your balance: ${escape(e.balance_display)} ${escape(e.symbol)}.</p>`);
    lines.push(`<label>Amount of ${escape(e.symbol)} ${sale ? "to sell" : "to pay"} <input type="text" inputmode="decimal" ${sale ? "data-sale-amount" : "data-route-amount"} value="${escape(e.amount_input)}"></label> `);
    if (sale) lines.push(`<label>Receive <select data-sale-receive>${(e.receive_options || []).map(option => `<option value="${escape(option.address.toLowerCase())}"${option.address.toLowerCase() === e.receive.address.toLowerCase() ? " selected" : ""}>${escape(option.symbol)} on ${escape(routeChainName(option.chain_id))}</option>`).join("")}</select></label> `);
    lines.push(`<button type="button" class="button" data-route-request ${e.phase === "LOADING" ? "disabled" : ""}>${sale ? "Request sale" : deposit ? "Request deposit" : "Request route"}</button>`);
    if (e.phase === "LOADING") lines.push(`<p>Asking the server for a ${deposit ? "deposit" : "route"} bound to your wallet…</p>`);
    if (e.error) lines.push(`<p class="route-error">${escape(e.error)}</p>`);
    const view = e.view;
    if (view) {
      lines.push(`<p><b>${escape(sale && e.data?.status === "REJECTED" ? "Fenorium will not route this sale" : sale && e.data?.status === "ROUTE_UNAVAILABLE" ? "No sale route could be obtained right now" : view.status_text)}</b></p>`);
      if (["insufficient", "native_value"].includes(view.gas_warning)) lines.push(renderGasNotice(view.gas_check, view.gas_warning, escape));
      if (view.route) {
        if (sale) {
          const size = e.data?.size_check;
          const cost = size && Number.isSafeInteger(size.route_cost_bps) ? `${size.route_cost_bps} bp` : "unknown";
          lines.push(`<p>You sell ${escape(view.route.pay_amount)} ${escape(e.instrument_symbol)} and receive at least ${escape(view.route.minimum_amount)} ${escape(e.receive.symbol)} · cost ${escape(cost)} at Fenorium's mark · via ${escape(view.route.tool)}</p>`);
          if (view.cost_acceptance && !e.amount_changed) {
            const percent = (view.cost_acceptance.route_cost_bps / 100).toFixed(2);
            lines.push(`<p>This sale keeps ${escape(percent)}% of what you sell, at Fenorium's valuation — more than the 3% Fenorium normally allows. You receive at least ${escape(view.route.minimum_amount)} ${escape(e.receive.symbol)} for ${escape(view.route.pay_amount)} ${escape(e.instrument_symbol)}.</p>`);
            lines.push(`<button type="button" class="button" data-sale-accept-cost="${view.cost_acceptance.route_cost_bps}" ${locked || e.phase === "LOADING" ? "disabled" : ""}>Accept ${escape(percent)}% and continue</button>`);
          }
        } else lines.push(`<p>You pay ${escape(view.route.pay_amount)} ${escape(e.symbol)} and receive at least ${escape(view.route.minimum_amount)} ${escape(e.instrument_symbol)}${deposit ? " at maturity" : ""}`
          + ` · gas ${view.route.gas_cost_usd === null ? "not stated" + (deposit ? "; the wallet prices it at signing" : "") : "≈ $" + escape(view.route.gas_cost_usd)} · via ${escape(view.route.tool)}`
          + ` · expires in ${escape(view.route.expires_in_seconds)}s</p>`);
        if (!sale && !deposit && view.size_check?.yield_to_maturity_bps != null) {
          const size = view.size_check;
          lines.push(`<p>Yield to maturity ${(size.yield_to_maturity_bps / 100).toFixed(2)}% · after this route's cost ${size.net_yield_to_maturity_bps == null ? "unknown" : (size.net_yield_to_maturity_bps / 100).toFixed(2) + "%"}</p>`);
          if (view.cost_acceptance && !e.amount_changed) {
            const percent = (view.cost_acceptance.route_cost_bps / 100).toFixed(2);
            lines.push(`<button type="button" class="button" data-purchase-accept-cost="${view.cost_acceptance.route_cost_bps}" ${locked || e.phase === "LOADING" ? "disabled" : ""}>Accept ${escape(percent)}% cost and continue</button>`);
          }
        }
      }
      if (view.cross_chain) {
        const source = escape(routeChainName(view.from_chain)), destination = escape(routeChainName(view.to_chain));
        lines.push(`<p>Crosses from ${source} to ${destination} via ${escape(view.route.tool)}.${view.execution_duration_s === null ? "" : ` LI.FI expects about ${Math.ceil(view.execution_duration_s / 60)} minutes.`}</p>`);
        lines.push(`<p>Fees are collected when your transaction on ${source} confirms.</p>`);
        lines.push(`<p>If the step on ${destination} fails, you may receive a different token on ${destination}, or a refund on ${source}, instead of ${escape(e.instrument_symbol)}. Fenorium shows which once LI.FI reports it.</p>`);
      }
      if (view.route) {
        const usd = value => value === null ? "not stated" : "≈ $" + escape(value);
        if (view.fees === null) lines.push("<p>The route did not state its fees, so it is not offered for signing.</p>");
        else lines.push("<ul>" + view.fees.map(fee => {
          const unit = escape((fee.token_chain_id == null || fee.token_chain_id === e.chain_id)
            && fee.token.toLowerCase() === e.pay.address.toLowerCase() ? e.symbol : fee.token);
          // LI.FI reports its own fee and Fenorium's as one entry; the split says whose is whose.
          const split = fee.integrator_fee === "0" ? "" : ` (LI.FI ${escape(fee.lifi_fee)} ${unit}, Fenorium ${escape(fee.integrator_fee)} ${unit})`;
          return `<li>${escape(fee.name)}: ${escape(fee.amount)} ${unit}${split}${fee.token_chain_id != null && fee.token_chain_id !== e.chain_id ? " on " + escape(routeChainName(fee.token_chain_id)) : ""} · ${usd(fee.amount_usd)} · ${fee.included ? "included in quoted amounts" : "charged on top"}</li>`;
        }).join("") + "</ul>");
        if (!deposit) lines.push(`<p>LI.FI fee: ${usd(view.lifi_fee_usd)} · Fenorium fee: ${usd(view.fenorium_fee_usd)}</p>`);
      }
      if (view.reasons.length) {
        const reasons = view.reasons.filter(reason => !["INSUFFICIENT_GAS", "LOW_GAS_BALANCE"].includes(reason.code));
        if (reasons.length) lines.push("<ul>" + reasons.map(reason => `<li><code>${escape(["NO_BID", "NO_QUOTE"].includes(reason.code) ? U.stateLabel(reason.code) : reason.code)}</code> ${escape(reason.text)}</li>`).join("") + "</ul>");
      }
      if (sale && e.data?.reason_codes?.includes("SALE_MATURED_REDEEM")
        && ["Pendle", "Spectra"].includes(e.instrument.venue) && configuredAddress(e.instrument.address)) {
        const redeemKey = `PT_REDEEM:${e.instrument.chain_id}:${e.instrument.address.toLowerCase()}`;
        const descriptor = {kind: "PT_REDEEM", key: redeemKey, chain_id: e.instrument.chain_id,
          instrument_address: e.instrument.address, venue: e.instrument.venue,
          maturity: e.instrument.maturity_timestamp, maturity_date: e.instrument.maturity,
          name: e.instrument.name, decimals: e.pay.decimals};
        // Exit state is keyed by the lowercased account; wallets may report a checksummed one.
        const account = String(e.account || "").toLowerCase();
        let prior = getPositionExitState(redeemKey) || {};
        if (prior.account !== account || prior.wallet_generation !== state.walletGeneration
          || prior.universe !== state.universe) prior = {};
        setPositionExitState(redeemKey, {...prior, key: redeemKey, descriptor, account,
          chain_id: descriptor.chain_id, wallet_generation: state.walletGeneration, universe: state.universe});
        lines.push(`<button type="button" class="button" data-sale-matured-redeem="${escape(redeemKey)}">This token has matured — redeem it</button>`);
        lines.push(renderPositionExit(getPositionExitState(redeemKey), escape));
      }
      if (e.signable && view.gas_warning === "low") lines.push(renderGasNotice(view.gas_check, view.gas_warning, escape));
      if (e.signable) {
        if (e.needs_reset) {
          lines.push(`<button type="button" class="button" data-route-reset ${locked ? "disabled" : ""}>Reset allowance to 0 ${escape(e.symbol)}</button> `);
        }
        if (e.needs_approval) {
          lines.push(`<button type="button" class="button" data-route-approve ${locked || e.needs_reset ? "disabled" : ""}>Approve ${escape(e.amount_input)} ${escape(e.symbol)}</button> `);
        }
        lines.push(`<button type="button" class="button" ${sale ? "data-route-sell" : "data-route-buy"} ${locked || e.needs_approval || (!sale && purchasedRouteInstructions.has(routeInstructionKey(e))) ? "disabled" : ""}>${sale ? "Sell" : deposit ? "Deposit" : "Buy"}</button>`);
      } else if (view.route) {
        lines.push(`<p>Signing is not available for this ${deposit ? "deposit" : "route"}. The reasons above are why; nothing has been sent.</p>`);
      }
    }
    lines.push("</div>");
    return lines.join("");
  }

  function routeSubmissionPanel(escape) {
    return routeSubmissions.filter(submissionVisible).map(tx => {
      const base = (EXPLORERS[tx.chain_id] || routeChainConfig(state.config)?.[tx.chain_id]?.explorer)?.replace(/\/+$/, "");
      const link = tx.hash && base
        ? `<a class="tx-link mono" href="${escape(base)}/tx/${escape(tx.hash)}" target="_blank" rel="noopener noreferrer">${escape(tx.hash.slice(0, 10))}…</a>`
        : escape(tx.hash || "Awaiting wallet outcome");
      return `<p>${escape(tx.label)}: ${link} · ${escape(tx.cross_chain_message || tx.status)}`
        + (tx.unresolved && tx.hash && !tx.tracking_expired ? ` <button type="button" class="button secondary" data-route-check="${escape(tx.hash)}" ${tx.polling ? "disabled" : ""}>Check again</button>` : "")
        + (tx.tracking_expired ? ` <a class="tx-link" href="https://scan.li.fi/tx/${escape(tx.hash.toLowerCase())}" target="_blank" rel="noopener noreferrer">LI.FI explorer</a>` : "")
        + (tx.unresolved ? " · Signing is locked until this submission is resolved." : "") + "</p>";
    }).join("");
  }

  async function selectExecutionRow(key) {
    const matches = routeRows.filter(item => {
      if (!item?.binding) return false;
      const token = String(item?.binding?.from_token || "").toLowerCase();
      return `${item.binding.from_chain}:${token}` === String(key || "").toLowerCase()
        || token === String(key || "").toLowerCase();
    });
    const row = matches.length === 1 ? matches[0] : null;
    if (!row?.binding || !state.selected) return;
    const binding = row.binding, instrument = state.selected, account = state.wallet.account;
    const accountGeneration = state.walletAccountGeneration;
    const selection = ++routeSelectionGeneration;
    routeSelectionMessage = "";
    execution = null;
    const stillSelected = () => selection === routeSelectionGeneration && state.selected === instrument
      && state.walletAccountGeneration === accountGeneration
      && String(state.wallet.account || "").toLowerCase() === String(account || "").toLowerCase();
    if (binding.from_chain !== state.wallet.chainId) {
      routeSelectionMessage = `Switching your wallet to ${routeChainName(binding.from_chain)}…`;
      renderRouteQuotes();
      let adding = false;
      try {
        const chainId = "0x" + binding.from_chain.toString(16);
        try {
          await state.provider.request({method: "wallet_switchEthereumChain", params: [{chainId}]});
        } catch (error) {
          if (!stillSelected()) return;
          if (Number(error?.code) !== 4902) throw error;
          const entry = routeChainConfig(state.config)?.[binding.from_chain];
          if (!entry?.native) throw error;
          adding = true;
          await state.provider.request({method: "wallet_addEthereumChain", params: [{
            chainId, chainName: entry.name,
            nativeCurrency: {name: entry.native.symbol, symbol: entry.native.symbol, decimals: 18},
            rpcUrls: [entry.rpc_url], ...(entry.explorer ? {blockExplorerUrls: [entry.explorer]} : {}),
          }]});
          if (!stillSelected()) return;
          adding = false;
          await state.provider.request({method: "wallet_switchEthereumChain", params: [{chainId}]});
        }
        if (!stillSelected()) return;
        const reported = normalizeChainId(await state.provider.request({method: "eth_chainId"}));
        if (!stillSelected()) return;
        if (reported !== binding.from_chain) throw new Error(`The wallet did not report ${routeChainName(binding.from_chain)}; no route was created.`);
        if (state.wallet.chainId !== reported) setWallet(account, reported);
        // The wallet may still announce this switch with its own event; that one is expected.
        appliedSwitch = {chainId: reported, at: Date.now()};
      } catch (error) {
        if (!stillSelected()) return;
        routeSelectionMessage = adding
          ? Number(error?.code) === 4001
            ? `The wallet refused to add ${routeChainName(binding.from_chain)}; no route was created.`
            : `Your wallet does not know ${routeChainName(binding.from_chain)} and adding it failed: ${boundedAdapterError(error)} No route was created.`
          : Number(error?.code) === 4902
          ? `Your wallet does not know ${routeChainName(binding.from_chain)}; no route was created.`
          : Number(error?.code) === 4001 ? "The wallet refused the chain switch; no route was created."
          : `The chain switch failed: ${boundedAdapterError(error)} No route was created.`;
        renderRouteQuotes();
        return;
      }
    }
    if (!stillSelected()) return;
    if (binding.from_chain !== instrument.chain_id) {
      // The preview checked the connected and destination chains; the paying chain is checked here,
      // on the wallet itself, because an account can be a contract on one chain and not another.
      let code = null;
      try { code = await state.provider.request({method: "eth_getCode", params: [account, "latest"]}); } catch (_) { code = null; }
      if (!stillSelected()) return;
      if (typeof code !== "string" || !/^0x(?:[0-9a-f]{2})*$/i.test(code)) {
        routeSelectionMessage = "Could not check whether this wallet is a smart contract, so cross-chain options are not offered.";
        renderRouteQuotes();
        return;
      }
      if (code !== "0x" && !code.toLowerCase().startsWith("0xef0100")) {
        routeSelectionMessage = "Smart-contract wallets can buy only on the instrument's own chain here: the same address on another chain may not be yours.";
        renderRouteQuotes();
        return;
      }
    }
    routeSelectionMessage = "";
    executionGeneration += 1;
    const balanceRaw = row.balance_raw ?? binding.from_amount;
    const balance = Routes.formatAmount(balanceRaw, binding.from_decimals);
    execution = {
      kind: "route", generation: executionGeneration, wallet_generation: state.walletGeneration,
      account, chain_id: binding.from_chain, instrument_chain_id: instrument.chain_id, instrument: {...instrument},
      instrument_symbol: instrument.symbol || instrument.name || binding.to_token,
      pay: {address: binding.from_token, decimals: binding.from_decimals, symbol: row.symbol, chain_id: binding.from_chain},
      symbol: row.symbol, balance_raw: balanceRaw, balance_display: balance,
      amount_input: Routes.formatAmount(binding.from_amount, binding.from_decimals), phase: "IDLE", view: null, data: null, error: null,
      signable: false, needs_approval: false, sending: false,
    };
    renderRouteQuotes();
  }

  async function selectSalePosition(key) {
    const selected = salePositions.get(String(key || "").toLowerCase());
    if (!selected || !state.provider || !state.wallet.account || state.sampleVisible) return;
    const {position, instrument} = selected, account = state.wallet.account;
    const chainId = Number(instrument.chain_id), address = String(instrument.address || "").toLowerCase();
    const settlementMap = new Map();
    const registeredUsdc = REGISTRY_CHAINS.find(chain => chain.chain_id === chainId)?.usdc;
    if (registeredUsdc && configuredAddress(registeredUsdc.address) && Number.isInteger(registeredUsdc.decimals)) {
      settlementMap.set(registeredUsdc.address.toLowerCase(), {address: registeredUsdc.address,
        decimals: registeredUsdc.decimals, symbol: registeredUsdc.symbol, chain_id: chainId});
    }
    for (const asset of state.universe || []) {
      if (String(asset?.category).toLowerCase() !== "settlement" || asset.chain_id !== chainId
        || !configuredAddress(asset.address) || !Number.isInteger(asset.decimals) || typeof asset.symbol !== "string") continue;
      settlementMap.set(asset.address.toLowerCase(), {address: asset.address, decimals: asset.decimals,
        symbol: asset.symbol, chain_id: chainId});
    }
    const settlements = [...settlementMap.values()];
    if (!settlements.length) {
      routeSelectionMessage = "No unambiguous settlement asset is available for this instrument chain.";
      renderRouteQuotes(); return;
    }
    const selection = ++routeSelectionGeneration, accountGeneration = state.walletAccountGeneration;
    const current = () => selection === routeSelectionGeneration && accountGeneration === state.walletAccountGeneration
      && String(state.wallet.account || "").toLowerCase() === String(account || "").toLowerCase();
    routeSelectionMessage = ""; execution = null;
    if (state.wallet.chainId !== chainId) {
      routeSelectionMessage = `Switching your wallet to ${routeChainName(chainId)}…`;
      renderRouteQuotes();
      try {
        const chainHex = `0x${chainId.toString(16)}`;
        try { await state.provider.request({method: "wallet_switchEthereumChain", params: [{chainId: chainHex}]}); }
        catch (error) {
          if (!current()) return;
          if (Number(error?.code) !== 4902) throw error;
          const entry = routeChainConfig(state.config)?.[chainId];
          if (!entry?.native) throw error;
          await state.provider.request({method: "wallet_addEthereumChain", params: [{chainId: chainHex,
            chainName: entry.name, nativeCurrency: {name: entry.native.symbol, symbol: entry.native.symbol, decimals: 18},
            rpcUrls: [entry.rpc_url], ...(entry.explorer ? {blockExplorerUrls: [entry.explorer]} : {})}]});
          if (!current()) return;
          await state.provider.request({method: "wallet_switchEthereumChain", params: [{chainId: chainHex}]});
        }
        if (!current()) return;
        const reported = normalizeChainId(await state.provider.request({method: "eth_chainId"}));
        if (!current()) return;
        if (reported !== chainId) throw new Error(`The wallet did not report ${routeChainName(chainId)}.`);
        if (state.wallet.chainId !== reported) setWallet(account, reported);
        appliedSwitch = {chainId: reported, at: Date.now()};
      } catch (error) {
        if (!current()) return;
        routeSelectionMessage = Number(error?.code) === 4001
          ? "The wallet refused the chain switch; no sale route was created."
          : `Could not switch to ${routeChainName(chainId)}: ${boundedAdapterError(error)} No sale route was created.`;
        renderRouteQuotes(); return;
      }
    }
    if (!current()) return;
    const receive = settlements.find(asset => asset.symbol.toUpperCase() === "USDC") || settlements[0];
    executionGeneration += 1;
    execution = {kind: "sale", generation: executionGeneration, wallet_generation: state.walletGeneration,
      account, chain_id: chainId, instrument_chain_id: chainId, instrument: {...instrument},
      instrument_symbol: instrument.symbol || instrument.name || address,
      pay: {address, decimals: position.decimals, symbol: instrument.symbol || instrument.name || "token", chain_id: chainId},
      receive_options: settlements.map(asset => ({address: asset.address, decimals: asset.decimals, symbol: asset.symbol, chain_id: chainId})),
      receive: {address: receive.address, decimals: receive.decimals, symbol: receive.symbol, chain_id: chainId},
      symbol: instrument.symbol || instrument.name || address, balance_raw: String(position.raw_units),
      balance_display: Routes.formatAmount(position.raw_units, position.decimals),
      amount_input: Routes.formatAmount(position.raw_units, position.decimals), phase: "IDLE", view: null, data: null,
      error: null, signable: false, needs_approval: false, needs_reset: false, sending: false};
    routeSelectionMessage = "";
    renderRouteQuotes();
  }

  function executionIsCurrent(capture) {
    const deposit = capture?.kind === "lending" ? lendingEligibility(state.selected)
      : capture?.kind === "deposit" ? depositEligibility(state.selected) : null;
    return Boolean(execution && capture && execution.generation === capture.generation
      && state.walletGeneration === capture.wallet_generation
      && (capture.kind === "sale" || instrumentIdentity(capture.instrument) === instrumentIdentity(state.selected))
      && (!deposit || (!deposit.error && sameAddress(deposit.pay?.address, capture.pay?.address)
        && deposit.pay.decimals === capture.pay.decimals && deposit.pay.symbol === capture.pay.symbol))
      && state.wallet.chainId === capture.chain_id
      && String(state.wallet.account || "").toLowerCase() === String(capture.account || "").toLowerCase());
  }

  async function fetchExecutionRoute(capture, amountRaw, acceptCostBps) {
    const deposit = capture.kind === "deposit", sale = capture.kind === "sale";
    const instrumentChain = capture.instrument_chain_id ?? capture.instrument.chain_id;
    const crossChain = !deposit && !sale && capture.chain_id !== instrumentChain;
    const operation = deposit ? {
      schema_version: "fenorium/platform-position-deposit-request/1",
      method_version: "deposit/1",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      wallet: capture.account.toLowerCase(), chain_id: capture.chain_id,
      venue: capture.instrument.venue, instrument: capture.instrument.name, amount_raw: amountRaw,
    } : sale ? {
      schema_version: "fenorium/platform-sale-route-request/1",
      method_version: "route/1",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      wallet: capture.account.toLowerCase(), chain_id: instrumentChain,
      instrument_address: capture.instrument.address,
      amount_raw: amountRaw,
      receive_with: {address: capture.receive.address, decimals: capture.receive.decimals,
        symbol: String(capture.receive.symbol || "").slice(0, 32), chain_id: capture.receive.chain_id},
      ...(acceptCostBps === undefined ? {} : {accept_cost_bps: acceptCostBps}),
    } : {
      schema_version: crossChain ? "fenorium/platform-execution-route-request/2" : "fenorium/platform-execution-route-request/1",
      method_version: "route/1",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      wallet: capture.account.toLowerCase(),
      chain_id: instrumentChain,
      instrument_address: capture.instrument.address,
      pay_with: {address: capture.pay.address, decimals: capture.pay.decimals, symbol: String(capture.pay.symbol || "").slice(0, 32),
        ...(crossChain ? {chain_id: capture.chain_id} : {})},
      amount_raw: amountRaw,
      ...(acceptCostBps === undefined ? {} : {accept_cost_bps: acceptCostBps}),
    };
    const routeKind = deposit ? "deposit" : sale ? "sale" : "route";
    const response = await root.fetch(deposit ? "/v1/position-deposits" : sale ? "/v1/sale-routes" : "/v1/execution-routes", {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": `${routeKind}-${PORTFOLIO_SESSION_ID}-${capture.generation}-${capture.request}`,
      },
      body: JSON.stringify(operation),
    });
    let envelope = null;
    try { envelope = await response.json(); } catch (_) { envelope = null; }
    if (!response.ok) throw makeCodeError(envelope?.error || "ROUTE_REQUEST_FAILED", envelope?.message || `route request failed (HTTP ${response.status})`);
    if (!validPlatformEnvelope(envelope) || !object(envelope.data)) {
      throw makeCodeError("INVALID_ROUTE_RESPONSE", "route HTTP 200 response failed its shape and execution-consistency checks");
    }
    // The server's current answer. A tab left open across a restart that changed the gate or the
    // fee must not go on describing the old deployment beside an instruction from the new one.
    if (envelope.execution_enabled !== state.serverExecution || envelope.fees_enabled !== state.serverFees) {
      applyDeploymentState(envelope);
      renderMarket();
      renderSelectedAsset();
    }
    const data = envelope.data;
    if (!deposit && !sale && ((crossChain || data.pay_chain_id !== undefined) && data.pay_chain_id !== capture.chain_id
      || data.route && (data.route.from_chain !== capture.chain_id || data.route.to_chain !== instrumentChain))) {
      throw new Error("The route response has inconsistent payment or destination chains.");
    }
    return data;
  }

  // What the wallet has already allowed the spender to take, read from the chain rather than
  // assumed. An allowance that already covers the amount needs no second approval.
  async function readAllowance(owner, token, spender) {
    const word = value => value.toLowerCase().replace(/^0x/, "").padStart(64, "0");
    const raw = await state.provider.request({method: "eth_call", params: [{to: token, data: "0xdd62ed3e" + word(owner) + word(spender)}, "latest"]});
    return typeof raw === "string" && /^0x[0-9a-f]+$/i.test(raw) ? BigInt(raw) : null;
  }

  // The evidence we simulate and the instruction we sign must describe the same deposit,
  // using only the universe's market and settlement units.
  function validateDepositResponse(e, data) {
    const row = e.instrument, r = data.route, p = data.instrument, tx = data.unsigned_transaction_request;
    const sameAddress = (a, b) => configuredAddress(a) && configuredAddress(b) && a.toLowerCase() === b.toLowerCase();
    if (data.route_provenance !== "SERVER_BUILT" || !r || !p || !object(tx)
      || !sameAddress(data.wallet, e.account) || !sameAddress(data.pay_address, e.pay.address)
      || !sameAddress(r.from_token, e.pay.address) || !sameAddress(r.to_token, row.market_address)
      || r.from_chain !== e.chain_id || r.to_chain !== e.chain_id
      || r.from_decimals !== e.pay.decimals || r.to_decimals !== e.pay.decimals
      || p.maturity !== row.maturity || p.maturity_timestamp !== positionMaturity(row)
      || p.underlying_decimals !== e.pay.decimals || p.underlying_symbol !== e.pay.symbol
      || p.rate_bps !== row.rate_bps || !sameAddress(p.underlying_address, e.pay.address)
      || !sameAddress(p.market_address, row.market_address)
      || !sameAddress(tx.to, row.market_address) || !sameAddress(tx.from, e.account)
      || tx.chainId !== e.chain_id || BigInt(tx.value || "0") !== 0n) {
      throw new Error("The deposit response does not match this wallet, position and settlement asset.");
    }
    const approval = data.unsigned_approval_request;
    if (!object(approval) || approval.chainId !== e.chain_id || !sameAddress(approval.from, e.account)
      || !sameAddress(approval.to, e.pay.address) || !sameAddress(approval.spender, row.market_address)
      || approval.amount !== data.amount_raw || BigInt(approval.value || "0") !== 0n
      || String(approval.data).toLowerCase() !== "0x095ea7b3" + encodeAddress(row.market_address).toLowerCase() + encodeUint256(data.amount_raw)) {
      throw new Error("The deposit approval does not approve the market for the exact amount.");
    }
    if (data.broadcast_enabled) {
      for (const key of ["transaction_request", "approval_request", "approval_reset_request"]) {
        const instruction = data[key], evidence = data["unsigned_" + key];
        if (instruction == null && evidence == null) continue;
        if (!object(instruction) || !object(evidence)
          || ["chainId", "from", "to", "data", "value", "gasLimit", "spender", "amount"].some(field => instruction[field] !== evidence[field])) {
          throw new Error("The deposit instruction differs from the server's unsigned evidence.");
        }
      }
    }
    // depositAtMaturity(uint256,uint256,uint256,address), derived with the server's keccak.
    // This is a comparison only: the server's original bytes remain the instruction.
    const expectedCall = "0x34f7d1f2" + encodeUint256(p.maturity_timestamp) + encodeUint256(data.amount_raw)
      + encodeUint256(r.to_amount_min) + encodeAddress(e.account).toLowerCase();
    if (String(tx.data).toLowerCase() !== expectedCall) {
      throw new Error("The deposit call does not bind the shown maturity, amount, floor and wallet.");
    }
  }

  async function simulateDeposit(capture, needsApproval) {
    if (!routeRequestIsCurrent(capture)) return;
    const e = execution, provider = state.provider, tx = e.data.unsigned_transaction_request;
    const chain = await provider.request({method: "eth_chainId"});
    if (!routeRequestIsCurrent(capture)) return;
    if (normalizeChainId(chain) !== capture.chain_id) throw new Error(`Switch your wallet to ${chainName(capture.chain_id)} before simulating the deposit.`);
    const call = {from: capture.account, to: tx.to, data: tx.data, value: "0x" + BigInt(tx.value || "0").toString(16)};
    if (tx.gasLimit) call.gas = "0x" + BigInt(tx.gasLimit).toString(16);
    try {
      const result = await provider.request({method: "eth_call", params: [call, "latest"]});
      if (typeof result !== "string" || !/^0x(?:[0-9a-f]{2})*$/i.test(result)) throw new Error("The simulation returned an invalid result.");
    } catch (error) {
      if (!routeRequestIsCurrent(capture)) return;
      const data = extractErrorData(error);
      if (data?.toLowerCase().startsWith("0xb06dad04")) {
        throw new Error("At this size the pool pays less than 90% of the priced rate; a smaller deposit may clear it. Nothing was sent.");
      }
      // Token transfer follows the floor check. Only an actual revert is expected here;
      // transport failures provide no evidence that the floor was checked.
      const reverted = data !== null || Number(error?.code) === 3 || /revert/i.test(String(error?.message || ""));
      if (needsApproval && reverted) return;
      throw new Error(`Deposit simulation failed: ${boundedAdapterError(error)} (${decodeError(error).message}). Nothing was sent.`);
    }
  }

  async function requestExecutionRoute(amountText, acceptCostBps) {
    if (execution?.kind === "lending") return requestLendingOrder(amountText);
    if (!execution || !executionIsCurrent(execution)) return;
    const e = execution;
    if (acceptCostBps !== undefined && (!(["sale", "route"].includes(e.kind)) || e.amount_changed
      || e.phase === "LOADING" || e.sending || routeLocked(e)
      || acceptCostBps !== e.view?.cost_acceptance?.route_cost_bps
      || Routes.parseAmount(String(amountText ?? ""), e.pay.decimals) !== e.amount_raw)) return;
    e.request = (e.request || 0) + 1;
    const amountRaw = Routes.parseAmount(String(amountText ?? ""), e.pay.decimals);
    e.amount_input = String(amountText ?? "").trim();
    e.view = null; e.data = null; e.signable = false; e.needs_approval = false; e.amount_changed = false; e.phase = "IDLE";
    e.approval_hash = null;
    if (amountRaw === null) { e.error = `Enter a positive amount with at most ${e.pay.decimals} decimals.`; renderRouteQuotes(); return; }
    // The balance check: a route for more than the wallet holds would only revert once signed.
    if (BigInt(amountRaw) > BigInt(e.balance_raw)) { e.error = `That is more than the ${e.balance_display} ${e.symbol} this wallet holds.`; renderRouteQuotes(); return; }
    e.error = null; e.phase = "LOADING";
    const capture = {...e};
    renderRouteQuotes();
    try {
      const data = await fetchExecutionRoute(capture, amountRaw, acceptCostBps);
      if (!executionIsCurrent(capture) || execution.request !== capture.request) return;
      const view = e.kind === "sale"
        ? Routes.saleRouteView(data, {wallet: e.account, instrument_address: e.instrument.address,
          instrument_chain_id: e.instrument_chain_id, instrument_decimals: e.pay.decimals,
          receive_address: e.receive.address, receive_chain_id: e.receive.chain_id,
          receive_decimals: e.receive.decimals, amount_raw: amountRaw}, Date.now())
        : Routes.executionRouteView(data, Date.now());
      if (!view) throw makeCodeError("INVALID_ROUTE_RESPONSE", "the route response contradicted itself and was not shown");
      if (e.kind === "route" && data.cost_accepted_bps != null
        && (acceptCostBps == null || data.cost_accepted_bps !== acceptCostBps)) {
        throw makeCodeError("INVALID_ROUTE_RESPONSE", "the route cost acceptance did not match this request");
      }
      if (e.kind === "deposit" && !["DEPOSIT_READY", "REJECTED", "ROUTE_UNAVAILABLE"].includes(data.status)) {
        throw new Error("The server did not return a position deposit response.");
      }
      if ((data.amount_raw != null && data.amount_raw !== amountRaw)
        || (data.route && (data.amount_raw !== amountRaw || data.route.from_amount !== amountRaw))) {
        throw new Error("The route amount does not match the requested amount.");
      }
      if (e.kind === "sale") validateSaleResponse(e, data, amountRaw);
      if (e.kind === "deposit" && data.status === "DEPOSIT_READY") validateDepositResponse(e, data);
      e.data = data; e.view = view; e.amount_raw = amountRaw;
      // Two places have to agree before anything can be signed: the server's gate and this
      // build's own configuration. Either one saying no is enough.
      await refreshRouteAllowance(capture);
    } catch (error) {
      if (!routeRequestIsCurrent(capture)) return;
      e.signable = false;
      e.error = `Could not obtain a ${e.kind === "deposit" ? "deposit" : "route"}: ${boundedAdapterError(error)}`;
    } finally {
      if (routeRequestIsCurrent(capture)) { e.phase = "IDLE"; renderRouteQuotes(); }
    }
  }

  function validateSaleResponse(e, data, amountRaw) {
    const same = (a, b) => configuredAddress(a) && configuredAddress(b) && a.toLowerCase() === b.toLowerCase();
    const receive = data.receive_with;
    if (!object(receive) || !same(receive.address, e.receive.address) || receive.chain_id !== e.receive.chain_id
      || receive.decimals !== e.receive.decimals || receive.symbol !== e.receive.symbol) {
      throw new Error("The response receive asset does not match the selected settlement asset.");
    }
    if (data.route === null) {
      if (data.direction !== "SELL" || data.broadcast_enabled === true) throw new Error("A refused sale response included an executable instruction.");
      return;
    }
    if (data.direction !== "SELL" || !same(data.wallet, e.account) || !same(data.pay_address, e.instrument.address)
      || data.pay_chain_id !== e.instrument_chain_id || !same(data.receive_address, e.receive.address)
      || data.receive_chain_id !== e.receive.chain_id
      || !same(data.instrument?.address, e.instrument.address) || data.instrument?.chain_id !== e.instrument_chain_id
      || data.instrument?.decimals !== e.pay.decimals || data.amount_raw !== amountRaw) {
      throw new Error("The sale response does not match this wallet, instrument, receive asset and amount.");
    }
    if (!same(data.recipient, e.account) || !same(data.route?.from_token, e.instrument.address) || !same(data.route?.to_token, e.receive.address)
      || data.route?.from_chain !== e.instrument_chain_id || data.route?.to_chain !== e.receive.chain_id
      || data.route?.from_decimals !== e.pay.decimals || data.route?.to_decimals !== e.receive.decimals
      || data.route?.from_amount !== amountRaw) {
      throw new Error("The sale route does not match the selected instrument, receive asset and amount.");
    }
    if (data.broadcast_enabled !== true) return;
    const tx = data.unsigned_transaction_request, approval = data.unsigned_approval_request;
    if (!object(tx) || !object(approval)
      || !same(tx.from, e.account) || tx.chainId !== e.instrument_chain_id
      || BigInt(tx.value || "0") !== 0n || !configuredAddress(tx.to)
      || !same(approval.from, e.account) || approval.chainId !== e.instrument_chain_id
      || !same(approval.to, e.instrument.address) || !same(approval.spender, tx.to)
      || !same(approval.spender, data.route.approval_address)
      || approval.amount !== amountRaw || BigInt(approval.value || "0") !== 0n
      || String(approval.data).toLowerCase() !== "0x095ea7b3" + encodeAddress(approval.spender).toLowerCase() + encodeUint256(amountRaw)) {
      throw new Error("The sale approval or transaction does not match this wallet, instrument and amount.");
    }
    const reset = data.unsigned_approval_reset_request;
    if (reset != null) {
      const resetData = "0x095ea7b3" + encodeAddress(approval.spender).toLowerCase() + "0".repeat(64);
      if (!object(reset) || reset.chainId !== approval.chainId || !same(reset.from, e.account)
        || !same(reset.to, e.instrument.address) || !same(reset.spender, approval.spender)
        || reset.amount !== "0" || BigInt(reset.value || "0") !== 0n
        || String(reset.data).toLowerCase() !== resetData) throw new Error("Invalid sale allowance reset instruction.");
    }
    if (data.broadcast_enabled) {
      for (const [instructionKey, evidenceKey] of [["transaction_request", "unsigned_transaction_request"],
        ["approval_request", "unsigned_approval_request"], ["approval_reset_request", "unsigned_approval_reset_request"]]) {
        const instruction = data[instructionKey], evidence = data[evidenceKey];
        if (instruction == null && evidence == null) continue;
        if (!object(instruction) || !object(evidence)
          || ["chainId", "from", "to", "data", "value", "gasLimit", "gasPrice", "spender", "amount"].some(field => instruction[field] !== evidence[field])) {
          throw new Error("The sale instruction differs from the server's unsigned evidence.");
        }
      }
    }
  }

  async function refreshRouteAllowance(capture) {
    if (capture?.kind === "lending") return refreshLendingAllowance(capture);
    if (!routeRequestIsCurrent(capture)) return;
    const e = execution, data = e.data;
    e.signable = false;
    const allowanceGeneration = e.allowance_generation = (e.allowance_generation || 0) + 1;
    renderRouteQuotes();
    e.needs_approval = false; e.needs_reset = false;
    const deposit = e.kind === "deposit" && data?.status === "DEPOSIT_READY";
    if ((!e.view?.can_sign && !deposit) || e.amount_changed || !routeFresh(e)
      || Routes.parseAmount(e.amount_input, e.pay.decimals) !== e.amount_raw) return;
    if (!deposit && !expectsExecution()) return;
    const approvalRequest = deposit ? data.unsigned_approval_request : data.approval_request;
    if (object(approvalRequest)) {
      if (String(approvalRequest.spender).toLowerCase() !== String((deposit ? data.unsigned_transaction_request : data.transaction_request)?.to).toLowerCase()) {
        throw new Error("The route asked to approve a different contract from the one it calls, so it was not offered.");
      }
      const allowance = await readAllowance(capture.account, approvalRequest.to, approvalRequest.spender);
      if (!routeRequestIsCurrent(capture) || e.allowance_generation !== allowanceGeneration) return;
      if (allowance === null) throw new Error("Allowance could not be read.");
      e.needs_approval = allowance < BigInt(e.amount_raw);
      const reset = deposit ? data.unsigned_approval_reset_request : data.approval_reset_request, approval = approvalRequest;
      if (reset != null) {
        const expected = "0x095ea7b3" + String(approval.spender).slice(2).toLowerCase().padStart(64, "0") + "0".repeat(64);
        if (!object(reset) || reset.chainId !== approval.chainId
          || String(reset.from).toLowerCase() !== String(approval.from).toLowerCase()
          || String(reset.to).toLowerCase() !== String(approval.to).toLowerCase()
          || String(reset.spender).toLowerCase() !== String(approval.spender).toLowerCase()
          || reset.amount !== "0" || BigInt(reset.value || "0") !== 0n
          || String(reset.data).toLowerCase() !== expected) throw new Error("Invalid allowance reset instruction.");
      }
      e.needs_reset = e.needs_approval && allowance > 0n && object(reset);
    }
    if (deposit) await simulateDeposit(capture, e.needs_approval);
    if (routeRequestIsCurrent(capture) && e.allowance_generation === allowanceGeneration && routeFresh(e)) {
      e.signable = e.view.can_sign && expectsExecution() && !e.amount_changed;
      e.error = null;
    }
  }

  // A Safe reports success on the outer transaction even when its inner call failed; the ExecutionFailure
  // event it emits is what says nothing happened (deploy/platform/route_tracker.py checks the same).
  const SAFE_EXECUTION_FAILURE_TOPIC = "0x23428b18acfb3ea64b08dc0c1d296ea9c09702c09083ca5272e64d115b687d23";
  function safeExecutionFailed(receipt, account) {
    return receipt?.status === "CONFIRMED" && Array.isArray(receipt.logs) && receipt.logs.some(log =>
      typeof log?.address === "string" && log.address.toLowerCase() === String(account || "").toLowerCase()
      && Array.isArray(log.topics) && String(log.topics[0]).toLowerCase() === SAFE_EXECUTION_FAILURE_TOPIC);
  }

  async function waitForRouteReceipt(hash, isCurrent, reader = state.provider) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await wait(3000);
      if (!isCurrent()) return {status: "UNKNOWN", block_number: null};
      const receipt = await reader.request({method: "eth_getTransactionReceipt", params: [hash]}).catch(() => null);
      if (!isCurrent()) return {status: "UNKNOWN", block_number: null};
      if (receipt && receiptSucceeded(receipt)) return {status: "CONFIRMED", block_number: receipt.blockNumber, logs: receipt.logs};
      if (receipt && receiptReverted(receipt)) return {status: "REVERTED", block_number: receipt.blockNumber};
    }
    return {status: "PENDING", block_number: null};
  }

  async function refreshAfterRoute(tx, afterRead = null) {
    if (tx.account !== String(state.wallet.account || "").toLowerCase()) return;
    const account = state.wallet.account, chainId = tx.chain_id, walletGeneration = state.walletGeneration;
    const activeChainId = state.wallet.chainId;
    const sameWallet = scope => scope && scope.account === state.wallet.account
      && scope.activeChainId === state.wallet.chainId && scope.walletGeneration === state.walletGeneration;
    const target = Math.max(tx.receipt_block || 0, sameWallet(depositRefresh) && depositRefresh.chainId === chainId ? depositRefresh.target : 0);
    const scope = {account, chainId, activeChainId, walletGeneration, target, afterRead: afterRead || (sameWallet(depositRefresh) ? depositRefresh.afterRead : null)};
    depositRefresh = scope;
    if (depositRefreshTimer !== null) root.clearTimeout(depositRefreshTimer);
    depositRefreshTimer = null;
    const current = () => depositRefresh === scope && sameWallet(scope);
    const destinationChain = tx.received?.chain_id || tx.to_chain;
    const crossChainSale = tx.cross_chain && tx.label === "Sale";
    let destinationBlock = null;
    const readDestinationBlock = async () => {
      if (!crossChainSale || destinationBlock !== null || !/^0x[0-9a-f]{64}$/i.test(tx.destination_tx || "")) return;
      const rpc = routeChainConfig(state.config)?.[destinationChain];
      if (!rpc) return;
      const receipt = await boundedRouteRpc(rpc, 10000).request({method: "eth_getTransactionReceipt", params: [tx.destination_tx]}).catch(() => null);
      const block = receipt?.blockNumber == null ? NaN : Number(receipt.blockNumber);
      if (receiptSucceeded(receipt) && Number.isSafeInteger(block) && block > 0) destinationBlock = block;
    };
    const reached = () => {
      const record = portfolioRecord(account, state.wallet.chainId);
      return record?.source === "server" && target > 0 && record.readBlocks?.[chainId] >= target
        && (!crossChainSale || (destinationBlock !== null && record.readBlocks?.[destinationChain] >= destinationBlock));
    };
    await readDestinationBlock();
    if (!current()) return;
    await refreshPortfolio({preserveExitRequests: !!scope.afterRead, scope: "connected"});
    if (!current()) return;
    if (scope.afterRead) await scope.afterRead();
    if (!current()) return;
    if (reached() || (scope.afterRead && !target && !crossChainSale)) { depositRefresh = null; return; }
    const browserRead = portfolioRecord(account, state.wallet.chainId)?.source === "browser";
    let rereads = 0;
    const schedule = () => {
      depositRefreshTimer = root.setTimeout(async () => {
        if (!current()) return;
        depositRefreshTimer = null;
        await readDestinationBlock();
        if (!current()) return;
        await refreshPortfolio({preserveExitRequests: !!scope.afterRead, scope: "connected"});
        if (!current()) return;
        if (scope.afterRead) await scope.afterRead();
        if (!current()) return;
        rereads += 1;
        if (browserRead || portfolioRecord(account, state.wallet.chainId)?.source === "browser" || reached() || rereads >= DEPOSIT_REFRESH_LIMIT) {
          depositRefresh = null;
          return;
        }
        schedule();
      }, DEPOSIT_REFRESH_DELAY_MS);
    };
    schedule();
  }

  function crossChainStatusMessage(tx, data) {
    const destination = routeChainName(tx.to_chain), source = routeChainName(tx.from_chain);
    const received = data.received;
    // LI.FI may not name the token; its address is then the only honest name for it.
    const quantity = received ? `${Routes.formatAmount(received.amount, received.decimals)} ${received.symbol || received.token}` : null;
    if (!data.terminal) return `Bridging — LI.FI reports ${data.substatus || data.status}.`;
    if (data.state === "SETTLED") return quantity ? `Delivered on ${destination}: ${quantity}.` : `Delivered on ${destination}.`;
    // LI.FI's own failure code, and whether a refund is still the buyer's to claim.
    const recovery = data.recovery_required
      ? ` LI.FI reports ${data.substatus || data.status}: a refund must be claimed. Follow it on LI.FI's explorer: https://scan.li.fi/tx/${tx.hash.toLowerCase()}`
      : "";
    const action = tx.direction === "SELL" ? "sale" : "purchase";
    if (data.state === "FAILED_DESTINATION") return (quantity
      ? `The ${action} did not complete on ${destination}. You received ${quantity} on ${routeChainName(received.chain_id)} instead of ${tx.direction === "SELL" ? tx.receive_symbol : tx.instrument_symbol}.`
      : `The ${action} did not complete on ${destination}; LI.FI has not said what you received.`) + recovery;
    // Where the refund landed, as LI.FI reports it; the source chain only when it does not say.
    if (data.state === "REFUNDED") return `Refunded on ${received ? routeChainName(received.chain_id) : source}.` + (quantity ? ` ${quantity}.` : "");
    return [data.status, data.substatus].filter(value => value != null).join(" · ");
  }

  async function fetchCrossChainStatus(tx, timeout) {
    const controller = typeof root.AbortController === "function" ? new root.AbortController() : null;
    let timer;
    const read = async () => {
      const response = await root.fetch(`/v1/executions/${tx.hash.toLowerCase()}?from_chain=${tx.from_chain}&to_chain=${tx.to_chain}`,
        {method: "GET", cache: "no-store", headers: {Accept: "application/json"}, signal: controller?.signal});
      let envelope = null;
      try { envelope = await response.json(); } catch (_) { /* Invalid evidence. */ }
      if (!response.ok) {
        const error = new Error(envelope?.message || `Execution status unavailable (HTTP ${response.status}).`);
        error.http_status = response.status;
        throw error;
      }
      const data = envelope?.data;
      if (!validPlatformEnvelope(envelope) || !object(data)
        || data.schema_version !== "fenorium/platform-execution-status/1"
        || data.source_tx !== tx.hash.toLowerCase() || data.from_chain !== tx.from_chain || data.to_chain !== tx.to_chain
        || typeof data.terminal !== "boolean" || typeof data.recovery_required !== "boolean" || typeof data.status !== "string"
        || (data.substatus !== null && typeof data.substatus !== "string")
        || (data.state !== null && typeof data.state !== "string")) throw new Error("Inconsistent execution status response was refused.");
      if (data.received !== null) {
        const r = data.received;
        if (!object(r) || !Number.isSafeInteger(r.chain_id) || r.chain_id <= 0 || typeof r.token !== "string" || !/^0x[0-9a-f]{40}$/i.test(r.token)
          || (r.symbol !== null && typeof r.symbol !== "string") || Routes.formatAmount(r.amount, r.decimals) === null) {
          throw new Error("Invalid received-token evidence was refused.");
        }
      }
      if (envelope.execution_enabled !== state.serverExecution || envelope.fees_enabled !== state.serverFees) {
        applyDeploymentState(envelope);
        renderMarket();
        renderSelectedAsset();
      }
      return data;
    };
    try {
      return await Promise.race([read(), new Promise((_, reject) => {
        timer = root.setTimeout(() => { controller?.abort(); reject(new Error("Execution status read timed out.")); }, timeout);
      })]);
    } finally { root.clearTimeout(timer); controller?.abort(); }
  }

  async function followCrossChainSubmission(tx) {
    tx.tracking_started_at ??= Date.now();
    const deadline = tx.tracking_started_at + 60 * 60 * 1000;
    let delay = 15000, busy = 0;
    tx.cross_chain_message ||= `Source transaction confirmed. Waiting for LI.FI to report the ${tx.direction === "SELL" ? "sale" : "purchase"} settlement.`;
    renderRouteQuotes();
    while (tx.unresolved && Date.now() < deadline) {
      await wait(Math.min(delay, Math.max(0, deadline - Date.now())));
      if (Date.now() >= deadline) break;
      try {
        const data = await fetchCrossChainStatus(tx, Math.min(15000, deadline - Date.now()));
        tx.cross_chain_message = crossChainStatusMessage(tx, data);
        tx.destination_tx = data.destination_tx;
        tx.received = data.received;
        tx.status = data.state || data.status;
        tx.unresolved = !data.terminal;
        busy = 0; delay = 15000;
      } catch (error) {
        // Missing or inconsistent evidence never releases the submission lock.
        tx.cross_chain_message = `Could not check cross-chain settlement: ${boundedAdapterError(error)} The ${tx.direction === "SELL" ? "sale" : "purchase"} remains unresolved.`;
        delay = error.http_status === 503 ? (++busy === 1 ? 30000 : 60000) : 15000;
        if (error.http_status !== 503) busy = 0;
      }
      renderRouteQuotes();
    }
    if (tx.unresolved) {
      tx.tracking_expired = true;
      tx.cross_chain_message = `Still not final after an hour. Check it on LI.FI's explorer: https://scan.li.fi/tx/${tx.hash.toLowerCase()}`;
    }
  }

  // Sends exactly what the server returned as an instruction, after checking it is addressed from
  // this wallet on this chain. Never retries: a second send before the first is known is how a
  // purchase happens twice.
  // A crossing is followed wherever the wallet is pointed afterwards: its source receipt is read on
  // its own chain through the read-only RPC, and its settlement from the server, so switching to the
  // destination chain to look for the purchase does not abandon it.
  const crossingScopeMatches = tx => tx.cross_chain && tx.account === String(state.wallet.account || "").toLowerCase();
  // The purchase lock covers an instrument across chains, so anything holding it must stay in view
  // and checkable from whichever chain the wallet is on now.
  const submissionVisible = tx => routeScopeMatches(tx)
    || (tx.account === String(state.wallet.account || "").toLowerCase() && (tx.cross_chain || tx.unresolved));

  // Each read gives up after `timeout` ms: a stalled public RPC must hand control back, so tracking
  // keeps its own deadline and "Check again" is never left disabled.
  function boundedRouteRpc(entry, timeout) {
    return {request: async args => {
      const controller = typeof root.AbortController === "function" ? new root.AbortController() : null;
      let timer;
      try {
        return await Promise.race([routeRpc(entry, root.fetch, controller?.signal).request(args), new Promise((_, reject) => {
          timer = root.setTimeout(() => { controller?.abort(); reject(new Error("RPC read timed out.")); }, timeout);
        })]);
      } finally { root.clearTimeout(timer); controller?.abort(); }
    }};
  }

  async function checkRouteSubmission(hash) {
    const tx = routeSubmissions.find(item => item.hash === hash && submissionVisible(item));
    if (!tx || !tx.unresolved || tx.polling || tx.tracking_expired) return;
    tx.polling = true;
    renderRouteQuotes();
    try {
      // Read on the chain the transaction was sent on: through the wallet while it is there, through
      // the read-only RPC for a crossing or once the wallet has moved on.
      const onOwnChain = routeScopeMatches(tx);
      const sourceRpc = tx.cross_chain || !onOwnChain ? routeChainConfig(state.config)?.[tx.chain_id] : null;
      const sameAccount = () => tx.account === String(state.wallet.account || "").toLowerCase();
      if (!tx.source_confirmed) {
        const receipt = sourceRpc
          ? await waitForRouteReceipt(hash, sameAccount, boundedRouteRpc(sourceRpc, 10000))
          : await waitForRouteReceipt(hash, () => routeScopeMatches(tx));
        tx.status = receipt.status;
        if (safeExecutionFailed(receipt, tx.account)) {
          tx.status = "REVERTED";
          tx.cross_chain_message = `The Safe executed the transaction but its inner call failed; nothing was ${tx.label === "Sale" ? "sold" : "bought"}.`;
        }
        const block = receipt.block_number == null ? NaN : Number(receipt.block_number);
        if (Number.isSafeInteger(block) && block >= 0) tx.receipt_block = block;
      }
      if (tx.cross_chain && (tx.source_confirmed || tx.status === "CONFIRMED")) {
        tx.source_confirmed = true;
        await followCrossChainSubmission(tx);
      } else if (tx.status === "CONFIRMED" || tx.status === "REVERTED") tx.unresolved = false;
      if (!tx.unresolved) {
        // Re-read through the cache window until the platform's block reaches the deposit;
        // finalized reads can trail its receipt for several minutes.
        if (tx.label === "Lending order" || tx.label === "Cancel lending order") void refreshLendingOrders();
        if ((tx.status === "CONFIRMED" && ["Deposit", "Sale"].includes(tx.label))
          || (tx.label === "Sale" && tx.cross_chain && tx.source_confirmed)) {
          void refreshAfterRoute(tx);
        }
        // Submission completion never mutates the old request. Read the current request's
        // own amount and spender again, even if it changed while the wallet was open.
        if (execution && routeScopeMatches(tx, execution.account, execution.chain_id)) {
          const capture = {...execution};
          try { await refreshRouteAllowance(capture); }
          catch (error) {
            if (routeRequestIsCurrent(capture)) {
              execution.signable = false;
              execution.error = `Could not refresh allowance: ${boundedAdapterError(error)}`;
            }
          }
        }
        if (tx.status === "CONFIRMED" && typeof tx.on_confirmed === "function" && !tx.confirmation_callback_run) {
          tx.confirmation_callback_run = true;
          await tx.on_confirmed();
        }
      }
    } finally {
      tx.polling = false;
      renderRouteQuotes();
    }
  }

  // Tell the server a route's transaction was broadcast, so it follows the purchase to an end and
  // reconciles its fee. Best effort: the purchase stands whether or not the server hears of it, and a
  // refusal (an unknown route, another wallet's) is the server's answer, not something to retry.
  async function reportRouteSubmission(e, hash) {
    const routeId = e.data?.route_id;
    if (typeof routeId !== "string" || !/^[0-9a-f]{32}$/.test(routeId)) return;
    const body = {schema_version: "fenorium/platform-route-submission/1", method_version: "route/1",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), route_id: routeId,
      wallet: String(e.account).toLowerCase(), tx_hash: hash.toLowerCase(),
      ...(e.approval_hash ? {approval_tx_hash: e.approval_hash} : {})};
    for (let attempt = 0; attempt < 5; attempt += 1) {
      let retryDelay = 2000 * (attempt + 1);
      try {
        const response = await root.fetch("/v1/routes/submitted", {method: "POST", cache: "no-store",
          headers: {Accept: "application/json", "Content-Type": "application/json",
            "Idempotency-Key": `submitted-${routeId}-${hash.toLowerCase()}`},
          body: JSON.stringify(body)});
        if (response.ok || (response.status >= 400 && response.status < 500 && response.status !== 429)) return;
        if (response.status === 429) {
          const retryAfter = response.headers?.get?.("Retry-After");
          if (typeof retryAfter === "string" && /^[0-9]+$/.test(retryAfter)) {
            const seconds = Number(retryAfter);
            if (Number.isSafeInteger(seconds) && seconds > 0) retryDelay = Math.min(seconds, 60) * 1000;
          }
        }
      } catch (_) { /* a network failure is worth another try */ }
      if (attempt === 4) break;
      await new Promise(resolve => { const timer = setTimeout(resolve, retryDelay); timer?.unref?.(); });
    }
  }

  async function sendRouteInstruction(kind) {
    if (execution?.kind === "lending") return sendLendingInstruction(kind);
    if (!["reset", "approve", "buy", "sell"].includes(kind) || !execution?.signable || execution.sending
      || routeLocked(execution) || !executionIsCurrent(execution)) return;
    const e = execution, request = kind === "reset" ? e.data?.approval_reset_request : kind === "approve" ? e.data?.approval_request : e.data?.transaction_request;
    if (!object(request) || (["buy", "sell"].includes(kind) && e.needs_approval)
      || (kind === "approve" && (!e.needs_approval || e.needs_reset))
      || (kind === "reset" && !e.needs_reset)
      || Routes.parseAmount(e.amount_input, e.pay.decimals) !== e.amount_raw) return;
    const capture = {...e}, label = kind === "reset" ? "Allowance reset" : kind === "approve" ? "Approval" : e.kind === "sale" ? "Sale" : e.kind === "deposit" ? "Deposit" : "Purchase";
    const submission = {account: String(e.account).toLowerCase(), chain_id: e.chain_id,
      kind, label, status: "CHECKING", unresolved: true, hash: null, polling: false,
      instrument_identity: instrumentIdentity(e.instrument), instrument_symbol: e.instrument_symbol,
      ...(e.kind === "sale" ? {receive_symbol: e.receive.symbol} : {}),
      instrument_chain_id: e.instrument_chain_id ?? e.instrument.chain_id,
      cross_chain: ["buy", "sell"].includes(kind) && e.view.cross_chain,
      direction: e.kind === "sale" ? "SELL" : "BUY",
      from_chain: e.data.route.from_chain, to_chain: e.data.route.to_chain};
    // Acquire both locks before the first await, including the wallet's chain query.
    e.sending = true;
    routeSubmissions.push(submission);
    renderRouteQuotes();
    let submitted = false;
    try {
      if (!routeFresh(e)) throw new Error("The route expired before it was signed; request it again.");
      if (request.chainId !== e.chain_id) throw new Error("The instruction targets a different chain; request the route again.");
      const chainHex = await state.provider.request({method: "eth_chainId"});
      if (typeof chainHex !== "string" || !/^0x[0-9a-f]+$/i.test(chainHex) || BigInt(chainHex) !== BigInt(e.chain_id)
        || state.wallet.chainId !== e.chain_id) throw new Error(`Switch your wallet to ${chainName(e.chain_id)} before signing.`);
      if (String(request.from).toLowerCase() !== String(state.wallet.account).toLowerCase()) {
        throw new Error("The route was built for a different wallet; request it again.");
      }
      if (e.kind === "deposit") {
        validateDepositResponse(e, e.data);
        await simulateDeposit(capture, e.needs_approval);
      }
      const params = {from: request.from, to: request.to, data: request.data,
        chainId: "0x" + e.chain_id.toString(16), value: "0x" + BigInt(request.value || "0").toString(16)};
      if (request.gasLimit) params.gas = "0x" + BigInt(request.gasLimit).toString(16);
      const instructionKey = routeInstructionKey(e);
      if (["buy", "sell"].includes(kind) && purchasedRouteInstructions.has(instructionKey)) throw new Error("This route instruction was already submitted; request a new route.");
      if (!routeRequestIsCurrent(capture) || !e.signable || Routes.parseAmount(e.amount_input, e.pay.decimals) !== e.amount_raw) throw new Error("The selected route changed before signing; request it again.");
      if (!routeFresh(e)) throw new Error("The route expired before it was signed; request it again.");
      e.error = null;
      submission.status = "AWAITING_WALLET";
      renderRouteQuotes();
      submitted = true;
      const hash = await state.provider.request({method: "eth_sendTransaction", params: [params]});
      if (["buy", "sell"].includes(kind)) purchasedRouteInstructions.add(instructionKey);
      if (typeof hash !== "string" || !/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error("Wallet did not return a transaction hash; submission outcome is unknown.");
      submission.hash = hash;
      submission.status = "PENDING";
      if (kind === "approve" || kind === "reset") e.approval_hash = hash.toLowerCase();
      // The route that was signed: `capture` holds its data, which a route requested meanwhile replaces on `e`.
      if (["buy", "sell"].includes(kind) && capture.kind !== "deposit") void reportRouteSubmission(capture, hash);
      if (["buy", "sell"].includes(kind) && routeRequestIsCurrent(capture)) e.signable = false;
      renderRouteQuotes();
      await checkRouteSubmission(hash);
    } catch (error) {
      // Only a definite rejection proves that an attempted send did not submit.
      if (!submitted || Number(error?.code) === 4001) {
        routeSubmissions.splice(routeSubmissions.indexOf(submission), 1);
      } else submission.status = "UNKNOWN";
      if (routeRequestIsCurrent(capture)) {
        e.signable = false;
        e.error = `${label}: ${boundedAdapterError(error)}`;
      }
    } finally {
      e.sending = false;
      renderRouteQuotes();
    }
  }

  async function boundedRouteRead(run, ms = 8000) {
    let timer;
    const controller = typeof root.AbortController === "function" ? new root.AbortController() : null;
    try {
      return await Promise.race([run(controller?.signal), new Promise((_, reject) => {
        timer = setTimeout(() => { controller?.abort(); reject(new Error("Read timed out.")); }, ms);
      })]);
    } finally { clearTimeout(timer); controller?.abort(); }
  }
  // Longer than the server's whole lending deadline (LENDING_RESPONSE_SECONDS = 18 in deploy/platform/api.py),
  // so the browser never abandons an answer the server is still allowed to give.
  const LENDING_READ_MS = 25000;

  async function crossChainAllowed(provider, wallet, chainId, instrument, chains, fetcher, bounded, isCurrent) {
    let message = null;
    let crossAllowed = Boolean(chains?.[instrument.chain_id]);
    if (crossAllowed) {
      const codeReads = await Promise.allSettled([
        bounded(() => provider.request({method: "eth_getCode", params: [wallet, "latest"]})),
        bounded(signal => routeRpc(chains[instrument.chain_id], fetcher, signal).request({method: "eth_getCode", params: [wallet, "latest"]})),
      ]);
      if (!isCurrent()) return {allowed: false, message: null};
      const validCode = code => typeof code === "string" && /^0x(?:[0-9a-f]{2})*$/i.test(code);
      if (codeReads.some(read => read.status !== "fulfilled" || !validCode(read.value))) {
        crossAllowed = false;
        message = "Could not check whether this wallet is a smart contract, so cross-chain options are not offered.";
      } else if (codeReads.some(read => read.value !== "0x" && !read.value.toLowerCase().startsWith("0xef0100"))) {
        crossAllowed = false;
        message = "Smart-contract wallets can buy only on the instrument's own chain here: the same address on another chain may not be yours.";
      }
    }
    return {allowed: crossAllowed, message};
  }

  async function readRouteQuotes(provider, wallet, chainId, instrument, assets, config, fetcher = root.fetch, isCurrent = () => true, served = null, onlyChains = null) {
    const settings = config?.route_quotes, chains = routeChainConfig(config);
    const result = [], messages = [];
    result.messages = messages;
    if (settings?.enabled !== true || !Routes.buildRequest || !provider?.request || typeof fetcher !== "function"
      || !configuredAddress(wallet) || !BUY_CHAIN_IDS.has(instrument?.chain_id)) return result;
    if (!chains && instrument.chain_id !== chainId) return result;
    const name = id => chains?.[id]?.name || chainName(id);
    const bounded = boundedRouteRead;
    let connectedReadable = true;
    try {
      if (!isCurrent() || normalizeChainId(await bounded(() => provider.request({method: "eth_chainId"}))) !== chainId) return result;
    } catch (_) { connectedReadable = false; messages.push(`Could not read balances on ${name(chainId)}`); }
    const guard = await crossChainAllowed(provider, wallet, chainId, instrument, chains, fetcher, bounded, isCurrent);
    if (!isCurrent()) return result;
    const crossAllowed = guard.allowed;
    if (guard.message) messages.push(guard.message);
    const ids = [chainId, ...Object.keys(chains || {}).map(Number).filter(id => id !== chainId).sort((a, b) => a - b)]
      .filter(id => !onlyChains || onlyChains.has(id))
      .filter(id => (id !== chainId || connectedReadable) && (id === instrument.chain_id || (crossAllowed && Boolean(chains?.[id]))));
    const holdings = new Array(ids.length), failed = new Set();
    await pooled(ids.map((id, index) => ({id, index})), 3, async ({id, index}) => {
      holdings[index] = [];
      try {
        await bounded(async signal => {
          if (!isCurrent()) return;
          // Only a complete chain read can replace this chain's RPC balance reads.
          const servedHere = served?.chains?.[id] === "READ" && Array.isArray(served.options)
            ? served.options.filter(option => Number(option?.chain_id) === id && /^0x[0-9a-fA-F]{40}$/.test(String(option?.address))
              && Number.isInteger(option.decimals) && /^[0-9]+$/.test(String(option.raw_amount)))
            : null;
          if (servedHere) {
            const offered = new Set();
            holdings[index] = servedHere.flatMap(option => {
              const key = String(option.address).toLowerCase();
              if (offered.has(key) || (chains?.[id]?.offer_native === false && /^0x0{40}$/.test(key))) return [];
              offered.add(key);
              const asset = {chain_id: id, address: option.address, symbol: option.symbol || option.address, decimals: option.decimals};
              const request = Routes.buildRequest(instrument.chain_id, instrument, asset, String(option.raw_amount), settings.fromAddress);
              return request ? [{asset, request}] : [];
            });
            return;
          }
          const rpc = id === chainId ? provider : routeRpc(chains[id], fetcher, signal);
          const listed = id === chainId ? assets.filter(asset => asset.chain_id === id) : P.registeredAssetsFromUniverse(registryUniverse(), id);
          // The portfolio registry intentionally excludes pay-only chains. Their addressed
          // universe assets may still be payment candidates, without becoming instruments.
          if (!MAINNET_CHAIN_IDS.has(id) && chains?.[id]) {
            listed.push(...state.universe.filter(asset => Number(asset.chain_id) === id
              && configuredAddress(asset.address) && Number.isInteger(asset.decimals)
              && asset.decimals >= 0 && asset.decimals <= 36));
          }
          const native = chains?.[id]?.native;
          const seen = new Set();
          const candidates = [...listed, ...(chains?.[id]?.tokens || [])].filter(asset => {
            const key = String(asset.address).toLowerCase();
            if (seen.has(key) || (chains?.[id]?.offer_native === false && /^0x0{40}$/.test(key))) return false;
            seen.add(key);
            return true;
          });
          if (native && chains[id].offer_native !== false && !candidates.some(asset => /^0x0{40}$/i.test(asset.address))) {
            candidates.push({chain_id: id, address: "0x" + "0".repeat(40), symbol: native.symbol, decimals: native.decimals});
          }
          const eligible = candidates.map(asset => ({...asset, chain_id: id}))
            .filter(asset => Routes.buildRequest(instrument.chain_id, instrument, asset, "1", settings.fromAddress));
          if (!eligible.length) return;
          const tokens = eligible.filter(asset => !/^0x0{40}$/i.test(asset.address));
          const read = await readPortfolioObservations(rpc, wallet, id, tokens);
          if (!isCurrent() || signal?.aborted) return;
          if (read.observations.some(row => row.error)) failed.add(id);
          for (const asset of eligible.filter(asset => /^0x0{40}$/i.test(asset.address))) {
            try {
              const raw = await rpc.request({method: "eth_getBalance", params: [wallet, "0x" + BigInt(read.blockReference.block_number).toString(16)]});
              if (typeof raw !== "string" || !/^0x[0-9a-f]+$/i.test(raw)) throw new Error("Invalid native balance.");
              read.observations.push({chain_id: id, address: asset.address, raw_amount: BigInt(raw).toString()});
            } catch (_) { failed.add(id); }
          }
          if (!isCurrent() || signal?.aborted) return;
          holdings[index] = eligible.flatMap(asset => {
            const balance = read.observations.find(row => row.chain_id === id && String(row.address).toLowerCase() === asset.address.toLowerCase() && !row.error);
            const request = Routes.buildRequest(instrument.chain_id, instrument, asset, balance?.raw_amount, settings.fromAddress);
            return request ? [{asset, request}] : [];
          });
        });
      } catch (_) { failed.add(id); }
    });
    if (!isCurrent()) return result;
    for (const id of ids) if (failed.has(id)) messages.push(`Could not read balances on ${name(id)}`);
    let crossCount = 0, omitted = 0;
    const previews = holdings.flat().filter(item => {
      if (item.asset.chain_id === instrument.chain_id) return true;
      if (crossCount++ < 6) return true;
      omitted += 1;
      return false;
    });
    if (omitted) messages.push(`${omitted} cross-chain holding${omitted === 1 ? " was" : "s were"} not previewed; at most 6 cross-chain holdings are previewed per instrument.`);
    const rows = new Array(previews.length);
    await pooled(previews.map((item, index) => ({...item, index})), 3, async ({asset, request, index}) => {
      try {
        await bounded(async signal => {
          if (!isCurrent()) return;
          const query = Object.entries(request.query).map(([key, value]) => encodeURIComponent(key) + "=" + encodeURIComponent(value)).join("&");
          const observedAt = Date.now();
          const response = await fetcher("https://li.quest/v1/quote?" + query, {method: "GET", cache: "no-store", credentials: "omit", signal});
          if (!isCurrent() || response.status !== 200) return;
          const payload = await response.json();
          if (!isCurrent() || signal?.aborted) return;
          const binding = Routes.bindQuote(request, payload, observedAt, Date.now());
          if (binding) rows[index] = {chain_id: asset.chain_id, symbol: asset.symbol || asset.address, binding};
        });
      } catch (_) { /* A failed preview is not an executable instruction. */ }
    });
    if (!isCurrent()) return result;
    try {
      if (normalizeChainId(await bounded(() => provider.request({method: "eth_chainId"}))) !== chainId || !isCurrent()) return result;
    } catch (_) {
      connectedReadable = false;
      const message = `Could not read balances on ${name(chainId)}`;
      if (!messages.includes(message)) messages.push(message);
    }
    result.push(...rows.filter(row => row && (connectedReadable || row.chain_id !== chainId) && Routes.displayQuote(row.binding, Date.now())));
    return result;
  }

  let routePreviewGeneration = 0;

  async function readRoutePreviews(wallet, instrument, served, notionalUsd, fetcher = root.fetch, isCurrent = () => true) {
    const chains = routeChainConfig(state.config), seen = new Set();
    const notional = notionalUsd * 1000000;
    if (!Array.isArray(served?.options) || !Number.isSafeInteger(notional) || notional <= 0 || typeof fetcher !== "function") return null;
    // Ineligible chains are removed before the field is cut to three, so a contract wallet's
    // same-chain holding is not pushed out by cross-chain ones it cannot use.
    let candidates = served.options.filter(option => {
      const id = Number(option?.chain_id), address = String(option?.address).toLowerCase();
      const key = id + ":" + address;
      if (seen.has(key) || !/^0x[0-9a-f]{40}$/.test(address) || !Number.isInteger(option.decimals)
        || !/^[0-9]+$/.test(String(option.raw_amount)) || BigInt(option.raw_amount) === 0n
        || (chains?.[id]?.offer_native === false && /^0x0{40}$/.test(address))
        || !Routes.buildRequest(instrument.chain_id, instrument, {...option, chain_id: id}, String(option.raw_amount), wallet)) return false;
      seen.add(key);
      return true;
    }).sort((a, b) => {
      const value = option => /^[0-9]+$/.test(String(option.value_usd_micros)) ? BigInt(option.value_usd_micros) : 0n;
      const left = value(a), right = value(b);
      return left > right ? -1 : left < right ? 1 : 0;
    });
    const messages = [];
    if (candidates.some(option => Number(option.chain_id) !== instrument.chain_id)) {
      const guard = await crossChainAllowed(state.provider, wallet, state.wallet.chainId, instrument,
        chains, fetcher, boundedRouteRead, isCurrent);
      if (!guard.allowed) candidates = candidates.filter(option => Number(option.chain_id) === instrument.chain_id);
      if (guard.message) messages.push(guard.message);
    }
    candidates = candidates.slice(0, 3);
    if (!candidates.length || !isCurrent()) return null;
    const controller = typeof root.AbortController === "function" ? new root.AbortController() : null;
    let timer;
    try {
      const envelope = await Promise.race([(async () => {
        const response = await fetcher("/v1/route-previews", {
          method: "POST", cache: "no-store", signal: controller?.signal,
          headers: {Accept: "application/json", "Content-Type": "application/json",
            "Idempotency-Key": `preview-${PORTFOLIO_SESSION_ID}-${++routePreviewGeneration}`},
          body: JSON.stringify({schema_version: "fenorium/platform-route-preview-request/1", method_version: "preview/1",
            as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), wallet: wallet.toLowerCase(),
            chain_id: instrument.chain_id, instrument_address: instrument.address, notional_usd_micros: notional,
            pay_with: candidates.map(option => ({chain_id: Number(option.chain_id), address: option.address,
              decimals: option.decimals, symbol: option.symbol || option.address}))}),
        });
        if (!response?.ok) return null;
        return response.json();
      })(), new Promise((_, reject) => {
        timer = setTimeout(() => { controller?.abort(); reject(new Error("Preview timed out.")); }, 20000);
      })]);
      if (!isCurrent() || controller?.signal.aborted || !validPlatformEnvelope(envelope)
        || envelope.data?.schema_version !== "fenorium/route-previews/1" || !Array.isArray(envelope.data.previews)) return null;
      const data = envelope.data;
      const rows = data.previews.map(preview => {
        const pay = preview.pay_with;
        const option = candidates.find(option => Number(option.chain_id) === Number(pay.chain_id)
          && String(option.address).toLowerCase() === String(pay.address).toLowerCase());
        if (!option) return null;
        const row = {chain_id: pay.chain_id, symbol: pay.symbol || pay.address, preview: true,
          balance_raw: String(option.raw_amount)};
        if (preview.quoted_usd_micros != null) row.quoted_usd_micros = preview.quoted_usd_micros;
        const acceptanceOffer = preview.status === "ANALYTICAL_ONLY"
          && Array.isArray(preview.reason_codes) && preview.reason_codes.length === 1
          && preview.reason_codes[0] === "PURCHASE_COST_NEEDS_ACCEPTANCE";
        if ((preview.status !== "ROUTE_READY" && !acceptanceOffer) || !/^[0-9]+$/.test(String(preview.amount_raw))
          || !/^[0-9]+$/.test(String(preview.to_amount_min))) return {...row, refused: preview.reason_codes};
        if (BigInt(preview.amount_raw) > BigInt(option.raw_amount)) return {...row, refused: ["INSUFFICIENT_BALANCE"]};
        const observedAt = Date.now();
        const expiresAt = typeof preview.expires_at === "number" ? preview.expires_at * 1000
          : typeof preview.expires_at === "string" ? Date.parse(preview.expires_at) : NaN;
        if (!Number.isFinite(expiresAt) || expiresAt <= observedAt) return {...row, refused: ["PREVIEW_EXPIRED"]};
        const best = data.best?.pay_with;
        return {...row, best: Boolean(best && best.chain_id === pay.chain_id
          && String(best.address).toLowerCase() === String(pay.address).toLowerCase()),
        tool: preview.tool, cost_bps: preview.size_check?.route_cost_bps ?? null,
        yield_to_maturity_bps: preview.yield_to_maturity_bps ?? null,
        net_yield_to_maturity_bps: preview.net_yield_to_maturity_bps ?? null,
        acceptance_offer: acceptanceOffer,
        binding: {from_chain: pay.chain_id, to_chain: instrument.chain_id, from_token: pay.address,
          to_token: instrument.address, from_amount: String(preview.amount_raw), to_amount_min: String(preview.to_amount_min),
          from_decimals: pay.decimals, to_decimals: instrument.decimals, observed_at: observedAt,
          expires_at: expiresAt}};
      }).filter(Boolean);
      const best = rows.find(row => row.binding && row.best) || rows.find(row => row.binding);
      if (best) best.best = true;
      rows.messages = messages;
      return {rows, messages};
    } catch (_) { return null; }
    finally { clearTimeout(timer); }
  }

  async function refreshRouteQuotes() {
    depositSelectionGeneration += 1;
    depositEntryError = null;
    const generation = ++routeGeneration, walletGeneration = state.walletGeneration;
    const instrument = state.selected, wallet = state.wallet.account, chainId = state.wallet.chainId;
    routeRows = [];
    routeQuotesServed = false;
    routeMessages = [];
    routeSelectionMessage = "";
    root.clearTimeout?.(routeExpiryTimer);
    renderRouteQuotes();
    if (!instrument || isSimulatedRow(instrument) || instrument.address == null || !wallet || state.config?.route_quotes?.enabled !== true) return [];
    const current = () => generation === routeGeneration && walletGeneration === state.walletGeneration
      && state.selected === instrument && state.wallet.account === wallet && state.wallet.chainId === chainId;
    const assets = P.registeredAssetsFromUniverse(registryUniverse(), chainId);
    // The pay options the platform named for this wallet, when it read it; otherwise the balances
    // are read here, chain by chain.
    const record = portfolioRecord(wallet, chainId);
    const served = record?.source === "server" && Array.isArray(record.payOptions)
      ? {options: record.payOptions, chains: record.servedChains || {}} : null;
    const preview = served ? await readRoutePreviews(wallet, instrument, served, state.notionalSize, root.fetch, current) : null;
    if (!current()) return [];
    const rows = preview ? preview.rows
      : await readRouteQuotes(state.provider, wallet, chainId, instrument, assets, state.config, root.fetch, current, served);
    if (preview) {
      const incompleteChains = new Set([chainId, ...Object.keys(routeChainConfig(state.config) || {}).map(Number)]
        .filter(id => served.chains[id] !== "READ"));
      if (incompleteChains.size) {
        const direct = await readRouteQuotes(state.provider, wallet, chainId, instrument, assets, state.config, root.fetch, current, served, incompleteChains);
        const previewKeys = new Set(rows.filter(row => row.binding)
          .map(row => `${row.binding.from_chain}:${row.binding.from_token.toLowerCase()}`));
        rows.push(...direct.filter(row => !row.binding
          || !previewKeys.has(`${row.binding.from_chain}:${row.binding.from_token.toLowerCase()}`)));
        rows.messages = [...(rows.messages || []), ...(direct.messages || [])];
      }
    }
    if (!current()) return [];
    routeRows = rows;
    routeQuotesServed = Boolean(served);
    routeMessages = rows.messages || [];
    if (served && !preview) routeMessages.push("Fenorium's server could not preview this purchase; the quotes below come from LI.FI directly, from a neutral address.");
    renderRouteQuotes();
    const boundRows = rows.filter(row => row.binding);
    if (boundRows.length) routeExpiryTimer = setTimeout(() => {
      if (current()) { routeRows = []; renderRouteQuotes(); }
    }, Math.max(0, Math.min(...boundRows.map(row => row.binding.expires_at)) - Date.now()));
    return rows;
  }

  // The platform reads a wallet on every chain it reads wallets on and values what it finds with
  // the same analysis this page's own read is sent to. When it cannot -- reads off (501), busy
  // (503), or any other failure -- the read is handed back to the browser; nothing is invented
  // in between, and the demo testnet is never asked of the platform.
  async function readServerPortfolio(capture) {
    if (testnetWorkspace()) return null;
    if (typeof root.fetch !== "function" || capture.chainId === BASE_SEPOLIA.chainId) return null;
    const chains = REGISTRY_CHAINS
      .filter((chain) => !chain.testnet && (chain.roles.includes("portfolio") || chain.roles.includes("pay")))
      .map((chain) => chain.chain_id)
      .sort((left, right) => left - right);
    if (!chains.length) return null;
    const controller = typeof root.AbortController === "function" ? new root.AbortController() : null;
    let timer;
    try {
      if (controller) timer = root.setTimeout(() => controller.abort(), 30000);
      const response = await root.fetch(`/v1/wallets/${capture.account}/portfolio?chains=${chains.join(",")}`, {
        cache: "no-store",
        signal: controller?.signal,
        headers: { Accept: "application/json" },
      });
      if (!response?.ok) return null;
      const envelope = await response.json();
      const data = envelope?.data;
      if (!object(data) || !object(data.wallet_snapshot) || !object(data.valuation) || !object(data.allocation) || !object(data.analytics)) return null;
      if (String(data.wallet || "").toLowerCase() !== capture.account
        || String(data.wallet_snapshot.wallet || "").toLowerCase() !== capture.account
        || (Array.isArray(data.chains) && data.chains.some(chain => Object.prototype.hasOwnProperty.call(chain, "wallet")
          && String(chain.wallet || "").toLowerCase() !== capture.account))) return null;
      const view = P.portfolioAnalyticsView({
        wallet_snapshot: data.wallet_snapshot, valuation: data.valuation, allocation: data.allocation, analytics: data.analytics,
        risk_scope: data.risk_scope,
      }, capture.account);
      const reads = Array.isArray(data.chains) ? data.chains : [];
      const snapshot = {
        ...view,
        // Every chain asked, failed ones included: a chain that could not be read is news.
        // Every chain asked, failed ones included, each with the finality its block actually has:
        // a block read at "safe" or at a depth is not finalized evidence and is not called that.
        chains: reads.map((chain) => {
          const ref = chain.block?.block_number == null ? null : `0x${Number(chain.block.block_number).toString(16)}`;
          const finality = chain.block?.finality || null;
          return {
            chain_id: chain.chain_id,
            block_ref: ref,
            finality,
            finalized_block_ref: finality === "finalized" ? ref : null,
            rpc_status: chain.status === "READ" ? "COMPLETE" : chain.status || "PENDING",
            source_timestamp: data.as_of,
          };
        }),
        provenance: {
          ...view.provenance,
          source: `platform wallet read on ${reads.length} chains; deterministic platform classification`,
        },
        totals: data.totals,
      };
      // The connected chain's read, in the shape this page's own Multicall3 read takes, so the
      // transfer scan and the P&L derived from it anchor on the block the balances were read at.
      // Only for a chain this page scans transfers on, and only a finalized block: the scan and
      // the derivation take their anchor as final, and a safe or depth block is not.
      const connected = MAINNET_CHAIN_IDS.has(capture.chainId)
        ? reads.find((chain) => chain.chain_id === capture.chainId && chain.block?.finality === "finalized") : null;
      const finalizedRead = connected ? {
        block_number: Number(connected.block.block_number),
        as_of: data.as_of,
        observations: (data.wallet_snapshot.positions || [])
          .filter((position) => position?.asset?.chain_id === capture.chainId && position.asset.address)
          .map((position) => position.unresolved || position.raw_amount == null
            ? { chain_id: capture.chainId, address: position.asset.address, error: position.adapter_error?.detail || "unresolved" }
            : { chain_id: capture.chainId, address: position.asset.address, raw_amount: String(position.raw_amount) }),
      } : null;
      return { snapshot, finalizedRead, read: data };
    } catch (_) {
      return null;
    } finally {
      if (timer != null) root.clearTimeout(timer);
    }
  }

  function invalidateHistoryPerformance(record, capture) {
    const anchor = state.history?.anchor;
    const block = record.finalizedRead?.block_number;
    if (state.history && (block == null || !Number.isSafeInteger(Number(block))
      || anchor?.account !== capture.account || anchor?.chain_id !== capture.chainId
      || anchor?.block !== Number(block))) {
      state.history.performance = null;
    }
  }

  function refreshPortfolio({preserveExitRequests = false, scope: requestedScope = "viewed"} = {}) {
    if (isTestnetBlocked()) return Promise.resolve(null);
    if (localRpcUnverified()) return Promise.resolve(null);
    const provider = state.provider;
    let scope = requestedScope;
    // "connected" reads the signing wallet's own book whatever is on screen: a receipt-triggered
    // refresh must advance that record, not the watched one being viewed.
    const viewed = scope === "connected"
      ? {account: state.wallet.account, chainId: state.wallet.chainId, connected: true, watched: null}
      : viewedWallet();
    // Judged when transfer or history state is about to change, not when the read started: the
    // view can switch to a watched wallet while a signer read is in flight.
    const displayedNow = () => scope !== "connected" || viewedWallet().connected;
    const account = viewed.account;
    const chainId = viewed.chainId;
    if (isTestnetReady() && chainId !== BASE_SEPOLIA.chainId) return Promise.resolve(null);
    const record = portfolioRecord(account, chainId);
    const payOnly = viewed.connected && onPayOnlyChain();
    // A pay-only chain has nothing registered for this page to read, but the platform reads every
    // chain it knows: the chain stays untracked only when that read is not to be had. A watched
    // wallet is read through the platform alone, so it needs no browser provider.
    if (!record || (!payOnly && !provider?.request && viewed.connected)) return Promise.resolve(null);
    if (record.promise && portfolioCaptureCurrent(record.capture)) {
      // A signer refresh joining the signer's own in-flight viewed read must not inherit that
      // read's cancellation by a view switch: the read is promoted to the connected scope.
      if (scope === "connected" && record.capture.scope !== "connected") record.capture.promote?.();
      return record.promise;
    }
    const assets = payOnly ? [] : P.registeredAssetsFromUniverse(registryUniverse(), chainId).map(withDemoTicker);
    const generation = scope === "connected" ? ++state.connectedReadGeneration : ++state.portfolioReadGeneration;
    const capture = {
      scope,
      generation,
      walletGeneration: state.walletGeneration,
      viewGeneration: state.viewGeneration,
      account: account.toLowerCase(),
      chainId,
      promote() {
        scope = "connected";
        capture.scope = "connected";
        capture.generation = ++state.connectedReadGeneration;
      },
    };
    record.capture = capture;
    record.loading = true;
    record.error = null;
    void refreshVenuePositions({preserveExitRequests});
    renderOverview();
    const task = (async () => {
      try {
        const served = await readServerPortfolio(capture);
        if (!portfolioCaptureCurrent(capture)) return null;
        const payOptionsChanged = served && !sameJsonValue(record.payOptions, served.read.pay_options || []);
        if (served) {
          record.payOptions = Array.isArray(served.read.pay_options) ? served.read.pay_options : [];
          record.redeemable = Array.isArray(served.read.redeemable) ? served.read.redeemable : [];
          record.servedChains = Object.fromEntries((served.read.chains || []).map(chain => [chain.chain_id, chain.status]));
          record.readBlocks = Object.fromEntries((served.read.chains || [])
            .filter(chain => chain.block?.block_number != null && Number.isSafeInteger(Number(chain.block.block_number)))
            .map(chain => [chain.chain_id, Number(chain.block.block_number)]));
          record.heldValueByChain = {};
          record.heldChains = [];
          for (const holding of served.read.holdings || []) {
            const value = P.asBigInt(holding.value_usd_micros) ?? 0n;
            const balance = holding.raw_amount != null ? P.asBigInt(holding.raw_amount) ?? 0n : value;
            if (balance > 0n && !record.heldChains.includes(holding.chain_id)) record.heldChains.push(holding.chain_id);
            const previous = record.heldValueByChain[holding.chain_id] || "0";
            record.heldValueByChain[holding.chain_id] = (BigInt(previous) + value).toString();
          }
        }
        // An empty platform read is accepted as the wallet's state unless the platform itself says
        // it could not read the connected chain: then this page reads that chain, as it did before.
        // Fixed-rate positions and matured PTs count as content: a browser read cannot see them and would drop them.
        const connectedStatus = served?.read.chains?.find(chain => chain.chain_id === chainId)?.status;
        const connectedUnread = connectedStatus != null && connectedStatus !== "READ";
        if (served && (payOnly || !connectedUnread || served.snapshot.positions?.length || served.read.positions?.length
          || served.read.redeemable?.length)) {
          record.snapshot = served.snapshot;
          record.finalizedRead = served.finalizedRead;
          if (displayedNow()) invalidateHistoryPerformance(record, capture);
          record.venuePositions = Array.isArray(served.read.positions) ? served.read.positions : [];
          record.source = "server";
          if (viewed.connected && state.selected?.address && state.config?.route_quotes?.enabled === true
            && portfolioCaptureCurrent(capture) && (!routeQuotesServed || payOptionsChanged)) void refreshRouteQuotes();
          if (displayedNow()) {
            state.transferGeneration += 1;
            if (state.transferHistory?.status !== "COMPLETE") state.transferHistory = null;
          }
          record.error = null;
          return served.snapshot;
        }
        record.source = "browser";
        record.venuePositions = null;
        record.redeemable = null;
        record.payOptions = null;
        record.servedChains = null;
        record.readBlocks = null;
        record.heldValueByChain = null;
        record.heldChains = null;
        if (payOnly) return null;
        if (!assets.length) {
          record.snapshot = null;
          record.error = { code: "NO_REGISTERED_ASSETS_ON_CHAIN", message: "No exact registered contract assets with known decimals exist on the selected chain." };
          return null;
        }
        // A browser read labels its block and balances with this chain, so the provider must be on
        // it: a watched book keyed to Base is never read through an extension on Base Sepolia.
        if (!provider?.request || (!viewed.connected && chainId !== state.wallet.chainId)) {
          record.snapshot = null;
          record.error = { code: "PORTFOLIO_READ_UNAVAILABLE", message: `The platform could not read this wallet, and a browser read needs a connected wallet on ${routeChainName(chainId)}.` };
          return null;
        }
        if (localRpcUnverified()) return null;
        const read = await readPortfolioObservations(provider, capture.account, chainId, assets);
        if (!portfolioCaptureCurrent(capture)) return null;
        const asOf = new Date().toISOString();
        const request = {
          schema_version: "fenorium/wallet-scan-request/v1",
          method_version: "portfolio-browser-multicall3/1",
          as_of: asOf,
          wallet: capture.account,
          registered_assets: assets,
          block_references: [read.blockReference],
          chain_ids: [chainId],
          include_base_sepolia_demo: chainId === BASE_SEPOLIA.chainId,
        };
        const priceObservations = P.priceObservationsFromUniverse(registryUniverse(), assets);
        priceObservations.push(...await readLivePriceObservations(provider, chainId, assets, asOf, state.config));
        if (!portfolioCaptureCurrent(capture)) return null;
        const priceMaxAgeSeconds = portfolioPriceMaxAgeSeconds(registryUniverse(), assets, asOf);
        const snapshot = await postPortfolioAnalytics(
          request,
          read.observations,
          ++state.portfolioRequestSequence,
          priceObservations,
          priceMaxAgeSeconds,
        );
        if (!portfolioCaptureCurrent(capture)) return null;
        if (String(snapshot.account || "").toLowerCase() !== capture.account) throw makeCodeError("PORTFOLIO_WALLET_MISMATCH", "portfolio response wallet does not match the captured account");
        record.snapshot = snapshot;
        record.finalizedRead = {block_number: read.blockReference.block_number, as_of: read.as_of, observations: read.observations};
        if (displayedNow()) invalidateHistoryPerformance(record, capture);
        if (displayedNow()) {
          state.transferGeneration += 1;
          // Deliberately not cleared: a finished scan of this wallet is what the next one extends.
          // Discarding it here is what made every refresh start another full-depth scan, and the
          // P&L fall back to UNKNOWN for the length of it. A scan for a different wallet or chain
          // is cleared by setWallet, which is where that actually changes.
          if (state.transferHistory?.status !== "COMPLETE") state.transferHistory = null;
        }
        record.error = null;
        return snapshot;
      } catch (error) {
        if (portfolioCaptureCurrent(capture)) {
          record.snapshot = null;
          record.error = { code: error?.code || "PORTFOLIO_READ_FAILED", message: boundedAdapterError(error) };
          if (displayedNow() && state.history) state.history.performance = null;
        }
        return null;
      } finally {
        if (record.capture === capture) {
          record.promise = null;
          record.loading = false;
        }
        if (portfolioCaptureCurrent(capture)) {
          renderOverview();
          void refreshVenuePositions({preserveExitRequests});
          void refreshLendingOrders();
        }
        // Derived history is what turns Cumulative P&L from UNKNOWN into a figure, and it needs
        // the transfers that funded the wallet. Waiting for the user to find a button meant the
        // card read UNKNOWN on every first look. Start it here, after the balances are on
        // screen, so the scan never delays what is already known.
        if (portfolioCaptureCurrent(capture)) ensureTransferScan();
      }
    })();
    const promise = task;
    record.promise = promise;
    return promise;
  }

  // A wallet that answered before the configuration arrived was named and classified without the
  // chains it lists; describe it again once they are known.
  function describeWalletAgain() {
    if (!state.wallet.account) return;
    state.wallet.chainName = routeChainName(state.wallet.chainId);
    if (state.wallet.status === STATUS.WRONG_CHAIN && knownWalletChain(state.wallet.chainId) && !state.wallet.errorMessage) {
      state.wallet.status = STATUS.CONNECTED;
      state.wallet.message = `${walletChainLabel(state.wallet.chainId)} · connected`;
    }
    renderWallet();
  }

  function renderWallet() {
    const connectedChain = $("global-chain");
    if (connectedChain) {
      const chains = state.wallet.account ? supportedWalletChains() : [];
      const listed = chains.some((chain) => chain.chainId === state.wallet.chainId);
      connectedChain.innerHTML = chains.length
        ? chains.map((chain) => `<option value="${chain.chainId}"${chain.chainId === state.wallet.chainId ? " selected" : ""}>${U.esc(chain.name)}</option>`).join("")
          + (listed ? "" : `<option value="" selected>${U.esc(state.wallet.chainName || `Chain ${state.wallet.chainId}`)}</option>`)
        : `<option value="">No connected chain</option>`;
      connectedChain.disabled = !state.wallet.account || typeof state.provider?.request !== "function";
      connectedChain.setAttribute("aria-label", state.wallet.account ? "Wallet chain; choose another to switch the wallet" : "Connected chain");
    }
    const accountShort = $("account-short");
    const walletStatus = $("wallet-status");
    const button = $("connect-wallet");
    const indicator = $("connection-indicator");
    if (!accountShort || !walletStatus || !button) return;
    if (state.wallet.status === STATUS.WRONG_CHAIN) {
      accountShort.textContent = "Switch to Base Sepolia testnet";
      walletStatus.textContent = state.wallet.errorMessage
        ? `${truncateAddress(state.wallet.account)} · ${state.wallet.errorMessage}`
        : `${truncateAddress(state.wallet.account)} · ${state.wallet.chainName || "wrong chain"}`;
      button.setAttribute("aria-label", "Switch to Base Sepolia testnet");
    } else if (state.wallet.account && state.wallet.status === STATUS.CONNECTED) {
      accountShort.textContent = truncateAddress(state.wallet.account);
      walletStatus.textContent = state.wallet.errorMessage
        ? `${walletChainLabel(state.wallet.chainId)} · ${state.wallet.errorMessage}`
        : `${walletChainLabel(state.wallet.chainId)} · connected`;
      button.setAttribute("aria-label", `Connected wallet ${truncateAddress(state.wallet.account)} on ${walletChainLabel(state.wallet.chainId)}`);
    } else {
      accountShort.textContent = "Connect";
      walletStatus.textContent = state.wallet.message || "Wallet optional · read-only market";
      button.setAttribute("aria-label", "Connect wallet");
    }
    if (isTestnetBlocked()) {
      accountShort.textContent = "Workspace unavailable";
      walletStatus.textContent = "No wallet or network requests started";
      button.disabled = true;
      button.setAttribute("aria-label", "Testnet workspace unavailable; wallet actions are disabled");
    }
    button.dataset.walletStatus = state.wallet.status;
    if (indicator) {
      indicator.className = `status-indicator ${state.wallet.status === STATUS.CONNECTED ? "connected" : state.wallet.status === STATUS.WRONG_CHAIN ? "warning" : "idle"}`;
      const walletLabel = typeof U.stateLabel === "function" ? U.stateLabel(state.wallet.status) : state.wallet.status;
      indicator.setAttribute("aria-label", isTestnetBlocked()
        ? "Testnet workspace unavailable; wallet actions are disabled"
        : `Wallet status: ${walletLabel}`);
    }
    const overviewNavState = $("overview-nav-state");
    if (overviewNavState) {
      overviewNavState.textContent = overviewNavText();
      overviewNavState.parentElement?.setAttribute("title", overviewNavState.textContent);
    }
    renderWalletSwitcher();
    renderOverview();
  }

  function bindProviderEvents(provider) {
    if (!provider?.on || state.providerEventsBound) return;
    state.providerEventsBound = true;
    provider.on("accountsChanged", handleAccountsChanged);
    provider.on("chainChanged", handleChainChanged);
  }

  function setInjectedProvider(provider = root.ethereum) {
    state.provider = provider?.request ? provider : null;
    state.providerEventsBound = false;
    bindProviderEvents(state.provider);
    return state.provider;
  }

  function handleAccountsChanged(accounts) {
    state.walletAccountGeneration += 1;
    const account = Array.isArray(accounts) ? accounts[0] : null;
    setWallet(account, state.wallet.chainId, account ? "Account changed · on-chain reads will refresh." : "Wallet disconnected · read-only market remains available.");
  }

  function handleChainChanged(chainId) {
    state.walletChainGeneration += 1;
    // Every event still refreshes the wallet. Only the announcement of a switch this page requested
    // and applied moments ago keeps the route bound to that chain; any other change discards it.
    const expected = appliedSwitch && appliedSwitch.chainId === normalizeChainId(chainId)
      && appliedSwitch.chainId === state.wallet.chainId && Date.now() - appliedSwitch.at < 10000;
    appliedSwitch = null;
    const arrived = normalizeChainId(chainId);
    setWallet(state.wallet.account, chainId, knownWalletChain(arrived) ? `${walletChainLabel(arrived)} · connected` : "Wrong chain · switch to Base Sepolia testnet.",
      {keepRoute: expected});
  }

  async function readWalletSnapshot(provider, options = {}) {
    if (isTestnetBlocked()) return {account: null, chainId: null};
    const accountMethod = options.accountMethod || "eth_accounts";
    const readChainWithoutAccount = options.readChainWithoutAccount === true;
    const capture = {
      wallet: state.walletGeneration,
      account: state.walletAccountGeneration,
      chain: state.walletChainGeneration,
    };
    try {
      const accounts = await provider.request({ method: accountMethod });
      const account = accounts?.[0] || null;
      let chainId = state.wallet.chainId;
      if (account || readChainWithoutAccount) {
        chainId = await provider.request({ method: "eth_chainId" });
      }
      const accountSuperseded = capture.account !== state.walletAccountGeneration;
      const chainSuperseded = capture.chain !== state.walletChainGeneration;
      const snapshotSuperseded = capture.wallet !== state.walletGeneration;
      if (snapshotSuperseded && !accountSuperseded && !chainSuperseded) {
        return { account: state.wallet.account, chainId: state.wallet.chainId };
      }
      return {
        account: accountSuperseded ? state.wallet.account : account,
        chainId: chainSuperseded ? state.wallet.chainId : chainId,
      };
    } catch (error) {
      error.walletSnapshotSuperseded = capture.wallet !== state.walletGeneration;
      throw error;
    }
  }

  async function hydrateWallet() {
    if (isTestnetBlocked()) return state.wallet;
    const provider = setInjectedProvider(root.ethereum);
    state.wallet.connectIntent = safeStorageGet(WALLET_INTENT_KEY) === "connect";
    if (!provider) {
      setWallet(null, null, "No injected wallet detected. Market is read-only and usable.");
      return state.wallet;
    }
    try {
      const snapshot = await readWalletSnapshot(provider);
      const disconnectedMessage = state.wallet.connectIntent
        ? "Connection preference remembered · click Connect to re-authorize this wallet."
        : "No wallet connected · click Connect to request access.";
      setWallet(snapshot.account, snapshot.chainId, snapshot.account ? "Silent reconnect complete." : disconnectedMessage);
      if (state.localWalletCheck) await state.localWalletCheck;
    } catch (error) {
      if (error?.walletSnapshotSuperseded) return state.wallet;
      setWallet(null, null, "Wallet state could not be read silently. Click Connect to try explicitly.");
      state.wallet.errorCode = error?.code || "WALLET_READ_FAILED";
    }
    return state.wallet;
  }

  async function syncWalletFromProvider(message) {
    const snapshot = await readWalletSnapshot(state.provider, { readChainWithoutAccount: true });
    setWallet(snapshot.account, snapshot.chainId, message);
    if (state.localWalletCheck) await state.localWalletCheck;
    return state.wallet;
  }

  // Move the wallet to a chain Fenorium knows, adding it to the wallet first when the wallet does
  // not have it. A refusal leaves the wallet where it was and says so beside the account.
  async function switchWalletChain(chainId) {
    if (isTestnetBlocked()) return state.wallet;
    const provider = state.provider || setInjectedProvider(root.ethereum);
    if (!provider) return setWallet(null, null, "No injected wallet detected.");
    const target = walletChainDetails(chainId);
    if (!target) return state.wallet;
    const sentence = (text) => text.charAt(0).toUpperCase() + text.slice(1);
    const fail = (message, code) => {
      setWallet(state.wallet.account, state.wallet.chainId, message);
      state.wallet.errorCode = code;
      state.wallet.errorMessage = message;
      state.wallet.switchTarget = chainId;  // the pill retries this switch, not a fresh connection
      renderWallet();
    };
    try {
      await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: target.hex }] });
      await syncWalletFromProvider("Network state re-read after switch.");
    } catch (error) {
      if (Number(error?.code) === 4902 && target.rpc && target.native) {
        try {
          await provider.request({
            method: "wallet_addEthereumChain",
            params: [{
              chainId: target.hex,
              chainName: target.name,
              nativeCurrency: target.native,
              rpcUrls: [target.rpc],
              ...(target.explorer ? {blockExplorerUrls: [target.explorer]} : {}),
            }],
          });
          await syncWalletFromProvider(`Network state re-read after adding ${target.name}.`);
        } catch (addError) {
          fail(userRejected(addError) ? `The wallet rejected adding ${target.spoken}.` : sentence(`${target.spoken} could not be added.`),
            addError?.code || "ADD_CHAIN_FAILED");
        }
      } else {
        fail(userRejected(error) ? `The wallet rejected switching to ${target.spoken}.` : sentence(`switching to ${target.spoken} was not completed.`),
          error?.code || "SWITCH_CHAIN_FAILED");
      }
    }
    return state.wallet;
  }

  function switchToBaseSepolia() {
    return switchWalletChain(BASE_SEPOLIA.chainId);
  }

  async function connectWallet() {
    if (isTestnetBlocked()) return state.wallet;
    const provider = state.provider || setInjectedProvider(root.ethereum);
    if (!provider) return setWallet(null, null, "No injected wallet detected. Market remains usable read-only.");
    if (state.wallet.account && switchOutstanding() && state.wallet.switchTarget) return switchWalletChain(state.wallet.switchTarget);
    if (state.wallet.account && state.wallet.status === STATUS.WRONG_CHAIN) return switchToBaseSepolia();
    safeStorageSet(WALLET_INTENT_KEY, "connect");
    try {
      const snapshot = await readWalletSnapshot(provider, {
        accountMethod: "eth_requestAccounts",
        readChainWithoutAccount: true,
      });
      setWallet(snapshot.account, snapshot.chainId, snapshot.account ? "Wallet connected." : "No wallet account was returned.");
      if (state.localWalletCheck) await state.localWalletCheck;
    } catch (error) {
      if (error?.walletSnapshotSuperseded) return state.wallet;
      setWallet(null, state.wallet.chainId, userRejected(error) ? "The wallet connection request was rejected." : "Wallet connection was not completed.");
      if (userRejected(error)) state.wallet.status = STATUS.USER_REJECTED;
      renderWallet();
    }
    return state.wallet;
  }

  function renderSnapshot(snapshot = state.snapshot) {
    if (!snapshot) return;
    const positions = snapshot.positions || [];
    const nav = snapshot.valuation
      ? {
          status: snapshot.valuation.authoritative_total_nav ? "AUTHORITATIVE_NAV" : "PRICED_NAV_LOWER_BOUND",
          value_usd_micros: String(snapshot.valuation.priced_nav_lower_bound_usd_micros),
          lower_bound: !snapshot.valuation.authoritative_total_nav,
          coverage_bps: snapshot.valuation.value_coverage_bps,
        }
      : P.navLowerBound(positions);
    const unvalued = Array.isArray(snapshot.valuation?.unvalued_identities)
      ? snapshot.valuation.unvalued_identities.length : 0;
    const coverage = snapshot.valuation
      ? {
          // The denominator is every position the scan knows about, including the ones it
          // confirmed and could not price. Counting only the registered rows read "1 / 1
          // aligned" beside an unresolved count of one, which cannot both be true.
          position_count: positions.length + unvalued,
          priced_positions: positions.filter((position) => position.value_usd_micros != null).length,
          unresolved_positions: snapshot.valuation.unresolved_position_count,
          position_coverage_bps: snapshot.valuation.position_count_coverage_bps,
          value_coverage_bps: snapshot.valuation.value_coverage_bps,
          discovery_complete: snapshot.discovery ? snapshot.discovery.complete !== false : true,
        }
      : P.coverageSummary(positions);
    const capital = P.deployableStablecoinCapital(positions);
    U.renderMetrics?.(snapshot, nav, coverage, capital);
    U.renderAllocation?.(positions, snapshot.allocation);
    U.renderDeployable?.(capital, positions);
    U.renderChains?.(snapshot);
    U.renderCoverage?.(state.universe, positions);
    U.renderOverviewOpportunity?.(state.candidate);
    U.renderRisk?.(positions);
    U.renderMaturity?.(state.universe, positions);
    U.renderEvidence?.(snapshot, positions, nav, state.candidate);
    U.renderExecution?.();
  }

  // Every portfolio control rebuilds the markup it lives in, so activating one drops focus to
  // the body and the next keypress does nothing. Restore by a stable identity: pagination keeps
  // data-holdings-nav because its page number changes under it.
  const FOCUS_KEYS = ["transferControl", "holdingsControl", "exposureDimension", "exposureUnit", "holdingsNav", "pnlRange", "pnlUnit", "pnlSeries"];
  const FOCUS_ATTRIBUTES = {
    transferControl: "data-transfer-control",
    holdingsControl: "data-holdings-control",
    exposureDimension: "data-exposure-dimension",
    exposureUnit: "data-exposure-unit",
    holdingsNav: "data-holdings-nav",
    pnlRange: "data-pnl-range",
    pnlUnit: "data-pnl-unit",
    pnlSeries: "data-pnl-series",
  };

  function renderPortfolioPreservingFocus() {
    renderOverview();
  }

  // Derived history is what turns Cumulative P&L from UNKNOWN into a figure, and it needs the
  // transfers that funded the wallet. Driven from the render rather than from one point in the
  // read, so a screen that is showing UNKNOWN with no scan running always starts one -- whatever
  // sequence of refreshes, chain switches or failures got it there. The key makes that at most
  // one attempt per wallet, chain and finalized block, so it cannot loop.
  // Indexed history for a book this page cannot scan: the request carries no browser transfers,
  // and the Transactions tab says the scan did not happen.
  // The explicit refresh controls retry an index-only history whose request failed (review dn):
  // the placeholder that keeps automatic repaints from re-requesting must not stop the reader.
  function retryIndexedHistory() {
    const viewed = viewedWallet();
    if (!viewed.account || state.sampleVisible || state.transferHistory?.status !== "UNAVAILABLE") return false;
    state.transferScanRequestedFor = null;
    state.history = null;
    state.historyError = null;
    ensureIndexedHistory(viewed);
    return true;
  }

  // Whether this browser can scan the viewed wallet's transfers itself: a provider on the
  // viewed chain. Without one, the platform's index is all there is.
  function browserCanScan() {
    return Boolean(state.provider?.request) && viewedWallet().chainId === state.wallet.chainId;
  }

  function ensureIndexedHistory(viewed) {
    const key = `indexed:${viewed.account.toLowerCase()}:${viewed.chainId}`;
    if (state.transferScanRequestedFor === key && state.transferHistory) return;
    state.transferScanRequestedFor = key;
    state.transferHistory = {status: "UNAVAILABLE", declared_depth: null, assets: [], rows: [], to_block: null,
      reason: "No browser wallet on this chain to scan transfers; the platform's indexed history is shown."};
    void refreshActivity();
  }

  function ensureTransferScan() {
    if (localRpcUnverified()) return;
    const viewed = viewedWallet();
    if (state.sampleVisible || !viewed.account || state.transferScanSuppressed) return;
    const record = portfolioRecord(viewed.account, viewed.chainId);
    const indexed = historyIndexed(viewed.chainId);
    // What the browser can scan: a provider on the viewed chain, and only chains this page scans
    // transfers on (a pay-only chain is never read through the provider).
    const scannable = Boolean(state.provider?.request) && viewed.chainId === state.wallet.chainId
      && REGISTRY_CHAINS.some((chain) => chain.chain_id === viewed.chainId && chain.roles.includes("portfolio"));
    if (!record?.snapshot) {
      // A read in flight is waited for. A read that failed, or one that will never start because
      // nothing here can read this chain, must not leave Transactions empty on a chain the platform
      // indexes (owner 2026-10-21: Base Sepolia purchases showed nowhere while the scan had
      // nothing to scan with).
      // Only for a history with nothing yet: a history already on screen is never replaced here.
      if (indexed && !state.transferHistory && (record?.error || (record == null && !scannable))) ensureIndexedHistory(viewed);
      return;
    }
    // Nothing to scan with on this chain: a watched book, and a connected one alike, get the
    // platform's indexed history alone.
    if (!scannable) {
      if (indexed || !viewed.connected) ensureIndexedHistory(viewed);
      return;
    }
    // A read at a safe or depth block anchors no P&L, but the wallet's transfers -- and the indexed
    // history Transactions requests beside them -- need no anchor: the scan then ends at the
    // provider's own finalized block, and the P&L stays UNKNOWN until both share one.
    const block = Number(record.finalizedRead?.block_number);
    const anchored = Number.isSafeInteger(block);
    const history = state.transferHistory;
    if (history?.status === "LOADING") return;
    // A history already at the block the snapshot reconciles against is finished, not stale.
    if (history?.status === "COMPLETE" && (!anchored || Number(history.to_block) === block)) return;
    const key = `${viewed.account.toLowerCase()}:${viewed.chainId}:${anchored ? block : "unanchored"}`;
    // The key stops a second launch while a scan exists (running, complete or failed); a history the
    // refresh cleared while the scan was running is nothing known, so that key must not hold.
    if (state.transferScanRequestedFor === key && history) return;
    state.transferScanRequestedFor = key;
    void refreshTransfers();
  }

  // A wallet's transfers, read from the platform. The history itself carries no value -- inventing
  // one is the whole thing the engine declines to do; what each transfer was worth comes back
  // beside it, at the stored daily price nearest its moment, and with the balances the scan
  // reconciled against, what each asset earned.
  async function fetchWalletHistory(wallet, chainIds, transfers, registry, generation, balances = null, indexed = []) {
    const operation = {
      schema_version: "fenorium/platform-wallet-history-request/2",
      method_version: "history/2",
      as_of: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      wallet,
      chain_ids: chainIds,
      ...(indexed.length ? { indexed_chain_ids: indexed } : {}),
      registry,
      transfers,
      ...(Array.isArray(balances) && balances.length ? { balances } : {}),
    };
    // Choose a newest-first prefix by actual UTF-8 body size, then send it in engine order.
    const recent = transfers.slice().sort((a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at)
      || b.chain_id - a.chain_id || b.block_number - a.block_number || b.log_index - a.log_index);
    const serialize = (count) => JSON.stringify({...operation, transfers: recent.slice(0, count)
      .sort((a, b) => a.chain_id - b.chain_id || a.block_number - b.block_number || a.log_index - b.log_index)});
    const encoder = new TextEncoder();
    const cap = 900000;
    let body = serialize(recent.length);
    if (encoder.encode(body).byteLength > cap) {
      let low = 0, high = recent.length;
      if (encoder.encode(serialize(0)).byteLength > cap) {
        throw makeCodeError("HISTORY_REQUEST_TOO_LARGE", "History request metadata exceeds the byte limit");
      }
      while (low < high) {
        const mid = Math.ceil((low + high) / 2);
        if (encoder.encode(serialize(mid)).byteLength <= cap) low = mid;
        else high = mid - 1;
      }
      body = serialize(low);
    }
    const sent = JSON.parse(body).transfers.length;
    const response = await root.fetch("/v1/wallet-history", {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        // The generation moves with the body. Keeping one key per wallet meant the second
        // read of the same wallet -- a later `as_of`, more transfers -- collided with the
        // first and came back 409, so history could never refresh in a session.
        "Idempotency-Key": `history-${PORTFOLIO_SESSION_ID}-${generation}-${wallet.toLowerCase()}`,
      },
      body,
    });
    let envelope = null;
    try { envelope = await response.json(); } catch (_) { envelope = null; }
    if (!response.ok) throw makeCodeError(envelope?.error || "HISTORY_REQUEST_FAILED", envelope?.message || `history request failed (HTTP ${response.status})`);
    if (!validPlatformEnvelope(envelope) || !object(envelope.data)) {
      throw makeCodeError("INVALID_HISTORY_RESPONSE", "history HTTP 200 response failed its shape and execution-consistency checks");
    }
    const data = envelope.data;
    // The history kernel validates the history's own contract, wrapped (v2) or bare.
    const history = object(data.history) ? data.history : data;
    // Keyed by the identity both sides build the same way: chain, transaction, log index.
    const values = {};
    for (const row of Array.isArray(data.transfer_values?.values) ? data.transfer_values.values : []) {
      if (object(row) && typeof row.identity === "string") {
        values[row.identity] = {
          value_usd_micros: row.value_usd_micros == null ? null : String(row.value_usd_micros),
          reason: row.reason ?? null,
          verified: row.verified === true,
        };
      }
    }
    return {
      ...P.walletHistoryView(history, wallet),
      values,
      indexed: Array.isArray(data.indexed) ? data.indexed : [],
      scannedChains: Array.isArray(history.scanned_chains) ? history.scanned_chains : [],
      performance: object(data.performance) ? data.performance : null,
      scanned: transfers.length,
      sent,
      truncated: sent < transfers.length,
    };
  }

  // Reuse the portfolio's transfer scan for browser observations so history does not scan
  // the same ERC-20 logs a second time.
  function transfersForHistory(rows, wallet) {
    const owner = String(wallet || "").toLowerCase();
    const seen = new Set();
    const out = [];
    for (const row of Array.isArray(rows) ? rows : []) {
      const amount = String(row?.raw_amount ?? "");
      // A scan row carries a sign for the ledger; the history contract carries a direction and
      // a magnitude, so the sign is read and then dropped rather than sent as a negative.
      const magnitude = amount.startsWith("-") ? amount.slice(1) : amount;
      if (!/^[0-9]+$/.test(magnitude)) continue;
      const identity = `${Number(row.chain_id)}:${String(row.tx_hash).toLowerCase()}:${Number(row.log_index)}`;
      if (seen.has(identity)) continue;
      seen.add(identity);
      const self = row.from === owner && row.to === owner;
      out.push({
        chain_id: Number(row.chain_id),
        token: row.asset_address,
        counterparty: self ? owner : row.action === "Outgoing" ? row.to : row.from,
        direction: self ? "SELF" : row.action === "Outgoing" ? "OUT" : "IN",
        raw_amount: magnitude,
        block_number: Number(row.block_number),
        transaction_hash: row.tx_hash,
        occurred_at: row.timestamp,
        log_index: Number(row.log_index),
        decimals: row.decimals != null && Number.isSafeInteger(Number(row.decimals)) ? Number(row.decimals) : null,
        symbol: row.symbol || null,
      });
    }
    return out.sort((a, b) => a.chain_id - b.chain_id || a.block_number - b.block_number || a.log_index - b.log_index);
  }

  async function refreshActivity() {
    const viewed = viewedWallet();
    const wallet = viewed.account;
    const chainId = viewed.chainId;
    const scan = state.transferHistory;
    const rows = scan?.rows;
    if (!wallet || !Array.isArray(rows)) return;
    const generation = ++state.historyGeneration;
    const walletGeneration = state.walletGeneration, viewGeneration = state.viewGeneration;
    const portfolioReadGeneration = state.portfolioReadGeneration;
    const historyAnchor = {account: wallet.toLowerCase(), chain_id: chainId, block: scan.to_block == null ? null : Number(scan.to_block)};
    const isCurrent = () => generation === state.historyGeneration
      && walletGeneration === state.walletGeneration && viewGeneration === state.viewGeneration
      && String(viewedWallet().account || "").toLowerCase() === wallet.toLowerCase();
    const discovery = {
      status: scan.status ?? null,
      reasons: [scan.error, scan.reason, ...(scan.assets || []).map((record) => record.reason)]
        .filter((reason) => typeof reason === "string" && reason.length > 0),
    };
    const record = portfolioRecord(wallet, chainId);
    const indexed = historyIndexed(chainId) ? [chainId] : [];
    const held = record?.heldValueByChain || {};
    const heldChains = new Set(record?.heldChains || []);
    const others = P.REGISTRY_CHAINS.filter((chain) => heldChains.has(chain.chain_id) && chain.chain_id !== chainId && !chain.testnet && chain.history_index)
      .map((chain) => ({chainId: chain.chain_id, value: P.asBigInt(held[chain.chain_id]) ?? 0n}))
      .sort((a, b) => a.value === b.value ? a.chainId - b.chainId : a.value > b.value ? -1 : 1);
    // The platform takes at most six chains per request, the connected one among them.
    indexed.push(...others.slice(0, 5).map((chain) => chain.chainId));
    const chainIds = [chainId, ...indexed.filter((id) => id !== chainId)];
    // Every chain the history covers, so a known instrument on an indexed chain is recognised too.
    const registry = chainIds.flatMap((id) => P.registeredAssetsFromUniverse(registryUniverse(), id)).map((asset) => ({
      chain_id: asset.chain_id, token: asset.address, identity: asset.identity,
    }));
    // The balances the scan reconciles against, at the block it ended on. With them the platform
    // can say what each asset earned; without them it values the transfers and nothing more.
    const anchor = record?.finalizedRead;
    const balances = anchor && Number.isSafeInteger(Number(anchor.block_number)) && Number(scan.to_block) === Number(anchor.block_number)
      ? (anchor.observations || [])
        .filter((observation) => object(observation) && !observation.error && /^[0-9]+$/.test(String(observation.raw_amount)) && configuredAddress(observation.address))
        .map((observation) => ({
          chain_id: Number(observation.chain_id), token: observation.address,
          raw_amount: String(observation.raw_amount), block_number: Number(anchor.block_number),
        }))
      : null;
    try {
      const view = await fetchWalletHistory(
        wallet.toLowerCase(), chainIds, transfersForHistory(rows, wallet), registry, generation, balances, indexed);
      if (!isCurrent()) return;
      // Kept while the wallet's finalized read is still the block the scan reconciled against, dropped
      // once that read has moved to another block. A read that lost its finalized anchor meanwhile (a
      // safe or depth block) keeps the transfers, never a P&L that was anchored elsewhere.
      const block = portfolioRecord(wallet, chainId)?.finalizedRead?.block_number;
      // An unanchored history (the platform's index alone, no browser scan) is never stale against
      // the portfolio's block: it carries transfers and no P&L.
      if (historyAnchor.block != null && block != null && Number(block) !== historyAnchor.block) return;
      const scannedIds = new Set(rows.map((row) => row.id));
      view.indexedRows = P.rowsFromHistoryEvents(view.events, wallet).filter((row) => !scannedIds.has(row.id));
      state.history = {...view, discovery, anchor: historyAnchor, portfolioReadGeneration,
        ...(block == null || historyAnchor.block == null ? {performance: null} : {})};
      state.historyError = null;
    } catch (error) {
      if (!isCurrent()) return;
      state.history = null;
      state.historyError = [boundedAdapterError(error),
        discovery.status === "COMPLETE" ? null : `Transfer discovery ${discovery.status || "UNKNOWN"}`,
        ...discovery.reasons].filter(Boolean).join(" · ");
    } finally {
      if (isCurrent()) {
        renderOverview();
      }
    }
  }

  // What the history read establishes, said above the Transactions table: waiting, scanning,
  // complete, partial or truncated. A short list and a complete past look identical in a table,
  // and only one of them is a statement about the wallet. A failed read, a forgotten history and
  // an index read in progress are stated by the tab itself.
  function transferHistoryStatus() {
    if (state.sampleVisible) return null;
    const view = state.history;
    const viewed = viewedWallet();
    if (!view) {
      if (state.historyError || (viewed.account && state.transferScanSuppressed)) return null;
      if (viewed.account && (!viewed.connected || state.transferHistory?.status === "UNAVAILABLE")) return null;
      return {coverage: "", summary: viewed.account && state.transferHistory?.status === "LOADING"
        ? `Reading this wallet's finalized transfers… ${state.transferHistory.scanned || 0} of ${state.transferHistory.scanning || 0} block ranges scanned.`
        : viewed.account
          ? "Waiting for this wallet's portfolio read before loading transfer history."
          : "Connect a wallet, or add one to watch, to read its transfer history."};
    }
    const { total, verified, recognised } = view.counts;
    const discovery = view.discovery;
    const scanComplete = discovery?.status === "COMPLETE" && !discovery.reasons.length;
    let summary = total
      ? `${view.truncated ? "The platform checked " : ""}${total} transfer${total === 1 ? "" : "s"} · ${recognised} in instruments Fenorium tracks · ${verified} confirmed against the chain.`
      : scanComplete && !view.truncated ? "No transfers were found for this wallet on the chains that were read."
        : "Transfer history is unavailable or incomplete.";
    if (!scanComplete) summary += discovery?.status === "UNKNOWN" ? " Transfer discovery UNKNOWN." : " Transfer discovery incomplete.";
    if (discovery?.reasons.length) summary += ` ${[...new Set(discovery.reasons)].join("; ")}`;
    // The table lists every scanned transfer; only the request that confirms and values them is
    // cut to fit, so the sentence says what was cut from that request, not from the table.
    if (view.truncated) summary += ` It was sent the newest ${view.sent} of ${view.scanned} scanned transfers, to fit the request size limit; older rows carry no confirmation or value from it.`;
    let coverage = view.complete && scanComplete
      ? "This is every transfer on the chains that were read."
      : `Partial history. ${view.gaps.map((gap) => gap.detail).join(" ")}`;
    const scan = state.transferHistory;
    if (viewed.chainId === BASE_SEPOLIA.chainId && scan?.from_block != null) {
      // A scan with a failed range covered less than it asked for: the interval is what was
      // requested, not what was read (review dn).
      coverage += scan.status === "COMPLETE"
        ? ` Scanned finalized blocks ${scan.from_block}–${scan.to_block}. Older activity is outside this scan.`
        : ` Requested finalized blocks ${scan.from_block}–${scan.to_block}; some ranges did not complete, so coverage inside them is unknown. Older activity is outside this scan.`;
    }
    return {summary, coverage};
  }

  // Every repaint rebuilds the panels, and not only the ones a control asks for: a history answer
  // or a scan finishing repaints too, and one of those arriving just after a filter was changed
  // dropped the focus the reader had on it. So the capture and restore live here, on every path.
  function renderOverview() {
    const active = root.document?.activeElement;
    const key = FOCUS_KEYS.find((name) => active?.dataset?.[name] != null);
    const selector = key ? `[${FOCUS_ATTRIBUTES[key]}="${active.dataset[key]}"]` : null;
    // Which of them, not just one of them. The Performance screen carries two panels with a
    // range control, so the identity is the attribute *and* the position: restoring by selector
    // alone sent the focus to the first match -- the chart at the top of the screen -- and
    // focusing it scrolled the reader there from whichever panel they had actually pressed.
    const matches = selector ? [...root.document.querySelectorAll(selector)] : [];
    const position = Math.max(0, matches.indexOf(active));
    paintOverview();
    const candidates = selector ? [...root.document.querySelectorAll(selector)] : [];
    const restored = candidates[position] || candidates[0] || null;
    // Restoring focus must not move the page: `focus()` scrolls its element into view, which on
    // a control the reader has just pressed is a jump to somewhere they were already looking.
    if (restored && typeof restored.focus === "function" && !restored.disabled) restored.focus({preventScroll: true});
  }

  // The portfolio renderer owns its table markup. Add a sale control only to held rows
  // that resolve to exactly one address-bearing universe instrument.
  function decorateSaleActions() {
    salePositions = new Map();
    const screen = $("portfolio-screen");
    if (!screen || !root.document || typeof root.document.createElement !== "function") return;
    if (state.sampleVisible) {
      if (execution?.kind === "sale") { execution = null; executionGeneration += 1; renderRouteQuotes(); }
      return;
    }
    let panel = $("portfolio-sale-route");
    if (!panel) { panel = root.document.createElement("div"); panel.id = "portfolio-sale-route"; screen.appendChild(panel); }
    if (execution?.kind === "sale") renderRouteQuotes();
    // A watched wallet's book is read-only: its tokens are not the connected wallet's to sell.
    if (!state.wallet.account || !viewedWallet().connected || !state.snapshot
      || !["summary", "holdings"].includes(state.portfolioTab)) return;
    const rows = P.portfolioRows(state.snapshot.positions || [], state.universe, state.snapshot.provenance?.as_of);
    const counts = new Map();
    for (const asset of state.universe || []) {
      if (!configuredAddress(asset?.address) || !Number.isSafeInteger(asset?.chain_id)) continue;
      const key = `${asset.chain_id}:${asset.address.toLowerCase()}`;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    for (const row of rows) {
      const key = `${row.chain_id}:${String(row.address || "").toLowerCase()}`;
      if (row.unresolved || !configuredAddress(row.address) || !Number.isSafeInteger(row.chain_id)
        || counts.get(key) !== 1 || !Number.isInteger(row.decimals) || row.decimals < 0 || row.decimals > 255
        || !/^[0-9]+$/.test(String(row.raw_units || "0")) || BigInt(row.raw_units || "0") <= 0n) continue;
      const matches = (state.universe || []).filter(asset => asset.chain_id === row.chain_id
        && String(asset.address || "").toLowerCase() === row.address.toLowerCase());
      if (matches.length !== 1 || matches[0].category === "settlement"
        || !Number.isInteger(matches[0].decimals) || matches[0].decimals !== row.decimals) continue;
      salePositions.set(key, {position: row, instrument: matches[0]});
    }
    for (const tr of screen.querySelectorAll("tr[data-holding-identity]")) {
      const identity = tr.getAttribute("data-holding-identity");
      const row = rows.find(item => item.identity === identity);
      if (!row) continue;
      const key = `${row.chain_id}:${String(row.address || "").toLowerCase()}`;
      if (!salePositions.has(key)) continue;
      const cell = tr.lastElementChild;
      if (!cell) continue;
      const button = root.document.createElement("button");
      button.type = "button"; button.className = "ghost"; button.textContent = "Sell";
      button.dataset.saleOpen = key;
      cell.appendChild(root.document.createTextNode(" "));
      cell.appendChild(button);
    }
  }

  function paintOverview() {
    const empty = $("overview-empty");
    const content = $("overview-content");
    const banner = $("sample-book-banner");
    const message = $("overview-empty-message");
    const connect = $("connect-overview-wallet");
    const sampleButton = $("view-sample-book");
    const showingSample = state.sampleVisible && Boolean(state.sampleSnapshot);
    const viewed = viewedWallet();
    const activeRecord = viewedRecord();
    // Holdings the platform read are tracked wherever the wallet is connected, and while that
    // read is in flight the chain is not yet known to be untracked.
    const payOnly = viewed.connected && onPayOnlyChain() && activeRecord?.source !== "server" && activeRecord?.loading !== true;
    const walletSnapshot = payOnly ? null : activeRecord?.snapshot || null;
    const showingWallet = !showingSample && Boolean(walletSnapshot && (walletSnapshot.positions?.length
      || (activeRecord?.source === "server" && activeRecord.venuePositions?.length)));
    const unreadChains = Object.entries(activeRecord?.servedChains || {})
      .filter(([, status]) => status !== "READ").map(([chainId]) => routeChainName(Number(chainId)));
    const showingContent = showingSample || showingWallet;
    const deferred = !(payOnly && !showingSample) && ["performance", "transactions"].includes(state.portfolioTab);
    const simulations = isTestnetReady() && testnetScenarios().length > 0;

    if (empty) empty.hidden = showingContent || deferred;
    if (content) content.hidden = !showingContent || deferred;
    if (banner) banner.hidden = !showingSample;
    if (message) {
      message.textContent = isTestnetBlocked()
        ? state.testnetWorkspace.error || "Testnet workspace data is unavailable. No wallet or network requests were started."
        : payOnly && !showingSample
        ? `Holdings on ${routeChainName(state.wallet.chainId)} are not tracked here — Fenorium only lets you pay from it.`
        : state.sampleError
        ? `Sample book unavailable: ${state.sampleError} No portfolio values were substituted.`
        : activeRecord?.loading
        ? "Reading holdings: every supported chain through the platform, or this chain's registered balances through Multicall3 at a finalized block. No portfolio value is shown until the response is attributed to this account."
        : activeRecord?.error
        ? `Wallet portfolio unavailable: ${activeRecord.error.code}. ${activeRecord.error.message} No sample or numeric fallback was substituted.`
        : walletSnapshot && unreadChains.length
        ? `Could not fully read ${unreadChains.join(", ")}. Holdings there are unknown.`
        : walletSnapshot
        ? "The finalized scan found no non-zero registered positions. Arbitrary tokens, protocol internals, debt, NFTs, and other chains were not inferred."
        : viewed.account
        ? (viewed.connected
          ? "Wallet connected. No verified portfolio snapshot is available for this wallet, so no portfolio values are shown."
          : "No verified portfolio snapshot is available for the watched wallet, so no portfolio values are shown.")
        : `Connect a wallet, or add one to watch, to view its registered positions, valuation coverage, allocation, and risk gates. No portfolio values are shown before that.${simulations ? " Or open a simulated portfolio: synthetic holdings drawn for the screens, not a wallet's." : ""}`;
    }
    if (connect) {
      connect.textContent = state.wallet.account ? "Wallet connected" : "Connect wallet";
      connect.disabled = isTestnetBlocked() || Boolean(state.wallet.account);
    }
    if (sampleButton) {
      sampleButton.disabled = isTestnetBlocked() || state.sampleLoading;
      sampleButton.textContent = simulations ? "View a simulated portfolio" : state.sampleLoading ? "Loading sample book…" : "View a sample book";
    }
    const overviewNavState = $("overview-nav-state");
    if (overviewNavState) {
      overviewNavState.textContent = overviewNavText();
      overviewNavState.parentElement?.setAttribute("title", overviewNavState.textContent);
    }
    if (banner && scenarioShowing()) {
      const scenario = testnetScenario();
      banner.innerHTML = `<strong>SIMULATED · ${U.esc(scenario.label || "Scenario")}</strong><span>${U.esc(scenario.description || "")} Synthetic positions and history, not a wallet; nothing here was read from a chain.</span><button class="ghost" id="hide-sample-book" type="button">Back to my wallet</button>`;
    }
    if (showingSample) {
      state.snapshot = state.sampleSnapshot;
      state.candidate = state.sampleSnapshot.opportunity_candidate || null;
      renderSnapshot(state.sampleSnapshot);
    } else if (showingWallet) {
      state.snapshot = walletSnapshot;
      state.candidate = null;
      renderSnapshot(walletSnapshot);
    } else {
      state.snapshot = null;
      state.candidate = null;
    }
    U.renderPortfolioScreen?.(state.snapshot, state.universe, state.portfolioTab, portfolioScreenOptions(activeRecord, viewed));
    decorateSaleActions();
    // The venue positions and the lending orders are the viewed wallet's. Under a fixture they
    // would put two books on one screen (review dg): hidden while it shows, back when it goes,
    // and a refresh that lands meanwhile writes its content without showing the panel.
    for (const id of ["venue-positions", "lending-orders"]) {
      const panel = $(id);
      if (panel) panel.hidden = showingSample || !panel.innerHTML;
    }
    if (payOnly && !showingSample) {
      // Keep the normal header/tabs, but never render an untracked chain as an empty book.
      const screen = $("portfolio-screen"), deferredPanel = $("portfolio-deferred");
      if (screen) screen.innerHTML = "";
      if (deferredPanel) { deferredPanel.hidden = true; deferredPanel.innerHTML = ""; }
      const venues = $("venue-positions");
      if (venues) venues.hidden = true;
      return;
    }
    ensureTransferScan();
  }

  async function showSampleBook() {
    if (isTestnetReady() && testnetScenarios().length) return selectTestnetScenario(state.testnetScenarioId);
    if (state.sampleLoading) return null;
    state.sampleLoading = true;
    renderOverview();
    try {
      if (!state.sampleSnapshot) {
        const response = await root.fetch("data/sample-portfolio.json", { cache: "no-store" });
        if (!response.ok) throw Error(`fixture request failed (HTTP ${response.status})`);
        state.sampleSnapshot = await response.json();
      }
      state.sampleError = null;
      state.sampleVisible = true;
      return state.sampleSnapshot;
    } catch (error) {
      state.sampleError = error.message || String(error);
      return null;
    } finally {
      state.sampleLoading = false;
      renderOverview();
    }
  }

  function hideSampleBook() {
    if (clearTestnetScenario()) {
      renderWalletSwitcher({screens: false});
      renderOverview();
      return;
    }
    state.sampleVisible = false;
    renderOverview();
  }

  function assistantAsOf() {
    const choices = [state.metadata?.as_of, state.metadata?.generated_at];
    const chosen = choices.find(validTime);
    return chosen ? new Date(chosen).toISOString() : null;
  }

  function assistantAssetSegment(row) {
    const value = compareKey(row).replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    return value || "unknown";
  }

  function assistantReasonCode(row) {
    const supplied = row?.reason_code || row?.rejection_code;
    if (typeof supplied === "string" && /^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(supplied)) return supplied;
    const verdictCode = String(P.marketVerdict?.(row) || "NOT_APPLICABLE").toUpperCase().replace(/[^A-Z0-9_-]+/g, "_");
    return `PRECOMPUTED_${verdictCode}`;
  }

  function assistantInstrumentFacts(selected) {
    if (!selected) return [];
    const assetSegment = assistantAssetSegment(selected);
    const reasonCode = assistantReasonCode(selected);
    const recordedReason = selected.reason || selected.rejection || null;
    const facts = [{
      field_ref: `asset/${assetSegment}/verdict`,
      status: P.marketVerdict?.(selected) || "N/A",
      reason_code: reasonCode,
      value: null,
      unit: "STATE",
      source_ref: "local:universe/verdict",
    }];
    if (recordedReason) {
      facts.push({
        field_ref: `asset/${assetSegment}/reason`,
        status: "RECORDED",
        reason_code: reasonCode,
        value: String(recordedReason),
        unit: "TEXT",
        source_ref: "local:universe/reason",
      });
    }
    if (isSimulatedRow(selected)) {
      facts.push({
        field_ref: `asset/${assetSegment}/provenance`,
        status: "SIMULATED_FIXTURE",
        reason_code: "TESTNET_PREVIEW_CATALOG",
        value: null,
        unit: "STATE",
        source_ref: "local:testnet-catalog",
      });
    }
    if (selected.is_demo) {
      facts.push({
        field_ref: `asset/${assetSegment}/deployment`,
        status: deploymentStatus(selected),
        reason_code: "WEB_CONFIG_DEPLOYMENT_FIELDS",
        value: null,
        unit: "STATE",
        source_ref: "local:config/deployment",
      });
    }
    return facts;
  }

  function buildAssistantFacts() {
    const positions = state.snapshot?.positions || [];
    const nav = P.navLowerBound(positions);
    const coverage = P.coverageSummary(positions);
    const selected = state.selected;
    // A sample book or a simulated portfolio on screen is a fixture, and the envelope says so
    // beside the wallet's own status (review de): the NAV it carries is nobody's holdings.
    const fixture = state.sampleVisible === true && Boolean(state.snapshot);
    const provenance = object(state.snapshot?.provenance) ? state.snapshot.provenance : {};
    const facts = [
      {
        field_ref: "portfolio/nav/status",
        status: nav.status || "NOT_COMPUTABLE",
        reason_code: nav.reason || "RECORDED",
        value: nav.value_usd_micros ?? null,
        unit: "USD_MICROS",
        source_ref: fixture ? "local:fixture/nav" : "local:snapshot/nav",
      },
      {
        field_ref: "portfolio/valuation/coverage",
        status: coverage.value_coverage_status || "VALUE_COVERAGE_NOT_COMPUTABLE",
        reason_code: coverage.value_coverage_status || "RECORDED",
        value: coverage.value_coverage_bps ?? null,
        unit: "BPS",
        source_ref: fixture ? "local:fixture/coverage" : "local:snapshot/coverage",
      },
      {
        field_ref: "portfolio/provenance",
        status: !state.snapshot ? "NO_SNAPSHOT" : fixture ? "FIXTURE" : "WALLET_READ",
        reason_code: String(provenance.scenario_kind || provenance.kind || (fixture ? "SAMPLE_BOOK" : "RECORDED")),
        value: fixture ? String(testnetScenario()?.label || provenance.label || "sample book") : null,
        unit: "TEXT",
        source_ref: "local:snapshot/provenance",
      },
      {
        field_ref: "wallet/status",
        status: state.wallet.status,
        reason_code: state.wallet.chainId == null ? "CHAIN_NOT_READ" : `CHAIN_${state.wallet.chainId}`,
        value: state.wallet.chainId == null ? null : String(state.wallet.chainId),
        unit: "CHAIN_ID",
        source_ref: "local:injected-provider",
      },
    ];
    if (selected) {
      facts.push(...assistantInstrumentFacts(selected));
    }
    return facts;
  }

  function buildAssistantEnvelope() {
    return P.createAssistantEnvelope({ as_of: assistantAsOf(), facts: buildAssistantFacts() });
  }

  function chatText(value, limit = 200) {
    return Array.from(String(value ?? "UNKNOWN").replace(/0x[0-9a-fA-F]{40}/g, "[address omitted]")).slice(0, limit).join("");
  }

  // Both columns use the same figures as the market, independently of the remaining term.
  function assistantNetCells(row) {
    const hp = P.holdingPeriod?.(row);
    return [U.assistantRateValue(row, U.assistantPlain(cell(row, "net"))),
      hp ? U.assistantRateValue(row, `${U.pct(hp.net_bps)} · to maturity · ${Number(hp.days).toFixed(1)} days`)
        : P.marketFieldApplies(row, "maturity") ? "UNKNOWN · Not measured" : "— · no maturity: held open-ended"];
  }

  function portfolioScreenOptions(activeRecord = viewedRecord(), viewed = viewedWallet()) {
    return {...state.holdingsOptions, ...state.exposureOptions, // The sample book is a fixture, not this wallet: forwarding a failed scan's state made it
      // say "Refresh failed" and "Showing the previous snapshot" about data that never refreshed.
      loading: state.sampleVisible !== true && activeRecord?.loading === true,
      error: state.sampleVisible === true ? null : activeRecord?.error,
      transferHistory: state.sampleVisible ? null : state.transferHistory,
      simulatedActivity: scenarioShowing() ? state.testnetActivity : null,
      historyStatus: transferHistoryStatus(),
      transferScanSuppressed: !state.sampleVisible && Boolean(viewed.account) && state.transferScanSuppressed,
      connected: !state.sampleVisible && Boolean(viewed.account) && (viewed.connected || Boolean(state.provider?.request)),
      serverPortfolio: !state.sampleVisible && activeRecord?.source === "server"
        ? {positions: activeRecord.venuePositions || [], totals: activeRecord.snapshot?.totals} : null,
      performance: state.sampleVisible ? null : P.derivePortfolioPerformance(state.transferHistory, activeRecord?.finalizedRead),
      // What the platform priced each transfer at, and what it says each asset earned.
      transferValues: state.sampleVisible ? null : state.history?.values || null,
      indexedRows: state.sampleVisible ? null : state.history?.indexedRows || null,
      indexed: state.sampleVisible ? null : state.history?.indexed || null,
      scannedChains: state.sampleVisible ? null : state.history?.scannedChains || null,
      // An index read that failed, so the Transactions tab says so instead of "reading".
      historyError: state.sampleVisible || state.history ? null : state.historyError || null,
      serverPerformance: state.sampleVisible || !activeRecord?.snapshot ? null : state.history?.performance || null,
      transferOptions: state.transferOptions};
  }

  function buildChatContext() {
    const all = state.notionalPending || state.universeError ? [] : filteredRows();
    const rows = all.slice(0, 60);
    const cells = row => {
      const score = row.fenorium_score;
      return [row.symbol || row.name, row.name, row.venue, row.chain, row.maturity || marketFieldState(row, "maturity").status,
        U.assistantRateValue(row, U.assistantPlain(cell(row, "headline"))), ...assistantNetCells(row),
        marketFieldState(row, "verdict").status === "N/A" ? "N/A" : score == null ? "UNKNOWN" : String(typeof score === "object" ? score.score ?? "UNKNOWN" : score),
        ...["depth", "tvl"].map(key => marketFieldState(row, key).status === "MEASURED" ? compactUsd(marketFieldState(row, key).value) : marketFieldState(row, key).status),
        marketFieldState(row, "permission").value === "GATED" ? "yes" : marketFieldState(row, "permission").value === "PERMISSIONLESS" ? "no" : "unknown",
        U.rewardQualifier?.(row) || "—"].map(value => chatText(value));
    };
    const selected = state.selected;
    const cached = selected && withRatingDay(instrumentAnalyticsCache.get(instrumentAnalyticsKey(selected)));
    const snapshot = state.sampleVisible || scenarioShowing() ? state.snapshot : viewedRecord()?.snapshot || state.snapshot;
    const instrument = selected ? U.assistantInstrumentContext(selected, cached?.status === "ready" ? cached.data : null, state.notionalSize, cached?.otherSnapshot, cached?.otherDay) : null;
    const portfolio = snapshot ? U.assistantPortfolioContext(snapshot, state.universe,
      scenarioShowing() ? "simulated" : state.sampleVisible ? "sample" : viewedWallet().connected ? "connected" : "watched", portfolioScreenOptions()) : null;
    const sanitize = value => Array.isArray(value) ? value.map(sanitize) : value && typeof value === "object"
      ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, sanitize(child)])) : typeof value === "string" ? chatText(value) : value;
    return sanitize({schema_version: "fenorium/assistant-context/1", as_of: assistantAsOf(),
      screen: ["overview", "market", "asset", "wallet", "settings"].includes(state.view) ? state.view : "overview",
      workspace: testnetWorkspace()?.local ? "local" : isTestnetReady() ? "testnet" : "mainnet",
      trade_size: formatNotional(state.notionalSize),
      market: {category: state.filters.category || "All", rows_total: all.length,
        sort: state.sort?.key ? `${headerLabel(state.sort.key, columns.find(([key]) => key === state.sort.key)?.[1] || state.sort.key)}, ${state.sort.dir === -1 ? "highest" : "lowest"} first` : "screen order",
        columns: ["Symbol", "Name", "Venue", "Chain", "Maturity", "Headline APY", "Net APY at trade size", "Net return to maturity at trade size", "Fenorium Score", "Depth", "TVL", "Gated", "Rewards"], rows: rows.map(cells)},
      instrument, portfolio});
  }

  function updateAssistantVisibility() {
    const visible = Boolean(chat.status?.backend && (chat.status.access === "open" || chat.key));
    if ($("assistant-toggle")) $("assistant-toggle").hidden = !visible;
    if (!visible) {
      if ($("assistant-panel")) $("assistant-panel").hidden = true;
      $("assistant-toggle")?.setAttribute("aria-expanded", "false");
    }
  }

  async function loadAssistantStatus() {
    try {
      const response = await root.fetch("/v1/assistant", {cache: "no-store"});
      if (!response.ok) throw new Error("Assistant status unavailable");
      const body = await response.json();
      chat.status = body.data || body;
    } catch (_) { /* Keep the last known status so a transient refresh does not hide the pill. */ }
    updateAssistantVisibility();
    renderSettingsScreen();
    const panel = $("assistant-panel");
    if (panel && !panel.hidden) assistantContextLine();
  }

  function saveAssistantKey() {
    chat.key = $("assistant-access-key")?.value.trim() || null;
    if (chat.key) safeStorageSet(ASSISTANT_KEY, chat.key);
    else safeStorageRemove(ASSISTANT_KEY);
    updateAssistantVisibility();
    renderSettingsScreen();
  }

  function assistantError(status, body = {}, retry = null) {
    const code = body.code || (typeof body.error === "string" ? body.error : body.error?.code);
    if (code === "ASSISTANT_EMPTY_ANSWER") return "The assistant returned no answer. Try asking again.";
    if (code === "ASSISTANT_QUESTION_TOO_LONG") return "This question is too long for the local model. Shorten it and ask again.";
    if (status === 401) return "The assistant access key was refused. Update it in Settings.";
    if (status === 429) return `Too many questions — try again in ${/^\d+$/.test(String(retry)) ? retry : 60} s`;
    if (status === 503 && code === "ASSISTANT_NOT_RUNNING" && chat.status?.backend === "local") return "The local assistant isn't running." + (chat.status.start_hint ? ` Start it with \`${chat.status.start_hint}\`.` : "");
    if (status === 503 && code === "ASSISTANT_BUSY") return "The assistant is answering another question. Try again in a moment.";
    return "The assistant could not answer. Nothing else on the page changed.";
  }

  function assistantErrorMarkup(error) {
    const hint = String(error).match(/^(The local assistant isn't running\. Start it with )`(.+)`\.$/);
    return hint ? `${U.esc(hint[1])}<code>${U.esc(hint[2])}</code>.` : U.esc(error);
  }

  function renderAssistantChat() {
    const list = $("assistant-messages");
    if (list) {
      list.innerHTML = chat.turns.map(turn => `<article class="assistant-turn assistant-turn-${turn.role}"><b>${turn.role === "user" ? "You" : "Nori"}</b>${U.assistantMarkup(turn.content, turn.unverified || [])}${turn.unverified?.length ? `<p class="assistant-note">${turn.unverified.length} ${turn.unverified.length === 1 ? "figure above is" : "figures above are"} not in Fenorium's data — the assistant computed or guessed them.</p>` : ""}${turn.error ? `<p class="assistant-note">${assistantErrorMarkup(turn.error)}</p>` : ""}</article>`).join("");
      list.scrollTop = list.scrollHeight;
    }
    if ($("send-assistant")) $("send-assistant").textContent = chat.controller ? "Stop" : "Send";
  }

  function paintAssistantChat() {
    if (chat.paint != null) return;
    chat.paint = root.setTimeout(() => { chat.paint = null; renderAssistantChat(); }, 50);
  }

  function stopAssistantChat() { chat.controller?.abort(); }
  function newAssistantChat() {
    stopAssistantChat(); chat.turns = []; chat.contextSummary = null; assistantContextLine(); renderAssistantChat();
    $("assistant-question")?.focus?.();
  }

  function assistantContextLine(context = buildChatContext(), summary = chat.contextSummary) {
    const status = chat.status?.available === false
      ? chat.status.backend === "local" ? "The local assistant isn't running." + (chat.status.start_hint ? ` Start it with <code>${U.esc(chat.status.start_hint)}</code>.` : "")
        : "The assistant is not available right now."
      : null;
    const parts = summary
      ? [summary.market_rows_total > 0 ? "Market" : null,
        summary.market_rows_total > 0 ? `${summary.market_rows} of ${summary.market_rows_total} rows` : null,
        summary.instrument, summary.portfolio ? "Portfolio" : null]
      : [context.market ? "Market" : null, context.market ? `${context.market.rows.length} rows` : null,
        context.instrument?.title, context.portfolio ? "Portfolio" : null];
    const contextLine = "Sees: " + parts.filter(Boolean).join(" · ");
    if ($("assistant-context")) $("assistant-context").innerHTML = `${status ? `${status}<br>` : ""}${U.esc(contextLine)}`;
  }

  function assistantRequestBody(messages, context) {
    const payload = {schema_version: "fenorium/assistant-chat-request/1", messages, context};
    const encode = () => JSON.stringify(payload);
    let body = encode();
    for (const entries of [context.market?.rows, context.portfolio?.holdings, context.instrument?.analytics,
      context.portfolio?.risk, context.instrument?.facts, context.portfolio?.facts]) {
      while (entries?.length && new root.TextEncoder().encode(body).length > 65536) { entries.pop(); body = encode(); }
    }
    while (messages.length > 1 && new root.TextEncoder().encode(body).length > 65536) {
      messages.splice(0, 2); body = encode();
    }
    return body;
  }

  async function sendAssistantChat() {
    if (chat.controller) { stopAssistantChat(); return null; }
    const field = $("assistant-question");
    const question = field?.value.trim() || "";
    if (!question || question.length > 4000) return null;
    const context = buildChatContext();
    if (!context.as_of) {
      chat.turns.push({role: "user", content: question}, {role: "assistant", content: "", error: "The screen has no valid source timestamp. Reload the market before asking."});
      renderAssistantChat(); return null;
    }
    // A failed or stopped turn stays visible, but cannot break alternation in the next request.
    const history = chat.turns.filter(turn => turn.complete).map(({role, content}) => ({role, content: Array.from(content).slice(0, 4000).join("")}));
    const messages = [...history.slice(-10), {role: "user", content: question}];
    const user = {role: "user", content: question};
    const answer = {role: "assistant", content: "", unverified: []};
    chat.turns.push(user, answer);
    const controller = new root.AbortController(); chat.controller = controller;
    const body = assistantRequestBody(messages, context);
    field.value = ""; assistantContextLine(context); renderAssistantChat();
    let reader = null;
    try {
      const response = await root.fetch("/v1/assistant/chat", {method: "POST", signal: controller.signal,
        headers: {"Content-Type": "application/json", "Idempotency-Key": `assistant-${PORTFOLIO_SESSION_ID}-${++chat.serial}`,
          ...(chat.key ? {"X-Fenorium-Assistant-Key": chat.key} : {})},
        body});
      if (!response.ok) {
        let body = {}; try { body = await response.json(); } catch (_) { /* HTTP status still identifies the failure. */ }
        answer.error = assistantError(response.status, body, response.headers?.get?.("Retry-After"));
        return null;
      }
      let finished = false;
      const line = text => {
        if (!text.trim() || finished) return;
        const event = JSON.parse(text);
        if (event.type === "start" && event.context) { chat.contextSummary = event.context; assistantContextLine(context); }
        if (event.type === "delta") { answer.content += String(event.text || ""); paintAssistantChat(); }
        if (event.type === "done") {
          answer.unverified = Array.isArray(event.unverified) ? event.unverified : [];
          answer.complete = user.complete = Boolean(answer.content.trim());
          if (!answer.complete) answer.error = assistantError(503, {code: "ASSISTANT_EMPTY_ANSWER"});
          finished = true;
        }
        if (event.type === "error") { answer.error = assistantError(503, event); finished = true; }
      };
      if (response.body) {
        reader = response.body.getReader(); const decoder = new root.TextDecoder(); let pending = "";
        while (!controller.signal.aborted) {
          const chunk = await reader.read();
          pending += chunk.done ? decoder.decode() : decoder.decode(chunk.value, {stream: true});
          const lines = pending.split("\n"); pending = lines.pop(); lines.forEach(line);
          if (chunk.done) { if (pending.trim()) line(pending); break; }
        }
      } else (await response.text()).split("\n").forEach(line);
      if (!finished && !controller.signal.aborted) answer.error = assistantError(500);
      return answer;
    } catch (error) {
      answer.error = error.name === "AbortError" ? "Answer stopped." : assistantError(500);
      return null;
    } finally {
      if (controller.signal.aborted) { try { await reader?.cancel?.(); } catch (_) { /* Already closed. */ } }
      reader?.releaseLock?.();
      if (chat.controller === controller) chat.controller = null;
      if (chat.paint != null) { root.clearTimeout(chat.paint); chat.paint = null; }
      renderAssistantChat();
    }
  }

  // Rows are focusable: arrows move the selection, Enter or Space selects, Escape closes the inspector.
  function rowKeyAction(key) {
    return {ArrowDown: "next", ArrowUp: "previous", Enter: "select", " ": "select", Escape: "close"}[key] || null;
  }

  function selectRow(index, openAsset = false) {
    if (!state.universe[index]) return;
    state.selected = normalized(state.universe[index], index);
    void refreshRouteQuotes();
    if (openAsset) {
      state.assetTab = "summary";
      activate("asset");
      renderSelectedAsset();
      if (state.selected.is_demo) void refreshPurchase(state.selected);
    } else {
      renderMarket();
    }
  }

  function toggleCompare(key, checked) {
    if (checked && state.compareSelection.size >= 5 && !state.compareSelection.has(key)) {
      state.compareMessage = "Maximum five instruments. Uncheck one before selecting another.";
      renderMarket();
      return;
    }
    if (checked) state.compareSelection.add(key);
    else state.compareSelection.delete(key);
    state.compareMessage = "";
    renderMarket();
  }

  function openCompare() {
    state.compareOpen = true;
    renderCompareState();
    $("compare-panel")?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }

  function download(name, type, body) {
    if (typeof document === "undefined") return;
    const link = document.createElement("a");
    const url = URL.createObjectURL(new Blob([body], { type }));
    link.href = url;
    link.download = name;
    link.click();
    const timer = root.setTimeout || setTimeout;
    timer(() => URL.revokeObjectURL(url), 0);
  }

  function exportValue(row, key) {
    if (key === "reward_apy") return U.rewardQualifier(row) ? U.pct(row.reward_apy_bps) : "";
    if (key === "net_to_maturity" && !P.holdingPeriod?.(row)) return P.marketFieldApplies(row, "maturity") ? "UNKNOWN | Not measured" : "— | no maturity: held open-ended";
    if (["headline", "net", "net_to_maturity", "execution", "depth", "tvl", "days"].includes(key)) {
      const value = numericCell(row, key);
      const source = (candidate) => candidate == null || candidate === "" ? "UNKNOWN · Not measured" : candidate;
      if (["net", "net_to_maturity", "execution"].includes(key)) {
        return `${value} | cost_model=${source(row.rating?.cost_model)} | not a live executable quote | depth_source=${source(row.depth_source)}`;
      }
      if (key === "depth") return `${value} | depth_source=${source(row.depth_source)}`;
      if (key === "tvl") return `${value} | tvl_source=${source(row.tvl_source)}`;
      return value;
    }
    const field = marketFieldState(row, key);
    if (field.status !== "MEASURED") return field.status;
    if (key === "verdict") return U.scoreLabel(row) + (row.score_cap ? ` · capped: ${row.score_cap}` : "");
    if (key === "maturity") return row.maturity;
    if (key === "permission") return row.gate.status;
    if (key === "execution_mode") return row.is_demo ? "Base Sepolia demo purchase" : mainnetExecutionOpen() ? "Mainnet · route on request" : "Mainnet execution disabled";
    return row[key];
  }

  function exportMarket() {
    const fields = [...columns.filter(([key]) => state.columns.has(key)), ["reward_apy", "reward_apy"]];
    const quote = (value) => `"${String(value).replaceAll('"', '""')}"`;
    const csv = [fields.map(([key, label]) => columnLabel(key, label)), ...filteredRows().map((row) => fields.map(([key]) => exportValue(row, key)))]
      .map((row) => row.map(quote).join(","))
      .join("\n");
    download("fenorium-market.csv", "text/csv", csv);
  }

  function utilityAction(name) {
    if (name === "methodology") {
      activate("market");
      $("universe-status")?.focus?.();
    }
  }

  function bindEvents() {
    if (typeof document === "undefined" || state.eventsBound) return;
    state.eventsBound = true;
    document.addEventListener("click", (event) => {
      const button = event.target.closest?.("button");
      const row = event.target.closest?.("tr[data-row]");
      if (row && !event.target.closest("button, input, a")) {
        selectRow(Number(row.dataset.row), false);
        return;
      }
      if (!button) return;
      if (button.dataset.positionExit !== undefined) void requestPositionExit(button.dataset.positionExit);
      if (button.dataset.saleMaturedRedeem !== undefined) void requestPositionExit(button.dataset.saleMaturedRedeem);
      if (button.dataset.positionExitAccept !== undefined) void requestPositionExit(button.dataset.positionExitAccept, Number(button.dataset.exitCost));
      if (button.dataset.positionExitCheck !== undefined) void checkPositionExitReceipt(button.dataset.positionExitCheck);
      if (button.dataset.positionExitRelease !== undefined) releasePositionExit(button.dataset.positionExitRelease);
      if (button.dataset.positionExitSend !== undefined) void sendPositionExit(button.dataset.positionExitSend);
      if (button.dataset.positionExitSwitch !== undefined) void switchPositionExitChain(button.dataset.positionExitSwitch);
      if (button.dataset.tab) activate(button.dataset.tab);
      if (button.dataset.portfolioTab) {
        state.portfolioTab = button.dataset.portfolioTab;
        if (["transactions", "performance"].includes(state.portfolioTab) && !state.transferHistory) void refreshTransfers();
        activate("overview");
        renderOverview();
      }
      if (button.id === "scan-transfers") {
        state.transferScanSuppressed = false;
        if (browserCanScan()) void refreshTransfers(P.transferScanDepth(viewedWallet().chainId).blocks);
        else retryIndexedHistory();
      }
      if (button.dataset.routeExecute !== undefined) selectExecutionRow(button.dataset.routeExecute);
      if (button.dataset.saleOpen !== undefined) void selectSalePosition(button.dataset.saleOpen);
      if (button.dataset.saleAcceptCost !== undefined) {
        void requestExecutionRoute(execution?.amount_input, Number(button.dataset.saleAcceptCost));
      }
      if (button.dataset.purchaseAcceptCost !== undefined) {
        void requestExecutionRoute(execution?.amount_input, Number(button.dataset.purchaseAcceptCost));
      }
      if (button.dataset.lendingSelect !== undefined) void selectLendingPosition();
      if (button.dataset.lendingSign !== undefined) void signLendingOrder();
      if (button.dataset.lendingAuthorize !== undefined) void sendLendingInstruction("authorize");
      if (button.dataset.lendingCancel !== undefined) void cancelLendingOrder(button.dataset.lendingCancel);
      if (button.dataset.positionDeposit !== undefined) void selectDepositPosition();
      if (button.dataset.routeRequest !== undefined) {
        if (execution?.kind === "lending") {
          const panel = button.closest(".route-execution");
          execution.apr_input = panel?.querySelector("[data-lending-apr]")?.value ?? execution.apr_input;
          execution.expiry_seconds = Number(panel?.querySelector("[data-lending-expiry]")?.value ?? execution.expiry_seconds);
        }
        void requestExecutionRoute(button.closest(".route-execution")?.querySelector("[data-sale-amount], [data-route-amount]")?.value);
      }
      if (button.dataset.routeReset !== undefined) void sendRouteInstruction("reset");
      if (button.dataset.routeApprove !== undefined) void sendRouteInstruction("approve");
      if (button.dataset.routeBuy !== undefined) void sendRouteInstruction("buy");
      if (button.dataset.routeSell !== undefined) void sendRouteInstruction("sell");
      if (button.dataset.routeCheck !== undefined) void checkRouteSubmission(button.dataset.routeCheck);
      if (button.id === "download-transactions") {
        const rows = state.sampleVisible ? [] : (state.transferHistory?.rows || []);
        download("fenorium-transactions.csv", "text/csv;charset=utf-8", U.transactionsCsv(P.queryTransfers(
          U.collapseSelfTransfers(U.allTransferRows(rows, state.sampleVisible ? null : state.history?.indexedRows,
            state.sampleVisible ? null : state.history?.values || null)), state.transferOptions)));
      }
      if (button.dataset.copyTransfer) {
        const hash = button.dataset.copyTransfer;
        if (/^0x[0-9a-f]{64}$/i.test(hash)) {
          Promise.resolve().then(() => root.navigator.clipboard.writeText(hash)).then(() => {button.textContent = "Copied";}, () => {button.textContent = "Copy failed";});
        }
      }
      if (button.dataset.pnlRange) {
        state.holdingsOptions.pnlRange = button.dataset.pnlRange;
        renderPortfolioPreservingFocus();
      }
      if (button.dataset.pnlUnit) {
        state.holdingsOptions.pnlUnit = button.dataset.pnlUnit;
        renderPortfolioPreservingFocus();
      }
      if (button.dataset.pnlSeries) {
        state.holdingsOptions.pnlSeries = button.dataset.pnlSeries;
        renderPortfolioPreservingFocus();
      }
      if (button.dataset.exposureDimension) {
        state.exposureOptions.dimension = button.dataset.exposureDimension;
        renderPortfolioPreservingFocus();
      }
      if (button.dataset.exposureUnit) {
        state.exposureOptions.unit = button.dataset.exposureUnit;
        renderPortfolioPreservingFocus();
      }
      if (button.dataset.holdingsPage) {
        state.holdingsOptions.page = Number(button.dataset.holdingsPage);
        renderPortfolioPreservingFocus();
      }
      if (button.dataset.viewWallet !== undefined) {
        // An explicit choice of a book to view lifts the post-forget scan suppression for it,
        // and leaves any simulated portfolio that was on screen.
        state.transferScanSuppressed = false;
        const leftSimulation = clearTestnetScenario();
        setViewedAccount(button.dataset.viewWallet || null);
        if (leftSimulation) { renderWalletSwitcher({screens: false}); renderOverview(); }
      }
      if (button.dataset.viewScenario !== undefined) selectTestnetScenario(button.dataset.viewScenario);
      // The way out of a simulation on every screen that shows it.
      if (button.dataset.leaveSimulation !== undefined) hideSampleBook();
      if (button.dataset.walletRemove !== undefined) removeWatchedWallet(button.dataset.walletRemove);
      if (button.dataset.walletRequest !== undefined) void requestExtensionAccounts();
      if (button.dataset.walletOpen !== undefined) {
        state.transferScanSuppressed = false;
        const leftSimulation = clearTestnetScenario();
        setViewedAccount(button.dataset.walletOpen || null);
        if (leftSimulation) { renderWalletSwitcher({screens: false}); renderOverview(); }
        activate("overview");
      }
      if (button.dataset.walletScreenAdd !== undefined) submitWalletScreenAdd();
      if (button.id === "wallet-screen-connect") void connectWallet();
      if (button.id === "settings-notional-apply") {
        const raw = String($("settings-notional")?.value || "").trim();
        const size = raw ? parseNotional(raw) : null;
        if (raw && size == null) {
          state.settingsError = `Enter a whole-dollar amount from ${formatNotional(MIN_NOTIONAL_USD)} to ${formatNotional(MAX_NOTIONAL_USD)}, or leave it empty.`;
          renderSettingsScreen();
        } else {
          state.settingsError = null;
          state.settingsDraft = null;
          updateSettings({notional_usd: size});
          if (size != null) void applyNotional(size);
        }
      }
      if (button.id === "forget-browser-data") forgetBrowserData();
      if (button.id === "refresh-portfolio") {
        state.transferScanSuppressed = false;
        // Whatever put the index placeholder on screen (no provider, or a connected wallet whose
        // portfolio read failed), Refresh retries it; a scan that can run replaces it afterwards.
        retryIndexedHistory();
        if (state.sampleVisible) void showSampleBook();
        else void refreshPortfolio();
      }
      if (button.id === "download-holdings" && state.snapshot) {
        const rows = P.portfolioRows(state.snapshot.positions, state.universe, state.snapshot.provenance?.as_of);
        download("fenorium-holdings.csv", "text/csv;charset=utf-8", U.holdingsCsv(P.queryHoldings(rows, {...state.holdingsOptions, asOf: state.snapshot.provenance?.as_of}).rows));
      }
      if (button.dataset.utility) utilityAction(button.dataset.utility);
      if (button.dataset.sort) {
        state.sort = { key: button.dataset.sort, dir: state.sort.key === button.dataset.sort ? -state.sort.dir : 1 };
        renderMarket();
      }
      if (button.dataset.page) {
        state.page = Number(button.dataset.page);
        renderMarket();
      }
      if (button.dataset.open) selectRow(Number(button.dataset.open), true);
      // Holdings persist across a universe rebuild, so they resolve by identity rather than by
      // the position an instrument happened to occupy when the row was drawn. Changing the Market
      // notional reloads and reorders the universe, and a stale index opened another instrument.
      if (button.dataset.openAddress || button.dataset.openIdentity) {
        const chainId = Number(button.dataset.openChain);
        const address = String(button.dataset.openAddress || "").toLowerCase();
        const identity = String(button.dataset.openIdentity || "");
        // A venue position has no token address: it opens the addressless universe row whose
        // rebuilt instrument id equals the one the server named it with.
        const index = (state.universe || []).findIndex((row) => Number(row?.chain_id) === chainId
          && (address ? String(row?.address || "").toLowerCase() === address
            : row?.address == null && typeof P.positionIdentity === "function" && P.positionIdentity(row) === identity));
        if (index >= 0) selectRow(index, true);
      }
      if (button.id === "inspector-close") {
        $("market-inspector").hidden = true;
        state.selected = null;
        void refreshRouteQuotes();
      }
      if (button.id === "back-to-market") activate("market");
      if (button.dataset.analyticsRetry != null && state.selected) {
        const key = instrumentAnalyticsKey(state.selected);
        if (instrumentAnalyticsCache.get(key)?.status === "error") instrumentAnalyticsCache.delete(key);
        renderSelectedAsset();
      }
      if (button.dataset.assetTab) {
        state.assetTab = normalizeAssetTab(button.dataset.assetTab);
        renderSelectedAsset();
      }
      if (button.id === "columns-toggle") {
        const menu = $("columns-menu");
        if (menu) menu.hidden = !menu.hidden;
        button.setAttribute("aria-expanded", String(menu ? !menu.hidden : false));
      }
      if (button.id === "compare-open") openCompare();
      if (button.id === "compare-close") {
        state.compareOpen = false;
        renderCompareState();
      }
      if (button.id === "export-market") exportMarket();
      if (button.dataset.category !== undefined) {
        state.filters.category = button.dataset.category;
        state.columns = new Set(defaultColumnKeys(button.dataset.category));
        state.page = 1;
        renderMarket();
      }
      if (button.id === "score-toggle") {
        state.filters.verdict = state.filters.verdict === "BID" ? "" : "BID";
        const select = $("filter-verdict");
        if (select) select.value = state.filters.verdict;
        state.page = 1;
        renderMarket();
      }
      if (button.id === "clear-filters") clearMarketFilters();
      if (button.dataset.notional) {
        const input = $("market-notional");
        if (input) input.value = button.dataset.notional;
        void applyNotional(button.dataset.notional);
      }
      if (button.id === "apply-notional") void applyNotional($("market-notional")?.value);
      if (button.id === "copy-address" && state.selected?.address) root.navigator?.clipboard?.writeText?.(state.selected.address);
      if (button.id === "connect-wallet") void connectWallet();
      if (button.id === "connect-overview-wallet") void connectWallet();
      if (button.id === "view-sample-book") void showSampleBook();
      if (button.id === "hide-sample-book") hideSampleBook();
      if (button.dataset.purchaseAction === "switch") void switchToBaseSepolia();
      if (button.dataset.purchaseAction === "purchase") void performPurchase(state.selected);
      if (button.dataset.purchaseAction === "refresh") void refreshPurchase(state.selected);
      if (button.dataset.purchaseAction === "faucet") void requestFaucet(state.selected);
      if (button.dataset.purchaseAction === "sell") void performSale(state.selected);
      if (button.dataset.executeAction === "evidence") {
        state.assetTab = "summary";
        renderSelectedAsset();
        const provenance = $("asset-provenance");
        if (provenance) provenance.open = true;
      }
      if (button.dataset.executeAction === "export") {
        download("fenorium-decision-bundle.json", "application/json", JSON.stringify({ schema_version: "DecisionBundle/v2", status: "UNANCHORED", instrument: state.selected, execution_enabled: false, fees_enabled: false }, null, 2));
      }
      if (button.id === "assistant-toggle") {
        const panel = $("assistant-panel");
        if (panel) panel.hidden = !panel.hidden;
        if (panel && !panel.hidden) { assistantContextLine(); $("assistant-question")?.focus?.(); void loadAssistantStatus(); }
        button.setAttribute("aria-expanded", String(panel ? !panel.hidden : false));
      }
      if (button.id === "assistant-close") {
        $("assistant-panel").hidden = true;
        $("assistant-toggle")?.setAttribute("aria-expanded", "false");
      }
      if (button.id === "send-assistant") void sendAssistantChat();
      if (button.id === "assistant-new") newAssistantChat();
      if (button.id === "assistant-key-save") saveAssistantKey();
      if (button.id === "assistant-key-remove") { chat.key = null; safeStorageRemove(ASSISTANT_KEY); updateAssistantVisibility(); renderSettingsScreen(); }
    });

    document.addEventListener("change", (event) => {
      const target = event.target;
      if (target.dataset.lendingExpiry !== undefined && execution?.kind === "lending") {
        execution.expiry_seconds = Number(target.value);
        invalidateLendingTerms();
        renderRouteQuotes();
      }
      if (target.dataset.saleReceive !== undefined && execution?.kind === "sale") {
        const option = execution.receive_options.find(item => item.address.toLowerCase() === target.value.toLowerCase());
        if (!option) return;
        execution.receive = {...option};
        execution.request = (execution.request || 0) + 1;
        execution.view = null; execution.data = null; execution.signable = false;
        execution.needs_approval = false; execution.needs_reset = false; execution.phase = "IDLE";
        execution.error = null;
        renderRouteQuotes();
      }
      if (target.dataset.transferControl) {
        state.transferOptions[target.dataset.transferControl] = target.value;
        renderPortfolioPreservingFocus();
      }
      if (target.dataset.holdingsControl) {
        state.holdingsOptions[target.dataset.holdingsControl] = target.dataset.holdingsControl === "pageSize" ? Number(target.value) : target.value;
        state.holdingsOptions.page = 1;
        renderPortfolioPreservingFocus();
      }
      if (target.dataset.compare) toggleCompare(target.dataset.compare, target.checked);
      if (target.dataset.column) {
        target.checked ? state.columns.add(target.dataset.column) : state.columns.delete(target.dataset.column);
        renderMarket();
      }
      if (target.dataset.columnsAll !== undefined) {
        // The attribute carries no value, so its dataset entry is the empty string: a truth test
        // on it never ran this branch, and "Select all" toggled nothing (owner 2026-10-20).
        // Unselecting all keeps the Instrument column, so the rows stay identifiable.
        if (target.checked) columns.forEach(([key]) => state.columns.add(key));
        else state.columns = new Set(["instrument"]);
        document.querySelectorAll("[data-column]").forEach((input) => { input.checked = state.columns.has(input.dataset?.column); });
        renderMarket();
      }
      if (target.id.startsWith("filter-")) {
        state.filters[target.id.replace("filter-", "")] = target.value;
        state.page = 1;
        renderMarket();
      }
      if (target.dataset?.walletLabel !== undefined) relabelWatchedWallet(target.dataset.walletLabel, target.value);
      if (target.id === "settings-notional") state.settingsDraft = String(target.value ?? "");
      if (target.id === "settings-score-only") {
        updateSettings({score_only: target.checked === true});
        state.filters.verdict = target.checked ? "BID" : "";
        syncVerdictSelect();
        renderMarket();
      }
      if (target.id === "global-chain") {
        const chainId = Number(target.value);
        if (Number.isInteger(chainId) && chainId > 0 && chainId !== state.wallet.chainId) void switchWalletChain(chainId);
      }
      if (target.id === "purchase-amount") {
        if (state.selected?.is_demo) {
          setPurchaseAmount(state.selected, target.value);
          void refreshPurchase(state.selected);
        }
      }
    });

    document.addEventListener("input", (event) => {
      if (event.target.id === "settings-notional") state.settingsDraft = String(event.target.value ?? "");
      const apr = event.target.closest?.("[data-lending-apr]");
      if (apr && execution?.kind === "lending") {
        const inputs = Array.from(document.querySelectorAll("[data-lending-apr]")), index = inputs.indexOf(apr);
        const start = apr.selectionStart, end = apr.selectionEnd;
        execution.apr_input = apr.value;
        invalidateLendingTerms();
        renderRouteQuotes();
        const replacement = document.querySelectorAll("[data-lending-apr]")[index];
        replacement?.focus?.(); replacement?.setSelectionRange?.(start, end);
        return;
      }
      const input = event.target.closest?.("[data-route-amount]");
      if (input && execution) {
        const index = Array.from(document.querySelectorAll("[data-route-amount]")).indexOf(input);
        const start = input.selectionStart, end = input.selectionEnd;
        execution.amount_input = input.value;
        execution.amount_changed = true;
        execution.request = (execution.request || 0) + 1;
        execution.signable = false;
        execution.phase = "IDLE";
        execution.error = "Amount changed; request a new route before signing.";
        renderRouteQuotes();
        const replacement = document.querySelectorAll("[data-route-amount]")[index];
        replacement?.focus?.();
        replacement?.setSelectionRange?.(start, end);
        return;
      }
      const saleInput = event.target.closest?.("[data-sale-amount]");
      if (saleInput && execution?.kind === "sale") {
        const index = Array.from(document.querySelectorAll("[data-sale-amount]")).indexOf(saleInput);
        const start = saleInput.selectionStart, end = saleInput.selectionEnd;
        execution.amount_input = saleInput.value;
        execution.amount_changed = true;
        execution.view = null; execution.data = null;
        execution.request = (execution.request || 0) + 1;
        execution.signable = false; execution.phase = "IDLE";
        execution.error = "Amount changed; request a new sale route before signing.";
        renderRouteQuotes();
        const replacement = document.querySelectorAll("[data-sale-amount]")[index];
        replacement?.focus?.(); replacement?.setSelectionRange?.(start, end);
        return;
      }
      const target = event.target;
      if (target.id === "holdings-search") {
        const start = target.selectionStart;
        state.holdingsOptions.search = target.value;
        state.holdingsOptions.page = 1;
        renderOverview();
        const input = $("holdings-search");
        input?.focus?.();
        input?.setSelectionRange?.(start, start);
      }
      if (target.id === "market-search") {
        state.filters.search = target.value.trim().toLowerCase();
        state.page = 1;
        renderMarket();
      }
      if (target.id === "global-search") {
        $("market-search").value = target.value;
        state.filters.search = target.value.trim().toLowerCase();
        state.page = 1;
        activate("market");
        renderMarket();
      }
      if (target.id === "purchase-amount" && state.selected?.is_demo) {
        setPurchaseAmount(state.selected, target.value);
      }
      if (target.id === "market-notional") scheduleNotional(target.value);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("assistant-panel") && !$("assistant-panel").hidden) {
        $("assistant-panel").hidden = true; $("assistant-toggle")?.setAttribute("aria-expanded", "false"); $("assistant-toggle")?.focus?.(); return;
      }
      // Enter that confirms an input-method candidate (Chinese, Japanese, Korean) is not a send.
      if (event.target?.id === "assistant-question" && event.key === "Enter" && !event.shiftKey
        && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault(); if (!chat.controller) void sendAssistantChat(); return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        $("global-search")?.focus?.();
      }
      // Only the row itself: a checkbox or button inside it keeps its own keys (Space toggles the compare box).
      const row = event.target?.closest?.("tr[data-row]");
      const action = row && event.target === row && !event.altKey && !event.metaKey && !event.ctrlKey
        ? rowKeyAction(event.key) : null;
      if (action) {
        event.preventDefault();
        if (action === "next" || action === "previous") {
          const sibling = action === "next" ? row.nextElementSibling : row.previousElementSibling;
          if (sibling?.dataset?.row !== undefined) {
            state.focusRow = Number(sibling.dataset.row);
            selectRow(state.focusRow, false);
          }
        } else if (action === "select") {
          state.focusRow = Number(row.dataset.row);
          selectRow(state.focusRow, false);
        } else if (state.selected) {
          const inspector = $("market-inspector");
          if (inspector) inspector.hidden = true;
          state.selected = null;
          state.focusRow = Number(row.dataset.row);
          renderMarket();
        }
        return;
      }
      if (event.key === "Enter" && event.target?.id === "market-notional") {
        event.preventDefault();
        void applyNotional(event.target.value);
      }
    });
  }

  async function init() {
    const testnetModule = root.FenoriumTestnet;
    if (testnetModule) {
      // Close every wallet-backed path while workspace configuration and fixture data load.
      // Event handlers are attached only after this resolves, so no early click can race into
      // normal wallet hydration or execution.
      state.testnetWorkspace = { enabled: true, mode: "blocked", error: "Loading Fenorium testnet workspace. No wallet or network requests have started." };
      state.provider = null;
    }
    let testnet = null;
    try {
      testnet = await testnetModule?.initialize?.();
    } catch (_) {
      if (testnetModule) testnet = { enabled: true, mode: "blocked", error: "Fenorium testnet workspace could not initialize. No wallet or network requests were started." };
    }
    state.testnetWorkspace = testnet?.enabled === true ? testnet : null;
    P.registerDeclaredSchedules(isTestnetReady() ? testnetWorkspace()?.catalog?.instruments || [] : []);
    bindEvents();
    // Before the first paint: what this browser remembers shapes the market's default filter and
    // the Settings form, and a form painted empty first was mistaken for a draft.
    loadSettings();
    if (state.settings.score_only) state.filters.verdict = "BID";
    loadWatchedWallets();
    renderWallet();
    const updateClock = () => {
      const clock = $("terminal-clock");
      if (!clock) return;
      const now = new Date().toISOString();
      clock.textContent = `${now.slice(0, 16).replace("T", " ")} UTC`;
      clock.dateTime = now;
    };
    updateClock();
    if (root.setInterval) root.setInterval(updateClock, 30000);

    if (isTestnetBlocked()) {
      state.provider = null;
      state.wallet.message = state.testnetWorkspace.error || "Testnet workspace is unavailable. No wallet or network requests were started.";
      state.universeError = { message: state.wallet.message, reason_code: "TESTNET_WORKSPACE_UNAVAILABLE" };
      mountTestnetToolbar();
      void loadAssistantStatus();
      root.FenoriumCharts?.bindTooltips?.(document);
      void renderBuildId();
      renderWalletSwitcher();
      renderOverview();
      setupMarket();
      syncVerdictSelect();
      const initial = root.location?.hash?.slice(1);
      activate(initial === "activity" ? initial : ["overview", "market", "wallet", "settings"].includes(initial) ? initial : "overview");
      return;
    }

    // Choose the portfolio source before starting any connected-chain venue reads.
    portfolioInitializing = true;
    // The checked-in universe intentionally retains historical ratings. Initial rendering uses
    // the sized endpoint so every displayed rating is recomputed under one current method.
    // Config first, and awaited. The universe envelope is checked against what this build was
    // configured to accept, and that check runs inside loadUniverse as its response arrives --
    // so racing the two meant a server with execution on was rejected by a client that had not
    // read its own config yet, leaving the market in an error state nothing retried.
    const config = await Promise.allSettled([loadConfig()]).then(([result]) => result);
    state.config = config.status === "fulfilled" ? config.value : null;
    const walletPromise = hydrateWallet();
    describeWalletAgain();
    const [universe] = await Promise.allSettled([loadUniverse(DEFAULT_NOTIONAL_USD)]);
    if (universe.status === "fulfilled") {
      const shippedSize = parseNotional(universe.value.metadata.size_usd ?? universe.value.metadata.assumptions?.size_usd) || DEFAULT_NOTIONAL_USD;
      applyUniverseRows(universe.value.instruments, shippedSize);
      applyDeploymentState(universe.value);
      state.metadata = universe.value.metadata;
      state.notionalSize = shippedSize;
      state.requestedNotionalSize = shippedSize;
    } else {
      state.universeError = { message: universe.reason?.message || String(universe.reason), reason_code: universe.reason?.reason_code || "UNIVERSE_REQUEST_FAILED" };
    }
    void loadAssistantStatus();
    root.FenoriumCharts?.bindTooltips?.(document);
    void renderBuildId();
    renderWalletSwitcher();
    renderOverview();
    setupMarket();
    syncVerdictSelect();
    mountTestnetToolbar();
    if (state.settings.notional_usd != null && state.settings.notional_usd !== state.notionalSize) void applyNotional(state.settings.notional_usd);
    await walletPromise;
    portfolioInitializing = false;
    await refreshPortfolio();
    const initial = root.location?.hash?.slice(1);
    activate(initial === "activity" ? initial : ["overview", "market", "wallet", "settings"].includes(initial) ? initial : "market");
  }

  // Which build this browser is actually running. A tab left open across a deploy keeps its old
  // JavaScript however the server is configured, and without this there is no way to tell that
  // apart from a bug in the current one -- which cost several rounds of chasing the wrong thing.
  // Absent in local development, where it stays blank rather than inventing a version.
  async function renderBuildId() {
    const element = $("build-id");
    if (!element) return null;
    try {
      const response = await fetch("build.json", {cache: "no-store"});
      if (!response.ok) return null;
      const build = await response.json();
      const commit = String(build?.commit || "").slice(0, 12);
      if (!/^[0-9a-f]{7,12}$/.test(commit)) return null;
      element.textContent = `Build ${commit}`;
      element.title = `Deployed ${build?.deployed_at || "UNKNOWN"}. Reload if this is older than a change you are looking for.`;
      return commit;
    } catch (_) {
      return null;
    }
  }

  function getWalletState() {
    return { ...state.wallet };
  }

  function getState() {
    return {
      view: state.view,
      testnetScenarioId: state.testnetScenarioId,
      testnetSimulation: scenarioShowing(),
      selected: state.selected,
      wallet: getWalletState(),
      filters: { ...state.filters },
      sampleVisible: state.sampleVisible,
      compareSelection: [...state.compareSelection],
      notionalSize: state.notionalSize,
      requestedNotionalSize: state.requestedNotionalSize,
      notionalPending: state.notionalPending,
      notionalRequestGeneration: state.notionalRequestGeneration,
      purchaseByKey: JSON.parse(JSON.stringify(state.purchaseByKey)),
      portfolioWallet: portfolioRecord()?.snapshot?.account || null,
      portfolioLoading: portfolioRecord()?.loading === true,
      portfolioError: portfolioRecord()?.error || null,
    };
  }

  const App = {
    rowKeyAction,
    switchWalletChain,
    supportedWalletChains,
    viewedWallet,
    setViewedAccount,
    addWatchedWallet,
    removeWatchedWallet,
    relabelWatchedWallet,
    loadWatchedWallets,
    requestExtensionAccounts,
    loadSettings,
    updateSettings,
    forgetBrowserData,
    renderWalletScreen,
    renderSettingsScreen,
    PAGE_SIZE,
    BASE_SEPOLIA,
    DEMO_INSTRUMENTS,
    demoInstruments,
    demoTicker,
    withDemoTicker,
    STATUS,
    SELECTORS,
    loadUniverse,
    applyNotional,
    parseNotional,
    validateUniverseEnvelope,
    loadConfig,
    validateConfig,
    normalized,
    maturityBucket,
    defaultColumnKeys,
    marketPopulation,
    normalizeAssetTab,
    ensureInstrumentAnalytics,
    columnCoverage,
    marketCell: cell,
    marketPage: (rows, page) => rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    compareKey,
    toggleCompare,
    compareRows: (rows, keys) => {
      const selected = new Set(keys || []);
      return rows.map(normalized).filter((row) => selected.has(row._key)).sort((left, right) => left._key.localeCompare(right._key));
    },
    deploymentInfo,
    deploymentEndpoints,
    deploymentStatus,
    selectorFor,
    encodeFunctionCall,
    encodeUint256,
    encodeAddress,
    encodeBytes32,
    encodeBalanceOf,
    encodeAllowance,
    encodeApprove,
    encodeQuote,
    encodePurchase,
    encodeSell,
    encodeIsVerified,
    encodeMint,
    decodeUint256,
    decodeAddress,
    decodeBool,
    decodeError,
    parseUnits,
    formatUnits,
    transactionExplorerUrl,
    waitForReceipt,
    ethCall,
    readUint,
    readDemoOnChain,
    safeStorageGet,
    safeStorageSet,
    safeStorageRemove,
    setInjectedProvider,
    renderBuildId,
    ensureTransferScan,
    readTransferCache,
    writeTransferCache,
    readWalletSnapshot,
    hydrateWallet,
    connectWallet,
    switchToBaseSepolia,
    handleAccountsChanged,
    handleChainChanged,
    getWalletState,
    getState,
    showSampleBook,
    hideSampleBook,
    selectTestnetScenario,
    clearTestnetScenario,
    buildAssistantFacts,
    buildAssistantEnvelope,
    assistantAssetSegment,
    assistantReasonCode,
    assistantInstrumentFacts,
    assistantRequestBody, buildChatContext, loadAssistantStatus, saveAssistantKey, assistantError, sendAssistantChat, stopAssistantChat, newAssistantChat,
    setPurchaseAmount,
    refreshPurchase,
    performPurchase,
    performSale,
    requestFaucet,
    readTransferHistory,
    refreshTransfers,
    readPortfolioObservations,
    postPortfolioAnalytics,
    readLivePriceObservations,
    readRouteQuotes,
    readRoutePreviews,
    refreshRouteQuotes,
    renderRouteQuotes,
    portfolioPriceMaxAgeSeconds,
    refreshPortfolio,
    readServerPortfolio,
  };

  root.FenoriumApp = App;
  if (root.addEventListener) root.addEventListener("DOMContentLoaded", init);
}(typeof globalThis !== "undefined" ? globalThis : this));
