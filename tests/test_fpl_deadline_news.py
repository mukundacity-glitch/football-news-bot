from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools import fpl_deadline_news as fdn


def _fpl_fixture(deadline: datetime | None = None):
    deadline = deadline or datetime(2026, 8, 15, 10, 30, tzinfo=timezone.utc)
    return {
        "events": [{"id": 1, "name": "Gameweek 1", "deadline_time": deadline.isoformat().replace("+00:00", "Z")}],
        "teams": [
            {"id": 1, "name": "Arsenal", "short_name": "ARS", "strength": 2, "strength_overall_home": 1300, "strength_overall_away": 1300},
            {"id": 2, "name": "Man City", "short_name": "MCI", "strength": 1, "strength_overall_home": 1350, "strength_overall_away": 1350},
            {"id": 3, "name": "Chelsea", "short_name": "CHE", "strength": 3, "strength_overall_home": 1200, "strength_overall_away": 1200},
            {"id": 4, "name": "Brighton", "short_name": "BHA", "strength": 4, "strength_overall_home": 1100, "strength_overall_away": 1100},
            {"id": 5, "name": "Liverpool", "short_name": "LIV", "strength": 2, "strength_overall_home": 1320, "strength_overall_away": 1320},
            {"id": 6, "name": "PSG", "short_name": "PSG", "strength": 10, "strength_overall_home": 1, "strength_overall_away": 1},
        ],
        "elements": [
            {
                "id": 10, "first_name": "Bukayo", "second_name": "Saka", "web_name": "Saka", "team": 1,
                "status": "d", "chance_of_playing_next_round": 75, "chance_of_playing_this_round": 75,
                "news": "Late fitness test", "selected_by_percent": "42.1", "now_cost": 101,
            },
            {
                "id": 11, "first_name": "Erling", "second_name": "Haaland", "web_name": "Haaland", "team": 2,
                "status": "i", "chance_of_playing_next_round": 0, "chance_of_playing_this_round": 0,
                "news": "Ankle injury", "selected_by_percent": "63.0", "now_cost": 150,
            },
            {
                "id": 12, "first_name": "Cole", "second_name": "Palmer", "web_name": "Palmer", "team": 3,
                "status": "a", "chance_of_playing_next_round": 100, "chance_of_playing_this_round": 100,
                "news": "Available after illness", "selected_by_percent": "37.0", "now_cost": 105,
            },
            {
                "id": 13, "first_name": "Mohamed", "second_name": "Salah", "web_name": "Salah", "team": 5,
                "status": "s", "chance_of_playing_next_round": 0, "chance_of_playing_this_round": 0,
                "news": "Suspended", "selected_by_percent": "50.0", "now_cost": 140,
            },
            {
                "id": 99, "first_name": "Kylian", "second_name": "Mbappe", "web_name": "Mbappe", "team": 6,
                "status": "i", "chance_of_playing_next_round": 0, "chance_of_playing_this_round": 0,
                "news": "Hamstring injury", "selected_by_percent": "99.9", "now_cost": 150,
            },
        ],
    }


def test_deadline_window_is_deadline_minus_30_minutes():
    deadline = datetime(2026, 8, 15, 10, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)
    ok, reason, event = fdn.deadline_window_status(
        fpl,
        now=deadline - timedelta(minutes=30),
        margin_minutes=30,
        window_minutes=20,
    )
    assert ok is True
    assert event.name == "Gameweek 1"
    assert "inside" in reason

    ok, reason, _ = fdn.deadline_window_status(
        fpl,
        now=deadline - timedelta(minutes=31),
        margin_minutes=30,
        window_minutes=20,
    )
    assert ok is False
    assert "too_early" in reason


def test_table_names_map_to_fpl_ids_with_common_aliases():
    fpl = _fpl_fixture()
    mapped = fdn.map_table_names_to_fpl_team_ids(
        ["Manchester City", "Chelsea", "Liverpool", "Brighton & Hove Albion"],
        fpl,
    )
    assert mapped == [2, 3, 5, 4]


def test_collect_cards_uses_top_teams_only_and_selects_top_three():
    fpl = _fpl_fixture()
    cards = fdn.collect_cards(fpl, [1, 2, 3, 5], limit=3)
    assert len(cards) == 3
    names = {c.player for c in cards}
    # PSG player is critical but excluded because PSG is not in the PL Top teams.
    assert "Kylian Mbappe" not in names
    # Priority is outs/suspensions first, then returns, then doubts. Display is
    # alphabetical by team after selection, so assert set rather than order.
    assert names == {"Erling Haaland", "Mohamed Salah", "Cole Palmer"}
    assert any(c.symbol == "❌" for c in cards)
    assert any(c.symbol == "✅" for c in cards)


def test_tweet_is_concise_and_uses_official_fpl_source_footer():
    fpl = _fpl_fixture()
    event = fdn.find_next_event(fpl, datetime(2026, 8, 14, tzinfo=timezone.utc))
    cards = fdn.collect_cards(fpl, [1, 2, 3, 5], limit=3)
    tweet = fdn.build_tweet(cards, event)
    assert "FPL TOP 5 TEAM NEWS" in tweet
    assert "Official FPL" in tweet
    assert "Source: Official FPL" in tweet
    assert "#PremierLeague" in tweet
    assert len(tweet) <= 280


def test_deadline_post_id_is_one_per_gameweek():
    fpl = _fpl_fixture()
    event = fdn.find_next_event(fpl, datetime(2026, 8, 14, tzinfo=timezone.utc))
    cards = fdn.collect_cards(fpl, [1, 2, 3, 5], limit=3)
    assert fdn.deadline_post_id(event, cards) == "fpl_deadline_top5_gw_1"
