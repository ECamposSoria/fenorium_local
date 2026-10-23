// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";

import {ParVault} from "../src/ParVault.sol";
import {RiskGuard} from "../src/RiskGuard.sol";
import {PendleAdapter} from "../src/adapters/PendleAdapter.sol";
import {TermMaxAdapter} from "../src/adapters/TermMaxAdapter.sol";
import {ReserveAdapter} from "../src/adapters/ReserveAdapter.sol";
import {MockPendleMarket} from "../src/adapters/mocks/MockPendleMarket.sol";
import {MockTermMaxMarket} from "../src/adapters/mocks/MockTermMaxMarket.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";
import {MockInstrument} from "../src/mocks/MockInstrument.sol";
import {MockParOracle} from "../src/mocks/MockParOracle.sol";

contract Deploy is Script {
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;
    uint256 internal constant MOCK_MARKET_LIQUIDITY = 10_000_000e6;

    struct Config {
        uint256 privateKey;
        address deployer;
        address governance;
        address allocator;
        address guardian;
        address sentinel;
    }

    struct Deployment {
        MockUSDC usdc;
        MockInstrument pendlePt;
        MockInstrument termMaxFt;
        MockParOracle oracle;
        RiskGuard guard;
        ParVault vault;
        MockPendleMarket pendleMarket;
        MockTermMaxMarket termMaxMarket;
        PendleAdapter pendleAdapter;
        TermMaxAdapter termMaxAdapter;
        ReserveAdapter reserveAdapter;
    }

    error WrongChain(uint256 actualChainId);

    function run() external {
        if (block.chainid != BASE_SEPOLIA_CHAIN_ID) revert WrongChain(block.chainid);

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        Config memory config = Config({
            privateKey: privateKey,
            deployer: vm.addr(privateKey),
            governance: vm.envAddress("PAR_GOVERNANCE"),
            allocator: vm.envAddress("PAR_ALLOCATOR"),
            guardian: vm.envAddress("PAR_GUARDIAN"),
            sentinel: vm.envAddress("PAR_SENTINEL")
        });

        vm.startBroadcast(config.privateKey);
        Deployment memory deployment = _deploy(config);
        _configure(deployment, config);
        vm.stopBroadcast();
    }

    function _deploy(Config memory config) private returns (Deployment memory deployment) {
        deployment.usdc = new MockUSDC();
        deployment.pendlePt = new MockInstrument("Mock Pendle PT", "mPT");
        deployment.termMaxFt = new MockInstrument("Mock TermMax FT", "mFT");
        deployment.oracle = new MockParOracle(config.deployer);
        deployment.guard = new RiskGuard(config.deployer, config.guardian, config.sentinel);
        deployment.vault = new ParVault(
            deployment.usdc, "Par Vault Share", "PAR", deployment.guard, config.deployer, config.allocator
        );

        uint64 maturity = uint64(block.timestamp + 180 days);
        deployment.pendleMarket =
            new MockPendleMarket(deployment.usdc, address(deployment.pendlePt), maturity, 500, 300);
        deployment.termMaxMarket =
            new MockTermMaxMarket(deployment.usdc, address(deployment.termMaxFt), maturity, 500, 300);
        deployment.pendleAdapter =
            new PendleAdapter(address(deployment.vault), deployment.usdc, deployment.pendleMarket);
        deployment.termMaxAdapter =
            new TermMaxAdapter(address(deployment.vault), deployment.usdc, deployment.termMaxMarket);
        deployment.reserveAdapter = new ReserveAdapter(address(deployment.vault), deployment.usdc);
    }

    function _configure(Deployment memory deployment, Config memory config) private {
        deployment.usdc.mint(address(deployment.pendleMarket), MOCK_MARKET_LIQUIDITY);
        deployment.usdc.mint(address(deployment.termMaxMarket), MOCK_MARKET_LIQUIDITY);
        deployment.usdc.mint(config.governance, MOCK_MARKET_LIQUIDITY);

        deployment.oracle.setPrice(address(deployment.pendlePt), 1e18, block.timestamp);
        deployment.oracle.setPrice(address(deployment.termMaxFt), 1e18, block.timestamp);
        deployment.oracle.setPrice(address(deployment.usdc), 1e18, block.timestamp);

        deployment.guard.setAssetPolicy(address(deployment.pendlePt), true, 7_000, deployment.oracle, 1 days);
        deployment.guard.setAssetPolicy(address(deployment.termMaxFt), true, 7_000, deployment.oracle, 1 days);
        deployment.guard.setAssetPolicy(address(deployment.usdc), true, 10_000, deployment.oracle, 1 days);
        deployment.guard.setJobCap(uint8(ParVault.Job.LEND_FIXED), 7_000);
        deployment.guard.setJobCap(uint8(ParVault.Job.DISPERSION), 7_000);
        deployment.guard.setJobCap(uint8(ParVault.Job.CARRY), 7_000);
        deployment.guard.setJobCap(uint8(ParVault.Job.RESERVE), 10_000);
        deployment.guard.setMinimumReserveBps(2_000);

        deployment.vault.setAdapter(deployment.pendleAdapter, true);
        deployment.vault.setAdapter(deployment.termMaxAdapter, true);
        deployment.vault.setAdapter(deployment.reserveAdapter, true);
        deployment.vault.setMaximumUnwindCostBps(1_000);
        deployment.vault.setWhitelist(config.governance, true);

        _handoffAdministration(deployment, config);
    }

    function _handoffAdministration(Deployment memory deployment, Config memory config) private {
        if (config.governance == config.deployer) return;
        deployment.guard.grantRole(deployment.guard.DEFAULT_ADMIN_ROLE(), config.governance);
        deployment.vault.grantRole(deployment.vault.DEFAULT_ADMIN_ROLE(), config.governance);
        deployment.oracle.grantRole(deployment.oracle.DEFAULT_ADMIN_ROLE(), config.governance);
        deployment.guard.renounceRole(deployment.guard.DEFAULT_ADMIN_ROLE(), config.deployer);
        deployment.vault.renounceRole(deployment.vault.DEFAULT_ADMIN_ROLE(), config.deployer);
        deployment.oracle.renounceRole(deployment.oracle.DEFAULT_ADMIN_ROLE(), config.deployer);
    }
}
