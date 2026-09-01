#!/usr/bin/env python3
"""
test-policies.py — assert that each Vault role refuses what it should.

    AWS_PROFILE=tf python vault/test-policies.py

A policy is only correct if it DENIES the right things, and nothing about a
successful pipeline run tells you that. Every check below that expects DENIED
is the actual test; the ALLOWED ones only confirm the roles are not broken.

Run this after any change to vault/policies/*.hcl or to the role bindings in
bootstrap.sh. Exit code is non-zero if any assertion fails, so it works in CI.

Deliberately does NOT use VAULT_TOKEN: a root token passes everything and would
turn this file into a no-op that looks like it is working.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chessvault as cv  # noqa: E402

PROBE = {"probe": "policy-test"}


def attempt(label: str, fn, should_succeed: bool) -> bool:
    try:
        fn()
        got = "ALLOWED"
    except Exception as exc:
        got = f"DENIED ({type(exc).__name__})"
    want = "ALLOWED" if should_succeed else "DENIED"
    ok = got.startswith(want)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:<46} {got}")
    return ok


def main() -> int:
    if os.environ.get("VAULT_TOKEN"):
        print("refusing to run with VAULT_TOKEN set — a root token passes\n"
              "everything and makes this test meaningless. Unset it.",
              file=sys.stderr)
        return 2

    try:
        ing = cv.client(cv.ROLE_INGEST, quiet=True)
        sig = cv.client(cv.ROLE_SIGNER, quiet=True)
    except cv.VaultUnavailable as exc:
        print(f"cannot authenticate: {exc}", file=sys.stderr)
        return 1

    def read_secret(c):
        return c.secrets.kv.v2.read_secret_version(
            mount_point=cv.KV_MOUNT, path=cv.LICHESS_PATH,
            raise_on_deleted_version=True)

    results = []
    print("--- chessbook-ingest: reads the secret, cannot sign ---")
    results.append(attempt("read chessbook/lichess", lambda: read_secret(ing), True))
    results.append(attempt("sign via transit", lambda: cv.sign(ing, PROBE), False))
    results.append(attempt("read transit public key", lambda: cv.public_key(ing), False))

    print("\n--- chessbook-signer: signs, cannot read the secret ---")
    results.append(attempt("sign via transit", lambda: cv.sign(sig, PROBE), True))
    results.append(attempt("read transit public key", lambda: cv.public_key(sig), True))
    results.append(attempt("read chessbook/lichess", lambda: read_secret(sig), False))

    print("\n--- neither writes secrets, lists the mount, or exports the key ---")
    results.append(attempt(
        "ingest: write chessbook/lichess",
        lambda: ing.secrets.kv.v2.create_or_update_secret(
            mount_point=cv.KV_MOUNT, path=cv.LICHESS_PATH, secret={"token": "x"}), False))
    results.append(attempt(
        "ingest: list chessbook/",
        lambda: ing.secrets.kv.v2.list_secrets(mount_point=cv.KV_MOUNT, path=""), False))
    # The one that matters most: if this ever returns ALLOWED, every signature
    # the project has ever produced is forgeable by whoever got that response.
    results.append(attempt(
        "signer: export the transit private key",
        lambda: sig.secrets.transit.export_key(
            name=cv.TRANSIT_KEY, key_type="signing-key"), False))

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} policy assertions passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
