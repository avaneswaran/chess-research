# The corpus bucket.
#
# Layout mirrors the local corpus/ directory plus a run namespace:
#
#   corpus/index.json                        the full index, uploaded once per submit
#   corpus/games/<game_uid>.pgn              immutable source of truth
#   runs/<run_id>/manifest.json              run params + engine expectations
#   runs/<run_id>/shards/<NNNN>.json         game_uids + index entries per child
#   runs/<run_id>/analysis/<game_uid>.json   worker output, one per game
#   runs/<run_id>/status/<NNNN>.json         per-shard completion record
#
# Analysis is namespaced by run rather than written to a single
# corpus/analysis/ prefix, because a run is defined by its parameters. Two
# runs at different --nodes produce legitimately different numbers for the
# same game, and flattening them into one prefix would mean the second run
# silently overwrites the first with results that are not comparable. Merging
# a finished run back into the local corpus is an explicit step
# (submit_batch.py fetch), which is where that judgement belongs.

resource "aws_s3_bucket" "corpus" {
  bucket        = local.bucket_name
  force_destroy = var.s3_force_destroy

  tags = { Name = local.bucket_name }
}

# Versioning on an "immutable" bucket is not a contradiction — it is what
# makes the immutability enforceable rather than aspirational. A PGN
# overwritten by a buggy re-ingest is recoverable; without versioning the
# source of truth is exactly as durable as the last person to run a sync.
resource "aws_s3_bucket_versioning" "corpus" {
  bucket = aws_s3_bucket.corpus.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "corpus" {
  bucket = aws_s3_bucket.corpus.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "corpus" {
  bucket = aws_s3_bucket.corpus.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "corpus" {
  bucket = aws_s3_bucket.corpus.id

  # A shard killed by spot reclamation mid-upload can leave an incomplete
  # multipart upload behind. They are invisible in the console's object list
  # and they are billed. Seven days is long enough that nothing in flight is
  # affected.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Keep noncurrent versions long enough to notice and undo a bad sync, then
  # stop paying for them.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
