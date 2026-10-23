// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";

import {ParVault} from "../src/ParVault.sol";
import {RiskGuard} from "../src/RiskGuard.sol";
import {PendleAdapter} from "../src/adapters/PendleAdapter.sol";
import {IParAdapter} from "../src/adapters/IParAdapter.sol";
import {MockPendleMarket} from "../src/adapters/mocks/MockPendleMarket.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";
import {MockInstrument} from "../src/mocks/MockInstrument.sol";
import {MockParOracle} from "../src/mocks/MockParOracle.sol";

contract ParVaultTest is Test {
    uint256 internal constant USDC = 1e6;
    uint64 internal constant MATURITY = 365 days;

    uint256 internal allocatorKey = 0xA110CA7E;
    address internal allocator;
    address internal guardian = makeAddr("guardian");
    address internal sentinel = makeAddr("sentinel");
    address internal alice = makeAddr("alice");
    address internal outsider = makeAddr("outsider");

    MockUSDC internal usdc;
    MockInstrument internal pt;
    MockParOracle internal oracle;
    RiskGuard internal guard;
    ParVault internal vault;
    MockPendleMarket internal market;
    PendleAdapter internal adapter;

    function setUp() public {
        vm.warp(10 days);
        allocator = vm.addr(allocatorKey);
        usdc = new MockUSDC();
        pt = new MockInstrument("Mock Pendle PT", "PT");
        oracle = new MockParOracle(address(this));
        guard = new RiskGuard(address(this), guardian, sentinel);
        vault = new ParVault(usdc, "Par Vault Share", "PAR", guard, address(this), allocator);
        market = new MockPendleMarket(usdc, address(pt), uint64(block.timestamp + MATURITY), 500, 300);
        adapter = new PendleAdapter(address(vault), usdc, market);

        vault.setWhitelist(alice, true);
        vault.setAdapter(adapter, true);
        vault.setMaximumUnwindCostBps(1_000);
        guard.setAssetPolicy(address(pt), true, 8_000, oracle, 1 days);
        guard.setJobCap(uint8(ParVault.Job.CARRY), 8_000);
        guard.setMinimumReserveBps(2_000);
        oracle.setPrice(address(pt), 1e18, block.timestamp);

        usdc.mint(alice, 2_000_000 * USDC);
        usdc.mint(address(market), 2_000_000 * USDC);
        vm.prank(alice);
        usdc.approve(address(vault), type(uint256).max);
    }

    function testAssetAndShareDecimalsUseOffset() public view {
        assertEq(vault.decimals(), 12);
        assertEq(usdc.decimals(), 6);
    }

    function testDepositWhitelistUsesERC4626MaxHooks() public {
        assertEq(vault.maxDeposit(outsider), 0);
        assertEq(vault.maxMint(outsider), 0);
        usdc.mint(outsider, 10 * USDC);
        vm.prank(outsider);
        usdc.approve(address(vault), type(uint256).max);

        vm.expectPartialRevert(ERC4626.ERC4626ExceededMaxDeposit.selector);
        vm.prank(outsider);
        vault.deposit(USDC, outsider);
    }

    function testAssetCapAcceptsBoundaryAndRejectsOneAbove() public {
        guard.setAssetPolicy(address(pt), true, 5_000, oracle, 1 days);
        guard.setJobCap(uint8(ParVault.Job.CARRY), 10_000);
        guard.setMinimumReserveBps(0);

        RiskGuard.AllocationContext memory context = _context(500 * USDC, 1_000 * USDC, 1_000 * USDC, 0, 0);
        guard.checkAllocation(context);
        context.assets += 1;
        vm.expectPartialRevert(RiskGuard.AssetConcentrationExceeded.selector);
        guard.checkAllocation(context);
    }

    function testJobCapAcceptsBoundaryAndRejectsOneAbove() public {
        guard.setAssetPolicy(address(pt), true, 10_000, oracle, 1 days);
        guard.setJobCap(uint8(ParVault.Job.CARRY), 5_000);
        guard.setMinimumReserveBps(0);

        RiskGuard.AllocationContext memory context = _context(500 * USDC, 1_000 * USDC, 1_000 * USDC, 0, 0);
        guard.checkAllocation(context);
        context.assets += 1;
        vm.expectPartialRevert(RiskGuard.JobConcentrationExceeded.selector);
        guard.checkAllocation(context);
    }

    function testReserveFloorAcceptsBoundaryAndRejectsOneAbove() public {
        guard.setAssetPolicy(address(pt), true, 10_000, oracle, 1 days);
        guard.setJobCap(uint8(ParVault.Job.CARRY), 10_000);
        guard.setMinimumReserveBps(2_000);

        RiskGuard.AllocationContext memory context = _context(800 * USDC, 1_000 * USDC, 1_000 * USDC, 0, 0);
        guard.checkAllocation(context);
        context.assets += 1;
        vm.expectPartialRevert(RiskGuard.ReserveFloorBreached.selector);
        guard.checkAllocation(context);
    }

    function testOracleFreshnessAcceptsInsideAndRejectsOutsideWindow() public {
        oracle.setPrice(address(pt), 1e18, block.timestamp - 1 days + 1);
        guard.checkAllocation(_context(USDC, 100 * USDC, 100 * USDC, 0, 0));

        oracle.setPrice(address(pt), 1e18, block.timestamp - 1 days - 1);
        vm.expectPartialRevert(RiskGuard.OracleStale.selector);
        guard.checkAllocation(_context(USDC, 100 * USDC, 100 * USDC, 0, 0));
    }

    function testCircuitBreakerStopsAllocationButScheduledExitStillClaims() public {
        _deposit(100 * USDC);
        vm.prank(guardian);
        guard.tripCircuitBreaker();

        vm.expectRevert(RiskGuard.CircuitBreakerActive.selector);
        vm.prank(allocator);
        vault.allocate(ParVault.Job.CARRY, address(adapter), 10 * USDC, _pendleInvestData());

        uint256 shares = vault.balanceOf(alice) / 2;
        vm.prank(alice);
        uint256 id = vault.requestScheduledExit(shares);
        uint256 beforeBalance = usdc.balanceOf(alice);
        vault.claimScheduledExit(id);
        assertGt(usdc.balanceOf(alice), beforeBalance);
    }

    function testBrakeOnlySentinelCannotClearAndIncidentForcesNoBid() public {
        vm.prank(sentinel);
        guard.raiseIncident(address(pt));
        assertTrue(guard.incidents(address(pt)));

        vm.expectPartialRevert(IAccessControl.AccessControlUnauthorizedAccount.selector);
        vm.prank(sentinel);
        guard.clearIncident(address(pt));

        _deposit(100 * USDC);
        vm.expectPartialRevert(RiskGuard.AssetIncident.selector);
        vm.prank(allocator);
        vault.allocate(ParVault.Job.CARRY, address(adapter), 10 * USDC, _pendleInvestData());
    }

    function testImmediateLossyExitDoesNotSocializeUnwindCost() public {
        _deposit(1_000 * USDC);
        vm.prank(allocator);
        vault.allocate(ParVault.Job.CARRY, address(adapter), 800 * USDC, _pendleInvestData());

        uint256 shares = vault.balanceOf(alice) / 2;
        uint256 grossAssets = vault.previewRedeem(shares);
        uint256 requested = grossAssets - vault.idleReserve();
        uint256 quotedLoss = _exactPendleLoss(requested);
        assertGt(quotedLoss, 0, "mock unwind must be genuinely lossy");

        uint256 ppsBefore = Math.mulDiv(vault.totalAssets(), 1e30, vault.totalSupply());
        uint256 deadline = block.timestamp + 1 hours;
        bytes memory data = _pendleDivestData();
        bytes memory signature = _signImmediateExit(alice, shares, quotedLoss, deadline, address(adapter), data, 0);
        vm.prank(alice);
        vault.redeemImmediate(shares, quotedLoss, deadline, address(adapter), data, 0, signature);
        uint256 ppsAfter = Math.mulDiv(vault.totalAssets(), 1e30, vault.totalSupply());

        assertGe(ppsAfter, ppsBefore, "the leaver must bear the realized unwind loss");
        assertLe(ppsAfter - ppsBefore, 1e15, "excess is at most one USDC atomic unit of PPS dust");
    }

    function testImmediateQuoteChecksDeadlineCeilingSignerAndNonce() public {
        _deposit(100 * USDC);
        uint256 shares = vault.balanceOf(alice) / 4;
        bytes memory data = _pendleDivestData();

        vm.expectPartialRevert(ParVault.QuoteExpired.selector);
        vm.prank(alice);
        vault.redeemImmediate(shares, 0, block.timestamp - 1, address(adapter), data, 0, "");

        uint256 excessiveCost = vault.previewRedeem(shares) * 1_001 / 10_000 + 1;
        vm.expectPartialRevert(ParVault.UnwindCostTooHigh.selector);
        vm.prank(alice);
        vault.redeemImmediate(shares, excessiveCost, block.timestamp + 1 hours, address(adapter), data, 0, "");

        uint256 deadline = block.timestamp + 1 hours;
        bytes32 digest = vault.immediateExitDigest(
            ParVault.ImmediateExitQuote({
                owner: alice,
                shares: shares,
                unwindCostAssets: 0,
                deadline: deadline,
                adapter: address(adapter),
                dataHash: keccak256(data),
                nonce: 0
            })
        );
        (uint8 badV, bytes32 badR, bytes32 badS) = vm.sign(0xBAD, digest);
        bytes memory badSignature = abi.encodePacked(badR, badS, badV);
        vm.expectPartialRevert(ParVault.InvalidQuoteSigner.selector);
        vm.prank(alice);
        vault.redeemImmediate(shares, 0, deadline, address(adapter), data, 0, badSignature);

        bytes memory signature = _signImmediateExit(alice, shares, 0, deadline, address(adapter), data, 0);
        vm.prank(alice);
        vault.redeemImmediate(shares, 0, deadline, address(adapter), data, 0, signature);

        vm.expectPartialRevert(ParVault.InvalidQuoteNonce.selector);
        vm.prank(alice);
        vault.redeemImmediate(shares, 0, deadline, address(adapter), data, 0, signature);
    }

    function testClaimScheduledExitUsesNavAtClaimTime() public {
        _deposit(1_000 * USDC);
        uint256 shares = vault.balanceOf(alice) / 4;
        vm.prank(alice);
        uint256 id = vault.requestScheduledExit(shares);

        usdc.mint(address(vault), 100 * USDC);
        uint256 expected = vault.previewRedeem(shares);
        uint256 beforeBalance = usdc.balanceOf(alice);
        vault.claimScheduledExit(id);
        assertEq(usdc.balanceOf(alice) - beforeBalance, expected);
    }

    function testEveryPrivilegedFunctionRejectsWrongRole() public {
        bytes4 unauthorized = IAccessControl.AccessControlUnauthorizedAccount.selector;

        vm.startPrank(outsider);
        vm.expectPartialRevert(unauthorized);
        vault.setWhitelist(outsider, true);
        vm.expectPartialRevert(unauthorized);
        vault.setAdapter(adapter, false);
        vm.expectPartialRevert(unauthorized);
        vault.setMaximumUnwindCostBps(100);
        vm.expectPartialRevert(unauthorized);
        vault.allocate(ParVault.Job.CARRY, address(adapter), USDC, _pendleInvestData());
        vm.expectPartialRevert(unauthorized);
        vault.divest(address(adapter), USDC, _pendleDivestData());
        vm.expectPartialRevert(unauthorized);
        vault.redeemMatured(address(adapter), "");
        vm.stopPrank();

        vm.startPrank(outsider);
        vm.expectPartialRevert(unauthorized);
        guard.setAssetPolicy(address(pt), false, 0, oracle, 0);
        vm.expectPartialRevert(unauthorized);
        guard.setJobCap(0, 0);
        vm.expectPartialRevert(unauthorized);
        guard.setMinimumReserveBps(0);
        vm.expectPartialRevert(unauthorized);
        guard.tripCircuitBreaker();
        vm.expectPartialRevert(unauthorized);
        guard.clearCircuitBreaker();
        vm.expectPartialRevert(unauthorized);
        guard.raiseIncident(address(pt));
        vm.expectPartialRevert(unauthorized);
        guard.clearIncident(address(pt));
        vm.stopPrank();

        vm.expectPartialRevert(unauthorized);
        vm.prank(guardian);
        guard.clearCircuitBreaker();
        vm.expectPartialRevert(unauthorized);
        vm.prank(sentinel);
        guard.tripCircuitBreaker();
    }

    function testFuzzAccountingTracksAdapterSumAcrossAllocateAndDivest(
        uint96 rawDeposit,
        uint96 rawAllocation,
        uint96 rawDivest,
        uint96 rawExit
    ) public {
        uint256 depositAssets = bound(uint256(rawDeposit), 100 * USDC, 1_000_000 * USDC);
        _deposit(depositAssets);
        uint256 allocation = bound(uint256(rawAllocation), USDC, depositAssets * 70 / 100);

        vm.prank(allocator);
        vault.allocate(ParVault.Job.CARRY, address(adapter), allocation, _pendleInvestData());
        assertEq(vault.totalAssets(), vault.idleReserve() + adapter.positionValue());

        uint256 position = adapter.positionValue();
        uint256 divestAssets = bound(uint256(rawDivest), 1, position);
        uint256 beforeAssets = vault.totalAssets();
        vm.prank(allocator);
        vault.divest(address(adapter), divestAssets, _pendleDivestData());
        uint256 afterAssets = vault.totalAssets();

        assertEq(afterAssets, vault.idleReserve() + adapter.positionValue());
        assertLe(afterAssets, beforeAssets);

        uint256 exitCapacity = vault.convertToShares(vault.idleReserve());
        if (exitCapacity > vault.balanceOf(alice)) exitCapacity = vault.balanceOf(alice);
        uint256 exitShares = bound(uint256(rawExit), 1, exitCapacity);
        vm.prank(alice);
        uint256 exitId = vault.requestScheduledExit(exitShares);
        vault.claimScheduledExit(exitId);
        assertEq(vault.totalAssets(), vault.idleReserve() + adapter.positionValue());

        uint256 shares = vault.totalSupply();
        uint256 roundTripShares = vault.convertToShares(vault.convertToAssets(shares));
        assertLe(roundTripShares, shares);
        assertLe(shares - roundTripShares, 1e6, "rounding is bounded by one virtual-share unit");
    }

    function _deposit(uint256 assets) internal returns (uint256 shares) {
        vm.prank(alice);
        return vault.deposit(assets, alice);
    }

    function _context(
        uint256 assets,
        uint256 totalAssets_,
        uint256 reserve,
        uint256 assetExposure_,
        uint256 jobExposure_
    ) internal view returns (RiskGuard.AllocationContext memory) {
        return RiskGuard.AllocationContext({
            asset: address(pt),
            job: uint8(ParVault.Job.CARRY),
            assets: assets,
            totalAssets: totalAssets_,
            idleReserve: reserve,
            currentAssetExposure: assetExposure_,
            currentJobExposure: jobExposure_
        });
    }

    function _pendleInvestData() internal pure returns (bytes memory) {
        return abi.encode(uint256(0), bytes(""), bytes(""));
    }

    function _pendleDivestData() internal pure returns (bytes memory) {
        return abi.encode(uint256(0), bytes(""));
    }

    function _exactPendleLoss(uint256 requested) internal view returns (uint256) {
        uint256 discount = market.currentDiscountBps();
        uint256 faceBefore = market.faceValueOf(address(adapter));
        uint256 positionBefore = market.positionValue(address(adapter));
        uint256 faceBurn = Math.mulDiv(requested, 10_000, 10_000 - discount, Math.Rounding.Ceil);
        if (faceBurn > faceBefore) faceBurn = faceBefore;
        uint256 positionAfter = (faceBefore - faceBurn) * (10_000 - discount) / 10_000;
        uint256 positionReduction = positionBefore - positionAfter;
        uint256 assetsOut = requested * (10_000 - market.currentExitSlippageBps()) / 10_000;
        return positionReduction - assetsOut;
    }

    function _signImmediateExit(
        address owner,
        uint256 shares,
        uint256 unwindCostAssets,
        uint256 deadline,
        address adapter_,
        bytes memory data,
        uint256 nonce
    ) internal view returns (bytes memory signature) {
        bytes32 digest = vault.immediateExitDigest(
            ParVault.ImmediateExitQuote({
                owner: owner,
                shares: shares,
                unwindCostAssets: unwindCostAssets,
                deadline: deadline,
                adapter: adapter_,
                dataHash: keccak256(data),
                nonce: nonce
            })
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(allocatorKey, digest);
        return abi.encodePacked(r, s, v);
    }
}
