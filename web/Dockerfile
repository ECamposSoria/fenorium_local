# No immutable Foundry image tag was available in the offline project cache. The build therefore
# takes an operator-verified cast binary as an explicit input instead of silently following a tag.
FROM scratch AS cast-source
COPY web/bin/cast /cast


FROM python:3.12.11-slim

# The issuer holds only a testnet key, but it still runs unprivileged because public request input
# should never grant filesystem authority inside a host that also carries a live trading stack.
RUN useradd --create-home --uid 10001 par
WORKDIR /app

COPY --from=cast-source --chown=par:par /cast /usr/local/bin/cast
COPY --chown=par:par engine/__init__.py engine/keccak.py ./engine/
COPY --chown=par:par web/issuer.py web/config.json web/universe.json web/index.html web/app.js web/ui.js web/charts.js web/icon-catalog.js web/portfolio.js web/prices.js web/routes.js web/styles.css web/terminal.css ./web/
COPY --chown=par:par web/data ./web/data
COPY --chown=par:par web/assets ./web/assets

RUN chmod 0755 /usr/local/bin/cast

ENV PAR_WEB_HOST=0.0.0.0 \
    PORT=8515 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER par
EXPOSE 8515

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8515/config.json', timeout=3).read()" || exit 1

CMD ["python", "web/issuer.py"]
