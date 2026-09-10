"""Presentation-only normalization for fixed FPL VORTEX post/card output.

This module never changes verification, source authority, event classification, or
publication gates. It only projects already-verified injury facts into one
consistent reader-facing status and removes contradictory/duplicate wording.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


_ALLOWED_INJURY_STATUSES = {"OUT", "DOUBTFUL", "RETURNING"}
_UNKNOWN_RETURN_VALUES = {
    "unknown", "tbc", "tbd", "n/a", "na", "none", "not known",
}
_UNKNOWN_RETURN_RE = re.compile(
    r"(?:unknown\s+return(?:\s+date)?|"
    r"return(?:\s+date)?\s*(?:[:\-–—]\s*)?(?:unknown|tbc|tbd|not\s+known))",
    re.IGNORECASE,
)
_UNKNOWN_RETURN_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—|:]\s*)?"
    r"(?:unknown\s+return(?:\s+date)?|"
    r"return(?:\s+date)?\s*(?:[:\-–—]\s*)?(?:unknown|tbc|tbd|not\s+known))"
    r"\s*$",
    re.IGNORECASE,
)
_KNOWN_RETURN_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—|:]\s*)?"
    r"(?P<return>"
    r"(?:expected|due)\s+(?:back|return(?:\s+date)?)(?:\s+.+)?|"
    r"expected\s+to\s+return(?:\s+.+)?|"
    r"return\s+date\s*(?:[:\-–—]\s*)?\S.+"
    r")\s*$",
    re.IGNORECASE,
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _explicit_unknown_return(facts: Mapping[str, Any]) -> bool:
    injury = _clean(facts.get("injury_status"))
    if _UNKNOWN_RETURN_RE.search(injury):
        return True
    return_date = _clean(facts.get("return_date"))
    if not return_date:
        return False
    normalized = return_date.casefold().strip(" .:-–—")
    return (
        normalized in _UNKNOWN_RETURN_VALUES
        or bool(_UNKNOWN_RETURN_RE.search(return_date))
    )


def injury_display_status(facts: Mapping[str, Any]) -> str:
    """Return the single public injury status used by both text and graphic.

    The source pipeline is left untouched. The one contradiction corrected here
    is a RETURNING label produced from text such as ``Unknown return date``: the
    word ``return`` is not positive return evidence, so that presentation becomes
    OUT unless the verified facts explicitly say DOUBTFUL/OUT instead.
    """
    explicit = _clean(facts.get("availability_status")).upper()
    evidence = " ".join(
        value.casefold()
        for value in (
            _clean(facts.get("injury_status")),
            _clean(facts.get("return_date")),
        )
        if value
    )

    if explicit in {"OUT", "DOUBTFUL"}:
        return explicit
    if explicit == "FIT":
        return "RETURNING"
    if explicit == "RETURNING":
        return "OUT" if _explicit_unknown_return(facts) else "RETURNING"

    if _explicit_unknown_return(facts):
        return "OUT"
    if any(
        token in evidence
        for token in ("ruled out", "unavailable", "will miss", "not available")
    ):
        return "OUT"
    if any(
        token in evidence
        for token in ("doubt", "50%", "75%", "late fitness", "chance of playing")
    ):
        return "DOUBTFUL"
    if any(
        token in evidence
        for token in (
            "back in training", "returned to training", "returning",
            "expected back", "expected to return", "due back", "recovering",
            "recovery", "cleared", "fit", "available",
        )
    ):
        return "RETURNING"

    # A verified injury with no positive availability cue is presented fail-safe
    # as OUT rather than inventing a return signal.
    return "OUT"


def injury_display_parts(facts: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(injury, return_text)`` without duplicated return wording."""
    raw_injury = _clean(facts.get("injury_status"))
    return_date = _clean(facts.get("return_date"))

    if _explicit_unknown_return(facts):
        injury = _UNKNOWN_RETURN_SUFFIX_RE.sub("", raw_injury).strip(" -–—|:")
        return_text = "Unknown return date"
    elif return_date:
        injury = raw_injury
        return_text = return_date
    else:
        match = _KNOWN_RETURN_SUFFIX_RE.search(raw_injury)
        if match:
            injury = raw_injury[: match.start()].strip(" -–—|:")
            return_text = _clean(match.group("return"))
        else:
            injury = raw_injury
            return_text = "Unknown return date"

    if not injury:
        injury = raw_injury or "Injury update"
    return injury, return_text


def injury_graphic_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Copy verified facts into a contradiction-free graphic projection."""
    projected = dict(facts)
    injury, return_text = injury_display_parts(facts)
    projected["availability_status"] = injury_display_status(facts)

    if return_text == "Unknown return date":
        projected["injury_status"] = f"{injury} - {return_text}"
        # Avoid a second row repeating UNKNOWN/TBC when the injury row already
        # contains the fixed unknown-return wording.
        if _clean(facts.get("return_date")):
            projected.pop("return_date", None)
    return projected


def valid_injury_status(value: object) -> bool:
    """Small public contract used by tests and future presentation adapters."""
    return _clean(value).upper() in _ALLOWED_INJURY_STATUSES
