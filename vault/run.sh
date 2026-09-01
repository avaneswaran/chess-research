#!/usr/bin/env bash
#
# run.sh — start, stop, or check the lab Vault.
#
#   ./vault/run.sh start | stop | status | logs | shell
#
# Data lives in a named docker volume (chessbook-vault-data), not a bind
# mount: the official image runs as uid 100 and a bind-mounted host directory
# needs matching ownership, which is a portability problem nobody needs. The
# volume survives `stop`, container removal, and image upgrades. It does not
# survive `docker volume rm`, which is the one command that loses your secrets.
#
# The port is published to 127.0.0.1 only. Binding 0.0.0.0 with tls_disable
# would put an unauthenticated-by-network Vault on your LAN.
#
# The command is bare `server`, with no -config flag. The image entrypoint
# already appends -config=/vault/config; passing the file path as well makes
# Vault parse the same listener stanza twice and die with
# "listen tcp4 0.0.0.0:8200: bind: address already in use" — inside a fresh
# container, which is a confusing way to learn this.
#
# The "Could not chown /vault/config" warning on startup is expected and
# harmless: the config is mounted read-only on purpose, and the entrypoint
# tries to take ownership of it.

set -euo pipefail

NAME=chessbook-vault
VOLUME=chessbook-vault-data
IMAGE=hashicorp/vault:1.21.4
CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)/config"

cmd="${1:-status}"

case "$cmd" in
  start)
    if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
      docker start "$NAME" >/dev/null
      echo "started existing container $NAME"
    else
      docker volume create "$VOLUME" >/dev/null
      docker run -d \
        --name "$NAME" \
        -p 127.0.0.1:8200:8200 \
        -v "$VOLUME:/vault/file" \
        -v "$CONFIG_DIR:/vault/config:ro" \
        --cap-add IPC_LOCK \
        "$IMAGE" server >/dev/null
      echo "created and started $NAME"
    fi
    echo "  VAULT_ADDR=http://127.0.0.1:8200"
    ;;
  stop)
    docker stop "$NAME" >/dev/null && echo "stopped $NAME (data preserved in volume $VOLUME)"
    ;;
  status)
    docker ps --filter "name=$NAME" --format '  {{.Names}}  {{.Status}}  {{.Ports}}' || true
    # .format(), not an f-string with escaped quotes: `\"` inside a
    # single-quoted `python3 -c` is a shell escape Python never sees, and the
    # resulting SyntaxError was swallowed by 2>/dev/null — so a perfectly
    # healthy Vault reported "API not responding".
    health=$(curl -s http://127.0.0.1:8200/v1/sys/health 2>/dev/null || true)
    if [ -n "$health" ]; then
      HEALTH="$health" python3 - <<'PYEOF'
import json, os
d = json.loads(os.environ["HEALTH"])
print("  initialized={} sealed={} version={}".format(
    d["initialized"], d["sealed"], d["version"]))
PYEOF
    else
      echo "  (API not responding — not started, or still booting)"
    fi
    ;;
  logs)  docker logs --tail 40 "$NAME" ;;
  shell) docker exec -it "$NAME" sh ;;
  *) echo "usage: $0 start|stop|status|logs|shell" >&2; exit 1 ;;
esac
