# Fenorium local

The Fenorium fixed-income terminal on your own machine: a local blockchain, the contracts deployed on
the Base Sepolia testnet, the platform that rates them, and the terminal page. Nothing here talks to
a public network.

Fenorium exists in three places:

| Where | What | URL |
| --- | --- | --- |
| Mainnet | Live markets on Ethereum, Base, Arbitrum and other networks, read-only ratings | https://app.fenorium.com |
| Testnet | The demo instruments on Base Sepolia, buyable with test money | https://testnet.fenorium.com |
| Local | This repository: the testnet deployment rebuilt on a chain on your computer | http://127.0.0.1:8517 |

## What you need

- Docker with Compose v2: Docker Desktop on macOS or Windows, Docker Engine on Linux.
- `make`: included on macOS and Linux; on Windows, use WSL.
- A browser wallet such as MetaMask, to buy and sell.
- 3 GB of disk and 2 GB of free memory for the stack.
- For the optional [chat](#chat-optional): an NVIDIA graphics card with 24 GB of memory, such as an
  RTX 3090 or 4090, 32 GB of system memory and about 25 GB more disk.

## Start it

```bash
git clone https://github.com/ECamposSoria/fenorium_local.git
cd fenorium_local
make up
```

The first run downloads the images and compiles the contracts. `make up` then:

1. starts Anvil, Foundry's local node, as Base Sepolia (chain id 84532, two-second blocks);
2. places the Ethereum Attestation Service at its Base Sepolia addresses and registers the demo
   credential schema;
3. deploys every contract from source in the testnet's order: the mGT, mPT, mFT and mVY mocks, the
   mock USDC with its faucet and sales, the buy-back desks, and the 132 catalog instruments, at the
   same addresses on every machine;
4. gives the demo credential to development accounts #0, #1 and #2;
5. rebuilds every data file from the chain and rates every instrument at every size;
6. starts the platform and prints the terminal's URL.

| Command | What it does |
| --- | --- |
| `make status` | lists the deployed addresses |
| `make logs` | follows the platform's log |
| `make down` | stops everything and keeps the chain |
| `make reset` | wipes the chain, so the next `make up` deploys again |

After `make reset`, clear MetaMask's history for each account (Settings → Advanced → Clear activity
tab data), or its first transaction fails with a nonce error.

## Use it with a wallet

The chain runs at `http://127.0.0.1:8545` with chain id **84532**. MetaMask treats that id as Base
Sepolia and will not add a second network with it, so edit the existing one: Settings → Networks →
Base Sepolia → RPC URL `http://127.0.0.1:8545`. Change it back when you are done.

Import one of Anvil's development accounts. Foundry publishes them, they hold 10,000 ETH on every
Anvil chain, and they must never be used on a real network:

| Account | Address | Private key |
| --- | --- | --- |
| #1 | 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 | 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d |
| #2 | 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC | 0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a |

Both hold the demo credential, so they can buy the gated instruments too. In the terminal:

1. open an instrument from the Market, connect the wallet, and use **Get mUSDC from faucet** on its
   purchase card;
2. approve and buy: the holding appears in the Portfolio;
3. sell it back to the desk from the same card;
4. see the yield at every size, the price path and the cashflows in the instrument's **Analytics**
   tab, and the risk of what you hold in the Portfolio's **Risk** tab.

## What is real and what is declared

The contracts are real: tokens, desks that sell and buy back, and a gated token that checks an
on-chain credential. Their rates, maturities and depth are declared by a scenario generator, and
every screen says so. On mainnet the same engine rates live markets; here it rates these
declarations. The eight simulated portfolios are fixtures with no transactions behind them.

## Chat (optional)

**Nori**, Fenorium's AI assistant, is a chat about what the terminal shows: the market as you filtered and
sorted it, the instrument you selected, and the portfolio you are viewing. It explains figures,
compares instruments and suggests ones that fit what you ask. It cannot trade, and nothing leaves
your machine.

It runs Qwen3 30B-A3B, quantized to Q4_K_M (an 18.6 GB download), on your graphics card. Only about
3 billion of its 30 billion parameters work on each word, so it should answer quickly once loaded.
Set it in `.env`:

```bash
LOCAL_ASSISTANT_MODEL=qwen3-30b-a3b
LOCAL_ASSISTANT_MEMORY=24g
```

Then:

```bash
make model GPU=1      # once: downloads the model
make assistant GPU=1  # starts the chat; the Nori button appears in the terminal
make assistant-down   # stops it
```

`GPU=1` needs an NVIDIA driver from the 580 series or newer and Docker with GPU access: Docker
Desktop with WSL2 on Windows, or the NVIDIA Container Toolkit on Linux. Docker on a Mac cannot use
the GPU. Without `GPU=1`, `make model` and `make assistant` run Qwen3 1.7B on the CPU, which shows
how the chat works but does not give reliable answers.

`LOCAL_ASSISTANT_MODEL` also takes these, all Qwen3 models quantized to 4 bits:

| Model | Quantization | Download | Needs |
|---|---|---|---|
| `qwen3-30b-a3b` (recommended) | Q4_K_M | 18.6 GB | an NVIDIA card with 24 GB |
| `qwen3-14b` | Q4_K_M | 9.0 GB | an NVIDIA card with 12 GB |
| `qwen3-8b` | Q4_K_M | 5.0 GB | an NVIDIA card with 8 GB |
| `qwen3-4b` | Q4_K_M | about 2.5 GB | 4 GB of free memory; slow on a CPU |
| `qwen3-1.7b` | 4-bit, QVAC's build | about 1 GB | any computer; a demonstration only |

**How accurate it is.** Each model was asked the same 73 questions about one screen, and every
answer was checked by hand:

| Model | Ran on | Correct answers | Time per answer |
|---|---|---|---|
| `qwen3-8b` | RTX 4070 Laptop GPU, 8 GB | 55 of 73 | about 4 s |
| `qwen3-8b`, thinking on | the same card | 61 of 73 | about 16 s |
| Gemini 3.8 Flash, for comparison | Fenorium's hosted testnet | 69 of 73 | about 3 s |

`LOCAL_ASSISTANT_THINKING=1` in `.env` lets the model reason before it answers: more correct
answers, four times slower, so use it on a graphics card only. If an answer comes back empty, ask
again.

Every figure the chat quotes should come from the screen, and any number that does not is
highlighted. Fenorium counts and ranks the rows itself and gives the model the results, but a model
can still pick the wrong row, so check its answers against the table.

## How this repository is built

`engine/`, `deploy/platform/`, `web/`, `scripts/`, `contracts/`, `assistant/`, `research/` and
`schemas/` are exported unchanged from the Fenorium repository at the commit in `UPSTREAM`. This
repository owns `Makefile`, `docker-compose.yml`, `images/` and `chain/`; `chain/predeploys.json`
records where the EAS code came from and its hash. `make test` runs the contracts' test suite.
