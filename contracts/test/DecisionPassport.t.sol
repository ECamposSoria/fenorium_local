// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

import {DecisionPassport} from "../src/DecisionPassport.sol";

contract DecisionPassportTest is Test {
    DecisionPassport internal passport;

    address internal governance = address(0xC0FFEE);
    address internal issuer = address(0xBEEF);
    address internal stranger = address(0xBAD);

    // Two different contracts that report the same symbol. Recording both is the point:
    // identity is the address, never the ticker.
    address internal usd3A = address(0xA111);
    address internal usd3B = address(0xB222);
    address internal usdc = address(0xC0DE);

    uint64 internal constant BASE_CHAIN_ID = 8453;
    string internal constant METHOD = "par-engine/2026.08.21";
    string internal constant CID = "bafkreigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy";

    function setUp() public {
        passport = new DecisionPassport(governance, issuer);
        vm.warp(1_800_000_000);
    }

    function _base(DecisionPassport.Verdict verdict, bytes32 decisionHash)
        internal
        view
        returns (DecisionPassport.DecisionInput memory input)
    {
        input = DecisionPassport.DecisionInput({
            instrumentChainId: BASE_CHAIN_ID,
            instrument: usd3A,
            denominationAsset: usdc,
            verdict: verdict,
            maturity: uint64(block.timestamp + 30 days),
            quoteExpiry: verdict == DecisionPassport.Verdict.BID ? uint64(block.timestamp + 120) : 0,
            executableApyBps: verdict == DecisionPassport.Verdict.BID ? int64(870) : int64(0),
            intendedSize: verdict == DecisionPassport.Verdict.BID ? 250_000e6 : 0,
            decisionHash: decisionHash,
            methodVersion: METHOD,
            evidenceCid: CID
        });
    }

    function _recordBid(bytes32 decisionHash) internal returns (uint256) {
        vm.prank(issuer);
        return passport.record(_base(DecisionPassport.Verdict.BID, decisionHash));
    }

    function test_recordsBidAndReadsBack() public {
        uint256 id = _recordBid(keccak256("d1"));
        assertEq(id, 1);
        assertEq(passport.total(), 1);
        assertEq(passport.idByDecisionHash(keccak256("d1")), 1);

        DecisionPassport.Passport memory p = passport.passportOf(id);
        assertEq(p.instrument, usd3A);
        assertEq(p.instrumentChainId, BASE_CHAIN_ID);
        assertEq(p.denominationAsset, usdc);
        assertEq(uint8(p.verdict), uint8(DecisionPassport.Verdict.BID));
        assertEq(p.executableApyBps, int64(870));
        assertEq(p.intendedSize, 250_000e6);
        assertEq(p.decisionHash, keccak256("d1"));
        assertEq(p.methodVersion, METHOD);
        assertEq(p.evidenceCid, CID);
        assertEq(p.recordedAt, uint64(block.timestamp));
        assertTrue(passport.isQuoteLive(id));
    }

    function test_recordsNoQuoteWithoutAnyExpiry() public {
        vm.prank(issuer);
        uint256 id = passport.record(_base(DecisionPassport.Verdict.NO_QUOTE, keccak256("noquote")));
        DecisionPassport.Passport memory p = passport.passportOf(id);
        assertEq(uint8(p.verdict), uint8(DecisionPassport.Verdict.NO_QUOTE));
        assertEq(p.quoteExpiry, 0);
        // A verdict with no quote has nothing to expire.
        assertFalse(passport.isQuoteLive(id));
    }

    function test_recordsNoBid() public {
        vm.prank(issuer);
        uint256 id = passport.record(_base(DecisionPassport.Verdict.NO_BID, keccak256("nobid")));
        assertEq(uint8(passport.passportOf(id).verdict), uint8(DecisionPassport.Verdict.NO_BID));
        assertFalse(passport.isQuoteLive(id));
    }

    /// Same ticker, two contracts, two independent passports.
    function test_sameSymbolDifferentContractsAreDistinct() public {
        _recordBid(keccak256("d1"));

        DecisionPassport.DecisionInput memory second = _base(DecisionPassport.Verdict.NO_BID, keccak256("d2"));
        second.instrument = usd3B;
        vm.prank(issuer);
        uint256 secondId = passport.record(second);

        assertEq(passport.total(), 2);
        assertEq(passport.passportOf(1).instrument, usd3A);
        assertEq(passport.passportOf(secondId).instrument, usd3B);
        assertTrue(passport.passportOf(1).verdict != passport.passportOf(secondId).verdict);
    }

    function test_revertsOnDuplicateDecisionHash() public {
        _recordBid(keccak256("dup"));
        vm.prank(issuer);
        vm.expectRevert(abi.encodeWithSelector(DecisionPassport.DuplicateDecision.selector, keccak256("dup"), 1));
        passport.record(_base(DecisionPassport.Verdict.BID, keccak256("dup")));
    }

    /// An expired quote cannot be passported as a BID: a stale quote is not a price.
    function test_revertsOnBidWithExpiredQuote() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.BID, keccak256("stale"));
        input.quoteExpiry = uint64(block.timestamp);
        vm.prank(issuer);
        vm.expectRevert(
            abi.encodeWithSelector(DecisionPassport.QuoteAlreadyExpired.selector, input.quoteExpiry, block.timestamp)
        );
        passport.record(input);
    }

    function test_revertsOnEmptyEvidenceCid() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.NO_QUOTE, keccak256("nocid"));
        input.evidenceCid = "";
        vm.prank(issuer);
        vm.expectRevert(DecisionPassport.EmptyEvidenceCid.selector);
        passport.record(input);
    }

    function test_revertsOnEmptyMethodVersion() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.NO_BID, keccak256("nm"));
        input.methodVersion = "";
        vm.prank(issuer);
        vm.expectRevert(DecisionPassport.EmptyMethodVersion.selector);
        passport.record(input);
    }

    function test_revertsOnZeroInstrument() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.NO_BID, keccak256("zi"));
        input.instrument = address(0);
        vm.prank(issuer);
        vm.expectRevert(DecisionPassport.InvalidInstrument.selector);
        passport.record(input);
    }

    function test_revertsOnZeroChainId() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.NO_BID, keccak256("zc"));
        input.instrumentChainId = 0;
        vm.prank(issuer);
        vm.expectRevert(DecisionPassport.InvalidChainId.selector);
        passport.record(input);
    }

    function test_revertsOnEmptyDecisionHash() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.NO_BID, bytes32(0));
        vm.prank(issuer);
        vm.expectRevert(DecisionPassport.EmptyDecisionHash.selector);
        passport.record(input);
    }

    function test_revertsOnNoneVerdict() public {
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.NONE, keccak256("nv"));
        vm.prank(issuer);
        vm.expectRevert(DecisionPassport.InvalidVerdict.selector);
        passport.record(input);
    }

    function test_strangerCannotRecord() public {
        // Cache the role first: reading it is itself an external call and would consume the prank.
        bytes32 role = passport.ISSUER_ROLE();
        DecisionPassport.DecisionInput memory input = _base(DecisionPassport.Verdict.BID, keccak256("x"));
        vm.prank(stranger);
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, stranger, role)
        );
        passport.record(input);
    }

    function test_attachesOutcomeOnce() public {
        uint256 id = _recordBid(keccak256("d1"));

        vm.prank(issuer);
        passport.attachOutcome(id, keccak256("tx"), int64(831), "redeemed at maturity");

        DecisionPassport.Outcome memory o = passport.outcomeOf(id);
        assertTrue(o.attached);
        assertEq(o.realizedRef, keccak256("tx"));
        assertEq(o.realizedApyBps, int64(831));
        assertEq(o.note, "redeemed at maturity");

        vm.prank(issuer);
        vm.expectRevert(abi.encodeWithSelector(DecisionPassport.OutcomeAlreadyAttached.selector, id));
        passport.attachOutcome(id, keccak256("tx2"), int64(0), "again");
    }

    /// An outcome may honestly record that nothing was executed.
    function test_outcomeCanRecordNonExecution() public {
        uint256 id = _recordBid(keccak256("d1"));
        vm.prank(issuer);
        passport.attachOutcome(id, bytes32(0), int64(0), "not executed: quote expired before signing");
        assertEq(passport.outcomeOf(id).note, "not executed: quote expired before signing");
    }

    function test_strangerCannotAttachOutcome() public {
        uint256 id = _recordBid(keccak256("d1"));
        bytes32 role = passport.ISSUER_ROLE();
        vm.prank(stranger);
        vm.expectRevert(
            abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, stranger, role)
        );
        passport.attachOutcome(id, bytes32(0), int64(0), "");
    }

    function test_revertsOnUnknownPassport() public {
        vm.expectRevert(abi.encodeWithSelector(DecisionPassport.UnknownPassport.selector, uint256(0)));
        passport.passportOf(0);
        vm.expectRevert(abi.encodeWithSelector(DecisionPassport.UnknownPassport.selector, uint256(99)));
        passport.outcomeOf(99);
        vm.prank(issuer);
        vm.expectRevert(abi.encodeWithSelector(DecisionPassport.UnknownPassport.selector, uint256(99)));
        passport.attachOutcome(99, bytes32(0), int64(0), "");
    }

    function test_quoteGoesStaleWithTime() public {
        uint256 id = _recordBid(keccak256("d1"));
        assertTrue(passport.isQuoteLive(id));
        vm.warp(block.timestamp + 121);
        assertFalse(passport.isQuoteLive(id));
        // The record itself is unchanged: only its live-ness expired.
        assertEq(passport.passportOf(id).decisionHash, keccak256("d1"));
    }

    function testFuzz_recordIsAppendOnlyAndHashKeyed(bytes32 h1, bytes32 h2) public {
        vm.assume(h1 != bytes32(0) && h2 != bytes32(0) && h1 != h2);
        DecisionPassport.DecisionInput memory a = _base(DecisionPassport.Verdict.NO_BID, h1);
        DecisionPassport.DecisionInput memory b = _base(DecisionPassport.Verdict.NO_BID, h2);
        b.instrument = usd3B;

        vm.startPrank(issuer);
        uint256 idA = passport.record(a);
        uint256 idB = passport.record(b);
        vm.stopPrank();

        assertEq(idB, idA + 1);
        assertEq(passport.idByDecisionHash(h1), idA);
        assertEq(passport.idByDecisionHash(h2), idB);
        assertEq(passport.total(), 2);
    }
}
