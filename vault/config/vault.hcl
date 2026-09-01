# chessbook lab Vault — single node, file storage, local only.
#
# This is a rehearsal instance, not a production one, and the differences are
# worth stating plainly rather than discovering later:
#
#   - ONE node, file storage. No HA, no raft peers. If the container dies mid
#     write the store can need repair. Production uses integrated raft with
#     three or five nodes.
#   - TLS DISABLED. The listener is bound to loopback and published only to
#     127.0.0.1 on the host, so nothing off this machine can reach it. That is
#     containment, not encryption — over any real network this is indefensible.
#   - disable_mlock. Normally Vault locks memory so secrets cannot be paged to
#     disk, which needs IPC_LOCK. Turning it off avoids granting the container
#     a capability it would otherwise hold for the life of the lab; the cost is
#     that decrypted secrets could in principle reach swap.
#
# What is NOT different: the auth methods, policies, KV mechanics, and transit
# signing below behave exactly as they would in production. Those are the parts
# being rehearsed.

ui = true
disable_mlock = true

storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# Advertised address for the API. Single node, so it points at itself.
api_addr = "http://127.0.0.1:8200"
