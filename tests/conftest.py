"""Shared test fixtures.

The entity classifier is CLOSED-WORLD: a name becomes a PLAYER only by resolving
against the squad registry (see ``src.squad_registry``). In production that
registry is the live FPL roster. Tests must therefore declare the players they
consider real, exactly as production declares them — a test that asserts
"Bukayo Saka is a player" is only meaningful against a roster that contains him.

This is the point of the design: there is no ambient "assume it is a player"
default to lean on, in tests or anywhere else.
"""

import pytest

from src import squad_registry
from src.squad_registry import PlayerRecord, SquadRegistry

# Real footballers used across the suite as valid subjects, with the club the
# tests treat as theirs. Add a player here when a test needs one to be real.
TEST_SQUAD = [
    ("Bukayo Saka", "arsenal", "MID"),
    ("Declan Rice", "arsenal", "MID"),
    ("Reece James", "chelsea", "DEF"),
    ("Cole Palmer", "chelsea", "MID"),
    ("Erling Haaland", "man_city", "FWD"),
    ("Kylian Mbappe", None, "FWD"),
    ("Mohamed Salah", "liverpool", "FWD"),
    ("Joe Gomez", "liverpool", "DEF"),
    ("Alexander Isak", "newcastle", "FWD"),
    ("Callum Wilson", "newcastle", "FWD"),
    ("Danny Welbeck", "brighton", "FWD"),
    ("Joao Pedro", "brighton", "FWD"),
    ("Brajan Gruda", "brighton", "MID"),
    ("Michael Svoboda", "brighton", "DEF"),
    ("Jeremy Sarmiento", "brighton", "MID"),
    ("Pascal Struijk", "leeds", "DEF"),
    ("Costinha", "brighton", "MID"),
    ("Zadok Yohanna", "brighton", "FWD"),
    ("Sil Swinkels", "aston_villa", "DEF"),
    ("Luca Stephenson", "liverpool", "DEF"),
    ("Michael Kayode", "brentford", "DEF"),
    ("Maxence Lacroix", "crystal_palace", "DEF"),
    ("Alejandro Garnacho", "man_utd", "FWD"),
    ("Nicolas Otamendi", None, "DEF"),
    ("Milan Djuric", None, "FWD"),
    ("David Villa", None, "FWD"),
    ("Sergio Ramos", None, "DEF"),
    ("Roma Cadette", None, "MID"),
    ("Amadou Onana", "aston_villa", "MID"),
    ("Carlos Baleba", "brighton", "MID"),
    ("Tyrique George", "chelsea", "FWD"),
    ("Martin Dubravka", "newcastle", "GKP"),
    ("Jack Draper", None, "MID"),
    ("Adam Wharton", "crystal_palace", "MID"),
]


def build_test_registry() -> SquadRegistry:
    return SquadRegistry(
        PlayerRecord(name=name, club_key=club, position=pos, origin="test")
        for name, club, pos in TEST_SQUAD
    )


@pytest.fixture(autouse=True)
def _squad_registry():
    """Install the test roster for every test, and tear it down afterwards."""
    squad_registry.set_registry(build_test_registry())
    yield
    squad_registry.set_registry(None)
