# The chat's language model: the upstream assistant service plus the QVAC runtime, pinned.
# Two targets share one set of files: `cpu` (the default) and `gpu` (NVIDIA cards through CUDA 13,
# `make assistant GPU=1`).
FROM node:22.22.1-bookworm-slim AS files

ARG TARGETARCH
WORKDIR /app
COPY images/assistant/package.json images/assistant/package-lock.json ./
# The SDK installs every QVAC add-on (speech, image generation, translation, OCR…) for every
# platform: 3.2 GB. The chat registers one plugin, text completion, through its own worker entry
# (chain/assistant-worker.mjs), so only that plugin's code and the build architecture's Linux
# binaries are kept (x64, or arm64 on Apple silicon). The language-detection package stays: the
# SDK's shared code imports it at start.
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev --no-audit --no-fund \
 && cd node_modules \
 && rm -rf @qvac/diffusion-cpp bare-ffmpeg @qvac/bci-whispercpp @qvac/tts-ggml @qvac/tts-ggml-linux-* \
      @qvac/asr-ggml @qvac/asr-ggml-linux-* @qvac/audiogen-ggml @qvac/audiogen-ggml-linux-* \
      @qvac/translation-nmtcpp @qvac/ocr-ggml @qvac/vla-ggml @qvac/classification-ggml \
      @qvac/decoder-audio @qvac/embed-llamacpp \
 && case "$TARGETARCH" in arm64) other=linux-x64 ;; *) other=linux-arm64 ;; esac \
 && find . -type d -path '*/prebuilds/*' \( -name 'android-*' -o -name 'darwin-*' -o -name 'ios-*' \
      -o -name 'win32-*' -o -name "$other" -o -name 'wasi-*' \) -prune -exec rm -rf {} + \
 && rm -f @qvac/fabric-linux-*/prebuilds/linux-*/qvac__fabric-linux-*/libqvac-ggml-hip.so \
          @qvac/fabric-linux-*/prebuilds/linux-*/qvac__fabric-linux-*/libqvac-ggml-vulkan.so

COPY assistant ./assistant
COPY chain/assistant.mjs chain/assistant-warm.mjs chain/assistant-worker.mjs ./chain/

# The CPU image has no use for the 140 MB CUDA backend.
FROM files AS cpu-files
RUN rm -f node_modules/@qvac/fabric-linux-*/prebuilds/linux-*/qvac__fabric-linux-*/libqvac-ggml-cuda.so

FROM nvidia/cuda:13.0.2-runtime-ubuntu24.04 AS gpu

# The same pinned Node the CPU image runs; the CUDA image brings the runtime libraries
# (libcudart, libcublas) the runtime's CUDA backend links against, the host driver the rest.
COPY --from=node:22.22.1-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN mkdir -p /home/node && chown 1000:1000 /home/node
WORKDIR /app
COPY --from=files --chown=1000:1000 /app /app
USER 1000:1000
ENV HOME=/home/node \
    NODE_ENV=production \
    FENORIUM_ASSISTANT_PORT=8787 \
    QVAC_WORKER_PATH=/app/chain/assistant-worker.mjs
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8787/health').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
ENTRYPOINT ["node", "/app/chain/assistant.mjs"]

FROM node:22.22.1-bookworm-slim AS cpu

WORKDIR /app
# --chown in the copy: a later `chown -R` would duplicate every file into a second layer.
COPY --from=cpu-files --chown=node:node /app /app
USER node
ENV HOME=/home/node \
    NODE_ENV=production \
    FENORIUM_ASSISTANT_PORT=8787 \
    QVAC_WORKER_PATH=/app/chain/assistant-worker.mjs
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8787/health').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
ENTRYPOINT ["node", "/app/chain/assistant.mjs"]
