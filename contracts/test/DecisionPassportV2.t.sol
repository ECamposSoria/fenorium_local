// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {DecisionPassportV2} from "../src/DecisionPassportV2.sol";

contract DecisionPassportV2Test is Test {
    DecisionPassportV2 internal passport;
    address internal governance = address(0xC0FFEE);
    address internal issuer = address(0xBEEF);
    address internal stranger = address(0xBAD);
    string internal constant CID = "bafkreigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy";

    event SnapshotRecorded(uint256 indexed id, uint64 indexed generatedAt, bytes32 contentSha256,
        string evidenceCid, string methodVersion, uint32 instrumentCount, uint32 scoredCount, uint32 passingCount);
    event SnapshotCorrected(uint256 indexed id, uint256 indexed supersededId, uint64 generatedAt,
        bytes32 contentSha256, string evidenceCid, string reason);

    function setUp() public {
        passport = new DecisionPassportV2(governance, issuer);
        vm.warp(1_800_000_000);
    }

    function _input() internal pure returns (DecisionPassportV2.SnapshotInput memory) {
        return DecisionPassportV2.SnapshotInput(1_799_999_000, bytes32(uint256(42)), CID, "rating/2", 500, 490, 200);
    }

    function _record() internal returns (uint256) {
        vm.prank(issuer);
        return passport.recordSnapshot(_input());
    }

    function test_happyPathAndEvent() public {
        assertTrue(passport.hasRole(passport.DEFAULT_ADMIN_ROLE(), governance));
        assertTrue(passport.hasRole(passport.ISSUER_ROLE(), issuer));
        vm.expectEmit(true, true, false, true, address(passport));
        emit SnapshotRecorded(1, 1_799_999_000, bytes32(uint256(42)), CID, "rating/2", 500, 490, 200);
        assertEq(_record(), 1);
        assertEq(passport.total(), 1);
        assertEq(passport.latestGeneratedAt(), 1_799_999_000);
        assertEq(passport.idByGeneratedAt(1_799_999_000), 1);
        DecisionPassportV2.Snapshot memory s = passport.snapshotOf(1);
        assertEq(s.generatedAt, 1_799_999_000);
        assertEq(s.contentSha256, bytes32(uint256(42)));
        assertEq(s.evidenceCid, CID);
        assertEq(s.methodVersion, "rating/2");
        assertEq(s.instrumentCount, 500);
        assertEq(s.scoredCount, 490);
        assertEq(s.passingCount, 200);
        assertEq(s.recordedAt, 1_800_000_000);
        assertEq(s.supersedes, 0);
        assertEq(s.correctionReason, "");
    }

    function test_zeroCountsAndTimestampEqualToBlockAreValid() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.generatedAt = 1_800_000_000;
        input.instrumentCount = 0;
        input.scoredCount = 0;
        input.passingCount = 0;
        vm.prank(issuer);
        assertEq(passport.recordSnapshot(input), 1);
        DecisionPassportV2.Snapshot memory s = passport.snapshotOf(1);
        assertEq(s.instrumentCount, 0);
        assertEq(s.scoredCount, 0);
        assertEq(s.passingCount, 0);
        assertEq(s.generatedAt, s.recordedAt);
    }

    function test_correctionRetainsOriginalAndEmitsExactFields() public {
        _record();
        bytes memory original = abi.encode(passport.snapshotOf(1));
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.contentSha256 = bytes32(uint256(43));
        input.passingCount = 199;
        vm.warp(1_800_000_100);
        vm.expectEmit(true, true, false, true, address(passport));
        emit SnapshotCorrected(2, 1, 1_799_999_000, bytes32(uint256(43)), CID, "source corrected");
        vm.prank(issuer);
        assertEq(passport.recordCorrection(1, input, "source corrected"), 2);
        assertEq(abi.encode(passport.snapshotOf(1)), original);
        assertEq(passport.idByGeneratedAt(input.generatedAt), 2);
        assertEq(passport.total(), 2);
        assertEq(passport.latestGeneratedAt(), 1_799_999_000);
        DecisionPassportV2.Snapshot memory s = passport.snapshotOf(2);
        assertEq(s.generatedAt, 1_799_999_000);
        assertEq(s.contentSha256, bytes32(uint256(43)));
        assertEq(s.evidenceCid, CID);
        assertEq(s.methodVersion, "rating/2");
        assertEq(s.instrumentCount, 500);
        assertEq(s.scoredCount, 490);
        assertEq(s.passingCount, 199);
        assertEq(s.recordedAt, 1_800_000_100);
        assertEq(s.supersedes, 1);
        assertEq(s.correctionReason, "source corrected");
    }

    function test_zeroDuplicateAndOlderGeneratedAtRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.generatedAt = 0;
        vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.InvalidGeneratedAt.selector, uint64(0), uint64(0)));
        vm.prank(issuer);
        passport.recordSnapshot(input);
        _record();
        for (uint64 t = 1_799_998_999; t <= 1_799_999_000; ++t) {
            input.generatedAt = t;
            vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.InvalidGeneratedAt.selector, t, uint64(1_799_999_000)));
            vm.prank(issuer);
            passport.recordSnapshot(input);
        }
    }

    function _rejectFields(DecisionPassportV2.SnapshotInput memory input, bytes memory error) internal {
        vm.expectRevert(error);
        vm.prank(issuer);
        passport.recordSnapshot(input);
        _record();
        input.generatedAt = 1_799_999_000;
        if (input.contentSha256 == bytes32(uint256(42))) input.contentSha256 = bytes32(uint256(43));
        vm.expectRevert(error);
        vm.prank(issuer);
        passport.recordCorrection(1, input, "fix");
        assertEq(passport.total(), 1);
    }

    function test_emptyHashRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.contentSha256 = bytes32(0);
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.EmptyContentSha256.selector));
    }

    function test_emptyMethodRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.methodVersion = "";
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.EmptyMethodVersion.selector));
    }

    function test_emptyCidRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.evidenceCid = "";
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.InvalidEvidenceCid.selector));
    }

    function test_shortCidRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.evidenceCid = "bafkre";
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.InvalidEvidenceCid.selector));
    }

    function test_dagPbCidRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.evidenceCid = "bafybeigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy";
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.InvalidEvidenceCid.selector));
    }

    function test_cidV0Rejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.evidenceCid = "QmSomething";
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.InvalidEvidenceCid.selector));
    }

    function test_scoredCountRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.scoredCount = 501;
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.InvalidCounts.selector));
    }

    function test_passingCountRejected() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.passingCount = 491;
        _rejectFields(input, abi.encodeWithSelector(DecisionPassportV2.InvalidCounts.selector));
    }

    function test_futureGeneratedAtRejectedForOriginalAndCorrection() public {
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.generatedAt = 1_800_000_001;
        vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.FutureGeneratedAt.selector, input.generatedAt, uint256(1_800_000_000)));
        vm.prank(issuer);
        passport.recordSnapshot(input);
        _record();
        vm.warp(1_799_998_999);
        input = _input();
        input.contentSha256 = bytes32(uint256(43));
        vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.FutureGeneratedAt.selector, input.generatedAt, uint256(1_799_998_999)));
        vm.prank(issuer);
        passport.recordCorrection(1, input, "fix");
    }

    function test_unknownRecordRejected() public {
        for (uint256 id; id < 2; ++id) {
            vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.UnknownSnapshot.selector, id));
            passport.snapshotOf(id);
            vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.UnknownSnapshot.selector, id));
            vm.prank(issuer);
            passport.recordCorrection(id, _input(), "fix");
        }
    }

    function test_correctionRequiresSameTimeReasonAndDifferentHash() public {
        _record();
        DecisionPassportV2.SnapshotInput memory input = _input();
        vm.expectRevert(DecisionPassportV2.UnchangedContentSha256.selector);
        vm.prank(issuer);
        passport.recordCorrection(1, input, "fix");
        input.contentSha256 = bytes32(uint256(43));
        vm.expectRevert(DecisionPassportV2.EmptyCorrectionReason.selector);
        vm.prank(issuer);
        passport.recordCorrection(1, input, "");
        input.generatedAt++;
        vm.expectRevert(DecisionPassportV2.CorrectionGeneratedAtMismatch.selector);
        vm.prank(issuer);
        passport.recordCorrection(1, input, "fix");
    }

    function test_nonIssuerRejectedForBothWrites() public {
        _record();
        bytes32 role = passport.ISSUER_ROLE();
        bytes memory error = abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, stranger, role);
        vm.expectRevert(error);
        vm.prank(stranger);
        passport.recordSnapshot(_input());
        vm.expectRevert(error);
        vm.prank(stranger);
        passport.recordCorrection(1, _input(), "fix");
    }

    function test_correctionToOlderSnapshotDoesNotRewindLatest() public {
        _record();
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.generatedAt = 1_800_000_000;
        vm.prank(issuer);
        assertEq(passport.recordSnapshot(input), 2);
        input = _input();
        input.contentSha256 = bytes32(uint256(43));
        vm.prank(issuer);
        assertEq(passport.recordCorrection(1, input, "fix"), 3);
        assertEq(passport.latestGeneratedAt(), 1_800_000_000);
        assertEq(passport.idByGeneratedAt(1_799_999_000), 3);
        assertEq(passport.idByGeneratedAt(1_800_000_000), 2);
    }

    function testFuzz_ordering(uint64 first, uint64 next) public {
        first = uint64(bound(first, 1, 1_800_000_000));
        next = uint64(bound(next, 0, 1_800_000_000));
        DecisionPassportV2.SnapshotInput memory input = _input();
        input.generatedAt = first;
        vm.prank(issuer);
        passport.recordSnapshot(input);
        input.generatedAt = next;
        if (next <= first) {
            vm.expectRevert(abi.encodeWithSelector(DecisionPassportV2.InvalidGeneratedAt.selector, next, first));
            vm.prank(issuer);
            passport.recordSnapshot(input);
            assertEq(passport.latestGeneratedAt(), first);
            assertEq(passport.total(), 1);
        } else {
            vm.prank(issuer);
            assertEq(passport.recordSnapshot(input), 2);
            assertEq(passport.latestGeneratedAt(), next);
            assertEq(passport.idByGeneratedAt(next), 2);
        }
    }
}
