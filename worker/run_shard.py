#!/usr/bin/env python3
"""
run_shard.py — container entrypoint for one AWS Batch array child.

This is a thin harness around analyze.py. It deliberately does not reimplement
any part of the analysis: it stages inputs, shells out to analyze.py exactly as
you would on the laptop, and ships the resulting JSON to S3. If this file and
analyze.py ever disagree about what an analysis document contains, that is a
bug in this file.

    S3 layout it expects (written by submit/submit_batch.py):

      runs/<run_id>/shards/<NNNN>.json        this child's work
      corpus/games/<game_uid>.pgn             immutable source PGN
      runs/<run_id>/analysis/<game_uid>.json  <- written here
      runs/<run_id>/status/<NNNN>.json        <- written here

Three properties matter more than throughput:

1. ONE analyze.py PROCESS PER GAME. This is load-bearing twice over.

   Resilience: the result is uploaded before the next game starts, so a spot
   reclamation loses at most one game rather than the whole shard. Shards
   exist to amortize Batch's per-child scheduling overhead (tens of seconds),
   not engine startup.

   Correctness: analyze.py reuses one engine process for every game it is
   given, and python-chess only sends `ucinewgame` when the `game=` argument
   changes — which analyze.py never passes. A batch of games therefore shares
   one transposition table, and each game's evaluations depend on which games
   ran before it. A fresh process per game means an empty table per game,
   which makes cloud output independent of shard size and of where a retry
   resumed. Without that, changing --shard-size would change the numbers.

   See CLOUD.md, "The transposition table carries across games".

   The cost is an engine restart per game: about a second against minutes of
   search, under 1%. It is worth considerably more than that.

2. RESUME IS A LIST, NOT A GUESS.
   On start, the analysis prefix for this run is listed once and any game that
   already has output is skipped. A retried shard therefore costs only the
   games it had not finished. This is what makes retry-on-reclamation cheap
   enough to be the default policy.

3. THE SOURCE PGN IS NEVER WRITTEN.
   The job role has no PutObject on corpus/*, so this is enforced rather than
   promised, but the code does not try either.

Exit codes are load-bearing — batch.tf's retry_strategy reads them:

    0   every game in the shard has output in S3
    75  EX_TEMPFAIL: something failed that might not fail again (S3 error,
        engine died, SIGTERM from spot reclamation). Batch retries.
    1   the shard is malformed or the environment is wrong. Retrying a
        deterministic failure just buries the first clear log line.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

EX_OK = 0
EX_FATAL = 1
EX_TEMPFAIL = 75

ANALYZE = Path("/app/analyze.py")

# Set when SIGTERM arrives.
#
# The timing here is worth being precise about, because the obvious design is
# wrong. EC2 gives a two-minute spot interruption notice, but that budget is
# not ours: ECS drains the task and then waits only ECS_CONTAINER_STOP_TIMEOUT
# (30 seconds by default) before SIGKILL. A game takes minutes. So "finish the
# current game, then exit cleanly" is not an option that exists — waiting for
# it just guarantees we are killed mid-write with no status record.
#
# What we do instead: kill the engine immediately, write the status record,
# and exit 75 while we still can. The work already uploaded is safe, the
# in-flight game is lost, and Batch retries the shard — which resumes from the
# last uploaded game rather than the start.
_terminating = False
_child: subprocess.Popen | None = None


def _on_sigterm(signum, frame):  # noqa: ARG001
    global _terminating
    _terminating = True
    log("SIGTERM (spot reclamation?) — abandoning the current game, exiting 75")
    if _child is not None and _child.poll() is None:
        _child.terminate()


def log(msg: str) -> None:
    """Unbuffered, because this is going to CloudWatch through the awslogs
    driver and a buffered crash is a silent one."""
    print(f"[run_shard] {msg}", file=sys.stderr, flush=True)


def env_or_die(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log(f"FATAL: {name} is not set")
        sys.exit(EX_FATAL)
    return val


def main() -> int:
    global _child

    signal.signal(signal.SIGTERM, _on_sigterm)

    bucket = env_or_die("CHESSBOOK_BUCKET")
    run_id = env_or_die("CHESSBOOK_RUN_ID")

    # Batch sets this on every child of an array job. Its absence means either
    # this was submitted as a single job by mistake, or someone is running the
    # image by hand — CHESSBOOK_SHARD_INDEX is the manual override.
    raw_index = os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX") \
        or os.environ.get("CHESSBOOK_SHARD_INDEX")
    if raw_index is None:
        log("FATAL: no AWS_BATCH_JOB_ARRAY_INDEX; was this submitted as an array job?")
        return EX_FATAL
    shard_index = int(raw_index)

    engine = os.environ.get("STOCKFISH_PATH", "stockfish")

    # Retries here are for transient S3, not for the job. Batch's retry
    # strategy handles the job-level case and this should fail fast enough to
    # let it.
    s3 = boto3.client(
        "s3",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )

    shard_key = f"runs/{run_id}/shards/{shard_index:04d}.json"
    log(f"bucket={bucket} run={run_id} shard={shard_index:04d}")

    try:
        body = s3.get_object(Bucket=bucket, Key=shard_key)["Body"].read()
        shard = json.loads(body)
    except (ClientError, BotoCoreError) as exc:
        log(f"FATAL: cannot read shard manifest s3://{bucket}/{shard_key}: {exc}")
        # A missing manifest is a submitter bug, not a blip. Do not retry.
        return EX_FATAL
    except json.JSONDecodeError as exc:
        log(f"FATAL: shard manifest is not valid JSON: {exc}")
        return EX_FATAL

    games = shard.get("games", [])
    params = shard.get("params", {})
    if not games:
        log("shard is empty — nothing to do")
        return EX_OK

    log(f"{len(games)} game(s) in shard; params={params}")

    # --- resume: what already has output? -----------------------------------
    analysis_prefix = f"runs/{run_id}/analysis/"
    done: set[str] = set()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=analysis_prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if name.endswith(".json"):
                    done.add(name[: -len(".json")])
    except (ClientError, BotoCoreError) as exc:
        # Not fatal — worst case we redo work that is already done, and the
        # PUT is idempotent. Better than failing the shard outright.
        log(f"WARN: could not list {analysis_prefix} ({exc}); assuming nothing done")

    pending = [g for g in games if g["game_uid"] not in done]
    skipped = len(games) - len(pending)
    if skipped:
        log(f"{skipped} game(s) already have output in this run — skipping")

    # --- stage a minimal local corpus ---------------------------------------
    #
    # analyze.py wants a corpus directory: an index.json it can look game_uids
    # up in, and games/<uid>.pgn beside it. The shard manifest carries the
    # index entries it needs, so the 4 MB full index is never downloaded and
    # only this shard's PGNs are fetched.
    workdir = Path(tempfile.mkdtemp(prefix="chessbook-"))
    corpus = workdir / "corpus"
    (corpus / "games").mkdir(parents=True)
    outdir = workdir / "out"
    outdir.mkdir()
    (corpus / "index.json").write_text(json.dumps(games))

    results: list[dict] = []
    tempfail = False

    for i, entry in enumerate(pending, 1):
        uid = entry["game_uid"]

        if _terminating:
            log("stopping before next game due to SIGTERM")
            tempfail = True
            break

        started = time.time()
        record = {"game_uid": uid, "node_id": entry.get("node_id")}

        try:
            pgn_key = f"corpus/games/{uid}.pgn"
            pgn_path = corpus / "games" / f"{uid}.pgn"
            s3.download_file(bucket, pgn_key, str(pgn_path))
        except (ClientError, BotoCoreError) as exc:
            log(f"[{i}/{len(pending)}] {uid}: PGN download failed: {exc}")
            record.update(status="error", stage="download", detail=str(exc))
            results.append(record)
            tempfail = True
            continue

        cmd = [
            sys.executable, str(ANALYZE),
            "--corpus", str(corpus),
            "--engine", engine,
            "--out-dir", str(outdir),
            "--game", uid,
            "--nodes", str(params.get("nodes", 2_000_000)),
            "--multipv", str(params.get("multipv", 3)),
            "--hash", str(params.get("hash", 256)),
            "--skip-plies", str(params.get("skip_plies", 6)),
        ]

        log(f"[{i}/{len(pending)}] {uid} {entry.get('node_id', '')}")

        # Popen rather than run(), so the SIGTERM handler above has something
        # to terminate. capture_output would deadlock on a full pipe over a
        # multi-minute search, so stderr goes to a file we read afterwards.
        errfile = workdir / "analyze.err"
        with errfile.open("wb") as errfh:
            _child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errfh)
            returncode = _child.wait()
        _child = None
        stderr_tail = errfile.read_text(errors="replace").strip()

        if _terminating:
            log("  abandoned mid-search")
            record.update(status="interrupted", stage="analyze")
            results.append(record)
            break

        if returncode != 0:
            log(f"  analyze.py exited {returncode}")
            for line in stderr_tail.splitlines()[-15:]:
                log(f"  | {line}")
            record.update(status="error", stage="analyze",
                          returncode=returncode,
                          detail=stderr_tail[-2000:])
            results.append(record)
            tempfail = True
            continue

        produced = outdir / f"{uid}.json"
        if not produced.exists():
            # analyze.py returns 0 and writes nothing when read_game() gives
            # back None — an unparseable PGN. That is a corpus defect, and it
            # will recur on every retry, so it is recorded but not retried.
            log(f"  analyze.py wrote no output for {uid} (unparseable PGN?)")
            record.update(status="no-output", stage="analyze")
            results.append(record)
            continue

        try:
            s3.upload_file(
                str(produced), bucket, f"{analysis_prefix}{uid}.json",
                ExtraArgs={"ContentType": "application/json"},
            )
        except (ClientError, BotoCoreError) as exc:
            log(f"  upload failed: {exc}")
            record.update(status="error", stage="upload", detail=str(exc))
            results.append(record)
            tempfail = True
            continue

        elapsed = round(time.time() - started, 1)
        produced.unlink()  # keep /tmp bounded on a long shard
        log(f"  ok ({elapsed}s)")
        record.update(status="ok", seconds=elapsed)
        results.append(record)

    # --- status record ------------------------------------------------------
    #
    # Written even on failure. It is the only per-shard artifact that outlives
    # the container's log retention, and `submit_batch.py status` reads it.
    ok = sum(1 for r in results if r["status"] == "ok")
    status = {
        "run_id": run_id,
        "shard_index": shard_index,
        "job_id": os.environ.get("AWS_BATCH_JOB_ID"),
        "attempt": os.environ.get("AWS_BATCH_JOB_ATTEMPT"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "games_in_shard": len(games),
        "already_done": skipped,
        "analyzed_now": ok,
        "terminated_early": _terminating,
        "results": results,
    }
    try:
        s3.put_object(
            Bucket=bucket,
            Key=f"runs/{run_id}/status/{shard_index:04d}.json",
            Body=json.dumps(status, indent=2).encode(),
            ContentType="application/json",
        )
    except (ClientError, BotoCoreError) as exc:
        log(f"WARN: could not write status record: {exc}")

    failed = [r for r in results if r["status"] != "ok"]
    log(f"shard done: {ok} analyzed, {skipped} skipped, {len(failed)} failed")

    if _terminating or tempfail:
        return EX_TEMPFAIL
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
