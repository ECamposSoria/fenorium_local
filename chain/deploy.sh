#!/bin/sh
# Deploys the Fenorium demo contracts on the local chain, from source, in the order they were
# deployed on Base Sepolia. Runs inside the Foundry image (sh, forge, cast; no jq or python).
#
# Every address is derived from the deployer's nonce, so with Anvil's published development key
# and a fresh chain the addresses are the same on every machine. The deployment is recorded in
# $OUT_DIR/local-deployment.env; a rerun on a chain that still holds the recorded contracts skips.
set -eu

RPC_URL="${RPC_URL:-http://anvil:8545}"
OUT_DIR="${OUT_DIR:-/out}"

# The container starts as root only to hand the fresh volumes to the image's unprivileged user,
# then runs everything else as that user.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /work/contracts/out /work/contracts/cache /work/contracts/broadcast "$OUT_DIR"
  chown -R 1000:1000 /work/contracts/out /work/contracts/cache /work/contracts/broadcast "$OUT_DIR"
  exec runuser -u foundry -- sh "$0" "$@"
fi
CHAIN_DIR="${CHAIN_DIR:-/work/chain}"
CONTRACTS_DIR="${CONTRACTS_DIR:-/work/contracts}"
SCHEMA_TEXT='bool verified,string jurisdiction,string tier'
EXPECTED_SCHEMA_UID='0xc6c30d8537afcc25aca394ef6b70c65d8378ae3a9416a9944c2f75e6feb71823'

: "${PRIVATE_KEY:?PRIVATE_KEY (the local deployer, Anvil account #0) is required}"
: "${LOCAL_RECIPIENT_KEYS:?LOCAL_RECIPIENT_KEYS (comma-separated Anvil keys to verify) is required}"

log() { printf '==> %s\n' "$*"; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

# The broadcast record lists "contractName" then "contractAddress" on consecutive lines.
address_of() { # <script> <contractName> [occurrence]
  file="$CONTRACTS_DIR/broadcast/$1/84532/run-latest.json"
  [ -f "$file" ] || die "no broadcast record for $1"
  grep -A1 "\"contractName\": \"$2\"" "$file" | grep -o '0x[0-9a-fA-F]\{40\}' | sed -n "${3:-1}p"
}
has_code() { [ "$(cast code "$1" --rpc-url "$RPC_URL")" != "0x" ]; }

log "waiting for the chain at $RPC_URL"
i=0
until cast chain-id --rpc-url "$RPC_URL" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || die "the chain did not answer in 60 s"; sleep 1
done
[ "$(cast chain-id --rpc-url "$RPC_URL")" = "84532" ] || die "the chain must present chain id 84532"

DEPLOYER="$(cast wallet address --private-key "$PRIVATE_KEY")"
if [ -f "$OUT_DIR/local-deployment.env" ]; then
  # shellcheck disable=SC1091
  . "$OUT_DIR/local-deployment.env"
  if [ -n "${CATALOG_FACTORY:-}" ] && has_code "$CATALOG_FACTORY"; then
    log "already deployed on this chain (factory $CATALOG_FACTORY); nothing to do"
    exit 0
  fi
  log "recorded deployment is not on this chain; deploying again"
fi

cd "$CONTRACTS_DIR"
export FOUNDRY_PROFILE=default
export PRIVATE_KEY

# 1. Base Sepolia's EAS and schema registry are OP-stack predeploys. Their implementation code,
#    captured from Base Sepolia (chain/predeploys.json records where and which versions), is placed
#    at the same addresses here so the gated instruments verify credentials the same way.
for name in SchemaRegistry EAS; do
  addr="$(cat "$CHAIN_DIR/predeploys/$name.address")"
  if ! has_code "$addr"; then
    cast rpc anvil_setCode "$addr" "$(cat "$CHAIN_DIR/predeploys/$name.code")" --rpc-url "$RPC_URL" >/dev/null
    log "placed $name at $addr"
  fi
done

# 2. The demo credential schema; its UID is the hash of its definition, so it equals Base Sepolia's.
SCHEMA_UID="$(cast call 0x4200000000000000000000000000000000000020 'getSchema(bytes32)((bytes32,address,bool,string))' \
  "$EXPECTED_SCHEMA_UID" --rpc-url "$RPC_URL" | grep -o '0x[0-9a-fA-F]\{64\}' | head -1 || true)"
if [ "$SCHEMA_UID" != "$EXPECTED_SCHEMA_UID" ]; then
  cast send 0x4200000000000000000000000000000000000020 'register(string,address,bool)' "$SCHEMA_TEXT" \
    0x0000000000000000000000000000000000000000 true --private-key "$PRIVATE_KEY" --rpc-url "$RPC_URL" >/dev/null
  log "registered the credential schema"
fi
export SCHEMA_UID="$EXPECTED_SCHEMA_UID"

# 3. The five mocks, their sales and desks, then the catalog: the Base Sepolia scripts, unchanged,
#    pointed at this chain through their environment overrides.
log "mGT and mPT"
forge script script/DeployDemoInstruments.s.sol:DeployDemoInstruments --rpc-url "$RPC_URL" --broadcast -q
GATED="$(address_of DeployDemoInstruments.s.sol GatedTreasuryMock)"
PERMISSIONLESS="$(address_of DeployDemoInstruments.s.sol PermissionlessTreasuryMock)"

log "mFT and mVY"
DEMO_HOLDER="$DEPLOYER" DEMO_FIXED_TERM_UNITS="${DEMO_FIXED_TERM_UNITS:-250000000000}" \
DEMO_VARIABLE_YIELD_UNITS="${DEMO_VARIABLE_YIELD_UNITS:-250000000000}" \
forge script script/DeployDemoExposureMocks.s.sol:DeployDemoExposureMocks --rpc-url "$RPC_URL" --broadcast -q
FIXED_TERM="$(address_of DeployDemoExposureMocks.s.sol DemoExposureMock 1)"
VARIABLE_YIELD="$(address_of DeployDemoExposureMocks.s.sol DemoExposureMock 2)"

log "mUSDC and the two sales"
DEMO_GATED_INSTRUMENT="$GATED" DEMO_PERMISSIONLESS_INSTRUMENT="$PERMISSIONLESS" \
forge script script/DeployDemoSales.s.sol:DeployDemoSales --rpc-url "$RPC_URL" --broadcast -q
PAYMENT_TOKEN="$(address_of DeployDemoSales.s.sol MockUSDC)"
GATED_SALE="$(address_of DeployDemoSales.s.sol DemoInstrumentSale 1)"
PERMISSIONLESS_SALE="$(address_of DeployDemoSales.s.sol DemoInstrumentSale 2)"

log "the two desks (the sales close, as on Base Sepolia)"
DEMO_PAYMENT_TOKEN="$PAYMENT_TOKEN" DEMO_GATED_INSTRUMENT="$GATED" DEMO_PERMISSIONLESS_INSTRUMENT="$PERMISSIONLESS" \
DEMO_GATED_SALE="$GATED_SALE" DEMO_PERMISSIONLESS_SALE="$PERMISSIONLESS_SALE" DEMO_DESK_CLOSE_SALES=true \
forge script script/DeployDemoDesks.s.sol:DeployDemoDesks --rpc-url "$RPC_URL" --broadcast -q
GATED_DESK="$(address_of DeployDemoDesks.s.sol DemoInstrumentDesk 1)"
PERMISSIONLESS_DESK="$(address_of DeployDemoDesks.s.sol DemoInstrumentDesk 2)"

log "the 132 catalog instruments and their desks"
DEMO_PAYMENT_TOKEN="$PAYMENT_TOKEN" DEMO_GATE_REGISTRY="$GATED" \
forge script script/DeployDemoCatalog.s.sol:DeployDemoCatalog --rpc-url "$RPC_URL" --broadcast -q
CATALOG_FACTORY="$(address_of DeployDemoCatalog.s.sol DemoCatalogFactory)"

log "DecisionPassportV2"
PAR_GOVERNANCE="$DEPLOYER" PAR_ISSUER="$DEPLOYER" \
forge script script/DeployDecisionPassportV2.s.sol:DeployDecisionPassportV2 --rpc-url "$RPC_URL" --broadcast -q
DECISION_PASSPORT="$(address_of DeployDecisionPassportV2.s.sol DecisionPassportV2)"

# 4. The demo credential for the development wallets: the trusted attester (the deployer) attests,
#    and each wallet registers the attestation with mGT, as the owner does on Base Sepolia. The
#    attestation's id is read back from the chain's own event, because it includes the block time.
log "credentials for the development wallets"
EAS=0x4200000000000000000000000000000000000021
ATTESTED_TOPIC=0x8bf46bf4cfd674fa735a3d63ec1c9ad4153f033c290341f3a588b75685141b35
CREDENTIAL_DATA="$(cast abi-encode 'f(bool,string,string)' true US retail)"
for recipient_key in $(printf '%s' "$LOCAL_RECIPIENT_KEYS" | tr ',' ' '); do
  recipient="$(cast wallet address --private-key "$recipient_key")"
  if [ "$(cast call "$GATED" 'isVerified(address)(bool)' "$recipient" --rpc-url "$RPC_URL")" = "true" ]; then
    log "already verified $recipient"; continue
  fi
  receipt="$(cast send "$EAS" 'attest((bytes32,(address,uint64,bool,bytes32,bytes,uint256)))' \
    "($SCHEMA_UID,($recipient,0,true,0x0000000000000000000000000000000000000000000000000000000000000000,$CREDENTIAL_DATA,0))" \
    --private-key "$PRIVATE_KEY" --rpc-url "$RPC_URL" --json)"
  uid="$(printf '%s' "$receipt" | tr -d ' \n' | grep -o "\"topics\":\[\"$ATTESTED_TOPIC\"[^]]*\],\"data\":\"0x[0-9a-f]\{64\}\"" | grep -o '0x[0-9a-f]\{64\}"$' | tr -d '"')"
  [ -n "$uid" ] || die "no Attested event in the attestation receipt for $recipient"
  cast send "$GATED" 'registerCredential(bytes32)' "$uid" --private-key "$recipient_key" --rpc-url "$RPC_URL" >/dev/null
  [ "$(cast call "$GATED" 'isVerified(address)(bool)' "$recipient" --rpc-url "$RPC_URL")" = "true" ] || die "$recipient is not verified after registering $uid"
  log "verified $recipient"
done

for addr in "$GATED" "$PERMISSIONLESS" "$FIXED_TERM" "$VARIABLE_YIELD" "$PAYMENT_TOKEN" "$GATED_DESK" "$PERMISSIONLESS_DESK" "$CATALOG_FACTORY" "$DECISION_PASSPORT"; do
  has_code "$addr" || die "no code at $addr after deployment"
done
mkdir -p "$OUT_DIR"
cat > "$OUT_DIR/local-deployment.env" <<EOF
DEPLOYER=$DEPLOYER
SCHEMA_UID=$SCHEMA_UID
GATED=$GATED
PERMISSIONLESS=$PERMISSIONLESS
FIXED_TERM=$FIXED_TERM
VARIABLE_YIELD=$VARIABLE_YIELD
PAYMENT_TOKEN=$PAYMENT_TOKEN
GATED_SALE=$GATED_SALE
PERMISSIONLESS_SALE=$PERMISSIONLESS_SALE
GATED_DESK=$GATED_DESK
PERMISSIONLESS_DESK=$PERMISSIONLESS_DESK
CATALOG_FACTORY=$CATALOG_FACTORY
DECISION_PASSPORT=$DECISION_PASSPORT
EOF
log "deployed: mGT $GATED · mPT $PERMISSIONLESS · mUSDC $PAYMENT_TOKEN · catalog factory $CATALOG_FACTORY"
