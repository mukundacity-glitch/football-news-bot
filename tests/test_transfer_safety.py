from types import SimpleNamespace

import pytest

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
    return {
        "event": "transfer",
        "subject_name": "John Stones",
        "to_club": to,
        "from_club": from_,
    }


def test_official_completed_transfer_allowed():
    verdict, _ = validate_before_publish(story(), [claim("John Stones has joined Inter Milan")])
    assert verdict == "ALLOW"


def test_official_club_first_completed_transfer_allowed():
    verdict, _ = validate_before_publish(
        story(), [claim("Inter Milan have signed John Stones from Manchester City")]
    )
    assert verdict == "ALLOW"


@pytest.mark.parametrize(
    "wording",
    [
        "John Stones signs a four-year contract with Inter Milan.",
        "Inter Milan are delighted to announce the arrival of John Stones from Manchester City.",
        "Inter Milan have acquired John Stones from Manchester City.",
        "Inter Milan secure the services of John Stones.",
        "John Stones puts pen to paper on a four-year deal with Inter Milan.",
        "John Stones is now an Inter Milan player.",
        "John Stones becomes Inter Milan's latest signing.",
        "John Stones has completed his permanent transfer to Inter Milan.",
        "John Stones has been loaned to Inter Milan for the season.",
        "John Stones will spend the season on loan with Inter Milan.",
        "Inter Milan unveil John Stones as their latest recruit.",
        "New signing: John Stones",
    ],
)
def test_varied_official_completion_wording_is_not_blocked(wording):
    verdict, reason = validate_before_publish(story(), [claim(wording)])
    assert verdict == "ALLOW", reason


def test_historical_speculation_does_not_block_a_later_official_confirmation():
    article = (
        "John Stones had been expected to join Inter Milan. "
        "Inter Milan have now signed John Stones from Manchester City."
    )
    verdict, reason = validate_before_publish(story(), [claim(article)])
    assert verdict == "ALLOW", reason


def test_match_reaction_words_cannot_invent_a_completed_transfer():
    article = (
        "John Stones: We can take positives from the City performance. "
        "John Stones believes Manchester City showed encouraging signs of the "
        "work being done before the opponents completed a late turnaround."
    )
    verdict, reason = validate_before_publish(
        story(), [claim(article, source_id="club.manchester-city")]
    )
    assert verdict == "REJECT"
    assert reason == "no_subject_bound_completed_route"


def test_unrelated_sentences_cannot_be_combined_into_a_transfer_route():
    article = (
        "John Stones discusses encouraging signs against Inter Milan. "
        "The move was completed late in the match."
    )
    verdict, reason = validate_before_publish(story(), [claim(article)])
    assert verdict == "REJECT"
    assert reason == "no_subject_bound_completed_route"


def test_club_name_plus_noun_signs_is_not_a_signing_verb():
    article = (
        "Inter Milan showed encouraging signs in a performance led by John Stones."
    )
    verdict, reason = validate_before_publish(story(), [claim(article)])
    assert verdict == "REJECT"
    assert reason == "no_subject_bound_completed_route"


def test_expected_transfer_rejected():
    verdict, reason = validate_before_publish(
        story(), [claim("John Stones is expected to join Inter Milan", status=EventStatus.HERE_WE_GO)]
    )
    assert verdict == "REJECT"
    assert "speculation_language" in reason


def test_set_to_join_rejected_even_if_source_is_official():
    verdict, _ = validate_before_publish(story(), [claim("John Stones is set to join Inter Milan")])
    assert verdict == "REJECT"


def test_set_to_sign_with_is_not_promoted_by_flexible_wording_support():
    verdict, reason = validate_before_publish(
        story(), [claim("John Stones is set to sign with Inter Milan")]
    )
    assert verdict == "REJECT"
    assert "speculation_language" in reason


def test_agreement_reached_rejected():
    verdict, _ = validate_before_publish(
        story(), [claim("John Stones agreement reached with Inter Milan", status=EventStatus.AGREEMENT)]
    )
    assert verdict == "REJECT"


def test_medical_booked_rejected():
    verdict, _ = validate_before_publish(
        story(), [claim("John Stones medical booked with Inter Milan", status=EventStatus.MEDICAL)]
    )
    assert verdict == "REJECT"


def test_here_we_go_rejected_on_its_own():
    verdict, _ = validate_before_publish(
        story(), [claim("John Stones here we go Inter Milan", status=EventStatus.HERE_WE_GO)]
    )
    assert verdict == "REJECT"


def test_unapproved_source_rejected():
    verdict, reason = validate_before_publish(
        story(), [claim("John Stones has joined Inter Milan", source_id="random_account", kind="MEDIA")]
    )
    assert verdict == "REJECT"
    assert reason == "source_not_first_party_official"


def test_journalist_cannot_supply_publication_authority():
    verdict, reason = validate_before_publish(
        story(),
        [claim("John Stones has joined Inter Milan", source_id="journalist.fabrizio_romano", kind="JOURNALIST")],
    )
    assert verdict == "REJECT"
    assert reason == "source_not_first_party_official"


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
        story(to="Some Unknown Club"), [claim("John Stones has joined Some Unknown Club")]
    )
    assert verdict == "REJECT"
    assert reason == "destination_unknown"


def test_destination_contamination_is_rejected_not_rewritten():
    verdict, reason = validate_before_publish(
        story(to="Arsenal", from_="Manchester City"),
        [claim("John Stones has joined Inter; Arsenal and Chelsea were interested")],
    )
    assert verdict == "REJECT"
    assert reason.startswith("conflicting_destination_evidence:")


def test_same_origin_and_destination_rejected():
    verdict, reason = validate_before_publish(
        story(to="Arsenal", from_="Arsenal"), [claim("John Stones has joined Arsenal")]
    )
    assert verdict == "REJECT"
    assert reason == "origin_equals_destination"
