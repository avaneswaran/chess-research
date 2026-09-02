# chessbook — pipeline stage 1

Corpus ingest and engine analysis for the 3.Nf3 gambit complex
(Scotch / Göring / Danish-by-transposition), White side, AerialAttack.

**Untested against live APIs.** My build sandbox has no network egress, so
these scripts were written but not exercised. Expect one or two small fixes on
first run — most likely in chess.com's game-URL shape or Lichess NDJSON field
names.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Stockfish
#   macOS:  brew install stockfish
#   Debian: apt install stockfish
#   or build from source for a specific, pinnable version
stockfish --help >/dev/null && echo "engine ok"
```

## Run order

```bash
# 1. Pull and filter. Start with lichess only — it's faster and its export
#    already carries opening names, which is a useful cross-check on the
#    classifier.
python ingest.py --out ./corpus --platforms lichess

# 2. Read the distribution table it prints. That table is your table of
#    contents. Nodes with 40+ games are episodes; nodes with 3 are footnotes.

# 3. Analyze a slice first to calibrate depth vs. wall-clock before
#    committing to the full corpus.
python analyze.py --corpus ./corpus --only hub/nc6/goring --limit 20
```

At 2M nodes with two engine calls per ply, budget roughly 3–6 seconds per ply
on a modern core. A 40-move game is therefore a couple of minutes. A thousand
games is an overnight job — which is exactly why this belongs on your lab
hardware and not in a chat session.

## Why it's shaped this way

**PGN is immutable.** `corpus/games/*.pgn` is written once and never edited.
Every derived artifact keys off `canonical_id`. If the analysis is wrong, you
re-run analysis; you never touch the source game.

**Analysis is reproducible.** Node-limited, `Threads=1`, fixed hash, engine
version recorded in every output file. Time-limited search would give you
different numbers on every run, which is fine for a coffee-break review and
useless for a published book.

**The taxonomy lives in one file.** `classify.py` defines the node tree, and
node IDs are URL slugs. Appending nodes is free. Renaming one breaks every
published link, so treat the IDs as a contract from day one.

**Prose is not in here yet, deliberately.** Annotations will live in markdown
keyed by `canonical_id` + ply, so the analysis can be regenerated with a newer
Stockfish without touching a word you wrote.

## Secrets and provenance

`ingest.py` reads `LICHESS_TOKEN` from the environment. That's the seam, and
it's where Vault attaches. The story here is not "secrets for a static site" —
it's the ingest identity and the provenance of what gets published:

- The Lichess personal token lives in KV v2 and is fetched at runtime, rather
  than sitting in an environment variable on a laptop.
- The ingest job authenticates with a workload identity — AWS IAM auth — so no
  Vault-specific credential is created, delivered, stored, or rotated.
- Published analysis is signed by a transit key whose private half cannot leave
  Vault, so a published variation is checkable against the pipeline that
  produced it. Verification is offline and needs no credentials.
- Still ahead: dynamic credentials if the corpus moves from flat files to
  Postgres.

See `VAULT.md` for the layout, the policy split, and the honest limits of the
deployment.

## Open items

- `classified_ply` is how deep the *taxonomy* resolved, not where you left
  book. Real book-exit detection needs a reference DB; the Lichess masters
  explorer API is the cheap way in.
- Motif tagging (Bxf7+ deflection, e-file pin, Nd5 fork nets, the d5 break) is
  currently derived only from engine tags. Pattern detectors for the specific
  motifs in this complex are a stage-2 job, and they're what make the site
  browsable by idea rather than by move order.
- Opponent naming. Publishing online opponents' handles alongside "here is how
  I crushed them" is worth a decision now rather than at episode 30.
