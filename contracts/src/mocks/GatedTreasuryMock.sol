// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @notice The subset of EAS this token needs. EAS is a predeploy on Base and Base Sepolia
///         at 0x4200000000000000000000000000000000000021.
interface IEAS {
    /// @dev Field order is EAS's, not a convenience ordering: `refUID` sits between
    ///      `revocationTime` and `recipient`. Getting it wrong does not fail loudly — the ABI
    ///      decode simply misaligns and the call reverts with no data, while every individual
    ///      field still reads correctly through a separate interface. Verified against the live
    ///      predeploy on Base Sepolia 2026-08-22.
    struct Attestation {
        bytes32 uid;
        bytes32 schema;
        uint64 time;
        uint64 expirationTime;
        uint64 revocationTime;
        bytes32 refUID;
        address recipient;
        address attester;
        bool revocable;
        bytes data;
    }

    function getAttestation(bytes32 uid) external view returns (Attestation memory);
}

/// @title GatedTreasuryMock — a permissioned instrument for the Base Sepolia demo.
/// @notice This is NOT a real fund and is NOT affiliated with any issuer. It exists so the
///         eligibility path can be exercised end to end on a testnet, because the permissioned
///         instruments Par reads on mainnet cannot be deployed anywhere else.
///
///         The gate deliberately mirrors the pattern Par measured on Ondo's OUSG, whose transfer
///         reverts with "must be KYC'd to initiate transfer" *before* the balance check. Both
///         sides of a transfer must hold a live attestation, so the failure a demo produces is
///         the same shape as the failure mainnet produces.
///
///         Credentials are real EAS attestations from a schema this deployment trusts. A wallet
///         registers the attestation it already holds; the token verifies schema, attester,
///         recipient, expiry and revocation itself rather than taking anyone's word for it.
contract GatedTreasuryMock is ERC20, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    IEAS public immutable eas;
    bytes32 public immutable schemaUid;
    address public immutable trustedAttester;

    mapping(address => bytes32) public credentialOf;

    event CredentialRegistered(address indexed wallet, bytes32 indexed uid);
    event CredentialCleared(address indexed wallet, bytes32 indexed uid, string reason);

    error NotVerified(address wallet);
    error ClaimTooLarge(uint256 amount, uint256 cap);
    error WrongSchema(bytes32 got, bytes32 want);
    error UntrustedAttester(address got, address want);
    error NotTheRecipient(address got, address caller);
    error CredentialRevoked(uint64 revokedAt);
    error CredentialExpired(uint64 expiredAt);

    constructor(address eas_, bytes32 schemaUid_, address trustedAttester_)
        ERC20("Mock Gated Treasury", "mGT")
    {
        eas = IEAS(eas_);
        schemaUid = schemaUid_;
        trustedAttester = trustedAttester_;
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(MINTER_ROLE, msg.sender);
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    /// @notice Present an attestation you already hold. Every condition is checked here; the
    ///         caller cannot assert eligibility, only point at evidence of it.
    function registerCredential(bytes32 uid) external {
        IEAS.Attestation memory a = eas.getAttestation(uid);

        if (a.schema != schemaUid) revert WrongSchema(a.schema, schemaUid);
        if (a.attester != trustedAttester) revert UntrustedAttester(a.attester, trustedAttester);
        if (a.recipient != msg.sender) revert NotTheRecipient(a.recipient, msg.sender);
        if (a.revocationTime != 0) revert CredentialRevoked(a.revocationTime);
        if (a.expirationTime != 0 && a.expirationTime <= block.timestamp) {
            revert CredentialExpired(a.expirationTime);
        }

        credentialOf[msg.sender] = uid;
        emit CredentialRegistered(msg.sender, uid);
    }

    /// @notice Whether a wallet may hold this instrument right now. Re-reads EAS on every call:
    ///         a credential registered yesterday and revoked today must not still read as valid.
    function isVerified(address wallet) public view returns (bool) {
        bytes32 uid = credentialOf[wallet];
        if (uid == bytes32(0)) return false;

        IEAS.Attestation memory a = eas.getAttestation(uid);
        if (a.recipient != wallet || a.attester != trustedAttester || a.schema != schemaUid) return false;
        if (a.revocationTime != 0) return false;
        if (a.expirationTime != 0 && a.expirationTime <= block.timestamp) return false;
        return true;
    }

    function mint(address to, uint256 amount) external onlyRole(MINTER_ROLE) {
        if (!isVerified(to)) revert NotVerified(to);
        _mint(to, amount);
    }

    /// @notice Testnet self-serve: a verified wallet mints its own demo position, signing the
    ///         transaction itself. The cap matches the depth the demo config reports, so the UI
    ///         cannot offer a size the contract would refuse. This stands in for execution on a
    ///         chain where the instrument has no market; it is not a claim about price.
    uint256 public constant CLAIM_CAP = 10_000_000e6;

    function claim(uint256 amount) external {
        if (!isVerified(msg.sender)) revert NotVerified(msg.sender);
        if (amount > CLAIM_CAP) revert ClaimTooLarge(amount, CLAIM_CAP);
        _mint(msg.sender, amount);
    }

    /// @dev The gate runs before any balance arithmetic, so an unverified wallet is told it is
    ///      unverified rather than told it is short of funds. Mints and burns are exempt.
    function _update(address from, address to, uint256 value) internal override {
        if (from != address(0) && !isVerified(from)) revert NotVerified(from);
        if (to != address(0) && !isVerified(to)) revert NotVerified(to);
        super._update(from, to, value);
    }
}
