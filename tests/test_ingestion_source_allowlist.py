from types import SimpleNamespace

import pytest

from src.verification.ingestion import _all_feed_definitions


def _runtime(*feeds):
    return SimpleNamespace(feeds=SimpleNamespace(feeds=list(feeds)))


def _feed(feed_id, source_hint="media.fotmob", transport="DIRECT_RSS"):
    return SimpleNamespace(
        id=feed_id,
        source_hint=source_hint,
        transport=transport,
    )


def test_only_fotmob_direct_feed_is_allowed():
    runtime = _runtime(
        _feed("bbc.premier_league", "media.bbc_sport"),
        _feed("google.fotmob.premier_league.transfer_topics", None, "GOOGLE_NEWS"),
        _feed("fotmob.premier_league.topnews"),
    )

    selected = _all_feed_definitions(runtime)

    assert [feed.id for feed in selected] == ["fotmob.premier_league.topnews"]


def test_missing_fotmob_feed_fails_closed():
    runtime = _runtime(_feed("bbc.premier_league", "media.bbc_sport"))

    with pytest.raises(RuntimeError, match="FotMob-only ingestion"):
        _all_feed_definitions(runtime)
