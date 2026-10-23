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

interface IFaucetToken {
    function mint(address account, uint256 amount) external;
}

interface ICredentialHolder {
    function registerCredential(bytes32 uid) external;
}

/// @title DemoInstrumentDesk
/// @notice A testnet-only two-way desk for a six-decimal demo instrument. It sells the instrument
///         at `quote` and buys it back at `sellQuote`, keeps the payment it collects and pays
///         buy-backs from that balance. When the balance is short it mints the shortfall through
///         the demo payment token's open faucet: a testnet convenience, not a claim about
///         liquidity, and the reason this contract must never see a mainnet.
/// @dev The purchase leg mirrors DemoInstrumentSale (same errors, same event) with one change:
///      proceeds stay here instead of going to a recipient, so a buyer who is also the deployer no
///      longer pays itself. Bought-back instruments are held, not burned: the mocks cannot burn.
contract DemoInstrumentDesk is AccessControl, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public constant TOKEN_SCALE = 1e6;

    IERC20 public immutable paymentToken;
    IERC20 public immutable instrument;
    address public immutable eligibilityChecker;

    /// @notice Payment-token micros per whole instrument token, for both directions.
    uint256 public pricePerWholeInstrument;

    error InvalidAddress();
    error InvalidTokenDecimals(uint8 paymentDecimals, uint8 instrumentDecimals);
    error ZeroAmount();
    error PriceNotSet();
    error InsufficientPaymentBalance(uint256 available, uint256 required);
    error InsufficientPaymentAllowance(uint256 available, uint256 required);
    error InsufficientInstrumentBalance(uint256 available, uint256 required);
    error InsufficientInstrumentAllowance(uint256 available, uint256 required);
    error PriceMovedAgainstBuyer(uint256 quoted, uint256 maxAccepted);
    error PriceMovedAgainstSeller(uint256 quoted, uint256 minAccepted);
    error NotVerified(address wallet);
    error EligibilityCheckFailed();
    error PaymentFailed();
    error DeliveryFailed();
    error SettlementFailed();
    error FaucetFailed();

    event PriceSet(uint256 indexed oldPrice, uint256 indexed newPrice);
    event Purchased(
        address indexed buyer, address indexed instrument, uint256 instrumentAmount, uint256 paidAmount, uint256 price
    );
    event Sold(
        address indexed seller, address indexed instrument, uint256 instrumentAmount, uint256 receivedAmount, uint256 price
    );
    event ShortfallMinted(uint256 amount);
    event Swept(address indexed token, address indexed to, uint256 amount);
    event CredentialRegistered(bytes32 indexed uid);

    constructor(IERC20 paymentToken_, IERC20 instrument_, address eligibilityChecker_, uint256 initialPrice_) {
        if (address(paymentToken_) == address(0) || address(instrument_) == address(0)) revert InvalidAddress();

        uint8 paymentDecimals = IERC20Metadata(address(paymentToken_)).decimals();
        uint8 instrumentDecimals = IERC20Metadata(address(instrument_)).decimals();
        if (paymentDecimals != 6 || instrumentDecimals != 6) {
            revert InvalidTokenDecimals(paymentDecimals, instrumentDecimals);
        }

        paymentToken = paymentToken_;
        instrument = instrument_;
        eligibilityChecker = eligibilityChecker_;
        pricePerWholeInstrument = initialPrice_;

        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
    }

    /// @notice Payment micros a buyer pays for `instrumentAmount`; a fractional micro rounds up.
    function quote(uint256 instrumentAmount) public view returns (uint256) {
        return Math.mulDiv(instrumentAmount, _price(instrumentAmount), TOKEN_SCALE, Math.Rounding.Ceil);
    }

    /// @notice Payment micros a seller receives for `instrumentAmount`; a fractional micro rounds
    ///         down. Rounding never favours the caller in either direction.
    function sellQuote(uint256 instrumentAmount) public view returns (uint256) {
        return Math.mulDiv(instrumentAmount, _price(instrumentAmount), TOKEN_SCALE, Math.Rounding.Floor);
    }

    /// @notice Sets the price for both directions. Zero closes the desk until a price is set.
    function setPrice(uint256 newPrice) external onlyRole(DEFAULT_ADMIN_ROLE) {
        uint256 oldPrice = pricePerWholeInstrument;
        pricePerWholeInstrument = newPrice;
        emit PriceSet(oldPrice, newPrice);
    }

    /// @notice Buys `instrumentAmount` when its quote is within the buyer's maximum. The payment
    ///         stays in this desk; the instrument is minted to the buyer.
    function purchase(uint256 instrumentAmount, uint256 maxPaymentAmount) external nonReentrant {
        uint256 paymentAmount = quote(instrumentAmount);
        if (paymentAmount > maxPaymentAmount) revert PriceMovedAgainstBuyer(paymentAmount, maxPaymentAmount);
        uint256 acceptedPrice = pricePerWholeInstrument;
        _checkEligibility(msg.sender);

        uint256 buyerBalance = paymentToken.balanceOf(msg.sender);
        if (buyerBalance < paymentAmount) revert InsufficientPaymentBalance(buyerBalance, paymentAmount);
        uint256 buyerAllowance = paymentToken.allowance(msg.sender, address(this));
        if (buyerAllowance < paymentAmount) revert InsufficientPaymentAllowance(buyerAllowance, paymentAmount);

        if (!paymentToken.trySafeTransferFrom(msg.sender, address(this), paymentAmount)) revert PaymentFailed();
        try IMintableInstrument(address(instrument)).mint(msg.sender, instrumentAmount) {}
        catch {
            revert DeliveryFailed();
        }

        emit Purchased(msg.sender, address(instrument), instrumentAmount, paymentAmount, acceptedPrice);
    }

    /// @notice Sells `instrumentAmount` back when its quote reaches the seller's minimum. The
    ///         instrument moves into this desk; the payment comes from the desk's balance, topped
    ///         up through the faucet when short.
    function sell(uint256 instrumentAmount, uint256 minPaymentAmount) external nonReentrant {
        uint256 paymentAmount = sellQuote(instrumentAmount);
        if (paymentAmount < minPaymentAmount) revert PriceMovedAgainstSeller(paymentAmount, minPaymentAmount);
        uint256 acceptedPrice = pricePerWholeInstrument;
        _checkEligibility(msg.sender);

        uint256 sellerBalance = instrument.balanceOf(msg.sender);
        if (sellerBalance < instrumentAmount) revert InsufficientInstrumentBalance(sellerBalance, instrumentAmount);
        uint256 sellerAllowance = instrument.allowance(msg.sender, address(this));
        if (sellerAllowance < instrumentAmount) {
            revert InsufficientInstrumentAllowance(sellerAllowance, instrumentAmount);
        }

        if (!instrument.trySafeTransferFrom(msg.sender, address(this), instrumentAmount)) revert SettlementFailed();

        uint256 held = paymentToken.balanceOf(address(this));
        if (held < paymentAmount) {
            uint256 shortfall = paymentAmount - held;
            try IFaucetToken(address(paymentToken)).mint(address(this), shortfall) {}
            catch {
                revert FaucetFailed();
            }
            emit ShortfallMinted(shortfall);
        }
        if (!paymentToken.trySafeTransfer(msg.sender, paymentAmount)) revert PaymentFailed();

        emit Sold(msg.sender, address(instrument), instrumentAmount, paymentAmount, acceptedPrice);
    }

    /// @notice Presents the desk's own credential to a gated instrument, so the desk may hold
    ///         what it buys back. The instrument verifies the attestation itself.
    function registerCredential(bytes32 uid) external onlyRole(DEFAULT_ADMIN_ROLE) {
        ICredentialHolder(address(instrument)).registerCredential(uid);
        emit CredentialRegistered(uid);
    }

    /// @notice Moves tokens the desk holds, so a demo's balances never strand here.
    function sweep(IERC20 token, address to, uint256 amount) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (to == address(0)) revert InvalidAddress();
        token.safeTransfer(to, amount);
        emit Swept(address(token), to, amount);
    }

    function _price(uint256 instrumentAmount) internal view returns (uint256 price) {
        if (instrumentAmount == 0) revert ZeroAmount();
        price = pricePerWholeInstrument;
        if (price == 0) revert PriceNotSet();
    }

    function _checkEligibility(address wallet) internal view {
        address checker = eligibilityChecker;
        if (checker == address(0)) return;
        try IEligibilityChecker(checker).isVerified(wallet) returns (bool verified) {
            if (!verified) revert NotVerified(wallet);
        } catch {
            revert EligibilityCheckFailed();
        }
    }
}
