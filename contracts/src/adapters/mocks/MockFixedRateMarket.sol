// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

abstract contract MockFixedRateMarket {
    using SafeERC20 for IERC20;

    uint256 internal constant BPS = 10_000;

    IERC20 public immutable underlying;
    address public immutable fixedToken;
    uint64 public immutable startTime;
    uint64 public immutable maturity;
    uint16 public immutable initialDiscountBps;
    uint16 public immutable maximumEarlyExitSlippageBps;

    mapping(address account => uint256 faceValue) public faceValueOf;

    error AlreadyMatured();
    error NotMatured();
    error MinimumOutputNotMet(uint256 output, uint256 minimum);
    error InvalidMarketParameters();

    constructor(
        IERC20 underlying_,
        address fixedToken_,
        uint64 maturity_,
        uint16 initialDiscountBps_,
        uint16 maximumEarlyExitSlippageBps_
    ) {
        if (
            address(underlying_) == address(0) || fixedToken_ == address(0) || maturity_ <= block.timestamp
                || initialDiscountBps_ == 0 || initialDiscountBps_ >= BPS || maximumEarlyExitSlippageBps_ == 0
                || maximumEarlyExitSlippageBps_ >= BPS
        ) revert InvalidMarketParameters();
        underlying = underlying_;
        fixedToken = fixedToken_;
        startTime = uint64(block.timestamp);
        maturity = maturity_;
        initialDiscountBps = initialDiscountBps_;
        maximumEarlyExitSlippageBps = maximumEarlyExitSlippageBps_;
    }

    function positionValue(address account) public view returns (uint256) {
        return faceValueOf[account] * (BPS - currentDiscountBps()) / BPS;
    }

    function currentDiscountBps() public view returns (uint256) {
        if (block.timestamp >= maturity) return 0;
        uint256 duration = maturity - startTime;
        uint256 remaining = maturity - block.timestamp;
        return uint256(initialDiscountBps) * remaining / duration;
    }

    function currentExitSlippageBps() public view returns (uint256) {
        if (block.timestamp >= maturity) return 0;
        uint256 duration = maturity - startTime;
        uint256 remaining = maturity - block.timestamp;
        return uint256(maximumEarlyExitSlippageBps) * remaining / duration;
    }

    function _buy(address receiver, uint256 assets, uint256 minimumFaceValue) internal returns (uint256 faceValue) {
        if (block.timestamp >= maturity) revert AlreadyMatured();
        uint256 discount = currentDiscountBps();
        faceValue = assets * BPS / (BPS - discount);
        if (faceValue < minimumFaceValue) revert MinimumOutputNotMet(faceValue, minimumFaceValue);
        underlying.safeTransferFrom(msg.sender, address(this), assets);
        faceValueOf[receiver] += faceValue;
    }

    function _sell(address owner, address receiver, uint256 positionAssets, uint256 minimumAssetsOut)
        internal
        returns (uint256 assetsOut)
    {
        uint256 value = positionValue(owner);
        uint256 valueToSell = Math.min(positionAssets, value);
        uint256 discount = currentDiscountBps();
        uint256 faceToBurn = Math.mulDiv(valueToSell, BPS, BPS - discount, Math.Rounding.Ceil);
        faceToBurn = Math.min(faceToBurn, faceValueOf[owner]);
        assetsOut = valueToSell * (BPS - currentExitSlippageBps()) / BPS;
        if (assetsOut < minimumAssetsOut) revert MinimumOutputNotMet(assetsOut, minimumAssetsOut);
        faceValueOf[owner] -= faceToBurn;
        underlying.safeTransfer(receiver, assetsOut);
    }

    function _redeem(address owner, address receiver) internal returns (uint256 assetsOut) {
        if (block.timestamp < maturity) revert NotMatured();
        assetsOut = faceValueOf[owner];
        faceValueOf[owner] = 0;
        underlying.safeTransfer(receiver, assetsOut);
    }
}
