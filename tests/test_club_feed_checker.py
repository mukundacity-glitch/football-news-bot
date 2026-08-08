"""The club-feed checker must be honest about what it found.

Its whole reason to exist is that guessing feed URLs is what produces 25 broken
feeds. So the bar is: never report a feed as usable unless it really parsed, and
never crash on a club site that is down.
"""

import json
from pathlib import Path

import pytest

from tools import check_club_feeds as checker


def test_probe_list_comes_from_the_trust_config():
    """The clubs probed are derived from the OFFICIAL_CLUB profiles, not from a
    second list that would drift out of step with them."""
    clubs = checker.official_clubs()
    assert len(clubs) >= 20, f"expected the PL clubs, got {len(clubs)}"
    assert all(c["kind"] == "OFFICIAL_CLUB" for c in clubs)
    assert all(c.get("domains") for c in clubs), "every club profile needs a domain to probe"


def test_html_page_is_not_mistaken_for_a_feed():
    """A club site that returns its news *page* on /rss must not be wired in."""
    html = b"<!doctype html><html><head><title>Arsenal News</title></head><body>"
    assert checker._looks_like_feed(html, "text/html") is False


@pytest.mark.parametrize("body,ctype", [
    (b'<?xml version="1.0"?><rss version="2.0"><channel>', "application/rss+xml"),
    (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">', "application/atom+xml"),
    (b'<rdf:RDF xmlns="http://purl.org/rss/1.0/">', "application/xml"),
])
def test_real_feed_shapes_are_recognised(body, ctype):
    assert checker._looks_like_feed(body, ctype) is True


def test_feed_without_dated_entries_is_rejected():
    """The bot rejects undated items anyway, and an undated feed cannot be
    freshness-checked — so it is no use even though it parses."""
    undated = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <title>Club</title><item><title>Signing announced</title></item>
    </channel></rss>"""
    count, newest = checker._parse_entries(undated)
    assert count == 1
    assert newest is None, "no usable date means the feed cannot be accepted"


def test_dated_feed_reports_its_newest_entry():
    dated = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Club</title>
      <item><title>Older</title><pubDate>Mon, 03 Aug 2026 09:00:00 GMT</pubDate></item>
      <item><title>Newer</title><pubDate>Fri, 07 Aug 2026 18:30:00 GMT</pubDate></item>
    </channel></rss>"""
    count, newest = checker._parse_entries(dated)
    assert count == 2
    assert newest.startswith("2026-08-07")


def test_unreachable_club_is_recorded_not_raised(monkeypatch):
    """A club site being down is information, not a crash."""
    def boom(url):
        raise TimeoutError("connection timed out")
    monkeypatch.setattr(checker, "_fetch", boom)

    result = checker.probe_club({"id": "club.test", "domains": ["example.com"]})
    assert result["ok"] is False
    assert result["feed_url"] is None
    assert result["attempts"], "every failed path should be recorded for diagnosis"
    assert all("reason" in a for a in result["attempts"])


def test_a_working_feed_short_circuits_and_builds_a_config_entry(monkeypatch):
    good = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Club</title>
      <item><title>Signed</title><pubDate>Fri, 07 Aug 2026 18:30:00 GMT</pubDate></item>
    </channel></rss>"""
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url.endswith("/rss"):
            return 200, good, "application/rss+xml"
        raise AssertionError("must stop at the first working path")

    monkeypatch.setattr(checker, "_fetch", fake_fetch)
    profile = {"id": "club.arsenal", "domains": ["arsenal.com"], "kind": "OFFICIAL_CLUB"}
    result = checker.probe_club(profile)

    assert result["ok"] is True
    assert result["feed_url"].endswith("/rss")
    assert len(calls) == 1, "a working first path must not trigger further requests"

    entry = checker.feed_entry(result, profile)
    # source_hint must be the club's existing profile id — that is what makes the
    # feed inherit OFFICIAL_CLUB trust and publish without a second source.
    assert entry["source_hint"] == "club.arsenal"
    assert entry["transport"] == "DIRECT_RSS"
    assert entry["declared_sport"] == "football"


def test_generated_entry_matches_the_existing_feeds_schema():
    """A generated entry must be pasteable into config/feeds.json as-is."""
    existing = json.loads(Path("config/feeds.json").read_text())["feeds"]
    direct = next(f for f in existing if f["transport"] == "DIRECT_RSS")
    generated = checker.feed_entry(
        {"club": "club.arsenal", "feed_url": "https://www.arsenal.com/rss"},
        {"id": "club.arsenal"},
    )
    assert set(generated) == set(direct), (
        f"schema drift: generated={sorted(generated)} existing={sorted(direct)}"
    )


def test_checker_never_fails_the_build(monkeypatch, tmp_path):
    monkeypatch.setattr(checker, "_fetch", lambda url: (_ for _ in ()).throw(TimeoutError()))
    assert checker.main(["--club", "arsenal", "--json", str(tmp_path / "r.json")]) == 0
