"""Deterministic, mobile-readable X post templates using verified facts only.

Every production caption has four concise information lines followed by two
SEO hashtag lines.  There are no blank spacer lines, URLs, rumours disguised as
facts, or premium-account assumptions.  The fitter preserves that six-line
shape while enforcing X's normal 280-character limit.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List

from .models import EventStatus, EventType, VerificationDecision
from .reported_transfer_gate import is_reported_transfer
from .source_registry import SourceRegistry


class RenderingError(RuntimeError):
    pass


class UnverifiedTransferError(RenderingError):
    """Raised when a TRANSFER decision fails its strict authority lane.

    Official/completed transfers require first-party evidence. The separate
    reported lane accepts only its narrow source/status rules and is always
    rendered with explicit REPORTED wording.
    """


class UnverifiedPressConferenceError(RenderingError):
    """Raised when a PRESS_CONFERENCE decision is not official-confirmed.

    Same bar as TRANSFER: only a first-party official source (the club's own
    site/video/transcript, or a verified official club account explicitly
    quoting the presser) may make this publishable -- a media outlet that was
    merely in the room is not sufficient.
    """


class VerifiedPostRenderer:
    def __init__(
        self,
        sources: SourceRegistry,
        limit: int = 280,
        max_optional_fact_chars: int = 120,
    ) -> None:
        self.sources = sources
        self.limit = limit
        self.max_optional_fact_chars = max_optional_fact_chars

    def render(self, decision: VerificationDecision) -> str:
        if not decision.may_publish:
            raise RenderingError("refusing to render an unverified decision")
        facts = decision.verified_facts
        event = decision.event_type

        if event == EventType.TRANSFER:
            return self._render_transfer_template(decision)

        if event == EventType.PRESS_CONFERENCE:
            return self._render_press_template(decision)

        if event == EventType.INJURY:
            return self._render_injury_template(decision)

        if event == EventType.SUSPENSION:
            return self._render_suspension_template(decision)

        if event == EventType.MANAGER:
            person = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            action = str(required(facts, "manager_action"))
            description = [
                "✅ OFFICIAL MANAGER UPDATE",
                f"{self._cap(person, 42)} — {self._cap(club, 38)}",
                f"Decision — {self._cap(action, 80)}",
                "STATUS — OFFICIAL",
            ]
            event_tag = "#ManagerNews"

        elif event == EventType.CONTRACT:
            person = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            contract_status = str(required(facts, "contract_status"))
            detail = facts.get("contract_length") or contract_status
            description = [
                "✍️ OFFICIAL CONTRACT UPDATE",
                f"{self._cap(person, 42)} — {self._cap(club, 38)}",
                f"Contract — {self._cap(detail, 80)}",
                f"STATUS — {self._cap(contract_status.upper(), 32)}",
            ]
            event_tag = "#ContractNews"

        elif event == EventType.OFFICIAL_STATEMENT:
            club = str(required(facts, "club_name"))
            topic = str(required(facts, "statement_topic"))
            person = club
            description = [
                "📣 OFFICIAL CLUB STATEMENT",
                self._cap(club, 48),
                f"Update — {self._cap(topic, 88)}",
                "STATUS — OFFICIAL",
            ]
            event_tag = "#ClubStatement"
        else:
            raise RenderingError(f"unsupported verified event: {event.value}")

        return self._finish_elite_template(
            decision, description,
            self._seo_hashtags(decision, event_tag, person),
        )

    def _render_transfer_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        origin = str(required(facts, "club_from_name"))
        destination = str(required(facts, "club_to_name"))
        if not is_reported_transfer(decision):
            if decision.status not in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
                raise UnverifiedTransferError(
                    f"refusing transfer status: {decision.status.value}"
                )
            status = "OFFICIAL"
        elif decision.status == EventStatus.COMPLETED:
            status = "COMPLETED"
        elif decision.status in {
            EventStatus.TALKS, EventStatus.NEGOTIATION, EventStatus.BID,
            EventStatus.AGREEMENT, EventStatus.MEDICAL, EventStatus.HERE_WE_GO,
        }:
            status = "REPORTED"
        else:
            status = "PENDING"

        prefix = "🚨 REPORTED TRANSFER" if is_reported_transfer(decision) else "✅ OFFICIAL TRANSFER"
        details = []
        if facts.get("transfer_kind"):
            details.append(str(facts["transfer_kind"]).replace("_", " ").title())
        if facts.get("fee"):
            details.append(f"Fee {facts['fee']}")
        if facts.get("contract_length"):
            details.append(f"Contract {facts['contract_length']}")
        detail_line = (
            "Deal — " + " | ".join(details[:2])
            if details else f"Verified by — {self._authority_label(decision)}"
        )
        description = [
            f"{prefix} — {self._cap(player, 42)}",
            f"{self._cap(origin, 40)} → {self._cap(destination, 40)}",
            detail_line,
            f"STATUS — {status}",
        ]
        return self._finish_elite_template(
            decision, description,
            self._seo_hashtags(decision, "#TransferNews", player),
        )

    def _render_suspension_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        club = str(required(facts, "club_name"))
        reason = str(required(facts, "suspension_status")).rstrip(".")
        status_text = " ".join(str(value or "") for value in (
            facts.get("suspension_status"), facts.get("return_date")
        )).casefold()
        if any(token in status_text for token in ("served", "completed", "complete")):
            status = "COMPLETED"
        elif any(token in status_text for token in ("return", "available", "eligible")):
            status = "RETURNING"
        else:
            status = "SUSPENDED"
        if facts.get("return_date"):
            detail = f"Return — {facts['return_date']}"
        elif facts.get("matches_to_miss"):
            detail = f"Matches — {facts['matches_to_miss']}"
        elif facts.get("suspension_length"):
            detail = f"Length — {facts['suspension_length']}"
        else:
            detail = f"Verified by — {self._authority_label(decision)}"
        description = [
            f"⛔ SUSPENSION UPDATE — {self._cap(player, 42)}",
            f"{self._cap(club, 38)} — {self._cap(reason, 72)}",
            detail,
            f"STATUS — {status}",
        ]
        return self._finish_elite_template(
            decision, description,
            self._seo_hashtags(decision, "#SuspensionNews", player),
        )

    def _render_injury_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        club = str(required(facts, "club_name"))
        injury = str(required(facts, "injury_status")).rstrip(".")
        explicit = str(facts.get("availability_status") or "").strip().upper()
        allowed = {"OUT", "DOUBTFUL", "RETURNING", "FIT"}
        if explicit in allowed:
            status = explicit
        else:
            evidence = " ".join(str(value or "") for value in (
                injury, facts.get("return_date")
            )).casefold()
            if any(token in evidence for token in ("fit", "available", "cleared")):
                status = "FIT"
            elif any(token in evidence for token in (
                "return", "back in training", "expected back", "recovery"
            )):
                status = "RETURNING"
            elif any(token in evidence for token in ("doubt", "75%", "50%")):
                status = "DOUBTFUL"
            elif any(token in evidence for token in ("out", "ruled out", "unavailable", "will miss")):
                status = "OUT"
            else:
                raise RenderingError(
                    "verified injury is missing an OUT/DOUBTFUL/RETURNING/FIT availability cue"
                )
        detail = (
            f"Return — {facts['return_date']}"
            if facts.get("return_date")
            else f"Verified by — {self._authority_label(decision)}"
        )
        description = [
            f"🚑 INJURY UPDATE — {self._cap(player, 42)}",
            f"{self._cap(club, 38)} — {self._cap(injury, 76)}",
            detail,
            f"STATUS — {status}",
        ]
        return self._finish_elite_template(
            decision, description,
            self._seo_hashtags(decision, "#InjuryNews", player),
        )

    def _render_press_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        speaker = str(required(facts, "subject_name"))
        club = str(required(facts, "club_name"))
        update = str(required(facts, "quote_summary")).rstrip(".")
        if decision.status in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
            status = "CONFIRMED"
        elif decision.status in {EventStatus.UNKNOWN, EventStatus.RUMOUR, EventStatus.INTEREST}:
            status = "EXPECTED"
        else:
            status = "REPORTED"
        description = [
            "🎙️ PREMIER LEAGUE PRESS UPDATE",
            f"{self._cap(speaker, 42)} — {self._cap(club, 38)}",
            f"Key update — {self._cap(update, 86)}",
            f"STATUS — {status}",
        ]
        return self._finish_elite_template(
            decision, description,
            self._seo_hashtags(decision, "#PressConference", speaker),
        )

    @staticmethod
    def _cap(value: object, maximum: int) -> str:
        text = " ".join(str(value or "").split()).strip()
        if len(text) <= maximum:
            return text
        return text[: max(1, maximum-1)].rstrip(" .;,|") + "…"

    @staticmethod
    def _tag(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"[^A-Za-z0-9]", "", text)
        return "#" + (text or "FPL")

    def _authority_label(self, decision: VerificationDecision) -> str:
        """Return a concise, truthful display name for the authority source."""
        source_ids = decision.authority_source_ids or decision.source_ids
        source_id = source_ids[0] if source_ids else ""
        if source_id == "official.fpl":
            return "Official FPL"
        profile = self.sources.get(source_id) if source_id else None
        return self._cap(profile.display_name if profile else source_id or "verified source", 36)

    def _seo_hashtags(
        self,
        decision: VerificationDecision,
        event_tag: str,
        person: object,
    ) -> list[str]:
        """Build two intentional search lines: broad discovery, then entities."""
        facts = decision.verified_facts
        club = facts.get("club_to_name") or facts.get("club_name") or facts.get("club_from_name")
        broad = [event_tag, "#PremierLeague", "#FPL"]
        specific = [self._tag(club), self._tag(person), "#FPLNews", "#FPLVortex"]

        def unique(tags: list[str]) -> list[str]:
            result: list[str] = []
            seen: set[str] = set()
            for tag in tags:
                key = tag.casefold()
                if key not in seen:
                    seen.add(key)
                    result.append(tag)
            return result

        return [" ".join(unique(broad)), " ".join(unique(specific))]

    def _finish_elite_template(
        self,
        decision: VerificationDecision,
        description: List[str],
        hashtag_lines: List[str],
    ) -> str:
        """Preserve four information + two hashtag lines inside 280 chars."""
        body = [" ".join(str(line or "").split()) for line in description]
        tags = [" ".join(str(line or "").split()) for line in hashtag_lines]
        if len(body) != 4 or not all(body):
            raise RenderingError("caption requires exactly four information lines")
        if len(tags) != 2 or not all(line.startswith("#") for line in tags):
            raise RenderingError("caption requires exactly two hashtag lines")

        def rendered() -> str:
            return "\n".join([*body, *tags])

        # Drop only low-priority discovery/brand tags first. Event, club and
        # person tags remain, so a normal-account caption keeps useful SEO.
        removable = ("#FPLVortex", "#FPLNews", "#PremierLeague")
        for unwanted in removable:
            if twitter_weight(rendered()) <= self.limit:
                break
            for index in range(len(tags)-1, -1, -1):
                parts = tags[index].split()
                if unwanted in parts and len(parts) > 1:
                    parts.remove(unwanted)
                    tags[index] = " ".join(parts)
                    break

        # Then shorten prose, never status or hashtags. The detail line is the
        # most elastic; headline and route/entity lines retain useful context.
        minima = {2: 34, 1: 32, 0: 34}
        while twitter_weight(rendered()) > self.limit:
            candidates = [
                (len(body[index]) - minimum, index, minimum)
                for index, minimum in minima.items()
                if len(body[index]) > minimum
            ]
            if not candidates:
                raise RenderingError("six-line verified caption does not fit X limit")
            _room, index, minimum = max(candidates)
            body[index] = self._cap(body[index], max(minimum, len(body[index])-8))

        result = rendered()
        if len(result.splitlines()) != 6:
            raise RenderingError("caption line count changed during fitting")
        decision.rendered_text = result
        return result

def required(facts: Dict[str, object], key: str) -> object:
    value = facts.get(key)
    if value in (None, ""):
        raise RenderingError(f"verified decision missing required fact: {key}")
    return value


def twitter_weight(text: str) -> int:
    urls = re.findall(r"https?://\S+", text)
    stripped = re.sub(r"https?://\S+", "", text)
    return len(stripped) + 23 * len(urls)
