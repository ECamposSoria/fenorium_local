// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IParAdapter} from "./IParAdapter.sol";
import {MockPendleMarket} from "./mocks/MockPendleMarket.sol";

contract PendleAdapter is IParAdapter {
    using SafeERC20 for IERC20;

    address public immutable vault;
    IERC20 public immutable reserveAsset;
    MockPendleMarket public immutable market;

    error OnlyVault();

    constructor(address vault_, IERC20 reserveAsset_, MockPendleMarket market_) {
        vault = vault_;
        reserveAsset = reserveAsset_;
        market = market_;
        reserveAsset_.forceApprove(address(market_), type(uint256).max);
    }

    modifier onlyVault() {
        if (msg.sender != vault) revert OnlyVault();
        _;
    }

    function asset() external view returns (address) {
        return market.fixedToken();
    }

    function maturity() external view returns (uint64) {
        return market.maturity();
    }

    function invest(uint256 assets, bytes calldata data) external onlyVault returns (uint256 assetsInvested) {
        (uint256 minPtOut, bytes memory approxParams, bytes memory limitOrderData) =
            abi.decode(data, (uint256, bytes, bytes));
        market.swapExactTokenForPt(address(this), assets, minPtOut, approxParams, limitOrderData);
        return assets;
    }

    function divest(uint256 assets, bytes calldata data) external onlyVault returns (uint256 assetsOut) {
        (uint256 minTokenOut, bytes memory limitOrderData) = abi.decode(data, (uint256, bytes));
        return market.swapExactPtForToken(vault, assets, minTokenOut, limitOrderData);
    }

    function positionValue() external view returns (uint256) {
        return market.positionValue(address(this));
    }

    function redeemMatured(bytes calldata) external onlyVault returns (uint256 assetsOut) {
        return market.redeemPyToToken(vault);
    }
}
