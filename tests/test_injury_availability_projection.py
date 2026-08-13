from __future__ import annotations

import main


def _fpl(status, chance, news):
    return {
        "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
        "elements": [{
            "id": 10, "code": 123, "first_name": "Dynamic", "second_name": "Player",
            "team": 1, "status": status, "news": news, "news_added": "",
            "chance_of_playing_this_round": chance,
        }],
    }


def test_fpl_doubt_projects_doubtful():
    item = main.fetch_fpl_injury_news(_fpl("d", 50, "Ankle injury - 50% chance of playing"))[0]
    assert item["_fpl_pre_built"]["availability_status"] == "DOUBTFUL"


def test_fpl_injury_projects_out():
    item = main.fetch_fpl_injury_news(_fpl("i", 0, "Hamstring injury - ruled out"))[0]
    assert item["_fpl_pre_built"]["availability_status"] == "OUT"


def test_fpl_returning_cue_projects_returning():
    item = main.fetch_fpl_injury_news(_fpl("d", 75, "Back in training following recovery"))[0]
    assert item["_fpl_pre_built"]["availability_status"] == "RETURNING"
