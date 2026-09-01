output "bucket" {
  description = "Corpus bucket name. Pass to submit_batch.py as --bucket."
  value       = aws_s3_bucket.corpus.id
}

output "ecr_repository_url" {
  description = "Push the worker image here."
  value       = aws_ecr_repository.worker.repository_url
}

output "job_queue" {
  description = "Batch job queue name. Pass to submit_batch.py as --queue."
  value       = aws_batch_job_queue.main.name
}

output "job_definition" {
  description = "Batch job definition name. Pass to submit_batch.py as --job-definition."
  value       = aws_batch_job_definition.analyze.name
}

output "job_definition_revision" {
  description = "Current revision. Batch runs the latest unless you name one explicitly."
  value       = aws_batch_job_definition.analyze.revision
}

output "worker_image" {
  description = "Image reference the job definition will run. Shows repo:tag until worker_image_digest is set, repo@sha256:... after."
  value       = local.worker_image
}

output "log_group" {
  description = "CloudWatch group holding worker logs."
  value       = aws_cloudwatch_log_group.worker.name
}

output "next_steps" {
  description = "Copy-paste sequence for a first run."
  value       = <<-EOT

    1. Build and push the worker:

         ./scripts/push_image.sh ${aws_ecr_repository.worker.repository_url} ${var.worker_image_tag}

       It prints the image digest. Put it in terraform.tfvars as
       worker_image_digest and re-apply, so the job definition names the bits
       rather than a mutable tag.

    2. Upload the corpus (once; re-runs only send what changed):

         python submit/submit_batch.py sync \
           --corpus ./corpus --bucket ${aws_s3_bucket.corpus.id}

    3. Dry-run a submission to see the shard plan without spending anything:

         python submit/submit_batch.py submit \
           --corpus ./corpus --bucket ${aws_s3_bucket.corpus.id} \
           --queue ${aws_batch_job_queue.main.name} \
           --job-definition ${aws_batch_job_definition.analyze.name} \
           --only hub/nc6/goring --limit 20 --shard-size 5 --dry-run

    4. Drop --dry-run. Then watch:

         python submit/submit_batch.py status --run-id <printed above> \
           --bucket ${aws_s3_bucket.corpus.id}

    5. Bring results home and verify them against the phase 0 calibration set:

         python submit/submit_batch.py fetch --run-id <run> \
           --bucket ${aws_s3_bucket.corpus.id} --dest /tmp/cloud-analysis
         python verify_determinism.py corpus/analysis /tmp/cloud-analysis

  EOT
}
