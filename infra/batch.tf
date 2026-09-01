resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/batch/${local.name}"
  retention_in_days = var.log_retention_days
}

# --- compute environment ----------------------------------------------------

resource "aws_batch_compute_environment" "spot" {
  # name_prefix, not name, plus create_before_destroy.
  #
  # Almost every meaningful edit to compute_resources (instance types, subnets,
  # allocation strategy) forces replacement, and Batch refuses to delete a
  # compute environment that a job queue still references. With a fixed name
  # Terraform tries to destroy-then-create, hits that refusal, and leaves you
  # hand-disabling the queue in the console. With a prefix it creates the
  # replacement first, repoints the queue, then removes the old one.
  compute_environment_name_prefix = "${local.name}-spot-"

  type         = "MANAGED"
  state        = "ENABLED"
  service_role = aws_iam_role.batch_service.arn

  compute_resources {
    type = "SPOT"

    # SPOT_CAPACITY_OPTIMIZED picks the pools with the deepest spare capacity
    # rather than the cheapest instantaneous price. For a job measured in
    # hours, avoiding a reclamation is worth far more than a few cents of
    # hourly rate — a reclaimed shard costs you the whole in-flight game.
    #
    # It also means no spot fleet IAM role is required. That role is only
    # consulted under the legacy BEST_FIT strategy, and its absence here is
    # deliberate rather than an oversight.
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"
    bid_percentage      = var.spot_bid_percentage

    instance_role = aws_iam_instance_profile.instance.arn
    instance_type = var.instance_types

    min_vcpus = 0
    max_vcpus = var.max_vcpus

    subnets            = aws_subnet.public[*].id
    security_group_ids = [aws_security_group.batch.id]

    # Pin the AMI family. The default is still Amazon Linux 2; being explicit
    # means a future change to Batch's default does not silently move the host
    # kernel and container runtime underneath a stack whose entire premise is
    # reproducibility.
    ec2_configuration {
      image_type = "ECS_AL2023"
    }

    tags = merge(local.tags, {
      Name = "${local.name}-worker"
    })
  }

  # Batch will not accept a compute environment whose service role is not yet
  # usable, and Terraform's implicit dependency is on the role, not on the
  # policy attachment. Without this, first applies fail intermittently.
  depends_on = [aws_iam_role_policy_attachment.batch_service]

  lifecycle {
    create_before_destroy = true

    # Batch owns this number. It scales it up when jobs queue and back to
    # min_vcpus when they drain; reading it back as configuration would make
    # every plan after a run show spurious drift.
    ignore_changes = [compute_resources[0].desired_vcpus]
  }
}

# --- queue ------------------------------------------------------------------

resource "aws_batch_job_queue" "main" {
  name     = "${local.name}-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.spot.arn
  }

  # create_before_destroy has to propagate to everything that depends on the
  # compute environment, or Terraform plans the old CE's destroy before the
  # queue has been repointed and the graph deadlocks.
  lifecycle {
    create_before_destroy = true
  }

  # An on-demand fallback environment would slot in here as order = 2, so
  # shards that cannot get spot capacity still finish. Not built in phase 1:
  # nothing here is time-critical, and an unattended fallback to on-demand is
  # a good way to discover you have been paying full price for a week.
}

# --- job definition ---------------------------------------------------------

resource "aws_batch_job_definition" "analyze" {
  name                  = "${local.name}-analyze"
  type                  = "container"
  platform_capabilities = ["EC2"]

  container_properties = jsonencode({
    image = local.worker_image

    # Batch `command` maps to Docker CMD and is APPENDED to the image's
    # ENTRYPOINT — it does not replace it, and containerProperties has no
    # entryPoint field to override. The image's ENTRYPOINT is therefore
    # ["python"] alone, so naming a script here is what selects it. Repeating
    # "python" would run `python python /app/run_shard.py`.
    command = ["/app/run_shard.py"]

    jobRoleArn       = aws_iam_role.job.arn
    executionRoleArn = aws_iam_role.execution.arn

    resourceRequirements = [
      { type = "VCPU", value = tostring(var.job_vcpus) },
      { type = "MEMORY", value = tostring(var.job_memory_mib) },
    ]

    environment = [
      { name = "CHESSBOOK_BUCKET", value = aws_s3_bucket.corpus.id },
      { name = "AWS_DEFAULT_REGION", value = local.region },
      # CHESSBOOK_RUN_ID is supplied per submission by submit_batch.py, along
      # with any engine parameter overrides. Anything that varies per run
      # belongs in the submission, not baked into the definition.
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "shard"
      }
    }

    # The engine is CPU-bound and single-threaded; nothing here needs to write
    # outside /tmp. Not set read-only because Stockfish and python-chess both
    # want scratch space, and fighting that buys nothing on an ephemeral host.
    user = "worker"
  })

  retry_strategy {
    attempts = var.job_retry_attempts

    # Order matters: first matching condition wins.

    # Spot reclaimed the host. Expected, not a bug, and the shard resumes from
    # whatever it already uploaded.
    evaluate_on_exit {
      action           = "RETRY"
      on_status_reason = "Host EC2*"
    }

    # 75 is EX_TEMPFAIL, which run_shard.py returns when a game failed for a
    # reason that might not recur — an S3 blip, an engine that died mid-search.
    evaluate_on_exit {
      action       = "RETRY"
      on_exit_code = "75"
    }

    # Attempt timeout. This one is counter-intuitive and was originally
    # misclassified here as "deterministic, do not retry".
    #
    # It is not deterministic, because the worker uploads per game and skips
    # anything already present on startup. A shard that timed out with 24 of
    # 25 games done resumes with one game left and finishes in minutes. On the
    # first live run, shard 22 hit the 5h cap on its last game, matched the
    # catch-all EXIT rule below, and was abandoned one game short — the retry
    # would have cost ten minutes and salvaged it.
    #
    # Each attempt makes strictly more progress than the last, so this cannot
    # spin: the only way to burn all four attempts is a single game that
    # genuinely cannot finish inside the cap, which at ~9 min/game would mean
    # something is very wrong and the logs will say so.
    evaluate_on_exit {
      action           = "RETRY"
      on_status_reason = "Job attempt duration exceeded*"
    }

    # Anything else is a real defect. Retrying a deterministic failure three
    # more times just costs money and buries the first, clearest log.
    evaluate_on_exit {
      action    = "EXIT"
      on_reason = "*"
    }
  }

  timeout {
    attempt_duration_seconds = var.job_attempt_timeout_seconds
  }

  # Job definitions are versioned and immutable in Batch; every change creates
  # a new revision and the old ones linger. Deregistering on destroy keeps the
  # list from filling with revisions nothing will ever run again.
  deregister_on_new_revision = true
}
