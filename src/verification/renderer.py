"""Deterministic fixed-format X post templates using verified facts only.

Every production post keeps the same six-line shape:
headline, context, status, blank spacer, one short take, and exactly three
hashtags.  No source claim is added to the caption; source provenance remains
available to the verification pipeline and the graphic footer.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List

from .models import EventStatus, EventType, VerificationDecision
from .presentation import injury_display_parts, injury_display_status
from .reported_transfer_gate import is_reported_transfer
from .source_registry import SourceRegistry


class RenderingError(RuntimeError):
    pass


class UnverifiedTransferError(RenderingError):
    """Raised when a TRANSFER decision fails its strict authority lane."""


class UnverifiedPressConferenceError(RenderingError):
    """Raised when a PRESS_CONFERENCE decision is not official-confirmed."""


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

        event = decision.event_type
        if event == EventType.TRANSFER:
            return self._render_transfer_template(decision)
        if event == EventType.PRESS_CONFERENCE:
            return self._render_press_template(decision)
        if event == EventType.INJURY:
            return self._render_injury_template(decision)
        if event == EventType.SUSPENSION:
            return self._render_suspension_template(decision)

        facts = decision.verified_facts
        if event == EventType.MANAGER:
            person = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            action = str(required(facts, "manager_action"))
            description = [
                f"✅ MANAGER UPDATE — {self._cap(person, 42)}",
                f"{self._cap(club, 38)} | {self._cap(action, 82)}",
                "Status: OFFICIAL",
                "Monitor the FPL impact before the next deadline.",
            ]
            event_tag = "#FPLNews"
        elif event == EventType.CONTRACT:
            person = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            contract_status = str(required(facts, "contract_status"))
            detail = facts.get("contract_length") or contract_status
            description = [
                f"✍️ CONTRACT UPDATE — {self._cap(person, 42)}",
                f"{self._cap(club, 38)} | {self._cap(detail, 82)}",
                f"Status: {self._cap(contract_status.upper(), 32)}",
                "Monitor the FPL impact before the next deadline.",
            ]
            event_tag = "#FPLNews"
        elif event == EventType.OFFICIAL_STATEMENT:
            club = str(required(facts, "club_name"))
            topic = str(required(facts, "statement_topic"))
            description = [
                "📣 OFFICIAL CLUB STATEMENT",
                f"{self._cap(club, 42)} | {self._cap(topic, 90)}",
                "Status: OFFICIAL",
                "Monitor for any confirmed FPL impact.",
            ]
            event_tag = "#FPLNews"
        else:
            raise RenderingError(f"unsupported verified event: {event.value}")

        return self._finish_fixed_template(
            decision,
            description,
            self._hashtags(event_tag, club),
        )

    def _render_transfer_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        origin = str(required(facts, "club_from_name"))
        destination = str(required(facts, "club_to_name"))

        reported = is_reported_transfer(decision)
        if not reported:
            if decision.status not in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
                raise UnverifiedTransferError(
                    f"refusing transfer status: {decision.status.value}"
                )
            status = "OFFICIAL"
        elif decision.status == EventStatus.COMPLETED:
            status = "COMPLETED"
        elif decision.status in {
            EventStatus.TALKS,
            EventStatus.NEGOTIATION,
            EventStatus.BID,
            EventStatus.AGREEMENT,
            EventStatus.MEDICAL,
            EventStatus.HERE_WE_GO,
        }:
            status = "REPORTED"
        else:
            status = "PENDING"

        details: list[str] = []
        if facts.get("transfer_kind"):
            details.append(str(facts["transfer_kind"]).replace("_", " ").title())
        if facts.get("fee"):
            details.append(f"Fee {facts['fee']}")
        if facts.get("contract_length"):
            details.append(f"Contract {facts['contract_length']}")

        route = f"{self._cap(origin, 34)} → {self._cap(destination, 34)}"
        context = route
        if details:
            context += " | " + " | ".join(details[:2])

        headline = "🚨 REPORTED TRANSFER" if reported else "✅ TRANSFER UPDATE"
        take = (
            "Monitor for confirmation before making an FPL move."
            if reported
            else "Review the FPL impact before your next transfer."
        )
        description = [
            f"{headline} — {self._cap(player, 42)}",
            self._cap(context, 104),
            f"Status: {status}",
            take,
        ]
        return self._finish_fixed_template(
            decision,
            description,
            self._hashtags("#TransferNews", destination),
        )

    def _render_suspension_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        club = str(required(facts, "club_name"))
        reason = str(required(facts, "suspension_status")).rstrip(".")

        status_text = " ".join(
            str(value or "")
            for value in (facts.get("suspension_status"), facts.get("return_date"))
        ).casefold()
        if any(token in status_text for token in ("served", "completed", "complete")):
            status = "COMPLETED"
        elif any(token in status_text for token in ("return", "available", "eligible")):
            status = "RETURNING"
        else:
            status = "SUSPENDED"

        context = f"{self._cap(club, 38)} | {self._cap(reason, 70)}"
        if facts.get("return_date"):
            context += f" – {self._cap(facts['return_date'], 34)}"
        elif facts.get("matches_to_miss"):
            context += f" – {self._cap(facts['matches_to_miss'], 34)}"
        elif facts.get("suspension_length"):
            context += f" – {self._cap(facts['suspension_length'], 34)}"

        description = [
            f"⛔ SUSPENSION UPDATE — {self._cap(player, 42)}",
            self._cap(context, 104),
            f"Status: {status}",
            "Check your squad before the deadline.",
        ]
        return self._finish_fixed_template(
            decision,
            description,
            self._hashtags("#SuspensionNews", club),
        )

    def _render_injury_template(self, decision: VerificationDecision) -> str:
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        club = str(required(facts, "club_name"))
        required(facts, "injury_status")

        injury, return_text = injury_display_parts(facts)
        status = injury_display_status(facts)
        description = [
            f"🚑 INJURY UPDATE — {self._cap(player, 42)}",
            self._cap(
                f"{club} | {injury} – {return_text}",
                112,
            ),
            f"Status: {status}",
            "Monitor for more news if you own him.",
        ]
        return self._finish_fixed_template(
            decision,
            description,
            self._hashtags("#InjuryNews", club),
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
            f"🎙️ PRESS CONFERENCE — {self._cap(speaker, 42)}",
            self._cap(
                f"{club} | {update}",
                min(112, self.max_optional_fact_chars),
            ),
            f"Status: {status}",
            "Use the update when planning your next FPL move.",
        ]
        return self._finish_fixed_template(
            decision,
            description,
            self._hashtags("#PressConference", club),
        )

    @staticmethod
    def _cap(value: object, maximum: int) -> str:
        text = " ".join(str(value or "").split()).strip()
        if len(text) <= maximum:
            return text
        return text[: max(1, maximum - 1)].rstrip(" .;,|") + "…"

    @staticmethod
    def _tag(value: object) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = re.sub(r"[^A-Za-z0-9]", "", text)
        return "#" + (text or "FPL")

    def _hashtags(self, event_tag: str, club: object) -> str:
        """Exactly three fixed tags: FPL, category, club."""
        tags = ["#FPL", event_tag, self._tag(club)]
        unique: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(tag)
        if len(unique) != 3:
            raise RenderingError("fixed post requires three distinct hashtags")
        return " ".join(unique)

    def _finish_fixed_template(
        self,
        decision: VerificationDecision,
        description: List[str],
        hashtag_line: str,
    ) -> str:
        """Preserve the owner-approved fixed shape inside X's 280-char limit."""
        body = [" ".join(str(line or "").split()) for line in description]
        tags = " ".join(str(hashtag_line or "").split())
        if len(body) != 4 or not all(body):
            raise RenderingError("caption requires exactly four information lines")
        if len(tags.split()) != 3 or not all(tag.startswith("#") for tag in tags.split()):
            raise RenderingError("caption requires exactly three hashtags")

        def rendered() -> str:
            return "\n".join([body[0], body[1], body[2], "", body[3], tags])

        # Only prose is elastic. Status, spacer and the three approved hashtags
        # are never removed or rewritten by the fitter.
        minima = {1: 34, 3: 28, 0: 32}
        while twitter_weight(rendered()) > self.limit:
            candidates = [
                (len(body[index]) - minimum, index, minimum)
                for index, minimum in minima.items()
                if len(body[index]) > minimum
            ]
            if not candidates:
                raise RenderingError("fixed verified caption does not fit X limit")
            _room, index, minimum = max(candidates)
            body[index] = self._cap(
                body[index], max(minimum, len(body[index]) - 8)
            )

        result = rendered()
        lines = result.splitlines()
        if len(lines) != 6 or lines[3] != "":
            raise RenderingError("fixed caption structure changed during fitting")
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
