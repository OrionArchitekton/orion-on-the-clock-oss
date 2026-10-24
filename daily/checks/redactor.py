"""The built-in redactor: a fail-closed verdict on text bound for a public video.

Contract (any replacement in settings.toml `checks.dir` must keep it): `decision(text)` returns ("PUBLISH", []) for
clean text, or ("ABSTAIN", hits) where each hit is (class, masked snippet). A matched value is never returned whole.

What it refuses, by class:
- secret: well-known token shapes (GitHub, sk- keys, Slack, AWS key ids, Google API keys, Hugging Face and Doppler
  tokens, Telegram bot tokens, JWTs, bearer tokens, PEM private keys) and any long unbroken run of letters, digits,
  "_" and "-" (40 or more), which is how most secrets look;
- email: any email address;
- network: private, loopback, link-local and carrier-grade NAT IPv4 addresses (the last is where tailnets live),
  unique-local and link-local IPv6 addresses, and a localhost or loopback address with a port;
- home-path: a home folder path (/home/<name>, /Users/<name>, C:\\Users\\<name>);
- internal-ref: a pull request number ("#123", "PR 123") and an agent branch name ("claude/...", "codex/...").

Your own private words (host names, client names, code names) belong in lanes.json `blocked_names` or in settings.toml
`checks.redact_patterns`, never in this file: this file is public.

Stdlib only. Long dashes are built from code points elsewhere; this file holds none.
"""
from __future__ import annotations

import ipaddress
import re

_DENY: list = []


def _add(cls: str, pattern: str, flags=re.IGNORECASE) -> None:
    _DENY.append((cls, re.compile(pattern, flags)))


_add("secret", r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,})")
_add("secret", r"(?<![\w-])(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}")
_add("secret", r"(?<![\w-])xox[a-z]-[\w-]{10,}")
_add("secret", r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Za-z])", 0)
_add("secret", r"(?<![\w-])AIza[0-9A-Za-z_-]{30,}")
_add("secret", r"(?<![\w-])hf_[A-Za-z0-9]{20,}")
_add("secret", r"(?<![\w.-])dp\.[a-z]{2,6}\.[A-Za-z0-9._-]{10,}")
_add("secret", r"(?<![\w:])\d{6,12}:[A-Za-z0-9_-]{30,}")
_add("secret", r"(?<![\w-])eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*")
_add("secret", r"\bBearer[ \t]+[\w.~+/-]{8,}")
_add("secret", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", 0)
_add("secret", r"(?<![\w-])[A-Za-z0-9_-]{40,}(?![\w-])")
_add("email", r"[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,}")
_add("network", r"\b(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0):\d{2,5}\b")
_add("home-path", r"(?:/home/|/Users/|[A-Za-z]:\\+Users\\+)[A-Za-z0-9._-]+")
_add("internal-ref", r"(?:\bPR\s?|(?<![\w&])#)\d{2,6}\b")
_add("internal-ref", r"\b(?:claude|codex)/[A-Za-z0-9._/-]+")

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_IPV6 = re.compile(r"(?<![\w:.])([0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7})(?![\w:])", re.IGNORECASE)  # a candidate only
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _private_ip(text: str) -> list:
    out = []
    for m in [*_IPV4.finditer(text), *_IPV6.finditer(text)]:
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in _CGNAT):
            out.append(m.group(1))
    return out


def _mask(s: str) -> str:
    """A matched token masked, so a hit can be reported without leaking it."""
    s = s.strip()
    if len(s) <= 4:
        return s[:1] + "*" * max(len(s) - 1, 0)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def scan(text: str) -> list:
    """Every (class, masked snippet) hit, each distinct match once."""
    hits, seen = [], set()
    found = [(cls, m.group(0)) for cls, rx in _DENY for m in rx.finditer(text)]
    found += [("network", ip) for ip in _private_ip(text)]
    for cls, raw in found:
        if (cls, raw.lower()) not in seen:
            seen.add((cls, raw.lower()))
            hits.append((cls, _mask(raw)))
    return hits


def decision(text: str):
    """Fail-closed verdict: ("PUBLISH", []) or ("ABSTAIN", hits)."""
    hits = scan(text)
    return ("ABSTAIN" if hits else "PUBLISH"), hits
