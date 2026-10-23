(function (root) {
  "use strict";

  const freezeGroup = (entries) => Object.freeze(
    Object.fromEntries(
      Object.entries(entries).map(([key, value]) => [key, Object.freeze(value)]),
    ),
  );

  root.FenoriumIconCatalog = Object.freeze({
    chains: freezeGroup({
      Aptos: { label: "Aptos", src: "assets/icons/chain-aptos.webp" },
      Arbitrum: { label: "Arbitrum", src: "assets/icons/chain-arbitrum.webp" },
      Avalanche: { label: "Avalanche", src: "assets/icons/chain-avalanche.webp" },
      BSC: { label: "BNB Smart Chain", src: "assets/icons/chain-bsc.webp" },
      Base: { label: "Base", src: "assets/icons/chain-base.webp" },
      "Base Sepolia": { label: "Base Sepolia", src: "assets/icons/chain-base.webp" },
      Ethereum: { label: "Ethereum", src: "assets/icons/chain-ethereum.webp" },
      Etherlink: { label: "Etherlink", src: "assets/icons/chain-etherlink.webp" },
      HyperEVM: { label: "HyperEVM", src: "assets/icons/chain-hyperevm.webp" },
      Mantle: { label: "Mantle", src: "assets/icons/chain-mantle.webp" },
      Monad: { label: "Monad", src: "assets/icons/chain-monad.webp" },
      Noble: { label: "Noble", src: "assets/icons/chain-noble.webp" },
      "OP Mainnet": { label: "OP Mainnet", src: "assets/icons/chain-optimism.webp" },
      Osmosis: { label: "Osmosis", src: "assets/icons/chain-osmosis.webp" },
      Polygon: { label: "Polygon", src: "assets/icons/chain-polygon.webp" },
      Sei: { label: "Sei", src: "assets/icons/chain-sei.webp" },
      Solana: { label: "Solana", src: "assets/icons/chain-solana.webp" },
      Stellar: { label: "Stellar", src: "assets/icons/chain-stellar.webp" },
      Sui: { label: "Sui", src: "assets/icons/chain-sui.webp" },
      XRPL: { label: "XRP Ledger", src: "assets/icons/chain-xrpl.webp" },
    }),
    venues: freezeGroup({
      Ethena: { label: "Ethena", src: "assets/icons/protocol-ethena-usde.webp" },
      Exactly: { label: "Exactly", src: "assets/icons/protocol-exactly.webp" },
      Maple: { label: "Maple", src: "assets/icons/protocol-maple.webp" },
      Midas: { label: "Midas", src: "assets/icons/protocol-midas-rwa.webp" },
      Morpho: { label: "Morpho", src: "assets/icons/protocol-morpho-blue.webp" },
      Mountain: { label: "Mountain", src: "assets/icons/protocol-mountain-protocol.webp" },
      Ondo: { label: "Ondo", src: "assets/icons/protocol-ondo-yield-assets.webp" },
      Pendle: { label: "Pendle", src: "assets/icons/protocol-pendle.webp" },
      Spectra: { label: "Spectra", src: "assets/icons/protocol-spectra.webp" },
      Tenor: { label: "Tenor", src: "assets/icons/protocol-tenor.webp" },
      "Term Finance": { label: "Term Finance", src: "assets/icons/protocol-term-finance.webp" },
      TermMax: { label: "TermMax", src: "assets/icons/protocol-termmax.webp" },
      Usual: { label: "Usual", src: "assets/icons/protocol-usual-usd0.webp" },
      "blackrock-buidl": { label: "blackrock-buidl", src: "assets/icons/protocol-blackrock-buidl.webp" },
      "matrixdock-stbt": { label: "matrixdock-stbt", src: "assets/icons/protocol-matrixdock-stbt.webp" },
      "midas-rwa": { label: "midas-rwa", src: "assets/icons/protocol-midas-rwa.webp" },
      "ondo-yield-assets": { label: "ondo-yield-assets", src: "assets/icons/protocol-ondo-yield-assets.webp" },
      "openeden-tbill": { label: "openeden-tbill", src: "assets/icons/protocol-openeden-tbill.webp" },
      "openeden-usdo": { label: "openeden-usdo", src: "assets/icons/protocol-openeden-usdo.webp" },
      "usual-usd0": { label: "usual-usd0", src: "assets/icons/protocol-usual-usd0.webp" },
    }),
    tokens: freezeGroup({
      BUIDL: { label: "BUIDL", src: "assets/icons/protocol-blackrock-buidl.webp" },
      OUSG: { label: "OUSG", src: "assets/icons/protocol-ondo-yield-assets.webp" },
      STBT: { label: "STBT", src: "assets/icons/protocol-matrixdock-stbt.webp" },
      TBILL: { label: "TBILL", src: "assets/icons/protocol-openeden-tbill.webp" },
      USD0: { label: "USD0", src: "assets/icons/protocol-usual-usd0.webp" },
      USDC: { label: "USDC", src: "assets/icons/token-usdc-tile.png" },
      USDO: { label: "USDO", src: "assets/icons/protocol-openeden-usdo.webp" },
      USDY: { label: "USDY", src: "assets/icons/protocol-ondo-yield-assets.webp" },
      USDe: { label: "USDe", src: "assets/icons/protocol-ethena-usde-tile.png" },
      mBASIS: { label: "mBASIS", src: "assets/icons/protocol-midas-rwa.webp" },
      // Every m-prefixed row is a Par-issued Base Sepolia mock, so it carries Par's mark.
      // mUSDC deliberately does not get Circle's: it is a mock, and dressing it in the real
      // issuer's brand is the misattribution this catalogue exists to prevent.
      mFT: { label: "mFT", src: "assets/brand/fenorium-mark-tile.svg" },
      mGT: { label: "mGT", src: "assets/brand/fenorium-mark-tile.svg" },
      mPT: { label: "mPT", src: "assets/brand/fenorium-mark-tile.svg" },
      mUSDC: { label: "mUSDC", src: "assets/brand/fenorium-mark-tile.svg" },
      mVY: { label: "mVY", src: "assets/brand/fenorium-mark-tile.svg" },
      mTBILL: { label: "mTBILL", src: "assets/icons/protocol-midas-rwa.webp" },
      sUSDe: { label: "sUSDe", src: "assets/icons/protocol-ethena-usde-tile.png" },
    }),
    // A holding often carries no mark of its own but names an issuer whose mark we ship. The
    // issuer is the identity the row already asserts, so it is the only permitted fallback:
    // never a lookalike ticker, never a guess.
    issuers: freezeGroup({
      BlackRock: { label: "BlackRock", src: "assets/icons/protocol-blackrock-buidl.webp" },
      Ethena: { label: "Ethena", src: "assets/icons/protocol-ethena-usde-tile.png" },
      Matrixdock: { label: "Matrixdock", src: "assets/icons/protocol-matrixdock-stbt.webp" },
      Midas: { label: "Midas", src: "assets/icons/protocol-midas-rwa.webp" },
      Mountain: { label: "Mountain", src: "assets/icons/protocol-mountain-protocol-tile.png" },
      Ondo: { label: "Ondo", src: "assets/icons/protocol-ondo-yield-assets.webp" },
      OpenEden: { label: "OpenEden", src: "assets/icons/protocol-openeden-tbill.webp" },
      Pendle: { label: "Pendle", src: "assets/icons/protocol-pendle.webp" },
      Usual: { label: "Usual", src: "assets/icons/protocol-usual-usd0.webp" },
    }),
    venueLabels: Object.freeze({
      "blackrock-buidl": "BlackRock BUIDL",
      "matrixdock-stbt": "Matrixdock STBT",
      "midas-rwa": "Midas RWA",
      "ondo-yield-assets": "Ondo Yield Assets",
      "openeden-tbill": "OpenEden TBILL",
      "openeden-usdo": "OpenEden USDO",
      "usual-usd0": "Usual USD0",
    }),
  });
}(typeof globalThis !== "undefined" ? globalThis : this));
