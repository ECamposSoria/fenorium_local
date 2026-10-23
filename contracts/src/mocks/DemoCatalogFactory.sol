// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {DemoCatalogDesk} from "./DemoCatalogDesk.sol";
import {DemoCatalogToken} from "./DemoCatalogToken.sol";

/// @title DemoCatalogFactory
/// @notice Testnet-only. Lists the Fenorium testnet catalog on-chain: for each instrument it clones
///         one DemoCatalogToken and one DemoCatalogDesk (EIP-1167), lets the desk mint the token,
///         makes the desk the token's transfer agent, and hands both to the caller as admin.
///         Clones keep a 132-instrument catalog within a testnet gas budget that whole contracts
///         would exceed.
contract DemoCatalogFactory is AccessControl {
    struct Spec {
        string name;
        string symbol;
        bool gated;
        uint256 priceMicros;
    }

    address public immutable tokenImplementation;
    address public immutable deskImplementation;
    IERC20 public immutable paymentToken;
    /// @notice Verification registry a gated instrument and its desk consult (isVerified).
    address public immutable gateRegistry;

    address[] public instruments;
    mapping(address instrument => address desk) public deskFor;

    error InvalidAddress();
    error UnknownGateRegistry();

    event Listed(
        uint256 indexed index,
        address indexed instrument,
        address indexed desk,
        string symbol,
        bool gated,
        uint256 priceMicros
    );

    constructor(IERC20 paymentToken_, address gateRegistry_, address admin) {
        if (address(paymentToken_) == address(0) || admin == address(0)) revert InvalidAddress();
        tokenImplementation = address(new DemoCatalogToken());
        deskImplementation = address(new DemoCatalogDesk());
        paymentToken = paymentToken_;
        gateRegistry = gateRegistry_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    function count() external view returns (uint256) {
        return instruments.length;
    }

    /// @notice Lists each spec; the caller becomes admin of every token and desk it creates.
    function list(Spec[] calldata specs) external onlyRole(DEFAULT_ADMIN_ROLE) {
        for (uint256 i = 0; i < specs.length; i++) {
            Spec calldata spec = specs[i];
            address registry = address(0);
            if (spec.gated) {
                if (gateRegistry == address(0)) revert UnknownGateRegistry();
                registry = gateRegistry;
            }

            DemoCatalogToken token = DemoCatalogToken(Clones.clone(tokenImplementation));
            DemoCatalogDesk desk = DemoCatalogDesk(Clones.clone(deskImplementation));
            token.initialize(spec.name, spec.symbol, address(this), registry);
            desk.initialize(paymentToken, IERC20(address(token)), registry, spec.priceMicros, msg.sender);

            token.grantRole(token.MINTER_ROLE(), address(desk));
            token.setTransferAgent(address(desk));
            token.grantRole(token.DEFAULT_ADMIN_ROLE(), msg.sender);
            token.renounceRole(token.DEFAULT_ADMIN_ROLE(), address(this));

            deskFor[address(token)] = address(desk);
            instruments.push(address(token));
            emit Listed(instruments.length - 1, address(token), address(desk), spec.symbol, spec.gated, spec.priceMicros);
        }
    }
}
