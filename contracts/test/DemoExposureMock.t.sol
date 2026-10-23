// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

import {DemoExposureMock} from "../src/mocks/DemoExposureMock.sol";

contract DemoExposureMockTest is Test {
    DemoExposureMock internal token;
    address internal constant ALICE = address(0xA11CE);

    function setUp() public {
        token = new DemoExposureMock("Mock Fixed-Term Note", "mFT", 6);
    }

    function test_identity_comes_from_the_constructor() public {
        assertEq(token.name(), "Mock Fixed-Term Note");
        assertEq(token.symbol(), "mFT");
        // The whole reason this contract is parameterised: decimals must not be a constant 6
        // inherited from the treasury mocks it replaces.
        assertEq(token.decimals(), 6);
        assertEq(new DemoExposureMock("Eighteen", "m18", 18).decimals(), 18);
    }

    function test_deployer_holds_both_roles() public view {
        assertTrue(token.hasRole(token.DEFAULT_ADMIN_ROLE(), address(this)));
        assertTrue(token.hasRole(token.MINTER_ROLE(), address(this)));
    }

    function test_minting_requires_the_minter_role() public {
        token.mint(ALICE, 1_000e6);
        assertEq(token.balanceOf(ALICE), 1_000e6);

        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, ALICE, token.MINTER_ROLE()
            )
        );
        vm.prank(ALICE);
        token.mint(ALICE, 1);
    }
}
