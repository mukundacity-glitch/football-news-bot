"""Regression coverage for transfer direction after FPL updates a completed move."""

import main


def _fpl(team_id: int):
    return {
        "teams": [
            {"id": 1, "name": "Arsenal", "short_name": "ARS"},
            {"id": 2, "name": "Chelsea", "short_name": "CHE"},
        ],
        "elements": [
            {
                "id": 9001,
                "first_name": "Example",
                "second_name": "Player",
                "web_name": "Player",
                "team": team_id,
            }
        ],
    }


def test_completed_inbound_transfer_keeps_grounded_origin_after_fpl_update():
    story = main.build_story(
        "Chelsea have completed the signing of Example Player from Arsenal.",
        _fpl(2),
    )

    assert story["event"] == "transfer"
    assert story["from_key"] == "arsenal"
    assert story["to_key"] == "chelsea"
    assert story["from_key"] != story["to_key"]


def test_current_fpl_club_remains_origin_for_outbound_move():
    story = main.build_story(
        "Example Player has joined Chelsea from Arsenal.",
        _fpl(1),
    )

    assert story["event"] == "transfer"
    assert story["from_key"] == "arsenal"
    assert story["to_key"] == "chelsea"
    assert story["from_key"] != story["to_key"]
