# Safety design

This pipeline turns private agent logs into a public video every morning with nobody watching. Each safety property
below is a place where it would be easy to ship something you did not mean to, and the mechanism that stops it. The
rule behind all of them: **a check passes only on positive evidence.** A check that cannot run, crashes, times out or
answers out of shape fails, and the day stops instead of shipping a guess. SECURITY.md lists what is and is not
defended; this page explains why the design looks the way it does.

## 1. The writer sees only the ledger

Collect reads the day's logs and writes `ledger.json`, and that file is the only thing the writer is shown.

- **Lanes are an allowlist, never a denylist.** A repo appears only if `lanes.json` lists it. A `public` lane may be
  the story, in detail. A `headline` lane is private work: it reaches the writer only as the label and the reason you
  wrote and a count, under an opaque id, never as a repo name, a PR title or a body. A repo you did not list never
  appears in any text (it still counts toward the day's PR total). A new repo stays out until you add it.
- **Hard excludes win over everything.** A `hard_exclude` entry never appears in text and is left out even of the PR
  and closed-task counts. A session that ever touched an excluded folder, on any day, directly or through a helper
  session, gives no quotes and no notes.
- **Blocked names and the embargo** are matched with separators ignored and from a word start, on the raw text and on
  a normalized form (compatibility forms folded, invisible and combining characters dropped, look-alike dashes
  mapped), so `Garden-Shed`, `garden shed` and a zero-width split all hit. An embargo lifts only when you set
  `active` to false: never on a date.
- **Untrusted text stays data.** Logs and PR bodies are fenced inside the prompt as escaped JSON so nothing can close
  the fence, the prompt says the ledger is data, and the writer runs as one headless `claude -p` call with every tool
  disabled and no settings or MCP servers. Whatever it writes, the gate judges.

## 2. The gate fails closed, and its verdict binds everything after it

The gate runs fourteen checks on the script (shape, the story and the quote in the ledger, the quote re-read word for
word from its source line, the redactor, AI-tell phrases and long dashes, secret shapes, blocked names, the embargo,
the word budget, no code, every number grounded in the ledger) and writes `verdict.json` with the sha256 of the script
and of the ledger it judged.

**Every later stage reads the script only through that verdict, in one read**: the verdict, the script and the ledger
are each read once, and the checks are made on the very bytes that are then used. A script edited after the gate,
by a person or a process, no longer matches and is refused: text nobody reviewed is never voiced, rendered or uploaded.
The render verdict in turn binds the episode's own hash to the script, ledger and voices it was made from, and publish
holds all of them against each other before it starts a browser (it uploads a staging copy of the exact bytes the
render verdict judged).

## 3. Receipts and the uploads ledger: one video per day, never two

An upload that half happened is the most dangerous state in the system: a second attempt would put a second copy of the
day on your channel. So:

- **The day is claimed before the bytes go up.** The driver appends `upload_started` to the uploads ledger
  (`<state>/uploads/ledger.jsonl`, outside every day's folder) and writes the receipt (`upload.json`) before it hands
  the file to Studio. A crash at any later moment leaves a record.
- **The ledger survives a lost receipt.** A day with any ledger row needs you (`ledger_has_day`), even if its receipt
  or its whole folder is gone. A ledger that cannot be read needs you too: it cannot rule out an earlier upload. Its
  rows are written whole (every byte, then fsync) or not at all.
- **The receipt resumes, it never restarts.** Each step advances a stage (`uploaded`, `details`, `save_clicked`,
  `saved`, then `private_verified` or `public_verified`). A rerun resumes the same draft by its video id. A started
  upload with no recorded video id, a receipt for another render, or one that cannot be read stops for you before any
  browser.
- **A press is recorded before it is sent.** `save_clicked` is written just before the page script that presses the
  done button runs, so a run cut off at that moment (a timeout, a kill, a lost answer) is never pressed again: the next
  run only verifies.
- **Verified means verified.** A terminal stage is written only after a fresh reload of the video's Studio page reads
  back the title, the description, both flags and the visibility, and oEmbed agrees (refused for private; an embed of
  this very video for public). A terminal day is never touched again in either mode.

## 4. The browser: one tab, marked, and nothing it was not told to press

The driver works in your signed-in browser, next to whatever else you have open, so it must never act anywhere else.

- **The endpoint is the loopback, and the browser is proven.** `browser.endpoint` and any leased endpoint must be a
  DevTools address on 127.0.0.1, localhost or ::1. After it connects, the driver needs a parsed answer showing a Chrome
  user agent that is not headless and `navigator.webdriver` exactly false: a failed connect can silently start a
  browser of agent-browser's own, with none of your sign-ins, and acting there would be acting as nobody.
- **Its own tab, marked at creation.** Each command opens a new tab with a unique token in its URL, finds the one tab
  carrying the token, and marks it (`window.name` and `sessionStorage`) in a batch that first re-checks the token, so
  a tab you open or close at the same moment cannot receive the mark. A tab that is already open is never reused.
- **Every action re-proves the tab in the same batch.** Each page read and each action selects the tab and runs a
  guard (the URL is this channel's Studio or this video's page, and the tab carries this run's mark) in one
  agent-browser batch, so an index that shifted between finding and acting can only fail the guard. Closing the tab
  is the same: select, re-check the mark, close.
- **Pinned targets only.** There is no generic click. Each action is a helper with one hard-coded target (Next, Show
  more, Edit draft, the two details radios by exact name, the done button, a dialog's own Close or Got it), and a
  static test parses the driver's source and fails if a forbidden word (`unlisted`, `schedule`, a click outside the
  allowed helpers, `type`, `fill`, `eval`, `getattr`) appears where it should not, or if a pinned definition is
  shadowed by a second one.
- **It lets go without closing anything of yours.** When it is done it closes only its own tab, stops its own
  agent-browser session (in a socket folder of its own, by its daemon's pid, after checking that pid is agent-browser),
  and with a pool gives the lease back. It never sends `close` to the browser: on a browser it attached to, that can
  close your browser and every tab in it.
- **It stops for you instead of guessing.** A sign-in page, another channel, or a channel chooser is a needs-you stop
  (after one re-open for an empty channel read), with a private screenshot of its own tab so you can see what it saw.
  It never types a password or a code.

## 5. Private by default, and public is an explicit switch

YouTube shows a public video to the world at once, and a viewer's copy, a notification or a recommendation cannot be
taken back. A private upload costs you one look and one tap. So the default is private, and public is a decision you
make once you trust a run of private days:

- **The switch is a file you write**, `publish.json`, read once per run by the publish stage. Only `"visibility":
  "public"`, exactly, turns it on. Every other state of the file (missing, unreadable, malformed, `"Public"`,
  `"unlisted"`, a directory, a FIFO that would block the read) is private, and none of them is an error that could
  stop a private upload.
- **The driver never reads it.** The stage passes `--visibility public` or `--visibility private` on the command line;
  the driver takes the visibility only from that argument (default private), and a static test pins that the setter
  and the save are called only with that threaded argument, in the one upload flow.
- **Only two values exist.** The driver can save PRIVATE or PUBLIC and nothing else: unlisted and scheduled releases
  have no code path, and the done button must read exactly `Save` for private or `Publish` for public, each refusing
  the other's label, in the same page script that presses it.
- **A day keeps its visibility.** Turning public mode on never publishes a day that was uploaded private, and a
  half-finished upload keeps the visibility it began with (`visibility_mismatch` otherwise).
- **A public release that cannot be proved is an ask, not a retry.** After the save attempt, any public run that cannot
  show the Public label and an oEmbed embed of this video stops with `public_unconfirmed` and saves nothing again.
- **The disclosure is always there.** Every upload sets YouTube's "altered or synthetic content" flag (read back
  before the save and again after it), and every description ends with your `show.disclosure` line exactly once.

## 6. Diagnostics never leak

Stage reasons, gate evidence and notify items hold codes, counts, check names, stage names, numbers and links built
from a video id: never log text, script text, quote text, exception text or paths. A reason that is not a short code
is dropped before it reaches an item, and an unsafe episode title (an address, a phone number) is left out. The
driver never echoes a DevTools endpoint or a connect command's output.

## 7. A rehearsal never reaches the real world

`--dry` works in `<day>-dry/` and refuses a dry folder that resolves into the real one. A dry voice is silence, a dry
render writes `-dry` names, a dry publish writes its item and never starts the driver (even with public mode on and a
passing real render on disk), and a dry notify sends nothing.
