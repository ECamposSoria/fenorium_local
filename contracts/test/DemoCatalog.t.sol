// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Test} from "forge-std/Test.sol";

import {DemoCatalogDesk} from "../src/mocks/DemoCatalogDesk.sol";
import {DemoCatalogFactory} from "../src/mocks/DemoCatalogFactory.sol";
import {DemoCatalogToken} from "../src/mocks/DemoCatalogToken.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";

contract CatalogRegistryMock {
    mapping(address => bool) internal _verified;

    function setVerified(address wallet, bool verified) external {
        _verified[wallet] = verified;
    }

    function isVerified(address wallet) external view returns (bool) {
        return _verified[wallet];
    }
}

contract DemoCatalogTest is Test {
    uint256 internal constant USDC = 1e6;

    address internal admin = makeAddr("admin");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    MockUSDC internal usdc;
    CatalogRegistryMock internal registry;
    DemoCatalogFactory internal factory;
    DemoCatalogToken internal open;
    DemoCatalogDesk internal openDesk;
    DemoCatalogToken internal gated;
    DemoCatalogDesk internal gatedDesk;

    function setUp() public {
        usdc = new MockUSDC();
        registry = new CatalogRegistryMock();
        factory = new DemoCatalogFactory(usdc, address(registry), admin);

        DemoCatalogFactory.Spec[] memory specs = new DemoCatalogFactory.Spec[](2);
        specs[0] = DemoCatalogFactory.Spec("PT-SYN05 Synthetic NATIVE Instrument", "PT-SYN05", false, 990_000);
        specs[1] = DemoCatalogFactory.Spec("RWA02 Synthetic RWA Instrument", "RWA02", true, 1_005_000);
        vm.prank(admin);
        factory.list(specs);

        open = DemoCatalogToken(factory.instruments(0));
        openDesk = DemoCatalogDesk(factory.deskFor(address(open)));
        gated = DemoCatalogToken(factory.instruments(1));
        gatedDesk = DemoCatalogDesk(factory.deskFor(address(gated)));

        usdc.mint(alice, 1_000 * USDC);
        usdc.mint(bob, 1_000 * USDC);
        vm.startPrank(alice);
        usdc.approve(address(openDesk), type(uint256).max);
        usdc.approve(address(gatedDesk), type(uint256).max);
        open.approve(address(openDesk), type(uint256).max);
        gated.approve(address(gatedDesk), type(uint256).max);
        vm.stopPrank();
    }

    function test_factoryListsAClonedTokenAndDeskWiredForTheCaller() public view {
        assertEq(factory.count(), 2);
        assertEq(open.name(), "PT-SYN05 Synthetic NATIVE Instrument");
        assertEq(open.symbol(), "PT-SYN05");
        assertEq(open.decimals(), 6);
        assertEq(open.gateRegistry(), address(0));
        assertEq(gated.gateRegistry(), address(registry));
        assertEq(open.transferAgent(), address(openDesk));
        assertTrue(open.hasRole(open.MINTER_ROLE(), address(openDesk)));
        assertTrue(open.hasRole(open.DEFAULT_ADMIN_ROLE(), admin));
        assertFalse(open.hasRole(open.DEFAULT_ADMIN_ROLE(), address(factory)));
        assertTrue(openDesk.hasRole(openDesk.DEFAULT_ADMIN_ROLE(), admin));
        assertEq(address(openDesk.instrument()), address(open));
        assertEq(address(openDesk.paymentToken()), address(usdc));
        assertEq(openDesk.eligibilityChecker(), address(0));
        assertEq(gatedDesk.eligibilityChecker(), address(registry));
        assertEq(openDesk.pricePerWholeInstrument(), 990_000);
        assertEq(gatedDesk.pricePerWholeInstrument(), 1_005_000);
    }

    function test_onlyTheAdminLists() public {
        DemoCatalogFactory.Spec[] memory specs = new DemoCatalogFactory.Spec[](1);
        specs[0] = DemoCatalogFactory.Spec("X", "X", false, USDC);
        bytes32 adminRole = factory.DEFAULT_ADMIN_ROLE();
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, adminRole)
        );
        vm.prank(alice);
        factory.list(specs);
    }

    function test_clonesAndImplementationsInitializeOnlyOnce() public {
        vm.expectRevert(DemoCatalogToken.AlreadyInitialized.selector);
        open.initialize("again", "AGN", alice, address(0));
        vm.expectRevert(DemoCatalogDesk.AlreadyInitialized.selector);
        openDesk.initialize(usdc, IERC20(address(open)), address(0), USDC, alice);

        DemoCatalogToken tokenImplementation = DemoCatalogToken(factory.tokenImplementation());
        vm.expectRevert(DemoCatalogToken.AlreadyInitialized.selector);
        tokenImplementation.initialize("impl", "IMPL", alice, address(0));
        DemoCatalogDesk deskImplementation = DemoCatalogDesk(factory.deskImplementation());
        vm.expectRevert(DemoCatalogDesk.AlreadyInitialized.selector);
        deskImplementation.initialize(usdc, IERC20(address(open)), address(0), USDC, alice);
    }

    function test_aPermissionlessCloneBuysAndSellsBackThroughItsDesk() public {
        uint256 quote = openDesk.quote(5 * USDC);
        assertEq(quote, 4_950_000);
        vm.prank(alice);
        openDesk.purchase(5 * USDC, quote);
        assertEq(open.balanceOf(alice), 5 * USDC);
        assertEq(usdc.balanceOf(address(openDesk)), quote);

        vm.prank(alice);
        open.transfer(bob, 1 * USDC);
        assertEq(open.balanceOf(bob), 1 * USDC, "an ungated instrument moves freely");

        uint256 sellQuote = openDesk.sellQuote(2 * USDC);
        vm.prank(alice);
        openDesk.sell(2 * USDC, sellQuote);
        assertEq(open.balanceOf(alice), 2 * USDC);
        assertEq(open.balanceOf(address(openDesk)), 2 * USDC);
        assertEq(usdc.balanceOf(alice), 1_000 * USDC - quote + sellQuote);
    }

    function test_aGatedCloneSellsOnlyToVerifiedWalletsAndItsDeskTakesItBack() public {
        uint256 quote = gatedDesk.quote(3 * USDC);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(DemoCatalogDesk.NotVerified.selector, alice));
        gatedDesk.purchase(3 * USDC, quote);

        registry.setVerified(alice, true);
        vm.prank(alice);
        gatedDesk.purchase(3 * USDC, quote);
        assertEq(gated.balanceOf(alice), 3 * USDC);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(DemoCatalogToken.NotVerified.selector, bob));
        gated.transfer(bob, 1 * USDC);

        // The desk holds no credential and still takes the instrument back: it is the transfer agent.
        assertFalse(registry.isVerified(address(gatedDesk)));
        uint256 sellQuote = gatedDesk.sellQuote(3 * USDC);
        vm.prank(alice);
        gatedDesk.sell(3 * USDC, sellQuote);
        assertEq(gated.balanceOf(address(gatedDesk)), 3 * USDC);
        assertEq(gated.balanceOf(alice), 0);
    }

    function test_theDeskMintsAShortfallWhenItHoldsLessThanItOwes() public {
        uint256 quote = openDesk.quote(1 * USDC);
        vm.prank(alice);
        openDesk.purchase(1 * USDC, quote);
        vm.prank(admin);
        openDesk.sweep(usdc, admin, quote);
        assertEq(usdc.balanceOf(address(openDesk)), 0);

        uint256 sellQuote = openDesk.sellQuote(1 * USDC);
        vm.prank(alice);
        openDesk.sell(1 * USDC, sellQuote);
        assertEq(usdc.balanceOf(alice), 1_000 * USDC - quote + sellQuote);
    }

    function test_onlyTheDeskMintsAndOnlyTheAdminSetsTheAgentOrPrice() public {
        bytes32 minterRole = open.MINTER_ROLE();
        bytes32 tokenAdminRole = open.DEFAULT_ADMIN_ROLE();
        bytes32 deskAdminRole = openDesk.DEFAULT_ADMIN_ROLE();

        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, minterRole)
        );
        vm.prank(alice);
        open.mint(alice, 1);

        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, tokenAdminRole)
        );
        vm.prank(alice);
        open.setTransferAgent(alice);

        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, alice, deskAdminRole)
        );
        vm.prank(alice);
        openDesk.setPrice(1);

        vm.prank(admin);
        openDesk.setPrice(0);
        vm.expectRevert(DemoCatalogDesk.PriceNotSet.selector);
        openDesk.quote(1 * USDC);
    }

    function test_listingCostStaysWithinTheTestnetBudget() public {
        DemoCatalogFactory.Spec[] memory specs = new DemoCatalogFactory.Spec[](11);
        for (uint256 i = 0; i < specs.length; i++) {
            specs[i] = DemoCatalogFactory.Spec("vUSD03 Synthetic VAULT Instrument", "vUSD03", i % 4 == 1, USDC);
        }
        vm.prank(admin);
        uint256 before = gasleft();
        factory.list(specs);
        uint256 used = before - gasleft();
        assertLt(used / specs.length, 700_000, "a listed instrument should cost well under a whole deploy");
    }
}
