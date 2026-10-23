# Fenorium local — the whole stack on your own machine.
#   make up      build the images, start the chain, deploy every contract, generate the data,
#                serve the terminal at http://127.0.0.1:8517
#   make down    stop everything, keep the chain
#   make reset   stop everything and wipe the chain, so the next `make up` deploys again
#   make status  what is running and what was deployed
#   make logs    follow the platform's log
#   make test    run the contract test suite in the Foundry image
#   make assistant  start the optional Explain assistant (http://127.0.0.1:8787)
#   make model   fetch the assistant's language model once (about 1 GB)
#   make doctor  check the prerequisites
SHELL := /bin/sh
COMPOSE := docker compose -p fenorium-local --env-file .env
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
	$(COMPOSE) up -d platform
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
	$(COMPOSE) --profile assistant up -d --build assistant
	@echo "Explain assistant: http://127.0.0.1:8787 · pairing token: $$(grep -E '^LOCAL_ASSISTANT_PAIRING_TOKEN=' .env | cut -d= -f2)"

model:
	$(COMPOSE) --profile assistant build assistant
	$(COMPOSE) --profile assistant run --rm --entrypoint node assistant /app/chain/assistant-warm.mjs

assistant-down:
	$(COMPOSE) --profile assistant stop assistant
