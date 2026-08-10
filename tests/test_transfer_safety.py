from types import SimpleNamespace

from src.transfer_safety import resolve_destination, validate_before_publish
from src.verification.models import EventStatus


def claim(text, source_id="club.inter", kind="OFFICIAL_CLUB", status=EventStatus.COMPLETED):
    return SimpleNamespace(
        status=status,
        document=SimpleNamespace(
            title=text,
            body="",
            source=SimpleNamespace(profile_id=source_id, kind=kind),
        ),
    )


def story(to="Inter Milan", from_="Manchester City"):
    return {"event": "transfer", "to_club": to, "from_club": from_}


def test_official_completed_transfer_allowed():
    verdict, _ = validate_before_publish(
        story(),
        [claim("John Stones has joined Inter Milan")],
    )
    assert verdict == "ALLOW"


def test_pending_expected_transfer_rejected():
    verdict, reason = validate_before_publish(
        story(),
        [claim("John Stones is expected to join Inter Milan", status=EventStatus.HERE_WE_GO)],
    )
    assert verdict == "REJECT"
    assert "speculation_language" in reason


def test_set_to_join_rejected_even_if_source_is_official():
    verdict, _ = validate_before_publish(
        story(),
        [claim("John Stones is set to join Inter Milan")],
    )
    assert verdict == "REJECT"


def test_agreement_reached_rejected():
    verdict, _ = validate_before_publish(
        story(),
        [claim("John Stones agreement reached with Inter Milan", status=EventStatus.AGREEMENT)],
    )
    assert verdict == "REJECT"


def test_medical_booked_rejected():
    verdict, _ = validate_before_publish(
        story(),
        [claim("John Stones medical booked with Inter Milan", status=EventStatus.MEDICAL)],
    )
    assert verdict == "REJECT"


def test_here_we_go_rejected_on_its_own():
    verdict, _ = validate_before_publish(
        story(),
        [claim("John Stones here we go Inter Milan", status=EventStatus.HERE_WE_GO)],
    )
    assert verdict == "REJECT"


def test_unapproved_source_rejected():
    verdict, reason = validate_before_publish(
        story(),
        [claim("John Stones has joined Inter Milan", source_id="random_account", kind="MEDIA")],
    )
    assert verdict == "REJECT"
    assert reason == "source_not_approved"


def test_approved_journalist_can_supply_explicit_completion():
    verdict, _ = validate_before_publish(
        story(),
        [claim("John Stones has joined Inter Milan", source_id="journalist.fabrizio_romano", kind="JOURNALIST")],
    )
    assert verdict == "ALLOW"


def test_inter_resolves_without_becoming_arsenal():
    assert resolve_destination("Inter") == ("RESOLVED", "Inter Milan")
    assert resolve_destination("Arsenal") == ("RESOLVED", "Arsenal")


def test_inter_milan_resolves():
    assert resolve_destination("Inter Milan") == ("RESOLVED", "Inter Milan")


def test_inter_miami_does_not_resolve_to_inter_milan():
    assert resolve_destination("Inter Miami") == ("RESOLVED", "Inter Miami")


def test_psg_resolves():
    assert resolve_destination("PSG") == ("RESOLVED", "Paris Saint-Germain")


def test_barca_and_barcelona_resolve():
    assert resolve_destination("Barça") == ("RESOLVED", "Barcelona")
    assert resolve_destination("Barcelona") == ("RESOLVED", "Barcelona")


def test_milan_and_united_are_ambiguous():
    assert resolve_destination("Milan")[0] == "AMBIGUOUS"
    assert resolve_destination("United")[0] == "AMBIGUOUS"


def test_unknown_destination_rejected():
    verdict, reason = validate_before_publish(
        story(to="Some Unknown Club"),
        [claim("John Stones has joined Some Unknown Club")],
    )
    assert verdict == "REJECT"
    assert reason == "destination_unknown"


def test_destination_cannot_be_inferred_from_rival_contamination():
    verdict, reason = validate_before_publish(
        story(to="Arsenal", from_="Manchester City"),
        [claim("John Stones has joined Inter; Arsenal and Chelsea were interested")],
    )
    # The final gate validates the already-resolved destination. It must never
    # rewrite Arsenal to Inter merely because Inter appears in source text.
    assert verdict == "ALLOW"
    assert reason == "completed_transfer:Arsenal"


def test_same_origin_and_destination_rejected():
    verdict, reason = validate_before_publish(
        story(to="Arsenal", from_="Arsenal"),
        [claim("John Stones has joined Arsenal")],
    )
    assert verdict == "REJECT"
    assert reason == "origin_equals_destination"
