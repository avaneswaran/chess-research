data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  # Local Zones and Wavelength Zones show up here and do not run Batch
  # managed compute environments. Filter to real regional AZs.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # S3 bucket names are globally unique across all AWS accounts, so the name
  # has to carry something nobody else has. Account id and region are
  # deterministic — no random_id, so the bucket name is the same on every
  # apply and can be hardcoded in scripts and docs.
  bucket_name = "${var.project}-corpus-${local.account_id}-${local.region}"

  azs = slice(
    data.aws_availability_zones.available.names,
    0,
    min(var.az_count, length(data.aws_availability_zones.available.names)),
  )

  # Digest wins over tag when both are available. See the worker_image_digest
  # variable for why you want the digest.
  worker_image = (
    var.worker_image_digest != null
    ? "${aws_ecr_repository.worker.repository_url}@${var.worker_image_digest}"
    : "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
  )

  tags = merge(
    {
      Project   = var.project
      ManagedBy = "terraform"
      Phase     = "1"
    },
    var.tags,
  )
}
