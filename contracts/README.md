# Par vault contracts

Par is a chain-agnostic ERC-4626 vault whose off-chain allocator proposes trades and whose on-chain contracts enforce policy. The deployment script targets Base Sepolia (chain 84532), but the contracts contain no chain-specific venue assumptions.

## Architecture

- `ParVault` holds mock USDC, issues 12-decimal shares over the 6-decimal asset, and values NAV as idle USDC plus every registered adapter's live `positionValue()`.
- `RiskGuard` is called before every allocation. It rejects non-allowlisted assets, asset/job cap breaches, reserve-floor breaches, stale or invalid oracle observations, incidents, and the global circuit breaker with typed errors.
- `PendleAdapter` and `TermMaxAdapter` call mocks shaped like the real Pendle token-to-PT router path and TermMax FT/XT order path. Their fixed-rate positions accrete toward par. Early sales incur non-zero slippage that falls with time-to-maturity. `ReserveAdapter` is the one-to-one reserve path.
- Governance controls whitelist, adapters, policy, incident clearing, and breaker clearing. The allocator can allocate, divest, redeem matured paper, and sign immediate-exit unwind quotes. The guardian can only trip the circuit breaker. The sentinel can only raise asset incidents.

The deposit whitelist is enforced through `maxDeposit` and `maxMint`, so rejected deposits use the standard OpenZeppelin ERC-4626 errors. The six-decimal virtual-share offset makes first-depositor donation attacks economically prohibitive. Standard ERC-4626 withdrawal/redeem entry points advertise zero capacity so exits cannot bypass the two explicit doors.

## Two exit doors

Scheduled exits transfer shares into a strict FIFO queue. `claimScheduledExit` burns the locked shares and pays their NAV at claim time only when idle reserve can cover the claim. It has no fee or penalty, and the circuit breaker does not block it.

Immediate exits use an EIP-712 quote signed by an address holding `ALLOCATOR_ROLE`. The quote binds the owner, shares, unwind cost, deadline, adapter, adapter calldata, per-owner nonce, chain, and vault. The vault checks quote expiry and the governance-set cost ceiling, burns shares, performs any required adapter unwind, and requires the quoted cost to cover the measured position-value loss. Payout is `floor(NAV(shares)) - unwindCostAssets`; the floor and any excess quote remain in the vault, so rounding always favors remaining holders.

## Brake-only rule

`SENTINEL_ROLE` can call only `raiseIncident(asset)`, which makes RiskGuard reject new bids for that asset. Only `DEFAULT_ADMIN_ROLE` can clear an incident. The sentinel cannot allocate, clear an incident, clear the circuit breaker, or alter policy.

## Build and test

Foundry 1.7.1, forge-std 1.11.0, OpenZeppelin 5.4.0, and Solidity 0.8.28 are pinned by the repository.

```bash
export PATH="$HOME/.foundry/bin:$PATH"
forge fmt --check
forge build
forge test
FOUNDRY_PROFILE=ci forge test
```

`script/Deploy.s.sol` wires a mock Base Sepolia deployment but has not been executed. It reads `PRIVATE_KEY`, `PAR_GOVERNANCE`, `PAR_ALLOCATOR`, `PAR_GUARDIAN`, and `PAR_SENTINEL` from the environment. Broadcasting is intentionally outside this deliverable.

## Base Sepolia demo purchases

`DemoInstrumentSale` is a testnet-only payment leg for the existing six-decimal mGT and mPT demo instruments. A purchase transfers the exact quoted MockUSDC amount directly from the buyer to an immutable proceeds recipient, then mints the instrument atomically. The gated sale checks the mGT verification registry before payment. The contract has no buyer-token withdrawal, custody, upgrade, or administrative transfer path.

`script/DeployDemoSales.s.sol` deploys one public-mint `MockUSDC` and two sales, grants each sale the corresponding instrument's `MINTER_ROLE`, and refuses any chain other than Base Sepolia. It reads `PRIVATE_KEY`, with optional `DEMO_SALE_PROCEEDS_RECIPIENT` and `DEMO_SALE_PRICE_MICROS` overrides. The default demo price is 1,000,000 MockUSDC micros per whole instrument and is not a market-price claim. Deployment and verification remain owner-run steps.

### Demo eligibility credential path

The deployed mGT contract's `trustedAttester` is
`0xDa909368646277cc22842Bd5dBb83E5923858411`. The owner controls that key. It is also a
high-authority key outside this demo, so it must never be imported into a browser, handed to a
tester, stored in a fixture, or exposed in logs. Issuance is an owner-run operation today; there is
no self-service tester issuance path.

The registered EAS schema UID is
`0xc6c30d8537afcc25aca394ef6b70c65d8378ae3a9416a9944c2f75e6feb71823`, with schema:

```text
bool verified,string jurisdiction,string tier
```

`script/IssueDemoAttestation.s.sol` takes the recipient from
`DEMO_ATTESTATION_RECIPIENT`, verifies that `PRIVATE_KEY` resolves to the deployed
`trustedAttester`, and issues this exact revocable, non-expiring EAS request:

```text
attest((bytes32,(address,uint64,bool,bytes32,bytes,uint256)))
  schema,
  (recipient, 0, true, bytes32(0), abi.encode(true, "US", "retail"), 0)
```

The owner runs it from `contracts/` after loading the trusted-attester key into `PRIVATE_KEY`:

```bash
DEMO_ATTESTATION_RECIPIENT=0x... \
forge script script/IssueDemoAttestation.s.sol:IssueDemoAttestation \
  --rpc-url base_sepolia --broadcast
```

The returned UID is not active merely because it was issued. The recipient wallet must make its
own transaction to the mGT contract at `0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54`:

```text
registerCredential(bytes32 uid)
```

That transaction must come from the attestation recipient. The contract checks the schema,
trusted attester, recipient, revocation status, and expiration before recording it. The tester
then confirms `isVerified(testerAddress) == true` before attempting the gated purchase. A wallet
extension can submit `registerCredential` through a contract-write interface; the live proof
harness in `proofs/live_wallet/` submits the same call through its disposable-wallet EIP-1193
provider. The Fenorium purchase card currently refreshes and displays the resulting eligibility,
but it does not issue credentials.

A self-service faucet or delegated testnet issuer would remove the owner bottleneck, but it would
also change the trust model. That is a product decision and is not implemented here.
