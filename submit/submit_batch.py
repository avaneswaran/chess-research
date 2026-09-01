#!/usr/bin/env python3
"""
submit_batch.py — turn corpus/index.json into an AWS Batch array job, and get
the results back.

Four subcommands, in the order you use them:

    sync      upload index.json and the PGNs to S3 (once; then incrementally)
    submit    plan shards, upload them, submit the array job
    status    aggregate the per-shard status records
    fetch     download a run's analysis, optionally merge into the local corpus

The shape of the thing:

    corpus/index.json  --filter-->  N games  --chunk-->  M shards
                                                          |
                                              one array job, M children
                                                          |
                                   runs/<run_id>/analysis/<game_uid>.json

WHY SHARDS AND NOT ONE CHILD PER GAME.
Batch spends tens of seconds per array child on placement, image pull, and
task startup. At roughly three minutes of engine time per game that overhead
is 15-20% if each child does one game, and under 1% if each child does
twenty-five. The worker still uploads per game (see worker/run_shard.py), so
sharding costs nothing in interruption resilience — a reclaimed shard resumes
from its last uploaded game.

WHY RUN IDS.
An analysis is only meaningful alongside the parameters that produced it. Two
runs at different --nodes are not interchangeable, so they do not share a
prefix, and the run id embeds a hash of the parameters so that two runs which
ARE comparable are visibly comparable.

CREDENTIALS.
Phase 1: whatever boto3's default chain finds — env vars, ~/.aws/credentials,
SSO. This script is the only piece of the system holding a long-lived key; the
workers get short-lived credentials from the Batch job role. That asymmetry is
the argument for phase 2, not an accident of this one.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# AWS Batch will not accept an array job with fewer than 2 children, and caps
# them at 10000. Both edges are real and both are hit by plausible inputs: a
# 20-game calibration slice at --shard-size 25 is one shard, and 3300 games at
# --shard-size 1 would be over the cap.
ARRAY_MIN = 2
ARRAY_MAX = 10_000

DEFAULT_PARAMS = {
    "nodes": 2_000_000,
    "multipv": 3,
    "hash": 256,
    "skip_plies": 6,
}


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


def s3_client():
    return boto3.client(
        "s3", config=Config(retries={"max_attempts": 10, "mode": "standard"})
    )


def load_index(corpus: Path) -> list[dict]:
    path = corpus / "index.json"
    if not path.exists():
        log(f"error: {path} not found")
        sys.exit(1)
    return json.loads(path.read_text())


def list_keys(s3, bucket: str, prefix: str) -> set[str]:
    """Every key under a prefix, as a set. One paginated list beats thousands
    of HEADs when deciding what still needs uploading."""
    keys: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


# --- sync -------------------------------------------------------------------

def cmd_sync(args) -> int:
    corpus = args.corpus
    s3 = s3_client()
    index = load_index(corpus)

    games_dir = corpus / "games"
    local = sorted(p for p in games_dir.glob("*.pgn"))
    log(f"local PGNs: {len(local)}")

    existing = list_keys(s3, args.bucket, "corpus/games/")
    log(f"already in s3: {len(existing)}")

    # PGNs are immutable by contract (see the README), so presence is
    # sufficient — there is no need to compare etags or mtimes. If a PGN ever
    # legitimately changes, the game_uid changes with it, because the uid is
    # content-addressed.
    todo = [p for p in local if f"corpus/games/{p.name}" not in existing]
    log(f"to upload: {len(todo)}")

    if args.dry_run:
        for p in todo[:10]:
            log(f"  would upload {p.name}")
        if len(todo) > 10:
            log(f"  ... and {len(todo) - 10} more")
        return 0

    failures = []

    def upload(p: Path) -> None:
        s3.upload_file(
            str(p), args.bucket, f"corpus/games/{p.name}",
            ExtraArgs={"ContentType": "application/x-chess-pgn"},
        )

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(upload, p): p for p in todo}
            for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                p = futures[fut]
                try:
                    fut.result()
                except (ClientError, BotoCoreError) as exc:
                    failures.append((p.name, str(exc)))
                if n % 250 == 0 or n == len(todo):
                    log(f"  {n}/{len(todo)}")

    # The full index goes up too. The workers do not read it — their shards
    # carry the entries they need — but a run is not self-describing without
    # it, and phase 2's site build will want it.
    s3.put_object(
        Bucket=args.bucket, Key="corpus/index.json",
        Body=json.dumps(index).encode(), ContentType="application/json",
    )
    log(f"uploaded corpus/index.json ({len(index)} entries)")

    if failures:
        log(f"\n{len(failures)} upload(s) failed:")
        for name, exc in failures[:10]:
            log(f"  {name}: {exc}")
        return 1

    log("sync complete")
    return 0


# --- submit -----------------------------------------------------------------

def select_games(index: list[dict], args) -> list[dict]:
    """Filter and order the games this run will analyze.

    Sorted by game_uid, always. Shard assignment has to be reproducible: if
    you re-submit the same selection you want the same games in the same
    shards, so that a partially-complete run resumes rather than reshuffles.
    Dict order out of json.loads is stable, but it is stable by accident of
    ingest order, which is not a property to build on.
    """
    # Explicit uids bypass every filter, exactly as analyze.py --game does.
    # This is the calibration path: "re-run precisely these games in the
    # cloud so I can diff them against the local run", which node-prefix
    # filtering cannot express — a prefix plus --limit picks the first N by
    # uid, which is not the same set.
    if args.game:
        wanted = set(args.game)
        found = [e for e in index if e["game_uid"] in wanted]
        missing = wanted - {e["game_uid"] for e in found}
        if missing:
            log(f"warning: {len(missing)} game_uid(s) not in the index: "
                + ", ".join(sorted(missing)[:5]))
        found.sort(key=lambda e: e["game_uid"])
        return found

    games = [e for e in index if e.get("in_book")]

    if not args.force:
        games = [e for e in games if not e.get("analyzed")]

    if args.only:
        games = [e for e in games if e.get("node_id", "").startswith(args.only)]

    games.sort(key=lambda e: e["game_uid"])

    if args.limit:
        games = games[: args.limit]

    return games


def make_run_id(params: dict, explicit: str | None) -> str:
    if explicit:
        return explicit
    # Timestamp for ordering, parameter digest so that two runs you can
    # legitimately compare are visibly the same shape at a glance.
    digest = hashlib.sha256(
        json.dumps(params, sort_keys=True).encode()
    ).hexdigest()[:8]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}"


def cmd_submit(args) -> int:
    corpus = args.corpus
    index = load_index(corpus)
    games = select_games(index, args)

    if not games:
        log("nothing to analyze — every matching game is already marked analyzed")
        log("(pass --force to re-analyze, or widen --only)")
        return 0

    params = dict(DEFAULT_PARAMS)
    for key in params:
        val = getattr(args, key, None)
        if val is not None:
            params[key] = val

    run_id = make_run_id(params, args.run_id)

    shards = [
        games[i : i + args.shard_size]
        for i in range(0, len(games), args.shard_size)
    ]

    # --- plan ---------------------------------------------------------------
    log("")
    log(f"run id       : {run_id}")
    log(f"games        : {len(games)}")
    log(f"shard size   : {args.shard_size}")
    log(f"shards       : {len(shards)}")
    log(f"params       : {params}")
    log(f"queue        : {args.queue}")
    log(f"job def      : {args.job_definition}")

    by_node: dict[str, int] = {}
    for g in games:
        by_node[g.get("node_id", "?")] = by_node.get(g.get("node_id", "?"), 0) + 1
    log("")
    log("top nodes in this run:")
    for node, count in sorted(by_node.items(), key=lambda kv: -kv[1])[:10]:
        log(f"  {count:5d}  {node}")
    if len(by_node) > 10:
        log(f"  ... and {len(by_node) - 10} more nodes")

    per_shard_min = args.shard_size * args.est_minutes_per_game
    log("")
    log(f"rough wall clock per shard: ~{per_shard_min:.0f} min "
        f"at {args.est_minutes_per_game} min/game")
    log("(measure it on a small --limit run before trusting it — the estimate")
    log(" is a placeholder, not a measurement)")

    if len(shards) > ARRAY_MAX:
        log("")
        log(f"error: {len(shards)} shards exceeds the Batch array cap of {ARRAY_MAX}")
        log(f"       raise --shard-size to at least {-(-len(games) // ARRAY_MAX)}")
        return 1

    if args.dry_run:
        log("")
        log("--dry-run: nothing uploaded, nothing submitted")
        return 0

    # --- upload shards ------------------------------------------------------
    s3 = s3_client()

    manifest = {
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "submitted_by": os.environ.get("USER", "unknown"),
        "params": params,
        "shard_size": args.shard_size,
        "shard_count": len(shards),
        "game_count": len(games),
        "selection": {
            "only": args.only,
            "limit": args.limit,
            "force": args.force,
        },
        "queue": args.queue,
        "job_definition": args.job_definition,
    }
    s3.put_object(
        Bucket=args.bucket, Key=f"runs/{run_id}/manifest.json",
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )

    def put_shard(i_shard):
        i, shard = i_shard
        payload = {
            "run_id": run_id,
            "shard_index": i,
            "shard_count": len(shards),
            "params": params,
            # Full index entries, not just uids: this is what lets the worker
            # skip downloading the 4 MB index on every one of N children.
            "games": shard,
        }
        s3.put_object(
            Bucket=args.bucket, Key=f"runs/{run_id}/shards/{i:04d}.json",
            Body=json.dumps(payload).encode(),
            ContentType="application/json",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(put_shard, enumerate(shards)))
    log(f"\nuploaded {len(shards)} shard manifest(s) to s3://{args.bucket}/runs/{run_id}/shards/")

    # --- submit -------------------------------------------------------------
    batch = boto3.client("batch")

    job_kwargs = {
        "jobName": f"{args.job_name_prefix}-{run_id}",
        "jobQueue": args.queue,
        "jobDefinition": args.job_definition,
        "containerOverrides": {
            "environment": [
                {"name": "CHESSBOOK_BUCKET", "value": args.bucket},
                {"name": "CHESSBOOK_RUN_ID", "value": run_id},
            ]
        },
    }

    if len(shards) >= ARRAY_MIN:
        job_kwargs["arrayProperties"] = {"size": len(shards)}
    else:
        # Batch rejects an array of size 1. A single-shard run is submitted as
        # an ordinary job instead, with the index the worker would otherwise
        # have read from AWS_BATCH_JOB_ARRAY_INDEX passed explicitly. This is
        # the common case for a calibration slice, so it is worth handling
        # rather than telling you to pick a smaller --shard-size.
        log("single shard: submitting as a non-array job")
        job_kwargs["containerOverrides"]["environment"].append(
            {"name": "CHESSBOOK_SHARD_INDEX", "value": "0"}
        )

    try:
        resp = batch.submit_job(**job_kwargs)
    except (ClientError, BotoCoreError) as exc:
        log(f"\nsubmit failed: {exc}")
        return 1

    job_id = resp["jobId"]

    # Record the job id next to the run so `status` can find it without you
    # having to keep the terminal scrollback.
    s3.put_object(
        Bucket=args.bucket, Key=f"runs/{run_id}/job.json",
        Body=json.dumps({
            "job_id": job_id,
            "job_name": resp["jobName"],
            "queue": args.queue,
            "job_definition": args.job_definition,
            "array_size": len(shards) if len(shards) >= ARRAY_MIN else None,
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2).encode(),
        ContentType="application/json",
    )

    log("")
    log(f"submitted: {job_id}")
    log("")
    log(f"  python submit/submit_batch.py status --bucket {args.bucket} --run-id {run_id}")
    log(f"  aws batch describe-jobs --jobs {job_id}")
    log("")
    print(run_id)  # stdout: the one machine-readable thing this prints
    return 0


# --- status -----------------------------------------------------------------

def cmd_status(args) -> int:
    s3 = s3_client()
    prefix = f"runs/{args.run_id}/"

    try:
        manifest = json.loads(
            s3.get_object(Bucket=args.bucket, Key=f"{prefix}manifest.json")["Body"].read()
        )
    except (ClientError, BotoCoreError) as exc:
        log(f"error: cannot read run manifest: {exc}")
        return 1

    analysis = list_keys(s3, args.bucket, f"{prefix}analysis/")
    statuses = list_keys(s3, args.bucket, f"{prefix}status/")

    log("")
    log(f"run          : {args.run_id}")
    log(f"created      : {manifest.get('created_at')}")
    log(f"params       : {manifest.get('params')}")
    log(f"games        : {manifest.get('game_count')}")
    log(f"shards       : {manifest.get('shard_count')}")
    log("")
    log(f"analysed     : {len(analysis)} / {manifest.get('game_count')}")
    log(f"shards done  : {len(statuses)} / {manifest.get('shard_count')}")

    # Per-shard detail comes from the status records the workers wrote. This
    # is the part CloudWatch retention eventually takes away, which is why the
    # worker writes it to S3 as well.
    failures = []
    early = 0
    for key in sorted(statuses):
        try:
            rec = json.loads(s3.get_object(Bucket=args.bucket, Key=key)["Body"].read())
        except (ClientError, BotoCoreError):
            continue
        if rec.get("terminated_early"):
            early += 1
        for r in rec.get("results", []):
            if r.get("status") != "ok":
                failures.append((rec["shard_index"], r))

    if early:
        log(f"reclaimed    : {early} shard(s) hit SIGTERM and will be retried")

    if failures:
        log("")
        log(f"{len(failures)} game-level failure(s):")
        for shard_index, r in failures[:20]:
            log(f"  shard {shard_index:04d}  {r['game_uid']}  {r.get('status')}/{r.get('stage')}")
            detail = (r.get("detail") or "").strip().splitlines()
            if detail:
                log(f"      {detail[-1][:160]}")
        if len(failures) > 20:
            log(f"  ... and {len(failures) - 20} more")

    if args.job:
        try:
            batch = boto3.client("batch")
            job = json.loads(
                s3.get_object(Bucket=args.bucket, Key=f"{prefix}job.json")["Body"].read()
            )
            desc = batch.describe_jobs(jobs=[job["job_id"]])["jobs"]
            if desc:
                d = desc[0]
                log("")
                log(f"batch job    : {d['jobId']}  status={d['status']}")
                summary = d.get("arrayProperties", {}).get("statusSummary")
                if summary:
                    log(f"array        : {summary}")
        except (ClientError, BotoCoreError, KeyError) as exc:
            log(f"(could not describe the Batch job: {exc})")

    log("")
    return 0


# --- fetch ------------------------------------------------------------------

def cmd_fetch(args) -> int:
    s3 = s3_client()
    prefix = f"runs/{args.run_id}/analysis/"
    keys = sorted(list_keys(s3, args.bucket, prefix))

    if not keys:
        log(f"no analysis objects under s3://{args.bucket}/{prefix}")
        return 1

    dest = args.dest
    dest.mkdir(parents=True, exist_ok=True)
    log(f"downloading {len(keys)} file(s) to {dest}")

    def download(key: str) -> None:
        s3.download_file(args.bucket, key, str(dest / key.rsplit("/", 1)[-1]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for n, _ in enumerate(pool.map(download, keys), 1):
            if n % 250 == 0 or n == len(keys):
                log(f"  {n}/{len(keys)}")

    if not args.merge:
        log("")
        log("not merged into the corpus. Verify first:")
        log(f"  python verify_determinism.py corpus/analysis {dest}")
        log("then re-run with --merge.")
        return 0

    # --- merge --------------------------------------------------------------
    #
    # Deliberately a separate, explicit step. Merging marks games analyzed in
    # index.json, and index.json is the thing every later stage trusts. It
    # should not happen as a side effect of a download you ran to look at.
    corpus = args.corpus
    index_path = corpus / "index.json"
    index = json.loads(index_path.read_text())
    analysis_dir = corpus / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    by_uid = {e["game_uid"]: e for e in index}
    merged = 0
    unknown = 0

    for f in sorted(dest.glob("*.json")):
        uid = f.stem
        entry = by_uid.get(uid)
        if entry is None:
            unknown += 1
            continue
        (analysis_dir / f.name).write_bytes(f.read_bytes())
        entry["analyzed"] = True
        merged += 1

    # Write via a temp file and replace. A half-written index.json is a much
    # worse outcome than a failed merge, and this is a 4 MB write.
    tmp = index_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(index_path)

    log("")
    log(f"merged {merged} analysis file(s) into {analysis_dir}")
    if unknown:
        log(f"WARNING: {unknown} file(s) had no matching game_uid in index.json — not merged")
    log(f"index.json updated: {sum(1 for e in index if e.get('analyzed'))} games now marked analyzed")
    return 0


# --- cli --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Submit corpus analysis to AWS Batch as an array job.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, need_corpus=True):
        if need_corpus:
            p.add_argument("--corpus", type=Path, default=Path("./corpus"))
        p.add_argument("--bucket", required=True,
                       help="corpus bucket (terraform output: bucket)")
        p.add_argument("--concurrency", type=int, default=16,
                       help="parallel S3 transfers")

    p_sync = sub.add_parser("sync", help="upload PGNs and index.json to S3")
    common(p_sync)
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_sub = sub.add_parser("submit", help="plan shards and submit the array job")
    common(p_sub)
    p_sub.add_argument("--queue", required=True,
                       help="Batch job queue (terraform output: job_queue)")
    p_sub.add_argument("--job-definition", required=True,
                       help="Batch job definition (terraform output: job_definition)")
    p_sub.add_argument("--shard-size", type=int, default=25,
                       help="games per array child (default 25)")
    p_sub.add_argument("--only", help="restrict to a node_id prefix, e.g. hub/nc6/goring")
    p_sub.add_argument("--limit", type=int, help="cap the number of games")
    p_sub.add_argument("--force", action="store_true",
                       help="include games already marked analyzed")
    p_sub.add_argument("--game", action="append", default=[],
                       help="analyze specific game_uid(s); repeatable. "
                            "Bypasses --only/--limit/--force and the analyzed "
                            "flag, like analyze.py --game.")
    p_sub.add_argument("--run-id", help="override the generated run id")
    p_sub.add_argument("--job-name-prefix", default="chessbook-analyze")
    p_sub.add_argument("--dry-run", action="store_true",
                       help="print the shard plan and stop")
    p_sub.add_argument("--est-minutes-per-game", type=float, default=3.0,
                       help="only used for the wall-clock estimate in the plan")
    # Engine parameters. Left as None so DEFAULT_PARAMS wins unless you say
    # otherwise, and so the run id's parameter digest is stable across
    # invocations that did not actually change anything.
    p_sub.add_argument("--nodes", type=int)
    p_sub.add_argument("--multipv", type=int)
    p_sub.add_argument("--hash", type=int)
    p_sub.add_argument("--skip-plies", type=int, dest="skip_plies")
    p_sub.set_defaults(func=cmd_submit)

    p_stat = sub.add_parser("status", help="aggregate per-shard status records")
    common(p_stat, need_corpus=False)
    p_stat.add_argument("--run-id", required=True)
    p_stat.add_argument("--job", action="store_true",
                        help="also describe the Batch job itself")
    p_stat.set_defaults(func=cmd_status)

    p_fetch = sub.add_parser("fetch", help="download a run's analysis")
    common(p_fetch)
    p_fetch.add_argument("--run-id", required=True)
    p_fetch.add_argument("--dest", type=Path, required=True,
                         help="local directory to download into")
    p_fetch.add_argument("--merge", action="store_true",
                         help="copy into corpus/analysis and mark games analyzed")
    p_fetch.set_defaults(func=cmd_fetch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
