/* Static, opt-in testnet workspace: the deployed catalog and the simulated portfolios.
   Loaded only by the testnet image, before app.js. Every wallet and transaction path stays in
   app.js; a simulation is a choice on the Portfolio screen, never a mode of the whole app. */
(function (root) {
  "use strict";

  const WORKSPACE_PATH = "testnet-workspace.json";
  const SCENARIOS_PATH = "data/testnet-scenarios.json";
  const CATALOG_PATH = "data/testnet-catalog.json";
  const BASE_SEPOLIA_CHAIN_ID = 84532;
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

  async function json(path) {
    const response = await root.fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`${path} unavailable (HTTP ${response.status})`);
    return response.json();
  }

  function validScenarioData(data) {
    if (data?.schema_version !== "fenorium/testnet-scenarios/1"
      || !Array.isArray(data.scenarios) || !data.scenarios.length
      || !Array.isArray(data.universe?.instruments) || !data.universe.instruments.length) return false;
    const ids = new Set();
    for (const scenario of data.scenarios) {
      if (!scenario?.id || ids.has(scenario.id) || !scenario.snapshot || !Array.isArray(scenario.snapshot.positions)) return false;
      ids.add(scenario.id);
    }
    return true;
  }

  function validCatalog(data) {
    const address = (value) => typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value);
    if (data?.schema_version !== "fenorium/testnet-catalog-deployment/1"
      || data.chain_id !== BASE_SEPOLIA_CHAIN_ID
      || !address(data.payment_token) || !address(data.gate_registry)
      || data.quote_signature !== "quote(uint256)"
      || data.purchase_signature !== "purchase(uint256,uint256)"
      || data.sell_quote_signature !== "sellQuote(uint256)"
      || data.sell_signature !== "sell(uint256,uint256)"
      || !Array.isArray(data.instruments)) return false;
    const symbols = new Set(["mGT", "mPT"]), addresses = new Set();
    return data.instruments.every((row) => {
      if (!row || typeof row.symbol !== "string" || !row.symbol.trim() || symbols.has(row.symbol)
        || !address(row.instrument) || !address(row.desk) || typeof row.gated !== "boolean"
        || !Number.isInteger(row.rate_bps) || row.rate_bps < 0 || row.rate_bps >= 10000
        || typeof row.base_price_usd_fixed !== "string" || !/^\d+$/.test(row.base_price_usd_fixed)
        || typeof row.accrual_start !== "string" || !Number.isFinite(Date.parse(row.accrual_start))
        || typeof row.accrual_end !== "string" || !Number.isFinite(Date.parse(row.accrual_end))
        || Date.parse(row.accrual_end) <= Date.parse(row.accrual_start)
        || !("maturity" in row)
        || (row.maturity !== null && (root.FenoriumPortfolio.maturityDateMs(row.maturity) === null
          || row.accrual_end !== `${row.maturity}T00:00:00+00:00`))
        || addresses.has(row.instrument.toLowerCase())) return false;
      symbols.add(row.symbol);
      addresses.add(row.instrument.toLowerCase());
      return true;
    });
  }

  // Each optional file can fail independently; the configured pair stays available.
  async function initialize() {
    let workspace;
    try {
      workspace = await json(WORKSPACE_PATH);
      if (workspace?.enabled === false) return { enabled: false };
      if (workspace?.enabled !== true || workspace.chain_id !== BASE_SEPOLIA_CHAIN_ID
        || (workspace.local !== undefined && (!workspace.local || typeof workspace.local !== "object"
          || Array.isArray(workspace.local) || typeof workspace.local.node !== "string"
          || !workspace.local.node.trim() || workspace.local.node.length > 40
          || /[\x00-\x1f\x7f]/.test(workspace.local.node)))) {
        return { enabled: true, mode: "blocked", error: "Testnet workspace configuration is missing or invalid." };
      }
    } catch (_) {
      return { enabled: true, mode: "blocked", error: "Testnet workspace configuration is unavailable. No wallet or network requests were started." };
    }
    let data = null, catalog = null;
    const warnings = [];
    try {
      data = await json(SCENARIOS_PATH);
      if (!validScenarioData(data)) throw new Error("Testnet scenario data is incomplete");
    } catch (_) {
      data = null;
      warnings.push("The simulated portfolios are unavailable; only the on-chain Base Sepolia instruments are shown.");
    }
    try {
      catalog = await json(CATALOG_PATH);
      if (!validCatalog(catalog)) throw new Error("Testnet catalog deployment is invalid");
    } catch (_) {
      catalog = null;
      warnings.push("The catalog deployment is unavailable; catalog purchases are disabled.");
    }
    return { enabled: true, workspace, local: workspace.local || null, data, catalog, mode: "ready", warning: warnings.join(" ") || null };
  }

  // One line under the freshness banner saying what this page is: which rows are on a chain,
  // and the simulated portfolios' fixture date. No controls: the simulations are chosen on the
  // Portfolio screen, and the wallet connects from the header as everywhere else.
  function mountToolbar(context, summary = {}) {
    if (!context?.enabled || !root.document) return null;
    let element = root.document.getElementById("testnet-workspace-toolbar");
    if (!element) {
      element = root.document.createElement("section");
      element.id = "testnet-workspace-toolbar";
      element.className = "testnet-toolbar";
      element.setAttribute("aria-label", "Testnet workspace");
      const banner = root.document.getElementById("universe-status");
      if (banner?.parentNode) banner.parentNode.insertBefore(element, banner.nextSibling);
      else root.document.body?.prepend(element);
    }
    const onChain = Number.isInteger(summary.onChain) ? summary.onChain : null;
    const P = root.FenoriumPortfolio;
    const maturities = [P.DEMO_PAIR_MATURITY, P.DEMO_PAIR_MATURITY, ...(context.catalog?.instruments || []).map((row) => row.maturity)];
    const matured = maturities.filter((maturity) => P.rowMatured({maturity})).length;
    const purchasable = maturities.length - matured;
    const scenarios = context.data?.scenarios?.length || 0;
    const status = context.mode === "blocked"
      ? escape(context.error || "Workspace unavailable. No network activity was started.")
      : [
        `${context.local ? `LOCAL CHAIN · ${escape(context.local.node)} presenting as Base Sepolia (chain id 84532)` : "ON-CHAIN · Base Sepolia"} · ${onChain == null ? "instruments" : `${onChain} instrument${onChain === 1 ? "" : "s"}`} · ${purchasable} can be bought with faucet mUSDC and sold back to their desks; ${context.local ? "local" : "Base Sepolia"} ETH pays gas`,
        matured ? `${matured} matured (sell-back only)` : "",
        context.data
          ? `PREVIEW · ${scenarios} simulated portfolios, snapshot ${escape(context.data.as_of || "UNKNOWN")} · not on any chain, nothing to buy`
          : "",
        escape(context.warning || ""),
      ].filter(Boolean).join(" · ");
    element.innerHTML = `<div class="testnet-toolbar-copy"><strong>Fenorium testnet</strong><span>${status}</span></div>`;
    return element;
  }

  root.FenoriumTestnet = Object.freeze({ initialize, mountToolbar });
})(typeof globalThis !== "undefined" ? globalThis : this);
