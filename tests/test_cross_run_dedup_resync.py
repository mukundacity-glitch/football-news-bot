"""
Locks the cross-run duplicate-posting fix (Aug 2026): the schedule
(`.github/workflows/bot.yml`, cron */20) allows overlapping runs
(`concurrency.cancel-in-progress: false`), and a run only pushes its
dedup memory (data/posted_news.json) to origin/main as its very last
step -- well after any live X post already happened. A slow or queued
run's `data` dict, loaded once at job start, has no way to see a
DIFFERENT run's post that happened (and was pushed) in the meantime.
Confirmed against real commit timing on data/posted_news.json: gaps
between "update bot state" commits regularly exceed the 20-minute
schedule interval, meaning overlapping runs are the normal case here,
not an edge case. This is the confirmed mechanism behind the same
story (e.g. a transfer card) posting multiple times minutes-to-hours
apart with each post showing distinct engagement, i.e. genuinely
separate posts rather than a UI re-render of one post.

The fix, `resync_dedup_state_from_origin()` in main.py, is called
immediately before the duplicate check inside `post_item()` -- not
once at job start -- so the dedup check always runs against state as
fresh as a read-only `git fetch` can make it, regardless of how long the run has
been alive or how many other runs have completed since this job's own
checkout.

These tests build `data` state entirely in memory and mock
`subprocess.run` (the fetch and git-show) -- they must NEVER touch the
real data/posted_news.json or make a real git/network call, matching
the discipline already established in test_dedup.py.

Run with pytest OR standalone:  python tests/test_cross_run_dedup_resync.py
"""

import json
import os
import sys
import types
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
import main  # noqa: E402

main.init_club_data()


def _fresh_data():
    return {"daily": {"date": "", "count": 0, "limit": 30}, "stories": {},
            "posted_ids": [], "pending": {}, "extracted": {},
            "posted_hashes": [], "posted_headlines": [],
            "posted_v2_fingerprints": [], "posted_v2_fact_signatures": []}


def _result(returncode=0, *, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _remote_results(origin_data):
    return [_result(), _result(stdout=json.dumps(origin_data))]


def test_resync_pulls_and_merges_a_story_another_run_already_posted():
    """The exact overlap scenario: this run's in-memory `data` has never
    heard of a story; a DIFFERENT run posted it and pushed updated state
    to origin/main in the meantime. After resync, this run's duplicate
    check must catch it -- closing the gap that let the same story post
    more than once."""
    story = main.build_story(
        "Crystal Palace complete signing of Thomas Meunier from Trabzonspor.",
        None,
    )
    story_key = main.build_story_key(
        story["player"], story.get("to_key") or "unknown", story["event"])

    # This run's own state at job start: no knowledge of the story at all.
    local_data = _fresh_data()

    # What a DIFFERENT, already-finished run pushed to origin/main:
    # the same story, already recorded as posted.
    origin_data = _fresh_data()
    main.record_content_dedup(story, origin_data)
    origin_data["stories"][story_key] = {
        "stage": story.get("stage", 1), "player": story["player"],
        "event": story["event"], "status": "active",
        "last_updated": "2026-08-11T12:00:00+00:00",
    }

    with patch.object(
        main.subprocess, "run", side_effect=_remote_results(origin_data)
    ) as mock_run, patch.object(main, "load_data") as mock_load:
        resynced = main.resync_dedup_state_from_origin(local_data)
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0].args[0] == [
            "git", "fetch", "--quiet", "origin", "main"
        ]
        assert mock_run.call_args_list[1].args[0] == [
            "git", "show", "FETCH_HEAD:data/posted_news.json"
        ]
        mock_load.assert_not_called()

    dup, dreason = main.is_duplicate_content(story, resynced)
    assert dup is True, (
        f"resync did not surface a story another run already posted "
        f"(dup={dup}, reason={dreason!r}) -- the cross-run overlap gap "
        f"this fix closes would still be open")


def test_resync_preserves_this_runs_own_in_memory_progress():
    """A pull must never erase what THIS run has already staged locally
    but not yet pushed -- resync unions dedup fields, it doesn't replace
    them wholesale."""
    story_a = main.build_story(
        "Arsenal sign Pascal Struijk from Leeds United.", None)
    story_b = main.build_story(
        "Brighton sign Michael Svoboda from Rapid Vienna.", None)

    local_data = _fresh_data()
    main.record_content_dedup(story_a, local_data)  # staged, not yet pushed

    origin_data = _fresh_data()
    main.record_content_dedup(story_b, origin_data)  # a different run's push

    with patch.object(
        main.subprocess, "run", side_effect=_remote_results(origin_data)
    ):
        resynced = main.resync_dedup_state_from_origin(local_data)

    dup_a, _ = main.is_duplicate_content(story_a, resynced)
    dup_b, _ = main.is_duplicate_content(story_b, resynced)
    assert dup_a is True, "this run's own already-staged dedup entry was lost on resync"
    assert dup_b is True, "the other run's pushed dedup entry was not merged in"


def test_resync_merges_source_independent_v2_duplicate_memory():
    local_data = _fresh_data()
    local_data["posted_v2_fingerprints"] = ["local-fingerprint"]
    local_data["posted_v2_fact_signatures"] = ["local-facts"]
    origin_data = _fresh_data()
    origin_data["posted_v2_fingerprints"] = ["remote-fingerprint"]
    origin_data["posted_v2_fact_signatures"] = ["remote-facts"]

    with patch.object(
        main.subprocess, "run", side_effect=_remote_results(origin_data)
    ):
        resynced = main.resync_dedup_state_from_origin(local_data)

    assert set(resynced["posted_v2_fingerprints"]) == {
        "local-fingerprint", "remote-fingerprint"
    }
    assert set(resynced["posted_v2_fact_signatures"]) == {
        "local-facts", "remote-facts"
    }


def test_resync_is_best_effort_and_never_blocks_posting_on_git_failure():
    """No network / git unavailable / fetch rejected -- posting must
    proceed on whatever was already loaded, not raise or block. This
    must degrade gracefully, e.g. for local/manual runs outside the
    workflow with no upstream configured."""
    local_data = _fresh_data()
    story = main.build_story(
        "Newcastle sign Johan Manzambi from Freiburg.", None)
    main.record_content_dedup(story, local_data)

    failed_result = _result(
        returncode=1, stderr="fatal: could not read from remote repository"
    )

    with patch.object(main.subprocess, "run", return_value=failed_result):
        resynced = main.resync_dedup_state_from_origin(local_data)

    # Falls back to the data it was given -- unchanged, not wiped.
    dup, _ = main.is_duplicate_content(story, resynced)
    assert dup is True, "a failed fetch must fall back to existing in-memory state, not discard it"

    with patch.object(main.subprocess, "run", side_effect=OSError("git not found")):
        resynced2 = main.resync_dedup_state_from_origin(local_data)
    dup2, _ = main.is_duplicate_content(story, resynced2)
    assert dup2 is True, "a missing git binary must not raise or block posting"


def test_resync_does_not_mutate_worktree_and_rejects_bad_remote_json():
    local_data = _fresh_data()
    local_data["posted_ids"] = ["keep-me"]

    with patch.object(
        main.subprocess,
        "run",
        side_effect=[_result(), _result(stdout="<<<<<<< malformed json")],
    ) as mock_run:
        resynced = main.resync_dedup_state_from_origin(local_data)

    assert resynced["posted_ids"] == ["keep-me"]
    commands = [call.args[0] for call in mock_run.call_args_list]
    assert all("pull" not in command for command in commands)
    assert all("rebase" not in command for command in commands)
    assert all("autostash" not in command for command in commands)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed.")
    sys.exit(1 if failed else 0)
