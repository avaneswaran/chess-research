#!/usr/bin/env python3
"""
ingest.py — pull AerialAttack's games from chess.com and Lichess, filter to the
3.Nf3 gambit complex, and write a normalized corpus.

Output layout:
    corpus/
      games/<canonical_id>.pgn      one game per file, untouched PGN
      index.json                    metadata + taxonomy node for every game

Canonical IDs are stable and platform-prefixed:
    cc:<chesscom_game_id>
    li:<lichess_game_id>

Nothing here mutates PGN. The PGN on disk stays the source of truth; every
downstream artifact (analysis, prose, site pages) keys off canonical_id.

Usage:
    python ingest.py --out ./corpus
    python ingest.py --out ./corpus --since 2023-01 --platforms lichess

Rate limits:
    chess.com  — serial requests only; parallel calls get 429. A descriptive
                 User-Agent is effectively required or you get blocked.
    lichess    — ~20 games/sec anonymous. A personal API token roughly doubles
                 throughput and is read from LICHESS_TOKEN (see README for the
                 Vault wiring).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import chess.pgn
import requests

from classify import classify
from provenance import classify_tier, compute_uids, has_annotations, identify, load_aliases, merge_source

USERNAME = "AerialAttack"
UA = "chessbook-ingest/0.1 (personal opening-book project; contact: anand)"

CHESSCOM_ARCHIVES = "https://api.chess.com/pub/player/{user}/games/archives"
LICHESS_EXPORT = "https://lichess.org/api/games/user/{user}"


# --- chess.com --------------------------------------------------------------

def fetch_chesscom(user: str, since: str | None) -> list[dict]:
    """Walk monthly archives serially. Returns raw game dicts."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    r = s.get(CHESSCOM_ARCHIVES.format(user=user.lower()), timeout=30)
    if r.status_code == 404:
        print(f"  chess.com: no such user '{user}'", file=sys.stderr)
        return []
    r.raise_for_status()
    archives = r.json().get("archives", [])

    if since:
        # archive URLs end in /YYYY/MM
        archives = [a for a in archives if a[-7:].replace("/", "-") >= since]

    games = []
    for i, url in enumerate(archives, 1):
        print(f"  chess.com archive {i}/{len(archives)}: {url[-7:]}", file=sys.stderr)
        resp = s.get(url, timeout=60)
        if resp.status_code == 429:
            time.sleep(10)
            resp = s.get(url, timeout=60)
        resp.raise_for_status()
        games.extend(resp.json().get("games", []))
        time.sleep(0.6)  # be polite; this is someone else's free API
    return games


def normalize_chesscom(g: dict) -> dict | None:
    pgn_text = g.get("pgn")
    if not pgn_text or g.get("rules") != "chess":
        return None
    # game id is the trailing path segment of the game URL
    gid = g.get("url", "").rstrip("/").split("/")[-1]
    return {
        "canonical_id": f"cc:{gid}",
        "platform": "chess.com",
        "url": g.get("url"),
        "pgn": pgn_text,
        "time_class": g.get("time_class"),
        "time_control": g.get("time_control"),
        "rated": g.get("rated"),
    }


# --- lichess ----------------------------------------------------------------

def fetch_lichess(user: str, since: str | None) -> list[dict]:
    """Stream NDJSON export. Includes server-side evals where Lichess has them."""
    headers = {"Accept": "application/x-ndjson", "User-Agent": UA}
    token = os.environ.get("LICHESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        print("  lichess: using token from LICHESS_TOKEN", file=sys.stderr)

    params = {
        "rated": "true",
        "perfType": "bullet,blitz,rapid,classical,correspondence",
        "opening": "true",
        "evals": "true",
        "clocks": "false",
        "pgnInJson": "true",
    }
    if since:
        dt = datetime.strptime(since, "%Y-%m")
        params["since"] = int(dt.timestamp() * 1000)

    url = LICHESS_EXPORT.format(user=user)
    out = []
    with requests.get(url, headers=headers, params=params, stream=True, timeout=300) as r:
        if r.status_code == 404:
            print(f"  lichess: no such user '{user}'", file=sys.stderr)
            return []
        r.raise_for_status()
        for n, line in enumerate(r.iter_lines(), 1):
            if not line:
                continue
            out.append(json.loads(line))
            if n % 500 == 0:
                print(f"  lichess: {n} games streamed", file=sys.stderr)
    return out


def normalize_lichess(g: dict) -> dict | None:
    if g.get("variant") != "standard" or not g.get("pgn"):
        return None
    return {
        "canonical_id": f"li:{g['id']}",
        "platform": "lichess",
        "url": f"https://lichess.org/{g['id']}",
        "pgn": g["pgn"],
        "time_class": g.get("speed"),
        "time_control": None,
        "rated": g.get("rated"),
        "lichess_opening": (g.get("opening") or {}).get("name"),
    }


# --- corpus assembly --------------------------------------------------------

def build(records: list[dict], user: str, out_dir: Path) -> list[dict]:
    """Merge API records into the corpus, keyed by content-addressed game_uid.

    Same schema as collect.py so studies and local files land in the same
    index. Black-side games are stored but marked in_book=False: this is a
    White repertoire, but throwing away data you already have is a decision
    that's annoying to reverse.
    """
    games_dir = out_dir / "games"
    games_dir.mkdir(parents=True, exist_ok=True)
    aliases = load_aliases(out_dir)

    # Merge into whatever is already there. Running ingest for a second
    # platform must not discard the first one's results.
    index_by_uid: dict[str, dict] = {}
    existing_path = out_dir / "index.json"
    if existing_path.exists():
        try:
            for e in json.loads(existing_path.read_text()):
                uid = e.get("game_uid") or e.get("canonical_id")
                if uid:
                    index_by_uid[uid] = e
            print(f"  merging into {len(index_by_uid)} existing entries",
                  file=sys.stderr)
        except json.JSONDecodeError:
            print("  existing index.json unreadable — starting fresh",
                  file=sys.stderr)

    skipped = 0

    for rec in records:
        game = chess.pgn.read_game(io.StringIO(rec["pgn"]))
        if game is None:
            skipped += 1
            continue

        side = identify(game, aliases)
        if side == "foreign":
            skipped += 1
            continue

        tax = classify(game)
        in_book = side == "white" and tax["in_scope"]
        if not in_book:
            continue

        game_uid, move_uid = compute_uids(game)
        tier = classify_tier(game, "api", rec.get("time_class"))
        source = {
            "kind": "api",
            "ref": rec["canonical_id"],
            "platform": rec["platform"],
            "url": rec["url"],
            "tier": tier,
            "has_annotations": has_annotations(rec["pgn"]),
        }

        if game_uid in index_by_uid:
            merge_source(index_by_uid[game_uid], source)
            continue

        (games_dir / f"{game_uid}.pgn").write_text(rec["pgn"])

        entry = {
            "game_uid": game_uid,
            "move_uid": move_uid,
            "side": side,
            "in_book": in_book,
            "tier": tier,
            "date": game.headers.get("UTCDate") or game.headers.get("Date"),
            "event": game.headers.get("Event"),
            "white": game.headers.get("White"),
            "black": game.headers.get("Black"),
            "opponent_elo": game.headers.get("BlackElo"),
            "own_elo": game.headers.get("WhiteElo"),
            "result": game.headers.get("Result"),
            "eco": game.headers.get("ECO"),
            "time_class": rec.get("time_class"),
            "time_control": rec.get("time_control"),
            "rated": rec.get("rated"),
            "node_id": tax["node_id"],
            "node_name": tax["node_name"],
            "node_path": tax["path"],
            "classified_ply": tax["classified_ply"],
            "opening_san": tax["san"],
            "ply_count": len(list(game.mainline_moves())),
            "sources": [],
            "analyzed": False,
        }
        merge_source(entry, source)
        index_by_uid[game_uid] = entry

    print(f"  skipped {skipped} unparseable/foreign games", file=sys.stderr)
    return list(index_by_uid.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=USERNAME)
    ap.add_argument("--out", default="./corpus", type=Path)
    ap.add_argument("--since", help="YYYY-MM lower bound")
    ap.add_argument("--platforms", default="chesscom,lichess")
    args = ap.parse_args()

    platforms = {p.strip() for p in args.platforms.split(",")}
    records = []

    if "chesscom" in platforms:
        print("Fetching chess.com...", file=sys.stderr)
        for g in fetch_chesscom(args.user, args.since):
            n = normalize_chesscom(g)
            if n:
                records.append(n)

    if "lichess" in platforms:
        print("Fetching lichess...", file=sys.stderr)
        for g in fetch_lichess(args.user, args.since):
            n = normalize_lichess(g)
            if n:
                records.append(n)

    print(f"\n{len(records)} raw games pulled. Filtering to the 3.Nf3 complex...",
          file=sys.stderr)
    index = build(records, args.user, args.out)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.json").write_text(json.dumps(index, indent=2))

    # quick distribution readout — this is your table of contents in embryo
    dist: dict[str, int] = {}
    for e in index:
        dist[e["node_id"]] = dist.get(e["node_id"], 0) + 1

    print(f"\n{len(index)} in-scope White games in corpus (all platforms).\n",
          file=sys.stderr)
    print(f"{'node':<45} {'games':>6}", file=sys.stderr)
    for nid, count in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"{nid:<45} {count:>6}", file=sys.stderr)


if __name__ == "__main__":
    main()
