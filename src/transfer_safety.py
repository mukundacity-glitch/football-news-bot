"""Fail-closed transfer publication policy.

This is a final safety layer, not a classifier. It can reject a candidate but
never promotes a rumour, agreement, medical, bid, talks, or journalist report
to an officially confirmed transfer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Mapping

_POLICY_PATH = Path("config/transfer_confirmation.json")
_POLICY = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))

_SPEC_RE = [re.compile(p, re.I) for p in _POLICY["speculation_patterns"]]
_DONE_RE = [re.compile(p, re.I) for p in _POLICY["completion_patterns"]]
_OFFICIAL_KINDS = set(_POLICY["official_source_kinds"])


def _blob(claim) -> str:
    document = getattr(claim, "document", None)
    title = getattr(document, "title", "") or ""
    body = getattr(document, "body", "") or ""
    return f"{title} {body}".strip()


def _source_id(claim) -> str:
    document = getattr(claim, "document", None)
    source = getattr(document, "source", None)
    return getattr(source, "profile_id", "") or ""


def _source_kind(claim) -> str:
    document = getattr(claim, "document", None)
    source = getattr(document, "source", None)
    kind = getattr(source, "kind", "")
    return getattr(kind, "value", str(kind))


def _canonical_club(value: str) -> str:
    key = re.sub(r"\s+", " ", (value or "").strip().lower())
    return _POLICY["canonical_clubs"].get(key, "UNKNOWN")


def resolve_destination(value: str) -> tuple[str, str]:
    """Return (state, canonical_name): RESOLVED, AMBIGUOUS, or UNKNOWN."""
    canonical = _canonical_club(value)
    if canonical == "AMBIGUOUS":
        return "AMBIGUOUS", ""
    if canonical == "UNKNOWN":
        return "UNKNOWN", ""
    return "RESOLVED", canonical


def _resolved_fact_club(story: Mapping, prefix: str) -> tuple[str, str]:
    """Resolve a club from canonical facts, never from nearest text mention."""
    club_id = story.get(f"club_{prefix}_id")
    club_name = story.get(f"club_{prefix}_name")
    if club_id and club_name:
        state, canonical = resolve_destination(str(club_name))
        if state == "RESOLVED":
            return state, canonical
        return "RESOLVED", str(club_name).strip()
    value = story.get("to_club" if prefix == "to" else "from_club") or story.get(
        "to_key" if prefix == "to" else "from_key"
    )
    return resolve_destination(str(value)) if value else ("UNKNOWN", "")


def _has_speculation(text: str) -> str | None:
    for pattern in _SPEC_RE:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _has_completion(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DONE_RE)


def _movement_destination(text: str) -> set[str]:
    aliases = sorted(_POLICY["canonical_clubs"], key=len, reverse=True)
    escaped = "|".join(re.escape(a) for a in aliases if _POLICY["canonical_clubs"][a] != "AMBIGUOUS")
    if not escaped:
        return set()
    pattern = re.compile(
        rf"\b(?:join(?:s|ed)?|sign(?:s|ed)?\s+for|move(?:s|d)?\s+to|"
        rf"switch(?:es|ed)?\s+to|transfer(?:red)?\s+to)\s+({escaped})\b",
        re.I,
    )
    out = set()
    for match in pattern.finditer(text):
        canonical = _canonical_club(match.group(1))
        if canonical not in {"UNKNOWN", "AMBIGUOUS"}:
            out.add(canonical)
    return out


def validate_before_publish(story: Mapping, claims: Iterable, *, event: str | None = None) -> tuple[str, str]:
    """Final ALLOW/REJECT gate for completed transfers.

    **Non-negotiable:** only a verified first-party official club/league/
    governing-body source can authorize a completed transfer. Trusted media or
    journalists can corroborate and help discovery, but they can never be the
    publication authority.
    """
    event = (event or story.get("event") or "").upper()
    claims = list(claims or [])
    if event not in {"TRANSFER", "LOAN", "LOAN_OPTION"}:
        return "ALLOW", "not a transfer gate"
    if not claims:
        return "REJECT", "no evidence claims"

    state, canonical = _resolved_fact_club(story, "to")
    if state != "RESOLVED":
        return "REJECT", f"destination_{state.lower()}"
    from_state, from_canonical = _resolved_fact_club(story, "from")
    if from_state == "AMBIGUOUS":
        return "REJECT", "origin_ambiguous"
    if from_state == "UNKNOWN" and story.get("club_from_id"):
        return "REJECT", "origin_unknown"
    if from_state == "RESOLVED" and from_canonical.lower() == canonical.lower():
        return "REJECT", "origin_equals_destination"

    text = " ".join(_blob(c) for c in claims).strip()
    speculation = _has_speculation(text)
    if speculation:
        return "REJECT", f"speculation_language:{speculation}"
    if not _has_completion(text):
        return "REJECT", "no_explicit_completion_evidence"

    explicit_destinations = _movement_destination(text)
    if explicit_destinations and not any(d.lower() == canonical.lower() for d in explicit_destinations):
        return "REJECT", (
            f"conflicting_destination_evidence:extracted={canonical};"
            f"source={sorted(explicit_destinations)}"
        )

    # Never promote approved media/journalist sources. They are corroborating
    # evidence only; publication authority must be first-party.
    authoritative = [c for c in claims if _source_kind(c) in _OFFICIAL_KINDS]
    if not authoritative:
        return "REJECT", "source_not_first_party_official"

    statuses = {
        str(getattr(c, "status", "")).split(".")[-1].upper()
        for c in authoritative
    }
    if not statuses.intersection(set(_POLICY["required_statuses"])):
        return "REJECT", f"status_not_completed:{sorted(statuses)}"

    return "ALLOW", f"completed_transfer:{canonical}"
