// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

interface IParOracle {
    function price(address asset) external view returns (uint256 price_, uint256 updatedAt);
}
