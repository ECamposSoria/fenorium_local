// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title DemoExposureMock — a named Base Sepolia demo instrument.
/// @notice Not a real fund, not affiliated with any issuer, and never deployed to a mainnet.
///         The two treasury mocks each hardcode their own name because each exists to prove one
///         specific behaviour. These exist for a different reason: the terminal groups a book by
///         asset class, and with only tokenized treasuries on the chain there was nothing to
///         group. One parameterised contract covers every further class the demo needs rather
///         than a near-copy per instrument.
contract DemoExposureMock is ERC20, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    uint8 private immutable _decimals;

    constructor(string memory name_, string memory symbol_, uint8 decimals_) ERC20(name_, symbol_) {
        _decimals = decimals_;
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(MINTER_ROLE, msg.sender);
    }

    function decimals() public view override returns (uint8) {
        return _decimals;
    }

    function mint(address to, uint256 amount) external onlyRole(MINTER_ROLE) {
        _mint(to, amount);
    }
}
