"""Tab-pinned browser primitives for the Studio driver (release/studio.py): every page-changing command and every page
read first proves, in the same agent-browser batch, that the tab it acts on is MY tab: its URL is this channel's
Studio (or this video's page) and, once release/studio.py has marked it, it carries this run's mark. Nothing here
clicks on its own; studio.py holds every fixed-purpose action.

Driven through agent-browser (https://github.com/vercel-labs/agent-browser, Apache-2.0), attached to the browser by
release/studio.py. Module state (the session name, the channel, the mark) is set by release/studio.py before use."""
import base64
import json
import os
import re
import subprocess
import sys

S = "oclock-unattached"   # the agent-browser session name; release/studio.py sets it once it has attached
CHAN = None   # the channel id every guard pins (release/studio.py sets it from settings.toml channel.id)
VIDEO_ID = None   # set once the upload dialog shows the id, so /video/<id>/ pages pass the guard too
EXTRA = []   # more URL prefixes my tab may be on
MARK = None   # the mark (window.name, sessionStorage) set on MY tab: the only way it is told from your own tabs
# AGENT_BROWSER_SOCKET_DIR is added by release/studio.py: this driver's sessions live in a folder of their own.
ENV = {**os.environ, "AGENT_BROWSER_CONTENT_BOUNDARIES": "1"}
MUTATING = {"open", "upload", "press", "click", "scrollintoview", "back", "type", "fill"}
DIALOG = "document.querySelector('ytcp-uploads-dialog')"
HEADING = (f"(() => {{ const d = {DIALOG}; const r = d && d.getBoundingClientRect(); "
           "return r && r.width > 0 && d.querySelector('h1') ? d.querySelector('h1').innerText.trim() : null; })()")


def _raw(*a, stdin=None, t=90):
    return subprocess.run(["timeout", str(t), "agent-browser", "--session", S, *a], input=stdin,
                          capture_output=True, text=True, env=ENV).stdout


def url():
    lines = [ln for ln in _raw("eval", "location.href", t=30).splitlines() if ln.strip() and not ln.startswith("--- ")]
    try:
        return json.loads(lines[0]) if lines else ""
    except ValueError:
        return lines[0] if lines else ""


def mine(u):
    return (CHAN is not None and u.startswith(f"https://studio.youtube.com/channel/{CHAN}")
            or (VIDEO_ID is not None and u.startswith(f"https://studio.youtube.com/video/{VIDEO_ID}/"))
            or any(u.startswith(p) for p in EXTRA))


def my_tab():
    """Index of MY tab, found by URL each time (indices shift when you open or close tabs). Exactly one tab may
    match: if you have the same page open, acting on the first match could act in your tab; with a mark set, the mark
    tells two matching tabs apart."""
    hits = []
    for attempt in range(4):  # a redirect mid-load can briefly show another URL
        hits = []
        for line in _raw("tab", "list", t=30).splitlines():
            m = re.match(r"\s*(?:→\s*)?\[(\d+)\]\s.*(https://(?:studio|www)\.youtube\.com/\S+)\s*$", line)  # last URL: titles can hold links
            if m and mine(m.group(2)):
                hits.append(m.group(1))
        if hits:
            break
        _raw("wait", "2000")
    if MARK and len(hits) > 1:
        hits = [n for n in hits if _tab_name(n) == MARK]
    if len(hits) != 1:
        die("tab pin", "my tab is gone" if not hits else f"{len(hits)} tabs match my URL (you may have the same page open)")
    return hits[0]


def _tab_name(n):
    """window.name of tab n (a read-only look, used only to tell my tab from yours). release/studio.py replaces this
    with a read that also checks the sessionStorage copy of the mark."""
    out = subprocess.run(["timeout", "60", "agent-browser", "--session", S, "batch", "--json"],
                         input=json.dumps([["tab", n], ["eval", "window.name"]]),
                         capture_output=True, text=True, env=ENV).stdout
    try:
        v = json.loads(out)[-1]["result"]
        return v.get("result") if isinstance(v, dict) else v
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _guard_js():
    """The tab guard run inside every pinned batch. release/studio.py replaces it with one that also requires the mark
    in window.name or sessionStorage."""
    pre = ([f"https://studio.youtube.com/channel/{CHAN}"] if CHAN else []) + \
        ([f"https://studio.youtube.com/video/{VIDEO_ID}/"] if VIDEO_ID else []) + EXTRA
    return (f"(() => {{ const ok = {json.dumps(pre)}.some(p => location.href.startsWith(p)); "
            "if (!ok) throw new Error('GUARD: not my Studio tab: ' + location.href.slice(0, 80)); "
            + (f"if (window.name !== {json.dumps(MARK)}) throw new Error('GUARD: tab not marked'); " if MARK else "")
            + "return 'guard ok'; })()")


def _pinned(cmd):
    """One batch: select my tab, assert its location in the SAME invocation, then run cmd (a list of args). Sent as a
    JSON batch, never the string form (which strips backslashes from file paths). Returns the last step's result dict
    ({"success", "result", "error"})."""
    g = base64.b64encode(_guard_js().encode()).decode()
    steps = [["tab", my_tab()], ["eval", "-b", g], [str(x) for x in cmd]]
    out = subprocess.run(["timeout", "180", "agent-browser", "--session", S, "batch", "--json", "--bail"],
                         input=json.dumps(steps), capture_output=True, text=True, env=ENV)
    try:
        res = json.loads(out.stdout)
    except ValueError:
        die("batch", "agent-browser gave no JSON for a pinned batch")   # never its output: it can carry page text
    if not isinstance(res, list) or len(res) < 2 or not isinstance(res[1], dict) or not res[1].get("success"):
        die("tab guard", "the guard refused the tab or the batch did not run")
    return res[2] if len(res) > 2 and isinstance(res[2], dict) else {"success": False, "error": "step not run"}


def ab(*a, stdin=None, t=90):
    if a and a[0] in MUTATING:
        return _pinned(list(a))
    return _raw(*a, stdin=stdin, t=t)


def ev(js):
    """Run js in my tab; its value (a JSON string is decoded), or None when the eval failed."""
    r = _pinned(["eval", "-b", base64.b64encode(js.encode()).decode()])
    if not r.get("success"):
        return None
    v = r.get("result")
    v = v.get("result") if isinstance(v, dict) and "result" in v else v
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def set_file(sel, path, label, root=DIALOG):
    """Hand a local file to a file input by its exact path, then prove Chrome can read it (the size matches). The
    browser must run on this machine (the endpoint is loopback only), so the path is the same for both."""
    want = os.path.getsize(path)
    r = _pinned(["upload", sel, path])
    if not r.get("success"):
        die(label, "the browser did not take the file")
    got = ev(f"(() => {{ const i = {root} && {root}.querySelector({json.dumps(sel.replace('ytcp-uploads-dialog ', ''))}) "
             f"|| document.querySelector({json.dumps(sel)});"
             " return i && i.files && i.files[0] ? JSON.stringify({ name: i.files[0].name, size: i.files[0].size }) : null; })()")
    if got is not None and (not isinstance(got, dict) or got.get("size") != want):
        die(label, f"Chrome sees another size than the file's {want} bytes")
    ok(label, f"{os.path.basename(path)} ({want} bytes)" + (" size verified" if got else " (input already replaced)"))


def wait(ms):
    _raw("wait", str(ms))


def die(step, why):
    print(f"ABORT at {step}: {why}")
    sys.exit(1)


def ok(step, detail=""):
    print(f"ok   {step} {detail}".rstrip())


def put_text(sel, text, label, root=DIALOG):
    """Write `text` into a contenteditable as ONE insertText (per-key typing trips autocompletes) and read it back."""
    focus_js = (f"(() => {{ const el = {root}.querySelector({json.dumps(sel)}); if (!el || el.getBoundingClientRect().width === 0) return false; "
                "window.getSelection().removeAllRanges(); el.focus(); return document.activeElement === el || el.contains(document.activeElement); })()")
    for _ in range(12):
        if ev(focus_js) is True:
            break
        wait(1500)
    else:
        die(label, "field never laid out or never took focus")
    write_js = (f"(() => {{ const T = {json.dumps(text)}; const el = {root}.querySelector({json.dumps(sel)}); "
                "if (!(document.activeElement === el || el.contains(document.activeElement))) return JSON.stringify({ error: 'focus lost' }); "
                "const s = window.getSelection(); const rg = document.createRange(); rg.selectNodeContents(el); s.removeAllRanges(); s.addRange(rg); "
                "document.execCommand('insertText', false, T); const g = el.innerText; "
                "return JSON.stringify({ want: T.length, got: g.length, equal: g === T, trailing: g === T + '\\n' }); })()")
    r = ev(write_js)
    tries = 1
    while (not isinstance(r, dict) or "error" in r or not (r.get("equal") or r.get("trailing"))) and tries < 5:
        wait(2500)
        tries += 1
        if ev(focus_js) is not True:
            continue
        r = ev(write_js)
    if not isinstance(r, dict) or "error" in r or not (r.get("equal") or r.get("trailing")):
        die(label, "the field does not read back what was written")
    ok(label, f"{r['want']} chars" + ("" if r["equal"] else " (+ trailing line box)"))


def dialog_inputs():
    """Every file input and visible button label inside the upload dialog (for discovering a control)."""
    return ev(f"(() => {{ const d = {DIALOG}; if (!d) return null; const vis = el => {{ const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }};"
              " return JSON.stringify({ files: [...d.querySelectorAll('input[type=file]')].map(i => (i.id || '') + '|' + (i.accept || '') + '|' + (i.closest('[id]') || {}).id),"
              " buttons: [...d.querySelectorAll('ytcp-button, button, [role=button]')].filter(vis).map(b => b.innerText.trim()).filter(Boolean).slice(0, 40) }); })()")
