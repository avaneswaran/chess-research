#!/usr/bin/env python3
"""
chessvault.py — the project's one door to Vault.

Two things live behind it:

    lichess_token()          the ingest credential, from KV v2
    sign() / verify()        transit signatures over analysis artifacts

WHY AWS IAM AUTH.
The client signs an sts:GetCallerIdentity request with credentials it already
has, and Vault forwards that signed request to AWS to learn who is calling. No
Vault-specific credential is created, delivered, stored, or rotated — the thing
you already have is the thing that authenticates. AppRole would work too, but a
role_id/secret_id pair is itself a secret you now have to deliver, which is the
problem this was supposed to solve rather than move.

WHY THE ENV FALLBACK STAYS.
`lichess_token()` still reads LICHESS_TOKEN if Vault is unreachable, and says
so loudly on stderr. Removing the fallback would mean a laptop without Vault
running cannot ingest at all, which trades a real capability for a symbolic
win. The fallback is not the security story; the default path is.

WHAT THIS DOES NOT DO.
It does not renew tokens in a background thread. Vault tokens here live 20
minutes and ingest runs are short. If an ingest ever runs long enough to
matter, `renew()` is there — the ingest policy allows renew-self precisely so
that a long run can extend its lease rather than dying on a 403 that looks like
a Lichess problem.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

DEFAULT_ADDR = "http://127.0.0.1:8200"
KV_MOUNT = "chessbook"
LICHESS_PATH = "lichess"
TRANSIT_KEY = "chessbook-analysis"
ROLE_INGEST = "chessbook-ingest"
ROLE_SIGNER = "chessbook-signer"


class VaultUnavailable(RuntimeError):
    """Vault could not be reached or authenticated against."""


def _log(msg: str) -> None:
    print(f"  vault: {msg}", file=sys.stderr)


def client(role: str = ROLE_INGEST, *, quiet: bool = False):
    """An authenticated hvac client for one job's role.

    `role` selects privileges: ROLE_INGEST can read the Lichess secret and
    cannot sign; ROLE_SIGNER can sign and cannot read the secret. The same AWS
    principal backs both, so the caller is choosing what it is allowed to do —
    and can only ever choose down. Ask for the role the work needs, never the
    union.

    Auth order, most-preferred last-resort first:

      1. VAULT_TOKEN in the environment. This is the admin path — the root
         token from bootstrap, or a token you minted by hand. Convenient, and
         exactly what the pipeline should NOT use day to day.
      2. AWS IAM. The real path. Uses whatever AWS credentials boto3 finds.

    Raises VaultUnavailable rather than returning a half-usable client, so
    callers can decide whether to fall back or fail.
    """
    try:
        import hvac
    except ImportError as exc:  # pragma: no cover
        raise VaultUnavailable(f"hvac not installed: {exc}") from exc

    addr = os.environ.get("VAULT_ADDR", DEFAULT_ADDR)
    c = hvac.Client(url=addr)

    token = os.environ.get("VAULT_TOKEN")
    if token:
        c.token = token
        if not quiet:
            _log(f"authenticated to {addr} with VAULT_TOKEN (admin path)")
    else:
        try:
            import boto3
            creds = boto3.Session().get_credentials()
            if creds is None:
                raise VaultUnavailable(
                    "no AWS credentials found; run `aws login` "
                    "(or set VAULT_TOKEN for the admin path)"
                )
            frozen = creds.get_frozen_credentials()
            c.auth.aws.iam_login(
                access_key=frozen.access_key,
                secret_key=frozen.secret_key,
                session_token=frozen.token,
                role=role,
            )
            if not quiet:
                _log(f"authenticated to {addr} via AWS IAM as role '{role}'")
        except VaultUnavailable:
            raise
        except Exception as exc:
            raise VaultUnavailable(f"AWS IAM auth failed: {exc}") from exc

    try:
        if not c.is_authenticated():
            raise VaultUnavailable("Vault rejected the credentials")
    except VaultUnavailable:
        raise
    except Exception as exc:
        raise VaultUnavailable(f"cannot reach Vault at {addr}: {exc}") from exc

    return c


def renew(c) -> None:
    """Extend the current token's lease. No-op for a root token, which has none."""
    try:
        c.auth.token.renew_self()
    except Exception as exc:
        _log(f"could not renew token: {exc}")


# --- the ingest credential --------------------------------------------------

def lichess_token(*, required: bool = False) -> str | None:
    """The Lichess API token, from Vault, falling back to the environment.

    Returns None when neither source has one — ingest.py treats that as
    "anonymous, at half throughput", which is a legitimate mode, not an error.
    """
    try:
        c = client(ROLE_INGEST)
        secret = c.secrets.kv.v2.read_secret_version(
            mount_point=KV_MOUNT, path=LICHESS_PATH, raise_on_deleted_version=True
        )
        tok = secret["data"]["data"].get("token")
        if tok:
            _log("lichess token read from Vault")
            return tok
        _log(f"{KV_MOUNT}/{LICHESS_PATH} exists but has no 'token' field")
    except VaultUnavailable as exc:
        _log(f"unavailable ({exc})")
    except Exception as exc:
        _log(f"could not read {KV_MOUNT}/{LICHESS_PATH}: {exc}")

    tok = os.environ.get("LICHESS_TOKEN")
    if tok:
        _log("FALLING BACK to LICHESS_TOKEN from the environment")
        return tok

    if required:
        raise VaultUnavailable(
            "no Lichess token in Vault or the environment. Store one with:\n"
            "  ./vault/put-lichess-token.sh"
        )
    return None


# --- transit signing --------------------------------------------------------

def canonical_bytes(doc: dict) -> bytes:
    """The exact bytes a signature covers.

    Sorted keys, no insignificant whitespace, UTF-8. A signature over
    "whatever json.dumps happened to emit" is not verifiable by anyone who
    re-serialises the document, so the canonical form is part of the contract,
    not an implementation detail. Change this and every existing signature
    becomes unverifiable.
    """
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(doc: dict) -> str:
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def sign(c, doc: dict) -> dict:
    """Sign a document. Returns the record to store alongside it.

    Signs the SHA-256 of the canonical form rather than the document itself:
    transit would otherwise base64 the whole payload over the wire for every
    one of 3183 files, and the hash is what a verifier recomputes anyway.
    """
    d = digest(doc)
    resp = c.secrets.transit.sign_data(
        name=TRANSIT_KEY,
        hash_input=base64.b64encode(d.encode()).decode(),
    )
    sig = resp["data"]["signature"]
    return {"sha256": d, "signature": sig, "key": TRANSIT_KEY}


def verify(c, doc: dict, record: dict) -> tuple[bool, str]:
    """Check a document against its signature record.

    Two ways to fail, and they mean different things:
      - digest mismatch: the document changed after signing
      - signature invalid: the record was not produced by this key
    """
    d = digest(doc)
    if d != record.get("sha256"):
        return False, f"digest mismatch (doc={d[:16]}… record={str(record.get('sha256'))[:16]}…)"
    resp = c.secrets.transit.verify_signed_data(
        name=TRANSIT_KEY,
        hash_input=base64.b64encode(d.encode()).decode(),
        signature=record["signature"],
    )
    return (True, "ok") if resp["data"]["valid"] else (False, "signature invalid")


def public_key(c) -> str:
    """The ed25519 public key, so a reader can verify without Vault access."""
    resp = c.secrets.transit.read_key(name=TRANSIT_KEY)
    keys = resp["data"]["keys"]
    latest = str(max(int(k) for k in keys))
    return keys[latest]["public_key"]


if __name__ == "__main__":
    # Smoke test: authenticate as each job role and report what it can do.
    for role in (ROLE_INGEST, ROLE_SIGNER):
        print(f"--- {role} ---")
        try:
            c = client(role)
        except VaultUnavailable as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            info = c.auth.token.lookup_self()["data"]
            print(f"  policies : {info.get('policies')}")
            print(f"  ttl      : {info.get('ttl')}s  renewable: {info.get('renewable')}")
        except Exception as exc:
            print(f"  (lookup-self failed: {exc})")
