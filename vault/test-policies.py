#!/usr/bin/env python3
"""
test-policies.py — assert that each Vault role refuses what it should.

    AWS_PROFILE=tf python vault/test-policies.py

A policy is only correct if it DENIES the right things, and nothing about a
successful pipeline run tells you that. Every check below that expects DENIED
is the actual test; the ALLOWED ones only confirm the roles are not broken.

Run this after any change to vault/policies/*.hcl or to the role bindings in
bootstrap.sh. Exit code is non-zero if any assertion fails, so it works in CI.

Deny-testing only covers the denials you thought to write. These assertions
cover direct access, the escalation paths around it (making the key exportable
rather than reading it), path scoping (a grant that quietly extends to
neighbours), and self-escalation (writing a policy, mounting an engine). A
grant nobody imagined would still pass — so treat a green run as evidence, not
proof, and add a case whenever a policy grows.

Deliberately does NOT use VAULT_TOKEN: a root token passes everything and would
turn this file into a no-op that looks like it is working.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chessvault as cv  # noqa: E402

PROBE = {"probe": "policy-test"}


def capability(c, who: str, path: str, cap: str, should_have: bool) -> bool:
    """Ask Vault whether this token may do `cap` on `path`, without doing it.

    Preferred over attempt() for anything destructive or irreversible. Vault
    resolves the same policy rules it would enforce on a real request.
    """
    try:
        caps = c.sys.get_capabilities(paths=[path])["data"].get(path, [])
    except Exception as exc:
        print(f"  [FAIL] {who}: capability check on {path} errored: {exc}")
        return False
    has = cap in caps or "root" in caps
    ok = has == should_have
    verdict = "ALLOWED" if has else "DENIED"
    label = f"{who}: {cap} {path}"
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:<46} {verdict}")
    return ok


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

    # --- escalation paths ---------------------------------------------------
    #
    # Direct export is denied above. These are the ways round it: rather than
    # reading the key, change the rules so that reading becomes legal.
    #
    # CHECKED BY CAPABILITY QUERY, NOT BY ATTEMPTING THE WRITE.
    #
    # An earlier version of this file performed these actions for real. When a
    # policy regression made them succeed, the test did what it was asked and
    # set exportable=true on the live signing key — and `exportable` and
    # `allow_plaintext_backup` are ONE-WAY in Vault. They cannot be set back to
    # false. The key had to be destroyed and the whole corpus re-signed.
    #
    # A test that permanently weakens the property it is testing, precisely
    # when that property is already broken, is worse than no test. sys/
    # capabilities-self asks Vault what the token WOULD be permitted to do on a
    # path and changes nothing. Same authorisation surface, no side effects.
    print("\n--- escalation: cannot make the key exportable, rotate, or delete it ---")
    results.append(capability(
        sig, "signer", f"transit/keys/{cv.TRANSIT_KEY}/config", "update", False))
    results.append(capability(
        sig, "signer", f"transit/keys/{cv.TRANSIT_KEY}/rotate", "update", False))
    results.append(capability(
        sig, "signer", f"transit/keys/{cv.TRANSIT_KEY}", "delete", False))
    results.append(capability(
        sig, "signer", f"transit/export/signing-key/{cv.TRANSIT_KEY}", "read", False))

    # --- path scoping -------------------------------------------------------
    #
    # Both policies name exact paths, not prefixes. These prove the grants do
    # not silently extend to neighbours — the failure mode of a `transit/*` or
    # `chessbook/data/*` rule written in a hurry.
    print("\n--- path scoping: grants do not extend to neighbouring paths ---")
    results.append(attempt(
        "signer: sign with a different transit key",
        lambda: sig.secrets.transit.sign_data(
            name="some-other-key", hash_input="YWJj"), False))
    results.append(attempt(
        "ingest: read a different KV path",
        lambda: ing.secrets.kv.v2.read_secret_version(
            mount_point=cv.KV_MOUNT, path="somethingelse",
            raise_on_deleted_version=True), False))

    # --- self-escalation ----------------------------------------------------
    print("\n--- cannot enumerate or grant privilege ---")
    results.append(attempt(
        "ingest: list ACL policies",
        lambda: ing.sys.list_acl_policies(), False))
    results.append(attempt(
        "ingest: write a new ACL policy",
        lambda: ing.sys.create_or_update_acl_policy(
            name="pwn", policy='path "*" { capabilities = ["sudo","read"] }'), False))
    results.append(attempt(
        "signer: mount a new secrets engine",
        lambda: sig.sys.enable_secrets_engine(backend_type="kv", path="pwn"), False))

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} policy assertions passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
