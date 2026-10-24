#!/usr/bin/env bash
# orion-on-the-clock.service entry. systemd starts with a minimal PATH, so build the one the run needs: python3, claude
# and agent-browser in ~/.local/bin, node for HyperFrames from the newest nvm install when there is one, then the
# system folders. Then run the day that was due, in the foreground (`--day due`: yesterday once 05:30 local time has
# passed, the day before when the machine wakes earlier). A missed 05:30 run fires once on wake (Persistent=true) and
# recaps the MISSED day only. Config and state folders keep their defaults unless the unit sets ORION_CLOCK_CONFIG or
# ORION_CLOCK_STATE.
set -euo pipefail

# HyperFrames runs a headless Chromium; it must never wait on a display that may be gone.
unset DISPLAY WAYLAND_DISPLAY

NODE_BIN=""
if [ -d "$HOME/.nvm/versions/node" ]; then
    NODE_VER="$(find "$HOME/.nvm/versions/node" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V | tail -1 || true)"
    if [ -n "$NODE_VER" ] && [ -d "$HOME/.nvm/versions/node/$NODE_VER/bin" ]; then
        NODE_BIN="$HOME/.nvm/versions/node/$NODE_VER/bin:"
    fi
fi
export PATH="$HOME/.local/bin:${NODE_BIN}/usr/local/bin:/usr/bin:/bin"

# Fail closed without node: exit non-zero here so the failure handler fires, before the pipeline starts.
if ! command -v node > /dev/null 2> /dev/null; then
    echo "orion-on-the-clock: no node on PATH (HyperFrames needs it), refusing to start" >> /dev/stderr
    exit 1
fi

# When HyperFrames cannot find a Chromium of its own, point it at one:
# export HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium

cd "$(dirname "$(readlink -f "$0")")/.."
exec python3 -u -m daily.run --day due
