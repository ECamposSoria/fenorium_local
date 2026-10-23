// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

interface IVerificationRegistry {
    function isVerified(address wallet) external view returns (bool);
}

/// @title DemoCatalogToken
/// @notice A testnet-only six-decimal instrument of the Fenorium testnet catalog, deployed as an
///         EIP-1167 clone by DemoCatalogFactory. Its economics (rate, maturity, tier) are declared
///         off-chain demo terms; the contract only holds balances, mints for its desk and, when
///         gated, refuses transfers to or from a wallet the verification registry does not know.
/// @dev The implementation locks itself in the constructor; each clone is initialized exactly once.
///      Name and symbol live in this contract's storage because ERC20's own are set by a
///      constructor a clone never runs. The transfer agent (the instrument's desk) is exempt from
///      the gate so a buy-back can settle without the desk holding a credential of its own.
contract DemoCatalogToken is ERC20, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    string private _catalogName;
    string private _catalogSymbol;
    bool private _initialized;

    /// @notice Registry consulted on every transfer when non-zero (the gate); zero is permissionless.
    address public gateRegistry;
    /// @notice The desk that sells and buys back this instrument; exempt from the gate.
    address public transferAgent;

    error AlreadyInitialized();
    error InvalidAddress();
    error NotVerified(address wallet);

    event TransferAgentSet(address indexed agent);

    constructor() ERC20("", "") {
        _initialized = true;
    }

    function initialize(string calldata name_, string calldata symbol_, address admin, address gateRegistry_)
        external
    {
        if (_initialized) revert AlreadyInitialized();
        if (admin == address(0)) revert InvalidAddress();
        _initialized = true;
        _catalogName = name_;
        _catalogSymbol = symbol_;
        gateRegistry = gateRegistry_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    function name() public view override returns (string memory) {
        return _catalogName;
    }

    function symbol() public view override returns (string memory) {
        return _catalogSymbol;
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address to, uint256 amount) external onlyRole(MINTER_ROLE) {
        _mint(to, amount);
    }

    function setTransferAgent(address agent) external onlyRole(DEFAULT_ADMIN_ROLE) {
        transferAgent = agent;
        emit TransferAgentSet(agent);
    }

    /// @notice Whether `wallet` may hold this instrument: always when ungated, else the registry's answer.
    function isVerified(address wallet) public view returns (bool) {
        address registry = gateRegistry;
        if (registry == address(0) || wallet == transferAgent) return true;
        return IVerificationRegistry(registry).isVerified(wallet);
    }

    function _update(address from, address to, uint256 value) internal override {
        if (gateRegistry != address(0)) {
            if (from != address(0) && !isVerified(from)) revert NotVerified(from);
            if (to != address(0) && !isVerified(to)) revert NotVerified(to);
        }
        super._update(from, to, value);
    }
}
