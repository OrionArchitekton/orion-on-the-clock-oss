"""Private configuration: which lanes may appear and how, what never appears, and the embargo.

The files live outside the repo, in the private config folder. Anything missing or malformed raises ConfigError, so the
collect stage fails closed instead of running with a partial allowlist.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from daily import textcheck

DISCLOSURES = ("public", "headline")
MIN_EXCLUDE_LEN = 3  # a shorter hard_exclude entry would exclude (or block) nearly everything it is substring-matched on


class ConfigError(Exception):
    pass


def repo_name(s: str) -> str:
    """Last path component of any repo reference, lowercased, `.git` (any case) stripped."""
    s = s.strip().rstrip("/")
    s = re.split(r"[/:]", s)[-1]
    return s.lower().removesuffix(".git")


@dataclass(frozen=True)
class Lane:
    name: str
    disclosure: str
    label: str
    why: str


@dataclass(frozen=True)
class Config:
    lanes: dict
    hard_exclude: tuple
    blocked_names: tuple
    embargo_active: bool
    embargo_terms: tuple

    def _excluded(self) -> set:
        """Repo names behind hard_exclude, however each entry is spelled. A name is matched against the incoming repo
        and as a fragment of the working directory, so any spelling of an entry reaches both."""
        return {repo_name(e) for e in self.hard_exclude} - {""}

    def classify(self, repo: str | None, cwd: str | None = None) -> tuple[str, Lane | None]:
        name = repo_name(repo) if repo else None
        excluded = self._excluded()
        where = cwd.lower() if cwd else ""
        if (name is not None and name in excluded) or any(x in where for x in excluded):
            return "hard_exclude", None
        if name is None or name not in self.lanes:
            return "unlisted_lane", None
        return "lane", self.lanes[name]

    def resolve_lane(self, repo: str | None, cwd: str | None = None) -> Lane | None:
        return self.classify(repo, cwd)[1]

    def embargo_hit(self, text: str) -> bool:
        """Whether an active embargo term hits the text or its normal form (textcheck.forms). Terms are matched in
        their own normal form, so an accented term hits the accented and the plain spelling alike."""
        if not self.embargo_active:
            return False
        patterns = [_term_pattern(t) for t in self.embargo_terms if _usable(t)]
        return any(re.search(p, form, re.IGNORECASE) for form in textcheck.forms(text) for p in patterns)

    def names_to_block(self) -> tuple:
        names: list = []
        for e in self.blocked_names:
            names += [e.strip(), repo_name(e)]
        names += [lane.name for lane in self.lanes.values() if lane.disclosure == "headline"]
        for e in self.hard_exclude:
            names += [e.strip(), repo_name(e)]
        return tuple(dict.fromkeys(n for n in names if n))


def _words(term: str) -> list:
    """The words of a term in its normal form (textcheck.normal_form, the form the text is read in: invisible
    characters and accents dropped), split on any run of whitespace, "_" or "-"."""
    return [w for w in re.split(r"[\s_-]+", textcheck.normal_form(term)) if w]


def _usable(term: str) -> bool:
    return bool(_words(term))


def _term_pattern(term: str) -> str:
    """Starts at a word boundary, no trailing boundary (so "pine fair" hits "pine fairs", never "spine fair").
    Any run of whitespace, "_" or "-" between a term's words matches any such run in the text."""
    return r"(?<!\w)" + r"[\s_-]+".join(re.escape(w) for w in _words(term))


def _read(dir_: Path, name: str) -> dict:
    repeated: list = []

    def pairs_hook(pairs):  # json keeps the last of a repeated key; here that would silently change a lane
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                repeated.append((obj, key))
            obj[key] = value
        return obj

    try:
        data = json.loads((Path(dir_) / name).read_text(encoding="utf-8"), object_pairs_hook=pairs_hook)
    except FileNotFoundError:
        raise ConfigError(f"config_missing:{name}") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"config_unreadable:{name}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config_unreadable:{name}")
    if repeated:
        obj, key = repeated[0]
        raise ConfigError(f"duplicate_lane:{repo_name(key)}" if obj is data.get("lanes") else f"duplicate_key:{key}")
    return data


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _non_empty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def load_config(dir_: Path) -> Config:
    raw = _read(dir_, "lanes.json")
    emb = _read(dir_, "embargo.json")
    if not isinstance(raw.get("lanes"), dict):
        raise ConfigError("bad_shape:lanes")
    lanes: dict = {}
    for raw_name, spec in raw["lanes"].items():
        name = repo_name(raw_name)
        if not name:
            raise ConfigError("bad_shape:lanes")
        if name in lanes:
            raise ConfigError(f"duplicate_lane:{name}")
        if not isinstance(spec, dict):
            raise ConfigError(f"bad_shape:lane:{name}")
        if spec.get("disclosure") not in DISCLOSURES:
            raise ConfigError(f"bad_shape:disclosure:{name}")
        for field in ("label", "why"):
            if not _non_empty_str(spec.get(field)):
                raise ConfigError(f"bad_shape:{field}:{name}")
        lanes[name] = Lane(name, spec["disclosure"], spec["label"], spec["why"])
    for field in ("hard_exclude", "blocked_names"):
        if not _is_str_list(raw.get(field)):
            raise ConfigError(f"bad_shape:{field}")
    hard_exclude = tuple(e.strip() for e in raw["hard_exclude"])
    if any(len(e) < MIN_EXCLUDE_LEN or len(repo_name(e)) < MIN_EXCLUDE_LEN for e in hard_exclude):
        raise ConfigError("bad_shape:hard_exclude")
    if not isinstance(emb.get("active"), bool):
        raise ConfigError("bad_shape:embargo.active")
    if not _is_str_list(emb.get("terms")):
        raise ConfigError("bad_shape:embargo.terms")
    terms = tuple(emb["terms"])
    if emb["active"] and not any(_usable(t) for t in terms):
        raise ConfigError("bad_shape:embargo.terms")  # an active embargo with nothing to match would guard nothing
    return Config(
        lanes=lanes,
        hard_exclude=hard_exclude,
        blocked_names=tuple(e.strip() for e in raw["blocked_names"]),
        embargo_active=emb["active"],
        embargo_terms=terms,
    )
