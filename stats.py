#!/usr/bin/env python3
"""
stats.py — read the corpus and answer the questions that decide the book.

    python stats.py --corpus ./corpus --gaps      what the tree fails to classify
    python stats.py --corpus ./corpus --scoring   results by node and tier
    python stats.py --corpus ./corpus --all

Nothing here touches the corpus. Read-only.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

TIER_ORDER = [
    "otb_classical", "otb_rapid", "otb_blitz",
    "online_classical", "online_rapid", "online_blitz", "online_bullet",
    "unknown",
]


def load(corpus: Path) -> list[dict]:
    return json.loads((corpus / "index.json").read_text())


def gaps(index: list[dict]) -> None:
    """For every game the tree could not fully resolve, show the move that
    stopped it. These are the branches missing from classify.py."""
    by_node: dict[str, Counter] = defaultdict(Counter)

    for e in index:
        if not e.get("in_book"):
            continue
        ply = e.get("classified_ply", 0)
        san = e.get("opening_san", [])
        if ply < len(san):
            by_node[e["node_id"]][san[ply]] += 1

    if not by_node:
        print("No unresolved games — the tree covers the whole corpus.")
        return

    total = sum(sum(c.values()) for c in by_node.values())
    print(f"UNRESOLVED: {total} games stop before a leaf\n")

    for node, counter in sorted(by_node.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(counter.values())
        print(f"{node}  ({n} games)")
        for move, count in counter.most_common(12):
            bar = "#" * min(40, count // 2 + 1)
            print(f"    {move:<10} {count:>4}  {bar}")
        print()


def scoring(index: list[dict]) -> None:
    """Score by node. A gambit that wins on time in bullet is not the same
    evidence as one that wins a rapid game, so tiers stay separate."""
    per_node: dict[str, Counter] = defaultdict(Counter)
    per_tier: dict[str, Counter] = defaultdict(Counter)

    for e in index:
        if not e.get("in_book"):
            continue
        res = e.get("result")
        outcome = {"1-0": "win", "0-1": "loss", "1/2-1/2": "draw"}.get(res, "other")
        per_node[e["node_id"]][outcome] += 1
        per_node[e["node_id"]]["n"] += 1
        per_tier[e.get("tier", "unknown")][outcome] += 1
        per_tier[e.get("tier", "unknown")]["n"] += 1

    def pct(c: Counter) -> float:
        n = c["n"] or 1
        return 100.0 * (c["win"] + 0.5 * c["draw"]) / n

    print(f"{'node':<45} {'n':>5} {'W':>5} {'D':>5} {'L':>5} {'score%':>7}")
    print("-" * 78)
    for node, c in sorted(per_node.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{node:<45} {c['n']:>5} {c['win']:>5} {c['draw']:>5} "
              f"{c['loss']:>5} {pct(c):>6.1f}%")

    print(f"\n{'tier':<45} {'n':>5} {'W':>5} {'D':>5} {'L':>5} {'score%':>7}")
    print("-" * 78)
    for tier in TIER_ORDER:
        if tier not in per_tier:
            continue
        c = per_tier[tier]
        print(f"{tier:<45} {c['n']:>5} {c['win']:>5} {c['draw']:>5} "
              f"{c['loss']:>5} {pct(c):>6.1f}%")

    print("\nNote: score% is (wins + half draws) / games. In blitz and bullet")
    print("this includes wins on time, which say nothing about the opening.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="./corpus", type=Path)
    ap.add_argument("--gaps", action="store_true")
    ap.add_argument("--scoring", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    index = load(args.corpus)
    print(f"corpus: {len(index)} games\n")

    if args.gaps or args.all:
        gaps(index)
    if args.scoring or args.all:
        scoring(index)
    if not any([args.gaps, args.scoring, args.all]):
        ap.print_help()


if __name__ == "__main__":
    main()
