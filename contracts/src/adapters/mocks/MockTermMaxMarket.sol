// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {MockFixedRateMarket} from "./MockFixedRateMarket.sol";

contract MockTermMaxMarket is MockFixedRateMarket {
    constructor(
        IERC20 underlying_,
        address ft_,
        uint64 maturity_,
        uint16 initialDiscountBps_,
        uint16 maximumEarlyExitSlippageBps_
    ) MockFixedRateMarket(underlying_, ft_, maturity_, initialDiscountBps_, maximumEarlyExitSlippageBps_) {}

    // Stands in for taking a TermMax FT/XT order; the lender receives FT and no residual XT here.
    function takeOrder(address receiver, uint256 exactTokenIn, uint256 minFtOut, bytes calldata orderData)
        external
        returns (uint256 ftOut, uint256 xtOut)
    {
        orderData;
        ftOut = _buy(receiver, exactTokenIn, minFtOut);
        xtOut = 0;
    }

    // Stands in for filling the opposite side of the FT order book before maturity.
    function sellFt(address receiver, uint256 exactPositionValueIn, uint256 minTokenOut, bytes calldata orderData)
        external
        returns (uint256 tokenOut)
    {
        orderData;
        return _sell(msg.sender, receiver, exactPositionValueIn, minTokenOut);
    }

    function redeemFt(address receiver) external returns (uint256 tokenOut) {
        return _redeem(msg.sender, receiver);
    }
}
