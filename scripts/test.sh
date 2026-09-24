#!/usr/bin/env bash
# Lint (ruff) and run the test suite in Docker against a throwaway PostgreSQL, the same
# checks the GitHub workflow runs. Also the estate's pre-push gate for this repo
# (kubernetes-ansible/ci/checks/cgm_bridge.py).
#
#   scripts/test.sh              # lint + all tests
#   scripts/test.sh -k export    # extra args go to pytest
#
# Builds the Dockerfile's `test` stage (needs network the first time, to fetch the locked
# dependencies; cached afterwards), then runs it on an internal Docker network that has no
# route out. Needs only Docker on the host: no Python, uv or PostgreSQL.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

POSTGRES_IMAGE="postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"
RUN_ID="cgm-bridge-test-$$"
TEST_IMAGE="cgm-bridge:test"

cleanup() {
    docker rm -f "$RUN_ID-pg" >/dev/null 2>&1 || true
    docker network rm "$RUN_ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --quiet --target test -t "$TEST_IMAGE" . >/dev/null

# Ruff comes from the locked dev group inside the image, so it matches CI exactly.
ruff() { docker run --rm --network none --entrypoint /app/.venv/bin/ruff "$TEST_IMAGE" "$@"; }
ruff check --no-cache .
ruff format --no-cache --check .

docker network create --internal "$RUN_ID" >/dev/null
docker run -d --name "$RUN_ID-pg" --network "$RUN_ID" \
    -e POSTGRES_PASSWORD=test-only-password \
    --tmpfs /var/lib/postgresql/data \
    "$POSTGRES_IMAGE" >/dev/null

for _ in $(seq 60); do
    if docker exec "$RUN_ID-pg" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

docker run --rm --network "$RUN_ID" \
    -e PGHOST="$RUN_ID-pg" -e PGUSER=postgres -e PGPASSWORD=test-only-password \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "$TEST_IMAGE" -p no:cacheprovider "$@"
