// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {GatedTreasuryMock} from "../src/mocks/GatedTreasuryMock.sol";
import {PermissionlessTreasuryMock} from "../src/mocks/PermissionlessTreasuryMock.sol";

interface ISchemaRegistry {
    function register(string calldata schema, address resolver, bool revocable)
        external
        returns (bytes32);
}

/// @notice Deploys the two demo instruments to Base Sepolia and registers the EAS schema the
///         gated one trusts. The schema and the attestations are real EAS records; only the
///         instruments are mocks, because permissioned funds exist on mainnet only.
contract DeployDemoInstruments is Script {
    address constant EAS = 0x4200000000000000000000000000000000000021;
    address constant SCHEMA_REGISTRY = 0x4200000000000000000000000000000000000020;

    function run() external {
        require(block.chainid == 84532, "demo instruments are for Base Sepolia only");

        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);

        vm.startBroadcast(pk);

        // EAS reverts AlreadyExists when the same schema string is registered twice, so a
        // redeploy must reuse the uid from the first run rather than register again.
        bytes32 schemaUid = vm.envOr("SCHEMA_UID", bytes32(0));
        if (schemaUid == bytes32(0)) {
            schemaUid = ISchemaRegistry(SCHEMA_REGISTRY).register(
                "bool verified,string jurisdiction,string tier", address(0), true
            );
        }

        GatedTreasuryMock gated = new GatedTreasuryMock(EAS, schemaUid, deployer);
        PermissionlessTreasuryMock open = new PermissionlessTreasuryMock();

        vm.stopBroadcast();

        console2.log("chain id            ", block.chainid);
        console2.log("trusted attester    ", deployer);
        console2.log("EAS schema uid      ");
        console2.logBytes32(schemaUid);
        console2.log("GatedTreasuryMock   ", address(gated));
        console2.log("PermissionlessMock  ", address(open));
    }
}
