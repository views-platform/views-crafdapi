#!/usr/bin/env bash
# The deploy gate (epic #184, S4 — pattern: views-datafactory ADR-022).
#
# Production serves exactly the git tag named in the deploy-tag file — never a
# branch tip. Rollback is one line: write the previous tag, restart the service.
# systemd runs this as ExecStartPre, so every service (re)start passes the gate.
#
#   deploy:   echo v0.1.1 > ~/.views-crafdapi-deploy-tag && sudo systemctl restart views-crafdapi
#   rollback: echo v0.1.0 > ~/.views-crafdapi-deploy-tag && sudo systemctl restart views-crafdapi
#
# Fail-loud: a missing/empty tag file or an unknown tag stops the start entirely
# (systemd shows the failure) rather than silently serving whatever is checked out.
# The tag-file path matches src/views_crafdapi/version.py (CRAFDAPI_DEPLOY_TAG_FILE),
# so GET /version reports the same tag this gate deployed.
set -euo pipefail

TAG_FILE="${CRAFDAPI_DEPLOY_TAG_FILE:-$HOME/.views-crafdapi-deploy-tag}"

if [ ! -s "$TAG_FILE" ]; then
    echo "FATAL deploy-gate: tag file missing or empty: $TAG_FILE" >&2
    echo "  Write the release tag to it, e.g.:  echo v0.1.0 > $TAG_FILE" >&2
    exit 1
fi

TAG="$(tr -d '[:space:]' < "$TAG_FILE")"
if [ -z "$TAG" ]; then
    echo "FATAL deploy-gate: tag file is blank: $TAG_FILE" >&2
    exit 1
fi

# Run from the repo root regardless of how we were invoked.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --force, because `git fetch --tags` will NOT overwrite a tag the box already has.
# 2026-08-15: v0.2.0 was cut without a version bump, the gate correctly refused it, the tag
# was re-cut on a fixed commit — and the box kept serving its stale copy of that tag name and
# failed identically. The operator had no way to see why: the tag file said v0.2.0, the remote
# said v0.2.0, and the code was a different v0.2.0. Re-cutting a tag is rare and should not be
# routine, but when it happens the box must converge on the remote rather than silently pin an
# object nobody can find any more.
#
# If GitHub cannot be reached (a reboot or crash while GitHub or DNS is down, or an outage on
# GitHub's side), the start goes on with the tags already on the server (#212). Failing instead
# would make every restart fail, and after 30 tries systemd gives up and the service stays down
# although the right tag is on disk.
#
# Only an answer from origin that says NO stops the start: a refused key or login (401, or a 403
# that says permission is denied; a bare 403 can also be rate limiting), a missing repository
# (404). Serving then could hide a re-cut tag. Every other failure (DNS,
# a dropped, refused, reset or stalled connection, TLS, a GitHub error, the time limit, or a
# fault on this server) starts from the tag already on the server, with a WARN and git's own
# words in the journal. Listing every way a network can fail does not work (each review round
# found more); the short list of refusals is stable. Trade-off: while the fetch fails, a tag
# re-cut on origin is served in its old form until a start whose fetch succeeds.
origin_refused() {
    grep -qiE 'permission denied \(|repository not found|does not appear to be a git repository|host key verification failed|authentication failed|too many authentication failures|could not read (username|password)|fatal: repository .* not found|invalid username or password|account is suspended|permission to .* denied|returned error: (401|404)' <<<"$1"
}

# A tool's own words, one journal line each, marked so the runbook's search finds them.
say_tool() {   # say_tool <tool> <text>
    [ -n "$2" ] || return 0
    printf '%s\n' "$2" | sed "s/^/deploy-gate: $1: /" >&2
}
say_git() { say_tool git "$1"; }

# Seconds the fetch may take (`CRAFDAPI_FETCH_TIMEOUT`, whole seconds from 1 to 90; anything else
# means 60, with a warning). systemd gives the whole start 180 seconds: the fetch takes at most
# 90 + 5 to kill it, the version check 20 + 5, and `uv sync` gets what is left of 170 seconds, at
# least 10 (at most 45 after a fetch that ran into its limit).
FETCH_SECONDS="${CRAFDAPI_FETCH_TIMEOUT:-60}"
case "$FETCH_SECONDS" in ''|*[!0-9]*|????*) FETCH_SECONDS=bad ;; esac
if [ "$FETCH_SECONDS" = bad ] || [ "$FETCH_SECONDS" -lt 1 ] || [ "$FETCH_SECONDS" -gt 90 ]; then
    FETCH_SECONDS=60
    echo "WARN deploy-gate: CRAFDAPI_FETCH_TIMEOUT=${CRAFDAPI_FETCH_TIMEOUT:-} is not 1 to 90 seconds; using $FETCH_SECONDS" >&2
fi
echo "deploy-gate: fetching tags, time limit $FETCH_SECONDS s" >&2
# A fetch killed earlier (by the time limit, or a crash) can leave its lock files, which make
# every later fetch fail. The locks a fetch takes (refs, packed-refs, shallow, FETCH_HEAD) are
# removed once they are more than a minute old, well inside systemd's 30 tries in 10 minutes.
# Assumed: no other git runs in this checkout at start (a hand-run `git gc` or `pack-refs`
# also takes packed-refs.lock). Any other lock (index.lock, HEAD.lock) is left alone.
find .git \( -path '.git/refs/*' -o -name packed-refs.lock -o -name shallow.lock -o -name FETCH_HEAD.lock \) \
    -name '*.lock' -mmin +1 -delete -print | sed 's/^/WARN deploy-gate: removed a leftover lock file: /' >&2 || true
# Never wait for a password or passphrase: a login problem must fail at once, not run into the
# time limit and pass for an outage.
export GIT_TERMINAL_PROMPT=0
# ssh connect and keep-alive limits, unless the environment or git's config already says how to
# run ssh (GIT_SSH, or `core.sshCommand`, may name the deploy key; GIT_SSH_COMMAND would
# override them).
if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -z "${GIT_SSH:-}" ] && [ -z "$(git config --get core.sshCommand || true)" ]; then
    export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2"
fi
# LC_ALL=C: git's messages in English, whatever the server's language, so the patterns match.
# `timeout -k 5` also kills git if it ignores the first signal (defensive: no test can make git
# ignore it). LC_ALL=C is not exercised either: the test machine has no git translations. GIT_TERMINAL_PROMPT=0 above is likewise not exercised by the tests, whose own
# environment already sets it.
set +e
# gc.auto=0: the fetch must not start git's background housekeeping, whose locks the clean-up
# above could otherwise remove while it still holds them.
fetch_err="$(LC_ALL=C timeout -k 5 "$FETCH_SECONDS" git -c gc.auto=0 fetch --tags --force --quiet origin 2>&1)"
fetch_rc=$?
set -e
ORIGIN_CHECKED=yes
ORIGIN_NOTE="origin checked"
if [ "$fetch_rc" -ne 0 ]; then
    if origin_refused "$fetch_err"; then
        echo "FATAL deploy-gate: origin refused the fetch (not an outage): a refused key or login, or a wrong repository. Fix it: serving now could serve a stale tag." >&2
        say_git "$fetch_err"
        exit 1
    fi
    ORIGIN_CHECKED=no
    if [ "$fetch_rc" -eq 124 ]; then
        ORIGIN_NOTE="origin NOT checked: the fetch ran past ${FETCH_SECONDS} s"
    elif [ "$fetch_rc" -eq 137 ]; then
        ORIGIN_NOTE="origin NOT checked: the fetch was killed (time limit or out of memory)"
    else
        ORIGIN_NOTE="origin NOT checked: the fetch failed"
    fi
    echo "WARN deploy-gate: could not fetch tags from origin (exit $fetch_rc; $ORIGIN_NOTE); using the tags already on this server." >&2
    say_git "$fetch_err"
fi

if ! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    if [ "$ORIGIN_CHECKED" = yes ]; then
        echo "FATAL deploy-gate: tag not found on origin: $TAG" >&2
    else
        echo "FATAL deploy-gate: tag not found on this server, and origin could not be checked: $TAG" >&2
    fi
    exit 1
fi

if ! checkout_err="$(git checkout --quiet "refs/tags/$TAG" 2>&1)"; then
    echo "FATAL deploy-gate: could not check out $TAG on this server." >&2
    say_git "$checkout_err"
    exit 1
fi

# Sync the environment to the tag's lockfile (fast no-op when unchanged).
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
# When origin was not checked the network may be down: sync offline, so a lockfile that needs
# downloads fails at once and says so, instead of retrying until systemd's start limit.
# The package sync and version check, each with a time limit (see the budget above), and a FATAL
# line naming the step that failed.
UV_OFFLINE=""
[ "$ORIGIN_CHECKED" = yes ] || UV_OFFLINE="--offline"
VERSION_SECONDS=20
# Whatever is left of the 170-second budget ($SECONDS counts since the gate started), keeping room
# for the version check: a large download over a working connection is not cut short.
SYNC_SECONDS=$(( 170 - SECONDS - 5 - VERSION_SECONDS - 5 ))
[ "$SYNC_SECONDS" -ge 10 ] || SYNC_SECONDS=10
# An empty $UV_OFFLINE adds no argument, so it is left unquoted below.
# shellcheck disable=SC2086
if ! sync_err="$(timeout -k 5 "$SYNC_SECONDS" uv sync --frozen --no-dev --quiet $UV_OFFLINE 2>&1)"; then
    if [ "$ORIGIN_CHECKED" = yes ]; then
        echo "FATAL deploy-gate: syncing the packages of tag $TAG failed (or ran past ${SYNC_SECONDS} s)." >&2
    else
        echo "FATAL deploy-gate: the fetch from origin failed, so packages were synced offline, and that failed: tag $TAG may need packages this server does not have (uv's words below)." >&2
    fi
    say_tool uv "$sync_err"
    exit 1
fi

# Fail-loud: the checked-out tag must match the package's own declared version, so
# GET /version can never disagree with the deployed tag. Without this, a tag cut
# without bumping pyproject (or vice-versa) serves silently under the wrong label
# (epic #100 postmortem). Convention: tag "vX.Y.Z" == package version "X.Y.Z".
# The version is the line Python marks with "VERSION="; nothing uv prints around it (a warning) is
# taken for the version, and on failure everything printed is shown.
# shellcheck disable=SC2086
if ! version_out="$(timeout -k 5 "$VERSION_SECONDS" uv run --frozen --no-dev --quiet $UV_OFFLINE python -c \
    'from importlib.metadata import version; print("VERSION=" + version("views-crafdapi"))' 2>&1)"; then
    echo "FATAL deploy-gate: reading the installed version of tag $TAG failed (or ran past ${VERSION_SECONDS} s)." >&2
    say_tool uv "$version_out"
    exit 1
fi
INSTALLED="$(sed -n 's/^VERSION=//p' <<<"$version_out" | tail -n 1)"
if [ "v$INSTALLED" != "$TAG" ]; then
    echo "FATAL deploy-gate: tag $TAG does not match package version v$INSTALLED ($ORIGIN_NOTE)" >&2
    if [ "$ORIGIN_CHECKED" = no ]; then
        echo "  Origin could not be checked, so the tag may have been re-made on GitHub: restart once the fetch works." >&2
    fi
    echo "  Release tag and pyproject version have drifted — refusing to serve." >&2
    echo "  Fix: bump pyproject.toml + uv.lock to match the tag, or re-cut the tag." >&2
    exit 1
fi

echo "deploy-gate: serving tag $TAG (v$INSTALLED, $(git rev-parse --short HEAD); $ORIGIN_NOTE)"
