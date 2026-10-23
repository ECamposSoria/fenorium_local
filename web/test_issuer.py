"""Offline ABI conformance tests for the Par demo issuer."""

import sys
import unittest
from pathlib import Path

# A direct `python3 web/test_issuer.py` run starts from web/; add the repository root so the test
# exercises the same shared Keccak module as module-form unittest discovery.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.keccak import keccak256
from web.issuer import ATTEST_SIGNATURE, encode_attest


SCHEMA = "0x" + "11" * 32
RECIPIENT = "0x" + "22" * 20
REF_UID = bytes.fromhex("33" * 32)


class EncodeAttestTests(unittest.TestCase):
    def test_selector_and_word_alignment(self):
        calldata = encode_attest(SCHEMA, RECIPIENT, b"demo")
        raw = bytes.fromhex(calldata[2:])

        self.assertEqual(raw[:4], keccak256(ATTEST_SIGNATURE.encode())[:4])
        self.assertEqual((len(raw) - 4) % 32, 0)

    def test_schema_and_recipient_words(self):
        words = bytes.fromhex(encode_attest(SCHEMA, RECIPIENT, b"demo")[10:])

        self.assertEqual(words[32:64], bytes.fromhex(SCHEMA[2:]))
        self.assertEqual(words[96:128], bytes.fromhex(RECIPIENT[2:]).rjust(32, b"\x00"))

    def test_longer_payload_changes_only_tail_and_grows_by_words(self):
        short = bytes.fromhex(encode_attest(SCHEMA, RECIPIENT, b"a")[2:])
        long = bytes.fromhex(encode_attest(SCHEMA, RECIPIENT, b"a" * 65)[2:])
        tail_start = 4 + (1 + 2 + 6) * 32

        self.assertEqual(short[:tail_start], long[:tail_start])
        self.assertEqual(len(long) - len(short), 64)
        self.assertEqual((len(long) - len(short)) % 32, 0)

    def test_matches_cast_calldata_golden_vector(self):
        # Produced once with cast calldata and the canonical ATTEST_SIGNATURE tuple input.
        expected = (
            "0xf17325e70000000000000000000000000000000000000000000000000000000000000020"
            "1111111111111111111111111111111111111111111111111111111111111111"
            "0000000000000000000000000000000000000000000000000000000000000040"
            "0000000000000000000000002222222222222222222222222222222222222222"
            "000000000000000000000000000000000000000000000000000000000000002a"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "3333333333333333333333333333333333333333333333333333333333333333"
            "00000000000000000000000000000000000000000000000000000000000000c0"
            "0000000000000000000000000000000000000000000000000000000000000007"
            "0000000000000000000000000000000000000000000000000000000000000006"
            "010264656d6f0000000000000000000000000000000000000000000000000000"
        )
        actual = encode_attest(
            SCHEMA,
            RECIPIENT,
            b"\x01\x02demo",
            expiration=42,
            revocable=False,
            ref_uid=REF_UID,
            value=7,
        )

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
