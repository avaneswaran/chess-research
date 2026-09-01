# chessbook — Vault (phase 2)

Two things live in Vault: the Lichess API token, and the key that signs
published analysis.

**Status: verified end to end.** The full sign/verify pipeline runs on AWS IAM
auth with no root token involved, and the policies are tested by denial
(`vault/test-policies.py`, 9/9). See "What is still unproven" at the bottom for
the honest remainder.

## What this is actually for

The obvious pitch — "Vault gives the workers their credentials" — does not
apply here, and claiming it would not survive scrutiny. The Batch workers get
short-lived, workload-scoped credentials from their IAM job role via the task
metadata endpoint. No static secret exists in that image. Vault would not
improve it.

What Vault does solve:

1. **`LICHESS_TOKEN` was the one real static secret** in the project, sitting
   in an environment variable on a laptop, read by `ingest.py` and
   `collect.py`. It now lives in KV v2 and is fetched at runtime.
2. **Analysis provenance was self-reported.** Every analysis file records the
   engine name, the engine binary's sha256, and the search parameters — all of
   which are trivially forged by editing the file. Transit signatures make a
   published variation checkable against a key whose private half cannot leave
   Vault.

## Layout

```
vault/
  config/vault.hcl          single node, file storage, loopback, TLS off
  run.sh                    start | stop | status | logs | shell
  bootstrap.sh              init, unseal, engines, policies, aws auth role
  policies/*.hcl            chessbook-ingest, chessbook-signer
  .init-keys.json           unseal keys + root token (gitignored, mode 600)

chessvault.py               the one door: auth, token read, sign, verify
sign_analysis.py            sign / verify the corpus
```

## Run it

```bash
./vault/run.sh start
./vault/bootstrap.sh              # idempotent
./vault/put-lichess-token.sh      # prompts, echo off
./vault/read-lichess-token.sh     # fingerprint only, never the secret
```

`bootstrap.sh` is safe to re-run and is also how you unseal after a restart.

Then, as the pipeline identity rather than root:

```bash
aws login                          # the credential that authenticates to Vault
python chessvault.py               # smoke test: who am I, what can I do
python ingest.py --out ./corpus --platforms lichess
```

## Signing

```bash
python sign_analysis.py sign                # needs Vault
python sign_analysis.py verify              # needs NOTHING but the files
python sign_analysis.py verify --via-vault  # cross-check through transit
```

Measured: 3183 analysis files signed in 4.6s (transit batches of 200), and
verified with the Vault container stopped, no token, no network.

Detection, tested: changing a single `centipawn_loss` value in one ply of one
file is caught as `CHANGED`; adding an unsigned analysis file is caught as
`UNSIGNED`; a signature whose file has been deleted is caught as `ORPHAN`.

### Verification is offline, and that is the point

`verify` reads `corpus/signatures.json` and the analysis files. Nothing else.
A signature only its issuer can check is close to useless to a reader — the
ed25519 public key is in the manifest, so any third party can verify without
access to your Vault, your network, or your credentials.

`signatures.json` is self-describing on purpose: it carries the public key, the
algorithm, and the canonicalisation rule.

**Self-describing is not self-authenticating.** Because the manifest carries
the key it was signed with, it always verifies against itself — swap in a
different key and re-sign, and verification still passes. This was observed
directly: after the signing key was replaced, the old manifest still reported
3183/3183 valid, because it was checked against the old key it carried. Offline
verification proves the bytes have not changed since *some* key signed them. It
does not prove *which* key should be trusted. That requires knowing the
expected public key out of band — pinning it in the site build, publishing it
separately, or cross-signing. Until then this is tamper-evidence, not
attestation. The signed message is the sha256 of
`json.dumps(doc, sort_keys=True, separators=(',',':'))` as a 64-char hex
string, ASCII-encoded — confirmed empirically against Vault rather than assumed
from the docs. **Change that canonical form and every existing signature
becomes unverifiable.**

### What signing does not prove

That the analysis is correct, or that Stockfish was honest. It proves the bytes
have not changed since a holder of the key saw them. Provenance, not truth.

## Why AWS IAM auth

The client signs an `sts:GetCallerIdentity` request with credentials it already
has; Vault forwards that signed request to AWS to learn who is calling. **No
Vault-specific credential is created, delivered, stored, or rotated.**

AppRole is the more commonly demonstrated pattern, and it is weaker here: a
`role_id`/`secret_id` pair is itself a secret you must deliver and rotate,
which relocates the problem rather than solving it.

`resolve_aws_unique_ids=false` is set. By default Vault resolves the bound ARN
to AWS's internal unique id, which requires Vault to hold AWS credentials to
call `iam:GetUser`; this lab Vault deliberately holds none, so it compares ARN
strings. The trade is real — unique-id binding survives a rename and rejects a
deleted-then-recreated principal of the same name, string binding does the
opposite. In production, give Vault a read-only IAM identity and let it resolve.

## Honest limits of this deployment

| | lab | production |
|---|---|---|
| nodes | one, file storage | 3 or 5, integrated raft |
| TLS | disabled, loopback only | required |
| unseal | keys in `vault/.init-keys.json` beside the server | auto-unseal via KMS or transit, shares held offline |
| mlock | disabled | enabled |

Storing unseal keys next to the server defeats the point of splitting them into
shares. It is a laptop lab and that is an acceptable trade; it is not
acceptable anywhere else. The production answer is `seal "awskms"` — roughly
ten lines of config and a $1/month KMS key.

The auth methods, policies, KV mechanics, and transit signing behave exactly as
they would in production. Those are the parts worth rehearsing.

## Why the env fallback stays

`chessvault.lichess_token()` still falls back to `LICHESS_TOKEN`, and says so
loudly on stderr. Removing it would mean a laptop without Vault running cannot
ingest at all — trading a real capability for a symbolic win. The fallback is
not the security story; the default path is.

## One role per job

Two AWS auth roles, both bound to the same IAM principal:

| role | policy | may | may not |
|---|---|---|---|
| `chessbook-ingest` | `chessbook-ingest` | read `chessbook/lichess` | sign, read the public key, write, list |
| `chessbook-signer` | `chessbook-signer` | sign, verify, read the public key | read the Lichess secret, export the key |

The same principal backing both looks redundant until you ask what each job is
allowed to do. A single role carrying both policies would mean an ingest run
compromised mid-flight could forge signatures. The caller picks the role, so it
picks its own privileges — and can only ever pick down.

```bash
AWS_PROFILE=tf python vault/test-policies.py     # 9/9
```

That test refuses to run with `VAULT_TOKEN` set, because a root token passes
everything and would turn it into a no-op that looks like it is working. The
assertion that matters most is that the signer cannot export the transit
private key. If that ever returns ALLOWED, every signature the project has
produced is forgeable.

Eighteen assertions across four groups: direct access, escalation paths around
it, path scoping, and self-escalation.

### The escalation checks are capability queries, not attempts

`transit/keys/<name>/config` is checked with `sys/capabilities-self` rather
than by trying the write. That is not fastidiousness — it is a scar.

An earlier version of this test performed the write for real. While
demonstrating that the test could actually fail, a deliberately injected policy
regression let the write succeed, and the test set `exportable=true` and
`allow_plaintext_backup=true` on the live signing key. **Both flags are one-way
in Vault: they cannot be set back to false.** The key had to be destroyed with
`deletion_allowed=true`, recreated, and all 3183 signatures regenerated under a
new public key.

A test that permanently weakens the property it is testing — and does so
exactly when that property is already broken — is worse than no test.
`sys/capabilities-self` asks Vault what the token *would* be permitted to do
and changes nothing. Same authorisation surface, no side effects. Verified:
with the regression injected the check still reports FAIL and exits 1, and the
key's flags are unchanged before and after.

**The compromised key was** `1pD7E7+oKkHBXqEmQL+7it5O5NfgTik+FPZQJg8SQfM=`.
Any signature bearing it should be treated as unverifiable — its private half
was exportable by root for the few minutes before it was destroyed.

## What is still unproven

- **Token renewal is unexercised.** `renew()` exists and both policies allow
  `renew-self`, but no run has lasted past the 20-minute TTL.
- **Key rotation is unexercised.** Transit supports rotating
  `chessbook-analysis`; signatures carry a `vault:v1:` version prefix so old
  ones stay verifiable. Nothing has tested that, and the signature manifest
  records only one public key — a rotation would need it to record several.
- **Nothing enforces that published analysis is signed.** `sign_analysis.py`
  is a command someone has to remember to run. A pre-publish hook that refuses
  unsigned artifacts is the difference between a capability and a guarantee.
