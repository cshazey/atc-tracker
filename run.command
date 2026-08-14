#!/bin/bash
# ATC Tracker — macOS launcher with auto-update supervisor.
#
# Double-click in Finder, or: bash run.command
#
# This script is a supervisor. It starts the tracker as a child process and
# then, once a minute, checks whether main has moved on GitHub. If it has, it
# pulls, announces the update in the Discord #commands channel, and restarts
# the tracker — while this script itself keeps running. That matters: the
# supervisor never re-execs itself, so a bad commit can never leave you with a
# launcher that will not start.
#
#   AUTO_UPDATE=0 bash run.command     supervise, but never pull
#   NO_SUPERVISOR=1 bash run.command   run the tracker directly, no wrapper

cd "$(dirname "$0")" || exit 1

UPDATE_INTERVAL="${UPDATE_INTERVAL:-60}"   # seconds between update checks
AUTO_UPDATE="${AUTO_UPDATE:-1}"
BRANCH="${BRANCH:-main}"

# ── Load credentials from .env ────────────────────────────────────────────────
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
else
    echo "⚠  No .env file found."
    echo "   Copy .env.example to .env and fill in your credentials."
    echo ""
fi

# ── Create virtual environment on first run ───────────────────────────────────
if [ ! -d venv ]; then
    echo "First run — creating virtual environment..."
    python3 -m venv venv
    echo "Installing dependencies (this may take a few minutes on first run)..."
    venv/bin/pip install --upgrade pip --quiet
    venv/bin/pip install -r requirements.txt --quiet
    echo "✓ Setup complete."
    echo ""
fi

PY=venv/bin/python

notify() {  # notify <title> <body> [colour]
    "$PY" notify_discord.py --title "$1" --body "$2" --colour "${3:-0x5865F2}" \
        >/dev/null 2>&1 || true
}

# ── Where can the map be reached? ─────────────────────────────────────────────
# Tailscale hands out 100.64.0.0/10 addresses, so scanning interfaces beats
# guessing where the tailscale binary was installed.
tailscale_ip() {
    ifconfig 2>/dev/null \
        | grep -Eo 'inet 100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+' \
        | awk '{print $2}' | head -1
}

print_access() {
    local port="${ADSB_WEB_PORT:-8099}"
    local ts; ts="$(tailscale_ip)"
    echo "────────────────────────────────────────────────────────────"
    if [ "${ADSB_WEB_ENABLED:-0}" = "1" ]; then
        echo "  Live aircraft map:"
        echo "    local      http://localhost:${port}/"
        if [ -n "$ts" ]; then
            echo "    Tailscale  http://${ts}:${port}/     ← from your other devices"
        else
            echo "    Tailscale  (not detected — is Tailscale running?)"
        fi
        [ -n "${ADSB_WEB_TOKEN:-}" ] && echo "    (append ?token=… — ADSB_WEB_TOKEN is set)"
    else
        echo "  Live map disabled. Set ADSB_WEB_ENABLED=1 in .env to turn it on."
    fi
    echo "────────────────────────────────────────────────────────────"
    echo ""
}

# ── Direct mode: no supervisor, no auto-update ────────────────────────────────
if [ "${NO_SUPERVISOR:-0}" = "1" ]; then
    print_access
    echo "Starting ATC Tracker — press K to toggle keywords, Q to quit."
    echo ""
    exec "$PY" atc_tracker.py
fi

# ── Supervisor ────────────────────────────────────────────────────────────────
APP_PID=""
SHUTTING_DOWN=0

cleanup() {
    SHUTTING_DOWN=1
    if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
        kill -TERM "$APP_PID" 2>/dev/null
        wait "$APP_PID" 2>/dev/null
    fi
    echo ""
    echo "Stopped."
    exit 0
}
trap cleanup INT TERM

updates_available() {
    [ "$AUTO_UPDATE" = "1" ] || return 1
    git rev-parse --git-dir >/dev/null 2>&1 || return 1
    git fetch --quiet origin "$BRANCH" 2>/dev/null || return 1
    local local_sha remote_sha
    local_sha="$(git rev-parse HEAD 2>/dev/null)"
    remote_sha="$(git rev-parse "origin/${BRANCH}" 2>/dev/null)"
    [ -n "$remote_sha" ] && [ "$local_sha" != "$remote_sha" ]
}

print_access
echo "Starting ATC Tracker — press K to toggle keywords, Q to quit."
if [ "$AUTO_UPDATE" = "1" ]; then
    echo "Auto-update: watching origin/${BRANCH} every ${UPDATE_INTERVAL}s."
else
    echo "Auto-update: disabled."
fi
echo ""

while true; do
    "$PY" atc_tracker.py &
    APP_PID=$!
    RESTART_FOR_UPDATE=0

    # Watch the child and the remote at the same time.
    while kill -0 "$APP_PID" 2>/dev/null; do
        sleep "$UPDATE_INTERVAL"
        [ "$SHUTTING_DOWN" = "1" ] && break
        kill -0 "$APP_PID" 2>/dev/null || break

        if updates_available; then
            SUMMARY="$(git log --oneline --no-decorate HEAD.."origin/${BRANCH}" \
                        2>/dev/null | head -10)"
            COUNT="$(git rev-list --count HEAD.."origin/${BRANCH}" 2>/dev/null)"
            echo ""
            echo "↻ ${COUNT} new commit(s) on origin/${BRANCH} — updating…"
            notify "⬇️ Update available" \
                   "${COUNT} new commit(s) on \`${BRANCH}\`:\`\`\`
${SUMMARY}
\`\`\`
Pulling and restarting the tracker…" "0xF1C40F"

            # Only restart if the pull actually succeeded. A dirty tree or a
            # conflict must leave the running tracker alone rather than kill it
            # and fail to come back.
            if git pull --ff-only --quiet origin "$BRANCH" 2>/dev/null; then
                if [ -f requirements.txt ]; then
                    venv/bin/pip install -r requirements.txt --quiet 2>/dev/null || true
                fi
                RESTART_FOR_UPDATE=1
                kill -TERM "$APP_PID" 2>/dev/null
                break
            else
                echo "⚠  Pull failed (local changes or a diverged branch) — staying on the current version."
                notify "⚠️ Update failed" \
                       "Could not fast-forward to \`origin/${BRANCH}\`. The tracker is still running on the old version; resolve it on the host." \
                       "0xE74C3C"
            fi
        fi
    done

    wait "$APP_PID" 2>/dev/null
    EXIT_CODE=$?
    APP_PID=""
    [ "$SHUTTING_DOWN" = "1" ] && break

    if [ "$RESTART_FOR_UPDATE" = "1" ]; then
        NOW_SHA="$(git rev-parse --short HEAD 2>/dev/null)"
        echo "↻ Restarting on ${NOW_SHA}…"
        echo ""
        notify "✅ Updated and restarting" \
               "Now running \`${NOW_SHA}\`. The tracker is coming back up." "0x2ECC71"
        print_access
        continue
    fi

    # The tracker exited on its own — a clean quit, or a crash.
    if [ "$EXIT_CODE" = "0" ]; then
        echo "Tracker exited."
        break
    fi
    echo "⚠  Tracker exited with code ${EXIT_CODE} — restarting in 10s. Ctrl-C to stop."
    notify "⚠️ Tracker crashed" \
           "Exited with code \`${EXIT_CODE}\`. Restarting in 10 seconds." "0xE74C3C"
    sleep 10
done
