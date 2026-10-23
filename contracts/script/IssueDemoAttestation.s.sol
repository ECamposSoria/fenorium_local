// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";

interface IEAS {
    struct AttestationRequestData {
        address recipient;
        uint64 expirationTime;
        bool revocable;
        bytes32 refUID;
        bytes data;
        uint256 value;
    }

    struct AttestationRequest {
        bytes32 schema;
        AttestationRequestData data;
    }

    function attest(AttestationRequest calldata request) external payable returns (bytes32);
}

interface IGatedTreasuryAttester {
    function trustedAttester() external view returns (address);
}

/// @notice Owner-run issuance of one Base Sepolia demo eligibility attestation.
/// @dev The recipient registers the returned UID in a separate wallet-signed transaction.
contract IssueDemoAttestation is Script {
    address constant EAS = 0x4200000000000000000000000000000000000021;
    address constant GATED_TREASURY = 0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54;
    bytes32 constant SCHEMA =
        0xc6c30d8537afcc25aca394ef6b70c65d8378ae3a9416a9944c2f75e6feb71823;

    function run() external {
        require(block.chainid == 84532, "demo attestation is for Base Sepolia only");

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address issuer = vm.addr(privateKey);
        address gatedTreasury = vm.envOr("DEMO_GATED_TREASURY", GATED_TREASURY);
        address recipient = vm.envAddress("DEMO_ATTESTATION_RECIPIENT");
        require(recipient != address(0), "recipient is zero");
        require(
            IGatedTreasuryAttester(gatedTreasury).trustedAttester() == issuer,
            "PRIVATE_KEY is not the deployed trustedAttester"
        );

        IEAS.AttestationRequest memory request = IEAS.AttestationRequest({
            schema: SCHEMA,
            data: IEAS.AttestationRequestData({
                recipient: recipient,
                expirationTime: 0,
                revocable: true,
                refUID: bytes32(0),
                data: abi.encode(true, "US", "retail"),
                value: 0
            })
        });

        vm.startBroadcast(privateKey);
        bytes32 uid = IEAS(EAS).attest(request);
        vm.stopBroadcast();

        console2.log("chain id       ", block.chainid);
        console2.log("trusted issuer ", issuer);
        console2.log("recipient      ", recipient);
        console2.log("attestation UID");
        console2.logBytes32(uid);
    }
}
