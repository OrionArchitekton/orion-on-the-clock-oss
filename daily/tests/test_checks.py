"""The built-in checks (daily/checks/): the redactor, the slop gate and the secret-shape scanner, loaded the way the
gate loads them (by file location), plus the light voice lint. Synthetic strings only; secret-shaped values are built
at run time so this file holds none."""
from __future__ import annotations

import pytest

from daily import paths, textcheck, voicelint

CLEAN = "The new browser pool shipped today and every single test passed."
EM = chr(0x2014)


@pytest.fixture(scope="module")
def redactor():
    return textcheck.load_module(paths.CHECKS_DIR / "redactor.py", "redactor")


@pytest.fixture(scope="module")
def slop():
    return textcheck.load_module(paths.CHECKS_DIR / "slop_gate.py", "slop_gate")


@pytest.fixture(scope="module")
def secrets():
    return textcheck.load_module(paths.CHECKS_DIR / "secret_scan.py", "secret_scan")


# ---- redactor ----

def test_clean_text_publishes(redactor):  # positive control
    assert redactor.decision(CLEAN) == ("PUBLISH", [])
    assert redactor.decision("We met at 20:00:05 and the ratio was 3:2, a:b:c style.") == ("PUBLISH", [])
    assert redactor.decision("The public resolver 2606:4700::1111 answered.") == ("PUBLISH", [])
    assert redactor.decision("We run it 24/7 and the a/b test is done.") == ("PUBLISH", [])
    assert redactor.decision("The public resolver 8.8.8.8 answered.") == ("PUBLISH", [])


@pytest.mark.parametrize("text,cls", [
    ("The token ghp_" + "a1" * 12 + " leaked.", "secret"),
    ("Key sk-" + "b2" * 10 + " found.", "secret"),
    ("AKIA" + "ABCDEFGHIJKLMNOP" + " is an AWS key id.", "secret"),
    ("Header Bearer " + "c3" * 8 + " sent.", "secret"),
    ("Key AIza" + "x1" * 18 + " leaked.", "secret"),          # every token here is built at run time
    ("Token hf_" + "Ab3" * 10 + " leaked.", "secret"),
    ("Token dp.st." + "y" * 20 + " leaked.", "secret"),
    ("Bot " + "123456789" + ":" + "AA" + "b" * 33 + " leaked.", "secret"),
    ("The node fd12:3456::7 answered.", "network"),
    ("The link fe80::1 answered.", "network"),
    ("-----BEGIN RSA PRIVATE KEY-----", "secret"),
    ("A run of " + "Z" * 44 + " is a secret shape.", "secret"),
    ("Mail someone@example.com about it.", "email"),
    ("The box at 192.168.1.20 rebooted.", "network"),
    ("The tailnet node " + ".".join(["100", "101", "7", "9"]) + " answered.", "network"),  # built: no literal one here
    ("It listens on localhost:3000 now.", "network"),
    ("The file in /home/someone/notes moved.", "home-path"),
    ("The folder /Users/someone/Desktop moved.", "home-path"),
    ("We merged #123 this morning.", "internal-ref"),
    ("PR 45 landed.", "internal-ref"),
    ("The branch claude/fix-the-thing merged.", "internal-ref"),
])
def test_each_class_abstains(redactor, text, cls):
    verdict, hits = redactor.decision(text)
    assert verdict == "ABSTAIN" and cls in {c for c, _ in hits}


def test_a_hit_is_masked_never_returned_whole(redactor):
    token = "ghp_" + "Q7" * 15
    _, hits = redactor.decision(f"The token {token} leaked.")
    assert hits and all(token not in snippet for _, snippet in hits)
    assert all("*" in snippet for _, snippet in hits)


# ---- slop gate ----

def test_slop_clean_and_hits(slop):
    assert slop.scan(CLEAN) == []
    assert ("banned_dash", "U+2014") in slop.scan(f"A long day {EM} done.")
    assert ("ai_slop_lexeme", "delve") in slop.scan("Let us delve into it.")
    assert ("ai_slop_lexeme", "a myriad of") in slop.scan("We saw a\nmyriad   of bugs.")  # a wrapped phrase still hits
    assert slop.scan("See https://example.com/seamless-rollbacks for more.") == []  # a link is not prose


def test_the_slop_list_holds_no_long_dash_literal():
    assert EM not in (paths.CHECKS_DIR / "slop_gate.py").read_text(encoding="utf-8")


# ---- secrets scanner ----

def test_secrets_clean_text_and_an_allowed_host(secrets):  # positive control
    assert secrets.scan_output(CLEAN, public_hosts=("youtube.com",)) == []
    assert secrets.scan_output("Watch it on https://youtube.com/shorts today.", public_hosts=("youtube.com",)) == []


@pytest.mark.parametrize("text,kind", [
    ("Write to someone@example.com today.", "email-like text"),
    ("Open https://internal.example.net/admin now.", "host not on the public allowlist"),
    ("Open https://user:pw@youtube.com/x now.", "URL with a user part"),
    ("It runs at http://127.0.0.1:8080 here.", "loopback URL"),
    ("It runs at localhost:3000 here.", "loopback address with a port"),
    ("api_key = " + "x" * 12, "credential"),  # built at run time: no credential-shaped literal in this file
    ("JWT eyJ" + "a" * 10 + "." + "b" * 10 + ".c", "JWT"),
    ("Key AIza" + "x1" * 18 + " here.", "Google API key"),
    ("Token hf_" + "Ab3" * 10 + " here.", "Hugging Face token"),
    ("Token dp.st." + "y" * 20 + " here.", "Doppler token"),
    ("Bot " + "123456789" + ":" + "AA" + "b" * 33 + " here.", "Telegram bot token"),
    ("Mail someone%40example.com now.", "email-like text"),  # percent-encoded still found
])
def test_secrets_findings_name_a_kind(secrets, text, kind):
    found = secrets.scan_output(text, public_hosts=("youtube.com",))
    assert any(f.startswith(kind) for f in found), found


def test_secrets_findings_never_carry_the_value(secrets):
    value = "ghp_" + "R9" * 15
    found = secrets.scan_output(f"token: {value} and https://secret-host.example.org/x and a@b.example.com",
                                public_hosts=())
    assert found and all(value not in f and "secret-host" not in f and "a@b" not in f for f in found)


# ---- the light voice lint ----

def test_voicelint_rules():
    lint = voicelint.Linter()
    assert lint.lint_line("So I shipped it and you can try it too.") == []  # positive control
    assert ("heres", "here's") in lint.lint_line("Here's the thing about it.")
    assert ("nobody", "no one") in lint.lint_line("No one saw it coming at all.")
    assert ("writerly", "clever") in lint.lint_line("That was clever.")
    assert ("impersonal", "The pool restarts every browser after each lease ends.") in \
        lint.lint_line("The pool restarts every browser after each lease ends.")


def test_voicelint_takes_the_operators_word_list():
    mine = voicelint.Linter(["robust"])
    assert ("writerly", "robust") in mine.lint_line("It is robust.")
    assert ("writerly", "clever") not in mine.lint_line("That was clever.")


def test_voicelint_counts_and_spells_numbers():
    assert voicelint.norm_tokens("We ran 43 tests") == ["we", "ran", "forty", "three", "tests"]
    assert voicelint.num_words(240) == "two hundred forty"
    result = voicelint.Linter().lint([("a", "Here's one."), ("b", "So I did it.")])
    assert result["open"] == 1 and result["counts"]["heres"] == 1 and result["flags"][0]["line"] == "a"
