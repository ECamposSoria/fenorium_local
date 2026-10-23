// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IParAdapter} from "./IParAdapter.sol";

contract ReserveAdapter is IParAdapter {
    using SafeERC20 for IERC20;

    address public immutable vault;
    IERC20 public immutable reserveAsset;

    error OnlyVault();

    constructor(address vault_, IERC20 reserveAsset_) {
        vault = vault_;
        reserveAsset = reserveAsset_;
    }

    modifier onlyVault() {
        if (msg.sender != vault) revert OnlyVault();
        _;
    }

    function asset() external view returns (address) {
        return address(reserveAsset);
    }

    function maturity() external pure returns (uint64) {
        return 0;
    }

    function invest(uint256 assets, bytes calldata) external view onlyVault returns (uint256 assetsInvested) {
        return assets;
    }

    function divest(uint256 assets, bytes calldata) external onlyVault returns (uint256 assetsOut) {
        assetsOut = assets > reserveAsset.balanceOf(address(this)) ? reserveAsset.balanceOf(address(this)) : assets;
        reserveAsset.safeTransfer(vault, assetsOut);
    }

    function positionValue() external view returns (uint256) {
        return reserveAsset.balanceOf(address(this));
    }

    function redeemMatured(bytes calldata) external onlyVault returns (uint256 assetsOut) {
        assetsOut = reserveAsset.balanceOf(address(this));
        reserveAsset.safeTransfer(vault, assetsOut);
    }
}
