# Four roles, because Batch on EC2 genuinely has four distinct identities and
# collapsing them is how you end up granting the analysis container the right
# to terminate the instance it is running on:
#
#   batch_service   AWS Batch itself, launching and scaling the instances
#   instance        the EC2 host, registering with ECS and pulling images
#   execution       the ECS agent, pulling THIS task's image and shipping logs
#   job             the analysis container's own credentials  <-- the only one
#                   the worker code ever sees, and the phase 2 Vault seam
#
# Only `job` is reachable from inside analyze.py. That is the whole point of
# separating them.

# --- Batch service role -----------------------------------------------------

data "aws_iam_policy_document" "batch_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["batch.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_service" {
  name               = "${local.name}-batch-service"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

# --- EC2 instance role ------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.name}-batch-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "instance_ecs" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

# SSM is not attached. There is no inbound path to these instances by design
# (see the security group in network.tf) and adding Session Manager would
# create one. If you need to debug a shard, reproduce it locally with the same
# image — that is what the determinism work in phase 0 bought you.

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-batch-instance"
  role = aws_iam_role.instance.name
}

# --- ECS task execution role ------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# --- Job role: the container's own credentials ------------------------------
#
# THIS IS THE PHASE 2 SEAM.
#
# Right now the worker reads and writes S3 with these credentials, delivered
# by the ECS task metadata endpoint — no static key ever enters the image.
# The submitter on your laptop is the only thing in phase 1 holding a long
# lived key pair.
#
# When Vault arrives, the argument to make is not "Vault replaces this role".
# It does not; the role is already short-lived and already workload-scoped.
# The argument is that the ingest side (LICHESS_TOKEN, per ingest.py) has no
# equivalent, and that a Vault JWT auth backend trusting this role's identity
# is how the ingest job stops holding a static token too.

resource "aws_iam_role" "job" {
  name               = "${local.name}-batch-job"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "job_s3" {
  # Read the run's inputs: the shard manifests and the PGNs they name.
  statement {
    sid     = "ReadCorpusAndRunInputs"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.corpus.arn}/corpus/*",
      "${aws_s3_bucket.corpus.arn}/runs/*",
    ]
  }

  # Write only under a run's output prefixes. The worker has no way to
  # overwrite corpus/games/*.pgn even by accident — the immutability of the
  # source PGN is enforced here, not just asserted in the README.
  statement {
    sid = "WriteRunOutputs"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      "${aws_s3_bucket.corpus.arn}/runs/*/analysis/*",
      "${aws_s3_bucket.corpus.arn}/runs/*/status/*",
    ]
  }

  # ListBucket is what makes the resume check cheap: the worker lists the
  # analysis prefix once instead of issuing a HEAD per game.
  statement {
    sid       = "ListRunPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.corpus.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["corpus/*", "runs/*"]
    }
  }

  # No s3:DeleteObject anywhere. A worker that can delete is a worker that can
  # destroy an overnight run on a bad retry.
}

resource "aws_iam_role_policy" "job_s3" {
  name   = "${local.name}-job-s3"
  role   = aws_iam_role.job.id
  policy = data.aws_iam_policy_document.job_s3.json
}
