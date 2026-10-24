"""The config layer: where the private config and state live, and the operator's settings.toml.

The config dir is `$ORION_CLOCK_CONFIG` (default `~/.config/orion-on-the-clock`) and the state dir `$ORION_CLOCK_STATE`
(default `~/.local/state/orion-on-the-clock`); daily.paths resolves both. The config dir holds:

- settings.toml: the pluggable pieces (this module): time zone, show names, which sources run, the GitHub owner,
  the text checks, the writer, and the pieces later stages use (channel, browser endpoint, notify command, TTS host);
- lanes.json and embargo.json: what may appear and how (daily.config);
- publish.json: private or public uploads (read by the publish stage).

Loading is strict and fails closed: a missing or unreadable file, an unknown key (a typo must never be ignored), a value
of the wrong type or shape, or a required key left out raises SettingsError with a short code (`missing:show.series`,
`bad:github.owner`, `unknown:show.serie`). The code names a key, never a value. Keys that only a later stage needs
(channel id, say) may be absent here; that stage asks for them with `require`, which raises the same way, so nothing
ever falls back to a default that publishes.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from daily import paths

FILE = "settings.toml"
BUILTIN_HARNESSES = ("claude_code", "codex")
DEFAULT_PREFIXES = (("claude/", "claude_code"), ("codex/", "codex"))
DEFAULT_PUBLIC_HOSTS = ("youtube.com", "www.youtube.com")
DEFAULT_BROWSER = "http://127.0.0.1:9222"
DEFAULT_MODEL = "sonnet"
DEFAULT_BUDGET_USD = 2.0
_HARNESS_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")  # a GitHub user or organization name
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")  # a YouTube channel id
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,252}\Z")  # an ssh destination: host, user@host or an alias
_ENDPOINT = re.compile(r"(?:https?|wss?)://[^\s/]+(?:/\S*)?\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_PUBLIC_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\Z")


class SettingsError(Exception):
    """settings.toml is missing, unreadable or invalid. The message is a short code naming a key, never a value."""


@dataclass(frozen=True)
class Adapter:
    """A counts-only source the operator supplies (docs/sources.md): a Python file exposing scan()."""

    harness: str
    display: str
    module: Path


@dataclass(frozen=True)
class Settings:
    timezone: str
    series: str
    host_name: str
    disclosure: str
    claude_code: bool = True
    codex: bool = True
    github: bool = True
    claude_projects: Path = field(default_factory=lambda: paths.DEFAULT_CLAUDE_PROJECTS.expanduser())
    codex_db: Path = field(default_factory=lambda: paths.DEFAULT_CODEX_DB.expanduser())
    github_owner: str | None = None
    harness_prefixes: tuple = DEFAULT_PREFIXES
    adapters: tuple = ()
    tracker: Path | None = None
    checks_dir: Path | None = None
    public_hosts: tuple = DEFAULT_PUBLIC_HOSTS
    redact_patterns: tuple = ()  # (name, compiled pattern)
    writer_model: str = DEFAULT_MODEL
    writer_budget_usd: float = DEFAULT_BUDGET_USD
    voice_style: tuple = ()
    voice_samples: tuple = ()
    voice_tags: tuple = ()
    avoid_words: tuple | None = None  # None: the built-in list (daily.voicelint.WRITERLY)
    channel_id: str | None = None
    channel_name: str | None = None
    home_channel_id: str | None = None
    browser_endpoint: str = DEFAULT_BROWSER
    notify_command: tuple = ()
    tts_host: str | None = None

    def require(self, key: str):
        """The value of an optional key a stage cannot run without (`channel_id`, say), or SettingsError
        `missing:<key>`. A stage calls this instead of supplying a default of its own."""
        value = getattr(self, key, None)
        if value is None or value == "" or value == ():
            raise SettingsError(f"missing:{key}")
        return value

    def harness_display(self) -> dict:
        """Harness id -> display name, built-ins first, then the adapters in their configured order."""
        out = {h: name for h, name in (("claude_code", "Claude Code"), ("codex", "Codex")) if getattr(self, h)}
        out.update({a.harness: a.display for a in self.adapters})
        return out


# ---- reading and validating ----

class _Table:
    """One TOML table read key by key: every key taken is recorded, and `done` refuses any key left untaken."""

    def __init__(self, data: dict, where: str):
        self.data, self.where, self.taken = data, where, set()

    def key(self, name: str) -> str:
        return f"{self.where}.{name}" if self.where else name

    def get(self, name: str, default=None):
        self.taken.add(name)
        return self.data.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.data

    def table(self, name: str) -> "_Table":
        value = self.get(name, {})
        if not isinstance(value, dict):
            raise SettingsError(f"bad:{self.key(name)}")
        return _Table(value, self.key(name))

    def done(self) -> None:
        extra = sorted(set(self.data) - self.taken)
        if extra:
            raise SettingsError(f"unknown:{self.key(extra[0])}")


def _text(t: _Table, name: str, *, required: bool = False, pattern: re.Pattern | None = None, cap: int = 400):
    value = t.get(name)
    if value is None:
        if required:
            raise SettingsError(f"missing:{t.key(name)}")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > cap or "\n" in value:
        raise SettingsError(f"bad:{t.key(name)}")
    if pattern is not None and not pattern.match(value):
        raise SettingsError(f"bad:{t.key(name)}")
    return value


def _flag(t: _Table, name: str, default: bool) -> bool:
    value = t.get(name, default)
    if not isinstance(value, bool):
        raise SettingsError(f"bad:{t.key(name)}")
    return value


def _path(t: _Table, name: str) -> Path | None:
    value = _text(t, name, cap=4096)
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SettingsError(f"bad:{t.key(name)}")  # a relative path would resolve against wherever the timer runs
    return path


def _texts(t: _Table, name: str, *, cap: int = 300, pattern: re.Pattern | None = None) -> tuple | None:
    value = t.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() and len(v) <= cap for v in value):
        raise SettingsError(f"bad:{t.key(name)}")
    if pattern is not None and not all(pattern.match(v) for v in value):
        raise SettingsError(f"bad:{t.key(name)}")
    return tuple(value)


def _read(dir_: Path) -> dict:
    try:
        raw = (Path(dir_) / FILE).read_bytes()
    except FileNotFoundError:
        raise SettingsError("settings_missing") from None
    except OSError as e:
        raise SettingsError("settings_unreadable") from e
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SettingsError("settings_unreadable") from e  # TOML itself refuses a repeated key


def _timezone(value) -> str:
    from daily import day  # the one place a zone name is judged
    if not isinstance(value, str):
        raise SettingsError("missing:timezone" if value is None else "bad:timezone")
    saved = day.zone_name()
    try:
        day.use_timezone(value)
    except ValueError:
        raise SettingsError("bad:timezone") from None
    finally:
        day.use_timezone(saved)
    return value


def _adapters(t: _Table, taken: set) -> tuple:
    value = t.get("adapters", [])
    if not isinstance(value, list):
        raise SettingsError("bad:sources.adapters")
    out = []
    for n, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise SettingsError(f"bad:sources.adapters[{n}]")
        a = _Table(raw, f"sources.adapters[{n}]")
        harness = _text(a, "harness", required=True, pattern=_HARNESS_ID)
        display = _text(a, "display", required=True, cap=40)
        module = _path(a, "module")
        if module is None:
            raise SettingsError(f"missing:sources.adapters[{n}].module")
        a.done()
        if harness in taken:
            raise SettingsError(f"duplicate:sources.adapters[{n}].harness")
        taken.add(harness)
        out.append(Adapter(harness, display, module))
    return tuple(out)


def _patterns(t: _Table) -> tuple:
    value = t.get("redact_patterns", [])
    if not isinstance(value, list):
        raise SettingsError("bad:checks.redact_patterns")
    out = []
    for n, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise SettingsError(f"bad:checks.redact_patterns[{n}]")
        p = _Table(raw, f"checks.redact_patterns[{n}]")
        name = _text(p, "name", required=True, pattern=_HARNESS_ID)
        regex = _text(p, "regex", required=True, cap=500)
        p.done()
        try:
            out.append((name, re.compile(regex, re.IGNORECASE)))
        except re.error:
            raise SettingsError(f"bad:checks.redact_patterns[{n}].regex") from None
    return tuple(out)


def _prefixes(t: _Table) -> tuple:
    value = t.get("harness_prefixes")
    if value is None:
        return DEFAULT_PREFIXES
    if not isinstance(value, dict) or not value:
        raise SettingsError("bad:github.harness_prefixes")
    out = []
    for prefix, harness in value.items():
        if not prefix.strip() or not isinstance(harness, str) or harness not in BUILTIN_HARNESSES:
            raise SettingsError("bad:github.harness_prefixes")  # only a transcript harness can own a PR quote
        out.append((prefix, harness))
    return tuple(out)


def _budget(t: _Table) -> float:
    value = t.get("budget_usd", DEFAULT_BUDGET_USD)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 50:
        raise SettingsError("bad:writer.budget_usd")
    return float(value)


def load_settings(dir_: Path | None = None) -> Settings:
    """settings.toml from the config dir (paths.config_dir() when dir_ is None), validated. Raises SettingsError."""
    top = _Table(_read(paths.config_dir() if dir_ is None else Path(dir_)), "")
    timezone = _timezone(top.get("timezone"))
    show = top.table("show")
    series = _text(show, "series", required=True, cap=80)
    host_name = _text(show, "host_name", required=True, cap=40)
    disclosure = _text(show, "disclosure", required=True, cap=200)
    show.done()

    src = top.table("sources")
    claude_code, codex, github = (_flag(src, k, True) for k in ("claude_code", "codex", "github"))
    claude_projects = _path(src, "claude_projects") or paths.DEFAULT_CLAUDE_PROJECTS.expanduser()
    codex_db = _path(src, "codex_db") or paths.DEFAULT_CODEX_DB.expanduser()
    adapters = _adapters(src, set(BUILTIN_HARNESSES))
    tracker = _path(src, "tracker")
    src.done()
    if not (claude_code or codex or adapters):
        raise SettingsError("bad:sources")  # with no agent log at all there is no show

    gh = top.table("github")
    owner = _text(gh, "owner", pattern=_OWNER, cap=39)
    prefixes = _prefixes(gh)
    gh.done()
    if github and owner is None:
        raise SettingsError("missing:github.owner")

    checks = top.table("checks")
    checks_dir = _path(checks, "dir")
    hosts = _texts(checks, "public_hosts", cap=253, pattern=_PUBLIC_HOST)
    redact = _patterns(checks)
    checks.done()

    wr = top.table("writer")
    model = _text(wr, "model", pattern=_MODEL, cap=64) or DEFAULT_MODEL
    budget = _budget(wr)
    style = _texts(wr, "voice_style") or ()
    samples = _texts(wr, "voice_samples") or ()
    tags = _texts(wr, "voice_tags", cap=40) or ()
    avoid = _texts(wr, "avoid_words", cap=40)
    wr.done()

    ch = top.table("channel")
    channel_id = _text(ch, "id", pattern=_CHANNEL)
    channel_name = _text(ch, "name", cap=100)
    home_channel_id = _text(ch, "home_id", pattern=_CHANNEL)
    ch.done()

    br = top.table("browser")
    endpoint = _text(br, "endpoint", pattern=_ENDPOINT, cap=500) or DEFAULT_BROWSER
    br.done()

    nt = top.table("notify")
    command = _texts(nt, "command", cap=4096) or ()
    nt.done()

    tts = top.table("tts")
    tts_host = _text(tts, "host", pattern=_HOST, cap=253)
    tts.done()
    top.done()

    return Settings(
        timezone=timezone, series=series, host_name=host_name, disclosure=disclosure,
        claude_code=claude_code, codex=codex, github=github, claude_projects=claude_projects, codex_db=codex_db,
        github_owner=owner, harness_prefixes=prefixes, adapters=adapters, tracker=tracker,
        checks_dir=checks_dir, public_hosts=hosts if hosts is not None else DEFAULT_PUBLIC_HOSTS,
        redact_patterns=redact, writer_model=model, writer_budget_usd=budget, voice_style=style,
        voice_samples=samples, voice_tags=tags, avoid_words=avoid, channel_id=channel_id, channel_name=channel_name,
        home_channel_id=home_channel_id, browser_endpoint=endpoint, notify_command=command, tts_host=tts_host,
    )
