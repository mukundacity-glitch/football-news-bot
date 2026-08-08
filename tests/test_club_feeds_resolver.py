"""Automatic club-feed resolution.

The resolver exists so nobody hand-maintains 25 club RSS URLs that go stale the
moment a club redesigns its site. That only works if it is honest about what it
found, cheap enough to run every 20 minutes, and incapable of breaking a run.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.verification import club_feeds


class _Profile:
    def __init__(self, pid, domains, kind="OFFICIAL_CLUB"):
        self.id = pid
        self.domains = domains
        self.kind = kind


class _Sources:
    def __init__(self, profiles):
        self._profiles = profiles

    def all(self):
        return list(self._profiles)


GOOD_FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Club</title>
  <item><title>Signing confirmed</title><pubDate>Fri, 07 Aug 2026 18:30:00 GMT</pubDate></item>
</channel></rss>"""

UNDATED_FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Club</title>
  <item><title>Signing confirmed</title></item>
</channel></rss>"""

HTML_PAGE = b"<!doctype html><html><head><title>Club News</title></head><body>News</body></html>"


def _sources(n=3):
    return _Sources([_Profile(f"club.team{i}", [f"team{i}.com"]) for i in range(n)])


# ── what counts as a usable feed ────────────────────────────────────────

def test_html_news_page_is_not_accepted_as_a_feed(monkeypatch, tmp_path):
    """A club serving its news *page* on /rss must not be wired in — that is how
    a feed list fills up with 200-OK garbage."""
    monkeypatch.setattr(club_feeds, "_fetch", lambda url: (200, HTML_PAGE, "text/html"))
    result = club_feeds.probe_club(_Profile("club.x", ["x.com"]))
    assert result["ok"] is False


def test_feed_without_dated_entries_is_rejected(monkeypatch):
    """Undated items cannot be freshness-checked and the pipeline drops them, so
    an undated feed is worse than no feed — it looks like coverage."""
    monkeypatch.setattr(club_feeds, "_fetch",
                        lambda url: (200, UNDATED_FEED, "application/rss+xml"))
    assert club_feeds.probe_club(_Profile("club.x", ["x.com"]))["ok"] is False


def test_dated_feed_is_accepted_and_stops_at_the_first_hit(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/rss"):
            return 200, GOOD_FEED, "application/rss+xml"
        raise AssertionError("should have stopped at the first working path")

    monkeypatch.setattr(club_feeds, "_fetch", fetch)
    result = club_feeds.probe_club(_Profile("club.x", ["x.com"]))
    assert result["ok"] is True
    assert result["url"].endswith("/rss")
    assert result["newest"].startswith("2026-08-07")
    assert len(calls) == 1, "a working first path must not trigger more requests"


def test_unreachable_club_never_raises(monkeypatch):
    monkeypatch.setattr(club_feeds, "_fetch",
                        lambda url: (_ for _ in ()).throw(TimeoutError("down")))
    result = club_feeds.probe_club(_Profile("club.x", ["x.com"]))
    assert result["ok"] is False and result["url"] is None


# ── cost control ────────────────────────────────────────────────────────

def test_probe_budget_bounds_the_network_cost_per_run(monkeypatch, tmp_path):
    """25 clubs x 9 paths every 20 minutes would be hundreds of requests. Each
    run may only probe a few."""
    probed = []

    def fake_probe(profile):
        probed.append(profile.id)
        return {"club_id": profile.id, "ok": False, "url": None,
                "checked_at": club_feeds._now().isoformat()}

    monkeypatch.setattr(club_feeds, "probe_club", fake_probe)
    club_feeds.resolve_club_feeds(_sources(10), cache_path=tmp_path / "c.json",
                                  probe_budget=3)
    assert len(probed) == 3


def test_known_good_feeds_need_no_network_at_all(monkeypatch, tmp_path):
    """The steady state must be free: nothing stale, nothing probed."""
    cache = tmp_path / "c.json"
    now = datetime.now(timezone.utc)
    cache.write_text(json.dumps({"clubs": {
        f"club.team{i}": {"club_id": f"club.team{i}", "ok": True,
                          "url": f"https://www.team{i}.com/rss", "entries": 5,
                          "newest": now.isoformat(), "checked_at": now.isoformat()}
        for i in range(3)
    }}))

    def explode(profile):
        raise AssertionError("fresh cache must not trigger a probe")

    monkeypatch.setattr(club_feeds, "probe_club", explode)
    resolved = club_feeds.resolve_club_feeds(_sources(3), cache_path=cache)
    assert len(resolved) == 3


def test_never_tried_clubs_are_probed_before_merely_stale_ones(monkeypatch, tmp_path):
    """Coverage should grow fast rather than re-checking the same club."""
    cache = tmp_path / "c.json"
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cache.write_text(json.dumps({"clubs": {
        "club.team0": {"club_id": "club.team0", "ok": True, "url": "u",
                       "entries": 1, "newest": old, "checked_at": old},
    }}))
    probed = []
    monkeypatch.setattr(club_feeds, "probe_club", lambda p: (
        probed.append(p.id) or {"club_id": p.id, "ok": False, "url": None,
                                "checked_at": club_feeds._now().isoformat()}))
    club_feeds.resolve_club_feeds(_sources(3), cache_path=cache, probe_budget=1)
    assert probed == ["club.team1"], "an untried club outranks a stale one"


# ── staleness policy ────────────────────────────────────────────────────

def test_a_working_feed_is_rechecked_after_a_week():
    now = datetime.now(timezone.utc)
    ok_fresh = {"ok": True, "checked_at": (now - timedelta(days=3)).isoformat()}
    ok_stale = {"ok": True, "checked_at": (now - timedelta(days=8)).isoformat()}
    assert club_feeds._needs_probe(ok_fresh, now) is False
    assert club_feeds._needs_probe(ok_stale, now) is True


def test_a_failed_club_is_retried_sooner_than_a_working_one():
    """A club site being briefly down is common and cheap to recheck."""
    now = datetime.now(timezone.utc)
    failed = {"ok": False, "checked_at": (now - timedelta(days=2)).isoformat()}
    working = {"ok": True, "checked_at": (now - timedelta(days=2)).isoformat()}
    assert club_feeds._needs_probe(failed, now) is True
    assert club_feeds._needs_probe(working, now) is False


def test_missing_or_corrupt_cache_is_survivable(tmp_path):
    assert club_feeds.load_cache(tmp_path / "nope.json") == {"schema_version": 1, "clubs": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert club_feeds.load_cache(bad) == {"schema_version": 1, "clubs": {}}


# ── the point of the whole exercise: first-party trust ──────────────────

def test_resolved_feeds_carry_the_clubs_own_source_hint():
    """source_hint = the club's OFFICIAL_CLUB profile id is what makes a club
    announcement self-confirming under official_first_party_sufficient. Without
    it the feed resolves as generic media and waits forever for a second
    source — which is exactly what the Google News discovery path does."""
    resolved = [club_feeds.ResolvedClubFeed(
        club_id="club.arsenal", url="https://www.arsenal.com/rss",
        entries=12, newest="2026-08-07T18:30:00+00:00", checked_at="2026-08-08T00:00:00+00:00")]
    definitions = club_feeds.as_feed_definitions(resolved)
    assert len(definitions) == 1
    d = definitions[0]
    assert d.source_hint == "club.arsenal"
    assert d.transport == "DIRECT_RSS"
    assert d.declared_sport == "football"
    assert d.id == "official.arsenal"


def test_real_club_profiles_are_all_probeable():
    """Every OFFICIAL_CLUB profile in the shipped config must have a domain, or
    it can never be resolved."""
    from src.verification.source_registry import SourceRegistry

    sources = SourceRegistry.load("config/sources.json")
    clubs = club_feeds.official_club_profiles(sources)
    assert len(clubs) >= 20, f"expected the PL clubs, found {len(clubs)}"
    assert all(c.domains for c in clubs)
