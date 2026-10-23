# The Explain assistant: the upstream loopback service plus the QVAC runtime, pinned.
FROM node:22.22.1-bookworm-slim

WORKDIR /app
COPY images/assistant/package.json images/assistant/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

COPY assistant ./assistant
COPY chain/assistant.mjs chain/assistant-warm.mjs ./chain/

# The model cache lives under the user's home, kept on a volume by Compose.
RUN mkdir -p /home/node && chown -R node:node /home/node /app
USER node
ENV HOME=/home/node \
    NODE_ENV=production \
    FENORIUM_ASSISTANT_PORT=8787

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8787/health').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
ENTRYPOINT ["node", "/app/chain/assistant.mjs"]
