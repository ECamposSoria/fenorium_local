"""Offline Midnight maker offers, ratifier trees and raw mempool calldata.

Addresses are hex strings; hashes and ABI bytes are bytes. Struct field names follow
Solidity so the same offer can be encoded for calldata or a wallet's typed data.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from fractions import Fraction
import gzip
import io
import re
import zlib
from typing import Sequence

from engine.keccak import keccak256

MIDNIGHT_VENUES = frozenset({"Tenor", "Morpho Midnight"})

ZERO_ADDRESS = '0x' + '00' * 20
ZERO_HASH = bytes(32)
WAD = 10**18
LN_ONE_PLUS_DELTA = 4987541511039073
MAX_TICK = 6744
PRICE_ROUNDING_STEP = 10**11
DEFAULT_TICK_SPACING = 4
SECONDS_PER_YEAR = 31_536_000
MAX_TREE_HEIGHT = 20
DEFAULT_CONTINUOUS_FEE_CAP = 0

COLLATERAL_PARAMS_TYPE = 'CollateralParams(address token,uint256 lltv,uint256 liquidationCursor,address oracle)'
MARKET_TYPE = 'Market(uint256 chainId,address midnight,address loanToken,CollateralParams[] collateralParams,uint256 maturity,uint256 rcfThreshold,address enterGate,address liquidatorGate)'
OFFER_TYPE = 'Offer(Market market,bool buy,address maker,uint256 start,uint256 expiry,uint256 tick,bytes32 group,address callback,bytes callbackData,address receiverIfMakerIsSeller,address ratifier,bool reduceOnly,uint128 maxUnits,uint128 maxAssets,uint256 continuousFeeCap)'
EIP712_DOMAIN_TYPE = 'EIP712Domain(uint256 chainId,address verifyingContract)'
COLLATERAL_PARAMS_TYPEHASH = bytes.fromhex('39ed3f928d24fd00574b1a02aba9c2483abcf5d9a3a366118c9a5aa29885b841')
MARKET_TYPEHASH = bytes.fromhex('510b3862f3816a109c9340b76972e8a30984246be06e034ae12ed2934220391a')
OFFER_TYPEHASH = bytes.fromhex('9905214264a9fb7b6cc1b0e33db7a04687c6e4185a84755d29914314aa9d8906')
EIP712_DOMAIN_TYPEHASH = bytes.fromhex('47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218')


def _uint(value, bits=256):
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f'expected uint{bits}')
    return value


def _bytes(value, size=None):
    if isinstance(value, str):
        if not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', value):
            raise ValueError('expected 0x-prefixed bytes')
        value = bytes.fromhex(value[2:])
    if not isinstance(value, (bytes, bytearray)):
        raise ValueError('expected bytes')
    value = bytes(value)
    if size is not None and len(value) != size:
        raise ValueError(f'expected {size} bytes')
    return value


def _address(value):
    return '0x' + _bytes(value, 20).hex()


@dataclass(frozen=True)
class CollateralParams:
    """Collateral terms; percentages retain their contract integer scale."""

    token: str
    lltv: int
    liquidationCursor: int
    oracle: str


@dataclass(frozen=True)
class Market:
    """A Midnight market identity, including its chain and collateral order."""

    chainId: int
    midnight: str
    loanToken: str
    collateralParams: tuple[CollateralParams, ...]
    maturity: int
    rcfThreshold: int
    enterGate: str
    liquidatorGate: str

    def __post_init__(self):
        object.__setattr__(self, 'collateralParams', tuple(self.collateralParams))


@dataclass(frozen=True)
class Offer:
    """Maker terms; buy means the lender buys credit with loan assets."""

    market: Market
    buy: bool
    maker: str
    start: int
    expiry: int
    tick: int
    group: bytes
    callback: str
    callbackData: bytes
    receiverIfMakerIsSeller: str
    ratifier: str
    reduceOnly: bool
    maxUnits: int
    maxAssets: int
    continuousFeeCap: int


@dataclass(frozen=True)
class RatifierSignature:
    """An Ecrecover signature with Ethereum v (27/28), not parity (0/1)."""

    v: int
    r: bytes
    s: bytes

    def __post_init__(self):
        if type(self.v) is not int or self.v not in (27, 28):
            raise ValueError('signature v must be 27 or 28')
        object.__setattr__(self, 'r', _bytes(self.r, 32))
        object.__setattr__(self, 's', _bytes(self.s, 32))


@dataclass(frozen=True)
class MidnightAddresses:
    midnight: str
    ratifier: str
    mempool: str


ADDRESSES: dict[int, MidnightAddresses] = {
    1: MidnightAddresses('0x471686c42792f93528b000bef54bc10e3aa2045f', '0xac439c81caa6ef4c7b7e8f0110f8ce63a4b6d43e', '0xde2d62449301a09a51ebf9326ea60d2e8bf4a8f7'),
    8453: MidnightAddresses('0xadedd8ab6de832766fedf0fac4992e5c4d3ea18a', '0xd6e70365c8e8dda9a4ca662c07bbe663b017755e', '0xdd6dce32e21f7b020898a8258da37355b4017993'),
    5042: MidnightAddresses('0x208786922be56fde2d1fa60e6b9ec5d723e8d7b0', '0xa3b53ade6668b6cec03a9e56a993b47034f43715', '0x26bded5fb01373ce875dea14e52799d04c839c1a'),
}
EMPTY_OFFER = Offer(Market(0, ZERO_ADDRESS, ZERO_ADDRESS, (), 0, 0, ZERO_ADDRESS, ZERO_ADDRESS),
                    False, ZERO_ADDRESS, 0, 0, 0, ZERO_HASH, ZERO_ADDRESS, b'',
                    ZERO_ADDRESS, ZERO_ADDRESS, False, 0, 0, 0)

COLLATERAL_ABI = '(address,uint256,uint256,address)'
MARKET_ABI = f'(uint256,address,address,{COLLATERAL_ABI}[],uint256,uint256,address,address)'
OFFER_ABI = f'({MARKET_ABI},bool,address,uint256,uint256,uint256,bytes32,address,bytes,address,address,bool,uint128,uint128,uint256)'
RATIFIER_DATA_TYPES = ('(uint8,bytes32,bytes32)', 'bytes32', 'uint256', 'bytes32[]')
ITEMS_ABI = f'({OFFER_ABI},bytes)[]'


def _parse_type(source):
    source = source.replace(' ', '')
    def parse(pos):
        if pos < len(source) and source[pos] == '(':
            members = []
            pos += 1
            while True:
                member, pos = parse(pos)
                members.append(member)
                if pos >= len(source):
                    raise ValueError('unterminated tuple')
                if source[pos] == ')':
                    pos += 1
                    break
                if source[pos] != ',':
                    raise ValueError('invalid tuple')
                pos += 1
            result = ('tuple', tuple(members))
        else:
            match = re.match(r'uint(?:8|128|256)|address|bool|bytes32|bytes', source[pos:])
            if not match:
                raise ValueError('unsupported ABI type')
            result = match.group()
            pos += len(result)
        while source[pos:pos + 2] == '[]':
            result = ('array', result)
            pos += 2
        return result, pos
    result, end = parse(0)
    if end != len(source):
        raise ValueError('unsupported ABI type')
    return result


def _dynamic(kind):
    if isinstance(kind, tuple):
        return kind[0] == 'array' or any(_dynamic(k) for k in kind[1])
    return kind == 'bytes'


def _size(kind):
    if _dynamic(kind):
        return 32
    if isinstance(kind, tuple):
        return sum(_size(k) for k in kind[1])
    return 32


def _word(value):
    return _uint(value).to_bytes(32, 'big')


def _encode_sequence(kinds, values):
    values = tuple(values)
    if len(kinds) != len(values):
        raise ValueError('ABI arity mismatch')
    offset = sum(_size(k) for k in kinds)
    heads, tails = [], []
    for kind, value in zip(kinds, values):
        encoded = _encode(kind, value)
        if _dynamic(kind):
            heads.append(_word(offset))
            tails.append(encoded)
            offset += len(encoded)
        else:
            heads.append(encoded)
    return b''.join(heads + tails)


def _encode(kind, value):
    if isinstance(kind, tuple):
        if kind[0] == 'array':
            value = tuple(value)
            return _word(len(value)) + _encode_sequence((kind[1],) * len(value), value)
        return _encode_sequence(kind[1], value)
    if kind.startswith('uint'):
        return _word(_uint(value, int(kind[4:])))
    if kind == 'bool':
        if type(value) is not bool:
            raise ValueError('expected bool')
        return _word(int(value))
    if kind == 'address':
        return bytes(12) + _bytes(value, 20)
    if kind == 'bytes32':
        return _bytes(value, 32)
    value = _bytes(value)
    return _word(len(value)) + value + bytes((-len(value)) % 32)


def abi_encode(types, values) -> bytes:
    """Encode canonical ABI type strings; tuple offsets use their own head base."""
    return _encode_sequence(tuple(_parse_type(t) for t in types), values)


def abi_decode(types, data) -> tuple:
    """Decode ABI values to tuples; reject truncated or noncanonical encodings."""
    data = _bytes(data)
    def word(pos):
        if pos < 0 or pos + 32 > len(data):
            raise ValueError('truncated ABI word')
        return int.from_bytes(data[pos:pos + 32], 'big')

    def sequence(kinds, base):
        head_size = sum(_size(k) for k in kinds)
        if base + head_size > len(data):
            raise ValueError('truncated ABI head')
        values, pos = [], base
        for kind in kinds:
            target = pos
            if _dynamic(kind):
                offset = word(pos)
                if offset % 32 or offset < head_size:
                    raise ValueError('invalid ABI offset')
                target = base + offset
            values.append(decode(kind, target))
            pos += _size(kind)
        return tuple(values)

    def decode(kind, pos):
        if isinstance(kind, tuple):
            if kind[0] == 'array':
                count = word(pos)
                if count > (len(data) - pos - 32) // _size(kind[1]):
                    raise ValueError('truncated ABI array')
                return sequence((kind[1],) * count, pos + 32)
            return sequence(kind[1], pos)
        value = word(pos)
        if kind.startswith('uint'):
            return _uint(value, int(kind[4:]))
        if kind == 'bool':
            if value not in (0, 1):
                raise ValueError('invalid ABI bool')
            return bool(value)
        if kind == 'address':
            if value >= 1 << 160:
                raise ValueError('invalid address padding')
            return '0x' + data[pos + 12:pos + 32].hex()
        if kind == 'bytes32':
            return data[pos:pos + 32]
        end = pos + 32 + value
        padded_end = end + (-value) % 32
        if padded_end > len(data) or any(data[end:padded_end]):
            raise ValueError('invalid ABI bytes')
        return data[pos + 32:end]

    kinds = tuple(_parse_type(t) for t in types)
    result = sequence(kinds, 0)
    if _encode_sequence(kinds, result) != data:
        raise ValueError('noncanonical ABI encoding')
    return result


def _values(value):
    if is_dataclass(value):
        return tuple(_values(getattr(value, f.name)) for f in fields(value))
    if isinstance(value, (tuple, list)):
        return tuple(_values(v) for v in value)
    return value


def hash_collateral_params(params: CollateralParams) -> bytes:
    """Hash one collateral struct with its EIP-712 type prefix."""
    return keccak256(abi_encode(('bytes32', 'address', 'uint256', 'uint256', 'address'),
                               (COLLATERAL_PARAMS_TYPEHASH, *_values(params))))


def hash_market(market: Market) -> bytes:
    """Hash a market; collateral arrays hash concatenated element hashes."""
    values = list(_values(market))
    values[3] = keccak256(b''.join(hash_collateral_params(c) for c in market.collateralParams))
    return keccak256(abi_encode(('bytes32', 'uint256', 'address', 'address', 'bytes32',
                                'uint256', 'uint256', 'address', 'address'),
                               (MARKET_TYPEHASH, *values)))


def hash_offer(offer: Offer) -> bytes:
    """Hash maker terms; callback bytes are hashed rather than ABI-offset encoded."""
    values = list(_values(offer))
    values[0] = hash_market(offer.market)
    values[8] = keccak256(_bytes(offer.callbackData))
    return keccak256(abi_encode(('bytes32', 'bytes32', 'bool', 'address', 'uint256',
                                'uint256', 'uint256', 'bytes32', 'address', 'bytes32',
                                'address', 'address', 'bool', 'uint128', 'uint128', 'uint256'),
                               (OFFER_TYPEHASH, *values)))


def hash_node(left, right) -> bytes:
    """Hash an ordered pair; Midnight does not sort siblings."""
    return keccak256(_bytes(left, 32) + _bytes(right, 32))


def _padded_offers(offers):
    offers = list(offers)
    if not 1 <= len(offers) <= 1 << MAX_TREE_HEIGHT:
        raise ValueError('offer tree must contain 1 through 2**20 offers')
    count = 1 << (len(offers) - 1).bit_length()
    return offers + [EMPTY_OFFER] * (count - len(offers))


def tree_leaves(offers: Sequence[Offer]) -> list[bytes]:
    """Hash offers padded with the canonical empty offer to a power of two."""
    return [hash_offer(o) for o in _padded_offers(offers)]


def _leaves(leaves):
    leaves = [_bytes(leaf, 32) for leaf in leaves]
    count = len(leaves)
    if not count or count & (count - 1) or count > 1 << MAX_TREE_HEIGHT:
        raise ValueError('leaf count must be a power of two through 2**20')
    return leaves


def tree_root(leaves) -> bytes:
    """Reduce a complete tree; a single leaf is already its root."""
    level = _leaves(leaves)
    while len(level) > 1:
        level = [hash_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def tree_proof(leaves, index: int) -> list[bytes]:
    """Return bottom-up siblings for the leaf's original position."""
    level = _leaves(leaves)
    _uint(index)
    if index >= len(level):
        raise ValueError('leaf index out of range')
    proof = []
    while len(level) > 1:
        proof.append(level[index ^ 1])
        index //= 2
        level = [hash_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return proof


def is_leaf(root, leaf, leaf_index: int, proof) -> bool:
    """Check an ordered proof, including whether the index fits its height."""
    proof = tuple(proof)
    if type(leaf_index) is not int or not 0 <= leaf_index < 1 << len(proof) or len(proof) > MAX_TREE_HEIGHT:
        return False
    current = _bytes(leaf, 32)
    for i, sibling in enumerate(proof):
        current = hash_node(sibling, current) if leaf_index >> i & 1 else hash_node(current, sibling)
    return current == _bytes(root, 32)


def offer_tree_type_hash(height: int) -> bytes:
    """Construct the ratifier type hash; only heights 0 through 20 exist."""
    if type(height) is not int or not 0 <= height <= MAX_TREE_HEIGHT:
        raise ValueError('unsupported tree height')
    return keccak256(('OfferTree(Offer' + '[2]' * height + ' offerTree)' +
                      COLLATERAL_PARAMS_TYPE + MARKET_TYPE + OFFER_TYPE).encode())


def domain_separator(chain_id: int, ratifier: str) -> bytes:
    """Bind signatures to chain and ratifier, without a name or version."""
    return keccak256(abi_encode(('bytes32', 'uint256', 'address'),
                               (EIP712_DOMAIN_TYPEHASH, chain_id, ratifier)))


def digest(root, height: int, chain_id: int, ratifier: str) -> bytes:
    """Return the EIP-712 digest signed for an entire offer tree."""
    struct_hash = keccak256(abi_encode(('bytes32', 'bytes32'), (offer_tree_type_hash(height), root)))
    return keccak256(b'\x19\x01' + domain_separator(chain_id, ratifier) + struct_hash)


def typed_data(offers, chain_id: int, ratifier: str) -> dict:
    """Build JSON-ready wallet data; integer strings preserve uint256 precision."""
    padded = _padded_offers(offers)
    height = (len(padded) - 1).bit_length()
    _uint(chain_id)
    ratifier = _address(ratifier)
    definitions = (COLLATERAL_PARAMS_TYPE, MARKET_TYPE, OFFER_TYPE, EIP712_DOMAIN_TYPE)
    types = {}
    for definition in definitions:
        name, body = definition[:-1].split('(', 1)
        types[name] = [dict(zip(('type', 'name'), field.split())) for field in body.split(',')]
    types['OfferTree'] = [{'name': 'offerTree', 'type': 'Offer' + '[2]' * height}]

    def convert(name, value):
        result = {}
        for field in types[name]:
            kind, key = field['type'], field['name']
            item = getattr(value, key)
            if kind.endswith('[]'):
                item = [convert(kind[:-2], v) for v in item]
            elif kind in types:
                item = convert(kind, item)
            elif kind.startswith('uint'):
                item = str(_uint(item, int(kind[4:])))
            elif kind == 'address':
                item = _address(item)
            elif kind.startswith('bytes'):
                item = '0x' + _bytes(item, 32 if kind == 'bytes32' else None).hex()
            elif type(item) is not bool:
                raise ValueError('expected bool')
            result[key] = item
        return result

    def pairs(values):
        if len(values) == 1:
            return values[0]
        half = len(values) // 2
        return [pairs(values[:half]), pairs(values[half:])]

    return {'domain': {'chainId': str(chain_id), 'verifyingContract': ratifier},
            'primaryType': 'OfferTree', 'types': types,
            'message': {'offerTree': pairs([convert('Offer', o) for o in padded])}}


def ratifier_data(signature: RatifierSignature, root, leaf_index: int, proof) -> bytes:
    """Encode the static signature tuple before the dynamic sibling array."""
    proof = tuple(proof)
    _uint(leaf_index)
    if len(proof) > MAX_TREE_HEIGHT or leaf_index >= 1 << len(proof):
        raise ValueError('leaf index or proof height out of range')
    return abi_encode(RATIFIER_DATA_TYPES, (_values(signature), root, leaf_index, proof))


def encode_items(items) -> bytes:
    """Encode (offer, ratifierData) pairs as one dynamic ABI argument."""
    return abi_encode((ITEMS_ABI,), (tuple((_values(o), r) for o, r in items),))


def decode_items(data) -> list[tuple[Offer, bytes]]:
    """Recover maker structs without treating ABI bytes as a function call."""
    result = []
    for values, ratifier in abi_decode((ITEMS_ABI,), data)[0]:
        market = list(values[0])
        market[3] = tuple(CollateralParams(*c) for c in market[3])
        result.append((Offer(Market(*market), *values[1:]), ratifier))
    return result


_MAX_PAYLOAD_BYTES = 1_000_000
_MAX_SUFFIX_BYTES = 256
_MAX_COMPRESSED_BYTES = _MAX_PAYLOAD_BYTES - 5 - _MAX_SUFFIX_BYTES
_MAX_ITEMS_BYTES = 6_000_000


def encode_payload(items) -> bytes:
    """Build raw mempool calldata with deterministic gzip and no selector."""
    data = encode_items(items)
    if len(data) > _MAX_ITEMS_BYTES:
        raise ValueError('items exceed payload limit')
    compressed = gzip.compress(data, mtime=0)
    if len(compressed) > _MAX_COMPRESSED_BYTES:
        raise ValueError('compressed payload exceeds limit')
    return b'\x01' + len(compressed).to_bytes(4, 'big') + compressed


def decode_payload(data) -> list[tuple[Offer, bytes]]:
    """Decode the bounded gzip member, ignoring at most 256 attribution bytes."""
    data = _bytes(data)
    if len(data) < 5 or len(data) > _MAX_PAYLOAD_BYTES or data[0] != 1:
        raise ValueError('invalid payload header')
    size = int.from_bytes(data[1:5], 'big')
    end = 5 + size
    if not 0 < size <= _MAX_COMPRESSED_BYTES or end > len(data) or len(data) - end > _MAX_SUFFIX_BYTES:
        raise ValueError('invalid compressed payload length')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data[5:end])) as stream:
            decoded = stream.read(_MAX_ITEMS_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError('invalid gzip payload') from exc
    if len(decoded) > _MAX_ITEMS_BYTES:
        raise ValueError('decompressed payload exceeds limit')
    return decode_items(decoded)


def _trunc_div(numerator, denominator):
    return numerator // denominator if numerator >= 0 else -((-numerator) // denominator)


def _w_exp(x):
    if x < 0:
        return WAD * WAD // _w_exp(-x)
    ln2 = 693147180559945309
    q = (x + 322611214989459870) // ln2
    r = x - q * ln2
    second = r * r // (2 * WAD)
    third = _trunc_div(second * r, 3 * WAD)
    return (WAD + r + second + third) << q


def _half_down(x, divisor):
    return (x + (divisor - 1) // 2) // divisor


def tick_to_price(tick: int) -> int:
    """Return the contract's rounded WAD price, using signed truncating division."""
    _uint(tick)
    if tick > MAX_TICK:
        raise ValueError('tick exceeds maximum')
    denominator = WAD + _w_exp(LN_ONE_PLUS_DELTA * (MAX_TICK // 2 - tick))
    return _half_down(_half_down(WAD * WAD, denominator), PRICE_ROUNDING_STEP) * PRICE_ROUNDING_STEP


def _spacing(spacing):
    _uint(spacing)
    if not 1 <= spacing <= MAX_TICK:
        raise ValueError('invalid tick spacing')


def price_to_tick(price: int, spacing: int = DEFAULT_TICK_SPACING) -> int:
    """Find the first covering tick and round upward to the requested spacing."""
    _uint(price)
    _spacing(spacing)
    if price > tick_to_price(MAX_TICK):
        raise ValueError('price exceeds highest tick')
    low, high = 0, MAX_TICK
    while low < high:
        mid = (low + high) // 2
        if tick_to_price(mid) >= price:
            high = mid
        else:
            low = mid + 1
    tick = ((low + spacing - 1) // spacing) * spacing
    if tick > MAX_TICK:
        raise ValueError('no spaced tick covers price')
    return tick


def lender_apr(price_wad: int, seconds_to_maturity: int) -> Fraction:
    """Simple annual lending return as a ratio; 0.03 denotes three percent."""
    _uint(price_wad)
    _uint(seconds_to_maturity)
    if not 0 < price_wad <= WAD or seconds_to_maturity == 0:
        raise ValueError('positive price and remaining duration required')
    return Fraction((WAD - price_wad) * SECONDS_PER_YEAR, price_wad * seconds_to_maturity)


def bid_tick_for_apr(apr, seconds: int, spacing: int = DEFAULT_TICK_SPACING) -> int:
    """Choose the highest qualifying bid; zero rounded prices cannot earn an APR."""
    _uint(seconds)
    _spacing(spacing)
    try:
        apr = Fraction(str(apr)) if isinstance(apr, float) else Fraction(apr)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError('invalid APR') from exc
    if apr < 0 or not seconds:
        raise ValueError('nonnegative APR and positive duration required')
    target = WAD / (1 + apr * seconds / SECONDS_PER_YEAR)
    if tick_to_price(0) > target:
        raise ValueError('no tick meets requested APR')
    low, high = 0, MAX_TICK // spacing
    while low < high:
        mid = (low + high + 1) // 2
        if tick_to_price(mid * spacing) <= target:
            low = mid
        else:
            high = mid - 1
    tick = low * spacing
    if tick_to_price(tick) == 0:
        raise ValueError('no positive-price tick meets requested APR')
    return tick


def lending_bid(market: Market, maker: str, assets_raw: int, apr, start: int,
                expiry: int, chain_id: int, spacing: int = DEFAULT_TICK_SPACING) -> Offer:
    """Create an asset-capped lender bid; APR uses the duration from start."""
    _uint(chain_id)
    _uint(start)
    _uint(expiry)
    _uint(assets_raw, 128)
    addresses = ADDRESSES.get(chain_id)
    if addresses is None or market.chainId != chain_id or _address(market.midnight) != addresses.midnight:
        raise ValueError('market does not match chain deployment')
    if not assets_raw or not start < expiry <= market.maturity:
        raise ValueError('positive assets and start < expiry <= maturity required')
    maker = _address(maker)
    tick = bid_tick_for_apr(apr, market.maturity - start, spacing)
    # Midnight shares consumption by (maker, group), so each order is its own group. The id is
    # the singleton rule of Morpho's SDK (GroupUtils.hash): keccak256 of the offer's hash with a
    # zero group. The mempool indexer refuses any other derivation (rule group_identity).
    offer = Offer(market, True, maker, start, expiry,
                  tick, ZERO_HASH,
                  ZERO_ADDRESS, b'', ZERO_ADDRESS, addresses.ratifier, False,
                  0, assets_raw, DEFAULT_CONTINUOUS_FEE_CAP)
    return replace(offer, group=keccak256(hash_offer(offer)))
