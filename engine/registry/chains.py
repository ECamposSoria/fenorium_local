"""The chains Fenorium knows, read from `chains.json` beside this module.

Every chain-keyed table in the collector, the portfolio backend, the router and the web app is
derived from this one file. Adding an EVM chain is an entry here plus whatever provider support
that chain needs; nothing else should have to learn its id.

A chain's `roles` say what this deployment does on it:

- `collect`: the nightly collector discovers instruments there.
- `buy`: an instrument there may be the destination of a purchase.
- `pay`: a buyer may pay from it. Fenorium's routing fee is paid to `fee_recipient` on the paying
  chain, so a chain is given this role only after the fee Safe was proved deployable at that
  address there (`fee_safe` is `deployable` or `deployed`).
- `portfolio`: the server reads wallet balances there.
- `demo`: the testnet demo runs there.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


CHAINS_PATH = Path(__file__).with_name("chains.json")
SCHEMA_VERSION = "fenorium/chains/1"
ROLES = frozenset({"collect", "buy", "pay", "portfolio", "demo"})
FEE_SAFE_STATES = frozenset({"deployed", "deployable"})
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class RegistryError(ValueError):
    """The chain file is malformed, or a caller asked about a chain it does not contain."""


@dataclass(frozen=True, slots=True)
class Token:
    symbol: str
    address: str
    decimals: int
    stablecoin: bool = False


@dataclass(frozen=True, slots=True)
class Chain:
    chain_id: int
    key: str
    name: str
    short: str
    # How the collector labels this chain in the curve and universe rows it writes.
    label: str
    testnet: bool
    block_seconds: float | None
    native_symbol: str
    native_decimals: int
    native_coingecko_id: str | None
    rpc_env: str
    rpc_public: tuple[str, ...]
    explorer: str | None
    # Whether Blockscout's multichain index (api.blockscout.com) answers for this chain.
    blockscout_index: bool
    # This chain's own Blockscout instance, which serves token holder lists.
    blockscout_api: str | None
    multicall3: str | None
    # The USDC a route is quoted in and a pay-only chain is paid from.
    usdc: Token | None
    # The cash a wallet holds here that Fenorium reads and prices but never offers as an instrument.
    settlement: tuple[Token, ...]
    # Arc's gas token is its USDC: offering both would count one balance twice.
    offer_native: bool
    defillama: str | None
    coingecko_platform: str | None
    # How DefiLlama's yields index names this chain; it has used more than one name for some.
    llama_yields: tuple[str, ...]
    spectra: str | None
    # Whether Pendle lists markets here; the collector asks Pendle only about these chains.
    pendle: bool
    roles: frozenset[str]
    fee_safe: str | None

    def has(self, role: str) -> bool:
        if role not in ROLES:
            raise RegistryError(f"unknown chain role {role!r}")
        return role in self.roles

    def rpc_endpoints(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        """Override-first, de-duplicated. The override is read at call time so a scheduled job can
        inject a private RPC without its URL or key ever entering source control."""
        override = (os.environ if environ is None else environ).get(self.rpc_env, "").strip()
        return tuple(dict.fromkeys(((override,) if override else ()) + self.rpc_public))

    def llama_coin(self, address: str | None) -> str | None:
        """DefiLlama's coin key for a token on this chain, or for its gas token when address is None."""
        if address is None:
            return f"coingecko:{self.native_coingecko_id}" if self.native_coingecko_id else None
        return f"{self.defillama}:{address.lower()}" if self.defillama else None

    def to_public(self) -> dict[str, Any]:
        """What a browser or another service may be told about this chain."""
        return {
            "chain_id": self.chain_id,
            "key": self.key,
            "name": self.name,
            "short": self.short,
            "testnet": self.testnet,
            "block_seconds": self.block_seconds,
            "history_index": self.blockscout_index,
            "native": {"symbol": self.native_symbol, "decimals": self.native_decimals},
            "rpc_url": self.rpc_public[0] if self.rpc_public else None,
            "explorer": self.explorer,
            "multicall3": self.multicall3,
            "usdc": None if self.usdc is None else {
                "symbol": self.usdc.symbol, "address": self.usdc.address, "decimals": self.usdc.decimals,
            },
            "offer_native": self.offer_native,
            "roles": sorted(self.roles),
            "fee_safe": self.fee_safe,
        }


def _text(value: Any, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RegistryError(f"{where} must be a non-empty string")
    return value


def _https(value: Any, where: str, *, optional: bool = False) -> str | None:
    text = _text(value, where, optional=optional)
    if text is not None and (not text.startswith("https://") or any(c.isspace() for c in text)):
        raise RegistryError(f"{where} must be an https URL")
    return text


def _address(value: Any, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        raise RegistryError(f"{where} must be a 0x-prefixed 20-byte address")
    return value


def _decimals(value: Any, where: str) -> int:
    if type(value) is not int or not 0 <= value <= 36:
        raise RegistryError(f"{where} must be an integer in [0, 36]")
    return value


def _flag(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise RegistryError(f"{where} must be a boolean")
    return value


def _chain(raw: Any, index: int) -> Chain:
    where = f"chains[{index}]"
    if not isinstance(raw, Mapping):
        raise RegistryError(f"{where} must be an object")
    chain_id = raw.get("chain_id")
    if type(chain_id) is not int or chain_id <= 0:
        raise RegistryError(f"{where}.chain_id must be a positive integer")
    key = raw.get("key")
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise RegistryError(f"{where}.key must be a lowercase slug")
    native = raw.get("native")
    rpc = raw.get("rpc")
    ids = raw.get("ids")
    if not isinstance(native, Mapping) or not isinstance(rpc, Mapping) or not isinstance(ids, Mapping):
        raise RegistryError(f"{where} must have native, rpc and ids objects")
    env = rpc.get("env")
    if not isinstance(env, str) or _ENV.fullmatch(env) is None:
        raise RegistryError(f"{where}.rpc.env must be an environment variable name")
    public = rpc.get("public")
    if not isinstance(public, list):
        raise RegistryError(f"{where}.rpc.public must be a list")
    roles = raw.get("roles")
    if not isinstance(roles, list) or not set(roles) <= ROLES or len(set(roles)) != len(roles):
        raise RegistryError(f"{where}.roles must be distinct values from {sorted(ROLES)}")
    fee_safe = raw.get("fee_safe")
    if fee_safe is not None and fee_safe not in FEE_SAFE_STATES:
        raise RegistryError(f"{where}.fee_safe must be one of {sorted(FEE_SAFE_STATES)} or null")
    if "pay" in roles and fee_safe is None:
        # Fees are paid to the Safe's address on the paying chain. Where it cannot be deployed at
        # that address they could never be moved.
        raise RegistryError(f"{where} has the pay role but no proof the fee Safe is deployable there")
    testnet = raw.get("testnet")
    if type(testnet) is not bool:
        raise RegistryError(f"{where}.testnet must be a boolean")
    if testnet and roles and set(roles) - {"portfolio", "demo"}:
        raise RegistryError(f"{where} is a testnet and may only have the portfolio and demo roles")
    block_seconds = raw.get("block_seconds")
    if block_seconds is not None and (isinstance(block_seconds, bool)
                                      or not isinstance(block_seconds, (int, float)) or block_seconds <= 0):
        raise RegistryError(f"{where}.block_seconds must be positive or null")
    usdc = raw.get("usdc")
    token = None
    if usdc is not None:
        if not isinstance(usdc, Mapping):
            raise RegistryError(f"{where}.usdc must be an object or null")
        token = Token(
            symbol=_text(usdc.get("symbol"), f"{where}.usdc.symbol"),
            address=_address(usdc.get("address"), f"{where}.usdc.address"),
            decimals=_decimals(usdc.get("decimals"), f"{where}.usdc.decimals"),
        )
    settlement = raw.get("settlement", [])
    if not isinstance(settlement, list):
        raise RegistryError(f"{where}.settlement must be a list")
    tokens = []
    for position, item in enumerate(settlement):
        if not isinstance(item, Mapping) or type(item.get("stablecoin")) is not bool:
            raise RegistryError(f"{where}.settlement[{position}] must be an object with a boolean stablecoin")
        tokens.append(Token(
            symbol=_text(item.get("symbol"), f"{where}.settlement[{position}].symbol"),
            address=_address(item.get("address"), f"{where}.settlement[{position}].address"),
            decimals=_decimals(item.get("decimals"), f"{where}.settlement[{position}].decimals"),
            stablecoin=item["stablecoin"],
        ))
    offer_native = raw.get("offer_native", True)
    if type(offer_native) is not bool:
        raise RegistryError(f"{where}.offer_native must be a boolean")
    blockscout = raw.get("blockscout_index", False)
    if type(blockscout) is not bool:
        raise RegistryError(f"{where}.blockscout_index must be a boolean")
    yields = ids.get("llama_yields", [])
    if not isinstance(yields, list) or not all(isinstance(name, str) and name for name in yields):
        raise RegistryError(f"{where}.ids.llama_yields must be a list of names")
    return Chain(
        chain_id=chain_id,
        key=key,
        name=_text(raw.get("name"), f"{where}.name"),
        short=_text(raw.get("short"), f"{where}.short"),
        label=_text(raw.get("label"), f"{where}.label"),
        testnet=testnet,
        block_seconds=block_seconds,
        native_symbol=_text(native.get("symbol"), f"{where}.native.symbol"),
        native_decimals=_decimals(native.get("decimals"), f"{where}.native.decimals"),
        native_coingecko_id=_text(native.get("coingecko_id"), f"{where}.native.coingecko_id", optional=True),
        rpc_env=env,
        rpc_public=tuple(_https(url, f"{where}.rpc.public[]") for url in public),
        explorer=_https(raw.get("explorer"), f"{where}.explorer", optional=True),
        blockscout_index=blockscout,
        blockscout_api=_https(raw.get("blockscout_api"), f"{where}.blockscout_api", optional=True),
        multicall3=_address(raw.get("multicall3"), f"{where}.multicall3", optional=True),
        usdc=token,
        settlement=tuple(tokens),
        offer_native=offer_native,
        defillama=_text(ids.get("defillama"), f"{where}.ids.defillama", optional=True),
        coingecko_platform=_text(ids.get("coingecko_platform"), f"{where}.ids.coingecko_platform", optional=True),
        llama_yields=tuple(yields),
        spectra=_text(ids.get("spectra"), f"{where}.ids.spectra", optional=True),
        pendle=_flag(ids.get("pendle", False), f"{where}.ids.pendle"),
        roles=frozenset(roles),
        fee_safe=fee_safe,
    )


@dataclass(frozen=True, slots=True)
class ChainRegistry:
    fee_recipient: str
    chains: tuple[Chain, ...]

    @classmethod
    def from_document(cls, document: Any) -> "ChainRegistry":
        if not isinstance(document, Mapping) or document.get("schema_version") != SCHEMA_VERSION:
            raise RegistryError(f"chain file must declare schema_version {SCHEMA_VERSION}")
        raw = document.get("chains")
        if not isinstance(raw, list) or not raw:
            raise RegistryError("chain file must list at least one chain")
        chains = tuple(_chain(entry, index) for index, entry in enumerate(raw))
        ids = [chain.chain_id for chain in chains]
        keys = [chain.key for chain in chains]
        if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise RegistryError("chain ids and keys must be unique")
        names: dict[str, int] = {}
        for chain in chains:
            for name in _names(chain):
                if names.setdefault(name, chain.chain_id) != chain.chain_id:
                    # One spelling resolving to two chains would let a source's label decide
                    # silently which chain an instrument lives on.
                    raise RegistryError(f"chain name {name!r} is claimed by two chains")
        return cls(
            fee_recipient=_address(document.get("fee_recipient"), "fee_recipient"),
            chains=tuple(sorted(chains, key=lambda chain: chain.chain_id)),
        )

    def get(self, chain_id: Any) -> Chain | None:
        if type(chain_id) is not int:
            return None
        for chain in self.chains:
            if chain.chain_id == chain_id:
                return chain
        return None

    def require(self, chain_id: Any) -> Chain:
        chain = self.get(chain_id)
        if chain is None:
            raise RegistryError(f"chain {chain_id!r} is not in the registry")
        return chain

    def with_role(self, role: str) -> tuple[Chain, ...]:
        return tuple(chain for chain in self.chains if chain.has(role))

    def ids_with_role(self, role: str) -> frozenset[int]:
        return frozenset(chain.chain_id for chain in self.with_role(role))

    def resolve(self, name: Any) -> int | None:
        """The chain a source means by `name`, or None when no registered chain answers to it.

        Accepts the decimal chain id too: some sources label a chain they cannot name by its id.
        """
        if isinstance(name, int) and not isinstance(name, bool):
            return name if self.get(name) is not None else None
        if not isinstance(name, str):
            return None
        text = name.strip()
        if text.isdigit() and len(text) <= 12:
            return self.resolve(int(text))
        return self._name_index().get(text.casefold())

    def _name_index(self) -> dict[str, int]:
        return {name: chain.chain_id for chain in self.chains for name in _names(chain)}


def _names(chain: Chain) -> Iterable[str]:
    return {text.casefold() for text in (chain.key, chain.name, chain.label, *chain.llama_yields)}


@lru_cache(maxsize=1)
def registry() -> ChainRegistry:
    """The registry this checkout ships. Read once per process; it is part of the code."""
    return ChainRegistry.from_document(json.loads(CHAINS_PATH.read_text(encoding="utf-8")))
