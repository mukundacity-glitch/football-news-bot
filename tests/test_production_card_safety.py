from types import SimpleNamespace

from src.transfer_safety import validate_before_publish
from src.verification.models import EventStatus
from src.verification.premium_cards import THEMES, SIZE


def claim(text, source_id="club.chelsea", kind="OFFICIAL_CLUB", status=EventStatus.COMPLETED):
    return SimpleNamespace(
        status=status,
        document=SimpleNamespace(
            title=text,
            body="",
            source=SimpleNamespace(profile_id=source_id, kind=kind),
        ),
    )


def story(to="Chelsea", from_="Brighton"):
    return {
        "event": "transfer",
        "subject_name": "Danny Welbeck",
        "to_club": to,
        "from_club": from_,
        "club_to_name": to,
        "club_from_name": from_,
    }


def test_official_signing_language_is_completion_evidence():
    verdict, reason = validate_before_publish(
        story(), [claim("Chelsea sign Danny Welbeck from Brighton")]
    )
    assert verdict == "ALLOW", reason


def test_journalist_completion_can_never_be_publication_authority():
    verdict, reason = validate_before_publish(
        story(), [claim("Danny Welbeck has joined Chelsea from Brighton", "media.bbc_sport", "MEDIA")]
    )
    assert verdict == "REJECT"
    assert reason == "source_not_first_party_official"


def test_speculation_is_rejected_even_from_official_source():
    verdict, reason = validate_before_publish(
        story(), [claim("Chelsea are set to sign Danny Welbeck from Brighton")]
    )
    assert verdict == "REJECT"
    assert "speculation_language" in reason


def test_medical_is_rejected():
    verdict, _ = validate_before_publish(
        story(), [claim("Chelsea medical booked for Danny Welbeck", status=EventStatus.MEDICAL)]
    )
    assert verdict == "REJECT"


def test_card_system_has_all_four_production_families():
    assert SIZE == (3840, 2160)
    assert THEMES
    assert THEMES[next(k for k in THEMES if k.value == "TRANSFER")][1] == (0, 255, 90)
    assert THEMES[next(k for k in THEMES if k.value == "INJURY")][1] == (255, 51, 51)
    assert THEMES[next(k for k in THEMES if k.value == "SUSPENSION")][1] == (255, 170, 0)
    assert THEMES[next(k for k in THEMES if k.value == "PRESS_CONFERENCE")][1] == (0, 191, 255)
