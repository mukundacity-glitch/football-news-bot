"""The same story was published four times. These are the three failures behind it.

Observed on @FplVortexM: "Brennan Johnson has joined Everton from Crystal Palace"
posted at 19h, 5h, 4h and 2h; "Anthony Patterson ... wrexham from Sunderland" at
5h, 4h and 2h.
"""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

import main


# ── 1. The state that records a post was never written ──────────────────

def _persist_step_script() -> str:
    """Extract the 'Persist state' run: block from the workflow."""
    text = Path(".github/workflows/bot.yml").read_text()
    block = text.split("Persist state (dedup, daily count, deferred queue)")[1]
    block = block.split("run: |", 1)[1]
    lines = []
    for line in block.split("\n")[1:]:
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line[10:] if len(line) > 10 else line)
    return "\n".join(lines)


def test_persist_state_step_is_valid_bash():
    """The step was truncated mid-`if`, so bash aborted with "unexpected end of
    file" (exit 2) BEFORE running a single command. The bot posted to X and then
    never committed data/posted_news.json, so the next run had no memory of the
    post and published it again. Every run since was red on this step."""
    script = _persist_step_script()
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert result.returncode == 0, f"persist step is not valid bash: {result.stderr}"


def test_persist_step_still_commits_the_dedup_ledger():
    """Guards the payload, not just the syntax — a step that parses but no longer
    saves the ledger reproduces the same duplicate posting."""
    script = _persist_step_script()
    assert "data/posted_news.json" in script
    assert "git commit" in script and "git push" in script


# ── 2. The module could not be imported at all ──────────────────────────

def test_all_sources_compile_on_the_python_the_workflow_runs():
    """src/renderer.py used a nested f-string containing backslash escapes,
    which is a SyntaxError before Python 3.12. Both bot.yml and diff_test.yml
    pin 3.11, so main.py could not even be imported."""
    import compileall

    assert compileall.compile_dir("src", quiet=2, force=True), "src/ must compile"
    assert compileall.compile_file("main.py", quiet=2, force=True), "main.py must compile"


def test_workflow_python_version_matches_what_the_code_requires():
    for workflow in (".github/workflows/bot.yml", ".github/workflows/diff_test.yml"):
        text = Path(workflow).read_text()
        versions = re.findall(r'python-version:\s*"([\d.]+)"', text)
        assert versions, f"{workflow} must pin a Python version"
        for version in versions:
            major, minor = (int(part) for part in version.split(".")[:2])
            assert (major, minor) >= (3, 11), f"{workflow} pins {version}"


# ── 3. The fuzzy layer compared two different string formats ────────────

STORY = {
    "player": "Brennan Johnson", "event": "transfer",
    "from_key": "Crystal_Palace", "to_key": "Everton",
    "headline": "Brennan Johnson joins Everton on loan", "stage": 4,
}


def _fresh_ledger():
    return {"posted_hashes": [], "posted_headlines": [], "stories": {}}


def test_recorder_and_checker_share_one_key_builder():
    """They previously built the string separately: the recorder stored the raw
    headline text, the checker compared "player_event_stageN_active". Measured
    similarity 0.48 against a 0.90 threshold, so the layer never fired once."""
    data = _fresh_ledger()
    main.record_content_dedup(STORY, data)
    assert data["posted_headlines"] == [main._dedup_headline_key(STORY)]


def test_exact_repeat_is_caught():
    data = _fresh_ledger()
    main.record_content_dedup(STORY, data)
    duplicate, reason = main.is_duplicate_content(STORY, data)
    assert duplicate is True and reason == "content_hash"


@pytest.mark.parametrize("headline", [
    "Everton complete loan signing of Brennan Johnson",
    "DONE DEAL: Johnson to Everton",
    "Official: Brennan Johnson seals Everton switch",
])
def test_same_move_reworded_by_another_feed_is_caught(headline):
    """content_hash() folds the headline in, so a second feed's wording produces
    a different hash and slips past it. Catching that is the whole purpose of
    the fuzzy layer — and with club feeds added, the same move now genuinely
    does arrive from several sources."""
    data = _fresh_ledger()
    main.record_content_dedup(STORY, data)
    duplicate, reason = main.is_duplicate_content(dict(STORY, headline=headline), data)
    assert duplicate is True, f"reworded headline slipped through: {headline!r}"
    assert reason.startswith("fuzzy_headline")


def test_a_genuinely_different_story_still_publishes():
    """The dedup must not become a blanket mute — that is the opposite failure."""
    data = _fresh_ledger()
    main.record_content_dedup(STORY, data)
    other = {"player": "Bukayo Saka", "event": "injury", "to_key": "Arsenal",
             "headline": "Saka injury update", "stage": 1}
    assert main.is_duplicate_content(other, data)[0] is False


def test_a_story_with_no_player_yields_no_key():
    """An empty key must not fuzzy-match every other empty key and mute the bot."""
    assert main._dedup_headline_key({"event": "transfer"}) == ""
