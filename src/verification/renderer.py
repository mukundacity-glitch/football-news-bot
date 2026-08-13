"""Deterministic concise X post templates using verified facts only.

Production captions follow the owner's event-specific master templates with
blank-line separation between headline, facts, status and hashtags. The image
card carries the visual detail; caption text never includes a URL or a
"Source:" line. Every template is deterministically fitted to the X limit.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List

from .models import EventStatus, EventType, VerificationDecision
from .reported_transfer_gate import (
    is_reported_transfer,
    reported_status_label,
)
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
            line1 = "✅ Official manager update"
            line2 = f"{club}: {person} — {action}."

        elif event == EventType.CONTRACT:
            person = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            required(facts, "contract_status")
            line1 = "✅ Official contract update"
            line2 = f"{person} — {club}."
            if facts.get("contract_length"):
                line2 += f" Contract: {facts['contract_length']}"

        elif event == EventType.OFFICIAL_STATEMENT:
            club = str(required(facts, "club_name"))
            topic = str(required(facts, "statement_topic"))
            line1 = "📣 Official club statement"
            line2 = f"{club}: {topic}"
        else:
            raise RenderingError(f"unsupported verified event: {event.value}")

        # Source citation lives on the card image footer only -- the caption
        # text never carries a URL or a "Source:" line. Keep it human, concise,
        # and non-premium-safe: news + SEO hashtags.
        hashtag_line = self._hashtags(decision)
        result = self._fit_four_lines([line1, line2, hashtag_line])
        decision.rendered_text = result
        return result

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

        blocks = [
            f"🚨 REPORTED TRANSFER: {self._cap(player, 64)}",
            f"{self._cap(origin, 48)} → {self._cap(destination, 48)}",
            f"STATUS: {status}",
            " ".join((
                "#TransferNews", self._tag(destination), self._tag(player), "#fpl"
            )),
        ]
        return self._finish_template(decision, blocks)

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
        blocks = [
            f"⛔ SUSPENSION: {self._cap(player, 64)}",
            f"{self._cap(club, 55)} | {self._cap(reason, 85)}",
            f"STATUS: {status}",
            " ".join(("#FPL", "#FPLNews", self._tag(club), "#suspension")),
        ]
        return self._finish_template(decision, blocks)

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
        blocks = [
            f"🚑 INJURY UPDATE: {self._cap(player, 64)}",
            self._cap(club, 60),
            f"INJURY: {self._cap(injury, 95)}",
            f"STATUS: {status}",
            " ".join(("#FPL", "#FPLNews", self._tag(club), "#Injury")),
        ]
        return self._finish_template(decision, blocks)

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
        blocks = [
            "🎙️ PRESS CONFERENCE",
            self._cap(speaker, 64),
            f"{self._cap(club, 55)} | UPDATE: {self._cap(update, 105)}",
            f"STATUS: {status}",
            " ".join(("#FPL", "#FPLNews", self._tag(club))),
        ]
        return self._finish_template(decision, blocks)

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

    def _finish_template(self, decision: VerificationDecision, blocks: List[str]) -> str:
        clean = [" ".join(str(block or "").split()) for block in blocks if str(block or "").strip()]
        text = "\n\n".join(clean)
        # Preserve the user's blank-line master layout. If an unusual long
        # verified value exceeds the X limit, shorten the longest non-hashtag
        # body block without removing status or hashtags.
        while twitter_weight(text) > self.limit:
            candidates = [
                (len(block), index) for index, block in enumerate(clean[:-1])
                if len(block) > 36 and not block.startswith("STATUS:")
            ]
            if not candidates:
                raise RenderingError("master caption does not fit X character limit")
            _, index = max(candidates)
            clean[index] = self._cap(clean[index], len(clean[index]) - 8)
            text = "\n\n".join(clean)
        decision.rendered_text = text
        return text

    def _source_line(self, decision: VerificationDecision) -> str:
        authority_ids = decision.authority_source_ids or decision.source_ids
        source_id = authority_ids[0] if authority_ids else ""
        profile = self.sources.get(source_id)
        if profile and profile.handles:
            handle = str(profile.handles[0]).strip()
            return "Source: @" + handle.lstrip("@")
        label = profile.display_name if profile else source_id
        return f"Source: {label}"

    def _hashtags(self, decision: VerificationDecision) -> str:
        club = (
            decision.verified_facts.get("club_to_name")
            or decision.verified_facts.get("club_name")
            or decision.verified_facts.get("club_from_name")
            or ""
        )
        club_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", str(club)) if club else ""
        event_tag = {
            EventType.TRANSFER: "#TransferNews",
            EventType.INJURY: "#InjuryNews",
            EventType.SUSPENSION: "#SuspensionNews",
            EventType.PRESS_CONFERENCE: "#PressConference",
            EventType.MANAGER: "#PremierLeague",
            EventType.CONTRACT: "#PremierLeague",
            EventType.OFFICIAL_STATEMENT: "#PremierLeague",
        }[decision.event_type]
        tags = [club_tag, event_tag, "#PremierLeague", "#FPL"]
        return " ".join(t for t in tags if t)

    def _fit_four_lines(self, lines: List[str], *, protect_first_n: int = 1) -> str:
        """Fit a caption within four visible lines and the X character limit.

        ``protect_first_n`` marks the leading lines that must never be
        dropped (the confirmation sentence, and for TRANSFER/PRESS_CONFERENCE
        the fee/quote line) -- only the optional meta line and the hashtag
        line may be trimmed or dropped to fit.
        """
        clean = [" ".join(str(line or "").split()) for line in lines if str(line or "").strip()]
        if len(clean) > 4:
            # Drop from the middle (optional meta line) first, keeping the
            # protected head and the hashtag tail.
            while len(clean) > 4:
                drop_at = protect_first_n if protect_first_n < len(clean) - 1 else len(clean) - 2
                clean.pop(max(protect_first_n, min(drop_at, len(clean) - 2)))
        while True:
            text = "\n".join(clean).strip()
            if twitter_weight(text) <= self.limit and len(clean) <= 4:
                return text
            if clean and clean[-1].startswith("#"):
                tags = clean[-1].split()
                if len(tags) > 3:
                    clean[-1] = " ".join(tags[1:])
                    continue
                if len(tags) > 2:
                    clean[-1] = " ".join(tags[:-1])
                    continue
            if len(clean) > protect_first_n + 1 and len(clean[-1]) == 0:
                clean.pop()
                continue
            if len(clean) >= 2 and len(clean[min(protect_first_n, len(clean) - 1)]) > 96:
                idx = min(protect_first_n, len(clean) - 1)
                clean[idx] = clean[idx][:93].rstrip(" .;,") + "…"
                continue
            if clean and len(clean[0]) > 120:
                clean[0] = clean[0][:117].rstrip(" .;,") + "…"
                continue
            if len(clean) > protect_first_n + 1:
                clean.pop(protect_first_n)
                continue
            raise RenderingError("verified required facts do not fit within X limit")



def _title_move_kind(kind: str) -> str:
    mapping = {
        "loan": "Loan",
        "permanent": "Permanent transfer",
        "free": "Free transfer",
        "loan_option": "Loan with option",
    }
    return mapping.get(kind.lower(), kind.replace("_", " ").title())


def required(facts: Dict[str, object], key: str) -> object:
    value = facts.get(key)
    if value in (None, ""):
        raise RenderingError(f"verified decision missing required fact: {key}")
    return value


def twitter_weight(text: str) -> int:
    urls = re.findall(r"https?://\S+", text)
    stripped = re.sub(r"https?://\S+", "", text)
    return len(stripped) + 23 * len(urls)
