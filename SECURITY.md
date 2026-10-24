# Security model

orion-on-the-clock turns private agent logs into a public video. Its job is to keep everything private out of that
video, and it is built to fail closed: when it cannot prove a line is safe, the line, or the whole day, does not ship.
It is not a sandbox, and it trusts the machine and the account it runs on.

## Defended

- **Private work reaching the script.** Only the ledger reaches the writer. Repos are allowlisted by lane; unlisted
  repos never appear in text, headline lanes appear only as an operator-written label and a count under an opaque id,
  and hard-excluded repos are left out even of the PR and task counts. A session that touched an excluded folder on
  any day, directly or through a helper session, gives no quotes or notes (it still counts as a session).
- **Secrets reaching the model provider.** Every text bound for the writer (a PR title, a PR body line, a quote, a
  session or teaser title) must pass the redactor, the slop gate and the secret scanner at collection, the same three
  the gate runs, so a credential-like value is dropped before the prompt is built, not only before the episode ships.
- **Prompt injection from logs and PR bodies.** Ledger text is fenced as ASCII JSON with `<` and `>` escaped, so no
  ledger text can close the fence; the prompt says the ledger is data; the writer runs with every tool disabled and
  without settings or MCP servers, and the gate judges whatever comes back.
- **Invented or edited agent words.** A confessional quote must be in the ledger and is re-read word for word from its
  source line (regular files only, under the known log roots, no symbolic links, no compressed segments), under the
  right record id and agent.
- **Hidden text.** Every text check reads the raw text and a normalized form (compatibility forms folded, invisible
  and combining characters dropped, look-alike dashes mapped), and a letter or digit outside ASCII fails the names
  check, so zero-width splits, full-width letters and homoglyphs cannot carry a blocked name or a long dash through.
- **Secrets and addresses in shipped text.** The redactor (token shapes, long high-entropy runs, emails, private and
  tailnet addresses, home paths, PR numbers, agent branch names, plus your own patterns) and the secret scanner (URLs
  outside a public host allowlist, credential fields, token shapes, encoded forms) must both pass.
- **Stale or swapped files.** A verdict carries the hashes of the script and the ledger it judged, and every consumer
  re-reads all three in one pass and refuses a mismatch. A new collect removes every file made from the old ledger.
- **A rehearsal touching real output.** A dry run works only in `<day>-dry/` and refuses a dry folder that resolves
  into the real one; seeding a rehearsal copies content and never writes through a symbolic link.
- **Silent misconfiguration.** settings.toml, lanes.json and embargo.json are read strictly: unknown keys, wrong types
  and missing required keys stop the run.
- **Leaks through diagnostics.** Stage reasons and gate evidence hold codes, counts and check names only; paths are
  replaced before `status.json` is written.
- **Unreviewed text on the air.** Voice and render read the script and ledger only through the text verdict, in one
  read, so text edited after the gate is refused. The render holds voices.json and the voiced lines to the voice
  manifest, and its verdict binds the episode's hash to the script, ledger and voices it was made from.
- **The wrong voice.** The host's clone comes only from the recording `voices.json` names, checked against its sha256
  prefix before any audio is made (here and again on a remote host). An agent voice can never carry a reference
  recording, its conditioning is set fresh before every sentence, and the runner refuses a plan in which an agent's
  conditioning matches the host's by content.
- **Filter injection through voice settings.** An ffmpeg chain is checked against an allowlist of audio filters and
  characters (no file names, no graph syntax) and reaches ffmpeg as one argument.
- **Markup in on-screen text.** Every value reaches the composition as JSON with `<`, `>` and `&` escaped and is drawn
  with text-only DOM calls; a long dash anywhere on screen stops the build.
- **A second copy of a day on your channel.** The day is claimed in an uploads ledger outside every day's folder and in
  the day's receipt before the bytes go up; a rerun resumes the same draft, a lost receipt or a second folder for the
  day is a stop for you, and a save is recorded before it is pressed, so a run cut off mid-save never presses again.
- **An unreviewed or swapped episode reaching the channel.** Publish reads the text verdict and the render verdict once
  each and binds them (the render must be of the accepted script and ledger), then uploads a staging copy of the exact
  bytes the render verdict judged; the driver re-hashes the file before and after handing it to Studio.
- **A public release nobody chose.** Uploads are private unless `publish.json` says exactly `"visibility": "public"`;
  the driver takes the visibility only from its command line, can save only private or public (no unlisted or
  scheduled path exists), proves the choice and the exact button label in the script that presses, and needs a fresh
  Public read and an oEmbed embed of the video before it calls a public release done. A day keeps the visibility it
  was uploaded with.
- **Acting in the wrong tab, account or browser.** The driver connects only to a loopback DevTools endpoint, proves it
  drives a headed Chrome that is not marked automated, opens and marks a tab of its own and re-checks that mark in the
  same batch as every action, presses only pinned targets, stops when Studio is signed out or on another channel, and
  never closes your browser. Diagnostics never echo the endpoint.
- **Someone else's GPU job.** The remote TTS preflight is read only: any GPU process not on your allow list means the
  remote is busy and the run voices locally. Nothing remote is paused, unloaded or stopped, and a remote run ends under
  its own timeout even if ssh drops.

## Not defended

- **Anything on your account.** A process running as you can read your agent logs, your config and the state folder,
  and can replace the checks folder or an adapter file. The pipeline trusts its own machine.
- **Your adapters.** An adapter or tracker file runs as your code with your rights, in the same process. Its output
  is checked, its behavior is not, and adapter session counts are taken as given (no exclusion applies to a count).
- **An open vocabulary.** The names check matches the names you list, with separators ignored; it does not catch
  leetspeak or an unlisted nickname. Keep `blocked_names` and `checks.redact_patterns` current, and watch each private
  upload before it goes public.
- **Numbers written in words.** The gate grounds every digit in the ledger; a count spelled out in words is covered
  only by the writer's rules and your review.
- **The model provider.** The writer sends the ledger (already filtered) to Claude through your own Claude Code
  login.
- **What a viewer infers.** The show describes real work; a careful viewer learns what you build and when.
- **The remote TTS host.** It receives the day's lines (gated text) and your voices.json, runs the runner as your ssh
  user, and must hold your voice reference. Trust it as you trust this machine.
- **The browser's DevTools port.** Anything running as your user (or, if you forward the port, anyone who can reach
  it) can drive the signed-in Chrome through it, with no password: every account signed in there. Give the pipeline a
  Chrome profile of its own with only the channel's account, and keep the port on the loopback (docs/runbook.md).
- **YouTube and Google.** The driver uses the Studio web page as you would, in your session. A page change can stop it
  (it stops rather than guess); detection of automation and the platform's terms are yours to keep within. A public
  release is public at once: watch private days before you turn public mode on.
- **Your notify command.** It receives every item (titles, links, the work folder path) and runs as your code.
- **HyperFrames and its browser.** The render runs a headless Chromium on a page built from gated data. Without
  `render.sandbox` it has your network and your user's rights; the sandbox example in docs/how-it-works.md removes the
  network and contains its processes.
- **Your media kit and models.** Fonts, sounds, GSAP and the TTS and Whisper models are loaded as given; their licenses
  and provenance are yours to check.

## Reporting

Report a vulnerability privately to the repository owner through GitHub's private vulnerability reporting.
