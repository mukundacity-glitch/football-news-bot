"""Deterministic concise X post templates using verified facts only.

Production rule: every live tweet must be clear, human-readable, and at most
four visible lines. The image card carries the large visual detail AND the
official-source citation (footer); the text caption never includes a URL or a
"Source:" line -- this keeps captions short enough for non-premium X accounts
without needing the character budget a link/citation would cost.
"""

from __future__ import annotations

import re
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
            if is_reported_transfer(decision):
                return self._render_reported_transfer(decision)
            return self._render_official_transfer(decision)

        if event == EventType.PRESS_CONFERENCE:
            return self._render_official_press_conference(decision)

        if event == EventType.INJURY:
            player = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            line1 = f"🚑 OFFICIAL INJURY UPDATE: {player} — {club}"
            status = str(required(facts, "injury_status")).rstrip(".")
            line2 = status
            if facts.get("return_date"):
                line2 = f"{line2} • Return: {facts['return_date']}"

        elif event == EventType.SUSPENSION:
            person = str(required(facts, "subject_name"))
            club = str(required(facts, "club_name"))
            line1 = f"🟥 Official suspension: {person} — {club}"
            status = str(required(facts, "suspension_status")).rstrip(".")
            parts = [status]
            if facts.get("suspension_length"):
                parts.append(f"Length: {facts['suspension_length']}")
            if facts.get("return_date"):
                parts.append(f"Return: {facts['return_date']}")
            line2 = " • ".join(parts)

        elif event == EventType.MANAGER:
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

    def _render_reported_transfer(self, decision: VerificationDecision) -> str:
        """Render a tier-one report without upgrading it to confirmation.

        The wording is intentionally template-only and fact-preserving. The
        source appears on the card footer, never in the caption.
        """
        facts = decision.verified_facts
        player = str(required(facts, "subject_name"))
        origin = str(required(facts, "club_from_name"))
        destination = str(required(facts, "club_to_name"))
        status = reported_status_label(decision.status, facts)

        detail_bits = [f"Status: {status}"]
        if facts.get("fee"):
            detail_bits.append(f"Fee: {facts['fee']}")
        if facts.get("contract_length"):
            detail_bits.append(f"Contract: {facts['contract_length']}")
        lines: List[str] = [
            f"🚨 REPORTED TRANSFER: {player} — {origin} to {destination}",
            " • ".join(detail_bits) + ".",
            "This is not an official transfer announcement.",
            self._hashtags(decision),
        ]
        result = self._fit_four_lines(lines, protect_first_n=3)
        decision.rendered_text = result
        return result

    def _render_official_transfer(self, decision: VerificationDecision) -> str:
        """Exact official-confirmed-only transfer caption template.

        Per the non-negotiable transfer policy this bot enforces:
          - status must be OFFICIAL or COMPLETED ("official_confirmed")
          - both FROM and TO clubs are always shown
          - fee is "Fee: <official fee>" if stated, else "Fee: undisclosed" --
            never invented, estimated, or attributed to a "reported" figure
          - contract length is shown only if the official announcement stated it
          - if player name, from club, to club, or official source is missing,
            the caller must not reach this method at all (the strict gate in
            official_transfer_gate.validate_official_transfer already refuses
            to authorize card/caption generation in that case)
          - no URL and no "Source:"/"Official confirmation:" line in the
            caption text -- the official source citation is shown on the card
            image footer instead, and the caption stays within four lines to
            fit non-premium X accounts.
        """
        facts = decision.verified_facts
        if decision.status not in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
            raise UnverifiedTransferError(
                f"refusing to render a non-official-confirmed transfer status: "
                f"{decision.status.value}"
            )

        player = str(required(facts, "subject_name"))
        destination = str(required(facts, "club_to_name"))
        origin = str(required(facts, "club_from_name"))
        # The official source must still exist and be resolvable -- the
        # caption simply does not print it (it prints on the card instead).
        authority_ids = decision.authority_source_ids or decision.source_ids
        source_id = authority_ids[0] if authority_ids else ""
        profile = self.sources.get(source_id)
        if not (profile and profile.display_name) or not decision.source_url:
            raise UnverifiedTransferError("missing official source name/url")

        lines: List[str] = [
            f"✅ Confirmed transfer: {player} has joined {destination} from {origin}."
        ]

        meta_bits = [
            str(b) for b in (
                facts.get("position"),
                facts.get("nationality"),
                (f"Age {facts['age']}" if facts.get("age") else None),
            ) if b
        ]
        if meta_bits:
            lines.append(" | ".join(meta_bits))

        fee_line = f"Fee: {facts['fee']}" if facts.get("fee") else "Fee: undisclosed"
        if facts.get("contract_length"):
            fee_line = f"{fee_line} • Contract: {facts['contract_length']}"
        lines.append(fee_line)

        lines.append(self._hashtags(decision))

        result = self._fit_four_lines(lines, protect_first_n=2)
        decision.rendered_text = result
        return result

    def _render_official_press_conference(self, decision: VerificationDecision) -> str:
        """Official-confirmed-only press conference caption template.

        Same non-negotiable bar as TRANSFER: only a first-party official
        source may make this publishable (see
        engine.py._configured_nonofficial_confirmation, which refuses every
        non-official source for PRESS_CONFERENCE exactly like TRANSFER). A
        reliable media outlet quoting the same presser is not sufficient.
        """
        facts = decision.verified_facts
        if decision.status not in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
            raise UnverifiedPressConferenceError(
                f"refusing to render a non-official-confirmed press conference "
                f"status: {decision.status.value}"
            )

        speaker = str(required(facts, "subject_name"))
        club = str(required(facts, "club_name"))
        quote_summary = str(required(facts, "quote_summary")).rstrip(".")
        authority_ids = decision.authority_source_ids or decision.source_ids
        source_id = authority_ids[0] if authority_ids else ""
        profile = self.sources.get(source_id)
        if not (profile and profile.display_name) or not decision.source_url:
            raise UnverifiedPressConferenceError("missing official source name/url")

        lines: List[str] = [f"🎙️ {speaker} ({club}) — press conference"]
        lines.append(quote_summary)
        if facts.get("quote_topic"):
            lines.append(str(facts["quote_topic"]))
        lines.append(self._hashtags(decision))

        result = self._fit_four_lines(lines, protect_first_n=2)
        decision.rendered_text = result
        return result

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
