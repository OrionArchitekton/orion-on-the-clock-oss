#!/usr/bin/env bash
# OnFailure handler for orion-on-the-clock.service: the run died before the notify stage could report (an entry script
# refusal, a crash, a kill). daily/unit_failed.py sends one item through the notify delivery (your notify.command, or
# the state folder and the journal) and records its id in that day's notify.json, so a later report expires it. A
# failed send exits non-zero and the journal keeps it.
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$here/daily/unit_failed.py"
