#!/usr/bin/env bash
#
# bootstrap.sh — initialise the lab Vault and configure it for chessbook.
#
#   ./vault/run.sh start && ./vault/bootstrap.sh
#
# Idempotent: safe to re-run. It initialises only if uninitialised, unseals
# only if sealed, and enables engines only if absent.
#
# THE UNSEAL KEYS.
# `vault operator init` prints Shamir key shares exactly once. This script
# writes them to vault/.init-keys.json (gitignored, mode 600) so the unseal
# step can be scripted across container restarts.
#
# Storing unseal keys on the same machine as the server defeats the purpose of
# splitting them — anyone who can read that file can unseal. That is an
# acceptable trade for a laptop lab and NOT acceptable anywhere else. The
# production answer is auto-unseal: seal "awskms" against a KMS key, so the
# key shares become recovery keys held offline by different people. That is a
# ~10 line config change and a $1/month KMS key, and it is the honest thing to
# point at when someone asks how this would really run.

set -euo pipefail

cd "$(dirname "$0")/.."
export VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
KEYFILE=vault/.init-keys.json

v() { docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN="${VAULT_TOKEN:-}" chessbook-vault vault "$@"; }
api() { curl -s "$VAULT_ADDR/v1/$1"; }

# --- init -------------------------------------------------------------------
if [ "$(api sys/seal-status | python3 -c 'import json,sys; print(json.load(sys.stdin)["initialized"])')" = "False" ]; then
    echo "==> initialising (3 key shares, threshold 2)"
    curl -s --request POST --data '{"secret_shares":3,"secret_threshold":2}' \
        "$VAULT_ADDR/v1/sys/init" > "$KEYFILE"
    chmod 600 "$KEYFILE"
    echo "    unseal keys and root token -> $KEYFILE (mode 600, gitignored)"
else
    echo "==> already initialised"
fi

[ -f "$KEYFILE" ] || { echo "error: $KEYFILE missing and Vault is already initialised." >&2
                       echo "       Without the keys this Vault cannot be unsealed. Recreate it with:" >&2
                       echo "         docker rm -f chessbook-vault && docker volume rm chessbook-vault-data" >&2
                       exit 1; }

ROOT_TOKEN=$(python3 -c "import json;print(json.load(open('$KEYFILE'))['root_token'])")

# --- unseal -----------------------------------------------------------------
if [ "$(api sys/seal-status | python3 -c 'import json,sys; print(json.load(sys.stdin)["sealed"])')" = "True" ]; then
    echo "==> unsealing"
    for i in 0 1; do
        k=$(python3 -c "import json;print(json.load(open('$KEYFILE'))['keys'][$i])")
        curl -s --request POST --data "{\"key\":\"$k\"}" "$VAULT_ADDR/v1/sys/unseal" >/dev/null
    done
    echo "    sealed=$(api sys/seal-status | python3 -c 'import json,sys; print(json.load(sys.stdin)["sealed"])')"
else
    echo "==> already unsealed"
fi

export VAULT_TOKEN="$ROOT_TOKEN"

# --- secret engines ---------------------------------------------------------
mounted() { v secrets list -format=json 2>/dev/null | python3 -c "import json,sys; print('$1/' in json.load(sys.stdin))"; }

if [ "$(mounted chessbook)" != "True" ]; then
    echo "==> enabling KV v2 at chessbook/"
    v secrets enable -path=chessbook -version=2 kv >/dev/null
else
    echo "==> KV already mounted at chessbook/"
fi

if [ "$(mounted transit)" != "True" ]; then
    echo "==> enabling transit/"
    v secrets enable transit >/dev/null
else
    echo "==> transit already enabled"
fi

# ed25519: small signatures, fast, and verifiable offline by any standard
# library. exportable=false and allow_plaintext_backup=false mean the private
# key cannot leave Vault by any API call, including as root.
if ! v read transit/keys/chessbook-analysis >/dev/null 2>&1; then
    echo "==> creating transit signing key (ed25519, non-exportable)"
    v write transit/keys/chessbook-analysis \
        type=ed25519 exportable=false allow_plaintext_backup=false >/dev/null
else
    echo "==> transit key already exists"
fi

# --- policies ---------------------------------------------------------------
for p in chessbook-ingest chessbook-signer; do
    echo "==> writing policy $p"
    docker exec -i -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN="$VAULT_TOKEN" \
        chessbook-vault vault policy write "$p" - < "vault/policies/$p.hcl" >/dev/null
done

# --- AWS IAM auth -----------------------------------------------------------
# Vault verifies a client-signed sts:GetCallerIdentity request. The client
# proves who it is using credentials it already has; no Vault-specific secret
# is created, delivered, or rotated. That is the entire point.
if ! v auth list -format=json | python3 -c 'import json,sys; sys.exit(0 if "aws/" in json.load(sys.stdin) else 1)'; then
    echo "==> enabling aws auth"
    v auth enable aws >/dev/null
else
    echo "==> aws auth already enabled"
fi

ARN="${CHESSBOOK_IAM_ARN:-}"
if [ -z "$ARN" ]; then
    ARN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || true)
fi
if [ -n "$ARN" ]; then
    echo "==> binding aws auth roles to $ARN"
    # resolve_aws_unique_ids=false: by default Vault converts the ARN to AWS's
    # internal unique id, which means Vault itself must hold AWS credentials to
    # call iam:GetUser. This lab Vault deliberately holds none, so it compares
    # the ARN string instead.
    #
    # The trade is real: unique-id binding survives a rename and refuses a
    # deleted-then-recreated principal of the same name; string binding does the
    # opposite. For a single-operator lab that is fine. In production, give
    # Vault a read-only IAM identity and let it resolve properly.
    # ONE ROLE PER JOB, not one role per human.
    #
    # The same IAM principal backs both, which looks redundant until you ask
    # what each job is allowed to do: ingest reads one secret and cannot sign;
    # the signer signs and cannot read the secret. A single role carrying both
    # policies would mean an ingest run compromised mid-flight could forge
    # signatures, and that is exactly the blast radius this phase exists to
    # shrink. The caller chooses the role, so it chooses its own privileges —
    # and can only ever choose down.
    for r in ingest signer; do
        v write "auth/aws/role/chessbook-$r" \
            auth_type=iam \
            bound_iam_principal_arn="$ARN" \
            resolve_aws_unique_ids=false \
            policies="chessbook-$r" \
            max_ttl=1h ttl=20m >/dev/null
        echo "    role chessbook-$r -> policy chessbook-$r"
    done
else
    echo "!! could not determine your IAM ARN (is AWS_PROFILE set?); skipping role binding"
fi

echo
echo "bootstrap complete."
echo "  VAULT_ADDR=http://127.0.0.1:8200"
echo "  root token is in $KEYFILE — use it for admin only, never for the pipeline"
