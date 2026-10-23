# Fenorium local

The Fenorium fixed-income terminal, running entirely on your own machine: a local blockchain, the
same smart contracts that are deployed on the Base Sepolia testnet, the platform that rates and
serves them, and the terminal page. Nothing here talks to a public network.

Fenorium exists in three places:

| Where | What | URL |
| --- | --- | --- |
| Mainnet | Live markets on Ethereum, Base, Arbitrum and other networks, read-only ratings | https://app.fenorium.com |
| Testnet | The demo instruments on Base Sepolia, buyable with test money | https://testnet.fenorium.com |
| Local | This repository: the testnet deployment rebuilt on a chain on your computer | http://127.0.0.1:8517 |

## What you need

- Docker with Compose v2 (Docker Desktop on macOS or Windows, Docker Engine on Linux).
- `make` (ships with macOS and Linux; on Windows use WSL).
- A browser wallet such as MetaMask, for buying and selling.
- About 3 GB of disk for the images and 2 GB of free memory while it runs; the optional Explain
  assistant needs 2 GB more disk for its model and about 2.5 GB of memory while it answers.

The first `make up` downloads the images (Foundry, Python) and compiles the contracts; later runs
reuse them.

## Start it

```bash
git clone https://github.com/ECamposSoria/fenorium_local.git
cd fenorium_local
make up
```

`make up` does, in order:

1. starts **Anvil**, Foundry's local Ethereum node, presenting as Base Sepolia (chain id 84532)
   with two-second blocks;
2. places the Ethereum Attestation Service at its Base Sepolia addresses and registers the demo
   credential schema (the same schema UID as on the testnet, because the UID is the hash of the
   schema definition);
3. deploys, from source, in the order they were deployed on Base Sepolia: the gated and
   permissionless treasury mocks (mGT, mPT), the fixed-term and variable-yield mocks (mFT, mVY),
   the mock USDC with its faucet and the two sales, the two desks that buy instruments back, then
   the 132 catalog instruments with their desks; the addresses are the same on every machine
   because the deployer is Anvil's first development account starting from nonce zero;
4. issues the demo credential to the development accounts, so the gated instruments can be bought;
5. regenerates every data file from that deployment and the chain (manifests, the page's catalog
   and simulated portfolios, the universe the platform serves, rated at every size);
6. starts the platform and serves the terminal.

When it finishes it prints the URL. `make status` shows the deployed addresses, `make logs`
follows the platform, `make down` stops everything but keeps the chain, `make reset` wipes the
chain so the next `make up` deploys again. After a reset, MetaMask still remembers the old chain's
transaction count for each account: open the account's Settings → Advanced → "Clear activity tab
data" before sending again, or the first transaction fails with a nonce error. The terminal itself
notices the reset (it reads the chain, not its own memory of it) and rereads the wallet's history.

## Use it with a wallet

The chain runs at `http://127.0.0.1:8545` with chain id **84532**. MetaMask already knows that
chain id as "Base Sepolia" and will not add a second network with it, so edit that network:
Settings → Networks → Base Sepolia → RPC URL `http://127.0.0.1:8545`. Switch it back when you are
done with the local stack.

Import one of Anvil's development accounts. They are published by Foundry, exist on every Anvil
chain with 10,000 ETH, and must never be used on a real network:

| Account | Address | Private key |
| --- | --- | --- |
| #1 | 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 | 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d |
| #2 | 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC | 0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a |

Accounts #0, #1 and #2 hold the demo credential, so they can buy the gated instruments as well as
the permissionless ones. Then, in the terminal:

1. open an instrument from the Market, connect the wallet, and use **Get mUSDC from faucet** on
   its purchase card (the mock USDC mints to anyone);
2. approve and buy; the holding appears in the Portfolio, valued at the desk's declared price;
3. sell back to the desk from the same card;
4. the **Analytics** tab shows the yield at every size, the declared price path, the cashflow
   terms and the instrument against its peers; the Portfolio's **Risk** tab shows the risk of what
   you hold.

## What is real and what is declared

The contracts are real: ERC-20 tokens, desks that quote, sell and buy back, a gated token that
checks an on-chain credential. Their economics — rates, maturities, tiers, depth — are declared
by the scenario generator, not observed in a market, and every screen says so ("DECLARED TESTNET
… not a market observation"). On mainnet the same platform rates live markets from their own
data; on the testnet and here it rates these declarations with the same engine.

The eight simulated portfolios on the Portfolio screen are fixtures: positions in the deployed
instruments, no transactions behind them.

## Explain (optional)

The **Explain** button asks a small language model to paraphrase facts the platform has already
computed; it never computes anything itself, and a grounding check rejects anything the model adds.
It runs on your machine only, as a separate container:

```bash
make assistant      # starts it at http://127.0.0.1:8787 and prints the pairing token
make model          # optional: fetches Qwen3 1.7B (about 1 GB) through the QVAC runtime
```

In the terminal, open Explain, paste the pairing token printed by `make assistant` (it is also in
`.env`, since the assistant is reachable only from this machine), select facts and ask. Without the
model fetched, or on a machine where the QVAC runtime cannot run, the assistant still answers with
the selected facts and says that no model answered.

## How this repository is built

`engine/`, `deploy/platform/`, `web/`, `scripts/`, `contracts/`, `assistant/`, `research/` and
`schemas/` are exported unchanged from the Fenorium repository at the commit recorded in
`UPSTREAM`. This repository owns `Makefile`, `docker-compose.yml`, `images/` and `chain/`. `chain/predeploys.json` records where the EAS code came from and its
hash. `make test` runs the contracts' own test suite in the Foundry image.
