"""
FPL VORTEX — Squad Registry (the ALLOWLIST).

WHY THIS MODULE EXISTS
----------------------
The bot used to decide "is this string a player?" with a BLACKLIST: a name was
treated as a real footballer unless it matched a list of journalists, sponsors,
stadiums, clubs, competitions or junk tokens. That is an open-world test, and an
open-world test can never be finished — every new false post ("Manchester United
Website has joined Man Utd", "Ken Bates", "Ben Duckett") could only be stopped by
adding one more name to one more blacklist AFTER it had already been published.

This module inverts that. It is a CLOSED-WORLD allowlist: a name is a player if,
and only if, it resolves against a real squad registry. Everything else is
UNKNOWN and unpublishable — including a capitalised phrase from an official club
account, which is exactly how the worst false posts were created.

SOURCES OF TRUTH (in priority order)
------------------------------------
1. ``data/fpl_cache.json`` — the live FPL bootstrap-static feed, refreshed each
   run by ``src.fpl_feed.fetch_fpl_data``. This is the authoritative roster of
   every registered Premier League player.
2. ``data/squad_overrides.json`` — a small, human-curated file for players who
   are confirmed signed/registered but have not yet appeared in the FPL feed
   (there is usually a lag of a day or two after a real transfer).

Override entries EXPIRE. An entry past ``expires_at`` is ignored, so the file
cannot silently rot into a stale second source of truth: it either gets renewed
deliberately, or it disappears and the FPL feed takes over. This is the only
sanctioned way to publish about a player the official feed does not yet know,
and it requires a human to record first-party evidence for the addition.

FAIL-CLOSED
-----------
If neither source can be loaded, the registry is EMPTY and every name resolves
to None. That blocks publishing. A registry that cannot prove a player is real
must never fall back to "assume it is real" — that fallback is the bug this
module was written to delete.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

_DATA = Path(__file__).resolve().parent.parent / "data"
_FPL_CACHE = _DATA / "fpl_cache.json"
_OVERRIDES = _DATA / "squad_overrides.json"


def normalize(value: str) -> str:
    """Lowercase, de-accent, drop punctuation, collapse whitespace."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(value: str) -> list[str]:
    return [t for t in normalize(value).split() if t]


@dataclass(frozen=True)
class PlayerRecord:
    """One verified player. ``origin`` records which source vouched for them."""

    name: str
    club_key: Optional[str] = None
    position: Optional[str] = None
    fpl_code: Optional[int] = None
    fpl_element: Optional[dict] = field(default=None, repr=False, compare=False)
    origin: str = "fpl"
    evidence_url: Optional[str] = None

    @property
    def surname(self) -> str:
        toks = _tokens(self.name)
        return toks[-1] if toks else ""


class SquadRegistry:
    """Closed-world lookup over the union of the FPL feed and live overrides."""

    def __init__(self, records: Iterable[PlayerRecord] = ()):
        self._records: list[PlayerRecord] = []
        self._exact: dict[str, PlayerRecord] = {}
        for record in records:
            self.add(record)

    # ── construction ────────────────────────────────────────────────────
    def add(self, record: PlayerRecord, aliases: Iterable[str] = ()) -> None:
        self._records.append(record)
        for key in (record.name, *aliases):
            norm = normalize(key)
            # First writer wins: the FPL feed is loaded before overrides, so an
            # override can never shadow a player the official feed already knows.
            if norm and norm not in self._exact:
                self._exact[norm] = record

    def __len__(self) -> int:
        return len(self._records)

    def __bool__(self) -> bool:
        return bool(self._records)

    # ── lookup ──────────────────────────────────────────────────────────
    def resolve(self, name: str) -> Optional[PlayerRecord]:
        """Return the player this name refers to, or None.

        Three matching tiers, all anchored on the SURNAME so that a phrase which
        merely contains a player's first name ("Following Amadou Onana", "Watch
        Martin Dubravka") can never resolve:

          1. Exact match on a full name, web name, or declared alias.
          2. Multi-token: every query token appears as a whole word in the
             player's full name. A query token the player's name does not
             contain ("website", "following", "watch") kills the match.
          3. Single token: matches only a player's short/web name exactly
             ("Costinha", "Rodri"), never a first name on its own.
        """
        query = normalize(name)
        if not query:
            return None

        if record := self._exact.get(query):
            return record

        toks = _tokens(query)
        if len(toks) < 2:
            return None  # tier 3 is exact-only, already covered by self._exact

        for record in self._records:
            full = normalize(record.name)
            if all(re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", full) for t in toks):
                return record
        return None

    def contains(self, name: str) -> bool:
        return self.resolve(name) is not None


# ── loading ─────────────────────────────────────────────────────────────

def _club_key_for_team(team_id, fpl_data: dict) -> Optional[str]:
    from src.fpl_feed import resolve_club_key

    for team in fpl_data.get("teams", []) or []:
        if team.get("id") == team_id:
            raw = f"{team.get('name', '')} {team.get('short_name', '')}".lower()
            return resolve_club_key(raw)
    return None


_FPL_POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _records_from_fpl(fpl_data: Optional[dict]) -> list[PlayerRecord]:
    if not isinstance(fpl_data, dict):
        return []
    elements = fpl_data.get("elements") or []
    if not isinstance(elements, list):
        return []

    out: list[PlayerRecord] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        full = f"{el.get('first_name', '')} {el.get('second_name', '')}".strip()
        if not full:
            continue
        out.append(
            PlayerRecord(
                name=full,
                club_key=_club_key_for_team(el.get("team"), fpl_data),
                position=_FPL_POSITIONS.get(el.get("element_type")),
                fpl_code=el.get("code"),
                fpl_element=el,
                origin="fpl",
            )
        )
    return out


def _fpl_aliases(el: dict) -> list[str]:
    """Web name is the only extra alias — never the bare first name."""
    web = (el or {}).get("web_name") or ""
    return [web] if web else []


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _records_from_overrides(today: Optional[date] = None) -> list[tuple[PlayerRecord, list[str]]]:
    """Load unexpired manual entries. A malformed or expired entry is skipped."""
    today = today or date.today()
    try:
        payload = json.loads(_OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    out: list[tuple[PlayerRecord, list[str]]] = []
    for entry in payload.get("players") or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        evidence = (entry.get("evidence_url") or "").strip()
        expires = _parse_date(entry.get("expires_at"))
        # Every field below is mandatory: an override without evidence or an
        # expiry is indistinguishable from a guess, and guesses are what this
        # registry exists to stop.
        if not name or not evidence or expires is None:
            continue
        if expires < today:
            continue
        record = PlayerRecord(
            name=name,
            club_key=entry.get("club") or None,
            position=entry.get("position") or None,
            origin="override",
            evidence_url=evidence,
        )
        aliases = [a for a in (entry.get("aliases") or []) if isinstance(a, str)]
        out.append((record, aliases))
    return out


def build_registry(fpl_data: Optional[dict] = None, today: Optional[date] = None) -> SquadRegistry:
    """Build a registry from the FPL feed plus unexpired manual overrides.

    ``fpl_data`` may be passed in by a caller that already fetched it this run;
    otherwise the on-disk cache is read. Neither available means an EMPTY
    registry, which blocks publishing rather than waving names through.
    """
    if fpl_data is None:
        try:
            fpl_data = json.loads(_FPL_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fpl_data = None

    registry = SquadRegistry()
    for record in _records_from_fpl(fpl_data):
        registry.add(record, aliases=_fpl_aliases(record.fpl_element))
    for record, aliases in _records_from_overrides(today):
        registry.add(record, aliases=aliases)
    return registry


# ── process-wide singleton ──────────────────────────────────────────────
_REGISTRY: Optional[SquadRegistry] = None


def get_registry(fpl_data: Optional[dict] = None) -> SquadRegistry:
    """Return the cached registry, building it on first use."""
    global _REGISTRY
    if _REGISTRY is None or (fpl_data is not None and not _REGISTRY):
        _REGISTRY = build_registry(fpl_data)
    return _REGISTRY


def refresh_registry(fpl_data: Optional[dict] = None) -> SquadRegistry:
    """Rebuild the singleton — call once per run after fetching the FPL feed."""
    global _REGISTRY
    _REGISTRY = build_registry(fpl_data)
    return _REGISTRY


def set_registry(registry: Optional[SquadRegistry]) -> None:
    """Install a specific registry (tests, or an offline replay run)."""
    global _REGISTRY
    _REGISTRY = registry


def resolve_player(name: str, fpl_data: Optional[dict] = None) -> Optional[PlayerRecord]:
    return get_registry(fpl_data).resolve(name)


def is_known_player(name: str, fpl_data: Optional[dict] = None) -> bool:
    return resolve_player(name, fpl_data) is not None
