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

SVC_USER="views-crafdapi-deploy"
SVC_HOME="/home/${SVC_USER}"
REPO_SSH="git@github.com:views-platform/views-crafdapi.git"
REPO_DIR="${SVC_HOME}/views-crafdapi"
# þing-01 #275 / PLATFORM-001 D2: coordinates come from the OWNED, versioned registry
# (read, never copied), the key from an operator-provided secret slot. The retired copy-chain
# origin — a personal laptop dotenv grepped for credentials — is gone. Override the registry
# path with APPWRITE_REGISTRY if the pinned checkout lives elsewhere on the box.
APPWRITE_REGISTRY="${APPWRITE_REGISTRY:-${SVC_HOME}/views-appwrite/docs/ADRs/platform/coordinate_registry.toml}"
RELEASE_TAG="${RELEASE_TAG:-v0.1.0}"

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
            ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "views-crafdapi-deploy (read-only deploy key)"
        fi
        ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
        sort -u ~/.ssh/known_hosts -o ~/.ssh/known_hosts
    '
    echo
    echo ">>> BROWSER STEP NOW <<<"
    echo "Add this PUBLIC key as a READ-ONLY deploy key on github.com/views-platform/views-crafdapi"
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
        echo '${RELEASE_TAG}' > \"\$HOME/.views-crafdapi-deploy-tag\"
        bash scripts/checkout-deploy-tag.sh
    "
    # Credentials (þing-01 #275 / PLATFORM-001 D2): coordinates are READ from the owned registry
    # (never copied from anyone's .env — the retired copy-chain); the ONE secret comes from an
    # OPERATOR-PROVIDED environment slot, never from a .env, never echoed to a log.
    : "${APPWRITE_DATASTORE_API_KEY:?FATAL #275: the operator must provide APPWRITE_DATASTORE_API_KEY in the environment (secret slot, PLATFORM-001 §5) — it is NEVER sourced from a .env}"
    [ -f "${APPWRITE_REGISTRY}" ] || {
        echo "FATAL #275: coordinate registry not found at ${APPWRITE_REGISTRY}"
        echo "  PLATFORM-001 A3 must be present first (migration order: registry lands -> re-point)."
        echo "  Set APPWRITE_REGISTRY to a pinned views-appwrite checkout of coordinate_registry.toml,"
        echo "  or (ADR-035 Decision 2) a copy of the versioned registry file placed on the box."
        exit 1
    }
    # Record WHICH registry version built this box (crafdapi#34 finding 4 / ADR-035): a copied
    # registry file is otherwise indistinguishable from a stale one, and the contract has moved
    # several versions in days. Read [meta].version with the venv's tomllib — never grep, a
    # commented key looks identical to a live one (register C-57). Stamped into the env below so
    # "which registry built this box?" is grep-able, not folkloric.
    #
    # The venv lives in ${SVC_USER}'s 0750 home, which this admin account cannot traverse — so the
    # two Python invocations run AS ${SVC_USER} (`sudo -u`), the venv's owner. The registry copy in
    # /tmp is world-readable, so ${SVC_USER} can read it. (This is the bug crafdapi's first real run
    # of the registry bootstrap surfaced: faoapi never hit it because its env was hand-built.)
    _reg_version="$(sudo -u "$SVC_USER" "${REPO_DIR}/.venv/bin/python" - "${APPWRITE_REGISTRY}" <<'PY' 2>/dev/null || echo unknown
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh).get("meta", {}).get("version", "unknown"))
PY
)"
    echo "building ${SVC_HOME}/.env.crafdapi: coordinates from ${APPWRITE_REGISTRY} (registry version ${_reg_version}), secret from operator slot"
    # Emit the registry-version stamp, then the non-secret coordinates (emitted AS ${SVC_USER} —
    # see above), then append the operator secret. The pipe to `sudo tee >/dev/null` writes the file
    # root-owned WITHOUT echoing the key value to the terminal.
    # Build into a private temp file and MOVE it into place only once the content is known
    # good. Writing `sudo tee` straight at the live path truncated it BEFORE knowing whether
    # registry_to_env.py would succeed: under `set -euo pipefail` a registry the service user
    # cannot read, malformed TOML, or a missing coordinate value aborted the function with the
    # live file already emptied and the chown/chmod below never reached — leaving a root-owned
    # 0644 one-line file and a Restart=always unit looping on _validate_appwrite_env, with the
    # previously working credentials destroyed. Re-running part2 is exactly what the runbook
    # tells an operator to do when a step fails, so this was reachable on the recovery path.
    _tmp_env="$(sudo mktemp "${SVC_HOME}/.env.crafdapi.XXXXXX")"
    # 0600 before a secret is written, not after: the old ordering left the key at root's
    # umask (0644) for the window between `tee` and `chmod`.
    sudo chmod 600 "$_tmp_env"
    if ! {
        printf 'APPWRITE_REGISTRY_VERSION=%s\n' "${_reg_version}"
        sudo -u "$SVC_USER" "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/deployment/registry_to_env.py" "${APPWRITE_REGISTRY}"
        printf 'APPWRITE_DATASTORE_API_KEY=%s\n' "${APPWRITE_DATASTORE_API_KEY}"
    } | sudo tee "$_tmp_env" >/dev/null; then
        sudo rm -f "$_tmp_env"
        echo "FATAL: could not build the credentials file (registry read or secret emit failed)." >&2
        echo "       ${SVC_HOME}/.env.crafdapi is UNCHANGED — the running service keeps working." >&2
        return 1
    fi
    N=$(sudo grep -c '^APPWRITE_' "$_tmp_env")
    if [ "$N" -lt 9 ]; then
        sudo rm -f "$_tmp_env"
        echo "FATAL: built credentials file has only ${N} APPWRITE_ lines (expected >= 9)." >&2
        echo "       ${SVC_HOME}/.env.crafdapi is UNCHANGED — the running service keeps working." >&2
        return 1
    fi
    sudo chown "${SVC_USER}:${SVC_USER}" "$_tmp_env"
    # Same filesystem, so this is atomic: readers see the old file or the new one, never a
    # half-written one.
    sudo mv "$_tmp_env" "${SVC_HOME}/.env.crafdapi"
    echo "credentials file written (${N} APPWRITE_ lines: 1 registry-version stamp + registry coordinates + 1 operator secret; values not displayed; expected >= 9)"
    echo "Then run:  bash bootstrap.sh part3"
}

part3() {
    echo "== Part 3: install the new systemd unit (no traffic switch yet) =="
    # Preserve the interim (June recovery) unit as the rollback path.
    if [ -f /etc/systemd/system/views-crafdapi.service ] && \
       ! grep -q "views-crafdapi-deploy" /etc/systemd/system/views-crafdapi.service; then
        sudo cp /etc/systemd/system/views-crafdapi.service /etc/systemd/system/views-crafdapi-legacy.service
        echo "old unit preserved as views-crafdapi-legacy.service (rollback path)"
    fi
    sudo cp "${REPO_DIR}/deployment/views-crafdapi.service" /etc/systemd/system/views-crafdapi.service
    sudo systemctl daemon-reload
    echo "unit installed. Traffic cutover is the NEXT runbook step (deliberate, reversible):"
    echo "  sudo systemctl restart views-crafdapi     # now runs the new model"
    echo "  rollback: sudo cp views-crafdapi-legacy -> views-crafdapi, daemon-reload, restart"
}

case "${1:-}" in
    part1) part1 ;;
    part2) part2 ;;
    part3) part3 ;;
    *) echo "usage: bash bootstrap.sh {part1|part2|part3}" >&2; exit 1 ;;
esac
