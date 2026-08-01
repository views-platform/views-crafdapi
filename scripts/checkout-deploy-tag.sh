#!/usr/bin/env bash
# The deploy gate (epic #184, S4 — pattern: views-datafactory ADR-022).
#
# Production serves exactly the git tag named in the deploy-tag file — never a
# branch tip. Rollback is one line: write the previous tag, restart the service.
# systemd runs this as ExecStartPre, so every service (re)start passes the gate.
#
#   deploy:   echo v1.0.1 > ~/.views-faoapi-deploy-tag && sudo systemctl restart views-faoapi
#   rollback: echo v1.0.0 > ~/.views-faoapi-deploy-tag && sudo systemctl restart views-faoapi
#
# Fail-loud: a missing/empty tag file or an unknown tag stops the start entirely
# (systemd shows the failure) rather than silently serving whatever is checked out.
# The tag-file path matches src/views_crafdapi/version.py (FAOAPI_DEPLOY_TAG_FILE),
# so GET /version reports the same tag this gate deployed.
set -euo pipefail

TAG_FILE="${FAOAPI_DEPLOY_TAG_FILE:-$HOME/.views-faoapi-deploy-tag}"

if [ ! -s "$TAG_FILE" ]; then
    echo "FATAL deploy-gate: tag file missing or empty: $TAG_FILE" >&2
    echo "  Write the release tag to it, e.g.:  echo v1.0.0 > $TAG_FILE" >&2
    exit 1
fi

TAG="$(tr -d '[:space:]' < "$TAG_FILE")"
if [ -z "$TAG" ]; then
    echo "FATAL deploy-gate: tag file is blank: $TAG_FILE" >&2
    exit 1
fi

# Run from the repo root regardless of how we were invoked.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

git fetch --tags --quiet origin

if ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "FATAL deploy-gate: tag not found on origin: $TAG" >&2
    exit 1
fi

git checkout --quiet "refs/tags/$TAG"

# Sync the environment to the tag's lockfile (fast no-op when unchanged).
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
uv sync --frozen --no-dev --quiet

# Fail-loud: the checked-out tag must match the package's own declared version, so
# GET /version can never disagree with the deployed tag. Without this, a tag cut
# without bumping pyproject (or vice-versa) serves silently under the wrong label
# (epic #100 postmortem). Convention: tag "vX.Y.Z" == package version "X.Y.Z".
INSTALLED="$(uv run --frozen --no-dev --quiet python -c \
    'from importlib.metadata import version; print(version("views-faoapi"))')"
if [ "v$INSTALLED" != "$TAG" ]; then
    echo "FATAL deploy-gate: tag $TAG does not match package version v$INSTALLED" >&2
    echo "  Release tag and pyproject version have drifted — refusing to serve." >&2
    echo "  Fix: bump pyproject.toml + uv.lock to match the tag, or re-cut the tag." >&2
    exit 1
fi

echo "deploy-gate: serving tag $TAG (v$INSTALLED, $(git rev-parse --short HEAD))"
