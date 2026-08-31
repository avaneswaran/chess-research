#!/usr/bin/env python3
"""
verify_determinism.py — compare two analysis runs ply by ply.

The question this answers: does analysis produced by the container match
analysis produced on the laptop? If it does, the corpus can mix them freely
and the 10 calibration games stay valid. If it does not, everything must be
re-analyzed under one engine and we need to know that BEFORE 3300 games are
processed, not after a reader fails to reproduce a variation.

    python verify_determinism.py corpus/analysis /tmp/container-analysis

Exit code 0 means identical. Non-zero means divergence, and the first few
differences are printed with enough context to diagnose them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Fields that must match exactly for two runs to be considered equivalent.
PLY_FIELDS = [
    "played_san",
    "best_san",
    "best_eval_cp_mover",
    "played_eval_cp_mover",
    "centipawn_loss",
]


def load_dir(path: Path) -> dict[str, dict]:
    out = {}
    for f in path.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            print(f"  unreadable: {f}", file=sys.stderr)
            continue
        out[data.get("game_uid", f.stem)] = data
    return out


def compare(a: dict, b: dict, label_a: str, label_b: str,
            max_report: int) -> int:
    shared = sorted(set(a) & set(b))
    if not shared:
        print("No games in common — nothing to compare.")
        print(f"  {label_a}: {len(a)} games")
        print(f"  {label_b}: {len(b)} games")
        return 2

    print(f"comparing {len(shared)} game(s) present in both runs\n")

    # engine metadata first — a mismatch here explains any divergence below
    for uid in shared[:1]:
        for field in ("engine", "engine_sha256", "params"):
            va, vb = a[uid].get(field), b[uid].get(field)
            mark = "OK " if va == vb else "DIFF"
            print(f"  [{mark}] {field}")
            if va != vb:
                print(f"         {label_a}: {va}")
                print(f"         {label_b}: {vb}")
    print()

    divergences = 0
    for uid in shared:
        pa = {p["ply"]: p for p in a[uid].get("plies", [])}
        pb = {p["ply"]: p for p in b[uid].get("plies", [])}

        if set(pa) != set(pb):
            print(f"{uid}: ply coverage differs "
                  f"({len(pa)} vs {len(pb)} plies analyzed)")
            divergences += 1
            continue

        for ply in sorted(pa):
            diffs = [(f, pa[ply].get(f), pb[ply].get(f))
                     for f in PLY_FIELDS
                     if pa[ply].get(f) != pb[ply].get(f)]
            if diffs:
                divergences += 1
                if divergences <= max_report:
                    mv = pa[ply].get("move_number")
                    side = pa[ply].get("side")
                    print(f"{uid}  ply {ply} (move {mv}, {side})")
                    for f, va, vb in diffs:
                        print(f"    {f:<24} {label_a}={va!r}  {label_b}={vb!r}")
                    print()

    if divergences == 0:
        print("IDENTICAL — the two runs agree on every compared field.")
        print("The container and the laptop produce the same analysis; "
              "results from both can live in one corpus.")
        return 0

    print(f"DIVERGENT — {divergences} differing ply/game record(s).")
    if divergences > max_report:
        print(f"(showing first {max_report})")
    print("\nDo not mix these runs. Pick one engine build, record its hash, "
          "and re-analyze everything under it.")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir_a", type=Path)
    ap.add_argument("dir_b", type=Path)
    ap.add_argument("--max-report", type=int, default=10)
    args = ap.parse_args()

    a, b = load_dir(args.dir_a), load_dir(args.dir_b)
    sys.exit(compare(a, b, args.dir_a.name, args.dir_b.name, args.max_report))


if __name__ == "__main__":
    main()
