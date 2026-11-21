#!/usr/bin/env bash
# Sets up a new server for crafdapi, once. The steps around it (the domain, nginx, the
# certificate, starting the service) are in deployment/RELEASE_RUNBOOK.md, section 4.
#
# Run the three parts in order, from your own sudo account on the server:
#   bash bootstrap.sh part1     # create the service account and its GitHub deploy key
#   bash bootstrap.sh part2     # (after adding the key on GitHub) get the code, build, write credentials
#   bash bootstrap.sh part3     # install the systemd service (does not start it)
set -euo pipefail

SVC_USER="views-crafdapi-deploy"
SVC_HOME="/home/${SVC_USER}"
REPO_SSH="git@github.com:views-platform/views-crafdapi.git"
REPO_DIR="${SVC_HOME}/views-crafdapi"
# The Appwrite coordinates (project, bucket, table ids) are read from the views-appwrite
# registry, copied to /tmp first (RELEASE_RUNBOOK.md, section 4). Set APPWRITE_REGISTRY if it is elsewhere.
APPWRITE_REGISTRY="${APPWRITE_REGISTRY:-/tmp/coordinate_registry.toml}"
RELEASE_TAG="${RELEASE_TAG:-}"  # required by part2, e.g. RELEASE_TAG=v0.7.0

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
    [ -n "${RELEASE_TAG}" ] || { echo "FATAL: say which version to install, e.g. RELEASE_TAG=v0.7.0 bash bootstrap.sh part2" >&2; exit 1; }
    # Credentials: the coordinates are read from the registry; the one secret, our reader key
    # (ADR-040), comes from the environment, where you put it with `read -rs`. Never from a file.
    : "${APPWRITE_DATASTORE_API_KEY:?FATAL: put our reader key in the environment first (RELEASE_RUNBOOK.md, section 4, step 4)}"
    [ -f "${APPWRITE_REGISTRY}" ] || {
        echo "FATAL: the views-appwrite registry was not found at ${APPWRITE_REGISTRY}" >&2
        echo "  Put a copy of coordinate_registry.toml on the server and set APPWRITE_REGISTRY to it." >&2
        exit 1
    }
    sudo -u "$SVC_USER" bash -c "
        set -euo pipefail
        if [ ! -d '${REPO_DIR}/.git' ]; then
            git clone '${REPO_SSH}' '${REPO_DIR}'
        fi
        cd '${REPO_DIR}'
        git fetch --tags --quiet origin
        # uv, the Python package manager
        if ! command -v uv >/dev/null && [ ! -x \"\$HOME/.local/bin/uv\" ]; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
        export PATH=\"\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH\"
        echo '${RELEASE_TAG}' > \"\$HOME/.views-crafdapi-deploy-tag\"
        bash scripts/checkout-deploy-tag.sh
    "
    # Record which registry version built this server, so it can be looked up later. The Python
    # runs as the service account, because only it can read the venv in its home.
    _reg_version="$(sudo -u "$SVC_USER" "${REPO_DIR}/.venv/bin/python" - "${APPWRITE_REGISTRY}" <<'PY' 2>/dev/null || echo unknown
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh).get("meta", {}).get("version", "unknown"))
PY
)"
    echo "building ${SVC_HOME}/.env.crafdapi: coordinates from ${APPWRITE_REGISTRY} (registry version ${_reg_version}), secret from operator slot"
    # Build the credentials file in a temporary file, private (0600) before the key is written,
    # and move it into place only when it is complete. If anything fails, the old file is left
    # untouched and a running service keeps working. The key is never shown on screen.
    _tmp_env="$(sudo mktemp "${SVC_HOME}/.env.crafdapi.XXXXXX")"
    sudo chmod 600 "$_tmp_env"
    if ! {
        printf 'APPWRITE_REGISTRY_VERSION=%s\n' "${_reg_version}" &&
        sudo -u "$SVC_USER" "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/deployment/registry_to_env.py" "${APPWRITE_REGISTRY}" &&
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
    # Every setting the code needs must have a value, or the service would install and then fail
    # at every start. The check prints names only, never values.
    if ! _absent="$(sudo cat "$_tmp_env" | sudo -u "$SVC_USER" "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/deployment/check_required_env.py")"; then
        sudo rm -f "$_tmp_env"
        echo "FATAL: the registry at ${APPWRITE_REGISTRY} (version ${_reg_version}) gives no value for: ${_absent:-<the check could not run>}" >&2
        echo "       Deploy against a registry edition that carries them." >&2
        echo "       ${SVC_HOME}/.env.crafdapi is UNCHANGED — the running service keeps working." >&2
        return 1
    fi
    sudo chown "${SVC_USER}:${SVC_USER}" "$_tmp_env"
    sudo mv "$_tmp_env" "${SVC_HOME}/.env.crafdapi"
    echo "credentials file written (${N} APPWRITE_ lines: 1 registry-version stamp + registry coordinates + 1 operator secret; values not displayed)"
    echo "Then run:  bash bootstrap.sh part3"
}

serverkey() {
    echo "== The server's read key (crafdapi-server, ADR-044) =="
    # The key comes from the environment, where you put it with `read -rs`; it is never shown.
    # It goes into a file of its own, private to the service account, apart from .env.crafdapi.
    : "${APPWRITE_CRAFDAPI_SERVER_API_KEY:?FATAL: put the server key in the environment first (RELEASE_RUNBOOK.md, section 5)}"
    _tmp_key="$(sudo mktemp "${SVC_HOME}/.env.crafdapi-server-key.XXXXXX")"
    sudo chmod 600 "$_tmp_key"
    printf 'APPWRITE_CRAFDAPI_SERVER_API_KEY=%s\n' "${APPWRITE_CRAFDAPI_SERVER_API_KEY}" | sudo tee "$_tmp_key" >/dev/null
    sudo chown "${SVC_USER}:${SVC_USER}" "$_tmp_key"
    sudo mv "$_tmp_key" "${SVC_HOME}/.env.crafdapi-server-key"
    echo "server key file written (value not displayed). Restart the service to use it."
}

part3() {
    echo "== Part 3: install the systemd service (it is not started yet) =="
    sudo cp "${REPO_DIR}/deployment/views-crafdapi.service" /etc/systemd/system/views-crafdapi.service
    sudo systemctl daemon-reload
    echo "service installed. Next: nginx and the certificate, then start it (RELEASE_RUNBOOK.md, section 4)."
}

case "${1:-}" in
    part1) part1 ;;
    part2) part2 ;;
    part3) part3 ;;
    serverkey) serverkey ;;
    *) echo "usage: bash bootstrap.sh {part1|part2|part3|serverkey}" >&2; exit 1 ;;
esac
