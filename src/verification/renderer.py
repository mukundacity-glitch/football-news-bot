"""Deterministic concise X post templates using verified facts only.

Production rule: every live tweet must be clear, human-readable, and at most
four visible lines. The image card carries the large visual detail; the text
caption stays short to avoid looking like automated spam and to fit non-premium
X accounts.
"""

from __future__ import annotations

import re
from typing import Dict, List

from .models import EventType, VerificationDecision
from .source_registry import SourceRegistry


class RenderingError(RuntimeError):
    pass


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
            player = str(required(facts, "subject_name"))
            destination = str(required(facts, "club_to_name"))
            origin = facts.get("club_from_name")
            kind = str(facts.get("transfer_kind") or "").lower()
            route = f" from {origin}" if origin else ""
            # Keep this sentence shape for audit/tests and human clarity.
            line1 = f"✅ Confirmed transfer: {player} has joined {destination}{route}."
            details: List[str] = []
            if kind:
                details.append(_title_move_kind(kind))
            if facts.get("fee"):
                details.append(f"Fee: {facts['fee']}")
            if facts.get("contract_length"):
                details.append(f"Contract: {facts['contract_length']}")
            line2 = " • ".join(details) if details else "Official club confirmation."

        elif event == EventType.INJURY:
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

        source_line = self._source_line(decision)
        hashtag_line = self._hashtags(decision)
        result = self._fit_four_lines([line1, line2, source_line, hashtag_line])
        decision.rendered_text = result
        return result

    def _source_line(self, decision: VerificationDecision) -> str:
        source_id = decision.source_ids[0] if decision.source_ids else ""
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
            EventType.MANAGER: "#PremierLeague",
            EventType.CONTRACT: "#PremierLeague",
            EventType.OFFICIAL_STATEMENT: "#PremierLeague",
        }[decision.event_type]
        tags = [club_tag, event_tag, "#PremierLeague", "#FPL"]
        return " ".join(t for t in tags if t)

    def _fit_four_lines(self, lines: List[str]) -> str:
        clean = [" ".join(str(line or "").split()) for line in lines if str(line or "").strip()]
        if len(clean) > 4:
            clean = clean[:4]
        # Keep no more than four visible lines. Remove/shorten lowest-priority
        # content first: club hashtag, then detail line. Verified facts are never
        # transformed into a different claim; they are only omitted or ellipsized.
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
            if len(clean) >= 2 and len(clean[1]) > 96:
                clean[1] = clean[1][:93].rstrip(" .;,") + "…"
                continue
            if clean and len(clean[0]) > 120:
                clean[0] = clean[0][:117].rstrip(" .;,") + "…"
                continue
            if len(clean) > 3:
                clean.pop(1)
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
