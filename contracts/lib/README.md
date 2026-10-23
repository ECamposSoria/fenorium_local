# Vendored Solidity dependencies

Only the source trees and license/version metadata required for the offline Foundry build are
tracked:

- forge-std 1.11.0 — `forge-std/src`
- OpenZeppelin Contracts 5.4.0 — `openzeppelin-contracts/contracts`

The upstream test suites, audits, documentation builds, and repository automation remain local but
are ignored. Versions are recorded in each package's tracked `package.json`; Par pins Solidity
0.8.28 and the import paths in `contracts/foundry.toml` and `contracts/remappings.txt`.
