"""Deterministic X post templates using verified facts only."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .models import DecisionType, EntityType, EventType, VerificationDecision
from .source_registry import SourceRegistry


class RenderingError(RuntimeError):
    pass


class VerifiedPostRenderer:
    def __init__(
        self, sources: SourceRegistry, limit: int = 280,
        max_optional_fact_chars: int = 140,
    ) -> None:
        self.sources = sources
        self.limit = limit
        self.max_optional_fact_chars = max_optional_fact_chars

    def render(self, decision: VerificationDecision) -> str:
        if not decision.may_publish:
            raise RenderingError("refusing to render an unverified decision")
        facts = decision.verified_facts
        event = decision.event_type
        lines: List[str] = []
        optional: List[str] = []

        if event == EventType.TRANSFER:
            player = required(facts, "subject_name")
            destination = required(facts, "club_to_name")
            origin = facts.get("club_from_name")
            kind = str(facts.get("transfer_kind") or "").lower()
            route = f" from {origin}" if origin else ""
            if kind == "loan":
                sentence = f"{player} has joined {destination}{route} on loan."
            elif kind == "permanent":
                sentence = f"{player} has joined {destination}{route} on a permanent transfer."
            elif kind == "free":
                sentence = f"{player} has joined {destination}{route} on a free transfer."
            else:
                sentence = f"{player} has joined {destination}{route}."
            lines = ["✅ CONFIRMED TRANSFER", sentence]
            if facts.get("fee"):
                optional.append(f"Fee: {facts['fee']}")
            if facts.get("contract_length"):
                optional.append(f"Contract: {facts['contract_length']}")

        elif event == EventType.INJURY:
            player = required(facts, "subject_name")
            club = required(facts, "club_name")
            lines = ["🚑 OFFICIAL INJURY UPDATE", f"{player} — {club}."]
            status = str(required(facts, "injury_status"))
            if len(status) <= self.max_optional_fact_chars:
                optional.append(status.rstrip("." ) + ".")
            if facts.get("return_date"):
                optional.append(f"Return: {facts['return_date']}")

        elif event == EventType.SUSPENSION:
            person = required(facts, "subject_name")
            club = required(facts, "club_name")
            lines = ["🟥 OFFICIAL SUSPENSION UPDATE", f"{person} — {club}."]
            status = str(required(facts, "suspension_status"))
            if len(status) <= self.max_optional_fact_chars:
                optional.append(status.rstrip(".") + ".")
            if facts.get("suspension_length"):
                optional.append(f"Length: {facts['suspension_length']}")
            if facts.get("return_date"):
                optional.append(f"Eligible return: {facts['return_date']}")

        elif event == EventType.MANAGER:
            person = required(facts, "subject_name")
            club = required(facts, "club_name")
            action = required(facts, "manager_action")
            lines = ["✅ OFFICIAL MANAGER UPDATE"]
            if action == "appointment":
                lines.append(f"{club} have appointed {person}.")
            elif action == "departure":
                lines.append(f"{person} has left {club}.")
            else:  # mandatory policy should make this unreachable
                raise RenderingError(f"unsupported manager action: {action}")
            if facts.get("contract_length"):
                optional.append(f"Contract: {facts['contract_length']}")

        elif event == EventType.CONTRACT:
            person = required(facts, "subject_name")
            club = required(facts, "club_name")
            required(facts, "contract_status")
            lines = ["✅ OFFICIAL CONTRACT EXTENSION", f"{person} has signed a contract extension with {club}."]
            if facts.get("contract_length"):
                optional.append(f"Terms: {facts['contract_length']}")

        elif event == EventType.OFFICIAL_STATEMENT:
            club = required(facts, "club_name")
            topic = str(required(facts, "statement_topic"))
            lines = ["📣 OFFICIAL CLUB STATEMENT", club]
            if len(topic) <= self.max_optional_fact_chars:
                optional.append(topic.rstrip(".") + ".")
        else:
            raise RenderingError(f"unsupported verified event: {event.value}")

        source_line = self._source_line(decision)
        hashtag_line = self._hashtags(decision)
        result = self._fit(lines, optional, source_line, hashtag_line)
        decision.rendered_text = result
        return result

    def _source_line(self, decision: VerificationDecision) -> str:
        source_id = decision.source_ids[0] if decision.source_ids else ""
        profile = self.sources.get(source_id)
        label = profile.display_name if profile else source_id
        if decision.source_url:
            return f"Source: {label} — {decision.source_url}"
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
            EventType.SUSPENSION: "#Suspension",
            EventType.MANAGER: "#ManagerNews",
            EventType.CONTRACT: "#ContractNews",
            EventType.OFFICIAL_STATEMENT: "#PremierLeague",
        }[decision.event_type]
        return " ".join(x for x in (club_tag, event_tag, "#FPL") if x)

    def _fit(
        self,
        required_lines: List[str],
        optional_lines: List[str],
        source_line: str,
        hashtag_line: str,
    ) -> str:
        # Optional facts are removed whole; verified statements are never
        # truncated into a potentially misleading fragment.
        optional = list(optional_lines)
        while True:
            sections = ["\n".join(required_lines + optional), source_line, hashtag_line]
            text = "\n\n".join(section for section in sections if section).strip()
            if twitter_weight(text) <= self.limit:
                return text
            if optional:
                optional.pop()
                continue
            if hashtag_line:
                hashtag_line = ""
                continue
            # The URL is more important than the source label if space is tight.
            if " — http" in source_line:
                source_line = "Source: " + source_line.split(" — ", 1)[1]
                continue
            raise RenderingError("verified required facts do not fit within X limit")


def required(facts: Dict[str, object], key: str) -> object:
    value = facts.get(key)
    if value in (None, ""):
        raise RenderingError(f"verified decision missing required fact: {key}")
    return value


def twitter_weight(text: str) -> int:
    urls = re.findall(r"https?://\S+", text)
    stripped = re.sub(r"https?://\S+", "", text)
    return len(stripped) + 23 * len(urls)
