// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";

import {DecisionPassport} from "../src/DecisionPassport.sol";

/// @notice Deploys the DecisionPassport to Base.
/// @dev Deliberately separate from `Deploy.s.sol`: the passport is an attestation registry that
/// custodies nothing, so it is safe on Base mainnet, whereas the vault deployment stays on Base
/// Sepolia with no real funds. Both chain ids are accepted; the script prints which one it used.
contract DeployDecisionPassport is Script {
    uint256 internal constant BASE_MAINNET_CHAIN_ID = 8_453;
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;

    error UnsupportedChain(uint256 actualChainId);

    function run() external returns (DecisionPassport passport) {
        if (block.chainid != BASE_MAINNET_CHAIN_ID && block.chainid != BASE_SEPOLIA_CHAIN_ID) {
            revert UnsupportedChain(block.chainid);
        }

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(privateKey);
        // Both default to the deployer so a solo hackathon deploy needs one key, not three.
        address governance = vm.envOr("PAR_GOVERNANCE", deployer);
        address issuer = vm.envOr("PAR_ISSUER", deployer);

        vm.startBroadcast(privateKey);
        passport = new DecisionPassport(governance, issuer);
        vm.stopBroadcast();

        console2.log("chainId    ", block.chainid);
        console2.log("passport   ", address(passport));
        console2.log("governance ", governance);
        console2.log("issuer     ", issuer);
    }
}
