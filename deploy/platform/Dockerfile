FROM python:3.12.11-slim

ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --gid "${APP_GID}" fenorium \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin fenorium

WORKDIR /app

COPY --chown=fenorium:fenorium engine ./engine
# Only the source clients, not the 2 GB of research data beside them. `deploy.platform.router`
# needs the LI.FI quote adapter, and one implementation shared with the evidence collector is
# what keeps the request this server makes identical to the one that was measured.
COPY --chown=fenorium:fenorium research/par_sources ./research/par_sources
COPY --chown=fenorium:fenorium schemas ./schemas
COPY --chown=fenorium:fenorium web ./web
COPY --chown=fenorium:fenorium deploy/platform ./deploy/platform

RUN mkdir -p /app/evidence \
    && chown fenorium:fenorium /app/evidence

ENV FENORIUM_PLATFORM_HOST=0.0.0.0 \
    FENORIUM_PLATFORM_PORT=8516 \
    FENORIUM_EVIDENCE_DIR=/app/evidence \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

USER 10001:10001

EXPOSE 8516

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-m", "deploy.platform.healthcheck"]

ENTRYPOINT ["python", "-m", "deploy.platform.service"]
