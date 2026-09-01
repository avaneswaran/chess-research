#!/usr/bin/env bash
#
# put-lichess-token.sh — store the Lichess API token in Vault.
#
#   ./vault/put-lichess-token.sh
#
# Prompts with echo OFF and never takes the token as an argument, because an
# argument lands in shell history, in `ps` output while the command runs, and
# in any terminal transcript. Piping from a password manager works too:
#
#   pass show lichess/token | ./vault/put-lichess-token.sh
#
# Uses the root token from bootstrap: writing secrets is an admin action, and
# the chessbook-ingest policy is deliberately read-only. If the pipeline could
# write this path it could also overwrite it.

set -euo pipefail
cd "$(dirname "$0")/.."

KEYFILE=vault/.init-keys.json
[ -f "$KEYFILE" ] || { echo "error: $KEYFILE not found — run ./vault/bootstrap.sh first" >&2; exit 1; }

if [ -t 0 ]; then
    printf 'Lichess API token (input hidden): ' >&2
    read -rs TOKEN
    printf '\n' >&2
else
    read -r TOKEN
fi

[ -n "${TOKEN:-}" ] || { echo "error: empty token, nothing written" >&2; exit 1; }

ROOT=$(python3 -c "import json;print(json.load(open('$KEYFILE'))['root_token'])")

docker exec -i \
    -e VAULT_ADDR=http://127.0.0.1:8200 \
    -e VAULT_TOKEN="$ROOT" \
    chessbook-vault \
    vault kv put chessbook/lichess token="$TOKEN" >/dev/null

echo "stored at chessbook/lichess (field: token)" >&2
echo "verify with: ./vault/read-lichess-token.sh" >&2
