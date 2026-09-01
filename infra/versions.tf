# Provider pinning.
#
# The AWS provider renamed several Batch arguments between major versions
# (v6 renamed `compute_environment_name` to `name`, among others). This stack
# is written against v5 syntax throughout. Bumping to v6 is a deliberate
# migration, not a `terraform init -upgrade` away, so the constraint is a
# major-version pin rather than a floor.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 5.61 for `compute_environment_order` on aws_batch_job_queue,
      # which replaced the now-deprecated `compute_environments` list.
      version = ">= 5.61.0, < 6.0.0"
    }
  }

  # Local state, deliberately, for phase 1.
  #
  # Remote state belongs with the Vault work in phase 2, because the two
  # decisions are the same decision: once state lives in S3 with a lock table,
  # the thing that reads it needs an identity, and that identity should not be
  # the static key pair this phase runs on. Moving state now would mean doing
  # it twice.
  #
  # backend "s3" {
  #   bucket         = "chessbook-tfstate-<account>"
  #   key            = "chessbook/phase1.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "chessbook-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = local.tags
  }
}
