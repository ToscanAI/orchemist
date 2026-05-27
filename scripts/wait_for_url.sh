#!/usr/bin/env bash
# scripts/wait_for_url.sh — poll a URL until it returns HTTP 200, with timeout.
#
# Used by `.github/workflows/ci.yml` to gate both the engine readiness probe
# (against `/api/v1/health`) and the Next dev server readiness probe.
#
# Usage:
#   scripts/wait_for_url.sh <url> <timeout_seconds> <label>
#
# Exits 0 on first HTTP 200 (prints "<label> ready in <N>s" to stdout).
# Exits 1 on timeout (prints "::error::<label> failed to become ready within <T>s" to stdout).
# Exits 1 on missing args (prints usage to stderr).
#
# Note: uses `set -uo pipefail` (NOT `set -e`) to avoid the silent-crash gotcha
# documented at sub-check 2a — the `if VAR=$(cmd); then ...` pattern is safe
# under `set -e`, but pipefail-safe count probes still need `|| true` to
# survive zero-match cases. Here we keep things simple with a curl exit-code
# check inside an `if` block.

set -uo pipefail

if [ "$#" -lt 3 ]; then
    echo "Usage: wait_for_url.sh <url> <timeout_seconds> <label>" >&2
    exit 1
fi

URL="$1"
TIMEOUT="$2"
LABEL="$3"

# Validate TIMEOUT is a non-negative integer. Without this, a non-numeric
# value (e.g. "foo") would expand into the C-style for-loop arithmetic
# context as a name reference and behave unpredictably under `set -u`.
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]]; then
    echo "Usage: wait_for_url.sh <url> <timeout_seconds> <label>" >&2
    echo "       (timeout_seconds must be a non-negative integer; got '$TIMEOUT')" >&2
    exit 1
fi

for ((i=1; i<=TIMEOUT; i++)); do
    if curl -sf "$URL" > /dev/null 2>&1; then
        echo "$LABEL ready in ${i}s"
        exit 0
    fi
    sleep 1
done

echo "::error::$LABEL failed to become ready within ${TIMEOUT}s"
exit 1
