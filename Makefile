# Fenorium local — the whole stack on your own machine.
#   make up      build the images, start the chain, deploy every contract, generate the data,
#                serve the terminal at http://127.0.0.1:8517
#   make down    stop everything, keep the chain
#   make reset   stop everything and wipe the chain, so the next `make up` deploys again
#   make status  what is running and what was deployed
#   make logs    follow the platform's log
#   make test    run the contract test suite in the Foundry image
#   make assistant  start the optional chat assistant (GPU=1 for an NVIDIA card)
#   make model   fetch the assistant's language model once (about 1 GB for the default)
#   make doctor  check the prerequisites
SHELL := /bin/sh
COMPOSE := docker compose -p fenorium-local --env-file .env
# `make assistant GPU=1` adds the NVIDIA override; without it the assistant runs on the CPU.
ASSISTANT_COMPOSE := $(COMPOSE) -f docker-compose.yml $(if $(GPU),-f docker-compose.gpu.yml,)
export UPSTREAM_COMMIT := $(shell cat UPSTREAM 2>/dev/null || echo unknown)

.PHONY: up down reset status logs test doctor assistant model assistant-down

# One image at a time: building both in one bake has crashed Docker 29's BuildKit on a
# concurrent-map race (the second build shares the first's base stage, so it costs seconds).
up: doctor
	$(COMPOSE) build builder
	$(COMPOSE) build platform
	$(COMPOSE) up -d anvil
	$(COMPOSE) up deployer
	$(COMPOSE) up builder
	$(COMPOSE) up -d --no-deps platform
	@echo
	@echo "Fenorium local is up: http://127.0.0.1:$$(grep -E '^LOCAL_PLATFORM_PORT=' .env | cut -d= -f2 || echo 8517)"
	@echo "Chain RPC for your wallet: http://127.0.0.1:$$(grep -E '^LOCAL_CHAIN_PORT=' .env | cut -d= -f2 || echo 8545) (chain id 84532)"
	@echo "See README.md for the wallet setup and the development accounts."

down:
	$(COMPOSE) --profile assistant down

reset:
	$(COMPOSE) --profile assistant down -v

status:
	$(COMPOSE) ps
	@$(COMPOSE) run --rm --no-deps --entrypoint sh deployer -c 'cat /out/local-deployment.env 2>/dev/null || echo "not deployed yet"'

logs:
	$(COMPOSE) logs -f platform

test:
	docker run --rm -u 1000:1000 -v "$(CURDIR)/contracts:/work" -w /work --entrypoint forge ghcr.io/foundry-rs/foundry:v1.7.1 test

doctor:
	@command -v docker >/dev/null || { echo "docker is required"; exit 1; }
	@docker compose version >/dev/null 2>&1 || { echo "docker compose v2 is required"; exit 1; }
	@test -f .env || { echo ".env is missing (it ships with the repo)"; exit 1; }
	@test -d contracts/lib/forge-std || { echo "contracts/lib is missing; clone with --recurse-submodules or run: git submodule update --init"; exit 1; }
	@echo "prerequisites ok"

assistant:
	$(ASSISTANT_COMPOSE) --profile assistant up -d --build assistant
	@echo "Chat assistant started$(if $(GPU), on the GPU,). Open Explain in the terminal; the first answer waits for the model to load."

model:
	$(ASSISTANT_COMPOSE) --profile assistant build assistant
	$(ASSISTANT_COMPOSE) --profile assistant run --rm --entrypoint node assistant /app/chain/assistant-warm.mjs

assistant-down:
	$(COMPOSE) --profile assistant stop assistant
