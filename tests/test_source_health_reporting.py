"""An empty run must never look the same as a broken one.

A total X outage once hid behind the message "Quiet run — no new stories cleared
all gates (sources read OK)" for 20 hours. Every journalist search was returning
404, so nothing could be corroborated and nothing could publish — but the health
figure the summary consulted covered RSS and Bluesky feeds, which were fine. The
run reported a quiet news day while the bot was blind.
"""

import asyncio

import pytest

from src import verifier


@pytest.fixture(autouse=True)
def _reset():
    verifier.reset_x_search_health()
    yield
    verifier.reset_x_search_health()


class _Profile:
    def __init__(self, handle):
        self.search_enabled = True
        self.handles = [handle]
        self.id = f"journalist.{handle}"
        self.declared_sports = ["football"]


class _Registry:
    def __init__(self, handles):
        self._profiles = [_Profile(h) for h in handles]

    def all(self):
        return list(self._profiles)


class _FailingClient:
    """Reproduces the observed failure: every search raises, none succeed."""

    def __init__(self, exc):
        self._exc = exc

    async def search_tweet(self, *_args, **_kwargs):
        raise self._exc


class _WorkingClient:
    async def search_tweet(self, *_args, **_kwargs):
        return []


HANDLES = ["fabrizioromano", "david_ornstein", "bendinnery"]
STORY = {"player": "Carlos Baleba", "event": "injury"}


def _run(client):
    """Drive the coroutine synchronously — the repo has no pytest-asyncio."""
    return asyncio.run(
        verifier._x_journalist_evidence(client, STORY, _Registry(HANDLES), log=[])
    )


def test_no_searches_attempted_is_not_reported_as_healthy():
    """None attempted means no data. It must not read as a clean bill of health."""
    health = verifier.x_search_health()
    assert health["attempted"] == 0
    assert health["fail_ratio"] is None


def test_total_outage_is_counted():
    _run(_FailingClient(Exception('status: 404, message: ""')))
    health = verifier.x_search_health()
    assert health["attempted"] == len(HANDLES)
    assert health["failed"] == len(HANDLES)
    assert health["fail_ratio"] == 1.0
    # The error text must survive to the summary — "404" is what distinguishes a
    # stale client from expired cookies, and it drives the suggested fix.
    assert any("404" in e for e in health["errors"])


def test_healthy_searches_report_zero_failures():
    _run(_WorkingClient())
    health = verifier.x_search_health()
    assert health["attempted"] == len(HANDLES)
    assert health["failed"] == 0
    assert health["fail_ratio"] == 0.0


def test_distinct_errors_are_deduplicated():
    _run(_FailingClient(Exception("status: 404")))
    assert len(verifier.x_search_health()["errors"]) == 1


def test_reset_clears_between_runs():
    _run(_FailingClient(Exception("status: 404")))
    assert verifier.x_search_health()["failed"] == len(HANDLES)
    verifier.reset_x_search_health()
    health = verifier.x_search_health()
    assert health == {"attempted": 0, "failed": 0, "fail_ratio": None, "errors": []}
