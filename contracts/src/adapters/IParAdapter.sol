// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

interface IParAdapter {
    function asset() external view returns (address);
    function maturity() external view returns (uint64);
    function invest(uint256 assets, bytes calldata data) external returns (uint256 assetsInvested);
    function divest(uint256 assets, bytes calldata data) external returns (uint256 assetsOut);
    function positionValue() external view returns (uint256);
    function redeemMatured(bytes calldata data) external returns (uint256 assetsOut);
}
