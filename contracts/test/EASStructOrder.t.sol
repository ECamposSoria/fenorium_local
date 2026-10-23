// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IEAS} from "../src/mocks/GatedTreasuryMock.sol";

/// @notice Guards the one thing a stub cannot: that our `IEAS.Attestation` field order matches
///         the real predeploy's.
///
/// The unit tests all pass against a stub that implements OUR struct, so they agree with
/// whatever we declare — including when it is wrong. Measured 2026-08-22: `refUID` was placed
/// after `revocable` instead of between `revocationTime` and `recipient`. Every field read
/// correctly through a separate interface, the unit suite stayed green, and the only symptom was
/// `registerCredential` reverting with no data because the ABI decode misaligned.
///
/// Skips when BASE_SEPOLIA_RPC_URL is unset so the default suite stays offline.
contract EASStructOrderTest is Test {
    address constant EAS = 0x4200000000000000000000000000000000000021;

    // Issued by the Par test attester to the demo wallet, Base Sepolia, 2026-08-22.
    bytes32 constant ATTESTATION =
        0x9065b7801e6eaab0e3d36d898c38275d36ed202dac41b926ee12781cd7208ac1;
    address constant RECIPIENT = 0xf1D2dD1523f9dA0B9e2f79c2Fc768A6F140A5783;
    address constant ATTESTER = 0xDa909368646277cc22842Bd5dBb83E5923858411;
    bytes32 constant SCHEMA =
        0xc6c30d8537afcc25aca394ef6b70c65d8378ae3a9416a9944c2f75e6feb71823;

    function test_our_attestation_struct_decodes_the_real_predeploy() public {
        string memory rpc = vm.envOr("BASE_SEPOLIA_RPC_URL", string(""));
        if (bytes(rpc).length == 0) {
            vm.skip(true);
            return;
        }
        vm.createSelectFork(rpc);

        IEAS.Attestation memory a = IEAS(EAS).getAttestation(ATTESTATION);

        // If the field order drifts, these land on the wrong words and the assertions fail here
        // rather than as an undiagnosable revert inside registerCredential.
        assertEq(a.uid, ATTESTATION, "uid");
        assertEq(a.schema, SCHEMA, "schema");
        assertEq(a.recipient, RECIPIENT, "recipient");
        assertEq(a.attester, ATTESTER, "attester");
        assertEq(a.refUID, bytes32(0), "refUID");
        assertEq(a.revocationTime, 0, "revocationTime");
        assertTrue(a.revocable, "revocable");
        assertGt(a.time, 0, "time");
    }
}
