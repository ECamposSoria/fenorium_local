// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title DecisionPassportV2
/// @notice Append-only commitments to published score snapshots, including visible corrections.
/// @dev Enforces chronology, count consistency and a raw CID prefix, never score correctness,
/// evidence availability, investment performance or correspondence between the CID and hash.
contract DecisionPassportV2 is AccessControl {
    bytes32 public constant ISSUER_ROLE = keccak256("ISSUER_ROLE");

    struct SnapshotInput {
        uint64 generatedAt;
        bytes32 contentSha256;
        string evidenceCid;
        string methodVersion;
        uint32 instrumentCount;
        uint32 scoredCount;
        uint32 passingCount;
    }

    struct Snapshot {
        uint64 generatedAt;
        bytes32 contentSha256;
        string evidenceCid;
        string methodVersion;
        uint32 instrumentCount;
        uint32 scoredCount;
        uint32 passingCount;
        uint64 recordedAt;
        uint256 supersedes;
        string correctionReason;
    }

    error InvalidGeneratedAt(uint64 generatedAt, uint64 latestGeneratedAt);
    error FutureGeneratedAt(uint64 generatedAt, uint256 recordedAt);
    error EmptyContentSha256();
    error InvalidEvidenceCid();
    error EmptyMethodVersion();
    error InvalidCounts();
    error UnknownSnapshot(uint256 id);
    error CorrectionGeneratedAtMismatch();
    error EmptyCorrectionReason();
    error UnchangedContentSha256();

    uint256 public total;
    uint64 public latestGeneratedAt;
    mapping(uint64 => uint256) public idByGeneratedAt;
    mapping(uint256 => Snapshot) private _snapshots;

    event SnapshotRecorded(
        uint256 indexed id, uint64 indexed generatedAt, bytes32 contentSha256, string evidenceCid,
        string methodVersion, uint32 instrumentCount, uint32 scoredCount, uint32 passingCount
    );
    event SnapshotCorrected(
        uint256 indexed id, uint256 indexed supersededId, uint64 generatedAt,
        bytes32 contentSha256, string evidenceCid, string reason
    );

    constructor(address governance, address issuer) {
        _grantRole(DEFAULT_ADMIN_ROLE, governance);
        _grantRole(ISSUER_ROLE, issuer);
    }

    function recordSnapshot(SnapshotInput calldata input) external onlyRole(ISSUER_ROLE) returns (uint256 id) {
        if (input.generatedAt <= latestGeneratedAt) revert InvalidGeneratedAt(input.generatedAt, latestGeneratedAt);
        _validate(input);
        id = _store(input, 0, "");
        latestGeneratedAt = input.generatedAt;
        emit SnapshotRecorded(id, input.generatedAt, input.contentSha256, input.evidenceCid,
            input.methodVersion, input.instrumentCount, input.scoredCount, input.passingCount);
    }

    /// @notice Retains the original verbatim; the timestamp lookup points to the correction.
    function recordCorrection(uint256 supersededId, SnapshotInput calldata input, string calldata reason)
        external onlyRole(ISSUER_ROLE) returns (uint256 id)
    {
        _requireSnapshot(supersededId);
        Snapshot storage previous = _snapshots[supersededId];
        if (input.generatedAt != previous.generatedAt) revert CorrectionGeneratedAtMismatch();
        if (bytes(reason).length == 0) revert EmptyCorrectionReason();
        if (input.contentSha256 == previous.contentSha256) revert UnchangedContentSha256();
        _validate(input);
        id = _store(input, supersededId, reason);
        emit SnapshotCorrected(id, supersededId, input.generatedAt, input.contentSha256, input.evidenceCid, reason);
    }

    function snapshotOf(uint256 id) external view returns (Snapshot memory) {
        _requireSnapshot(id);
        return _snapshots[id];
    }

    function _requireSnapshot(uint256 id) private view {
        if (id == 0 || id > total) revert UnknownSnapshot(id);
    }

    function _validate(SnapshotInput calldata input) private view {
        if (input.generatedAt > block.timestamp) revert FutureGeneratedAt(input.generatedAt, block.timestamp);
        if (input.contentSha256 == bytes32(0)) revert EmptyContentSha256();
        bytes memory cid = bytes(input.evidenceCid);
        bytes memory prefix = bytes("bafkrei");
        if (cid.length < prefix.length) revert InvalidEvidenceCid();
        for (uint256 i; i < prefix.length; ++i) {
            if (cid[i] != prefix[i]) revert InvalidEvidenceCid();
        }
        if (bytes(input.methodVersion).length == 0) revert EmptyMethodVersion();
        if (input.passingCount > input.scoredCount || input.scoredCount > input.instrumentCount) revert InvalidCounts();
    }

    function _store(SnapshotInput calldata input, uint256 supersedes, string memory reason) private returns (uint256 id) {
        id = ++total;
        Snapshot storage s = _snapshots[id];
        s.generatedAt = input.generatedAt;
        s.contentSha256 = input.contentSha256;
        s.evidenceCid = input.evidenceCid;
        s.methodVersion = input.methodVersion;
        s.instrumentCount = input.instrumentCount;
        s.scoredCount = input.scoredCount;
        s.passingCount = input.passingCount;
        s.recordedAt = uint64(block.timestamp);
        s.supersedes = supersedes;
        s.correctionReason = reason;
        idByGeneratedAt[input.generatedAt] = id;
    }
}
