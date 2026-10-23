// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";

import {DemoExposureMock} from "../src/mocks/DemoExposureMock.sol";

/// @notice Deploys the demo instruments that give the Base Sepolia book more than one asset
///         class, and mints each holder balance in the same transaction batch. Base Sepolia
///         only; the guard below is the only thing standing between this and a mainnet, so it
///         reverts rather than trusting the caller's --rpc-url.
contract DeployDemoExposureMocks is Script {
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;

    function run() external {
        if (block.chainid != BASE_SEPOLIA_CHAIN_ID) {
            revert("demo exposure mocks are for Base Sepolia only");
        }

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address holder = vm.envOr("DEMO_HOLDER", vm.addr(privateKey));

        vm.startBroadcast(privateKey);

        DemoExposureMock fixedTerm = new DemoExposureMock("Mock Fixed-Term Note", "mFT", 6);
        DemoExposureMock variableYield = new DemoExposureMock("Mock Variable Yield Vault", "mVY", 6);

        fixedTerm.mint(holder, vm.envUint("DEMO_FIXED_TERM_UNITS"));
        variableYield.mint(holder, vm.envUint("DEMO_VARIABLE_YIELD_UNITS"));

        vm.stopBroadcast();

        console2.log("chain id            ", block.chainid);
        console2.log("holder              ", holder);
        console2.log("mFT  fixed-term note", address(fixedTerm));
        console2.log("mVY  variable yield ", address(variableYield));
    }
}
