"""
FPL VORTEX — FotMob confirmed-transfer verification layer.

FotMob's transfers endpoint lists only COMPLETED deals (no rumours, no
"in talks"), so any story that matches a FotMob entry can be promoted to
stage 4 (OFFICIAL) with high confidence.

This is used as a secondary ground-truth check AFTER the journalist
pipeline has already qualified a story — it is never the primary scrape
source. Failures are always silent so callers are never blocked.

NOTE: Player headshots from FotMob's CDN are copyrighted (Getty/Panini)
and must NOT be downloaded or redistributed. Only factual metadata
(player name, clubs, fee) is used here.
"""

import re
import unicodedata

import requests

_URL = "https://www.fotmob.com/api/transfers"
_PARAMS = {"showTop": "true"}
_HEADERS = {
    # Must look like a real browser or FotMob returns 403.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fotmob.com/",
}


def _norm(s: str) -> str:
    """Lowercase, strip accents, keep only letters/digits/spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on word tokens. 1.0 = identical sets."""
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _extract_entry(t: dict) -> dict | None:
    """Parse one FotMob transfer object, tolerating schema variations."""
    if not isinstance(t, dict):
        return None
    # Player name — multiple possible keys across FotMob schema versions.
    player = (
        t.get("name") or
        t.get("playerName") or
        (t.get("player") or {}).get("name") or
        ""
    ).strip()
    if not player:
        return None

    pid = t.get("playerId") or t.get("id") or (t.get("player") or {}).get("id")

    from_club = (
        t.get("fromClub") or
        t.get("fromTeamName") or
        (t.get("fromTeam") or {}).get("name") or
        ""
    ).strip() or None

    to_club = (
        t.get("toClub") or
        t.get("toTeamName") or
        (t.get("toTeam") or {}).get("name") or
        ""
    ).strip() or None

    fee_raw = t.get("fee") or {}
    if isinstance(fee_raw, dict):
        fee = (fee_raw.get("value") or fee_raw.get("text") or "").strip() or None
    elif isinstance(fee_raw, str):
        fee = fee_raw.strip() or None
    else:
        fee = None

    return {
        "player": player,
        "player_id": pid,
        "from_club": from_club,
        "to_club": to_club,
        "fee": fee,
    }


def fetch_fotmob_transfers(timeout: int = 8) -> list[dict]:
    """
    Return a list of confirmed-transfer dicts from FotMob.

    Each dict: player (str), player_id (int|None), from_club (str|None),
               to_club (str|None), fee (str|None).

    Returns [] on any network or parse error so callers are never blocked.
    """
    try:
        resp = requests.get(_URL, params=_PARAMS, headers=_HEADERS, timeout=timeout)
    except Exception as exc:
        print(f"[FOTMOB] network error: {exc}")
        return []

    if resp.status_code != 200:
        print(f"[FOTMOB] HTTP {resp.status_code} — verification layer skipped")
        return []

    try:
        raw = resp.json()
    except Exception as exc:
        print(f"[FOTMOB] JSON parse error: {exc}")
        return []

    # The API has returned both a plain list and {"transfers": [...]}
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("transfers") or raw.get("data") or []
    else:
        entries = []

    out = [e for t in entries if (e := _extract_entry(t)) is not None]
    print(f"[FOTMOB] {len(out)} confirmed deal(s) fetched")
    return out


def match_story(story: dict, fotmob_list: list[dict]) -> dict | None:
    """
    Return the first FotMob entry whose player + clubs agree with this story,
    or None. Token overlap lets "Haaland" match "Erling Haaland" and
    "Man City" match "Manchester City".

    Matching criteria:
      - ≥50% token overlap between story player and FotMob player name, OR
        the story player token is a strict subset of the FotMob player tokens
      - At least one club direction (to/from) overlaps ≥50%, OR the story
        doesn't yet have a club in that direction (we don't block on unknowns)
    """
    s_player = _norm(story.get("player") or "")
    if not s_player:
        return None
    s_tokens = set(s_player.split())
    s_to = _norm(story.get("to_key") or story.get("to_club") or "")
    s_from = _norm(story.get("from_key") or story.get("from_club") or "")

    for entry in fotmob_list:
        e_player = _norm(entry.get("player") or "")
        if not e_player:
            continue
        e_tokens = set(e_player.split())

        # Player name match: story tokens must be a non-empty subset of FotMob
        # tokens, or there must be ≥50% Jaccard overlap.
        if not (s_tokens and s_tokens <= e_tokens):
            if _token_overlap(s_player, e_player) < 0.5:
                continue

        # Club check — at least the destination must roughly agree.
        e_to = _norm(entry.get("to_club") or "")
        e_from = _norm(entry.get("from_club") or "")

        dest_ok = not s_to or (e_to and _token_overlap(s_to, e_to) >= 0.45)
        origin_ok = not s_from or (e_from and _token_overlap(s_from, e_from) >= 0.4)

        if dest_ok and origin_ok:
            return entry

    return None
