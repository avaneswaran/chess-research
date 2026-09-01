# chessbook analysis worker
#
# Two things make this image worth pinning rather than just installing:
#
# 1. Ubuntu's stockfish package is the portable SSE2 fallback. It runs
#    anywhere and uses none of the host CPU's vector instructions —
#    ~512k nodes/sec versus several million for a properly built binary.
#    Same results, several times slower, which on a 3000-game corpus is the
#    difference between an hour and most of a day.
#
# 2. The bench gate below fails the BUILD if the engine's search behaviour
#    differs from the reference. Stockfish's own CI uses bench node count as
#    its correctness check: same version and same net produce the same node
#    count regardless of which SIMD build you use. So a mismatch here means a
#    version or net drift, and it means analysis from this image would not be
#    comparable to analysis already in the corpus. Better to fail at build
#    time than to discover it in a published variation.
#
# Build (from the repo root — the context must include worker/ and docker/):
#   docker build \
#     --build-arg SF_SHA256=<sha of the release tarball> \
#     -t chessbook-worker:sf17.1 -f docker/Dockerfile .
#
# Or ./scripts/push_image.sh <ecr-repo-url> sf17.1, which builds, runs the
# gate, pushes, and prints the digest to pin in terraform.tfvars.
#
# ARCHITECTURE CHOICE: bmi2, deliberately, everywhere.
# AVX-512 is faster on Ice Lake / Sapphire Rapids spot instances, but a
# binary built for it will SIGILL on machines without it — including,
# possibly, your laptop. One build used in every environment is worth more
# than peak throughput in one of them. Revisit only if throughput becomes
# the constraint, and if you do, change it for ALL runs, not some.

FROM debian:bookworm-slim AS engine

ARG SF_VERSION=sf_17.1
ARG SF_ARCH=x86-64-bmi2
ARG SF_SHA256
ARG EXPECTED_BENCH_NODES=2030154

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    url="https://github.com/official-stockfish/Stockfish/releases/download/${SF_VERSION}/stockfish-ubuntu-${SF_ARCH}.tar"; \
    curl -fsSL -o /tmp/sf.tar "$url"; \
    if [ -n "${SF_SHA256:-}" ]; then \
        echo "${SF_SHA256}  /tmp/sf.tar" | sha256sum -c -; \
    else \
        echo "WARNING: no SF_SHA256 supplied — binary is unverified"; \
        sha256sum /tmp/sf.tar; \
    fi; \
    mkdir -p /opt/sf; \
    tar -xf /tmp/sf.tar -C /opt/sf; \
    find /opt/sf -type f -name "stockfish*" -perm -u+x -exec cp {} /usr/local/bin/stockfish \; ; \
    chmod +x /usr/local/bin/stockfish; \
    rm -rf /tmp/sf.tar /opt/sf

# --- the determinism gate ---------------------------------------------------
# If this fails, do not proceed. It means this image would produce analysis
# that is not comparable to what is already in the corpus.
RUN set -eu; \
    out="$(stockfish bench 2>&1 || true)"; \
    nodes="$(echo "$out" | sed -n 's/.*Nodes searched *: *\([0-9]\+\).*/\1/p' | tail -1)"; \
    echo "----------------------------------------"; \
    echo "$out" | sed -n '/Nodes searched/,$p'; \
    echo "bench nodes  : ${nodes:-<none>}"; \
    echo "expected     : ${EXPECTED_BENCH_NODES}"; \
    echo "----------------------------------------"; \
    if [ "${nodes:-}" != "${EXPECTED_BENCH_NODES}" ]; then \
        echo "BENCH MISMATCH — engine search behaviour differs from reference."; \
        echo "Analysis from this image would not be comparable to the corpus."; \
        exit 1; \
    fi; \
    echo "bench OK"

RUN sha256sum /usr/local/bin/stockfish | tee /usr/local/share/stockfish.sha256


# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm

COPY --from=engine /usr/local/bin/stockfish /usr/local/bin/stockfish
COPY --from=engine /usr/local/share/stockfish.sha256 /usr/local/share/stockfish.sha256

COPY docker/requirements-worker.txt /tmp/requirements-worker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-worker.txt \
 && rm /tmp/requirements-worker.txt

WORKDIR /app
COPY classify.py provenance.py analyze.py stats.py /app/

# The AWS Batch shard runner. It shells out to analyze.py rather than
# reimplementing any of it, so this file being present changes nothing about
# how the image behaves when you run it by hand.
COPY worker/run_shard.py /app/

# Non-root: the worker needs no privileges, and Batch job roles are the
# identity that matters, not the uid.
RUN useradd --create-home --uid 10001 worker
USER worker

ENV STOCKFISH_PATH=/usr/local/bin/stockfish

# ENTRYPOINT is the interpreter alone, not the interpreter plus a script.
#
# AWS Batch has no entryPoint field in containerProperties — only `command`,
# which maps to Docker's CMD and is APPENDED to ENTRYPOINT rather than
# replacing it. With `ENTRYPOINT ["python", "/app/analyze.py"]` a job
# definition asking for run_shard.py actually ran
#     python /app/analyze.py python /app/run_shard.py
# and died on "unrecognized arguments". Keeping the script out of ENTRYPOINT
# is what lets the job definition choose which one runs.
#
# By hand, the script is now explicit:
#   docker run --rm -v $PWD/corpus:/corpus:ro -v /tmp/out:/out \
#     chessbook-worker:<tag> /app/analyze.py --corpus /corpus --out-dir /out --game <uid>
ENTRYPOINT ["python"]
CMD ["/app/analyze.py", "--help"]
