#!/usr/bin/env bash
#
# read-lichess-token.sh — confirm a token is stored, WITHOUT printing it.
#
# Prints metadata and a fingerprint, never the secret. A helper that echoes the
# credential to a terminal is a helper that puts it in scrollback.
#
# The JSON goes through an env var rather than a pipe: `python3 - <<'PY'` takes
# its stdin from the heredoc, so a piped payload would never reach it.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(python3 -c "import json;print(json.load(open('vault/.init-keys.json'))['root_token'])")
VAULT_JSON=$(docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN="$ROOT" \
    chessbook-vault vault kv get -format=json chessbook/lichess)
export VAULT_JSON
python3 - <<'PY'
import hashlib, json, os
d = json.loads(os.environ["VAULT_JSON"])["data"]
tok = d["data"].get("token", "")
m = d["metadata"]
print("  path       : chessbook/lichess")
print("  version    : {}   created: {}".format(m["version"], m["created_time"]))
print("  length     : {} chars".format(len(tok)))
print("  sha256[:12]: {}  (fingerprint, not the token)".format(
    hashlib.sha256(tok.encode()).hexdigest()[:12]))
PY
