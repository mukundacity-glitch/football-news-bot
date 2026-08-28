from __future__ import annotations

from pathlib import Path

import main


def test_live_scope_can_reserve_press_conferences_for_deadline_workflow():
    assert main._parse_live_event_scope("press_conference") == {
        "press_conference",
    }
    assert "press_conference" not in main._parse_live_event_scope(
        "transfer,loan,loan_option,injury,suspension"
    )


def test_deadline_workflow_uses_verified_roundup_not_removed_top_five_bot():
    deadline = Path(".github/workflows/fpl-deadline-news.yml").read_text(
        encoding="utf-8",
    )
    general = Path(".github/workflows/bot.yml").read_text(encoding="utf-8")

    assert "FPL Deadline Press Conference Round-Up" in deadline
    assert "tools/press_deadline_window.py" in deadline
    assert "LIVE_EVENT_SCOPE: press_conference" in deadline
    assert "python main.py" in deadline
    assert "Top-5" not in deadline and "fpl_deadline_news.py" not in deadline
    assert "LIVE_EVENT_SCOPE: transfer,loan,loan_option,injury,suspension" in general


def test_combined_roundup_caption_reports_every_included_manager():
    story = {
        "event": "press_conference",
        "player": "Dynamic Manager",
        "to_key": "Arsenal",
        "roundup": [f"Club {n} — Manager {n}: Update" for n in range(1, 21)],
    }

    caption = main.format_press_conference_post(story)

    assert "20 verified manager updates" in caption
    assert "All clubs and key quotes" in caption
