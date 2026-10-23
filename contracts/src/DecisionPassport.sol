// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title DecisionPassport
/// @notice Append-only record of Par verdicts on onchain fixed-income instruments.
/// @dev The contract enforces immutability, identity and quote freshness — never correctness.
/// It stores what the engine decided and the content address of the evidence that produced it,
/// so a third party can re-derive the decision without trusting Par. A passport is not a claim
/// of performance, and an attached outcome reference is not an assertion that a fill succeeded.
contract DecisionPassport is AccessControl {
    bytes32 public constant ISSUER_ROLE = keccak256("ISSUER_ROLE");

    /// @notice NO_QUOTE is a first-class verdict: the engine declares it cannot honestly price
    /// the instrument. It is recorded with evidence, exactly like a BID or a NO_BID.
    enum Verdict {
        NONE,
        BID,
        NO_BID,
        NO_QUOTE
    }

    /// @dev Passed as a calldata struct rather than positional arguments: the field count
    /// exhausts the stack under the non-IR pipeline this project builds with.
    struct DecisionInput {
        uint64 instrumentChainId;
        address instrument;
        address denominationAsset;
        Verdict verdict;
        uint64 maturity;
        uint64 quoteExpiry;
        int64 executableApyBps;
        uint256 intendedSize;
        bytes32 decisionHash;
        string methodVersion;
        string evidenceCid;
    }

    struct Passport {
        // Identity is a contract, never a ticker. Two instruments may share a symbol.
        uint64 instrumentChainId;
        address instrument;
        address denominationAsset;
        Verdict verdict;
        uint64 maturity;
        uint64 quoteExpiry;
        uint64 recordedAt;
        int64 executableApyBps;
        uint256 intendedSize;
        bytes32 decisionHash;
        string methodVersion;
        string evidenceCid;
    }

    struct Outcome {
        bool attached;
        uint64 attachedAt;
        bytes32 realizedRef;
        int64 realizedApyBps;
        string note;
    }

    error DuplicateDecision(bytes32 decisionHash, uint256 existingId);
    error UnknownPassport(uint256 id);
    error OutcomeAlreadyAttached(uint256 id);
    error InvalidVerdict();
    error InvalidInstrument();
    error InvalidChainId();
    error EmptyDecisionHash();
    error EmptyEvidenceCid();
    error EmptyMethodVersion();
    error QuoteAlreadyExpired(uint64 quoteExpiry, uint256 recordedAt);

    uint256 private _total;

    mapping(uint256 id => Passport) private _passports;
    mapping(uint256 id => Outcome) private _outcomes;
    mapping(bytes32 decisionHash => uint256 id) public idByDecisionHash;

    event DecisionRecorded(
        uint256 indexed id,
        bytes32 indexed decisionHash,
        address indexed instrument,
        uint64 instrumentChainId,
        Verdict verdict,
        uint256 intendedSize,
        int64 executableApyBps,
        uint64 quoteExpiry,
        string methodVersion,
        string evidenceCid
    );

    event OutcomeAttached(
        uint256 indexed id, bytes32 indexed decisionHash, bytes32 realizedRef, int64 realizedApyBps, string note
    );

    constructor(address governance, address issuer) {
        _grantRole(DEFAULT_ADMIN_ROLE, governance);
        _grantRole(ISSUER_ROLE, issuer);
    }

    /// @notice Record a verdict. Reverts rather than overwriting an existing decision hash.
    /// @dev A BID must carry a quote that has not already expired at recording time. This is the
    /// on-chain expression of "an API quote is not a fill": a stale quote cannot be passported.
    function record(DecisionInput calldata input) external onlyRole(ISSUER_ROLE) returns (uint256 id) {
        if (input.verdict == Verdict.NONE) revert InvalidVerdict();
        if (input.instrument == address(0)) revert InvalidInstrument();
        if (input.instrumentChainId == 0) revert InvalidChainId();
        if (input.decisionHash == bytes32(0)) revert EmptyDecisionHash();
        // Every verdict carries evidence. A declared gap is a product, not a blank field.
        if (bytes(input.evidenceCid).length == 0) revert EmptyEvidenceCid();
        if (bytes(input.methodVersion).length == 0) revert EmptyMethodVersion();

        uint256 existing = idByDecisionHash[input.decisionHash];
        if (existing != 0) revert DuplicateDecision(input.decisionHash, existing);

        if (input.verdict == Verdict.BID && input.quoteExpiry <= block.timestamp) {
            revert QuoteAlreadyExpired(input.quoteExpiry, block.timestamp);
        }

        id = ++_total;
        idByDecisionHash[input.decisionHash] = id;

        Passport storage p = _passports[id];
        p.instrumentChainId = input.instrumentChainId;
        p.instrument = input.instrument;
        p.denominationAsset = input.denominationAsset;
        p.verdict = input.verdict;
        p.maturity = input.maturity;
        p.quoteExpiry = input.quoteExpiry;
        p.recordedAt = uint64(block.timestamp);
        p.executableApyBps = input.executableApyBps;
        p.intendedSize = input.intendedSize;
        p.decisionHash = input.decisionHash;
        p.methodVersion = input.methodVersion;
        p.evidenceCid = input.evidenceCid;

        emit DecisionRecorded(
            id,
            input.decisionHash,
            input.instrument,
            input.instrumentChainId,
            input.verdict,
            input.intendedSize,
            input.executableApyBps,
            input.quoteExpiry,
            input.methodVersion,
            input.evidenceCid
        );
    }

    /// @notice Attach what actually happened, once, so forecast and realization can be compared.
    /// @dev `realizedRef` is a reference (transaction or settlement identifier), not an assertion
    /// that execution succeeded. `note` is recorded verbatim and may say the decision was never
    /// acted on.
    function attachOutcome(uint256 id, bytes32 realizedRef, int64 realizedApyBps, string calldata note)
        external
        onlyRole(ISSUER_ROLE)
    {
        if (id == 0 || id > _total) revert UnknownPassport(id);
        if (_outcomes[id].attached) revert OutcomeAlreadyAttached(id);

        Outcome storage o = _outcomes[id];
        o.attached = true;
        o.attachedAt = uint64(block.timestamp);
        o.realizedRef = realizedRef;
        o.realizedApyBps = realizedApyBps;
        o.note = note;

        emit OutcomeAttached(id, _passports[id].decisionHash, realizedRef, realizedApyBps, note);
    }

    function total() external view returns (uint256) {
        return _total;
    }

    function passportOf(uint256 id) external view returns (Passport memory) {
        if (id == 0 || id > _total) revert UnknownPassport(id);
        return _passports[id];
    }

    function outcomeOf(uint256 id) external view returns (Outcome memory) {
        if (id == 0 || id > _total) revert UnknownPassport(id);
        return _outcomes[id];
    }

    /// @notice True when the recorded quote is still valid at call time.
    /// @dev Verdicts other than BID have no quote to expire and always read false.
    function isQuoteLive(uint256 id) external view returns (bool) {
        if (id == 0 || id > _total) revert UnknownPassport(id);
        Passport storage p = _passports[id];
        if (p.verdict != Verdict.BID) return false;
        return p.quoteExpiry > block.timestamp;
    }
}
