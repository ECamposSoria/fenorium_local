// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Test} from "forge-std/Test.sol";

import {DemoInstrumentDesk} from "../src/mocks/DemoInstrumentDesk.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";
import {PermissionlessTreasuryMock} from "../src/mocks/PermissionlessTreasuryMock.sol";

contract EligibilityCheckerMock {
    mapping(address => bool) internal _verified;

    function setVerified(address wallet, bool verified) external {
        _verified[wallet] = verified;
    }

    function isVerified(address wallet) external view returns (bool) {
        return _verified[wallet];
    }
}

/// @dev Records the credential a desk presents, the way the gated mock would verify it.
contract CredentialRecorder is PermissionlessTreasuryMock {
    mapping(address => bytes32) public credentialOf;

    function registerCredential(bytes32 uid) external {
        credentialOf[msg.sender] = uid;
    }
}

contract DemoInstrumentDeskTest is Test {
    uint256 internal constant USDC = 1e6;
    uint256 internal constant PRICE = 1_025_000;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    MockUSDC internal usdc;
    PermissionlessTreasuryMock internal instrument;
    DemoInstrumentDesk internal desk;

    function setUp() public {
        usdc = new MockUSDC();
        instrument = new PermissionlessTreasuryMock();
        desk = new DemoInstrumentDesk(usdc, instrument, address(0), PRICE);
        instrument.grantRole(instrument.MINTER_ROLE(), address(desk));

        usdc.mint(alice, 10_000 * USDC);
        vm.startPrank(alice);
        usdc.approve(address(desk), type(uint256).max);
        instrument.approve(address(desk), type(uint256).max);
        vm.stopPrank();
    }

    function test_quotesRoundAgainstTheCallerInBothDirections() public view {
        assertEq(desk.quote(1 * USDC), PRICE);
        assertEq(desk.sellQuote(1 * USDC), PRICE);
        assertEq(desk.quote(1), 2, "a buyer's fractional micro rounds up");
        assertEq(desk.sellQuote(1), 1, "a seller's fractional micro rounds down");
        assertEq(desk.quote(1_234_567), (1_234_567 * PRICE + USDC - 1) / USDC);
        assertEq(desk.sellQuote(1_234_567), 1_234_567 * PRICE / USDC);
    }

    function test_zeroAmountAndUnsetPriceAreRefusedInBothDirections() public {
        vm.expectRevert(DemoInstrumentDesk.ZeroAmount.selector);
        desk.quote(0);
        vm.expectRevert(DemoInstrumentDesk.ZeroAmount.selector);
        desk.sellQuote(0);
        desk.setPrice(0);
        vm.expectRevert(DemoInstrumentDesk.PriceNotSet.selector);
        desk.quote(1 * USDC);
        vm.expectRevert(DemoInstrumentDesk.PriceNotSet.selector);
        desk.sellQuote(1 * USDC);
    }

    function test_purchaseKeepsThePaymentInTheDeskAndMintsTheInstrument() public {
        uint256 amount = 250 * USDC;
        uint256 payment = desk.quote(amount);
        uint256 before = usdc.balanceOf(alice);

        vm.expectEmit(true, true, false, true, address(desk));
        emit DemoInstrumentDesk.Purchased(alice, address(instrument), amount, payment, PRICE);
        vm.prank(alice);
        desk.purchase(amount, payment);

        assertEq(usdc.balanceOf(alice), before - payment);
        assertEq(usdc.balanceOf(address(desk)), payment, "proceeds stay in the desk");
        assertEq(instrument.balanceOf(alice), amount);
        assertEq(instrument.balanceOf(address(desk)), 0);
    }

    function test_purchaseRefusesAQuoteAboveTheMaximumAndAnUnfundedBuyer() public {
        uint256 amount = 1 * USDC;
        uint256 payment = desk.quote(amount);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.PriceMovedAgainstBuyer.selector, payment, payment - 1));
        desk.purchase(amount, payment - 1);

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.InsufficientPaymentBalance.selector, 0, payment));
        desk.purchase(amount, payment);

        usdc.mint(bob, payment);
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.InsufficientPaymentAllowance.selector, 0, payment));
        desk.purchase(amount, payment);
    }

    function test_sellPaysFromTheDeskBalanceAndHoldsTheInstrument() public {
        uint256 amount = 100 * USDC;
        uint256 purchasePayment = desk.quote(amount);
        vm.prank(alice);
        desk.purchase(amount, purchasePayment);
        uint256 held = usdc.balanceOf(address(desk));
        uint256 payment = desk.sellQuote(40 * USDC);
        uint256 before = usdc.balanceOf(alice);

        vm.expectEmit(true, true, false, true, address(desk));
        emit DemoInstrumentDesk.Sold(alice, address(instrument), 40 * USDC, payment, PRICE);
        vm.prank(alice);
        desk.sell(40 * USDC, payment);

        assertEq(usdc.balanceOf(alice), before + payment);
        assertEq(usdc.balanceOf(address(desk)), held - payment, "paid from what the desk held; nothing minted");
        assertEq(instrument.balanceOf(alice), 60 * USDC);
        assertEq(instrument.balanceOf(address(desk)), 40 * USDC, "bought-back instruments are held, not burned");
        assertEq(usdc.totalSupply(), 10_000 * USDC, "no faucet mint when the desk can pay");
    }

    function test_sellMintsOnlyTheShortfallWhenTheDeskIsShort() public {
        instrument.mint(alice, 10 * USDC);
        uint256 payment = desk.sellQuote(10 * USDC);
        assertEq(usdc.balanceOf(address(desk)), 0);

        vm.expectEmit(false, false, false, true, address(desk));
        emit DemoInstrumentDesk.ShortfallMinted(payment);
        vm.prank(alice);
        desk.sell(10 * USDC, payment);

        assertEq(usdc.balanceOf(alice), 10_000 * USDC + payment);
        assertEq(usdc.balanceOf(address(desk)), 0, "the shortfall minted was exactly what was paid");
        assertEq(usdc.totalSupply(), 10_000 * USDC + payment);
    }

    function test_sellRefusesAQuoteBelowTheMinimumAndAnUnfundedSeller() public {
        instrument.mint(alice, 1 * USDC);
        uint256 payment = desk.sellQuote(1 * USDC);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.PriceMovedAgainstSeller.selector, payment, payment + 1));
        desk.sell(1 * USDC, payment + 1);

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.InsufficientInstrumentBalance.selector, 0, 1 * USDC));
        desk.sell(1 * USDC, payment);

        instrument.mint(bob, 1 * USDC);
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.InsufficientInstrumentAllowance.selector, 0, 1 * USDC));
        desk.sell(1 * USDC, payment);
    }

    function test_theGateHoldsForBothDirections() public {
        EligibilityCheckerMock checker = new EligibilityCheckerMock();
        DemoInstrumentDesk gated = new DemoInstrumentDesk(usdc, instrument, address(checker), PRICE);
        instrument.grantRole(instrument.MINTER_ROLE(), address(gated));
        instrument.mint(alice, 1 * USDC);
        vm.startPrank(alice);
        usdc.approve(address(gated), type(uint256).max);
        instrument.approve(address(gated), type(uint256).max);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.NotVerified.selector, alice));
        gated.purchase(1 * USDC, PRICE);
        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentDesk.NotVerified.selector, alice));
        gated.sell(1 * USDC, 0);
        vm.stopPrank();

        checker.setVerified(alice, true);
        vm.startPrank(alice);
        gated.purchase(1 * USDC, PRICE);
        gated.sell(1 * USDC, 0);
        vm.stopPrank();
        assertEq(instrument.balanceOf(address(gated)), 1 * USDC);
    }

    function test_adminPresentsTheDesksCredentialAndSweeps() public {
        CredentialRecorder recorder = new CredentialRecorder();
        DemoInstrumentDesk held = new DemoInstrumentDesk(usdc, recorder, address(0), PRICE);
        bytes32 uid = keccak256("credential");
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, bytes32(0)));
        held.registerCredential(uid);
        held.registerCredential(uid);
        assertEq(recorder.credentialOf(address(held)), uid);

        usdc.mint(address(held), 5 * USDC);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, bytes32(0)));
        held.sweep(IERC20(address(usdc)), bob, 5 * USDC);
        held.sweep(IERC20(address(usdc)), bob, 5 * USDC);
        assertEq(usdc.balanceOf(bob), 5 * USDC);
    }

    function test_onlyTheAdminSetsThePrice() public {
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, bytes32(0)));
        desk.setPrice(1);
    }
}
