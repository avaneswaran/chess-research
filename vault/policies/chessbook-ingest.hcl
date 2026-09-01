# Policy for the ingest/collect jobs.
#
# Read one secret. Nothing else. Not list, not write, not the metadata that
# would let it enumerate what other secrets exist.

path "chessbook/data/lichess" {
  capabilities = ["read"]
}

# Renew and check its own token. Without this a long ingest run cannot extend
# its lease and dies partway through with a 403 that looks like a Lichess
# problem rather than a Vault one.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
