#!/usr/bin/env python3
"""
analyze.py — run Stockfish over the corpus and emit reproducible, versioned
analysis JSON.

Design decisions worth knowing before you run this:

1. NODE-LIMITED, SINGLE-THREADED, FIXED HASH.
   Time-limited analysis is not reproducible; node-limited analysis with
   Threads=1 and a fixed Hash size is. That matters because a book's
   variations must be re-verifiable a year from now against the same engine
   build. The engine version is recorded in every output file.

2. MULTIPV=3.
   You need the second- and third-best moves to say anything honest about
   "only move" positions, which is where gambit play actually lives.

3. EVALS ARE STORED WHITE-POV in centipawns.
   Mate scores are stored separately as signed mate distance. Never collapse
   the two into one number.

Output:
    corpus/analysis/<game_uid>.json

Usage:
    python analyze.py --corpus ./corpus --engine /usr/local/bin/stockfish
    python analyze.py --corpus ./corpus --nodes 4000000 --only hub/nc6/goring
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

import chess
import chess.engine
import chess.pgn

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

# Thresholds in centipawns, from White's perspective normalized to the mover.
INACCURACY = 50
MISTAKE = 150
BLUNDER = 300
ONLY_MOVE_GAP = 200        # best vs second-best
SAC_MATERIAL = 300         # material handed over
SAC_HOLDS = -50            # ...and eval stays about here or better


def material_balance(board: chess.Board) -> int:
    """White material minus Black material, in centipawns."""
    total = 0
    for piece_type, value in PIECE_VALUES.items():
        total += len(board.pieces(piece_type, chess.WHITE)) * value
        total -= len(board.pieces(piece_type, chess.BLACK)) * value
    return total


def score_to_dict(score: chess.engine.PovScore) -> dict:
    """Normalize to White POV; keep mate separate from cp."""
    white = score.white()
    if white.is_mate():
        return {"cp": None, "mate": white.mate()}
    return {"cp": white.score(), "mate": None}


def cp_for_mover(entry: dict, turn: bool) -> float:
    """Collapse an eval to a single number from the mover's perspective.
    Mates are clamped so arithmetic stays sane."""
    if entry["mate"] is not None:
        val = 10000 - abs(entry["mate"]) * 10
        val = val if entry["mate"] > 0 else -val
    else:
        val = entry["cp"]
    return val if turn == chess.WHITE else -val


def analyze_game(game: chess.pgn.Game, engine: chess.engine.SimpleEngine,
                 nodes: int, multipv: int, skip_plies: int) -> dict:
    board = game.board()
    limit = chess.engine.Limit(nodes=nodes)
    plies = []

    moves = list(game.mainline_moves())
    for ply, move in enumerate(moves):
        san = board.san(move)

        if ply < skip_plies:
            board.push(move)
            continue

        info = engine.analyse(board, limit, multipv=multipv)
        if isinstance(info, dict):
            info = [info]

        lines = []
        for pv_entry in info:
            pv_moves = pv_entry.get("pv", [])
            lines.append({
                "rank": pv_entry.get("multipv", 1),
                "move_san": board.san(pv_moves[0]) if pv_moves else None,
                "eval": score_to_dict(pv_entry["score"]),
                "pv_san": board.variation_san(pv_moves[:8]) if pv_moves else None,
            })

        turn = board.turn
        best = lines[0]
        best_cp = cp_for_mover(best["eval"], turn)
        second_cp = cp_for_mover(lines[1]["eval"], turn) if len(lines) > 1 else None

        mat_before = material_balance(board)
        board.push(move)
        mat_after = material_balance(board)

        # eval after the move actually played, from the same POV
        after_info = engine.analyse(board, limit)
        played_cp = cp_for_mover(score_to_dict(after_info["score"]), turn)

        loss = max(0, best_cp - played_cp)
        mat_delta = (mat_before - mat_after) if turn == chess.WHITE else (mat_after - mat_before)

        tags = []
        if loss >= BLUNDER:
            tags.append("blunder")
        elif loss >= MISTAKE:
            tags.append("mistake")
        elif loss >= INACCURACY:
            tags.append("inaccuracy")
        if second_cp is not None and (best_cp - second_cp) >= ONLY_MOVE_GAP:
            tags.append("only-move")
        if mat_delta >= SAC_MATERIAL and played_cp >= SAC_HOLDS:
            tags.append("sacrifice")
        if best["move_san"] == san and "only-move" in tags:
            tags.append("found-the-only-move")

        plies.append({
            "ply": ply,
            "move_number": ply // 2 + 1,
            "side": "white" if turn == chess.WHITE else "black",
            "played_san": san,
            "played_eval_cp_mover": played_cp,
            "best_san": best["move_san"],
            "best_eval_cp_mover": best_cp,
            "centipawn_loss": loss,
            "material_delta": mat_delta,
            "lines": lines,
            "tags": tags,
        })

    return {"plies": plies}


def _binary_sha256(engine_path: str) -> str | None:
    """Hash the engine binary. Goes into every artifact: an evaluation is only
    meaningful alongside the exact binary that produced it."""
    path = shutil.which(engine_path) or engine_path
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="./corpus", type=Path)
    ap.add_argument("--engine", default="stockfish")
    ap.add_argument("--nodes", type=int, default=2_000_000)
    ap.add_argument("--multipv", type=int, default=3)
    ap.add_argument("--hash", type=int, default=256)
    ap.add_argument("--skip-plies", type=int, default=6,
                    help="don't burn nodes on the forced opening moves")
    ap.add_argument("--only", help="restrict to a node_id prefix")
    ap.add_argument("--limit", type=int, help="max games this run")
    ap.add_argument("--out-dir", type=Path,
                    help="write analysis here instead of corpus/analysis "
                         "(use for determinism checks; index is not updated)")
    ap.add_argument("--force", action="store_true",
                    help="re-analyze games already marked analyzed")
    ap.add_argument("--game", action="append", default=[],
                    help="analyze specific game_uid(s); repeatable")
    args = ap.parse_args()

    index_path = args.corpus / "index.json"
    index = json.loads(index_path.read_text())
    scratch = args.out_dir is not None
    out_dir = args.out_dir or (args.corpus / "analysis")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.game:
        wanted = set(args.game)
        targets = [e for e in index if e["game_uid"] in wanted]
    else:
        targets = [e for e in index
                   if (args.force or not e["analyzed"]) and e.get("in_book")]
        if args.only:
            targets = [e for e in targets if e["node_id"].startswith(args.only)]
    if args.limit:
        targets = targets[:args.limit]

    engine_sha = _binary_sha256(args.engine)
    engine = chess.engine.SimpleEngine.popen_uci(args.engine)
    engine.configure({"Threads": 1, "Hash": args.hash})
    engine_id = engine.id.get("name", "unknown")
    print(f"engine: {engine_id}  nodes={args.nodes}  multipv={args.multipv}",
          file=sys.stderr)

    try:
        for i, entry in enumerate(targets, 1):
            cid = entry["game_uid"]
            pgn_file = args.corpus / "games" / f"{cid}.pgn"
            game = chess.pgn.read_game(io.StringIO(pgn_file.read_text()))
            if game is None:
                continue

            print(f"[{i}/{len(targets)}] {cid}  {entry['node_id']}", file=sys.stderr)
            result = analyze_game(game, engine, args.nodes, args.multipv, args.skip_plies)

            payload = {
                "game_uid": cid,
                "node_id": entry["node_id"],
                "engine": engine_id,
                "engine_sha256": engine_sha,
                "params": {
                    "nodes": args.nodes,
                    "multipv": args.multipv,
                    "threads": 1,
                    "hash_mb": args.hash,
                    "skip_plies": args.skip_plies,
                },
                **result,
            }
            (out_dir / f"{cid}.json").write_text(
                json.dumps(payload, indent=2))
            # A scratch run must not mutate the corpus: it exists precisely
            # to be compared against the real one.
            if not scratch:
                entry["analyzed"] = True
                # checkpoint after every game — these runs are long
                index_path.write_text(json.dumps(index, indent=2))
    finally:
        engine.quit()

    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
