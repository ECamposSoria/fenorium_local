# One image, three targets. `base` is the exported Fenorium tree with the chain registry pointed at
# the local node; `builder` regenerates every data file against that node; `platform` is the
# testnet terminal image (mirrors upstream deploy/testnet/Dockerfile) serving the generated files.
FROM python:3.12.11-slim AS base

ARG UPSTREAM_COMMIT=unknown
LABEL org.opencontainers.image.title="Fenorium local" \
      org.opencontainers.image.revision="${UPSTREAM_COMMIT}"

RUN groupadd --gid 10001 fenorium \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin fenorium

WORKDIR /app

COPY --chown=fenorium:fenorium engine ./engine
COPY --chown=fenorium:fenorium research/par_sources ./research/par_sources
COPY --chown=fenorium:fenorium schemas ./schemas
COPY --chown=fenorium:fenorium scripts ./scripts
COPY --chown=fenorium:fenorium web ./web
COPY --chown=fenorium:fenorium deploy/platform ./deploy/platform
COPY --chown=fenorium:fenorium deploy/score_ledger.py ./deploy/score_ledger.py
COPY --chown=fenorium:fenorium contracts/deployments ./contracts/deployments
COPY --chown=fenorium:fenorium chain/build.py ./chain/build.py

# The registry's Base Sepolia entry: the only RPC is the local node, there is no public explorer
# and no Blockscout index for it, so nothing here ever reads the public testnet by mistake.
RUN python - <<'PY'
import json
from pathlib import Path
path = Path("/app/engine/registry/chains.json")
document = json.loads(path.read_text())
entries = document if isinstance(document, list) else document.get("chains", document)
local = [entry for entry in entries if isinstance(entry, dict) and entry.get("chain_id") == 84532]
assert len(local) == 1, "expected one Base Sepolia entry"
# The registry accepts only https public endpoints; the local node arrives through the env
# override alone, which every service sets to http://anvil:8545.
local[0]["rpc"] = {"env": "BASE_SEPOLIA_RPC_URL", "public": []}
local[0]["explorer"] = None
local[0]["blockscout_index"] = False
local[0]["blockscout_api"] = None
path.write_text(json.dumps(document, indent=1) + "\n")
PY

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0


FROM base AS builder
COPY --from=ghcr.io/foundry-rs/foundry:v1.7.1 /usr/local/bin/cast /usr/local/bin/cast
ENTRYPOINT ["python", "-m", "chain.build"]


FROM base AS platform
RUN mkdir -p /app/evidence /app/curve && chown fenorium:fenorium /app/evidence /app/curve

ENV FENORIUM_PLATFORM_HOST=0.0.0.0 \
    FENORIUM_PLATFORM_PORT=8517 \
    FENORIUM_EVIDENCE_DIR=/app/evidence \
    FENORIUM_UNIVERSE_PATH=/app/curve/universe.json \
    FENORIUM_ROUTER= \
    FENORIUM_EXECUTION_CLEARED=

USER 10001:10001

EXPOSE 8517

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-m", "deploy.platform.healthcheck"]

ENTRYPOINT ["python", "-m", "deploy.platform.service"]
