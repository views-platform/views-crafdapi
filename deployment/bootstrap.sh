#!/usr/bin/env bash
# One-time server bootstrap for the production deployment model (epic #184, S3).
# Pattern: views-datafactory's Hetzner setup, adapted for an always-on API.
#
# Run in THREE parts, in order, from an admin (sudo-capable) account on the box.
# Part 2 PAUSES for one browser step (registering the deploy key on GitHub).
# This script only PREPARES — the actual traffic cutover is a separate, explicit
# runbook step (deployment/RELEASE_RUNBOOK.md), with the old unit kept as rollback.
#
# Usage:
#   bash bootstrap.sh part1     # create the service account + deploy key
#   bash bootstrap.sh part2     # (after GitHub step) clone, build env, credentials
#   bash bootstrap.sh part3     # install the systemd unit (does NOT switch traffic)
set -euo pipefail

SVC_USER="views-faoapi-deploy"
SVC_HOME="/home/${SVC_USER}"
REPO_SSH="git@github.com:views-platform/views-faoapi.git"
REPO_DIR="${SVC_HOME}/views-faoapi"
# þing-01 #275 / PLATFORM-001 D2: coordinates come from the OWNED, versioned registry
# (read, never copied), the key from an operator-provided secret slot. The retired copy-chain
# origin — a personal laptop dotenv grepped for credentials — is gone. Override the registry
# path with APPWRITE_REGISTRY if the pinned checkout lives elsewhere on the box.
APPWRITE_REGISTRY="${APPWRITE_REGISTRY:-${SVC_HOME}/views-appwrite/docs/ADRs/platform/coordinate_registry.toml}"
RELEASE_TAG="${RELEASE_TAG:-v1.0.0}"

part1() {
    echo "== Part 1: service account + deploy key =="
    if ! id "$SVC_USER" >/dev/null 2>&1; then
        sudo useradd -m -s /bin/bash "$SVC_USER"
        echo "created user $SVC_USER"
    else
        echo "user $SVC_USER already exists (ok)"
    fi
    sudo -u "$SVC_USER" bash -c '
        set -euo pipefail
        mkdir -p ~/.ssh && chmod 700 ~/.ssh
        if [ ! -f ~/.ssh/id_ed25519 ]; then
            ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "views-faoapi-deploy (read-only deploy key)"
        fi
        ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
        sort -u ~/.ssh/known_hosts -o ~/.ssh/known_hosts
    '
    echo
    echo ">>> BROWSER STEP NOW <<<"
    echo "Add this PUBLIC key as a READ-ONLY deploy key on github.com/views-platform/views-faoapi"
    echo "(Settings -> Deploy keys -> Add deploy key; do NOT tick write access):"
    echo
    sudo -u "$SVC_USER" cat "${SVC_HOME}/.ssh/id_ed25519.pub"
    echo
    echo "Then run:  bash bootstrap.sh part2"
}

part2() {
    echo "== Part 2: clone, toolchain, environment, credentials =="
    sudo -u "$SVC_USER" bash -c "
        set -euo pipefail
        if [ ! -d '${REPO_DIR}/.git' ]; then
            git clone '${REPO_SSH}' '${REPO_DIR}'
        fi
        cd '${REPO_DIR}'
        git fetch --tags --quiet origin
        # uv (same toolchain as views-datafactory)
        if ! command -v uv >/dev/null && [ ! -x \"\$HOME/.local/bin/uv\" ]; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
        export PATH=\"\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH\"
        echo '${RELEASE_TAG}' > \"\$HOME/.views-faoapi-deploy-tag\"
        bash scripts/checkout-deploy-tag.sh
    "
    # Credentials (þing-01 #275 / PLATFORM-001 D2): coordinates are READ from the owned registry
    # (never copied from anyone's .env — the retired copy-chain); the ONE secret comes from an
    # OPERATOR-PROVIDED environment slot, never from a .env, never echoed to a log.
    : "${APPWRITE_DATASTORE_API_KEY:?FATAL #275: the operator must provide APPWRITE_DATASTORE_API_KEY in the environment (secret slot, PLATFORM-001 §5) — it is NEVER sourced from a .env}"
    [ -f "${APPWRITE_REGISTRY}" ] || {
        echo "FATAL #275: coordinate registry not found at ${APPWRITE_REGISTRY}"
        echo "  PLATFORM-001 A3 must be present first (migration order: registry lands -> re-point)."
        echo "  Set APPWRITE_REGISTRY to a pinned checkout of views-appwrite's coordinate_registry.toml."
        exit 1
    }
    echo "building ${SVC_HOME}/.env.faoapi: coordinates from ${APPWRITE_REGISTRY}, secret from operator slot"
    # Emit non-secret coordinates from the registry, then append the operator secret. The pipe to
    # `sudo tee >/dev/null` writes the file root-owned WITHOUT echoing the key value to the terminal.
    # Use the venv's pinned Python (3.13, has `tomllib`) — it exists after the gate's `uv sync`
    # above, so the parse never depends on the box's system Python version.
    {
        "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/deployment/registry_to_env.py" "${APPWRITE_REGISTRY}"
        printf 'APPWRITE_DATASTORE_API_KEY=%s\n' "${APPWRITE_DATASTORE_API_KEY}"
    } | sudo tee "${SVC_HOME}/.env.faoapi" >/dev/null
    sudo chown "${SVC_USER}:${SVC_USER}" "${SVC_HOME}/.env.faoapi"
    sudo chmod 600 "${SVC_HOME}/.env.faoapi"
    N=$(sudo grep -c '^APPWRITE_' "${SVC_HOME}/.env.faoapi")
    echo "credentials file written (${N} APPWRITE_ lines: registry coordinates + 1 operator secret; values not displayed; expected >= 9)"
    echo "Then run:  bash bootstrap.sh part3"
}

part3() {
    echo "== Part 3: install the new systemd unit (no traffic switch yet) =="
    # Preserve the interim (June recovery) unit as the rollback path.
    if [ -f /etc/systemd/system/views-faoapi.service ] && \
       ! grep -q "views-faoapi-deploy" /etc/systemd/system/views-faoapi.service; then
        sudo cp /etc/systemd/system/views-faoapi.service /etc/systemd/system/views-faoapi-legacy.service
        echo "old unit preserved as views-faoapi-legacy.service (rollback path)"
    fi
    sudo cp "${REPO_DIR}/deployment/views-faoapi.service" /etc/systemd/system/views-faoapi.service
    sudo systemctl daemon-reload
    echo "unit installed. Traffic cutover is the NEXT runbook step (deliberate, reversible):"
    echo "  sudo systemctl restart views-faoapi     # now runs the new model"
    echo "  rollback: sudo cp views-faoapi-legacy -> views-faoapi, daemon-reload, restart"
}

case "${1:-}" in
    part1) part1 ;;
    part2) part2 ;;
    part3) part3 ;;
    *) echo "usage: bash bootstrap.sh {part1|part2|part3}" >&2; exit 1 ;;
esac
