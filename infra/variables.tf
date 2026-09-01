variable "project" {
  description = "Name prefix for every resource in this stack."
  type        = string
  default     = "chessbook"
}

variable "aws_region" {
  description = "Region to build in. Spot depth for the general-purpose and compute families this stack asks for is best in us-east-1 and us-west-2."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Named profile from ~/.aws/credentials. Leave null to use the ambient credential chain (env vars, instance role, SSO). Phase 1 runs on a static key pair; phase 2 replaces this seam with a Vault-issued identity."
  type        = string
  default     = null
}

# --- networking -------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR for the dedicated VPC. Nothing else lives in it, so the range only has to avoid colliding with anything you might peer to later."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = "How many AZs to spread public subnets across. More AZs means Batch has more spot pools to choose from, which is the single cheapest way to reduce interruption rate."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 1 && var.az_count <= 6
    error_message = "az_count must be between 1 and 6."
  }
}

# --- compute ----------------------------------------------------------------

variable "instance_types" {
  description = <<-DESC
    Instance families Batch may launch. Family-level entries (no size suffix)
    let Batch pick whatever size fits the job, which widens the spot pool.

    MUST BE x86-64 WITH BMI2. docker/Dockerfile builds Stockfish as
    x86-64-bmi2 on purpose; that binary SIGILLs on a CPU without those
    instructions. Every family below is Skylake-or-later Intel or Zen-or-later
    AMD, which all carry BMI2.

    DO NOT add Graviton families (c6g, c7g, m6g, r7g, ...). They are arm64 and
    the worker image is linux/amd64 — the job would fail to start at all,
    which is at least loud, but it wastes a scheduling round trip per attempt.

    DO NOT add burstable families (t3, t4g). Analysis pins a core at 100% for
    minutes at a time and would exhaust the CPU credit balance almost
    immediately, then run at the baseline fraction of a core.
  DESC
  type        = list(string)
  default = [
    "c5", "c5a", "c5n",
    "c6i", "c6a",
    "m5", "m5a", "m5n",
    "m6i", "m6a",
    "r5", "r5a", "r6i",
  ]
}

variable "max_vcpus" {
  description = "Ceiling on concurrent vCPUs. Each job asks for job_vcpus, so this divided by job_vcpus is roughly your maximum parallel game count. Raise it to go faster; your spot quota is the real limit."
  type        = number
  default     = 64
}

variable "spot_bid_percentage" {
  description = "Maximum spot price as a percentage of the on-demand price for the same instance type. 100 means 'never pay more than on-demand', which is the sane default — spot is typically 60-80% off, and capping lower mostly buys you interruptions rather than savings."
  type        = number
  default     = 100
}

# --- worker image -----------------------------------------------------------

variable "worker_image_tag" {
  description = "Tag to run when worker_image_digest is null."
  type        = string
  default     = "sf17.1"
}

variable "worker_image_digest" {
  description = <<-DESC
    Pin the job definition to an image digest ("sha256:abc123...") instead of
    a tag. Prefer this once you have pushed.

    A tag is a mutable pointer even in an IMMUTABLE repository — immutability
    stops you overwriting sf17.1, it does not stop you deleting and re-pushing
    it. A digest is the image. For a corpus whose entire claim is that a
    variation can be re-derived a year from now, the job definition should
    name the bits, not a label pointing at them. scripts/push_image.sh prints
    the digest to paste here.
  DESC
  type        = string
  default     = null
}

# --- job shape --------------------------------------------------------------

variable "job_vcpus" {
  description = "vCPUs per array child. analyze.py configures the engine with Threads=1 and there is no parallelism above that, so anything above 1 is paid for and idle."
  type        = number
  default     = 1
}

variable "job_memory_mib" {
  description = "Memory per array child. The engine's hash table is 256 MiB (analyze.py --hash), plus the interpreter, python-chess, and the shard's PGNs. 2 GiB is comfortable; below ~1 GiB the container will be OOM-killed mid-shard."
  type        = number
  default     = 2048
}

variable "job_attempt_timeout_seconds" {
  description = <<-DESC
    Kill an attempt after this long. Batch counts it per attempt, not per job,
    so a retried shard gets the full budget again.

    Size it as shard_size x per-game-minutes x 2. The per-game figure is the
    trap: measured on a lightly loaded instance a game takes ~4 min, but under
    full packing it is ~9 min, because Batch allocates vCPUs (hyperthreads)
    and Stockfish gets very little from SMT. The original 18000 (25 x 10 min,
    "pessimistic") was actually only 1.3x the real 25 x 9 = 225 min, and one
    shard in the first live run hit the cap.

    25 games x 9 min x 2 = 27000.
  DESC
  type        = number
  default     = 27000
}

variable "job_retry_attempts" {
  description = "Attempts per array child. Spot reclamation is the expected failure and is worth retrying; a genuine bug in the analysis is not. batch.tf's evaluate_on_exit block encodes that distinction."
  type        = number
  default     = 4
}

# --- storage ----------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch retention for worker logs. These are the only record of why a shard failed once the container is gone."
  type        = number
  default     = 30
}

variable "ecr_image_tag_mutability" {
  description = "IMMUTABLE refuses to overwrite an existing tag, which is the behaviour you want for a pinned analysis worker. Set MUTABLE only if you are iterating on the image and re-pushing the same tag repeatedly."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["IMMUTABLE", "MUTABLE"], var.ecr_image_tag_mutability)
    error_message = "ecr_image_tag_mutability must be IMMUTABLE or MUTABLE."
  }
}

variable "ecr_force_delete" {
  description = "Allow `terraform destroy` to delete the ECR repository while it still holds images."
  type        = bool
  default     = false
}

variable "s3_force_destroy" {
  description = <<-DESC
    Allow `terraform destroy` to empty and delete the corpus bucket.

    Left false on purpose. The bucket holds the immutable PGN source of truth
    and every analysis run's output; `terraform destroy` on a stack you were
    only meaning to rebuild should fail loudly rather than delete an overnight
    grind. Flip it, destroy, flip it back.
  DESC
  type        = bool
  default     = false
}

variable "tags" {
  description = "Extra tags merged into the provider's default_tags."
  type        = map(string)
  default     = {}
}
