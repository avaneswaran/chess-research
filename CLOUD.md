# chessbook — cloud analysis (phase 1)

Runs the analysis worker from `docker/Dockerfile` across the corpus on AWS
Batch spot instances, instead of one game at a time on a laptop overnight.

**Nothing here has been run against AWS.** The Terraform validates and the
Python compiles and was exercised against the local corpus, but no `apply` has
happened, no image has been pushed, and no job has been submitted. Expect the
usual first-run friction — most likely a spot vCPU quota that is lower than
you think, and the compute environment sitting at `INVALID` until it is
raised.

## What phase 1 builds

```
  corpus/index.json
        |
        |  submit_batch.py submit      filter -> sort -> chunk
        v
  s3://<bucket>/runs/<run_id>/shards/0000.json ... NNNN.json
        |
        |  one Batch array job, one child per shard
        v
  EC2 spot (SPOT_CAPACITY_OPTIMIZED, bmi2-capable families only)
        |
        |  run_shard.py -> analyze.py, once per game, upload after each
        v
  s3://<bucket>/runs/<run_id>/analysis/<game_uid>.json
        |
        |  submit_batch.py fetch --merge
        v
  corpus/analysis/  +  index.json marked analyzed
```

| file | what it is |
|---|---|
| `infra/` | S3, ECR, VPC, IAM, Batch. `terraform -chdir=infra` |
| `worker/run_shard.py` | container entrypoint for one array child |
| `submit/submit_batch.py` | `sync` / `submit` / `status` / `fetch` |
| `scripts/push_image.sh` | build, push, print the digest to pin |

## Run order

### Credentials: `aws login` needs a bridge

If you authenticate with `aws login` rather than a static key pair, the session
lands in `~/.aws/cli/cache/session.db`, a format only the v2 CLI understands.
**Terraform and boto3 cannot read it** — Terraform reports `No valid credential
sources found` after a slow probe of EC2 instance metadata, and boto3 raises
`MissingDependencyException: Using the login credential provider requires an
additional dependency`.

The bridge is a second profile that shells back out to the CLI:

```ini
# ~/.aws/config
[default]
region = us-east-1
output = json

[profile tf]
region = us-east-1
output = json
credential_process = /home/you/.local/bin/aws configure export-credentials --profile default --format process
```

`credential_process` re-invokes the CLI on every call, so it picks up the
automatically refreshed session instead of pinning a snapshot that expires
mid-run. It resolves against `[default]`, which has no `credential_process` of
its own — that is what stops it recursing.

Terraform picks it up via `aws_profile = "tf"` in `terraform.tfvars`. Every
`submit_batch.py` call needs `AWS_PROFILE=tf` exported, or boto3 fails on the
default profile. None of this applies with a static key pair — Terraform and
boto3 both read `~/.aws/credentials` natively.

### Steps

```bash
export AWS_PROFILE=tf

cd infra
cp terraform.tfvars.example terraform.tfvars   # edit region, scale
terraform init
terraform apply                                # ECR must exist before you push
cd ..

./scripts/push_image.sh "$(terraform -chdir=infra output -raw ecr_repository_url)" sf17.1
# paste the digest it prints into infra/terraform.tfvars as worker_image_digest
terraform -chdir=infra apply

BUCKET=$(terraform -chdir=infra output -raw bucket)
QUEUE=$(terraform -chdir=infra output -raw job_queue)
JOBDEF=$(terraform -chdir=infra output -raw job_definition)

python submit/submit_batch.py sync --corpus ./corpus --bucket "$BUCKET"
```

Then calibrate before committing to 3300 games — but **read the next section
first**, because the ten files currently in `corpus/analysis/` are not a valid
baseline to diff against.

```bash
RUN=$(python submit/submit_batch.py submit \
        --corpus ./corpus --bucket "$BUCKET" --queue "$QUEUE" --job-definition "$JOBDEF" \
        --only hub/nc6/goring/accepted/bc4/cxb2/bxb2 --limit 10 --force \
        --shard-size 5)

python submit/submit_batch.py status --bucket "$BUCKET" --run-id "$RUN" --job
python submit/submit_batch.py fetch  --bucket "$BUCKET" --run-id "$RUN" --dest /tmp/cloud

# Build a comparable local baseline: ONE analyze.py process per game, which is
# what the cloud worker does. Diffing against corpus/analysis/ instead will
# report a spurious divergence — see below.
mkdir -p /tmp/local-ref
for uid in $(python -c "
import json
idx = json.load(open('corpus/index.json'))
print(' '.join(e['game_uid'] for e in idx if e.get('analyzed')))"); do
    python analyze.py --corpus ./corpus --out-dir /tmp/local-ref --game "$uid"
done

python verify_determinism.py /tmp/local-ref /tmp/cloud
```

Exit 0 means cloud and laptop analysis are interchangeable and you can mix them
in one corpus. Run it before the big job, not after.

## The transposition table carries across games

**Found while building phase 1, on 2026-08-31. Not yet fixed — it changes
analysis semantics, so it is your call.**

`analyze.py` opens one engine process and reuses it for every game in the run
(`analyze.py:191`), and python-chess only sends `ucinewgame` when the `game=`
argument to `analyse()` changes. It is never passed, so it defaults to `None`
on every call and `ucinewgame` is sent **exactly once per process**.

Consequence: every game after the first inherits a transposition table warmed
by its predecessors, and its evaluations depend on which games preceded it and
in what order. That contradicts the reproducibility claim in the module
docstring and in the README.

Measured, with the engine binary held bit-identical
(`sha256:2ca4238…`) across both images:

| comparison | result |
|---|---|
| `5c85766c…` (1st game of the phase 0 run) re-run alone vs `corpus/analysis/` | **identical** |
| `4ec50aae…` (5th game of the phase 0 run) re-run alone vs `corpus/analysis/` | **all 25 plies differ** |
| same game, phase 0 image vs phase 1 image, both alone | identical |

The first game matches because it saw an empty table in both runs. The fifth
does not, because in the phase 0 run it saw a table warmed by four games. The
phase 0 determinism check was still valid — it compared container and laptop
under the same batch ordering, which holds the contamination constant and so
cancels it out.

**This does not block phase 1.** `worker/run_shard.py` invokes `analyze.py`
once per game, so every cloud game starts from an empty table. Cloud output is
internally consistent and reproducible, and is independent of shard size and
of where a retry resumed — which is exactly the property sharding needs.

What it does mean: **cloud output will not match nine of the ten files now in
`corpus/analysis/`**, and merging a cloud run into that directory would leave
the corpus with two incompatible flavours of analysis in it.

The fix is three lines in `analyze_game()`:

```python
    plies = []
    game_token = object()      # fresh per game -> python-chess sends ucinewgame
...
        info = engine.analyse(board, limit, multipv=multipv, game=game_token)
...
        after_info = engine.analyse(board, limit, game=game_token)
```

After that, a local batch run and a per-game cloud run agree, and the ten
calibration files need re-running once (they are the only analysis that
exists, so the cost is ten games).

Then the real thing:

```bash
python submit/submit_batch.py submit \
  --corpus ./corpus --bucket "$BUCKET" --queue "$QUEUE" --job-definition "$JOBDEF" \
  --shard-size 25 --dry-run       # read the plan first

# drop --dry-run, wait, then:
python submit/submit_batch.py fetch --bucket "$BUCKET" --run-id "$RUN" \
  --dest /tmp/cloud --merge
```

## Decisions worth knowing

**Shards of 25, not one game per child.** Batch spends tens of seconds per
array child on placement and image pull. At ~3 minutes per game that is 15-20%
overhead at one game per child and under 1% at twenty-five. The worker still
uploads after every game, so a shard is not an atomic unit of loss.

**Retry is cheap because resume is a list.** A child lists the run's analysis
prefix on startup and skips anything already uploaded. Spot reclamation
therefore costs one game, not one shard, and `job_retry_attempts = 4` is a
reasonable default rather than a way to burn money.

**Runs are namespaced by parameters.** `runs/<run_id>/` where the run id
carries a digest of `{nodes, multipv, hash, skip_plies}`. Two runs at
different `--nodes` produce legitimately different numbers for the same game;
they must not share a prefix and silently overwrite each other.

**The source PGN is unwritable from the cluster.** The Batch job role has
`GetObject` on `corpus/*` and `PutObject` only on `runs/*/analysis/*` and
`runs/*/status/*`. The README's "PGN is immutable" claim is enforced by IAM
here, not just by convention.

**Merging back into the corpus is a separate, explicit command.** `fetch`
downloads; `fetch --merge` marks games analyzed in `index.json`. Everything
downstream trusts that file, so flipping those flags should not be a side
effect of a download you ran to look at.

**Public subnets, no NAT.** Saves the ~$32/month standing charge on a stack
that is idle between runs. The security group has no ingress rules at all and
there is no SSM path in. This is a lab-economics choice; the version to
present in a bank review is private subnets with ECR/logs interface endpoints,
and the swap touches `infra/network.tf` and nothing else.

## Cost

Roughly, and unverified — measure a small run before believing any of it.
3312 games at ~3 minutes each is ~166 core-hours. At a spot rate around
$0.015-0.02 per vCPU-hour for the families in `var.instance_types`, that is
**$3-5 for the full corpus**, plus a few cents of S3. `max_vcpus` changes how
long it takes, not what it costs.

The thing that actually costs money here is leaving `max_vcpus` high with a
stuck job, so check `status` before going to bed.

## What phase 1 deliberately does not do

- **No Vault.** Static credentials for the submitter, per the plan. The
  workers already use short-lived Batch job-role credentials, so the honest
  phase 2 argument is about the ingest side's `LICHESS_TOKEN`, not about
  replacing the job role. `infra/iam.tf` marks the seam.
- **No remote state.** Local `terraform.tfstate`. Moving state to S3 means the
  thing reading it needs an identity, which is the same decision as above —
  worth doing once, in phase 2.
- **No on-demand fallback queue.** Spot only. Nothing here is time-critical
  and an unattended fallback to on-demand is how you find out you have been
  paying full price for a week. `infra/batch.tf` notes where it would slot in.
- **No cost alarm.** Worth adding before the first unattended overnight run.

## Run log

**2026-08-31 — phase 1 first live run.** Account 093807443918, us-east-1.

Measured, not estimated:

| | |
|---|---|
| corpus sync (3322 PGNs + index) | ~3 min |
| per-game analysis, `m6i.large` spot | ~4.2 min (the 3.0 default in `--est-minutes-per-game` is optimistic) |
| calibration run, 10 games / 2 shards | run `20260831-172654-8cd004da` |
| full backlog, 3173 games / 127 shards | run `20260831-173947-8cd004da` |

Determinism verified end to end: local batch analysis (with the `game_token`
fix) and cloud per-game analysis agree on every compared field —
`2777e28a56af8e92`, 0 of 55 plies differ.

### Two bugs this run surfaced

**Batch `command` does not replace ENTRYPOINT.** It maps to Docker CMD and is
appended. With `ENTRYPOINT ["python", "/app/analyze.py"]`, a job definition
asking for `run_shard.py` ran `python /app/analyze.py python /app/run_shard.py`
and every child died on argument parsing. `terraform validate` cannot see this
— the config is schema-valid and the image reference is correct; the fault
lives only in the interaction between the Dockerfile and the job definition.
Batch has no `entryPoint` field in `containerProperties`, so the fix had to be
in the image: `ENTRYPOINT ["python"]`, script chosen by `command`.

Smoke-test both container paths before pushing:

```bash
docker run --rm -e CHESSBOOK_BUCKET=x -e CHESSBOOK_RUN_ID=y \
  -e CHESSBOOK_SHARD_INDEX=0 chessbook-worker:<tag> /app/run_shard.py
docker run --rm chessbook-worker:<tag> /app/analyze.py --help
```

**The transposition table carried across games** — see the section above. Both
bugs were caught by the 10-game calibration run rather than the 3173-game one,
which is the entire argument for calibrating first.
