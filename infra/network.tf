# Public subnets, no NAT gateway.
#
# The worker needs outbound reach to exactly three things: ECR (pull the
# image), S3 (read PGNs, write analysis), and CloudWatch Logs. The two ways to
# give a private subnet that reach are a NAT gateway (~$32/month standing,
# before data processing) or a set of interface endpoints (~$7/month each for
# ECR API, ECR DKR, and Logs, plus the free S3 gateway endpoint). Both are a
# recurring charge on a stack that is idle between overnight runs.
#
# Public subnets with instance-level public IPs cost nothing standing. The
# security group below allows no inbound at all, so the instances are not
# reachable from the internet in any meaningful sense — they can dial out,
# nothing can dial in.
#
# This is a lab-economics choice, not a security-posture claim. If this stack
# ever needs to be defensible in the bank review the README mentions, private
# subnets with endpoints is the version to present, and the swap touches only
# this file plus the subnet ids in batch.tf.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]

  # /20 per subnet out of a /16: 4094 usable addresses each, far more than
  # max_vcpus will ever need, and the arithmetic stays readable.
  cidr_block = cidrsubnet(var.vpc_cidr, 4, count.index)

  # Batch's managed compute environment does not attach public IPs itself
  # when it launches into a subnet; the subnet has to hand them out. Without
  # this, instances come up with no route to ECR and the compute environment
  # sits at INVALID with a singularly unhelpful message.
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "batch" {
  name_prefix = "${local.name}-batch-"
  description = "chessbook Batch workers: egress only"
  vpc_id      = aws_vpc.main.id

  # No ingress rules at all. Nothing needs to reach these instances; there is
  # no SSH path in by design. If a shard fails, the evidence is in CloudWatch,
  # not on the box — and the box is gone by the time you look anyway.

  egress {
    description = "Outbound to ECR, S3, and CloudWatch Logs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-batch" }

  lifecycle {
    create_before_destroy = true
  }
}

# S3 gateway endpoint. Free, and it keeps the corpus transfer off the public
# path and out of any future NAT data-processing charge. The only reason not
# to have one is if you enjoy paying for bytes you did not need to route.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${local.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = { Name = "${local.name}-s3-endpoint" }
}
