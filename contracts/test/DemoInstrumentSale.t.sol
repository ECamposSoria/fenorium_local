// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Test} from "forge-std/Test.sol";

import {DemoInstrumentSale} from "../src/mocks/DemoInstrumentSale.sol";
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

contract FalseTransferToken is ERC20 {
    constructor() ERC20("False Transfer Token", "FTT") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address account, uint256 amount) external {
        _mint(account, amount);
    }

    function transferFrom(address, address, uint256) public pure override returns (bool) {
        return false;
    }
}

contract DemoInstrumentSaleTest is Test {
    uint256 internal constant USDC = 1e6;
    uint256 internal constant PRICE = 1_025_000;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal treasury = makeAddr("treasury");

    MockUSDC internal usdc;
    PermissionlessTreasuryMock internal instrument;
    DemoInstrumentSale internal sale;

    function setUp() public {
        usdc = new MockUSDC();
        instrument = new PermissionlessTreasuryMock();
        sale = new DemoInstrumentSale(usdc, instrument, treasury, address(0), PRICE);
        instrument.grantRole(instrument.MINTER_ROLE(), address(sale));

        usdc.mint(alice, 10_000 * USDC);
        vm.prank(alice);
        usdc.approve(address(sale), type(uint256).max);
    }

    function test_quoteUsesSixDecimalIntegerArithmetic() public view {
        assertEq(sale.quote(1 * USDC), PRICE);
        assertEq(sale.quote(1_234_567), (1_234_567 * PRICE + USDC - 1) / USDC);
        assertEq(sale.quote(1), 2);
    }

    function test_quoteNeverRoundsANonzeroPurchaseToFree() public {
        sale.setPrice(500_000);
        assertEq(sale.quote(1), 1);
    }

    function test_purchasePaysTreasuryDirectlyAndDeliversInstrument() public {
        uint256 instrumentAmount = 250 * USDC;
        uint256 paymentAmount = sale.quote(instrumentAmount);
        uint256 alicePaymentBefore = usdc.balanceOf(alice);

        vm.prank(alice);
        sale.purchase(instrumentAmount, paymentAmount);

        assertEq(usdc.balanceOf(alice), alicePaymentBefore - paymentAmount);
        assertEq(usdc.balanceOf(treasury), paymentAmount);
        assertEq(usdc.balanceOf(address(sale)), 0);
        assertEq(instrument.balanceOf(alice), instrumentAmount);
        assertEq(instrument.balanceOf(address(sale)), 0);
    }

    function test_purchaseEmitsEvent() public {
        uint256 instrumentAmount = 3 * USDC + 17;
        uint256 paymentAmount = sale.quote(instrumentAmount);

        vm.expectEmit(true, false, false, true, address(sale));
        emit DemoInstrumentSale.Purchased(alice, address(instrument), instrumentAmount, paymentAmount, PRICE);

        vm.prank(alice);
        sale.purchase(instrumentAmount, paymentAmount);
    }

    function test_purchaseSucceedsAtAndUnderMaximum() public {
        uint256 instrumentAmount = 1 * USDC;
        uint256 paymentAmount = sale.quote(instrumentAmount);

        vm.prank(alice);
        sale.purchase(instrumentAmount, paymentAmount);

        usdc.mint(bob, paymentAmount);
        vm.prank(bob);
        usdc.approve(address(sale), paymentAmount);
        vm.prank(bob);
        sale.purchase(instrumentAmount, paymentAmount + 1);

        assertEq(instrument.balanceOf(alice), instrumentAmount);
        assertEq(instrument.balanceOf(bob), instrumentAmount);
        assertEq(usdc.balanceOf(treasury), paymentAmount * 2);
    }

    function test_priceIncreaseBetweenQuoteAndPurchaseReverts() public {
        uint256 instrumentAmount = 1 * USDC;
        uint256 maxAccepted = sale.quote(instrumentAmount);

        sale.setPrice(PRICE + 100_000);
        uint256 increasedQuote = sale.quote(instrumentAmount);
        assertGt(increasedQuote, maxAccepted);

        vm.expectRevert(
            abi.encodeWithSelector(
                DemoInstrumentSale.PriceMovedAgainstBuyer.selector, increasedQuote, maxAccepted
            )
        );
        vm.prank(alice);
        sale.purchase(instrumentAmount, maxAccepted);

        assertEq(usdc.balanceOf(treasury), 0);
        assertEq(instrument.balanceOf(alice), 0);
    }

    function test_gatedFailureIsAtomic() public {
        EligibilityCheckerMock checker = new EligibilityCheckerMock();
        DemoInstrumentSale gatedSale = new DemoInstrumentSale(usdc, instrument, treasury, address(checker), PRICE);
        instrument.grantRole(instrument.MINTER_ROLE(), address(gatedSale));

        uint256 instrumentAmount = 10 * USDC;
        uint256 paymentAmount = gatedSale.quote(instrumentAmount);
        uint256 alicePaymentBefore = usdc.balanceOf(alice);

        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentSale.BuyerNotVerified.selector, alice));
        vm.prank(alice);
        gatedSale.purchase(instrumentAmount, paymentAmount);

        assertEq(usdc.balanceOf(alice), alicePaymentBefore);
        assertEq(usdc.balanceOf(treasury), 0);
        assertEq(instrument.balanceOf(alice), 0);
        assertEq(paymentAmount, PRICE * 10);
    }

    function test_gatedBuyerCanPurchaseWhenVerified() public {
        EligibilityCheckerMock checker = new EligibilityCheckerMock();
        checker.setVerified(alice, true);
        DemoInstrumentSale gatedSale = new DemoInstrumentSale(usdc, instrument, treasury, address(checker), PRICE);
        instrument.grantRole(instrument.MINTER_ROLE(), address(gatedSale));

        vm.prank(alice);
        usdc.approve(address(gatedSale), type(uint256).max);
        uint256 paymentAmount = gatedSale.quote(2 * USDC);
        vm.prank(alice);
        gatedSale.purchase(2 * USDC, paymentAmount);

        assertEq(instrument.balanceOf(alice), 2 * USDC);
        assertEq(usdc.balanceOf(treasury), 2 * PRICE);
    }

    function test_insufficientBalanceRevertsBeforePayment() public {
        uint256 instrumentAmount = 20_000 * USDC;
        uint256 required = sale.quote(instrumentAmount);

        vm.expectRevert(
            abi.encodeWithSelector(
                DemoInstrumentSale.InsufficientPaymentBalance.selector, usdc.balanceOf(alice), required
            )
        );
        vm.prank(alice);
        sale.purchase(instrumentAmount, required);
    }

    function test_insufficientAllowanceRevertsBeforePayment() public {
        vm.prank(alice);
        usdc.approve(address(sale), 0);
        uint256 instrumentAmount = 20 * USDC;
        uint256 required = sale.quote(instrumentAmount);

        vm.expectRevert(abi.encodeWithSelector(DemoInstrumentSale.InsufficientPaymentAllowance.selector, 0, required));
        vm.prank(alice);
        sale.purchase(instrumentAmount, required);
    }

    function test_allowanceShortfallRevertsAtomically() public {
        uint256 insufficientAllowance = sale.quote(1 * USDC) - 1;
        uint256 required = sale.quote(1 * USDC);
        vm.prank(alice);
        usdc.approve(address(sale), insufficientAllowance);

        vm.expectRevert(
            abi.encodeWithSelector(
                DemoInstrumentSale.InsufficientPaymentAllowance.selector, insufficientAllowance, required
            )
        );
        vm.prank(alice);
        sale.purchase(1 * USDC, required);

        assertEq(usdc.balanceOf(treasury), 0);
        assertEq(instrument.balanceOf(alice), 0);
    }

    function test_paymentFailureRevertsAtomically() public {
        FalseTransferToken falseToken = new FalseTransferToken();
        DemoInstrumentSale falseSale = new DemoInstrumentSale(falseToken, instrument, treasury, address(0), PRICE);
        instrument.grantRole(instrument.MINTER_ROLE(), address(falseSale));
        falseToken.mint(alice, 10 * USDC);
        vm.prank(alice);
        falseToken.approve(address(falseSale), type(uint256).max);
        uint256 paymentAmount = falseSale.quote(1 * USDC);

        vm.expectRevert(DemoInstrumentSale.PaymentFailed.selector);
        vm.prank(alice);
        falseSale.purchase(1 * USDC, paymentAmount);

        assertEq(falseToken.balanceOf(treasury), 0);
        assertEq(instrument.balanceOf(alice), 0);
    }

    function test_deliveryFailureRollsBackPayment() public {
        instrument.revokeRole(instrument.MINTER_ROLE(), address(sale));
        uint256 instrumentAmount = 10 * USDC;
        uint256 paymentAmount = sale.quote(instrumentAmount);
        uint256 alicePaymentBefore = usdc.balanceOf(alice);

        vm.expectRevert(DemoInstrumentSale.DeliveryFailed.selector);
        vm.prank(alice);
        sale.purchase(instrumentAmount, paymentAmount);

        assertEq(usdc.balanceOf(alice), alicePaymentBefore);
        assertEq(usdc.balanceOf(treasury), 0);
        assertEq(instrument.balanceOf(alice), 0);
        assertEq(paymentAmount, 10 * PRICE);
    }

    function test_zeroAmountRevertsFromQuoteAndPurchase() public {
        vm.expectRevert(DemoInstrumentSale.ZeroAmount.selector);
        sale.quote(0);

        vm.expectRevert(DemoInstrumentSale.ZeroAmount.selector);
        vm.prank(alice);
        sale.purchase(0, 0);
    }

    function test_priceMustBeSet() public {
        sale.setPrice(0);

        vm.expectRevert(DemoInstrumentSale.PriceNotSet.selector);
        sale.quote(1);

        vm.expectRevert(DemoInstrumentSale.PriceNotSet.selector);
        vm.prank(alice);
        sale.purchase(1, 0);
    }

    function test_adminCanSetPriceAndEventReportsChange() public {
        vm.expectEmit(true, true, false, true, address(sale));
        emit DemoInstrumentSale.PriceSet(PRICE, 2 * USDC);
        sale.setPrice(2 * USDC);
        assertEq(sale.pricePerWholeInstrument(), 2 * USDC);
    }

    function test_nonAdminCannotSetPrice() public {
        vm.expectRevert(
            abi.encodeWithSelector(
                IAccessControl.AccessControlUnauthorizedAccount.selector, alice, sale.DEFAULT_ADMIN_ROLE()
            )
        );
        vm.prank(alice);
        sale.setPrice(2 * USDC);
    }

    function test_permissionlessCheckerIsZero() public view {
        assertEq(sale.eligibilityChecker(), address(0));
    }
}
