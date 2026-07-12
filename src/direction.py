"""
FPL VORTEX — Transfer Direction Resolver.

The base parser only knows the 20 Premier League clubs, so it misses non-PL
origins/destinations (EFL + foreign) and can invert direction — e.g.
"Swinkels joins Sheffield Wednesday from Aston Villa" wrongly resolves the
destination to Aston Villa because Sheffield Wednesday is invisible to it.

This module resolves ORIGIN and DESTINATION for a transfer using the full club
lexicon (PL aliases + EFL + foreign) and the sentence's directional grammar:

    "from <ORIGIN>"                              -> origin
    "<DEST> sign/complete signing of <player>"   -> destination (subject club)
    "<player> joins/signs for/moves to <DEST>"   -> destination

It returns canonical clubs and, where possible, PL keys, so build_story can
correct a mis-parsed direction and fill foreign clubs the parser dropped.
"""

import re
import unicodedata

from src.constants import CLUB_ALIASES

# EFL + foreign club knowledge (original-cased) for display + matching.
try:
    import json
    from pathlib import Path
    _ext = json.load(open(Path(__file__).resolve().parent.parent /
                          "data" / "clubs_extended.json", encoding="utf-8"))
    _EXTRA_CLUBS = list(_ext.get("known_clubs", []))
except Exception:
    _EXTRA_CLUBS = []


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title(norm_phrase):
    return " ".join(w.capitalize() for w in norm_phrase.split())


# Lexicon: normalized club phrase -> (canonical_display, pl_key_or_None),
# sorted LONGEST first for greedy, non-overlapping matching.
def _build_lexicon():
    lex = {}
    for alias, key in CLUB_ALIASES.items():
        n = _norm(alias)
        if n:
            lex[n] = (key.replace("_", " "), key)
    for club in _EXTRA_CLUBS:
        n = _norm(club)
        if n and n not in lex:
            lex[n] = (_title(n), None)
    return sorted(lex.items(), key=lambda kv: len(kv[0].split()), reverse=True)


_LEXICON = _build_lexicon()
_STOP = {"fc", "cf", "sc", "ac", "afc", "the", "de", "united", "city", "town"}


def _find_clubs(text_norm):
    """Return [(start_index, canonical, pl_key)] for club mentions, longest-match
    first, without overlapping a longer match already claimed."""
    claimed = [False] * (len(text_norm) + 1)
    hits = []
    for phrase, (canon, key) in _LEXICON:
        for m in re.finditer(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", text_norm):
            a, b = m.start(), m.end()
            if any(claimed[a:b]):
                continue
            for i in range(a, b):
                claimed[i] = True
            hits.append((a, canon, key))
    return sorted(hits)


_ORIGIN_PREP = re.compile(r"\bfrom\s+$")
_DEST_PREP = re.compile(r"\bto\s+$")
_DEST_VERB = re.compile(
    r"\b(join|joins|joined|joining|sign\s+for|signs\s+for|signed\s+for|"
    r"move\s+to|moves\s+to|moved\s+to|moving\s+to|switch\s+to|"
    r"loan\s+(?:move\s+)?to|heading\s+to|set\s+to\s+join|complete[sd]?\s+move\s+to)\s+$")
# "agreed a fee/deal/terms WITH <club>" is standard journalism phrasing for the
# SELLING club — just as strong an origin signal as a literal "from <club>".
# The lazy middle span absorbs any fee amount ("a 60million fee", "a fee of
# 60m") between the verb and "fee/deal/terms with" without crossing a clause.
_ORIGIN_AGREE = re.compile(
    r"\bagree[d]?\s+(?:an?\s+)?[a-z0-9\s]{0,40}?"
    r"(?:fee|deal|terms)\s+with\s+$")
# Raw proper-noun after "from" — captures an ORIGIN club not in our lexicon
# (e.g. "from Rapid Vienna"), so a genuine foreign signing keeps its direction.
_RAW_ORIGIN = re.compile(
    r"\bfrom\s+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})")
# Raw fallback for the "agreed ... with <club>" origin pattern above, for a
# selling club our lexicon doesn't know (foreign / lower-league). The verb
# phrase is matched case-insensitively but the captured club name must stay
# capitalized (a real proper noun), so re.I is scoped to the verb only. The
# lazy middle span absorbs a fee amount, incl. a "(£x/€y)" conversion aside.
_RAW_ORIGIN_AGREE = re.compile(
    r"(?i:agree[d]?\s+(?:an?\s+)?[\w.,'£€$()\s]{0,60}?"
    r"(?:fee|deal|terms)\s+with)\s+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})")
# "<CLUB1> and <CLUB2> have/has reached a (full) agreement (over the transfer)"
# — standard wire-service phrasing where the BUYING club is named first and the
# SELLING club second. Used to fill origin/destination when neither the "from"
# nor movement-verb grammar above fires (no literal "from"/"to <club>").
_RAW_JOINT_AGREEMENT = re.compile(
    r"\b([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})\s+and\s+"
    r"([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})\s+(?:have|has)\s+reached\s+"
    r"(?:an?\s+)?(?:full\s+)?agreement\b")
_ORIGIN_STOPWORDS = {"the", "a", "an", "his", "her", "their", "june", "july",
                     "january", "summer", "winter", "next", "last"}
_SUBJECT_SIGN = re.compile(
    r"\b(sign|signs|signed|complete[sd]?\s+the\s+signing\s+of|"
    r"complete[sd]?\s+signing\s+of|land[s]?|announce[sd]?|unveil[s]?|"
    r"confirm[s]?\s+signing\s+of|swoop\s+for|snap\s+up)\b")


def _lookup_club(raw_phrase):
    """Best-effort resolve a raw captured phrase against the club lexicon;
    fall back to the cleaned raw text if it isn't a club we know by name."""
    n = _norm(raw_phrase)
    for phrase, (canon, key) in _LEXICON:
        if n == phrase or n.endswith(" " + phrase) or n.startswith(phrase + " "):
            return canon, key
    cand = raw_phrase.strip().rstrip(".,;:")
    if cand and _norm(cand.split()[0]) not in _ORIGIN_STOPWORDS:
        return cand, None
    return None, None


def resolve(text):
    """Return (from_club, from_key, to_club, to_key) as best resolved from grammar.
    Any field may be None. Only fields the grammar clearly supports are set."""
    tn = _norm(text)
    if not tn:
        return None, None, None, None
    # NOTE: `clubs` may legitimately be empty (e.g. a foreign-to-foreign move
    # where neither club is in our lexicon) — the raw-text fallbacks below
    # (unknown "from X", "agreed ... with X", joint-agreement) still apply,
    # so we do NOT bail out early just because the lexicon found nothing.
    clubs = _find_clubs(tn)

    from_club = from_key = to_club = to_key = None

    # ORIGIN: club immediately preceded by "from", or by "agreed a fee/deal/
    # terms with" — the standard phrasing for naming the SELLING club.
    for start, canon, key in clubs:
        before = tn[:start]
        if _ORIGIN_PREP.search(before) or _ORIGIN_AGREE.search(before):
            from_club, from_key = canon, key
            break
    # ORIGIN fallback: a raw proper-noun after "from" / "agreed ... with" not
    # in our lexicon (e.g. a foreign or lower-league selling club).
    if from_club is None:
        m = _RAW_ORIGIN.search(text or "") or _RAW_ORIGIN_AGREE.search(text or "")
        if m:
            cand = m.group(1).strip().rstrip(".,;:")
            if cand and _norm(cand.split()[0]) not in _ORIGIN_STOPWORDS:
                from_club = cand

    # DESTINATION 1: club immediately preceded by a movement verb OR bare "to".
    for start, canon, key in clubs:
        if (canon, key) == (from_club, from_key):
            continue
        before = tn[:start]
        if _DEST_VERB.search(before) or _DEST_PREP.search(before):
            to_club, to_key = canon, key
            break

    # DESTINATION 2 (subject-signs): "<DEST> sign/complete signing of ...".
    # The destination is the club that is the grammatical SUBJECT doing the
    # signing — i.e. a club that appears BEFORE a signing verb.
    if to_club is None:
        sm = _SUBJECT_SIGN.search(tn)
        if sm:
            for start, canon, key in clubs:
                if start < sm.start() and (canon, key) != (from_club, from_key):
                    to_club, to_key = canon, key  # last club before the verb wins

    # ORIGIN/DESTINATION 3 (joint agreement): "<BUYER> and <SELLER> have/has
    # reached a (full) agreement (over the transfer of ...)" — wire-service
    # phrasing that names the buying club first, selling club second. Only
    # fills whichever side the grammar above still hasn't resolved.
    if to_club is None or from_club is None:
        jm = _RAW_JOINT_AGREEMENT.search(text or "")
        if jm:
            buyer_raw, seller_raw = jm.group(1), jm.group(2)
            if to_club is None:
                cand_canon, cand_key = _lookup_club(buyer_raw)
                if cand_canon and (cand_canon, cand_key) != (from_club, from_key):
                    to_club, to_key = cand_canon, cand_key
            if from_club is None:
                cand_canon, cand_key = _lookup_club(seller_raw)
                if cand_canon and (cand_canon, cand_key) != (to_club, to_key):
                    from_club, from_key = cand_canon, cand_key
    return from_club, from_key, to_club, to_key
