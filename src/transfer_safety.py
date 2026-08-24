"""Fail-closed transfer publication policy.

This is a final safety layer, not a classifier. It can reject a candidate but
never promotes a rumour, agreement, medical, bid, talks, or journalist report
to an officially confirmed transfer.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Mapping

from src.constants import CLUB_ALIASES

_POLICY_PATH = Path("config/transfer_confirmation.json")
_POLICY = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))

_SPEC_RE = [re.compile(p, re.I) for p in _POLICY["speculation_patterns"]]
_DONE_RE = [re.compile(p, re.I) for p in _POLICY["completion_patterns"]]
_ROUTE_RE = [re.compile(p, re.I) for p in _POLICY["route_evidence_patterns"]]
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
    return any(pattern.search(text) for pattern in (*_DONE_RE, *_ROUTE_RE))


def _normalize_phrase(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _phrase_regex(values: Iterable[str]) -> str:
    clean = sorted(
        {_normalize_phrase(value) for value in values if _normalize_phrase(value)},
        key=len,
        reverse=True,
    )
    return "(?:" + "|".join(
        re.escape(value).replace(r"\ ", r"\s+") for value in clean
    ) + ")" if clean else ""


def _claim_segments(claim: object) -> list[str]:
    """Return normalized title/body sentences without joining their grammar.

    A title, one body sentence, and a later match sentence are separate pieces
    of evidence. Keeping them separate prevents unrelated words across an
    article from being assembled into a transfer claim.
    """
    document = getattr(claim, "document", None)
    raw_parts = (
        getattr(document, "title", "") or "",
        getattr(document, "body", "") or "",
    )
    segments: list[str] = []
    seen: set[str] = set()
    for raw in raw_parts:
        for part in re.split(r"(?:[\r\n]+|(?<=[.!?;])\s+|\s+[|•]\s+)", raw):
            normalized = _normalize_phrase(part)
            if normalized and normalized not in seen:
                seen.add(normalized)
                segments.append(normalized)
    return segments


def _has_bounded_route_evidence(
    text: str,
    subject_re: str,
    destination_re: str,
    *,
    source_is_destination: bool,
) -> bool:
    """Match entities plus a configurable completed-transfer signal nearby.

    This is deliberately independent of sentence word order. The vocabulary
    lives in config/transfer_confirmation.json, while the code only enforces
    that the player, destination (or destination publisher), and completed
    transfer evidence belong to one short statement.
    """
    subject_matches = list(re.finditer(rf"\b{subject_re}\b", text, re.I))
    if not subject_matches:
        return False
    destination_matches = list(
        re.finditer(rf"\b{destination_re}\b", text, re.I)
    )
    if not source_is_destination and not destination_matches:
        return False

    marker_matches = [
        match
        for pattern in _ROUTE_RE
        for match in pattern.finditer(text)
    ]
    for subject in subject_matches:
        for marker in marker_matches:
            if source_is_destination:
                left = min(subject.start(), marker.start())
                right = max(subject.end(), marker.end())
                if right - left <= 180:
                    return True
            for destination in destination_matches:
                left = min(subject.start(), marker.start(), destination.start())
                right = max(subject.end(), marker.end(), destination.end())
                if right - left <= 220:
                    return True
    return False


def _subject_aliases(story: Mapping) -> set[str]:
    aliases: set[str] = set()
    for value in (story.get("subject_name"), story.get("player")):
        normalized = _normalize_phrase(value)
        if not normalized:
            continue
        aliases.add(normalized)
        tokens = normalized.split()
        if len(tokens) >= 2 and len(tokens[-1]) >= 4:
            aliases.add(tokens[-1])
    return aliases


def _club_aliases(story: Mapping, prefix: str) -> set[str]:
    aliases: set[str] = set()
    raw_values = (
        story.get(f"club_{prefix}_name"),
        story.get(f"club_{prefix}_id"),
        story.get("to_club" if prefix == "to" else "from_club"),
        story.get("to_key" if prefix == "to" else "from_key"),
    )
    target_keys: set[str] = set()
    for value in raw_values:
        normalized = _normalize_phrase(value)
        if not normalized:
            continue
        aliases.add(normalized)
        target_keys.add(normalized.removeprefix("club "))

    # Reuse the central club registry so "Man City" and "Manchester City"
    # are the same destination. This is data-driven and applies to every club;
    # no player/incident exception is introduced here.
    for alias, key in CLUB_ALIASES.items():
        normalized_key = _normalize_phrase(key)
        if normalized_key in target_keys:
            aliases.add(_normalize_phrase(alias))

    # The confirmation-policy aliases cover common non-Premier-League clubs.
    resolved_names = {
        _normalize_phrase(_canonical_club(value))
        for value in aliases
        if _canonical_club(value) not in {"UNKNOWN", "AMBIGUOUS"}
    }
    for alias, canonical in _POLICY["canonical_clubs"].items():
        if _normalize_phrase(canonical) in resolved_names:
            aliases.add(_normalize_phrase(alias))
    return {value for value in aliases if value}


def _source_is_destination(claim: object, destination_aliases: set[str]) -> bool:
    source_id = _normalize_phrase(_source_id(claim)).removeprefix("club ")
    if not source_id:
        return False
    normalized_aliases = {
        alias.removeprefix("club ") for alias in destination_aliases
    }
    return source_id in normalized_aliases


def _has_subject_bound_completed_route(story: Mapping, claim: object) -> bool:
    """Require one grammatical, player-bound completed route.

    An official publisher proves who published an article, not what every verb
    in that article means. Match reports contain words such as "signs" (noun),
    "move" (passage of play), and "complete" (a comeback). None can authorize
    a transfer unless the same claim explicitly binds the named player to a
    completed movement predicate and the extracted destination.
    """
    subjects = _subject_aliases(story)
    destinations = _club_aliases(story, "to")
    subject_re = _phrase_regex(subjects)
    destination_re = _phrase_regex(destinations)
    if not subject_re or not destination_re:
        return False

    segments = _claim_segments(claim)
    if not segments:
        return False

    player_to_club = re.compile(
        rf"\b{subject_re}\b.{{0,100}}\b(?:"
        rf"(?:has|have|had)\s+(?:signed(?:\s+(?:for|with))?|joined|moved\s+to|"
        rf"transferred\s+to|been\s+loaned\s+to|completed\s+(?:a|the)?\s*"
        rf"(?:signing|move|transfer|loan)(?:\s+to)?)|"
        rf"signs\s+for|signed\s+for|joins|joined|moves\s+to|moved\s+to|"
        rf"transfers\s+to|transferred\s+to|loaned\s+to|"
        rf"completes?\s+(?:a|the)?\s*(?:signing|move|transfer|loan)(?:\s+to)?"
        rf")\b.{{0,100}}\b{destination_re}\b",
        re.IGNORECASE,
    )
    club_directly_signs_player = re.compile(
        rf"\b{destination_re}\b(?:\s+(?:fc|football\s+club))?\s+"
        rf"(?:(?:has|have)\s+(?:now\s+)?)?(?:sign|signs|signed)\b"
        rf".{{0,120}}\b{subject_re}\b",
        re.IGNORECASE,
    )
    club_announces_player = re.compile(
        rf"\b{destination_re}\b.{{0,100}}\b(?:"
        rf"(?:has|have)\s+completed\s+(?:the\s+)?signing\s+of|"
        rf"completes?\s+(?:the\s+)?signing\s+of|"
        rf"announces?\s+(?:the\s+)?signing\s+of|"
        rf"confirms?\s+(?:the\s+)?signing\s+of"
        rf")\b.{{0,120}}\b{subject_re}\b",
        re.IGNORECASE,
    )
    club_confirms_player = re.compile(
        rf"\b{destination_re}\b.{{0,80}}\b(?:officially\s+)?(?:confirms?|announces?)\b"
        rf".{{0,100}}\b{subject_re}\b.{{0,60}}\b(?:signing|signed|joined|"
        rf"transfer|move|loan)\b",
        re.IGNORECASE,
    )
    source_is_destination = _source_is_destination(claim, destinations)
    source_destination = re.compile(
        rf"(?:\bwe\b.{{0,50}}\b(?:have\s+signed|announce\s+(?:the\s+)?"
        rf"signing\s+of|completed\s+(?:the\s+)?signing\s+of)\b.{{0,100}}"
        rf"\b{subject_re}\b|\b{subject_re}\b.{{0,80}}\b(?:joins|joined)\s+us\b)",
        re.IGNORECASE,
    )

    for text in segments:
        # A speculative sentence can never be the completed route. Historical
        # speculation in a different sentence does not taint a later explicit
        # first-party announcement.
        if _has_speculation(text):
            continue
        # A renewal sentence is not evidence of movement. A separate, explicit
        # transfer sentence in the same official article may still qualify.
        if re.search(r"\b(?:new|extended?|renewed?)\s+(?:contract|deal)\b", text):
            continue
        if (
            player_to_club.search(text)
            or club_directly_signs_player.search(text)
            or club_announces_player.search(text)
            or club_confirms_player.search(text)
            or (source_is_destination and source_destination.search(text))
            or _has_bounded_route_evidence(
                text,
                subject_re,
                destination_re,
                source_is_destination=source_is_destination,
            )
        ):
            return True
    return False


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
    if speculation and not _has_completion(text):
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

    completed_authority = [
        claim for claim in authoritative
        if str(getattr(claim, "status", "")).split(".")[-1].upper()
        in set(_POLICY["required_statuses"])
    ]
    if not any(
        _has_subject_bound_completed_route(story, claim)
        for claim in completed_authority
    ):
        if speculation:
            return "REJECT", f"speculation_language:{speculation}"
        return "REJECT", "no_subject_bound_completed_route"

    return "ALLOW", f"completed_transfer:{canonical}"
