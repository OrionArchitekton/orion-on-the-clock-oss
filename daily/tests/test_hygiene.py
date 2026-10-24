"""The repository must stay publishable: only the kinds of file it holds, all UTF-8 text, no media, no symbolic links,
no real email addresses, no long dashes, no personal markers, and a .gitignore that keeps artifacts out before they
are staged.

The personal-marker denylist holds only sha256 prefixes (the markers themselves are never written here): the
original author's home folder name, machine names, private channel ids, private repo and product names, and the hash
prefix that identifies the private pipeline's voice reference recording. Each
file is tokenized and every candidate (a token and its parts, the user part of a home path, a channel id) is hashed and
looked up. A hit names the file and the token, so a fix is easy; the token is already in that file anyway."""
from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The kinds of file this repository holds. Anything else is refused: an allowlist, so an artifact nobody thought to name
# (a session log, a voice sample, a rendered video) cannot slip through. Extend it deliberately.
ALLOWED_FILES = {"LICENSE", ".gitignore", "pyproject.toml", "daily/script_schema.json"}
ALLOWED_PATTERNS = ("*.md", "daily/*.py", "daily/sources/*.py", "daily/checks/*.py", "daily/tests/*.py",
                    "daily/engine/*.html", "config.example/*.json", "config.example/*.toml", "docs/examples/*.py",
                    "release/*.py", "systemd/*.service", "systemd/*.timer", "systemd/*.sh")
MEDIA_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".png",
                  ".jpg", ".jpeg", ".webp", ".gif", ".ttf", ".otf", ".woff", ".woff2", ".srt", ".vtt"}
ARTIFACT_SUFFIXES = MEDIA_SUFFIXES | {".jsonl", ".sqlite", ".db", ".zst", ".har"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Not addresses: a URL's user part ("https://user:pw@host") and a git remote ("git@host:owner/repo")
NOT_AN_ADDRESS = re.compile(r"(?<=[:/])[^@\s/]+@|git@[A-Za-z0-9.-]+:")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.invalid", "users.noreply.github.com")
LONG_DASHES = re.compile("[" + "".join(map(chr, (0x2013, 0x2014, 0x2015))) + "]")  # built from code points
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # where tailnet addresses live; the network's own name is no address
TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
HOME_USER = re.compile(r"(?:/home/|/Users/|\\Users\\)([A-Za-z0-9._-]+)", re.IGNORECASE)
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
AUTHOR = re.compile(r"dan[\s._-]*mercede", re.IGNORECASE)
AUTHOR_FILES = {"LICENSE", "README.md"}  # the copyright line and the origin story; nowhere else

DENY_TOKENS = frozenset({  # machine names, private repo and product names
    "010fc70e93310a5c", "0cf44bf5e081060d", "1409968006d49ad1", "2879c35c5d4eb420",
    "37b7b6222d0fe8e8", "41e9bcfeee72b974", "5a7b2e919d9eb13c", "5dcce5c5e9b9b202",
    "6780e8436f199e61", "6cb784e0f985cba2", "785167a8b6b50bc4", "84544529c82916c8", "89c80b3e93435ce1",
    "8cfde6efdfc4ed5a", "948f99c20215fdb2", "96a4bc2602655473", "adeae880690a0084", "af3258a44c19a747",
    "b158623dcefd6013", "b2fa820ea30681b5", "b7c250c47019fa2b", "d09a8874fc377172", "ea677ca35a088b79",
    "f97fb47d80af8858", "fcf1d7decc4ba529", "1bfd8bc308632b62", "14faafdf20658fb4", "eb5e27d899cd83e5",
    "7aeaa6e84de649e8", "c929d4fc5e25b5a5",
    "923ccab3fb2aa54c", "433e58a2d6a39e54", "f42ad6e9f822ba22", "c34ece26823fd2ac",
    "60f19f2d268603cb"})  # the last: the private voice reference recording's sha256 prefix
DENY_HOME_USERS = frozenset({"a925617886e8799c"})  # the original author's home folder name
DENY_CHANNELS = frozenset({"261a46a4e7422010", "5b89b3cc056ff93c"})  # the private pipeline's channel ids


def digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def repo_files() -> list:
    """Every path git would commit: tracked paths plus untracked paths that .gitignore does not cover."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        if os.environ.get("CI"):
            raise
        pytest.skip("not a git checkout")
    names = [os.fsdecode(raw) for raw in out.split(b"\0") if raw]
    paths = sorted({ROOT / n for n in names if (ROOT / n).exists() or (ROOT / n).is_symlink()})
    assert ROOT / "pyproject.toml" in paths, "file listing looks wrong; the hygiene guard would pass vacuously"
    return paths


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def texts() -> list:
    found = []
    for path in repo_files():
        if path.is_file() and not path.is_symlink():
            try:
                found.append((path, path.read_bytes().decode("utf-8")))
            except UnicodeDecodeError:
                pass
    return found


def personal_markers(text: str, tokens=DENY_TOKENS, users=DENY_HOME_USERS, channels=DENY_CHANNELS) -> list:
    """Every denied candidate in text, as (kind, token)."""
    hits = []
    for token in set(TOKEN.findall(text.lower())):
        for candidate in {token, *re.split(r"[-_.]", token)}:
            if digest(candidate) in tokens:
                hits.append(("name", candidate))
    hits += [("home", m.group(1)) for m in HOME_USER.finditer(text) if digest(m.group(1).lower()) in users]
    hits += [("channel", c) for c in CHANNEL_ID.findall(text) if digest(c) in channels]
    for m in IPV4.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(1))
            if ip in CGNAT and ip != CGNAT.network_address:
                hits.append(("tailnet-address", m.group(1)))
        except ValueError:
            pass
    return sorted(set(hits))


def test_only_the_kinds_of_file_this_repository_holds():
    bad = [rel(p) for p in repo_files()
           if rel(p) not in ALLOWED_FILES and not any(fnmatch.fnmatchcase(rel(p), pat) for pat in ALLOWED_PATTERNS)]
    assert not bad, f"not a kind of file this repository holds; extend ALLOWED_PATTERNS deliberately if real: {bad}"


def test_no_media_session_logs_or_databases_are_committed():
    bad = [rel(p) for p in repo_files() if p.suffix.lower() in ARTIFACT_SUFFIXES]
    assert not bad, f"media, voice samples, session logs and databases are never committed (bring your own): {bad}"


def test_no_symbolic_links():
    assert not [rel(p) for p in repo_files() if p.is_symlink()]


def test_every_committed_file_is_utf8_text():
    bad = []
    for p in repo_files():
        if p.is_file() and not p.is_symlink():
            raw = p.read_bytes()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                bad.append(rel(p))
            else:
                if b"\0" in raw:
                    bad.append(rel(p))
    assert not bad, bad


def real_addresses(text: str) -> list:
    out = []
    for m in EMAIL.finditer(text):
        domain = m.group().rsplit("@", 1)[1].lower()
        if any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
            continue
        if NOT_AN_ADDRESS.match(text, m.start()) or (m.start() and text[m.start() - 1] in ":/"):
            continue
        out.append(m.group())
    return out


def test_no_real_email_addresses():
    hits = [f"{rel(p)}: {a}" for p, t in texts() for a in real_addresses(t)]
    assert not hits, hits


def test_the_address_scan_tells_addresses_from_urls_and_remotes():
    address = "pat" + "@" + "corp-mail.test"  # built at run time, so this file holds no address
    assert real_addresses(f"mail {address} now") == [address]
    assert real_addresses("see https://user:pw@host.test/x and git@host.test:owner/repo.git") == []
    assert real_addresses("write to sam@example.com") == []


def test_no_long_dashes():
    assert not [rel(p) for p, t in texts() if LONG_DASHES.search(t)]


def test_no_personal_markers():
    hits = {rel(p): personal_markers(t) for p, t in texts()}
    hits = {k: v for k, v in hits.items() if v}
    assert not hits, f"personal markers (home folder, machine, channel, private repo or product names): {hits}"


def test_the_author_is_named_only_in_the_license_and_the_readme():
    assert not [rel(p) for p, t in texts() if AUTHOR.search(t) and rel(p) not in AUTHOR_FILES]
    for spelling in ("Dan Mer" + "cede", "dan-mer" + "cede", "dan.mer" + "cede", "dan_mer" + "cede"):
        assert AUTHOR.search(spelling), spelling  # positive control, built so this file holds none


def test_the_marker_scan_finds_what_it_is_meant_to_find():
    """Positive control on synthetic markers (never a real one): the same scan, handed deny sets built here."""
    tokens, users, channels = {digest("zorblat-07"), digest("quuxbox")}, {digest("pat")}, {digest("UC" + "q" * 22)}
    scan = lambda text: personal_markers(text, tokens, users, channels)  # noqa: E731
    assert scan("deployed to zorblat-07 at night") == [("name", "zorblat-07")]
    assert scan("the quuxbox-02 node") == [("name", "quuxbox")]  # a part of a longer token counts
    assert scan("see /home/pat/notes and C:\\Users\\Pat\\x") == [("home", "Pat"), ("home", "pat")]
    assert scan("channel UC" + "q" * 22 + " today") == [("channel", "UC" + "q" * 22)]
    node = ".".join(["100", "80", "1", "2"])  # built at run time: no literal one in this file
    assert scan("node " + node) == [("tailnet-address", node)]
    assert scan("the orion-on-the-clock pipeline from /home/someone at 10.0.0.1") == []  # positive control
    assert all(len(h) == 16 and int(h, 16) >= 0 for h in DENY_TOKENS | DENY_HOME_USERS | DENY_CHANNELS)


def ignored(path: str) -> bool:
    proc = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, capture_output=True)
    if proc.returncode not in (0, 1):
        raise AssertionError(f"git check-ignore failed for {path}: {proc.stderr!r}")
    return proc.returncode == 0


def test_gitignore_keeps_every_artifact_kind_out():
    repo_files()  # skips outside a git checkout, fails in CI
    must = [f"probe/file{suffix}" for suffix in sorted(ARTIFACT_SUFFIXES)]
    must += ["settings.toml", "lanes.json", "embargo.json", "publish.json", "state/2026-10-16/ledger.json", ".env"]
    missed = [p for p in must if not ignored(p)]
    assert not missed, f".gitignore lets these through: {missed}"
    for keep in ("config.example/settings.toml", "config.example/lanes.json", "daily/script_schema.json"):
        assert not ignored(keep), keep
