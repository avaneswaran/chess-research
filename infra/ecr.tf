resource "aws_ecr_repository" "worker" {
  name                 = "${local.name}-worker"
  image_tag_mutability = var.ecr_image_tag_mutability
  force_delete         = var.ecr_force_delete

  # Scan on push is free and the finding you care about is not in your code —
  # it is in the base image's OpenSSL six months from now, on an image you
  # pinned precisely so it would never change.
  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = "${local.name}-worker" }
}

# Untagged images are the layers orphaned by a re-push. Nothing references
# them and they are billed per GB-month. Tagged images are kept indefinitely
# on purpose: the tag is the record of which engine build produced which
# analysis, and expiring it would break the provenance chain the whole corpus
# is built on.
resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
    ]
  })
}
