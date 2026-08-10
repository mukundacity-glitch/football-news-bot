"""Fail-closed transfer publication policy.

This module is deliberately conservative. It is a final safety layer, not a
classifier: it can only reject a candidate. It never upgrades a rumour,
agreement, medical, bid, or "here we go" item into a completed transfer.
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
_APPROVED_NONOFFICIAL = set(_POLICY["approved_nonofficial_sources"])
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


def _has_speculation(text: str) -> str | None:
    for pattern in _SPEC_RE:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _has_completion(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DONE_RE)


def _movement_destination(text: str) -> set[str]:
    """Resolve only clubs attached to explicit completed-move grammar.

    This deliberately ignores unrelated club mentions such as "Arsenal and
    Chelsea were interested". A contradiction between this set and the
    extracted destination is a hard reject; the gate never chooses a club.
    """
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
    """Final ALLOW/REJECT gate. This function is intentionally one-way.

    It returns exactly ``("ALLOW", reason)`` or ``("REJECT", reason)``.
    Uncertain, conflicting, ambiguous, missing, or speculative evidence always
    rejects. No keyword or score can create an ALLOW by itself.
    """
    event = (event or story.get("event") or "").upper()
    claims = list(claims or [])
    if event not in {"TRANSFER", "LOAN", "LOAN_OPTION"}:
        return "ALLOW", "not a transfer gate"
    if not claims:
        return "REJECT", "no evidence claims"

    destination = story.get("to_club") or story.get("to_key") or ""
    state, canonical = resolve_destination(str(destination))
    if state != "RESOLVED":
        return "REJECT", f"destination_{state.lower()}"

    from_value = story.get("from_club") or story.get("from_key") or ""
    if from_value:
        from_state, from_canonical = resolve_destination(str(from_value))
        if from_state != "RESOLVED":
            return "REJECT", f"origin_{from_state.lower()}"
        if from_canonical == canonical:
            return "REJECT", "origin_equals_destination"

    text = " ".join(_blob(c) for c in claims).strip()
    speculation = _has_speculation(text)
    if speculation:
        return "REJECT", f"speculation_language:{speculation}"
    if not _has_completion(text):
        return "REJECT", "no_explicit_completion_evidence"

    explicit_destinations = _movement_destination(text)
    if explicit_destinations and canonical not in explicit_destinations:
        return "REJECT", (
            f"conflicting_destination_evidence:extracted={canonical};"
            f"source={sorted(explicit_destinations)}"
        )

    authoritative = []
    for claim in claims:
        source_id = _source_id(claim)
        source_kind = _source_kind(claim)
        if source_kind in _OFFICIAL_KINDS:
            authoritative.append(claim)
        elif source_id in _APPROVED_NONOFFICIAL:
            # Approved journalists/media are permitted evidence only when the
            # source explicitly states completion. They are never elevated by
            # "here we go", medical, agreement, or a keyword alone.
            authoritative.append(claim)

    if not authoritative:
        return "REJECT", "source_not_approved"

    statuses = {
        str(getattr(c, "status", "")).split(".")[-1].upper()
        for c in authoritative
    }
    if not statuses.intersection(set(_POLICY["required_statuses"])):
        return "REJECT", f"status_not_completed:{sorted(statuses)}"

    return "ALLOW", f"completed_transfer:{canonical}"
