# Runbook

How to set the pipeline up to run every morning, what to do when a run stops, and how to turn public mode on and off.
Commands run from the repository root. Paths use the defaults (config `~/.config/orion-on-the-clock/`, state
`~/.local/state/orion-on-the-clock/`); `ORION_CLOCK_CONFIG` and `ORION_CLOCK_STATE` move them.

## Before the first real run

1. Config: copy `config.example/*` to your config folder and fill in every file (docs/configuration.md). Leave
   `publish.json` private.
2. Rehearse the whole run: `python3 -m daily.run --day yesterday --dry`. It works in `<day>-dry/`, calls the writer,
   voices silence, renders, writes the `upload-item.json` the driver would get, and reports nothing. Watch
   `episode-dry.mp4`.
3. Set up the browser (next section) and prove it: `python3 release/studio.py preflight` opens a tab of its own on
   your channel's Studio, checks the channel, closes the tab and lets go of the browser.
4. One attended real run: `python3 -m daily.run --day yesterday`. Watch the private upload in Studio before anything
   else.
5. Install the timer (below).

## The browser

The Studio driver drives a Chrome you start and sign in to yourself, over Chrome's DevTools protocol, through
[agent-browser](https://github.com/vercel-labs/agent-browser) (Apache-2.0; install it so `agent-browser` is on the
PATH the timer builds, for example in `~/.local/bin`).

**A DevTools port is full control of the browser behind it**, every signed-in account included, with no password. So:

- give the pipeline a Chrome profile of its own (`--user-data-dir`; recent Chrome refuses remote debugging on the
  default profile anyway), signed in only to the Google account that holds the channel;
- keep the port on the loopback: Chrome listens on 127.0.0.1 by default. Never pass `--remote-debugging-address`, never
  forward or proxy the port, and check with `ss -ltnp | grep 9222` that it shows `127.0.0.1:9222`. `browser.endpoint`
  accepts only a loopback address, and the driver refuses anything else again before it connects;
- anything else running as your user can use that port too while Chrome runs. The pipeline trusts its own machine
  (SECURITY.md).

Start it like this (the binary may be `google-chrome-stable`, `chromium` or `chromium-browser`):

```bash
google-chrome --user-data-dir="$HOME/.local/share/orion-on-the-clock/chrome" \
  --remote-debugging-port=9222 --disable-blink-features=AutomationControlled --no-first-run
```

Recent Chrome marks every page as automated (`navigator.webdriver` true) while a DevTools port is open, and Google
refuses to sign in a browser that says so. `--disable-blink-features=AutomationControlled` turns that marker off, and
the driver refuses a browser that still reports it (it also refuses a headless one: the fallback agent-browser starts
when a connect fails has no sign-ins). Sites can tell automation by other means; what you automate on YouTube is
yours to keep within its terms.

Then, once, by hand: sign in to the channel's Google account and open the channel in YouTube Studio. Keep this Chrome
running in a visible window (not minimized; on a machine without a desktop, a virtual display such as Xvfb):
Studio's dialogs lay out only in a tab that is in front. The driver opens a tab of its own for each command, acts
only in that tab, closes it afterwards, and never closes the browser.

**A leased browser instead.** If your browsers live in a pool, set `browser.lease` and `browser.release` to commands
that lease one and give it back. The lease command prints `{"ok": true, "data": {"endpoint": "<loopback ws url>"}}`,
or `{"ok": false, "code": "BUSY"}` while another holder has it (the driver waits up to 40 minutes, then stops with
`browser_busy`); the release command prints `{"ok": true, "data": {"stop": "clean"}}` (`signal` or `kill` also count as
given back, with a warning). [orion-browser](https://github.com/OrionArchitekton/orion-browser-oss) (MIT) speaks exactly
this:

```toml
[browser]
lease = ["orion-browser", "--json", "lease", "my-browser", "--holder", "orion-on-the-clock", "--minutes", "30"]
release = ["orion-browser", "--json", "release", "my-browser", "--holder", "orion-on-the-clock"]
```

## Install the timer

The units in `systemd/` are examples: copy them and point `ExecStart` at your checkout.

```bash
cp systemd/orion-on-the-clock.service systemd/orion-on-the-clock.timer systemd/orion-on-the-clock-failed.service \
   ~/.config/systemd/user/
# edit ExecStart in the two .service files: %h/src/orion-on-the-clock-oss is only the example path
systemctl --user daemon-reload
systemd-analyze --user verify ~/.config/systemd/user/orion-on-the-clock.service
systemctl --user enable --now orion-on-the-clock.timer
systemctl --user list-timers orion-on-the-clock.timer
```

`loginctl enable-linger "$USER"` lets user timers run while you are logged out; the Chrome above must then run
somewhere a logged-out session can reach (a virtual display).

What runs when:

- The timer fires at 05:30 local time (`Persistent=true`, so a missed run fires once on wake). The entry script runs
  `--day due`: the day whose run was due at the most recent 05:30 in `settings.toml` `timezone` (yesterday once 05:30
  has passed, the day before when the machine wakes earlier). A whole missed day beyond that is not made
  automatically; run it by hand. When the machine's own zone differs from `timezone`, add the zone to `OnCalendar`.
- The entry script clears `DISPLAY` and `WAYLAND_DISPLAY` (HyperFrames' headless Chromium needs neither), builds PATH
  (`~/.local/bin`, the newest nvm node if there is one, the system folders), refuses to start without node (exit 1, so
  the failure handler fires), and runs `python3 -m daily.run --day due` in the foreground. If HyperFrames cannot find a
  Chromium, uncomment `HYPERFRAMES_BROWSER_PATH` there.
- Exit codes of `daily.run`:

  | Code | Meaning | Unit result |
  | --- | --- | --- |
  | 0 | the episode was uploaded and verified, or a quiet day | success |
  | 1 | a stage failed (or needed you, or the config was bad) and was NOT reported; or settings or the lock failed | failed: the failure unit sends its item |
  | 2 | usage error | failed |
  | 3 | another run of that day holds the lock | success (nothing to report) |
  | 5 | a stage failed (or needed you, or the config was bad) and the notify stage reported it | success (the item is the report) |

- `TimeoutStartSec=32400` (9 hours) covers every stage at its own timeout (a test sums them). A day takes minutes when
  Chatterbox has a GPU and an hour or more on the CPU.
- `systemctl --user stop orion-on-the-clock.service`: the running stage is recorded failed with `signal:SIGTERM`, the
  driver lets go of the browser, notify still reports, and the next start resumes from `status.json`.

## If something stopped (start here)

1. Find the day's item: with no `notify.command`, `ls -t ~/.local/state/orion-on-the-clock/notify/ | head` and the
   journal (`journalctl --user -u orion-on-the-clock -n 80`), else wherever your command sends it. Its title names the
   stage and a short reason code; a stopped stage carries the exact rerun command.
2. No item at all, or one titled "the timer run failed before it could report": the run died before notify (the failure
   unit sent that item, keyed by the day the run had chosen). Read the journal.
3. Read the day's `status.json` for the failing stage and its code, then look the code up (docs/how-it-works.md for
   collect to render, below for publish and notify).
4. Fix the cause and rerun from that stage (below). A rerun skips the stages already done.
5. A `needs_you` code means only you can clear it. Never retry around one.

```bash
cat ~/.local/state/orion-on-the-clock/$(date -d yesterday +%F)/status.json
ls ~/.local/state/orion-on-the-clock/$(date -d yesterday +%F)/
```

## Rerun a day by hand

```bash
python3 -m daily.run --day 2026-10-16                          # from where it stopped
python3 -m daily.run --day 2026-10-16 --from-stage publish     # publish and notify again
python3 -m daily.run --day 2026-10-16 --dry --from-stage voice # a media rehearsal in 2026-10-16-dry/
```

A rerun of publish never uploads a second copy: a verified receipt makes it `already_private:<id>`, and a half-finished
upload resumes the same draft. A manual run while the timer's run is active exits 3; wait for it.

## Public mode

Off by default. When it is on and every check passes, the run releases the day's episode PUBLIC through the same Studio
flow, proves it is live, and sends one FYI ("Published: <title>") instead of the ready-for-review item.

- **On:** set `"visibility": "public"` in `publish.json` in your config folder. Only that exact lowercase value turns it
  on; `"Public"`, `"unlisted"`, a typo, bad JSON or an unreadable file all mean private, and never an error.
- **Off:** set `"visibility": "private"` or delete the file.
- It takes effect on the next run. The publish stage reads it once, records the mode in `upload-item.json` and starts
  the driver with `--visibility public` or `--visibility private`; the driver never reads the file. A dry run records
  the mode and still never starts a browser.
- A day already uploaded keeps its visibility: `already_private:<id>` stays private and `already_public:<id>` stays
  public, whatever the switch says. Change one in Studio by hand if you want to.
- A half-finished upload keeps the visibility it began with. Rerunning it with the other setting stops with
  `needs_you:visibility_mismatch` before any browser: put the switch back for that rerun, or settle the draft in Studio.
- Released only on proof: the page script that presses the done button first reads Public alone chosen, no schedule
  picker, and an enabled button reading exactly `Publish` (a private save needs exactly `Save`; each refuses the
  other's label). After the press, a fresh Studio read must show the label Public and oEmbed must return an embed for
  this very video within ten tries thirty seconds apart.
- `needs_you:public_unconfirmed`: a public run could not prove the release after the save attempt (a read that did not
  match, a flag, or oEmbed never confirming). Nothing was saved again and the visibility was not touched. Open
  `https://youtube.com/shorts/<video_id from upload.json>` signed out, or check Studio. If it is live, rerun
  `--from-stage publish` with public mode still on: it only verifies again and finishes `public_verified`. If it is
  not, decide in Studio.

Why public is a switch you flip, not a default: docs/safety.md.

## Needs-you codes

Stop and look; never retry around these. Each is `needs_you:<code>` in `status.json` (the driver prints the code as
its last line, `NEEDS_YOU <code>`, in `publish.log`).

| Code | What happened | What to do |
| --- | --- | --- |
| `signed_out` | the browser landed on a Google sign-in page | sign in by hand in that browser (an agent never types a password or a code), then `--from-stage publish` |
| `wrong_channel` | Studio opened on another channel, or on none after one re-open, or off Studio (a channel chooser) | look at the stop screenshot first (below); switch the browser's YouTube account to the channel by hand, then rerun publish |
| `receipt_other_render` | the day already has an upload of a different render | check Studio and decide which video stands; do not rerun publish |
| `upload_started_no_video` | a crash after the upload began but before its video id was recorded | Studio may hold a draft of that day: find it in Content, keep or delete it by hand; the ledger blocks a second copy |
| `bad_video_id`, `receipt_not_resumable` | the receipt is in a state the driver does not resume from | as above: check Studio by hand |
| `receipt_corrupt` | `upload.json` is unreadable | do not delete or rewrite it; check Studio for the day's video first |
| `receipt_other_day`, `receipt_identity_changed`, `upload_locked` | a receipt for another day, or another upload command holds the day's lock | look for a running `release/studio.py`; wait for it; if none, treat as a corrupt receipt |
| `draft_gone` | resuming found no Edit draft button: the draft was saved or removed | check Studio; `publish.log` has the driver's lines |
| `ledger_has_day`, `unreadable_ledger` | the uploads ledger already has the day, or cannot be read | check Studio by hand; never delete the ledger |
| `visibility_mismatch` | a draft began with the other visibility | see Public mode |
| `public_unconfirmed` | a public release could not be proved | see Public mode |

`browser_busy` (exit 3 of the driver) is not a needs-you stop: a leased browser stayed busy for 40 minutes. Rerun
publish later.

## Stop screenshots

When the switch to your channel stops (at the channel check, or because the tab landed off Studio), the driver first
saves a screenshot of its own tab in `~/.local/state/orion-on-the-clock/_studio-stops/<UTC time>-<code>.png` and
prints `studio-stop screenshot: <path>` (or `none (<reason>)`) in `publish.log`. Look at it first: it shows what Studio
was showing. The folder is 0700 and the files 0600, nothing is uploaded or sent anywhere, the newest 20 are kept, and
you may delete them freely.

## Notify: your own command

With `notify.command` empty, each item is written to `<state>/notify/<item id>.json` and printed as one JSON line (the
journal keeps it). To send items elsewhere, set the command (an argument list, never run through a shell). It gets one
JSON message on stdin and has 60 seconds:

```json
{"schema": "orion-on-the-clock/notify/v1", "op": "add",
 "item": {"key": "daily:2026-10-16:ready", "kind": "approval", "outcome": "ready", "day": "2026-10-16",
          "title": "Daily episode 2026-10-16: ready in Studio (private), review and publish or delete",
          "pointer": "https://studio.youtube.com/video/<id>/edit", "severity": null, "rerun": null,
          "meta": {"episode": "...", "duration": "61.7 s", "loudness": "-14.1 LUFS", "checks": "text gate 14/14, render gate 5/5"}}}
```

For an `add` it must exit 0 and print `{"item_id": "<id>", "state": "open"}` (`acked` also counts as delivered;
`expired` or `done` means you had already closed that item, and it is never sent again). The id is yours to choose
(letters, digits, `.`, `_`, `:` and `-`, up to 64 characters), and comes back in an `expire` message when a later
outcome of the same day supersedes the item: `{"schema": ..., "op": "expire", "item_id": "<id>", "key": "...",
"reason": "superseded"}`, which needs only exit 0. A command that fails, times out or answers anything else fails the
stage (`notify_exit_<n>`, `notify_timeout`, `notify_bad_answer`), and the next run sends the item again.

A desktop notification, for example (`notify.command = ["~/bin/notify-desktop.py"]`):

```python
#!/usr/bin/env python3
"""Show each item with notify-send and answer as the notify stage expects."""
import hashlib, json, subprocess, sys

msg = json.load(sys.stdin)
if msg["op"] == "add":
    item = msg["item"]
    urgency = "critical" if item["kind"] == "approval" else "normal"
    subprocess.run(["notify-send", "-u", urgency, item["title"], item["pointer"]], check=True)
    print(json.dumps({"item_id": "d-" + hashlib.sha1(item["key"].encode()).hexdigest()[:12], "state": "open"}))
```

Item kinds: `approval` asks something of you (`ready`: the episode waits private in Studio; `needs_you`; the failure
unit's item), `fyi` does not (`published`, `quiet`, `failed-<stage>`, `config_error`).

## Reason codes: publish and notify

- publish: `settings_missing`, `missing:channel.id`, `text_not_accepted:<code | dry>`, `script_unreadable`,
  `script_quiet`, `script_day_mismatch`, `no_title`, `bad_disclosure` (`show.disclosure` has a long dash, an angle
  bracket or a control character), `render_not_accepted:<code>`, `staging_mismatch`, `upload_item_unwritable`,
  `dry_run`, `already_private:<id>`, `already_public:<id>`, `uploaded_private:<id>`, `uploaded_public:<id>`,
  `browser_busy`, `studio_abort` (the driver stopped: `publish.log` says where), `studio_usage`, `studio_exit_<n>`,
  `studio_signal`, `studio_timeout`, `studio_not_runnable`, `studio_receipt_missing`, and the needs-you codes above. A
  verified upload counts whatever the driver's exit code; a dirty exit after it shows in `publish.log` as
  `release_problem:<code>`.
- notify: `notified: <outcome>`, `already notified: <outcome>`, `closed outside: <outcome>`, `dry run: nothing sent`,
  `nothing_to_report`, `settings_missing`, `upload_receipt_invalid`, `notify_exit_<n>`, `notify_timeout`,
  `notify_not_runnable`, `notify_bad_answer`, `notify_receipt_unwritable`, `notify_busy`.

## Attended discovery (when Studio changes)

Studio's page changes from time to time; the driver stops rather than guess. To look at the dialog where it stopped:

```bash
ORION_CLOCK_KEEP_ON_ABORT=1 python3 release/studio.py upload ~/.local/state/orion-on-the-clock/<day>
python3 release/studio.py look      # the dialog's heading, buttons and the visibility read-back
python3 release/studio.py release   # always end with this
python3 release/studio.py verify ~/.local/state/orion-on-the-clock/<day> [--visibility public]   # a fresh read only
```

Every stop before the save leaves at most a private draft, and the receipt resumes it. A new selector or label is a
code change with its tests: every click target in `release/studio.py` is pinned by `daily/tests/test_studio.py`.

## Stop or roll back

- Pause: `systemctl --user disable --now orion-on-the-clock.timer`.
- Remove: also delete the three unit files from `~/.config/systemd/user/`, then `systemctl --user daemon-reload`.
- Turn public mode off first if it is on. Uploads keep their visibility in Studio; change or delete one there by hand.
- Keep the uploads ledger. Day folders can go one by one (dry folders freely; a real day's folder holds the upload
  receipt, so remove it only once its video is settled).
