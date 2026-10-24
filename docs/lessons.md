# Lessons learned

What building and running the original pipeline taught, in the order it taught it. Each lesson is now a mechanism in
the code with a test that pins it; this page is the why.

## Text

**A model with tools will use them.** A headless model call left on its default tools, handed transcripts to
summarize, acted on what the transcripts said. The writer runs with every tool disabled, no settings and no MCP
servers, and the ledger is fenced as data. Treat everything read from logs as untrusted input, always.

**Allowlist what may appear; never try to list what may not.** A denylist misses the repo you created this morning.
Lanes are opt-in, and the exclusion list only ever removes more.

**Hidden characters hide hits.** A zero-width space inside a name, full-width letters, a soft hyphen or a look-alike
dash all slip past a plain substring check. Every check reads the raw text and a normalized form, and a letter outside
ASCII fails the names check outright.

**A check that read a file and a consumer that read it again are two different files.** Between the gate's read and
the voice stage's read, a script can change. Every consumer reads the verdict, the script and the ledger once and uses
the very bytes it judged.

**Write the word budget from the voice, not from a guess.** The first real episode's host lines (137 words) ran 41
seconds of speech against a 60 second minimum, a third of the episode in padding. The cloned voice speaks about 3.3
words a second, so the budget became 165 to 210 words.

## Release

**An unreadable value is not a passing value.** A flag that could not be read back, a channel read that came back
empty, an oEmbed lookup that did not answer: none of them is evidence. Positive evidence, or stop.

**Verify before you claim, with a fresh read.** After saving, the driver reloads the video's page and reads everything
back, then asks YouTube's public oEmbed lookup. A private video must be refused; a public one must answer with an
embed of this very video. A 200 alone is not enough: a redirect can land on a page that answers 200. And the lookup is
eventual, so a public release is polled within a bound (ten tries, thirty seconds apart), then becomes a question for
you, never a retry.

**Never press twice.** A page script can press the button and then lose its answer (a timeout, a dropped connection).
If the attempt is recorded only after the answer, the rerun presses again. The receipt records `save_clicked` before
the press is sent; a rerun from there only verifies.

**Exact labels, per value.** Studio's done button reads Save for a private video and Publish for a public one. A
private run that sees Publish means Public is selected; a public run that sees Save means it is not. Each value
accepts only its own label, checked in the same page script that presses, so nothing can change between the proof and
the press.

**The page you expected may show no channel at all.** Studio sometimes opens with no channel loaded, which is not the
same as another channel. One re-open of the channel home on the driver's own tab, then a stop with a screenshot of
that tab, so you can see what it saw. And a URL pin can land on your own open tab when the driver's tab was sent to a
chooser: that counts as "no channel", never as an abort and never as permission to act in your tab.

**Tabs move under you.** Indices shift whenever someone opens or closes a tab. The driver finds its tab by a token it
put in the URL at creation, marks it, and re-checks the mark in the same batch as every action, so a shifted index can
only fail the guard.

**A failed connect can look like success.** When a DevTools connect fails, a browser automation tool may quietly start
a browser of its own, with none of your sign-ins, and every later command runs there. The driver proves the user agent
is a headed Chrome and that it is not marked automated before its first action.

**Do not close what you did not open.** On a browser you attached to, "close the browser" closes the person's browser
and every tab. The driver closes only its own tab and ends its own automation session by its process.

**One video per day needs two records.** A receipt in the day's folder resumes a half-finished upload; a ledger
outside every folder survives a deleted receipt or a second folder for the same day. Claim the day in both before the
bytes go up.

**Private first, public by a switch you flip.** The pipeline ran private for its first days: every episode was
watched before it went out, and every stop was read. Public mode came only after that, as a file the operator writes,
read by the stage and handed to the driver as an argument, so no other path can publish.

## Running it unattended

**A rehearsal must not share a folder with the real thing.** Dry runs got their own `<day>-dry` folder after a
rehearsal could overwrite a real day's files.

**A missed timer run must recap the day it missed.** With `Persistent=true` a missed 05:30 run fires on wake; if the
machine wakes before 05:30 the next morning, "yesterday" is the wrong day. `--day due` picks the day whose run was due
at the most recent 05:30.

**Tell the service manager the difference between "failed and reported" and "died".** A stage that failed and was
reported exits 5, which the unit counts as success; only a run that died before reporting triggers the failure unit,
so you never get two alerts for one stop. The failure handler and the notify stage share one lock on the receipt they
both write.

**Size the timeout from the code, not from memory.** The service timeout is pinned by a test that sums every stage's
own timeout constant, so raising one (a slow CPU voicing day, say) fails the test until the unit is raised too.
