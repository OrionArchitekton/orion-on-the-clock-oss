# Spec: a daily Short of yesterday's agent work

The behavior this release commits to, in domain language. It is the reference for later changes: a change that alters
a scenario below updates this file in the same change.

## Purpose

Every morning, with nobody doing anything, turn what your AI coding agents built yesterday into a short, entertaining
vertical video. A layperson should understand what got built and why it matters; a technical viewer should be
impressed by the real numbers and the real agent behavior behind it. Recaps are the product, and character is found
in the logs, never cast.

## Terms (one word per concept)

| Term | Meaning | Not |
| --- | --- | --- |
| **episode** | One daily vertical Short about one day of work | daily video, post |
| **agent** | An AI coding agent whose work is recapped: Claude Code, Codex, or one you plug in | harness, bot |
| **lane** | One repo, with a disclosure level | project, source |
| **disclosure level** | `public` (detail allowed), `headline` (what and why only), or absent (excluded) | privacy tier |
| **ledger** | The day's record of work after the allowlist: the only input the writer sees | digest, data |
| **story** | The one build an episode is about | topic, feature |
| **scoreboard** | The day's counts: PRs merged, tasks closed, sessions per agent | stats |
| **confessional** | An agent's own log line, word for word, read in that agent's voice and labeled with its source | quote, cutaway |
| **host** | The narrator: your own cloned voice, from a recording you supply | narrator, VO |
| **gate** | The full set of checks on the script; it writes a **verdict** with evidence for every check | QA |
| **quiet day** | A day with no story that passes the gate; no episode ships | skip day |
| **item** | The one report of a day's outcome, sent to your notify command or written to the state folder | notification |
| **public mode** | Your switch under which a run that passes every check releases the episode public itself; off means private uploads | auto publish |

## Decisions

1. **Format:** a daily vertical episode of 60 to 75 seconds.
2. **Voices:** the host is your own cloned voice, from a reference recording you supply and consent to. Each agent
   gets a distinct synthetic voice that reads only its own verbatim log lines. No agent voice imitates a person.
3. **Disclosure:** public lanes in detail; headline lanes only as the label and reason you wrote; everything else out.
   Transcript quotes and session titles may come from any lane you listed, never from one you excluded. Repo, host and
   branch names, secrets and anything under the embargo stay out of every episode.
4. **Approach:** a deterministic pipeline with one bounded writer call. Rejected: an agent session each morning (it
   would hold untrusted input, private access and publishing at once, and cost and vary from day to day) and a pure
   template with no model (repetitive, the pattern platforms treat as inauthentic content).
5. **Where it runs:** a morning timer on the machine that holds the agent logs and the renderer. A missed run catches
   up once when the machine wakes, for the missed day only.
6. **Publishing:** through YouTube Studio in your own signed-in browser, PRIVATE by default: you watch it and publish
   it yourself. With public mode on, a run that passes every check releases the episode PUBLIC itself and tells you
   afterwards. Unlisted and scheduled releases are impossible.
7. **The writer** is one headless `claude -p` call on your own Claude Code login, with no tools and a spending cap.
8. **Reporting:** one item per day and outcome, delivered by a command you choose, or written to the state folder.
9. **Viewer value:** every non-quiet episode carries a "Steal this" takeaway and a teaser from work still in progress.

## Hard constraints

1. **Allowlist, never denylist.** A lane absent from the lane list is excluded; a new repo stays excluded until you add
   it.
2. **Hard excludes win.** An excluded repo or folder never appears in text, is left out of the counts, and taints the
   sessions that touched it (no quotes, no notes).
3. **Embargo.** Embargoed terms stay out until you lift the embargo by hand; it never lifts on a date alone.
4. **Verbatim or nothing.** Every confessional is the exact text of a real log line with a resolvable source
   reference, re-read from that source at the gate.
5. **Positive evidence.** Nothing uploads without a complete passing verdict for the text and for the render, bound to
   each other. A missing, partial, stale or unreadable verdict means no upload.
6. **The writer has no tools**, and its environment carries no provider API key.
7. **No screen recordings.** Every visual is drawn from data, because terminals and browsers can show what the
   allowlist removed.
8. **No long dashes** (en, em or horizontal bar) in anything the pipeline writes.
9. **Disclosure on every upload.** The synthetic content flag is set, and the description ends with your disclosure
   line.
10. **Private config stays out of the repository.** Lanes, excludes, embargo terms, settings and media live in your
    config and kit folders; the repository carries examples with made-up names.
11. **Coverage is reported, never assumed.** A reader that cannot read its source drops that agent for the day, the run
    goes on, and the item names what was missing. A missing source narrows coverage; it never loosens privacy.
12. **One video per day.** A rerun, a lost receipt or a second work folder never uploads a second copy.

## Episode shape (60 to 75 s, vertical)

1. **Cold open:** the day's best moment, a real agent line or a striking number.
2. **Scoreboard:** the day's counts, split across agents; an unknown count is left out, never shown as zero.
3. **Story:** one build in plain words: what it does, why it matters, an everyday comparison.
4. **Steal this:** one concrete takeaway a viewer can apply to their own work with agents.
5. **Confessional (optional):** one agent reads its own log line in its voice, labeled with the agent and the time.
6. **Out:** a teaser from work in progress, and a like and subscribe beat.

The host lines total 165 to 210 words. Never a code block, command, file path or config on screen.

## Stages

collect, write, gate, voice, render, publish, notify, in that order. Every stage writes its output to disk, so any
stage can be rerun alone from the one before it; a run resumes from the last completed stage.

## Scenarios

Each is an end-to-end slice, demoable alone, in dependency order. Every acceptance line is pinned by a test unless
marked **(live)**: something only a run against the real service can show.

### S1. One day, one rendered episode

Given a day, the pipeline produces a rendered episode on disk from that day's logs, with no upload.

- A listed lane appears in the ledger; an unlisted one never does. Every ledger item carries a source reference.
- The writer call launches with no tools; the script has the fixed shape; a script that does not parse gets one
  repair attempt, then the day fails with its reason recorded.
- The episode runs 60 to 75 seconds, is vertical, is loudness normalized, has captions matching the spoken words, and
  comes with a contact sheet.

### S2. The gate fails closed

- A credential, a non-public repo or host name, an embargoed term or a blocked name anywhere in shipped text fails.
- A confessional that differs from its source line by any word, or whose source does not resolve, fails.
- A script outside the word budget, with code, or with a number not in the ledger fails.
- Every failure names its check and evidence, and no episode is made that day.

### S3. Voices

- The host is voiced only from your reference recording, checked by hash before every run.
- An agent never speaks in the host's voice: its conditioning is set fresh for every line and proved different.
- Each agent always gets the same voice, color and glyph.

### S4. Private upload and the report

Given a rendered episode with passing verdicts, the run uploads it private to your channel and reports it.

- The upload is private, carries the synthetic content flag and the disclosure line, and its title, description and
  both flags are read back before the save and again after it.
- The upload goes only to the channel in your settings; Studio on another channel, or signed out, stops for you.
- The browser endpoint is the loopback only; the driver acts only in a tab it opened and marked, and never closes your
  browser.
- Rerunning the day never uploads a second copy, even after a crash mid-upload or a lost receipt.
- The item carries the Studio link, the contact sheet path, the episode title, the length, the loudness and the gate
  counts, and asks you to review.
- **(live)** The upload works against the real Studio page in your browser.

### S5. Quiet days and failures

- A day with no story that passes produces a quiet-day item and no upload.
- A failed stage produces an item with the stage, a short reason code and the exact rerun command.
- A run that dies before it can report produces an item from the failure handler, keyed by the day it had chosen.
- A new outcome for the same day supersedes the earlier item; returning to an outcome sends a revision.

### S6. Public mode

Given a rendered episode with passing verdicts and public mode on, the run releases it public, proves it is live, and
tells you afterwards.

- Public mode is on only when your switch says `public`, in exactly that word. Anything else (missing, unreadable,
  malformed, another value) means a private upload, never an error. A change takes effect on the next run, and a
  rehearsal never uploads.
- The browser step learns the visibility only from the run that starts it. It can choose only private or public.
- The visibility is proved in the same step that presses the button: public alone chosen, no schedule picker, and the
  exact label Studio shows for a public release. A private save needs its own exact label and refuses the public one.
- The save is recorded as attempted before the press is sent, so a run cut off from that point on never presses again.
- After saving, a fresh Studio read must show the label Public and the public embed lookup must return an embed of this
  very video within about five minutes. A public run that cannot prove this stops for you without saving again.
- A day already uploaded keeps its visibility; a half-finished upload keeps the one it began with.
- You get one FYI titled "Published: <episode title>" with the watch link; nothing asks you to act.
- **(live)** The real Studio page shows the Publish label, the published dialog, and the embed lookup answers.

### S7. Rehearsal

- `--dry` works in its own folder and never creates, changes or deletes a file of the real day.
- A dry run voices silence, renders under dry names, writes the upload item without starting a browser, and reports
  nothing.

## Test seams

1. **Ledger builder** (privacy): raw source records and the lane list in, the ledger out.
2. **Gate** (safety): script, ledger, sources and embargo in, verdict out.
3. **The run in dry mode** (end to end): a day in, an episode and an upload item out, with fake TTS, Whisper and
   renderer.
4. **The Studio driver against fake browsers** (release): a scripted page, a fake tab list and a fake DOM for the page
   scripts; the publish stage against a fake child; notify against a fake command.

Upload and the report are exercised with stand-ins in tests; the real Studio page can only be proven by a live run.

## Out of scope for 0.1.0

A weekly recap video, other platforms, on-camera footage, screen recordings, same-day episodes, comments and replies,
and the YouTube Data API (uploads from an unaudited API project are locked private).
