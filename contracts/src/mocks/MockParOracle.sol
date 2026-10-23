// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

import {IParOracle} from "../interfaces/IParOracle.sol";

contract MockParOracle is IParOracle, AccessControl {
    struct PriceData {
        uint256 value;
        uint256 updatedAt;
    }

    mapping(address asset => PriceData) public prices;

    event PriceSet(address indexed asset, uint256 value, uint256 updatedAt);

    constructor(address admin) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    function setPrice(address asset, uint256 value, uint256 updatedAt) external onlyRole(DEFAULT_ADMIN_ROLE) {
        prices[asset] = PriceData(value, updatedAt);
        emit PriceSet(asset, value, updatedAt);
    }

    function price(address asset) external view returns (uint256 price_, uint256 updatedAt) {
        PriceData memory data = prices[asset];
        return (data.value, data.updatedAt);
    }
}
