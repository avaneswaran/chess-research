# Policy for signing published analysis.
#
# SIGN AND VERIFY, NEVER EXPORT. The transit key is created non-exportable, so
# the private key cannot leave Vault even for an operator — but the policy says
# so too, because defence that relies on one setting being right is not defence.
#
# This is the whole argument: the pipeline can prove it produced an artifact,
# and cannot hand anyone else the ability to forge that proof.

path "transit/sign/chessbook-analysis" {
  capabilities = ["update"]
}

path "transit/verify/chessbook-analysis" {
  capabilities = ["update"]
}

# Read the public key so a reader can verify offline, without Vault access.
path "transit/keys/chessbook-analysis" {
  capabilities = ["read"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
