#!/usr/bin/env bash
#
# root-token.sh — print the root token, for pasting into the UI login.
#
#   ./vault/root-token.sh
#
# Exists because the inline python one-liner is long enough that terminals wrap
# it mid-string-literal and it fails to parse.
#
# This is the ROOT token: it bypasses every policy, including the least
# privilege split between chessbook-ingest and chessbook-signer. Use it for the
# UI and for admin tasks. Never for anything the pipeline does — that is what
# the AWS IAM roles are for.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f vault/.init-keys.json ] || { echo "vault/.init-keys.json not found" >&2; exit 1; }
python3 -c 'import json;print(json.load(open("vault/.init-keys.json"))["root_token"])'
