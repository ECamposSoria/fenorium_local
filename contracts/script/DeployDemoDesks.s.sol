// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {DemoInstrumentDesk} from "../src/mocks/DemoInstrumentDesk.sol";

interface IRoleGranter {
    function grantRole(bytes32 role, address account) external;
}

interface IPriced {
    function setPrice(uint256 newPrice) external;
}

/// @notice Deploys a two-way desk for each Base Sepolia demo instrument against the existing
///         mUSDC, lets each desk mint its instrument, and closes the buy-only sales so there is one
///         place to buy and sell. The gated desk still needs its credential: issue the demo
///         attestation to its address (IssueDemoAttestation, DEMO_ATTESTATION_RECIPIENT) and call
///         `registerCredential(uid)` on it afterwards.
contract DeployDemoDesks is Script {
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84_532;
    uint256 internal constant DEFAULT_PRICE_MICROS = 1_000_000;
    bytes32 internal constant MINTER_ROLE = keccak256("MINTER_ROLE");

    address internal constant PAYMENT_TOKEN = 0xfECb2EfF5603b44daD440561cA3cD889F29239ac;
    address internal constant GATED_INSTRUMENT = 0xe72A18aDF4Fdb9DF991ad4218B7199d324214f54;
    address internal constant PERMISSIONLESS_INSTRUMENT = 0x5C8Ea45363e98e8647eE3759ac888E12817E86a3;
    address internal constant GATED_SALE = 0x4B0b98996DB072cC05765DfD256459fD6972D619;
    address internal constant PERMISSIONLESS_SALE = 0x9829ba1B645BE0f8EC27F4679D533C3dbb8De194;

    function run() external {
        if (block.chainid != BASE_SEPOLIA_CHAIN_ID) {
            revert("demo desks are for Base Sepolia only");
        }

        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address paymentToken = vm.envOr("DEMO_PAYMENT_TOKEN", PAYMENT_TOKEN);
        address gatedInstrument = vm.envOr("DEMO_GATED_INSTRUMENT", GATED_INSTRUMENT);
        address permissionlessInstrument = vm.envOr("DEMO_PERMISSIONLESS_INSTRUMENT", PERMISSIONLESS_INSTRUMENT);
        address gatedSale = vm.envOr("DEMO_GATED_SALE", GATED_SALE);
        address permissionlessSale = vm.envOr("DEMO_PERMISSIONLESS_SALE", PERMISSIONLESS_SALE);
        uint256 priceMicros = vm.envOr("DEMO_DESK_PRICE_MICROS", DEFAULT_PRICE_MICROS);
        bool closeSales = vm.envOr("DEMO_DESK_CLOSE_SALES", true);

        vm.startBroadcast(privateKey);

        DemoInstrumentDesk gatedDesk =
            new DemoInstrumentDesk(IERC20(paymentToken), IERC20(gatedInstrument), gatedInstrument, priceMicros);
        DemoInstrumentDesk permissionlessDesk =
            new DemoInstrumentDesk(IERC20(paymentToken), IERC20(permissionlessInstrument), address(0), priceMicros);

        IRoleGranter(gatedInstrument).grantRole(MINTER_ROLE, address(gatedDesk));
        IRoleGranter(permissionlessInstrument).grantRole(MINTER_ROLE, address(permissionlessDesk));

        if (closeSales) {
            IPriced(gatedSale).setPrice(0);
            IPriced(permissionlessSale).setPrice(0);
        }

        vm.stopBroadcast();

        console2.log("chain id                    ", block.chainid);
        console2.log("payment token               ", paymentToken);
        console2.log("price micros / whole token ", priceMicros);
        console2.log("gated instrument            ", gatedInstrument);
        console2.log("gated desk                  ", address(gatedDesk));
        console2.log("permissionless instrument  ", permissionlessInstrument);
        console2.log("permissionless desk         ", address(permissionlessDesk));
        console2.log("buy-only sales closed       ", closeSales);
    }
}
