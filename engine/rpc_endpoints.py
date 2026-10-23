"""Credential-free RPC endpoint policy shared by engine collectors and the universe builder.

Environment overrides are read at call time so scheduled jobs can inject private or paid RPCs
without putting a URL or key in source control. Public endpoints remain as failover capacity.
Both come from the chain registry (`engine/registry/chains.json`).
"""
from __future__ import annotations

from engine.registry import registry


RPC_ENVIRONMENT = {chain.chain_id: chain.rpc_env for chain in registry().chains}


PUBLIC_RPC_ENDPOINTS: dict[int, tuple[str, ...]] = {
    chain.chain_id: chain.rpc_public for chain in registry().chains if chain.rpc_public
}


def rpc_endpoints(chain_id: int) -> tuple[str, ...]:
    """Return a deterministic, de-duplicated override-first endpoint sequence."""
    chain = registry().get(chain_id)
    return () if chain is None else chain.rpc_endpoints()
