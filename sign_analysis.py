#!/usr/bin/env python3
"""
sign_analysis.py — sign the analysis corpus, and verify it without Vault.

    python sign_analysis.py sign                 # needs Vault
    python sign_analysis.py verify               # needs NOTHING but the files
    python sign_analysis.py verify --via-vault   # cross-check through transit

THE ARGUMENT THIS MAKES.
The corpus already records what produced each analysis: the engine name, the
engine binary's sha256, and the search parameters. All of that is self-reported
— a file claiming to come from Stockfish 17.1 is trivially forged by editing
the file. Signing closes that gap. A published variation can be checked against
a key held only by Vault, and the private half of that key cannot be exported
by any API call, including as root.

VERIFICATION IS OFFLINE BY DEFAULT, AND THAT IS THE POINT.
`verify` needs the signature file and the analysis files, nothing else. No
Vault, no network, no credentials. A signature only its issuer can check is
almost useless to a reader; ed25519 public keys make third-party verification
a property of the format rather than a service you have to keep running.

WHAT IS SIGNED.
The sha256 of the canonical JSON (sorted keys, no insignificant whitespace,
UTF-8), as a 64-character hex string, encoded ASCII. That canonical form is
part of the contract — see chessvault.canonical_bytes(). Change it and every
existing signature becomes unverifiable, which is why signatures.json records
the rule in the file itself rather than leaving it implicit in this source.

WHAT THIS DOES NOT PROVE.
That the analysis is correct, or that the engine was honest. It proves the
bytes have not changed since a holder of the signing key saw them. Provenance,
not truth.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import chessvault as cv

SIG_FILE = "signatures.json"
BATCH = 200

CANONICAL_SPEC = (
    "sha256 of json.dumps(doc, sort_keys=True, separators=(',',':')).encode('utf-8'); "
    "the signed message is that digest as a 64-char lowercase hex string, ASCII-encoded"
)


def load_docs(analysis_dir: Path) -> dict[str, dict]:
    docs = {}
    for f in sorted(analysis_dir.glob("*.json")):
        try:
            docs[f.stem] = json.loads(f.read_text())
        except json.JSONDecodeError as exc:
            print(f"  unreadable, skipping: {f.name} ({exc})", file=sys.stderr)
    return docs


# --- sign -------------------------------------------------------------------

def cmd_sign(args) -> int:
    analysis = args.corpus / "analysis"
    docs = load_docs(analysis)
    if not docs:
        print(f"no analysis files in {analysis}", file=sys.stderr)
        return 1
    print(f"signing {len(docs)} analysis file(s)", file=sys.stderr)

    try:
        c = cv.client(cv.ROLE_SIGNER)
    except cv.VaultUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    uids = sorted(docs)
    digests = {u: cv.digest(docs[u]) for u in uids}
    sigs: dict[str, dict] = {}

    # Batched: 3183 individual round trips would take minutes and hammer the
    # audit log for no benefit. Transit takes a batch and returns results in
    # the order submitted.
    for i in range(0, len(uids), BATCH):
        chunk = uids[i : i + BATCH]
        resp = c.secrets.transit.sign_data(
            name=cv.TRANSIT_KEY,
            batch_input=[
                {"input": base64.b64encode(digests[u].encode()).decode()}
                for u in chunk
            ],
        )
        results = resp["data"]["batch_results"]
        if len(results) != len(chunk):
            print(f"error: transit returned {len(results)} results for "
                  f"{len(chunk)} inputs", file=sys.stderr)
            return 1
        for u, r in zip(chunk, results):
            if "signature" not in r:
                print(f"error: no signature for {u}: {r}", file=sys.stderr)
                return 1
            sigs[u] = {"sha256": digests[u], "signature": r["signature"]}
        print(f"  {min(i+BATCH, len(uids))}/{len(uids)}", file=sys.stderr)

    payload = {
        "key": cv.TRANSIT_KEY,
        "algorithm": "ed25519",
        "public_key": cv.public_key(c),
        "canonical": CANONICAL_SPEC,
        "signed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(sigs),
        "signatures": sigs,
    }
    out = args.corpus / SIG_FILE
    out.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nwrote {out} ({len(sigs)} signatures)", file=sys.stderr)
    print(f"public key: {payload['public_key']}", file=sys.stderr)
    return 0


# --- verify -----------------------------------------------------------------

def verify_offline(pub_b64: str, digest_hex: str, signature: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    # "vault:v1:<b64>" — the version prefix is Vault's key-rotation marker.
    try:
        raw = base64.b64decode(signature.split(":", 2)[2])
    except (IndexError, ValueError):
        return False
    try:
        pub.verify(raw, digest_hex.encode())
        return True
    except InvalidSignature:
        return False


def cmd_verify(args) -> int:
    sig_path = args.corpus / SIG_FILE
    if not sig_path.exists():
        print(f"no {sig_path} — run `sign_analysis.py sign` first", file=sys.stderr)
        return 1
    manifest = json.loads(sig_path.read_text())
    sigs = manifest["signatures"]
    pub = manifest["public_key"]

    docs = load_docs(args.corpus / "analysis")

    print(f"signature file : {sig_path}")
    print(f"  signed at    : {manifest.get('signed_at')}")
    print(f"  key          : {manifest.get('key')} ({manifest.get('algorithm')})")
    print(f"  public key   : {pub}")
    print(f"  mode         : {'via Vault transit' if args.via_vault else 'OFFLINE (no Vault, no network)'}")
    print()

    c = None
    if args.via_vault:
        try:
            c = cv.client(cv.ROLE_SIGNER, quiet=True)
        except cv.VaultUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    ok = tampered = missing_sig = unsigned = 0
    problems: list[str] = []

    for uid, doc in sorted(docs.items()):
        rec = sigs.get(uid)
        if rec is None:
            unsigned += 1
            problems.append(f"  UNSIGNED   {uid} — present on disk, absent from the manifest")
            continue
        d = cv.digest(doc)
        if d != rec["sha256"]:
            tampered += 1
            problems.append(f"  CHANGED    {uid} — digest {d[:16]}… != signed {rec['sha256'][:16]}…")
            continue
        good = (cv.verify(c, doc, rec)[0] if args.via_vault
                else verify_offline(pub, d, rec["signature"]))
        if good:
            ok += 1
        else:
            missing_sig += 1
            problems.append(f"  BAD SIG    {uid} — digest matches but signature does not verify")

    # A signature for a file that no longer exists is its own kind of problem.
    orphans = sorted(set(sigs) - set(docs))
    for uid in orphans:
        problems.append(f"  ORPHAN     {uid} — signed, but no such analysis file")

    print(f"  verified   : {ok}")
    print(f"  changed    : {tampered}")
    print(f"  bad sig    : {missing_sig}")
    print(f"  unsigned   : {unsigned}")
    print(f"  orphaned   : {len(orphans)}")
    if problems:
        print()
        for p in problems[:30]:
            print(p)
        if len(problems) > 30:
            print(f"  ... and {len(problems)-30} more")
        print("\nFAILED")
        return 1
    print("\nALL SIGNATURES VALID")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("sign", help="sign every analysis file (needs Vault)")
    ps.add_argument("--corpus", type=Path, default=Path("./corpus"))
    ps.set_defaults(func=cmd_sign)

    pv = sub.add_parser("verify", help="verify signatures (offline by default)")
    pv.add_argument("--corpus", type=Path, default=Path("./corpus"))
    pv.add_argument("--via-vault", action="store_true",
                    help="cross-check through transit instead of locally")
    pv.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
