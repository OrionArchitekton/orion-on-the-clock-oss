import json

import pytest

from daily import paths
from daily.config import Config, ConfigError, Lane, load_config, repo_name


def test_repo_name_normalizes_every_shape():
    for s in ("Owner/Demo-Tool", "https://github.com/owner/demo-tool.git", "git@github.com:owner/demo-tool.git",
              "platform/demo-tool", "demo-tool", "demo-tool/", "Owner/Demo-Tool.GIT",
              "https://github.com/owner/demo-tool.Git/"):
        assert repo_name(s) == "demo-tool"


def test_listed_public_lane_resolves(cfg):  # positive control for every exclusion below
    kind, lane = cfg.classify("owner/demo-tool")
    assert kind == "lane"
    assert (lane.name, lane.disclosure, lane.label) == ("demo-tool", "public", "the demo tool")
    assert cfg.resolve_lane("owner/demo-tool") is lane


def test_unlisted_or_missing_repo_is_excluded(cfg):
    assert cfg.classify("owner/mystery-repo") == ("unlisted_lane", None)
    assert cfg.classify(None) == ("unlisted_lane", None)


def test_hard_exclude_beats_the_lane_list(cfg):
    assert cfg.classify("owner/diary-app") == ("hard_exclude", None)


def test_hard_exclude_matches_a_cwd_fragment(cfg):
    assert cfg.classify("owner/demo-tool", "/home/x/src/secret-garden/demo-tool") == ("hard_exclude", None)
    assert cfg.classify("owner/demo-tool", "/home/x/src/demo-tool")[0] == "lane"


def test_embargo_matches_from_a_word_start_and_ignores_case(cfg):
    assert cfg.embargo_hit("Notes for the Pine Fair demo")
    assert cfg.embargo_hit("see (PINE FAIR).")  # punctuation at the edges still hits
    assert cfg.embargo_hit("the pine fairs")  # no trailing boundary: inflections hit
    assert cfg.embargo_hit("Pine Fair's agenda")
    assert not cfg.embargo_hit("a pinefair parrot")
    assert not cfg.embargo_hit("spine fair")  # the term must start at a word boundary
    assert not cfg.embargo_hit("the alpine fair")  # holds the term mid-word; "spine fair" alone cannot catch that


@pytest.mark.parametrize("text", ["built on .NET today", ".net first", "(.Net)"])
def test_embargo_term_may_start_with_a_non_word_character(cfg_dir, text):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [".net"]}))
    assert load_config(cfg_dir).embargo_hit(text)  # a plain \b before the dot would miss every one of these


def test_inactive_embargo_never_hits(cfg_dir):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": False, "terms": ["pine fair"]}))
    assert not load_config(cfg_dir).embargo_hit("the pine fair")


def test_names_to_block_covers_headline_hard_and_blocked(cfg):
    names = cfg.names_to_block()
    assert {"garden-shed", "diary-app", "secret-garden", "Zephyr"} <= set(names)
    assert "demo-tool" not in names


def test_missing_files_fail_closed(tmp_path):
    with pytest.raises(ConfigError, match="config_missing:lanes.json"):
        load_config(tmp_path)


def _edit(cfg_dir, name, fn):
    path = cfg_dir / name
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def _lane(d, **kw):
    d["lanes"]["demo-tool"].update(kw)


def _drop(d, key):
    d["lanes"]["demo-tool"].pop(key)


LANES_SHAPE_CASES = [
    ("lanes-is-a-list", lambda d: d.update(lanes=["demo-tool"]), "bad_shape:lanes"),
    ("lanes-is-missing", lambda d: d.pop("lanes"), "bad_shape:lanes"),
    ("lane-entry-is-a-string", lambda d: d["lanes"].update({"demo-tool": "public"}), "bad_shape:lane:demo-tool"),
    ("disclosure-unknown", lambda d: _lane(d, disclosure="secret"), "bad_shape:disclosure:demo-tool"),
    ("disclosure-missing", lambda d: _drop(d, "disclosure"), "bad_shape:disclosure:demo-tool"),
    ("disclosure-not-a-string", lambda d: _lane(d, disclosure=["public"]), "bad_shape:disclosure:demo-tool"),
    ("label-empty", lambda d: _lane(d, label=""), "bad_shape:label:demo-tool"),
    ("label-blank", lambda d: _lane(d, label="   "), "bad_shape:label:demo-tool"),
    ("label-not-a-string", lambda d: _lane(d, label=5), "bad_shape:label:demo-tool"),
    ("label-missing", lambda d: _drop(d, "label"), "bad_shape:label:demo-tool"),
    ("why-empty", lambda d: _lane(d, why=""), "bad_shape:why:demo-tool"),
    ("why-not-a-string", lambda d: _lane(d, why=["so"]), "bad_shape:why:demo-tool"),
    ("why-missing", lambda d: _drop(d, "why"), "bad_shape:why:demo-tool"),
    ("headline-why-empty", lambda d: d["lanes"]["garden-shed"].update(why=""), "bad_shape:why:garden-shed"),
    ("hard-exclude-is-a-string", lambda d: d.update(hard_exclude="diary-app"), "bad_shape:hard_exclude"),
    ("hard-exclude-has-a-non-string", lambda d: d.update(hard_exclude=["diary-app", 1]), "bad_shape:hard_exclude"),
    ("hard-exclude-missing", lambda d: d.pop("hard_exclude"), "bad_shape:hard_exclude"),
    ("hard-exclude-one-char", lambda d: d.update(hard_exclude=["a"]), "bad_shape:hard_exclude"),
    ("hard-exclude-two-chars-padded", lambda d: d.update(hard_exclude=["  ab  "]), "bad_shape:hard_exclude"),
    ("hard-exclude-blank", lambda d: d.update(hard_exclude=["diary-app", "   "]), "bad_shape:hard_exclude"),
    ("hard-exclude-empty-string", lambda d: d.update(hard_exclude=[""]), "bad_shape:hard_exclude"),
    ("hard-exclude-short-repo-name", lambda d: d.update(hard_exclude=["owner/ab"]), "bad_shape:hard_exclude"),
    ("hard-exclude-no-repo-name", lambda d: d.update(hard_exclude=["///"]), "bad_shape:hard_exclude"),
    ("blocked-names-is-an-int", lambda d: d.update(blocked_names=5), "bad_shape:blocked_names"),
    ("blocked-names-has-a-none", lambda d: d.update(blocked_names=["Zephyr", None]), "bad_shape:blocked_names"),
    ("blocked-names-missing", lambda d: d.pop("blocked_names"), "bad_shape:blocked_names"),
]


@pytest.mark.parametrize("edit,code", [pytest.param(e, c, id=i) for i, e, c in LANES_SHAPE_CASES])
def test_wrong_shape_in_lanes_file_fails_closed(cfg_dir, edit, code):
    _edit(cfg_dir, "lanes.json", edit)
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == code


EMBARGO_SHAPE_CASES = [
    ("active-missing", lambda d: d.pop("active"), "bad_shape:embargo.active"),
    ("active-is-a-string", lambda d: d.update(active="yes"), "bad_shape:embargo.active"),
    ("active-is-an-int", lambda d: d.update(active=1), "bad_shape:embargo.active"),
    ("terms-is-a-string", lambda d: d.update(terms="pine fair"), "bad_shape:embargo.terms"),
    ("terms-has-a-non-string", lambda d: d.update(terms=["pine fair", 1]), "bad_shape:embargo.terms"),
    ("terms-missing", lambda d: d.pop("terms"), "bad_shape:embargo.terms"),
    ("terms-empty-while-active", lambda d: d.update(terms=[]), "bad_shape:embargo.terms"),
    ("terms-all-blank-while-active", lambda d: d.update(terms=["", "   "]), "bad_shape:embargo.terms"),
]


@pytest.mark.parametrize("edit,code", [pytest.param(e, c, id=i) for i, e, c in EMBARGO_SHAPE_CASES])
def test_wrong_shape_in_embargo_file_fails_closed(cfg_dir, edit, code):
    _edit(cfg_dir, "embargo.json", edit)
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == code


def test_right_shapes_still_load(cfg_dir):  # positive control for the shape table: empty lists and no lanes are valid
    _edit(cfg_dir, "lanes.json", lambda d: d.update(lanes={}, hard_exclude=[], blocked_names=[]))
    _edit(cfg_dir, "embargo.json", lambda d: d.update(active=False, terms=[]))
    cfg = load_config(cfg_dir)
    assert cfg.lanes == {} and cfg.hard_exclude == () and cfg.blocked_names == () and not cfg.embargo_active


@pytest.mark.parametrize("dup_key", ["x/garden-shed", "Garden-Shed", "https://github.com/x/garden-shed.git"])
def test_two_lane_keys_with_one_normalized_name_fail(cfg_dir, dup_key):
    # the fixture lists garden-shed as headline; a second spelling must not silently downgrade it to public
    entry = {"disclosure": "public", "label": "a label", "why": "a reason"}
    _edit(cfg_dir, "lanes.json", lambda d: d["lanes"].update({dup_key: entry}))
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == "duplicate_lane:garden-shed"


def test_distinct_lane_names_load_fine(cfg_dir):  # positive control for the duplicate check
    entry = {"disclosure": "public", "label": "a label", "why": "a reason"}
    _edit(cfg_dir, "lanes.json", lambda d: d["lanes"].update({"Owner/Alpha-Tool": entry, "beta-tool": entry}))
    lanes = load_config(cfg_dir).lanes
    assert set(lanes) == {"demo-tool", "garden-shed", "diary-app", "alpha-tool", "beta-tool"}
    assert lanes["garden-shed"].disclosure == "headline"


def test_example_config_loads():
    """config.example/ is a working config folder as shipped: its lanes and embargo load, notes and all."""
    loaded = load_config(paths.REPO / "config.example")
    assert set(loaded.lanes) == {"example-public-tool", "example-private-system"}
    assert loaded.hard_exclude == ("example-personal-notes",) and not loaded.embargo_active


# ---- review round: spellings, repeated keys, embargo variants -------------------------------------------------

HARD_SPELLINGS = [" diary-app", "diary-app\n", "Owner/Diary-App", "diary-app/", "diary-app.git", "diary-app.GIT", "DIARY-APP",
                  "https://github.com/o/diary-app.git", "git@github.com:o/diary-app.git"]


@pytest.mark.parametrize("entry", HARD_SPELLINGS)
def test_hard_exclude_entry_in_any_spelling_excludes_a_listed_lane(cfg_dir, entry):
    _edit(cfg_dir, "lanes.json", lambda d: d.update(hard_exclude=[entry]))
    cfg = load_config(cfg_dir)
    assert cfg.classify("owner/diary-app") == ("hard_exclude", None)  # diary-app is also a listed lane
    assert cfg.classify("owner/demo-tool")[0] == "lane"  # positive control: same config, another listed lane


INCOMING_SPELLINGS = ["https://github.com/o/{}.git", "git@github.com:o/{}.git", "o/{}/", " O/{} ",
                      "https://github.com/o/{}/", "o/{}.GIT", "https://github.com/o/{}.git/"]


@pytest.mark.parametrize("pattern", INCOMING_SPELLINGS)
def test_every_spelling_of_an_incoming_repo_is_normalized(cfg, pattern):
    assert cfg.classify(pattern.format("diary-app")) == ("hard_exclude", None)
    kind, lane = cfg.classify(pattern.format("Demo-Tool"))  # positive control: same spelling, listed lane
    assert (kind, lane.name) == ("lane", "demo-tool")


@pytest.mark.parametrize("cwd", ["/home/x/src/secret-garden", "/home/x/src/secret-garden/", "/HOME/X/Secret-Garden/app"])
@pytest.mark.parametrize("entry", ["secret-garden/", " secret-garden", "Owner/Secret-Garden", "SECRET-GARDEN",
                                   "https://github.com/o/secret-garden.git"])
def test_hard_exclude_cwd_fragment_in_any_spelling(cfg_dir, entry, cwd):
    _edit(cfg_dir, "lanes.json", lambda d: d.update(hard_exclude=[entry]))
    cfg = load_config(cfg_dir)
    assert cfg.classify("owner/demo-tool", cwd) == ("hard_exclude", None)
    assert cfg.classify("owner/demo-tool", "/home/x/src/demo-tool")[0] == "lane"  # positive control


def test_classify_normalizes_entries_without_relying_on_load():
    lane = Lane("diary-app", "public", "a diary", "listed")
    base = dict(lanes={"diary-app": lane}, blocked_names=(), embargo_active=False, embargo_terms=())
    assert Config(hard_exclude=(" Owner/Diary-App.git ",), **base).classify("diary-app") == ("hard_exclude", None)
    assert Config(hard_exclude=("elsewhere",), **base).classify("diary-app")[0] == "lane"  # positive control


def test_a_degenerate_hard_exclude_entry_never_excludes_everything():
    lane = Lane("demo-tool", "public", "the demo tool", "listed")
    cfg = Config(lanes={"demo-tool": lane}, hard_exclude=("///", "   ", ""), blocked_names=(), embargo_active=False,
                 embargo_terms=())
    assert cfg.classify("o/demo-tool", "/home/x/src/demo-tool") == ("lane", lane)


@pytest.mark.parametrize("entry", ["abc", " abc ", "owner/abc", "secret-garden"])
def test_hard_exclude_entry_of_three_characters_is_fine(cfg_dir, entry):  # control for the short-entry rows
    _edit(cfg_dir, "lanes.json", lambda d: d.update(hard_exclude=[entry]))
    assert load_config(cfg_dir).hard_exclude == (entry.strip(),)


def test_names_to_block_normalizes_every_entry(cfg_dir):
    _edit(cfg_dir, "lanes.json",
          lambda d: d.update(hard_exclude=["Owner/Diary-App", "secret-garden/"],
                             blocked_names=[" Zephyr", "Owner/Codename-X", "  "]))
    cfg = load_config(cfg_dir)
    assert cfg.blocked_names == ("Zephyr", "Owner/Codename-X", "") and cfg.hard_exclude == ("Owner/Diary-App", "secret-garden/")
    names = cfg.names_to_block()
    assert {"Zephyr", "Owner/Codename-X", "codename-x", "diary-app", "Owner/Diary-App", "secret-garden",
            "garden-shed"} <= set(names)
    assert all(n and n == n.strip() for n in names)
    assert "demo-tool" not in names  # positive control: a plain public lane is never blocked


@pytest.mark.parametrize("key", ["", "/", "   "])
def test_a_lane_key_that_normalizes_to_nothing_fails(cfg_dir, key):
    assert load_config(cfg_dir)  # positive control: the unedited file loads
    entry = {"disclosure": "public", "label": "a label", "why": "a reason"}
    _edit(cfg_dir, "lanes.json", lambda d: d["lanes"].update({key: entry}))
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == "bad_shape:lanes"


def _lane_json(disclosure="public", extra=""):
    return '{"disclosure": "%s", %s"label": "a label", "why": "a reason"}' % (disclosure, extra)


def _lanes_file(body):
    return '{"lanes": %s, "hard_exclude": [], "blocked_names": []}' % body


REPEATED_KEY_CASES = [
    ("lane-key", "lanes.json",
     _lanes_file('{"garden-shed": %s, "garden-shed": %s}' % (_lane_json("headline"), _lane_json("public"))),
     "duplicate_lane:garden-shed"),
    ("top-level-key", "lanes.json",
     '{"lanes": {}, "hard_exclude": [], "hard_exclude": ["diary-app"], "blocked_names": []}', "duplicate_key:hard_exclude"),
    ("field-inside-a-lane", "lanes.json",
     _lanes_file('{"garden-shed": %s}' % _lane_json("headline", '"disclosure": "public", ')), "duplicate_key:disclosure"),
    ("embargo-active", "embargo.json", '{"active": true, "active": false, "terms": ["pine fair"]}', "duplicate_key:active"),
    ("embargo-terms", "embargo.json", '{"active": true, "terms": ["pine fair"], "terms": []}', "duplicate_key:terms"),
]


@pytest.mark.parametrize("name,text,code", [pytest.param(n, t, c, id=i) for i, n, t, c in REPEATED_KEY_CASES])
def test_a_repeated_json_key_fails_instead_of_last_one_winning(cfg_dir, name, text, code):
    (cfg_dir / name).write_text(text)
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == code


def test_the_same_files_without_repeated_keys_load(cfg_dir):  # positive control for the repeated-key table
    (cfg_dir / "lanes.json").write_text(_lanes_file('{"garden-shed": %s, "other-box": %s}' % (_lane_json("headline"),
                                                                                              _lane_json("public"))))
    (cfg_dir / "embargo.json").write_text('{"active": true, "terms": ["pine fair"]}')
    cfg = load_config(cfg_dir)
    assert cfg.lanes["garden-shed"].disclosure == "headline" and cfg.lanes["other-box"].disclosure == "public"


@pytest.mark.parametrize("text", ["pine fair", "pine  fair", "pine\nfair", "pine \t\n fair", "Pine-Fair", "pine_fair", "PINE - FAIR"])
def test_embargo_term_hits_across_separator_variants(cfg, text):
    assert cfg.embargo_hit(f"notes on {text} today")


@pytest.mark.parametrize("text", ["pinefair", "pines fair", "pine and fair", "spine fair", "alpine fair", "pine.fair"])
def test_embargo_term_does_not_hit_without_a_separator_or_word_start(cfg, text):  # controls for the variants above
    assert not cfg.embargo_hit(f"notes on {text} today")


def test_embargo_term_with_irregular_whitespace_still_hits(cfg_dir):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": ["  Pine \t  Fair  ", "   "]}))
    cfg = load_config(cfg_dir)
    assert cfg.embargo_hit("the pine fair") and cfg.embargo_hit("the pine-fair")
    assert not cfg.embargo_hit("the spine fair")  # control; the blank term must not match everything


INVISIBLE = [chr(c) for c in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060, 0x00AD)]  # zero-width, joiner, soft hyphen
INVISIBLE_IDS = [f"{ord(c):04X}" for c in INVISIBLE]


@pytest.mark.parametrize("zw", INVISIBLE, ids=INVISIBLE_IDS)
def test_invisible_characters_cannot_split_an_embargo_term(cfg, zw):
    assert cfg.embargo_hit(f"a pi{zw}ne fair piece")
    assert cfg.embargo_hit(f"a pine fa{zw}ir piece")
    assert cfg.embargo_hit(f"a {zw}pine fair piece")
    assert not cfg.embargo_hit(f"a pi{zw}ne walk piece")  # positive control: stripping invents no hits


@pytest.mark.parametrize("zw", INVISIBLE, ids=INVISIBLE_IDS)
def test_invisible_characters_pasted_into_a_term_do_not_stop_it_matching(cfg_dir, zw):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [f"pine{zw} fair", zw]}))
    cfg = load_config(cfg_dir)
    assert cfg.embargo_hit("the pine fair") and not cfg.embargo_hit("the spine fair")  # the zero-width-only term is skipped


@pytest.mark.parametrize("zw", INVISIBLE, ids=INVISIBLE_IDS)
def test_an_active_embargo_whose_only_term_is_invisible_fails(cfg_dir, zw):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [zw, " "]}))
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == "bad_shape:embargo.terms"


def test_active_embargo_with_one_usable_term_loads(cfg_dir):  # control for the empty or blank terms rows
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": ["", " launch codename "]}))
    assert load_config(cfg_dir).embargo_hit("the Launch Codename slipped")


@pytest.mark.parametrize("term", ["pine fair", "pine-fair", "pine_fair", "  Pine - Fair  ", "pine\t_\tfair"])
def test_embargo_term_separators_are_interchangeable(cfg_dir, term):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [term]}))
    cfg = load_config(cfg_dir)
    for text in ("pine fair", "Pine-Fair", "pine\nfair", "pine_fair", "the pine fairs"):
        assert cfg.embargo_hit(text), (term, text)
    for text in ("alpine fair", "pinefair", "spine fair"):  # controls: still needs a word start and a separator
        assert not cfg.embargo_hit(text), (term, text)


@pytest.mark.parametrize("junk", ["-", "_", " - _ ", "--"])
def test_a_separator_only_term_is_not_usable(cfg_dir, junk):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [junk, "pine fair"]}))
    cfg = load_config(cfg_dir)  # control: loads because "pine fair" is usable
    assert cfg.embargo_hit("the pine fair") and not cfg.embargo_hit("an unrelated - line_of_text")  # junk matches nothing
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [junk]}))
    with pytest.raises(ConfigError) as err:
        load_config(cfg_dir)
    assert str(err.value) == "bad_shape:embargo.terms"


# ---- accented terms: a term goes through the same normal form as the text ----

JOSE = "Jos" + chr(0xE9) + " Ram" + chr(0xED) + "rez"  # an accented embargo term


@pytest.mark.parametrize("text", ["we met " + JOSE + " today", "we met Jose Ramirez today",
                                  "we met Jose" + chr(0x301) + " Rami" + chr(0x301) + "rez today",
                                  "we met JOSE-RAMIREZ"], ids=["accented", "ascii", "decomposed", "caps_hyphen"])
def test_an_accented_embargo_term_hits_every_spelling(cfg_dir, text):
    (cfg_dir / "embargo.json").write_text(json.dumps({"active": True, "terms": [JOSE]}))
    cfg = load_config(cfg_dir)
    assert cfg.embargo_hit(text)
    assert not cfg.embargo_hit("we met Jose Ramos today")  # positive control
