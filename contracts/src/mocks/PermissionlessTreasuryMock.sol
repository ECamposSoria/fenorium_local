// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title PermissionlessTreasuryMock — the unrestricted half of the Base Sepolia demo.
/// @notice Not a real fund and not affiliated with any issuer. It exists so the demo has an
///         instrument that behaves the way Par measured Ondo's USDY and Backed's bIB01 behaving:
///         a transfer from an empty wallet reaches the balance check, because nothing gates it.
///         That contrast is the point — the same probe distinguishes the two without any
///         off-chain knowledge of either.
contract PermissionlessTreasuryMock is ERC20, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    constructor() ERC20("Mock Permissionless Treasury", "mPT") {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(MINTER_ROLE, msg.sender);
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address to, uint256 amount) external onlyRole(MINTER_ROLE) {
        _mint(to, amount);
    }

    /// @notice Testnet self-serve, ungated — mirrors how the real permissionless leg needs no
    ///         permission. Same cap as the gated mock so the two rows behave symmetrically.
    uint256 public constant CLAIM_CAP = 10_000_000e6;

    error ClaimTooLarge(uint256 amount, uint256 cap);

    function claim(uint256 amount) external {
        if (amount > CLAIM_CAP) revert ClaimTooLarge(amount, CLAIM_CAP);
        _mint(msg.sender, amount);
    }
}
