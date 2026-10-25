// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {DecisionPassportV2} from "../src/DecisionPassportV2.sol";

/// @notice Deploy the score commitment registry on Base or Base Sepolia; custodies nothing.
contract DeployDecisionPassportV2 is Script {
    uint256 internal constant BASE_MAINNET_CHAIN_ID = 8_453;
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;
    error UnsupportedChain(uint256 actualChainId);

    function run() external returns (DecisionPassportV2 passport) {
        if (block.chainid != BASE_MAINNET_CHAIN_ID && block.chainid != BASE_SEPOLIA_CHAIN_ID) {
            revert UnsupportedChain(block.chainid);
        }
        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(privateKey);
        address governance = vm.envOr("PAR_GOVERNANCE", deployer);
        address issuer = vm.envOr("PAR_ISSUER", deployer);
        vm.startBroadcast(privateKey);
        passport = new DecisionPassportV2(governance, issuer);
        vm.stopBroadcast();
        console2.log("chainId    ", block.chainid);
        console2.log("passport   ", address(passport));
        console2.log("governance ", governance);
        console2.log("issuer     ", issuer);
    }
}
