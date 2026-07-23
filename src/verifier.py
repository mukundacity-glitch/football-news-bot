"""
FPL VORTEX — Automatic Cross-Verification Engine.

Replaces the manual verification step entirely: every candidate story is
automatically checked against independent, reliable outlets BEFORE the
publish decision is made:

  • Official club websites (the club's own domain — tier-1 confirmation)
  • Google News (aggregated verification across the trusted press)
  • FotMob, Sky Sports, BBC Sport, The Athletic, Guardian, Telegraph
  • Elite journalists on X (Fabrizio Romano, David Ornstein) — best-effort,
    only when read cookies are configured

Every independent corroborating outlet found is merged into the story's
source list as its canonical handle, so the EXISTING tier / classification /
confidence machinery upgrades the story naturally:

  - a second trusted outlet    -> multiple_sources signal (+10)
  - the club's own website     -> official_source signal (+15) + CONFIRMED
  - an elite journalist on X   -> elite_source signal (+5) + CONFIRMED path

A story no reliable outlet corroborates never reaches the AUTO_POST
threshold; it is simply re-checked automatically on the next scheduled run
(sources accumulate as the story develops). There is no human review step
anywhere in the loop.

All network calls are best-effort: any failure degrades to "no extra
corroboration found" and can never crash the pipeline.
"""

import calendar
import re
import time
import urllib.parse

import feedparser
import requests

from src.constants import (
    JOURNALISTS, CLUB_OFFICIAL_DOMAINS, TRUSTED_MEDIA_DOMAINS,
)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/1.0; +https://x.com/FPLVortex)"}
HTTP_TIMEOUT_S = 12
MAX_AGE_DAYS = 3          # corroboration must be as fresh as the news itself
MAX_ENTRIES_PER_QUERY = 25

# Words too generic to identify a club inside a headline.
_GENERIC_CLUB_WORDS = {"united", "city", "town", "club", "albion", "hotspur",
                       "forest", "the", "and", "real", "athletic"}

_EVENT_KW = {
    "injury": ("injur", "ruled out", "out for", "scan", "surgery", "hamstring",
               "knee", "ankle", "fitness", "doubt", "sidelined", "blow", "return"),
    "suspension": ("suspend", "ban", "red card", "sent off", "miss"),
    "manager": ("manager", "head coach", "appoint", "sack", "boss", "coach",
                "in charge", "job"),
    "renewal": ("contract", "new deal", "extend", "renew", "stay"),
    "stay": ("contract", "new deal", "extend", "renew", "stay", "remain"),
}
_TRANSFER_KW = ("transfer", "sign", "deal", "join", "move", "bid", "fee",
                "medical", "agree", "loan", "talks", "swoop", "switch", "exit")


def _norm_handle(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _surname(name: str) -> str:
    toks = [t for t in re.split(r"[\s\-']+", (name or "").strip()) if t]
    if not toks:
        return ""
    last = toks[-1].lower()
    # Very short surnames ("Sá") match too loosely — use the full name instead.
    return last if len(last) >= 3 else " ".join(toks).lower()


def _club_tokens(story: dict) -> set:
    toks = set()
    for f in ("to_club", "from_club", "to_key", "from_key"):
        for w in re.split(r"[\s_\-]+", str(story.get(f) or "").lower()):
            if len(w) >= 4 and w not in _GENERIC_CLUB_WORDS:
                toks.add(w)
    return toks


def matches_story(text: str, story: dict) -> bool:
    """A headline corroborates a story only if it names the player AND carries
    a club token or an event cue for the same kind of news."""
    tl = " " + (text or "").lower() + " "
    sur = _surname(story.get("player"))
    if not sur or sur not in tl:
        return False
    if any(t in tl for t in _club_tokens(story)):
        return True
    kws = _EVENT_KW.get(story.get("event"), _TRANSFER_KW)
    return any(k in tl for k in kws)


def _too_old(entry) -> bool:
    tp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tp:
        return False  # queries are already scoped with when:Nd
    return (time.time() - calendar.timegm(tp)) > MAX_AGE_DAYS * 86400


def _entry_domain(entry) -> str:
    """Publisher domain of a Google News entry. entry.link is a Google
    redirect, so prefer the <source href=...> publisher URL."""
    src = entry.get("source") or {}
    href = src.get("href") if isinstance(src, dict) else getattr(src, "href", "")
    link = href or entry.get("link", "")
    host = urllib.parse.urlparse(link).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _handle_for_domain(domain: str):
    """(canonical_handle, kind) for a publisher domain, or (None, None)."""
    if not domain:
        return None, None
    for d, handle in TRUSTED_MEDIA_DOMAINS.items():
        if domain == d or domain.endswith("." + d):
            return handle, "media"
    for _club, (d, handle) in CLUB_OFFICIAL_DOMAINS.items():
        if domain == d or domain.endswith("." + d):
            return handle, "official"
    return None, None


def _google_news(query: str, log: list) -> list:
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote(query))
    try:
        resp = requests.get(url, headers=_UA, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:
        log.append(f"google news unreachable for {query!r}: {e}")
        return []
    feed = feedparser.parse(resp.content)
    return list(feed.entries or [])[:MAX_ENTRIES_PER_QUERY]


def _official_domains_for(story: dict) -> list:
    out = []
    for f in ("to_key", "from_key"):
        k = story.get(f)
        if k and k in CLUB_OFFICIAL_DOMAINS:
            out.append(CLUB_OFFICIAL_DOMAINS[k])
    return out


async def _x_journalists(read_client, story: dict, log: list) -> list:
    """Best-effort check of elite journalists' recent posts on X. Skipped
    silently when no read client is available."""
    found = []
    if read_client is None:
        return found
    sur = _surname(story.get("player"))
    if not sur:
        return found
    for j in JOURNALISTS[:4]:
        try:
            res = await read_client.search_tweet(f"from:{j} {sur}", "Latest")
        except Exception as e:
            log.append(f"X @{j}: search failed ({e})")
            continue
        for tw in list(res or [])[:10]:
            text = getattr(tw, "text", "") or getattr(tw, "full_text", "") or ""
            if matches_story(text, story):
                found.append(j.lower())
                log.append(f"X @{j}: corroborates ✓ {text[:70]!r}")
                break
    return found


async def cross_verify(story: dict, known_sources=(), read_client=None) -> dict:
    """Verify a story against independent reliable outlets.

    Returns {"handles": [new corroborating handles], "official_confirmed":
    bool, "n_independent": int, "log": [human-readable check lines]}.
    Handles already present in known_sources are not double-counted.
    """
    log, handles = [], []
    known = {_norm_handle(s) for s in (known_sources or [])}

    def _add(h):
        n = _norm_handle(h)
        if n and n not in known:
            known.add(n)
            handles.append(h)

    player = (story.get("player") or "").strip()
    if not player:
        return {"handles": [], "official_confirmed": False,
                "n_independent": 0, "log": ["no player name to verify"]}

    official_confirmed = False

    # 1) Google News aggregate check — one query covers BBC, Sky Sports,
    #    The Athletic, FotMob, Guardian, Telegraph AND official club sites.
    for entry in _google_news(f'"{player}" football when:{MAX_AGE_DAYS}d', log):
        if _too_old(entry):
            continue
        title = entry.get("title", "")
        if not matches_story(title, story):
            continue
        handle, kind = _handle_for_domain(_entry_domain(entry))
        if not handle:
            continue
        _add(handle)
        if kind == "official":
            official_confirmed = True
        log.append(f"{_entry_domain(entry)}: {title[:70]!r} ✓")

    # 2) Direct official-club-website check (site:-scoped query) when the
    #    club's own site didn't already appear in the aggregate results.
    if not official_confirmed:
        for domain, handle in _official_domains_for(story):
            time.sleep(0.4)
            for entry in _google_news(f"site:{domain} {_surname(player)} when:7d", log):
                if matches_story(entry.get("title", ""), story):
                    _add(handle)
                    official_confirmed = True
                    log.append(f"official club site {domain}: ✓")
                    break
            if official_confirmed:
                break

    # 3) Elite journalists on X.
    for h in await _x_journalists(read_client, story, log):
        _add(h)

    return {
        "handles": handles,
        "official_confirmed": official_confirmed,
        "n_independent": len(handles),
        "log": log,
    }
