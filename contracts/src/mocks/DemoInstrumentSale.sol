// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IMintableInstrument {
    function mint(address to, uint256 amount) external;
}

interface IEligibilityChecker {
    function isVerified(address wallet) external view returns (bool);
}

/// @title DemoInstrumentSale
/// @notice A non-custodial, testnet-only purchase leg for a six-decimal instrument.
/// @dev Payment is transferred directly from the buyer to the proceeds recipient. The sale
///      contract never receives or holds buyer payment tokens. A zero checker means permissionless.
contract DemoInstrumentSale is AccessControl, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public constant TOKEN_SCALE = 1e6;

    IERC20 public immutable paymentToken;
    IERC20 public immutable instrument;
    address public immutable proceedsRecipient;
    address public immutable eligibilityChecker;

    /// @notice Payment-token micros required for one whole instrument token.
    uint256 public pricePerWholeInstrument;

    error InvalidAddress();
    error InvalidTokenDecimals(uint8 paymentDecimals, uint8 instrumentDecimals);
    error ZeroAmount();
    error PriceNotSet();
    error InsufficientPaymentBalance(uint256 available, uint256 required);
    error InsufficientPaymentAllowance(uint256 available, uint256 required);
    error PriceMovedAgainstBuyer(uint256 quoted, uint256 maxAccepted);
    error BuyerNotVerified(address buyer);
    error EligibilityCheckFailed();
    error PaymentFailed();
    error DeliveryFailed();

    event PriceSet(uint256 indexed oldPrice, uint256 indexed newPrice);
    event Purchased(
        address indexed buyer, address indexed instrument, uint256 instrumentAmount, uint256 paidAmount, uint256 price
    );

    constructor(
        IERC20 paymentToken_,
        IERC20 instrument_,
        address proceedsRecipient_,
        address eligibilityChecker_,
        uint256 initialPrice_
    ) {
        if (
            address(paymentToken_) == address(0) || address(instrument_) == address(0)
                || proceedsRecipient_ == address(0)
        ) {
            revert InvalidAddress();
        }

        uint8 paymentDecimals = IERC20Metadata(address(paymentToken_)).decimals();
        uint8 instrumentDecimals = IERC20Metadata(address(instrument_)).decimals();
        if (paymentDecimals != 6 || instrumentDecimals != 6) {
            revert InvalidTokenDecimals(paymentDecimals, instrumentDecimals);
        }

        paymentToken = paymentToken_;
        instrument = instrument_;
        proceedsRecipient = proceedsRecipient_;
        eligibilityChecker = eligibilityChecker_;
        pricePerWholeInstrument = initialPrice_;

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
    }

    /// @notice Quotes the payment-token atomic amount for an instrument atomic amount.
    /// @dev Both tokens use six decimals, so the price is scaled down once by TOKEN_SCALE.
    function quote(uint256 instrumentAmount) public view returns (uint256) {
        if (instrumentAmount == 0) revert ZeroAmount();

        uint256 price = pricePerWholeInstrument;
        if (price == 0) revert PriceNotSet();

        // Round a fractional payment micro up. A nonzero instrument amount must never become a
        // free mint merely because the payment token cannot represent a sub-micro amount.
        return Math.mulDiv(instrumentAmount, price, TOKEN_SCALE, Math.Rounding.Ceil);
    }

    /// @notice Sets the payment-token micros required for one whole instrument token.
    /// @dev Setting zero disables purchases until a nonzero price is configured.
    function setPrice(uint256 newPrice) external onlyRole(DEFAULT_ADMIN_ROLE) {
        uint256 oldPrice = pricePerWholeInstrument;
        pricePerWholeInstrument = newPrice;
        emit PriceSet(oldPrice, newPrice);
    }

    /// @notice Purchases instrumentAmount atomically when its quote is within the buyer's maximum.
    /// @dev Payment is sent directly to the treasury. There is deliberately no unbounded overload.
    function purchase(uint256 instrumentAmount, uint256 maxPaymentAmount) external nonReentrant {
        uint256 paymentAmount = quote(instrumentAmount);
        if (paymentAmount > maxPaymentAmount) {
            revert PriceMovedAgainstBuyer(paymentAmount, maxPaymentAmount);
        }
        uint256 acceptedPrice = pricePerWholeInstrument;
        _checkEligibility(msg.sender);

        uint256 buyerBalance = paymentToken.balanceOf(msg.sender);
        if (buyerBalance < paymentAmount) {
            revert InsufficientPaymentBalance(buyerBalance, paymentAmount);
        }

        uint256 buyerAllowance = paymentToken.allowance(msg.sender, address(this));
        if (buyerAllowance < paymentAmount) {
            revert InsufficientPaymentAllowance(buyerAllowance, paymentAmount);
        }

        if (!paymentToken.trySafeTransferFrom(msg.sender, proceedsRecipient, paymentAmount)) {
            revert PaymentFailed();
        }

        try IMintableInstrument(address(instrument)).mint(msg.sender, instrumentAmount) {}
        catch {
            revert DeliveryFailed();
        }

        emit Purchased(msg.sender, address(instrument), instrumentAmount, paymentAmount, acceptedPrice);
    }

    function _checkEligibility(address buyer) internal view {
        address checker = eligibilityChecker;
        if (checker == address(0)) return;

        try IEligibilityChecker(checker).isVerified(buyer) returns (bool verified) {
            if (!verified) revert BuyerNotVerified(buyer);
        } catch {
            revert EligibilityCheckFailed();
        }
    }
}
