"""The built-in secret-shape scanner behind the gate's `secrets` check.

Contract (any replacement in settings.toml `checks.dir` must keep it): `scan_output(text, public_hosts)` returns [] for
clean text, or findings that each start with a plain-words kind ("email-like text", "credential", "host not on the
public allowlist") and never carry a secret value. The gate reports the kinds only.

The text is first decoded the ways an encoded secret could hide (terminal escapes stripped; backslash-u escapes,
percent-encoding and JSON's escaped slashes undone, a few rounds), then searched for: an email address, a URL whose
host is not on the public allowlist, a URL with a user part (it can hold a password), a loopback address with a port,
a value under a credential-like key ("token = ...", "password: ..."), and well-known token shapes.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

_ESC = chr(27)
_TERMINAL_ESCAPE = re.compile(
    _ESC + r"\[[0-?]*[ -/]*[@-~]" + "|" + _ESC + r"\][^" + chr(7) + _ESC + "]*(?:" + chr(7) + "|" + _ESC + r"\\)"
    + "|" + _ESC + "[ -/]*[0-~]")
_UNICODE_ESCAPE = re.compile(re.escape(chr(92)) + "u([0-9a-fA-F]{4})")
_EMAIL = re.compile(r"[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,}", re.I)
_URL = re.compile(r"(?<![\w+.-])[\w+.-]*?(https?|wss?)://(\[[0-9a-f:.]*\]|[\w.-]+)(?::(\d+))?", re.I)
_USER_PART = re.compile(r"""(?::[^/?#\s"'<>@]*)?@([^/?#\s"'<>]*)""")
_LOOPBACK = re.compile(r"(?<![\w.-])(127(?:\.\d{1,3}){3}|0\.0\.0\.0|localhost|\[::1?\]):(\d{1,5})(?!\d)", re.I)
_LOOPBACK_HOST = re.compile(r"127(?:\.\d{1,3}){3}|localhost|\[::1\]", re.I)
_SECRET_KEYS = ("password", "passwd", "pwd", "secret", "client_secret", "token", "access_token", "refresh_token",
                "id_token", "api_key", "apikey", "authorization", "cookie", "set_cookie", "session_token", "private_key")
_SECRET_KEY = "|".join(k.replace("_", "[_-]") for k in sorted(_SECRET_KEYS, key=len, reverse=True))
_SECRET_PAIR = re.compile(rf"""(?<![\w-])["']?(?:[\w-]*?[_-])??({_SECRET_KEY})["']?[ \t]*[:=][ \t]*["']?"""
                          r"""([^\s"',;}\]]*)""", re.I)
_NO_VALUE = {"", "null", "none", "true", "false", "undefined"}
_SECRET_SHAPES = (
    ("GitHub token", re.compile(r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})")),
    ("sk- API key", re.compile(r"(?<![\w-])sk-[\w-]{16,}")),
    ("Slack token", re.compile(r"(?<![\w-])xox[a-z]-[\w-]{10,}")),
    ("AWS access key id", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Za-z])")),
    ("Google API key", re.compile(r"(?<![\w-])AIza[0-9A-Za-z_-]{30,}")),
    ("Hugging Face token", re.compile(r"(?<![\w-])hf_[A-Za-z0-9]{20,}")),
    ("Doppler token", re.compile(r"(?<![\w.-])dp\.[a-z]{2,6}\.[A-Za-z0-9._-]{10,}")),
    ("Telegram bot token", re.compile(r"(?<![\w:])\d{6,12}:[A-Za-z0-9_-]{30,}")),
    ("JWT", re.compile(r"(?<![\w-])eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*")),
    ("Bearer token", re.compile(r"\bBearer[ \t]+[\w.~+/-]{8,}", re.I)),
    ("PEM private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
)


def normalize(text: str) -> str:
    """Terminal escapes stripped, then backslash-u escapes, percent-encoding and JSON's escaped slashes undone, repeated
    (a few rounds) until nothing changes, so a layered encoding is undone too."""
    for _ in range(4):
        decoded = _TERMINAL_ESCAPE.sub("", text)
        decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
        decoded = unquote(decoded).replace("\\/", "/")
        if decoded == text:
            break
        text = decoded
    return text


def _credentials_in(text: str) -> tuple:
    """Credential-like text, named by kind only, and the spans of the values, which no other finding may quote."""
    found, spans = [], []
    for m in _SECRET_PAIR.finditer(text):
        if m.group(2).lower() not in _NO_VALUE:
            found.append(f"credential ({m.group(1).lower().replace('-', '_')} field, value withheld)")
            spans.append(m.span(2))
    for kind, shape in _SECRET_SHAPES:
        for m in shape.finditer(text):
            found.append(f"{kind} (value withheld)")
            spans.append(m.span())
    return found, spans


def scan_output(text: str, public_hosts=()) -> list:
    """Findings for everything in text that must never ship ([] when it may)."""
    text = normalize(text)
    hosts = frozenset(h.lower() for h in public_hosts)
    credentials, withheld = _credentials_in(text)
    findings = []
    for m in _URL.finditer(text):
        host = m.group(2).lower().rstrip(".")
        user = _USER_PART.match(text, m.end())
        if user:  # the user part may hold a credential: only the scheme is named
            withheld.append((m.start(), user.end()))
            findings.append(f"URL with a user part (withheld): {m.group(1)}://...")
        elif _LOOPBACK_HOST.fullmatch(host):
            findings.append("loopback URL (withheld)")
        elif host not in hosts:
            findings.append("host not on the public allowlist (withheld)")
    outside_urls = _URL.sub(lambda m: " " * len(m.group(0)), text)
    findings += ["loopback address with a port (withheld)" for _ in _LOOPBACK.finditer(outside_urls)]
    emails = ["email-like text (withheld)" for m in _EMAIL.finditer(text)
              if not any(start < m.end() and m.start() < end for start, end in withheld)]
    return emails + findings + credentials
