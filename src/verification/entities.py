"""Dynamic football entity registry.

Current Premier League clubs and players come from the official FPL bootstrap
feed, so promotion/relegation and roster changes do not require code edits.  The
registry uses exact, unique aliases only; fuzzy substring matches are candidate
hints, never validation.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import EntityRecord, EntityType


def normalize_entity_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def entity_slug(value: object) -> str:
    return normalize_entity_name(value).replace(" ", "-")


class EntityRegistry:
    def __init__(self) -> None:
        self._records: Dict[str, EntityRecord] = {}
        self._aliases: Dict[tuple[EntityType, str], List[str]] = {}
        self._active_clubs: set[str] = set()
        self._player_clubs: Dict[str, str] = {}
        self.healthy = False
        self.health_reason = "not_initialized"
        self.competition_id = "competition:premier-league"

    def _add(self, record: EntityRecord) -> None:
        self._records[record.id] = record
        aliases = {record.name, *record.aliases}
        for alias in aliases:
            n = normalize_entity_name(alias)
            if n:
                self._aliases.setdefault((record.entity_type, n), []).append(record.id)
        if record.entity_type == EntityType.CLUB and record.active_premier_league:
            self._active_clubs.add(record.id)
        if record.entity_type == EntityType.PLAYER and record.club_id:
            self._player_clubs[record.id] = record.club_id

    @classmethod
    def from_fpl(
        cls,
        fpl_data: Optional[dict],
        snapshot_path: str | Path = "data/entity_registry.json",
    ) -> "EntityRegistry":
        registry = cls()
        registry._add(
            EntityRecord(
                id=registry.competition_id,
                name="Premier League",
                entity_type=EntityType.COMPETITION,
                sport="football",
                confidence=1.0,
                aliases=("English Premier League", "EPL"),
                validation_source="configured_competition",
            )
        )

        # Optional external/provider snapshot (managers, coaches and foreign
        # clubs/players).  It is data, not executable logic, and can be refreshed
        # by any future API adapter without changing this module.
        registry._load_snapshot(snapshot_path)
        registry._load_reference_clubs("data/clubs_extended.json")

        if not isinstance(fpl_data, dict):
            registry.health_reason = "official_fpl_registry_unavailable"
            return registry
        teams = fpl_data.get("teams")
        elements = fpl_data.get("elements")
        if not isinstance(teams, list) or not teams or not isinstance(elements, list):
            registry.health_reason = "official_fpl_registry_incomplete"
            return registry

        team_ids: Dict[int, str] = {}
        for team in teams:
            name = str(team.get("name") or "").strip()
            if not name or team.get("id") is None:
                continue
            club_id = f"club:{entity_slug(name)}"
            aliases = tuple(
                value
                for value in (
                    team.get("short_name"),
                    team.get("code"),
                    name.replace(" and ", " & "),
                )
                if value
            )
            registry._add(
                EntityRecord(
                    id=club_id,
                    name=name,
                    entity_type=EntityType.CLUB,
                    sport="football",
                    confidence=1.0,
                    aliases=aliases,
                    active_premier_league=True,
                    validation_source="official_fpl",
                )
            )
            team_ids[int(team["id"])] = club_id

        for player in elements:
            if player.get("id") is None:
                continue
            first = str(player.get("first_name") or "").strip()
            second = str(player.get("second_name") or "").strip()
            full = f"{first} {second}".strip()
            web = str(player.get("web_name") or "").strip()
            if not full and not web:
                continue
            club_id = team_ids.get(int(player.get("team") or 0))
            aliases = tuple(value for value in (web, second) if value and value != full)
            registry._add(
                EntityRecord(
                    id=f"player:fpl:{player['id']}",
                    name=full or web,
                    entity_type=EntityType.PLAYER,
                    sport="football",
                    confidence=1.0,
                    aliases=aliases,
                    club_id=club_id,
                    active_premier_league=bool(club_id),
                    validation_source="official_fpl",
                )
            )

        registry.healthy = bool(registry._active_clubs)
        registry.health_reason = "ok" if registry.healthy else "no_active_premier_league_clubs"
        return registry

    def _load_snapshot(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("schema_version") != 1:
            return
        for item in data.get("entities", []):
            try:
                record = EntityRecord(
                    id=item["id"],
                    name=item["name"],
                    entity_type=EntityType(item["entity_type"]),
                    sport=item.get("sport", "football"),
                    confidence=float(item.get("confidence", 0.0)),
                    aliases=tuple(item.get("aliases", [])),
                    club_id=item.get("club_id"),
                    active_premier_league=bool(item.get("active_premier_league", False)),
                    validation_source=item.get("validation_source", "provider_snapshot"),
                )
            except Exception:
                continue
            self._add(record)

    def _load_reference_clubs(self, path: str | Path) -> None:
        """Load non-PL clubs from a replaceable reference-data snapshot.

        These records establish that an extracted organization is a football
        club; they never establish Premier League relevance.  Active PL status
        always comes from the live official FPL feed.
        """
        p = Path(path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        for name in data.get("known_clubs", []):
            display = str(name or "").strip()
            if not display:
                continue
            club_id = f"club:{entity_slug(display)}"
            if club_id in self._records:
                continue
            self._add(
                EntityRecord(
                    id=club_id,
                    name=display,
                    entity_type=EntityType.CLUB,
                    sport="football",
                    confidence=0.98,
                    aliases=(),
                    active_premier_league=False,
                    validation_source="football_club_reference",
                )
            )

    def get(self, entity_id: Optional[str]) -> Optional[EntityRecord]:
        return self._records.get(entity_id or "")

    def resolve(self, name_or_id: object, entity_type: EntityType) -> Optional[EntityRecord]:
        raw = str(name_or_id or "").strip()
        if raw in self._records and self._records[raw].entity_type == entity_type:
            return self._records[raw]
        n = normalize_entity_name(raw)
        ids = list(dict.fromkeys(self._aliases.get((entity_type, n), [])))
        if len(ids) != 1:
            return None
        return self._records[ids[0]]

    def resolve_club(self, name_or_id: object) -> Optional[EntityRecord]:
        return self.resolve(name_or_id, EntityType.CLUB)

    def resolve_player(self, name_or_id: object) -> Optional[EntityRecord]:
        return self.resolve(name_or_id, EntityType.PLAYER)

    def resolve_staff(self, name_or_id: object) -> Optional[EntityRecord]:
        for kind in (EntityType.MANAGER, EntityType.COACH):
            record = self.resolve(name_or_id, kind)
            if record:
                return record
        return None

    def officially_established_person(
        self, name: str, entity_type: EntityType
    ) -> Optional[EntityRecord]:
        """Create a stable identity from a first-party official announcement.

        This is not a general fallback.  The caller may use it only after source
        provenance, official relation and a grounded two-token person name have
        been established.  It supports brand-new signings/managers before a
        provider roster has updated, while unknown media claims remain unresolved.
        """
        normalized = normalize_entity_name(name)
        tokens = normalized.split()
        if entity_type not in {EntityType.PLAYER, EntityType.MANAGER, EntityType.COACH}:
            return None
        if len(tokens) < 2 or any(not token.isalpha() for token in tokens):
            return None
        digest = hashlib.sha256(f"{entity_type.value}:{normalized}".encode()).hexdigest()[:20]
        record = EntityRecord(
            id=f"{entity_type.value.lower()}:official:{digest}",
            name=" ".join(part.capitalize() for part in normalized.split()),
            entity_type=entity_type,
            sport="football",
            confidence=0.99,
            aliases=(name,),
            validation_source="first_party_official_announcement",
        )
        self._add(record)
        return record

    def find_mentions(
        self,
        text: str,
        entity_types: Iterable[EntityType],
    ) -> list[tuple[int, EntityRecord, str]]:
        """Return exact, unambiguous registry mentions in document order."""
        normalized = normalize_entity_name(text)
        allowed = set(entity_types)
        found: Dict[str, tuple[int, EntityRecord, str]] = {}
        for (entity_type, alias), ids in self._aliases.items():
            unique_ids = list(dict.fromkeys(ids))
            if entity_type not in allowed or len(unique_ids) != 1 or len(alias) < 3:
                continue
            match = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", normalized)
            if not match:
                continue
            record = self._records[unique_ids[0]]
            previous = found.get(record.id)
            candidate = (match.start(), record, alias)
            if previous is None or match.start() < previous[0] or (
                match.start() == previous[0] and len(alias) > len(previous[2])
            ):
                found[record.id] = candidate
        return sorted(found.values(), key=lambda item: item[0])

    def active_premier_league_club(self, club_id: Optional[str]) -> bool:
        return bool(club_id and club_id in self._active_clubs)

    def player_current_club(self, player_id: Optional[str]) -> Optional[str]:
        return self._player_clubs.get(player_id or "")

    def relationship_valid(self, player_id: Optional[str], club_id: Optional[str]) -> bool:
        return bool(player_id and club_id and self._player_clubs.get(player_id) == club_id)

    @property
    def active_club_ids(self) -> frozenset[str]:
        return frozenset(self._active_clubs)
