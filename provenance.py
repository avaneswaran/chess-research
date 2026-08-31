"""
provenance.py — identity, deduplication, and trust tiers.

The corpus is about to draw from four sources with very different guarantees:

    lichess API     canonical ID, trustworthy headers, machine-generated
    chess.com API   canonical ID, trustworthy headers, machine-generated
    lichess studies no ID, hand-entered headers, may carry YOUR annotations
    local disk      no ID, unknown origin, may not be your games at all

This module exists so those can be merged without the corpus quietly filling
up with duplicates or with games you never played.

Three ideas:

1. GAME_UID is content-addressed. Same game found in five places collapses to
   one entry with five source records. Nothing is counted twice.

2. MOVE_UID hashes only the moves. Two records with the same MOVE_UID but
   different GAME_UID are *candidate* duplicates — headers disagree. Those are
   flagged for human review, never auto-merged, because a sharp gambit really
   can produce the same 14-move miniature against two different opponents.

3. Anything that fails the identity check is QUARANTINED, not dropped and not
   included. Silent inclusion of a downloaded master database would corrupt
   every statistic in the book while looking completely normal.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import chess.pgn

# --- identity ---------------------------------------------------------------
# Every name you have ever appeared under. Add to this freely; it is the single
# most likely reason a real game gets quarantined by mistake.

DEFAULT_ALIASES = [
    "AerialAttack",
    "Vaneswaran, Anand",
    "Anand Vaneswaran",
    "Vaneswaran Anand",
    "Anand V",
    "V, Anand",
]


def load_aliases(corpus: Path) -> list[str]:
    """Aliases live in corpus/aliases.json so you can extend them without
    editing code. Falls back to the defaults on first run."""
    path = corpus / "aliases.json"
    if path.exists():
        return json.loads(path.read_text())
    corpus.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_ALIASES, indent=2))
    return list(DEFAULT_ALIASES)


def normalize_name(name: str) -> str:
    """Fold 'Vaneswaran, Anand' and 'anand vaneswaran' to the same token."""
    if not name:
        return ""
    n = name.strip().lower()
    n = re.sub(r"[^a-z0-9, ]", "", n)
    if "," in n:
        last, _, first = n.partition(",")
        n = f"{first.strip()} {last.strip()}"
    return " ".join(sorted(n.split()))


VS_SEPARATORS = (" vs. ", " vs ", " VS. ", " VS ", " v. ")


def identify_from_event(event: str, aliases: list[str]) -> str | None:
    """
    Recover identity from the Event tag when White/Black headers are empty.

    Lichess study exports put the CHAPTER TITLE in Event and frequently leave
    the player headers blank. Chapters titled like

        2022 US Teams Parsippany: Round 5: Anand Vaneswaran (1601) vs. Leo Wang (1066) 1-0

    carry everything we need, just not where PGN says it should be.

    Returns 'white', 'black', or None. None means "cannot tell" — which for
    opening-prep chapters is the correct answer, and they stay quarantined.
    """
    if not event:
        return None
    low = event.lower()

    # find where the alias appears
    pos = -1
    for alias in aliases:
        a = alias.lower()
        # skip surname-comma forms; Event text uses natural order
        if "," in a:
            a = " ".join(reversed([x.strip() for x in a.split(",")]))
        idx = low.find(a)
        if idx != -1 and (pos == -1 or idx < pos):
            pos = idx
    if pos == -1:
        return None

    # find the vs. separator
    sep = -1
    for token in VS_SEPARATORS:
        idx = event.find(token)
        if idx != -1 and (sep == -1 or idx < sep):
            sep = idx
    if sep == -1:
        # no opponent structure — a prep chapter, not a game
        return None

    return "white" if pos < sep else "black"


def looks_like_tournament(event: str) -> bool:
    """Heuristic for 'this Event names a real over-the-board tournament'."""
    if not event:
        return False
    low = event.lower()
    if any(t.lower() in low for t in VS_SEPARATORS):
        markers = ("round", "rd", "open", "congress", "class", "champ",
                   "tourney", "tournament", "teams", "monthly", "swiss")
        return any(m in low for m in markers)
    return False


def identify(game: chess.pgn.Game, aliases: list[str]) -> str:
    """Return 'white', 'black', or 'foreign'."""
    norm = {normalize_name(a) for a in aliases}
    white = normalize_name(game.headers.get("White", ""))
    black = normalize_name(game.headers.get("Black", ""))
    if white in norm:
        return "white"
    if black in norm:
        return "black"
    return "foreign"


# --- content addressing -----------------------------------------------------

def _mainline_san(game: chess.pgn.Game) -> str:
    board = game.board()
    out = []
    for move in game.mainline_moves():
        out.append(board.san(move))
        board.push(move)
    return " ".join(out)


def _norm_date(raw: str | None) -> str:
    if not raw:
        return "????.??.??"
    return raw.strip().replace("-", ".")


def compute_uids(game: chess.pgn.Game) -> tuple[str, str]:
    """Return (game_uid, move_uid)."""
    san = _mainline_san(game)
    move_uid = hashlib.sha1(san.encode()).hexdigest()[:16]

    parts = [
        san,
        game.headers.get("Result", "*"),
        normalize_name(game.headers.get("White", "")),
        normalize_name(game.headers.get("Black", "")),
        _norm_date(game.headers.get("UTCDate") or game.headers.get("Date")),
    ]
    game_uid = hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
    return game_uid, move_uid


# --- trust tiers ------------------------------------------------------------
# Evidential weight is not uniform. A 1+0 bullet win and a Marshall Monthly
# classical win are both real games, but they cannot appear in the same
# statistic without a label, and they should not carry the same weight in a
# claim like "this line scores well for White".

TIER_WEIGHTS = {
    "otb_classical": 1.00,
    "otb_rapid": 0.70,
    "otb_blitz": 0.45,
    "online_classical": 0.60,
    "online_rapid": 0.45,
    "online_blitz": 0.25,
    "online_bullet": 0.10,
    "unknown": 0.00,
}


def classify_tier(game: chess.pgn.Game, source_kind: str,
                  time_class: str | None) -> str:
    """
    source_kind: 'api' | 'study' | 'local'
    Studies and local files are treated as OTB *only* when the PGN actually
    looks like a tournament game. Guessing generously here would inflate the
    most heavily weighted tier, so the check is deliberately strict.
    """
    event = (game.headers.get("Event") or "").lower()
    site = (game.headers.get("Site") or "").lower()

    is_online_site = any(s in site for s in ("lichess.org", "chess.com"))
    online_event = any(s in event for s in ("rated", "casual", "arena", "hourly"))

    if source_kind == "api" or is_online_site or online_event:
        tc = (time_class or "").lower()
        if tc in ("classical", "daily", "correspondence"):
            return "online_classical"
        if tc == "rapid":
            return "online_rapid"
        if tc == "blitz":
            return "online_blitz"
        if tc == "bullet":
            return "online_bullet"
        return "unknown"

    # Hand-entered: infer from the PGN TimeControl tag, in seconds.
    raw_tc = (game.headers.get("TimeControl") or "").split("+")[0]
    try:
        base = int(raw_tc)
    except (ValueError, TypeError):
        base = None

    if base is None:
        # No time control at all is typical of manually entered tournament
        # games. Treat as classical only if there is a real Event name.
        has_event = bool(event) and event not in ("?", "-", "casual game")
        return "otb_classical" if has_event else "unknown"
    if base >= 1500:
        return "otb_classical"
    if base >= 600:
        return "otb_rapid"
    return "otb_blitz"


# --- merge ------------------------------------------------------------------

def merge_source(entry: dict, source: dict) -> dict:
    """Add a source record to an existing corpus entry without duplicating it."""
    sources = entry.setdefault("sources", [])
    key = (source.get("kind"), source.get("ref"))
    if not any((s.get("kind"), s.get("ref")) == key for s in sources):
        sources.append(source)

    # Prefer the highest-trust tier seen for this game.
    incoming = source.get("tier", "unknown")
    if TIER_WEIGHTS.get(incoming, 0) > TIER_WEIGHTS.get(entry.get("tier", "unknown"), 0):
        entry["tier"] = incoming

    # A hand-annotated copy is more valuable than a bare one — it is draft
    # prose for the book. Keep a pointer to the richest version.
    if source.get("has_annotations") and not entry.get("annotated_source"):
        entry["annotated_source"] = source.get("ref")
    return entry


def has_annotations(pgn_text: str) -> bool:
    """True only for HUMAN prose in comments.

    Naive brace-detection does not work: Lichess ships {[%eval 0.17]} when
    exported with evals=true, and chess.com always ships {[%clk 0:03:00]}.
    Those are machine metadata, present on essentially every online game, and
    counting them made this flag fire on 100% of the corpus.

    So: strip every bracketed %-command from each comment, and only call it an
    annotation if real text survives.
    """
    for comment in re.findall(r"\{([^}]*)\}", pgn_text):
        stripped = re.sub(r"\[%[a-zA-Z]+[^\]]*\]", "", comment).strip()
        if len(stripped) >= 4:
            return True
    return False


def read_games(pgn_text: str):
    """Yield every game in a possibly-multi-game PGN blob."""
    stream = io.StringIO(pgn_text)
    while True:
        try:
            game = chess.pgn.read_game(stream)
        except Exception:
            break
        if game is None:
            break
        yield game
