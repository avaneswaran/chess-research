#!/usr/bin/env python3
"""
collect.py — bring Lichess studies and local-disk PGNs into the corpus.

Runs after (or alongside) ingest.py and merges into the same index, deduping
by content-addressed UID. Safe to re-run: re-running adds source records to
existing entries rather than duplicating games.

    # your OTB games, entered by hand into Lichess studies
    # token comes from Vault (see VAULT.md); LICHESS_TOKEN still works as a
    # fallback. Either way it needs study:read for private studies.
    python collect.py --corpus ./corpus --studies AerialAttack

    # the scattered stuff on disk
    python collect.py --corpus ./corpus \
        --scan /mnt/c/Users/scud1/Downloads \
        --scan /mnt/c/Users/scud1/Desktop \
        --scan /mnt/c/Users/scud1/Documents

    # see what got quarantined and why, before trusting anything
    python collect.py --corpus ./corpus --report

Quarantine policy: any game where neither player matches your alias list goes
to corpus/quarantine/ and is never counted. If real games of yours land there,
the fix is to add the name variant to corpus/aliases.json and re-run — not to
loosen the check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests

import chessvault
from classify import classify
from provenance import (
    classify_tier,
    compute_uids,
    has_annotations,
    identify,
    identify_from_event,
    load_aliases,
    looks_like_tournament,
    merge_source,
    read_games,
)

UA = "chessbook-collect/0.1 (personal opening-book project)"
# NOTE: the /api/ prefix is required. https://lichess.org/study/by/... is
# the web route and returns 404 to API clients even with study:read.
STUDY_EXPORT = "https://lichess.org/api/study/by/{user}/export.pgn"

PGN_SUFFIXES = {".pgn"}
# ChessBase native formats are not readable here. Flagged, not parsed.
OPAQUE_SUFFIXES = {".cbh", ".cbv", ".cbf", ".si4", ".sg4", ".sn4"}


# --- sources ----------------------------------------------------------------

def fetch_studies(user: str) -> str:
    # Vault first, LICHESS_TOKEN second. Studies are the case where a missing
    # token is quietly destructive rather than merely slow: you get the public
    # subset and no error, so the warning below matters.
    token = chessvault.lichess_token()
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("  WARNING: no token from Vault or LICHESS_TOKEN — you will get "
              "PUBLIC studies only. Private OTB studies will be silently "
              "missing.", file=sys.stderr)

    r = requests.get(STUDY_EXPORT.format(user=user), headers=headers,
                     stream=True, timeout=300)
    if r.status_code == 401:
        print("  401 — token missing or lacks study:read scope", file=sys.stderr)
        return ""
    r.raise_for_status()
    return r.text


def scan_disk(roots: list[Path]) -> list[tuple[Path, str]]:
    found, opaque = [], []
    for root in roots:
        if not root.exists():
            print(f"  skip (missing): {root}", file=sys.stderr)
            continue
        print(f"  walking {root} ...", file=sys.stderr)
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            suf = path.suffix.lower()
            if suf in PGN_SUFFIXES:
                try:
                    found.append((path, path.read_text(errors="replace")))
                except Exception as e:
                    print(f"  unreadable {path}: {e}", file=sys.stderr)
            elif suf in OPAQUE_SUFFIXES:
                opaque.append(path)

    if opaque:
        print(f"\n  {len(opaque)} ChessBase/Scid database file(s) found — these "
              f"need export to PGN before they can be used:", file=sys.stderr)
        for p in opaque[:10]:
            print(f"    {p}", file=sys.stderr)
    return found


# --- corpus merge -----------------------------------------------------------

def absorb(games_iter, source_kind: str, source_ref: str, corpus: Path,
           index_by_uid: dict, aliases: list[str], stats: Counter,
           move_uid_map: dict) -> None:
    games_dir = corpus / "games"
    quar_dir = corpus / "quarantine"
    games_dir.mkdir(parents=True, exist_ok=True)
    quar_dir.mkdir(parents=True, exist_ok=True)

    for game in games_iter:
        try:
            pgn_text = str(game)
            event = game.headers.get("Event", "")
            side = identify(game, aliases)
            identity_from_event = False

            # Lichess study chapters usually have empty White/Black headers and
            # put the chapter title in Event instead. Fall back to parsing it.
            if side == "foreign" and source_kind in ("study", "local"):
                recovered = identify_from_event(event, aliases)
                if recovered:
                    side = recovered
                    identity_from_event = True

            stats[f"seen_{source_kind}"] += 1

            if side == "foreign":
                stats["quarantined"] += 1
                uid, _ = compute_uids(game)
                (quar_dir / f"{uid}.pgn").write_text(pgn_text)
                continue

            game_uid, move_uid = compute_uids(game)
            tax = classify(game)
            tier = classify_tier(game, source_kind, None)
            # A chapter titled like a tournament round IS an OTB game, even
            # with no TimeControl tag. These are the only slow games in the
            # project; leaving them at 'unknown' would give them zero weight.
            if source_kind == "study" and looks_like_tournament(event):
                tier = "otb_classical"
                stats["otb_recovered"] += 1

            source = {
                "kind": source_kind,
                "ref": source_ref,
                "tier": tier,
                "has_annotations": has_annotations(pgn_text),
            }

            if game_uid in index_by_uid:
                merge_source(index_by_uid[game_uid], source)
                stats["merged_duplicate"] += 1
                continue

            # same moves, different headers — surface, don't merge
            if move_uid in move_uid_map and move_uid_map[move_uid] != game_uid:
                stats["candidate_duplicate"] += 1

            (games_dir / f"{game_uid}.pgn").write_text(pgn_text)

            entry = {
                "game_uid": game_uid,
                "move_uid": move_uid,
                "side": side,
                "in_book": side == "white" and tax["in_scope"],
                "tier": tier,
                "date": game.headers.get("UTCDate") or game.headers.get("Date"),
                "event": event,
                "identity_from_event": identity_from_event,
                "white": game.headers.get("White"),
                "black": game.headers.get("Black"),
                "result": game.headers.get("Result"),
                "eco": game.headers.get("ECO"),
                "node_id": tax["node_id"],
                "node_name": tax["node_name"],
                "node_path": tax["path"],
                "opening_san": tax["san"],
                "sources": [],
                "analyzed": False,
            }
            merge_source(entry, source)
            index_by_uid[game_uid] = entry
            move_uid_map.setdefault(move_uid, game_uid)
            stats["added"] += 1
            if entry["in_book"]:
                stats["in_book"] += 1

        except Exception as e:
            stats["errors"] += 1
            print(f"  error on a game from {source_ref}: {e}", file=sys.stderr)


def load_index(corpus: Path) -> dict:
    path = corpus / "index.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    out = {}
    for e in raw:
        # entries written by the original ingest.py predate game_uid
        uid = e.get("game_uid") or e.get("canonical_id")
        out[uid] = e
    return out


def report(corpus: Path) -> None:
    index = load_index(corpus)
    by_tier = Counter(e.get("tier", "unknown") for e in index.values())
    by_node = Counter(e["node_id"] for e in index.values()
                      if e.get("in_book") and e.get("node_id"))
    annotated = sum(1 for e in index.values() if e.get("annotated_source"))
    quar = len(list((corpus / "quarantine").glob("*.pgn"))) \
        if (corpus / "quarantine").exists() else 0

    print(f"\ncorpus: {len(index)} games   quarantined: {quar}   "
          f"hand-annotated: {annotated}\n")
    print("by trust tier")
    for tier, n in by_tier.most_common():
        print(f"  {tier:<20} {n:>6}")
    print("\nin-book nodes (White, 3.Nf3 complex)")
    for node, n in by_node.most_common():
        print(f"  {node:<45} {n:>6}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="./corpus", type=Path)
    ap.add_argument("--studies", help="lichess username to export studies from")
    ap.add_argument("--scan", action="append", default=[], type=Path)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report(args.corpus)
        return

    aliases = load_aliases(args.corpus)
    print(f"identity aliases: {aliases}\n", file=sys.stderr)

    index_by_uid = load_index(args.corpus)
    move_uid_map = {e.get("move_uid"): uid for uid, e in index_by_uid.items()
                    if e.get("move_uid")}
    stats: Counter = Counter()

    if args.studies:
        print(f"Fetching Lichess studies for {args.studies} ...", file=sys.stderr)
        text = fetch_studies(args.studies)
        if text:
            absorb(read_games(text), "study",
                   f"lichess:studies:{args.studies}", args.corpus,
                   index_by_uid, aliases, stats, move_uid_map)

    if args.scan:
        print(f"\nScanning {len(args.scan)} location(s) ...", file=sys.stderr)
        for path, text in scan_disk(args.scan):
            absorb(read_games(text), "local", str(path), args.corpus,
                   index_by_uid, aliases, stats, move_uid_map)

    (args.corpus / "index.json").write_text(
        json.dumps(list(index_by_uid.values()), indent=2))

    print("\n--- collection summary ---", file=sys.stderr)
    for k, v in sorted(stats.items()):
        print(f"  {k:<22} {v:>6}", file=sys.stderr)
    print("\nRun with --report for the tier and node breakdown.", file=sys.stderr)
    if stats.get("quarantined"):
        print(f"\n{stats['quarantined']} games quarantined in "
              f"{args.corpus}/quarantine/. Spot-check a few: if any are yours, "
              f"add the name variant to {args.corpus}/aliases.json and re-run.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
