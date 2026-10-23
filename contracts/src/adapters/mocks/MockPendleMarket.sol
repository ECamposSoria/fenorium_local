// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {MockFixedRateMarket} from "./MockFixedRateMarket.sol";

contract MockPendleMarket is MockFixedRateMarket {
    constructor(
        IERC20 underlying_,
        address pt_,
        uint64 maturity_,
        uint16 initialDiscountBps_,
        uint16 maximumEarlyExitSlippageBps_
    ) MockFixedRateMarket(underlying_, pt_, maturity_, initialDiscountBps_, maximumEarlyExitSlippageBps_) {}

    // Stands in for Pendle Router's swapExactTokenForPt call and approximation/limit-order payloads.
    function swapExactTokenForPt(
        address receiver,
        uint256 exactTokenIn,
        uint256 minPtOut,
        bytes calldata approxParams,
        bytes calldata limitOrderData
    ) external returns (uint256 netPtOut) {
        approxParams;
        limitOrderData;
        return _buy(receiver, exactTokenIn, minPtOut);
    }

    // Stands in for the inverse Pendle Router PT-to-token swap used before maturity.
    function swapExactPtForToken(
        address receiver,
        uint256 exactPositionValueIn,
        uint256 minTokenOut,
        bytes calldata limitOrderData
    ) external returns (uint256 netTokenOut) {
        limitOrderData;
        return _sell(msg.sender, receiver, exactPositionValueIn, minTokenOut);
    }

    function redeemPyToToken(address receiver) external returns (uint256 netTokenOut) {
        return _redeem(msg.sender, receiver);
    }
}
