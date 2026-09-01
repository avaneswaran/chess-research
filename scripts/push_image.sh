#!/usr/bin/env bash
#
# push_image.sh — build the worker, push it to ECR, print the digest.
#
#   ./scripts/push_image.sh <ecr-repository-url> [tag] [sf-sha256]
#
#   ./scripts/push_image.sh \
#       123456789012.dkr.ecr.us-east-1.amazonaws.com/chessbook-worker sf17.1
#
# The digest it prints is what you put in terraform.tfvars as
# worker_image_digest. A tag is a mutable pointer even in an IMMUTABLE
# repository — immutability stops you overwriting sf17.1, it does not stop you
# deleting and re-pushing it. Pinning the job definition to a digest is what
# makes "this analysis came from that engine build" checkable rather than
# asserted.
#
# The build runs the bench gate in docker/Dockerfile. If the engine's search
# behaviour has drifted from the reference node count, this fails here rather
# than after 3300 games of incomparable analysis.

set -euo pipefail

REPO="${1:?usage: push_image.sh <ecr-repository-url> [tag] [sf-sha256]}"
TAG="${2:-sf17.1}"
SF_SHA256="${3:-}"

REGISTRY="${REPO%%/*}"
REGION="$(echo "$REGISTRY" | sed -n 's/.*\.dkr\.ecr\.\([a-z0-9-]*\)\.amazonaws\.com/\1/p')"

if [ -z "$REGION" ]; then
    echo "error: could not parse a region out of '$REGISTRY'" >&2
    echo "       expected <account>.dkr.ecr.<region>.amazonaws.com/<repo>" >&2
    exit 1
fi

cd "$(dirname "$0")/.."

BUILD_ARGS=()
if [ -n "$SF_SHA256" ]; then
    BUILD_ARGS+=(--build-arg "SF_SHA256=${SF_SHA256}")
else
    echo "note: no SF_SHA256 given — the Stockfish tarball will not be verified" >&2
    echo "      (the bench gate still runs; it catches drift, not tampering)" >&2
fi

echo "==> building ${TAG}"
# Explicit platform: the Batch compute environment is x86-64 and the engine is
# built x86-64-bmi2. Building this on an Apple Silicon laptop without the flag
# produces an arm64 image that will not start on the cluster at all.
docker build \
    --platform linux/amd64 \
    "${BUILD_ARGS[@]}" \
    -t "chessbook-worker:${TAG}" \
    -f docker/Dockerfile .

echo "==> logging in to ${REGISTRY}"
aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> pushing ${REPO}:${TAG}"
docker tag "chessbook-worker:${TAG}" "${REPO}:${TAG}"
docker push "${REPO}:${TAG}"

DIGEST="$(aws ecr describe-images \
    --region "$REGION" \
    --repository-name "${REPO#*/}" \
    --image-ids "imageTag=${TAG}" \
    --query 'imageDetails[0].imageDigest' \
    --output text)"

ENGINE_SHA="$(docker run --rm --entrypoint cat "chessbook-worker:${TAG}" \
    /usr/local/share/stockfish.sha256 | awk '{print $1}')"

cat <<SUMMARY

  pushed  ${REPO}:${TAG}
  digest  ${DIGEST}
  engine  ${ENGINE_SHA}

  Pin it — add to infra/terraform.tfvars:

    worker_image_digest = "${DIGEST}"

  then: terraform -chdir=infra apply

SUMMARY
