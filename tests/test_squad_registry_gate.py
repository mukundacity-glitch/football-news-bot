"""Regression corpus: subjects the bot actually published, that were not players.

Every name in ``PUBLISHED_FALSE_SUBJECTS`` was pulled from ``queue/posted/`` —
these are real tweets that went out. They share one property: not one of them is
a footballer, and not one of them could be stopped by a blacklist, because a
blacklist can only reject a phrase somebody already thought of.

The closed-world registry stops all of them for the same reason, without naming
any of them: they resolve against no squad.
"""

import pytest

from src import squad_registry
from src.entity_guard import classify_entity_detailed, is_postable_player
from src.squad_registry import PlayerRecord, SquadRegistry, build_registry
from src.verification.extractor import document_name_is_person_like

# Subjects that were published as transfer/injury/manager news. Grouped by what
# they really are, to show the failure was never one bad name — it was a whole
# open category the old default let through.
PUBLISHED_FALSE_SUBJECTS = [
    # Website furniture scraped off an official club account.
    "Manchester United Website",
    # Club officials and historical figures, not players.
    "Ken Bates",
    # An athlete from an entirely different sport.
    "Ben Duckett",
    # A player who exists, but not in the Premier League.
    "Vinicius Jr",
    # A journalist's name fused onto a player's.
    "Ederson Fabrizio Romano",
    # A club parsed as a person.
    "Inter Milan",
    # Social/RSS chrome glued to a real player's name.
    "Following Amadou Onana",
    "Watch Martin Dubravka",
    "Link Click",
    # Generic page copy that satisfies every "looks like a name" shape test.
    "Season Ticket Holders",
    "Club Statement",
    "Premier League Fixtures",
]

REAL_PLAYERS = ["Carlos Baleba", "Tyrique George", "Bukayo Saka", "Amadou Onana"]


@pytest.mark.parametrize("subject", PUBLISHED_FALSE_SUBJECTS)
def test_published_false_subject_is_not_a_player(subject):
    etype, reason = classify_entity_detailed(subject, "", None)
    assert etype != "PLAYER", (subject, etype, reason)
    assert is_postable_player(subject, "", "transfer")[0] is False, subject


@pytest.mark.parametrize("subject", PUBLISHED_FALSE_SUBJECTS)
def test_official_source_cannot_establish_a_false_subject(subject):
    """The V2 hole: an official club account used to be able to mint a new
    entity from any 2-6 word capitalised phrase. Being first-party is authority
    over the FACTS of a story, never over whether its subject exists."""
    assert document_name_is_person_like(subject) is False, subject


@pytest.mark.parametrize("subject", REAL_PLAYERS)
def test_real_players_still_publish(subject):
    assert is_postable_player(subject, "", "transfer")[0] is True, subject


def test_confirmation_language_does_not_override_the_registry():
    """'OFFICIAL', 'confirmed', 'here we go' are claims about a story, not
    evidence its subject is a person. They must not buy a way past the gate."""
    for text in (
        "OFFICIAL: Manchester United Website has joined Man Utd.",
        "Here we go! Season Ticket Holders completes permanent move to Arsenal.",
        "Confirmed: Ben Duckett signs for Leeds, medical passed.",
    ):
        subject = text.split(":")[-1].strip().split(" has ")[0].split(" completes")[0].split(" signs")[0]
        assert is_postable_player(subject, text, "transfer")[0] is False, subject


# ── the registry itself ─────────────────────────────────────────────────

def test_empty_registry_fails_closed():
    """No roster means nothing publishes. It must never mean 'assume real'."""
    squad_registry.set_registry(SquadRegistry())
    for name in [*REAL_PLAYERS, *PUBLISHED_FALSE_SUBJECTS]:
        assert is_postable_player(name, "", "transfer")[0] is False, name


def test_partial_name_does_not_resolve_to_a_real_player():
    """A phrase that merely CONTAINS a player's name is not that player."""
    squad_registry.set_registry(SquadRegistry([
        PlayerRecord(name="Amadou Onana", club_key="aston_villa"),
        PlayerRecord(name="Martin Dubravka", club_key="newcastle"),
    ]))
    registry = squad_registry.get_registry()
    assert registry.resolve("Amadou Onana") is not None
    assert registry.resolve("Following Amadou Onana") is None
    assert registry.resolve("Watch Martin Dubravka") is None
    assert registry.resolve("Amadou Onana Transfer News") is None


def test_first_name_alone_never_resolves():
    """'Amadou' must not become Amadou Onana — that is how a wrong player ends
    up on a card with the right club's crest."""
    squad_registry.set_registry(SquadRegistry([
        PlayerRecord(name="Amadou Onana", club_key="aston_villa"),
    ]))
    assert squad_registry.resolve_player("Amadou") is None


def test_overrides_require_evidence_and_expiry(tmp_path, monkeypatch):
    """A manual override without first-party evidence, or without an expiry, is
    indistinguishable from a guess — and is ignored."""
    import json
    from datetime import date

    overrides = tmp_path / "squad_overrides.json"
    overrides.write_text(json.dumps({
        "players": [
            {"name": "Valid Signing", "club": "arsenal",
             "evidence_url": "https://www.arsenal.com/news/valid-signing",
             "expires_at": "2099-01-01"},
            {"name": "No Evidence", "club": "arsenal", "expires_at": "2099-01-01"},
            {"name": "No Expiry", "club": "arsenal",
             "evidence_url": "https://www.arsenal.com/news/no-expiry"},
            {"name": "Expired Entry", "club": "arsenal",
             "evidence_url": "https://www.arsenal.com/news/expired",
             "expires_at": "2020-01-01"},
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(squad_registry, "_OVERRIDES", overrides)

    registry = build_registry(fpl_data=None, today=date(2026, 8, 7))
    assert registry.resolve("Valid Signing") is not None
    assert registry.resolve("No Evidence") is None
    assert registry.resolve("No Expiry") is None
    assert registry.resolve("Expired Entry") is None


def test_override_cannot_shadow_the_official_feed():
    """The FPL roster wins. An override is a stopgap for players it has not
    listed yet, never a way to redefine one it has."""
    registry = SquadRegistry()
    registry.add(PlayerRecord(name="Cole Palmer", club_key="chelsea", origin="fpl"))
    registry.add(PlayerRecord(name="Cole Palmer", club_key="arsenal", origin="override"))
    resolved = registry.resolve("Cole Palmer")
    assert resolved.origin == "fpl"
    assert resolved.club_key == "chelsea"
