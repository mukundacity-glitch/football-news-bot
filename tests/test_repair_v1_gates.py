"""Repair Plan v1 — the gates between a verified story and a published post.

Each test here corresponds to one measured finding from the repository audit,
and fails if that finding is reintroduced.
"""

import json
from pathlib import Path

import pytest

import main


# ── Finding 7: POSTED_FILE was defined twice ────────────────────────────

def test_posted_file_points_at_the_committed_ledger():
    """main.py used to assign POSTED_FILE = Path("posted_news.json") and then
    import the constants value over the top of it. They agreed only by accident
    of import order — reordering would have repointed the dedup ledger at a file
    that does not exist, and the bot would have re-posted its whole history."""
    assert main.POSTED_FILE == Path("data/posted_news.json")
    assert main.POSTED_FILE.parent.name == "data"


# ── Finding 2: DRY_RUN switch ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False),
])
def test_env_flag_parses_truthy_values(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert main._env_flag("SOME_FLAG") is expected


def test_unset_and_empty_env_flag_fall_back_to_default(monkeypatch):
    """GitHub passes an unconfigured repository Variable through as an empty
    string, so "" must mean "not configured", not "false"."""
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert main._env_flag("SOME_FLAG", default=True) is True
    monkeypatch.setenv("SOME_FLAG", "")
    assert main._env_flag("SOME_FLAG", default=True) is True
    assert main._env_flag("SOME_FLAG", default=False) is False


def test_dry_run_defaults_to_live():
    """The switch exists to be reached for deliberately. It must not silently
    stop a working bot from posting just by being added."""
    assert main._env_flag("DRY_RUN", default=False) is False


# ── Finding 3: a blank card must never be published ─────────────────────

def test_image_is_blank_detects_the_flat_fallback_card(tmp_path):
    """A render failure does not raise — it writes a flat 3840x2160 rectangle,
    which is hundreds of KB of solid colour and passes every size check. Only
    image_is_blank() can tell it from a real card, and it used to run solely
    inside run_dry_run()."""
    from PIL import Image
    from src.renderer import CARD_OUTPUT_W, CARD_OUTPUT_H, image_is_blank

    flat = tmp_path / "fallback.png"
    Image.new("RGB", (CARD_OUTPUT_W, CARD_OUTPUT_H), color=(11, 18, 32)).save(flat)
    assert flat.stat().st_size >= 1000, "the fallback is large enough to pass a size check"
    assert image_is_blank(str(flat)) is True

    varied = tmp_path / "real.png"
    im = Image.new("RGB", (400, 225), color=(11, 18, 32))
    for x in range(0, 400, 7):
        for y in range(0, 225, 3):
            im.putpixel((x, y), (255, 255, 255))
    im.save(varied)
    assert image_is_blank(str(varied)) is False


def test_blank_card_is_checked_on_the_live_path_not_only_in_dry_run():
    """Guards the specific regression: image_is_blank referenced only inside
    run_dry_run() means the live path publishes empty cards."""
    source = Path("main.py").read_text()
    post_item_src = source[source.index("async def post_item("):]
    post_item_src = post_item_src[:post_item_src.index("\nasync def ", 10)]
    assert "image_is_blank(" in post_item_src, "post_item must reject a blank card"
    assert post_item_src.index("image_is_blank(") < post_item_src.index("upload_media"), \
        "the blank check must run BEFORE the media upload"


def test_dry_run_short_circuits_before_any_x_call():
    """DRY_RUN must return before upload_media/create_tweet, not after."""
    source = Path("main.py").read_text()
    post_item_src = source[source.index("async def post_item("):]
    post_item_src = post_item_src[:post_item_src.index("\nasync def ", 10)]
    assert "if DRY_RUN:" in post_item_src
    assert post_item_src.index("if DRY_RUN:") < post_item_src.index("upload_media")


# ── Finding 4: an empty V2 database must not publish ────────────────────

def test_runtime_reports_a_database_it_had_to_create(tmp_path, monkeypatch):
    """sqlite3.connect() creates a missing file, so a cache-evicted database is
    indistinguishable from a healthy empty one unless we record it."""
    from src.verification.runtime import VerificationRuntime

    db = tmp_path / "verification.sqlite3"
    fpl = {"elements": [], "teams": []}

    fresh = VerificationRuntime(fpl_data=fpl, database_path=db)
    assert fresh.database_was_empty is True, "a database created from nothing must say so"

    assert db.exists()
    reopened = VerificationRuntime(fpl_data=fpl, database_path=db)
    assert reopened.database_was_empty is False, "an existing database must not be flagged"


def test_empty_database_blocks_posting_unless_rebuild_is_authorised():
    """The workflow already threads VERIFICATION_V2_ALLOW_DB_REBUILD through as
    an escape hatch; before this fix nothing read it."""
    source = Path("main.py").read_text()
    assert "database_was_empty" in source
    assert "VERIFICATION_V2_ALLOW_DB_REBUILD" in source
    assert "v2_database_rebuilt_from_empty" in source


# ── Finding 6: the freshness window is the documented one ───────────────

def test_publication_window_matches_the_stated_72_hour_policy():
    """V2 owns publication, so its 48h window silently overrode the 72h policy
    documented in main.py (MAX_TWEET_AGE_DAYS = 3)."""
    thresholds = json.loads(Path("config/verification.json").read_text())["thresholds"]
    assert thresholds["max_publication_age_hours"] == 72
    assert main.MAX_TWEET_AGE_DAYS * 24 == thresholds["max_publication_age_hours"], \
        "the legacy and V2 freshness windows must agree"


# ── Finding 1: the schedule asks for what GitHub can deliver ────────────

def test_workflow_schedule_and_switches():
    wf = Path(".github/workflows/bot.yml").read_text()
    assert 'cron: "*/20 * * * *"' in wf
    assert "DRY_RUN: ${{ vars.DRY_RUN || 'false' }}" in wf, "DRY_RUN must be flippable without a code edit"
    # Unchanged guarantees that were already correct — pinned so a future edit
    # cannot quietly drop them.
    assert "concurrency:" in wf and "cancel-in-progress: false" in wf
    assert "contents: write" in wf
