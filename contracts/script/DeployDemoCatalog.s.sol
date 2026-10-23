// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {DemoCatalogFactory} from "../src/mocks/DemoCatalogFactory.sol";
import {DemoCatalogSpecs} from "./catalog/DemoCatalogSpecs.sol";

/// @notice Lists the Fenorium testnet catalog on Base Sepolia: one cloned token and desk per
///         instrument declared in scripts/build_testnet_scenarios.py (see DemoCatalogSpecs). Gated
///         instruments consult mGT's verification registry. Resumable: with DEMO_CATALOG_FACTORY
///         set it reuses that factory and lists only the instruments it does not hold yet, so an
///         interrupted run continues where it stopped instead of listing duplicates.
contract DeployDemoCatalog is Script {
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;
    uint256 internal constant DEFAULT_BATCH = 12;

    address internal constant PAYMENT_TOKEN = 0xfECb2EfF5603b44daD440561cA3cD889F29239ac;
    /// @dev mGT: its isVerified(address) is the registry every gated catalog instrument consults.
    address internal constant GATE_REGISTRY = 0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54;

    function run() external {
        if (block.chainid != BASE_SEPOLIA_CHAIN_ID) revert("the demo catalog is for Base Sepolia only");

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(privateKey);
        address paymentToken = vm.envOr("DEMO_PAYMENT_TOKEN", PAYMENT_TOKEN);
        address gateRegistry = vm.envOr("DEMO_GATE_REGISTRY", GATE_REGISTRY);
        uint256 batch = vm.envOr("DEMO_CATALOG_BATCH", DEFAULT_BATCH);
        address existing = vm.envOr("DEMO_CATALOG_FACTORY", address(0));

        vm.startBroadcast(privateKey);
        DemoCatalogFactory factory = existing == address(0)
            ? new DemoCatalogFactory(IERC20(paymentToken), gateRegistry, deployer)
            : DemoCatalogFactory(existing);

        uint256 listed = factory.count();
        while (listed < DemoCatalogSpecs.COUNT) {
            uint256 size = DemoCatalogSpecs.COUNT - listed < batch ? DemoCatalogSpecs.COUNT - listed : batch;
            DemoCatalogFactory.Spec[] memory specs = new DemoCatalogFactory.Spec[](size);
            for (uint256 i = 0; i < size; i++) {
                specs[i] = DemoCatalogSpecs.spec(listed + i);
            }
            factory.list(specs);
            listed += size;
        }
        vm.stopBroadcast();

        console2.log("DemoCatalogFactory", address(factory));
        console2.log("instruments listed", factory.count());
    }
}
