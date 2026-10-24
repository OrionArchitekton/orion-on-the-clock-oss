import json

import pytest

from daily import gate as G
from daily import paths
from daily_helpers import good_script, no_confessional_script, quiet_script

DAY = "2026-10-16"
SCHEMA = json.loads(paths.SCHEMA_PATH.read_text())
# The structured-output call accepts only a plain subset of JSON Schema: no const, no pattern.
SCHEMA_KEYWORDS = {"type", "enum", "maxLength", "maxItems", "items", "properties", "required", "additionalProperties"}
JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}
SHAPE_CODES = {"not_object", "keys", "type", "too_long", "too_many", "schema_const"}
ASTRAL = chr(0x1F600)  # one code point, two UTF-16 units, four UTF-8 bytes
REQUIRED_TEXT = ["story_item_id", "title", "description", "cold_open.host", "cold_open.on_screen", "scoreboard.host",
                 "steal_this.host", "steal_this.on_screen", "out.host", "out.teaser"]


def test_schema_file_matches_the_contract():
    schema = json.loads(paths.SCHEMA_PATH.read_text())
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["required"] == list(G.TOP)
    assert schema["properties"]["schema"]["enum"] == ["daily-build/script/v1"]
    order = list(G.TOP)
    assert order[order.index("story") + 1:order.index("story") + 3] == ["steal_this", "confessional"]


def test_validator_limits_match_the_schema():
    props = json.loads(paths.SCHEMA_PATH.read_text())["properties"]
    assert {k: props[k]["maxLength"] for k in G.TOP_LIMITS} == G.TOP_LIMITS
    assert set(G.NESTED) == {k for k, t in G.TOP.items() if t is dict}
    for section, fields in G.NESTED.items():
        sp = props[section]
        assert sp["additionalProperties"] is False and sp["required"] == list(fields)
        assert set(sp["properties"]) == set(fields)
        for field, lim in fields.items():
            if isinstance(lim, tuple):
                assert (sp["properties"][field]["items"]["maxLength"], sp["properties"][field]["maxItems"]) == lim
            else:
                assert sp["properties"][field]["maxLength"] == lim


def test_validator_types_match_the_schema():
    props = SCHEMA["properties"]
    assert {k: JSON_TYPES[props[k]["type"]] for k in G.TOP} == G.TOP
    for section, fields in G.NESTED.items():
        for field, lim in fields.items():
            field_schema = props[section]["properties"][field]
            if isinstance(lim, tuple):
                assert field_schema["type"] == "array" and field_schema["items"]["type"] == "string"
            else:
                assert field_schema["type"] == "string"


def schema_violations(schema: dict, value, path: str = "$") -> list:
    """A minimal reader for exactly the keywords script_schema.json may use; any other keyword fails the test."""
    assert set(schema) <= SCHEMA_KEYWORDS, set(schema) - SCHEMA_KEYWORDS
    if not isinstance(value, JSON_TYPES[schema["type"]]):
        return [path]
    out = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(path)
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        out.append(path)
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        out.append(path)
    if schema["type"] == "array":
        for i, item in enumerate(value):
            out += schema_violations(schema["items"], item, f"{path}[{i}]")
    if schema["type"] == "object":
        props = schema["properties"]
        if set(schema["required"]) - set(value) or set(value) - set(props):
            out.append(path)
        for key, sub in value.items():
            if key in props:
                out += schema_violations(props[key], sub, f"{path}.{key}")
    return out


def schema_corpus():
    """Valid scripts, then single-fault mutants: a missing or extra key, wrong types, and each limit at and past the edge."""
    base = good_script()
    wrong = [0, True, None, [], {}, "s"]
    yield base
    yield no_confessional_script()
    yield quiet_script()
    yield {**base, "extra": 1}
    yield {**base, "schema": "v0"}
    for key in base:
        yield {k: v for k, v in base.items() if k != key}
        for w in wrong:
            yield {**base, key: w}
    for key, lim in G.TOP_LIMITS.items():
        for n in (lim, lim + 1):
            yield {**base, key: "x" * n}
            yield {**base, key: ASTRAL * n}
    for section, fields in G.NESTED.items():
        yield {**base, section: {**base[section], "extra": 1}}
        for field, lim in fields.items():
            yield {**base, section: {k: v for k, v in base[section].items() if k != field}}
            for w in wrong:
                yield {**base, section: {**base[section], field: w}}
            if isinstance(lim, tuple):
                for count in (lim[1], lim[1] + 1):
                    yield {**base, section: {**base[section], field: ["x"] * count}}
                for n in (lim[0], lim[0] + 1):
                    yield {**base, section: {**base[section], field: ["x" * n]}}
                    yield {**base, section: {**base[section], field: [ASTRAL * n]}}
                yield {**base, section: {**base[section], field: ["fine", 1]}}
            else:
                for n in (lim, lim + 1):
                    yield {**base, section: {**base[section], field: "x" * n}}
                    yield {**base, section: {**base[section], field: ASTRAL * n}}


def test_validator_agrees_with_the_schema_on_shape():
    verdicts = []
    for script in schema_corpus():
        shape = [p for p in G.validate_script(script, DAY) if p.split(":")[0] in SHAPE_CODES]
        verdicts.append(not shape)
        assert (not shape) == (not schema_violations(SCHEMA, script)), (shape, script)
    # both verdicts are exercised, so the agreement is not two empty lists
    assert verdicts.count(True) >= 20 and verdicts.count(False) >= 100


def test_good_and_quiet_scripts_pass():  # positive controls
    assert G.validate_script(good_script(), DAY) == []
    assert G.validate_script(quiet_script(), DAY) == []
    assert G.validate_script(no_confessional_script(), DAY) == []


def test_rejects_missing_or_extra_keys():
    s = good_script()
    del s["title"]
    assert G.validate_script(s, DAY) == ["keys:title"]
    assert G.validate_script({**good_script(), "extra": 1}, DAY) == ["keys:extra"]
    assert G.validate_script(good_script(**{"out": {"host": "x"}}), DAY) == ["keys:out"]
    s = good_script()
    del s["steal_this"]
    assert G.validate_script(s, DAY) == ["keys:steal_this"]
    assert G.validate_script(good_script(**{"steal_this": {"host": "x"}}), DAY) == ["keys:steal_this"]
    both = {**good_script()["steal_this"], "extra": "x"}
    assert G.validate_script(good_script(**{"steal_this": both}), DAY) == ["keys:steal_this"]


def test_non_string_keys_are_reported_not_raised():
    assert G.validate_script({**good_script(), 1: "x"}, DAY) == ["keys:1"]
    assert G.validate_script({**good_script(), 1: "x", None: "y", "extra": 2}, DAY) == ["keys:1,None,extra"]
    gone = {k: v for k, v in good_script().items() if k != "title"}
    assert G.validate_script({**gone, 2: "x"}, DAY) == ["keys:2,title"]
    assert G.validate_script(good_script(**{"out": {"host": "x", "teaser": "y", 7: "z"}}), DAY) == ["keys:out"]


def test_rejects_wrong_types():
    assert G.validate_script(good_script(quiet="no"), DAY) == ["type:quiet"]
    assert G.validate_script(good_script(**{"story.host": "one line"}), DAY) == ["type:story.host"]
    assert G.validate_script(good_script(**{"steal_this.host": ["a line"]}), DAY) == ["type:steal_this.host"]
    assert G.validate_script(good_script(steal_this="a line"), DAY) == ["type:steal_this"]


def test_rejects_lengths_and_counts():
    assert G.validate_script(good_script(title="x" * 71), DAY) == ["too_long:title"]
    assert G.validate_script(good_script(title="x" * 70), DAY) == []
    assert G.validate_script(good_script(**{"cold_open.on_screen": "y" * 43}), DAY) == ["too_long:cold_open.on_screen"]
    assert G.validate_script(good_script(**{"story.on_screen": ["a"] * 5}), DAY) == ["too_many:story.on_screen"]
    assert G.validate_script(good_script(**{"steal_this.on_screen": "y" * 43}), DAY) == ["too_long:steal_this.on_screen"]
    assert G.validate_script(good_script(**{"steal_this.on_screen": "y" * 42}), DAY) == []
    assert G.validate_script(good_script(**{"steal_this.host": "y" * 401}), DAY) == ["too_long:steal_this.host"]
    assert G.validate_script(good_script(**{"steal_this.host": "y" * 400}), DAY) == []


def test_lengths_count_code_points_not_utf16_units_or_bytes():
    assert G.validate_script(good_script(title=ASTRAL * 70), DAY) == []
    assert G.validate_script(good_script(title=ASTRAL * 71), DAY) == ["too_long:title"]
    assert G.validate_script(good_script(**{"steal_this.on_screen": ASTRAL * 42}), DAY) == []
    assert G.validate_script(good_script(**{"steal_this.on_screen": ASTRAL * 43}), DAY) == ["too_long:steal_this.on_screen"]
    assert G.validate_script(good_script(**{"story.on_screen": [ASTRAL * 42]}), DAY) == []
    assert G.validate_script(good_script(**{"story.on_screen": [ASTRAL * 43]}), DAY) == ["too_long:story.on_screen"]


def test_rejects_day_and_schema_tag():
    assert G.validate_script(good_script(day="2026-10-15"), DAY) == ["day_mismatch"]
    assert G.validate_script(good_script(schema="v0"), DAY) == ["schema_const"]


def test_a_story_script_needs_its_required_fields():
    assert G.validate_script(good_script(story_item_id=""), DAY) == ["empty_required_field"]
    assert G.validate_script(good_script(**{"confessional.quote_id": " "}), DAY) == ["empty_required_field"]
    assert G.validate_script(good_script(**{"story.host": []}), DAY) == ["empty:story.host"]


@pytest.mark.parametrize("name", REQUIRED_TEXT)
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_every_required_text_field_is_non_blank_unless_quiet(name, blank):
    assert G.validate_script(good_script(**{name: blank}), DAY) == ["empty_required_field"]
    # positive controls: the same blank is fine on a quiet script, and a filled field is fine on a story script
    assert G.validate_script(good_script(quiet=True, quiet_reason="no_items", **{name: blank}), DAY) == []
    assert G.validate_script(good_script(**{name: "Filled in"}), DAY) == []


@pytest.mark.parametrize("name, lines", [
    ("story.host", ["", "A real line"]),
    ("story.host", ["A real line", "   "]),
    ("story.on_screen", [""]),
    ("story.on_screen", ["Fine", " ", "Also fine"]),
])
def test_a_blank_story_line_is_an_empty_required_field(name, lines):
    assert G.validate_script(good_script(**{name: lines}), DAY) == ["empty_required_field"]
    assert G.validate_script(good_script(quiet=True, quiet_reason="no_items", **{name: lines}), DAY) == []


def test_story_on_screen_is_optional_but_story_host_is_not():
    assert G.validate_script(good_script(**{"story.on_screen": []}), DAY) == []
    assert G.validate_script(good_script(**{"story.on_screen": ["a", "b", "c", "d"]}), DAY) == []
    assert G.validate_script(good_script(**{"story.host": ["one line"]}), DAY) == []
    assert G.validate_script(good_script(**{"story.host": []}), DAY) == ["empty:story.host"]


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_a_quiet_script_needs_a_reason(reason):
    assert G.validate_script(quiet_script(reason), DAY) == ["empty_required_field"]
    assert G.validate_script(quiet_script("no_material"), DAY) == []  # positive control


def test_the_confessional_is_optional():
    s = no_confessional_script()
    assert s["confessional"] == {"host_intro": "", "quote_id": ""}
    assert G.validate_script(s, DAY) == []
    # a non-empty pair is the other valid state
    assert G.validate_script(good_script(), DAY) == []


@pytest.mark.parametrize("over", [
    {"confessional.quote_id": "", "confessional.host_intro": "A real intro line"},
    {"confessional.quote_id": "q:codex:0a1b2c3d", "confessional.host_intro": ""},
])
def test_a_half_confessional_is_a_mismatch(over):
    assert G.validate_script(good_script(**over), DAY) == ["confessional_mismatch"]


@pytest.mark.parametrize("over", [
    {"confessional.quote_id": " "},
    {"confessional.quote_id": " ", "confessional.host_intro": ""},
    {"confessional.quote_id": "", "confessional.host_intro": " "},
    {"confessional.quote_id": "q:codex:0a1b2c3d", "confessional.host_intro": "  "},
    {"confessional.quote_id": "\t", "confessional.host_intro": "A real intro line"},
])
def test_a_whitespace_only_confessional_field_stays_an_empty_required_field(over):
    assert G.validate_script(good_script(**over), DAY) == ["empty_required_field"]


def test_each_empty_field_is_reported_once_with_the_others():
    s = good_script(story_item_id="", title=" ", **{"steal_this.host": "", "confessional.quote_id": " "})
    assert G.validate_script(s, DAY) == ["empty_required_field"]
    s = good_script(**{"story.host": [], "steal_this.host": ""})
    assert G.validate_script(s, DAY) == ["empty_required_field", "empty:story.host"]
    s = good_script(**{"confessional.host_intro": "", "steal_this.on_screen": ""})
    assert G.validate_script(s, DAY) == ["empty_required_field", "confessional_mismatch"]


def test_a_quiet_script_skips_the_story_rules():
    quiet = {"quiet": True, "quiet_reason": "no_items"}
    assert G.validate_script(good_script(story_item_id="", title="", **{"story.host": []}, **quiet), DAY) == []
    assert G.validate_script(good_script(**{"confessional.quote_id": "q:x:1"}, **quiet), DAY) == []


def test_story_rules_wait_for_a_valid_shape():
    # a shape failure returns alone, so a content rule never rides along with it
    assert G.validate_script(good_script(title="x" * 71, **{"steal_this.host": ""}), DAY) == ["too_long:title"]


def test_not_an_object():
    assert G.validate_script([], DAY) == ["not_object"]
