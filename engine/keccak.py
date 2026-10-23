"""Pure-Python keccak256.

Ethereum uses original Keccak padding (0x01), not the NIST SHA-3 padding (0x06) that
``hashlib.sha3_256`` implements, so the standard library cannot be used here. The engine has no
crypto dependency and the offline replica must produce byte-identical output on any machine, so
this is implemented directly rather than pulling in ``eth-hash``.

Verified against ``cast keccak`` and the published empty-string vector; see ``tests/test_keccak.py``.
"""

from __future__ import annotations

_MASK = (1 << 64) - 1

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

_ROTATIONS = (1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44)

_PI_LANE = (10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1)

_RATE_BYTES = 136  # 1600 bits state - 2 * 256 bits capacity


def _rotl64(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK


def _keccak_f1600(state: list[int]) -> None:
    for round_constant in _ROUND_CONSTANTS:
        # theta
        column = [state[i] ^ state[i + 5] ^ state[i + 10] ^ state[i + 15] ^ state[i + 20] for i in range(5)]
        for i in range(5):
            diffusion = column[(i + 4) % 5] ^ _rotl64(column[(i + 1) % 5], 1)
            for row in range(0, 25, 5):
                state[row + i] ^= diffusion

        # rho and pi
        carried = state[1]
        for i in range(24):
            lane = _PI_LANE[i]
            held = state[lane]
            state[lane] = _rotl64(carried, _ROTATIONS[i])
            carried = held

        # chi
        for row in range(0, 25, 5):
            plane = state[row:row + 5]
            for i in range(5):
                state[row + i] = plane[i] ^ ((~plane[(i + 1) % 5] & _MASK) & plane[(i + 2) % 5])

        # iota
        state[0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte keccak256 digest of ``data``."""
    state = [0] * 25

    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE_BYTES != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for offset in range(0, len(padded), _RATE_BYTES):
        block = padded[offset:offset + _RATE_BYTES]
        for lane in range(_RATE_BYTES // 8):
            state[lane] ^= int.from_bytes(block[lane * 8:lane * 8 + 8], "little")
        _keccak_f1600(state)

    out = bytearray()
    for lane in range(4):  # 4 lanes * 8 bytes = 32 bytes
        out += state[lane].to_bytes(8, "little")
    return bytes(out)


def keccak256_hex(data: bytes) -> str:
    """Return the digest as an ``0x``-prefixed lowercase hex string."""
    return "0x" + keccak256(data).hex()
