// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {DemoInstrumentSale} from "../src/mocks/DemoInstrumentSale.sol";
import {MockUSDC} from "../src/mocks/MockUSDC.sol";

interface IRoleGranter {
    function grantRole(bytes32 role, address account) external;
}

/// @notice Deploys one MockUSDC and a purchase sale for each existing Base Sepolia demo
///         instrument. This script is intentionally separate from DeployDemoInstruments.
contract DeployDemoSales is Script {
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;
    uint256 internal constant DEFAULT_PRICE_MICROS = 1_000_000;
    bytes32 internal constant MINTER_ROLE = keccak256("MINTER_ROLE");

    address internal constant GATED_INSTRUMENT = 0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54;
    address internal constant PERMISSIONLESS_INSTRUMENT = 0x5C8Ea45363e98e8647eE3759ac888E12817E86a3;

    function run() external {
        if (block.chainid != BASE_SEPOLIA_CHAIN_ID) {
            revert("demo sales are for Base Sepolia only");
        }

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(privateKey);
        address gatedInstrument = vm.envOr("DEMO_GATED_INSTRUMENT", GATED_INSTRUMENT);
        address permissionlessInstrument = vm.envOr("DEMO_PERMISSIONLESS_INSTRUMENT", PERMISSIONLESS_INSTRUMENT);
        // The fallback is a clearly labeled demo price, not a claim about market value.
        address proceedsRecipient = vm.envOr("DEMO_SALE_PROCEEDS_RECIPIENT", deployer);
        uint256 priceMicros = vm.envOr("DEMO_SALE_PRICE_MICROS", DEFAULT_PRICE_MICROS);

        vm.startBroadcast(privateKey);

        MockUSDC usdc = new MockUSDC();
        DemoInstrumentSale gatedSale =
            new DemoInstrumentSale(usdc, IERC20(gatedInstrument), proceedsRecipient, gatedInstrument, priceMicros);
        DemoInstrumentSale permissionlessSale =
            new DemoInstrumentSale(usdc, IERC20(permissionlessInstrument), proceedsRecipient, address(0), priceMicros);

        IRoleGranter(gatedInstrument).grantRole(MINTER_ROLE, address(gatedSale));
        IRoleGranter(permissionlessInstrument).grantRole(MINTER_ROLE, address(permissionlessSale));

        vm.stopBroadcast();

        console2.log("chain id                    ", block.chainid);
        console2.log("payment token               ", address(usdc));
        console2.log("proceeds recipient          ", proceedsRecipient);
        console2.log("price micros / whole token ", priceMicros);
        console2.log("gated instrument            ", gatedInstrument);
        console2.log("gated eligibility checker   ", gatedInstrument);
        console2.log("gated sale                  ", address(gatedSale));
        console2.log("permissionless instrument  ", permissionlessInstrument);
        console2.log("permissionless checker     ", address(0));
        console2.log("permissionless sale         ", address(permissionlessSale));
    }
}
