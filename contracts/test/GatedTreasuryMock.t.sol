// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {GatedTreasuryMock, IEAS} from "../src/mocks/GatedTreasuryMock.sol";

/// @dev Stands in for the EAS predeploy so the gate can be tested without a fork.
contract EASStub is IEAS {
    mapping(bytes32 => Attestation) private _a;

    function set(bytes32 uid, Attestation memory att) external {
        _a[uid] = att;
    }

    function revoke(bytes32 uid, uint64 at) external {
        _a[uid].revocationTime = at;
    }

    function getAttestation(bytes32 uid) external view returns (Attestation memory) {
        return _a[uid];
    }
}

contract GatedTreasuryMockTest is Test {
    EASStub eas;
    GatedTreasuryMock token;

    bytes32 constant SCHEMA = keccak256("par/test-kyc/1");
    bytes32 constant OTHER_SCHEMA = keccak256("someone-elses-schema");
    address constant ATTESTER = address(0xA11CE);
    address constant IMPOSTOR = address(0xBAD);
    address alice = address(0xA1);
    address bob = address(0xB0);

    bytes32 constant UID_ALICE = bytes32(uint256(1));
    bytes32 constant UID_BOB = bytes32(uint256(2));

    function setUp() public {
        eas = new EASStub();
        token = new GatedTreasuryMock(address(eas), SCHEMA, ATTESTER);
        _issue(UID_ALICE, alice, SCHEMA, ATTESTER, 0);
        _issue(UID_BOB, bob, SCHEMA, ATTESTER, 0);
    }

    function _issue(bytes32 uid, address to, bytes32 schema, address attester, uint64 expiry)
        internal
    {
        IEAS.Attestation memory a;
        a.uid = uid;
        a.schema = schema;
        a.time = uint64(block.timestamp);
        a.expirationTime = expiry;
        a.recipient = to;
        a.attester = attester;
        a.revocable = true;
        eas.set(uid, a);
    }

    function _verify(address who, bytes32 uid) internal {
        vm.prank(who);
        token.registerCredential(uid);
    }

    function test_an_unverified_wallet_cannot_receive() public {
        assertFalse(token.isVerified(alice));
        vm.expectRevert(abi.encodeWithSelector(GatedTreasuryMock.NotVerified.selector, alice));
        token.mint(alice, 1_000e6);
    }

    function test_registering_a_real_attestation_opens_the_gate() public {
        _verify(alice, UID_ALICE);
        assertTrue(token.isVerified(alice));
        token.mint(alice, 1_000e6);
        assertEq(token.balanceOf(alice), 1_000e6);
    }

    function test_the_gate_fires_before_the_balance_check() public {
        _verify(alice, UID_ALICE);
        token.mint(alice, 1_000e6);
        // bob never registered: the revert names him, not a shortfall
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(GatedTreasuryMock.NotVerified.selector, bob));
        token.transfer(bob, 1e6);
    }

    function test_a_verified_pair_can_transfer() public {
        _verify(alice, UID_ALICE);
        _verify(bob, UID_BOB);
        token.mint(alice, 1_000e6);
        vm.prank(alice);
        token.transfer(bob, 250e6);
        assertEq(token.balanceOf(bob), 250e6);
    }

    function test_you_cannot_register_someone_elses_credential() public {
        vm.prank(bob);
        vm.expectRevert(
            abi.encodeWithSelector(GatedTreasuryMock.NotTheRecipient.selector, alice, bob)
        );
        token.registerCredential(UID_ALICE);
    }

    function test_an_untrusted_attester_is_refused() public {
        _issue(UID_ALICE, alice, SCHEMA, IMPOSTOR, 0);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(GatedTreasuryMock.UntrustedAttester.selector, IMPOSTOR, ATTESTER)
        );
        token.registerCredential(UID_ALICE);
    }

    function test_a_credential_for_a_different_schema_is_refused() public {
        _issue(UID_ALICE, alice, OTHER_SCHEMA, ATTESTER, 0);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(GatedTreasuryMock.WrongSchema.selector, OTHER_SCHEMA, SCHEMA)
        );
        token.registerCredential(UID_ALICE);
    }

    function test_revoking_after_registration_closes_the_gate() public {
        _verify(alice, UID_ALICE);
        _verify(bob, UID_BOB);
        token.mint(alice, 1_000e6);

        eas.revoke(UID_ALICE, uint64(block.timestamp));

        assertFalse(token.isVerified(alice));
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(GatedTreasuryMock.NotVerified.selector, alice));
        token.transfer(bob, 1e6);
    }

    function test_an_expired_credential_stops_working_without_being_touched() public {
        _issue(UID_ALICE, alice, SCHEMA, ATTESTER, uint64(block.timestamp + 1 days));
        _verify(alice, UID_ALICE);
        assertTrue(token.isVerified(alice));

        vm.warp(block.timestamp + 2 days);
        assertFalse(token.isVerified(alice));
    }

    function test_an_unverified_wallet_cannot_claim() public {
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(GatedTreasuryMock.NotVerified.selector, bob));
        token.claim(1_000e6);
    }

    function test_a_verified_wallet_claims_its_own_position() public {
        _verify(alice, UID_ALICE);
        vm.prank(alice);
        token.claim(250_000e6);
        assertEq(token.balanceOf(alice), 250_000e6);
    }

    function test_the_claim_cap_is_enforced() public {
        _verify(alice, UID_ALICE);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                GatedTreasuryMock.ClaimTooLarge.selector, 10_000_001e6, 10_000_000e6
            )
        );
        token.claim(10_000_001e6);
    }
}
