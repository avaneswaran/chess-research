"""
classify.py — taxonomy spine for the 3.Nf3 gambit complex.

POSITION-BASED, NOT MOVE-ORDER-BASED.

The first version of this file matched literal SAN sequences and required
1.e4 e5 2.d4 exd4 as the opening four plies. That was wrong in a way that
silently destroyed data: the standard Goring move order is

    1.e4 e5 2.Nf3 Nc6 3.d4 exd4 4.c3

which reaches an identical position by a different route and was discarded
before classification ever ran. A repertoire whose whole identity is
transpositional cannot be classified by move order.

So: every node is defined by a canonical line, that line is played out at
import time, and the resulting POSITION (EPD) becomes the key. A game is
classified by walking its opening and looking up every position it passes
through. Move order becomes irrelevant, which is the entire point.

Node IDs are URL slugs. Append freely; never rename.
"""

from __future__ import annotations

import chess
import chess.pgn

MAX_PLIES = 24  # how deep to look for a transposition

# --- node definitions -------------------------------------------------------
# (node_id, display_name, canonical SAN line from the initial position)
# Lines are written in whichever move order is most natural; position-matching
# means the route taken here has no effect on what gets matched.

DEFINITIONS: list[tuple[str, str, str]] = [
    # --- the hub -------------------------------------------------------
    ("hub", "3.Nf3 hub — Scotch/Goring/Danish transpositional core",
     "e4 e5 d4 exd4 Nf3"),

    # Black's 3rd move alternatives (only reachable via the 2.d4 route;
    # after 2.Nf3 Black has already committed to something)
    ("hub/c5", "3...c5 — the pawn-holding wedge", "e4 e5 d4 exd4 Nf3 c5"),
    ("hub/d5", "3...d5 — central counter-strike", "e4 e5 d4 exd4 Nf3 d5"),
    ("hub/d6", "3...d6", "e4 e5 d4 exd4 Nf3 d6"),
    ("hub/bc5", "3...Bc5", "e4 e5 d4 exd4 Nf3 Bc5"),
    ("hub/bb4-check", "3...Bb4+", "e4 e5 d4 exd4 Nf3 Bb4+"),
    ("hub/nf6", "3...Nf6 — the developing decline", "e4 e5 d4 exd4 Nf3 Nf6"),
    ("hub/ne7", "3...Ne7", "e4 e5 d4 exd4 Nf3 Ne7"),
    ("hub/qe7", "3...Qe7", "e4 e5 d4 exd4 Nf3 Qe7"),
    ("hub/qf6", "3...Qf6", "e4 e5 d4 exd4 Nf3 Qf6"),
    ("hub/bd6", "3...Bd6", "e4 e5 d4 exd4 Nf3 Bd6"),
    ("hub/g6", "3...g6", "e4 e5 d4 exd4 Nf3 g6"),
    ("hub/c6", "3...c6", "e4 e5 d4 exd4 Nf3 c6"),
    ("hub/f6", "3...f6", "e4 e5 d4 exd4 Nf3 f6"),
    ("hub/b5", "3...b5", "e4 e5 d4 exd4 Nf3 b5"),
    ("hub/a6", "3...a6", "e4 e5 d4 exd4 Nf3 a6"),
    ("hub/h6", "3...h6", "e4 e5 d4 exd4 Nf3 h6"),
    ("hub/nh6", "3...Nh6", "e4 e5 d4 exd4 Nf3 Nh6"),
    ("hub/be7", "3...Be7", "e4 e5 d4 exd4 Nf3 Be7"),
    ("hub/qg5", "3...Qg5", "e4 e5 d4 exd4 Nf3 Qg5"),
    ("hub/d3", "3...d3 — immediate return", "e4 e5 d4 exd4 Nf3 d3"),

    # --- the main crossroads --------------------------------------------
    # Reachable via BOTH 2.d4 exd4 3.Nf3 Nc6 and 2.Nf3 Nc6 3.d4 exd4.
    # Identical position; position-matching catches both automatically.
    ("hub/nc6", "The main crossroads (Nc6 and Nf3, pawn on d4)",
     "e4 e5 d4 exd4 Nf3 Nc6"),

    # --- Goring Gambit ---------------------------------------------------
    ("hub/nc6/goring", "Goring Gambit (c3)", "e4 e5 Nf3 Nc6 d4 exd4 c3"),
    ("hub/nc6/goring/accepted", "Goring Accepted (...dxc3)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3"),
    ("hub/nc6/goring/accepted/nxc3", "One-pawn Goring (Nxc3)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Nxc3"),
    ("hub/nc6/goring/accepted/bc4", "Two-pawn Danish via Nf3 (Bc4)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4"),
    ("hub/nc6/goring/accepted/bc4/cxb2", "Two-pawn accepted (...cxb2)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 cxb2"),
    ("hub/nc6/goring/accepted/bc4/cxb2/bxb2", "Full Danish structure (Bxb2)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 cxb2 Bxb2"),
    ("hub/nc6/goring/accepted/bc4/d6", "Two-pawn met by ...d6",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 d6"),
    ("hub/nc6/goring/accepted/bc4/nf6", "Two-pawn met by ...Nf6",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 Nf6"),
    ("hub/nc6/goring/accepted/bc4/be7", "Two-pawn met by ...Be7",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 Be7"),
    ("hub/nc6/goring/accepted/bc4/bb4", "Two-pawn met by ...Bb4",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 Bb4"),
    ("hub/nc6/goring/accepted/bc4/d5", "Two-pawn met by ...d5",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 dxc3 Bc4 d5"),

    # Goring declined
    ("hub/nc6/goring/declined-d5", "Goring Declined 4...d5 (critical test)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 d5"),
    ("hub/nc6/goring/declined-d3", "Goring Declined 4...d3 (the returner)",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 d3"),
    ("hub/nc6/goring/declined-nf6", "Goring Declined 4...Nf6",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 Nf6"),
    ("hub/nc6/goring/declined-qe7", "Goring Declined 4...Qe7",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 Qe7"),
    ("hub/nc6/goring/declined-bc5", "Goring Declined 4...Bc5",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 Bc5"),
    ("hub/nc6/goring/declined-nge7", "Goring Declined 4...Nge7",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 Nge7"),
    ("hub/nc6/goring/declined-bb4", "Goring Declined 4...Bb4",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 Bb4"),
    ("hub/nc6/goring/declined-d6", "Goring Declined 4...d6",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 d6"),
    ("hub/nc6/goring/declined-qf6", "Goring Declined 4...Qf6",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 Qf6"),
    ("hub/nc6/goring/declined-f5", "Goring Declined 4...f5",
     "e4 e5 Nf3 Nc6 d4 exd4 c3 f5"),

    # --- Scotch Gambit ---------------------------------------------------
    ("hub/nc6/scotch-gambit", "Scotch Gambit (Bc4)",
     "e4 e5 Nf3 Nc6 d4 exd4 Bc4"),
    ("hub/nc6/scotch-gambit/nf6", "Scotch Gambit ...Nf6 (Max Lange territory)",
     "e4 e5 Nf3 Nc6 d4 exd4 Bc4 Nf6"),
    ("hub/nc6/scotch-gambit/bc5", "Scotch Gambit ...Bc5",
     "e4 e5 Nf3 Nc6 d4 exd4 Bc4 Bc5"),
    ("hub/nc6/scotch-gambit/be7", "Scotch Gambit ...Be7",
     "e4 e5 Nf3 Nc6 d4 exd4 Bc4 Be7"),
    ("hub/nc6/scotch-gambit/d6", "Scotch Gambit ...d6",
     "e4 e5 Nf3 Nc6 d4 exd4 Bc4 d6"),

    # --- Scotch proper ---------------------------------------------------
    ("hub/nc6/scotch", "Scotch Game proper (Nxd4)",
     "e4 e5 Nf3 Nc6 d4 exd4 Nxd4"),
    ("hub/nc6/bb5", "Bb5 sideline", "e4 e5 Nf3 Nc6 d4 exd4 Bb5"),

    # --- out of scope, tracked for comparison ----------------------------
    ("danish/classical", "Classical Danish (3.c3)", "e4 e5 d4 exd4 c3"),
    ("danish/classical/accepted", "Classical Danish accepted",
     "e4 e5 d4 exd4 c3 dxc3"),
    ("danish/classical/two-pawn", "Classical Danish two-pawn (4.Bc4)",
     "e4 e5 d4 exd4 c3 dxc3 Bc4"),
    ("danish/classical/declined-d5", "Classical Danish 3...d5",
     "e4 e5 d4 exd4 c3 d5"),
    ("centre-game", "Centre Game (3.Qxd4)", "e4 e5 d4 exd4 Qxd4"),
]


def _build_index() -> dict[str, dict]:
    """Play every definition out and key it by the resulting position."""
    index: dict[str, dict] = {}
    for node_id, name, line in DEFINITIONS:
        board = chess.Board()
        moves = line.split()
        try:
            for san in moves:
                board.push_san(san)
        except ValueError as exc:  # a typo in a definition, not bad user data
            raise ValueError(f"bad definition for {node_id}: {line} ({exc})") from exc

        key = board.epd(en_passant="legal")
        depth = len(moves)
        # deeper definition wins if two lines land on the same position
        if key not in index or depth > index[key]["depth"]:
            index[key] = {"node_id": node_id, "name": name, "depth": depth}
    return index


POSITION_INDEX = _build_index()


def classify(game: chess.pgn.Game) -> dict:
    """
    Walk the game's opening and match every position against the tree.
    Deepest match wins; every match along the way becomes the breadcrumb path.

    Returns:
        in_scope        bool — inside the 3.Nf3 complex
        node_id         str  — URL slug of the deepest node reached
        node_name       str
        path            list — node_ids crossed, shallow to deep
        classified_ply  int  — plies played to reach the deepest match
        san             list — opening SAN, for gap analysis
    """
    board = game.board()
    san: list[str] = []
    matches: list[tuple[int, int, str, str]] = []  # (depth, ply, node_id, name)

    key = board.epd(en_passant="legal")
    if key in POSITION_INDEX:
        hit = POSITION_INDEX[key]
        matches.append((hit["depth"], 0, hit["node_id"], hit["name"]))

    for move in game.mainline_moves():
        if len(san) >= MAX_PLIES:
            break
        san.append(board.san(move))
        board.push(move)
        key = board.epd(en_passant="legal")
        if key in POSITION_INDEX:
            hit = POSITION_INDEX[key]
            matches.append((hit["depth"], len(san), hit["node_id"], hit["name"]))

    if not matches:
        return {
            "in_scope": False, "node_id": None, "node_name": None,
            "path": [], "classified_ply": 0, "san": san,
        }

    matches.sort()
    deepest = matches[-1]
    seen, path = set(), []
    for _, _, node_id, _ in matches:
        if node_id not in seen:
            seen.add(node_id)
            path.append(node_id)

    return {
        "in_scope": deepest[2].startswith("hub"),
        "node_id": deepest[2],
        "node_name": deepest[3],
        "path": path,
        "classified_ply": deepest[1],
        "san": san,
    }


def all_node_ids() -> list[str]:
    """Every node, for site index generation."""
    return sorted({node_id for node_id, _, _ in DEFINITIONS})
