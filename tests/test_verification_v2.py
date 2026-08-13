"""End-to-end tests for the fail-closed verification engine v2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.verification import DecisionType, VerificationRuntime
from src.verification.card import create_verified_card
from src.verification.models import EventType
from src.verification.source_registry import SourceRegistry


@pytest.fixture
def fpl_data():
    return {
        "teams": [
            {"id": 1, "name": "Chelsea", "short_name": "CHE"},
            {"id": 2, "name": "Brighton", "short_name": "BHA"},
            {"id": 3, "name": "Arsenal", "short_name": "ARS"},
            {"id": 4, "name": "Leeds", "short_name": "LEE"},
            {"id": 5, "name": "Crystal Palace", "short_name": "CRY"},
        ],
        "elements": [
            {"id": 10, "first_name": "Danny", "second_name": "Welbeck", "web_name": "Welbeck", "team": 2},
            {"id": 11, "first_name": "Example", "second_name": "Player", "web_name": "Player", "team": 2},
        ],
    }


@pytest.fixture
def runtime(tmp_path, fpl_data):
    rt = VerificationRuntime(fpl_data=fpl_data, database_path=tmp_path / "verification.sqlite3")
    yield rt
    rt.close()


def now_iso(hours=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def observation(*, title, source_id, url, story, declared_sport="football",
                transport="DIRECT_RSS", published_at=None, structured=False):
    item = {
        "title": title,
        "summary": title,
        "source_url": url,
        "source_id": source_id,
        "source_hint": source_id,
        "transport": transport,
        "configured_direct_feed": True,
        "declared_sport": declared_sport,
        "created_at": published_at or now_iso(),
        "metadata": {"structured_official": structured},
    }
    return {"document": item, "legacy_story": story}


def transfer_story(player="Danny Welbeck", destination="Chelsea"):
    return {
        "player": player,
        "event": "transfer",
        "from_key": "Brighton",
        "from_club": "Brighton",
        "to_key": destination,
        "to_club": destination,
        "stage": 4,
    }


def test_future_promoted_club_is_loaded_dynamically_from_fpl(tmp_path):
    future_fpl = {
        "teams": [{"id": 99, "name": "Future Town", "short_name": "FUT"}],
        "elements": [{
            "id": 999, "first_name": "Existing", "second_name": "Player",
            "web_name": "Existing", "team": 99,
        }],
    }
    rt = VerificationRuntime(
        fpl_data=future_fpl, database_path=tmp_path / "future.sqlite3"
    )
    obs = observation(
        title="Future Town sign New Player from Ajax",
        source_id="official.premier_league",
        url="https://www.premierleague.com/en/news/future-town-sign-new-player",
        story={
            "player": "New Player", "event": "transfer",
            "from_club": "Ajax", "to_club": "Future Town", "stage": 4,
        },
    )
    decision = rt.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.verified_facts["club_to_id"] == "club:future-town"
    rt.close()


def test_google_query_label_never_becomes_source_identity():
    registry = SourceRegistry.load()
    identity = registry.resolve(
        url="https://news.google.com/articles/example",
        source_hint="journalist.fabrizio_romano",
        transport="GOOGLE_NEWS",
        configured_direct_feed=False,
    )
    assert identity.verified is False
    assert identity.profile_id.startswith("unknown:")


def test_google_result_uses_actual_publisher_domain():
    registry = SourceRegistry.load()
    identity = registry.resolve(
        url="https://news.google.com/articles/example",
        publisher_url="https://www.bbc.co.uk/sport/football/example",
        source_hint="journalist.fabrizio_romano",
        transport="GOOGLE_NEWS",
    )
    assert identity.verified is True
    assert identity.profile_id == "media.bbc_sport"


def test_official_title_can_recover_new_player_after_nonperson_fragment(runtime):
    obs = observation(
        title="Chelsea sign France World Cup defender Maxence Lacroix from Crystal Palace",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/maxence-lacroix-signs",
        story={
            "player": "France World Cup", "event": "transfer",
            "from_club": "Crystal Palace", "to_club": "Chelsea", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.verified_facts["subject_name"] == "Maxence Lacroix"


def test_official_transfer_publishes_and_renders_only_verified_facts(runtime):
    """Official-confirmed-only transfer caption template.

    Per policy, the caption must always include both FROM and TO clubs and a
    fee line (an explicit official fee, or "Fee: undisclosed" when none was
    stated), capped at four lines with no URL/"Source:" line -- the official
    source citation is shown on the card image footer instead, since the
    account has no X Premium long-post allowance.
    """
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/chelsea-sign-danny-welbeck",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert "Danny Welbeck has joined Chelsea from Brighton" in decision.rendered_text
    assert "http" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text
    assert len(decision.rendered_text.splitlines()) <= 4
    assert "#TransferNews" in decision.rendered_text
    assert "#PremierLeague" in decision.rendered_text
    assert "TBC" not in decision.rendered_text
    assert "Contract" not in decision.rendered_text
    assert "Fee: undisclosed" in decision.rendered_text


def test_verified_card_contains_only_authorized_decision(runtime, tmp_path):
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/chelsea-sign-danny-welbeck",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    path = tmp_path / "verified.png"
    create_verified_card(decision, runtime.sources, path)
    assert path.exists() and path.stat().st_size > 1000


def test_media_rumour_stays_pending_even_from_major_outlet(runtime):
    obs = observation(
        title="Chelsea interested in signing Danny Welbeck from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/example",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish
    assert decision.gate("official_confirmation").state.value == "WAIT"


def test_medical_transfer_never_publishes_even_from_two_elite_outlets(runtime):
    """NON-NEGOTIABLE: a medical/deal-agreed transfer must NEVER publish.

    Previously a "fast path" let two milestone-configured outlets publish a
    booked medical as a MEDICAL-worded post. The bot's mandate now is
    official-confirmed-completed-transfers only, so this must stay blocked
    regardless of how many non-official outlets agree.
    """
    sky = observation(
        title="Chelsea agree fee with Brighton; Danny Welbeck given permission to undergo medical",
        source_id="media.sky_sports",
        url="https://www.skysports.com/football/news/welbeck-chelsea-medical",
        story=transfer_story(),
    )
    bbc = observation(
        title="Danny Welbeck set for a medical at Chelsea after fee agreed with Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-medical",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([sky, bbc])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_single_elite_source_cannot_publish_a_medical(runtime):
    """One outlet is not enough, however reliable it is.

    Sky alone used to clear this path. A single report of an unannounced
    move is exactly the claim that gets retracted, so it now waits for a
    second independent publisher like every other non-official route.
    """
    obs = observation(
        title="Chelsea agree fee with Brighton; Danny Welbeck given permission to undergo medical",
        source_id="media.sky_sports",
        url="https://www.skysports.com/football/news/welbeck-chelsea-medical",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_reliable_but_unconfigured_outlets_cannot_publish_a_medical(runtime):
    """Reliability alone does not make a source elite.

    Nearly every configured publisher clears source_reliability_min, so if
    the path keyed off that score it would be open to secondary outlets.
    Qualification comes from allowed_milestones, which these do not have.
    """
    guardian = observation(
        title="Danny Welbeck set for a medical at Chelsea after fee agreed with Brighton",
        source_id="media.the_guardian",
        url="https://www.theguardian.com/football/welbeck-medical",
        story=transfer_story(),
    )
    espn = observation(
        title="Chelsea agree fee with Brighton; Danny Welbeck given permission to undergo medical",
        source_id="media.espn",
        url="https://www.espn.com/soccer/story/welbeck-medical",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([guardian, espn])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_two_major_media_sources_cannot_manufacture_official_confirmation(runtime):
    bbc = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck",
        story=transfer_story(),
    )
    sky = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="media.sky_sports",
        url="https://www.skysports.com/football/news/welbeck",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([bbc, sky])
    assert decision.decision == DecisionType.PENDING
    assert decision.gate("official_confirmation").state.value == "WAIT"


def _romano_here_we_go():
    obs = observation(
        title="Danny Welbeck to Chelsea, here we go; agreement completed with Brighton",
        source_id="journalist.fabrizio_romano",
        url="https://x.com/FabrizioRomano/status/123",
        story=transfer_story(),
        transport="X",
    )
    obs["document"]["source_handle"] = "FabrizioRomano"
    obs["document"]["configured_direct_feed"] = False
    return obs


def test_here_we_go_needs_a_second_publisher(runtime):
    """Even the milestone's own source cannot publish it alone.

    HERE_WE_GO goes through the dedicated allow_here_we_go path, which wants
    a source configured for the milestone AND minimum_here_we_go_publishers
    independent claims at AGREEMENT or above. It previously leaked through
    the medical fast path on a single claim with neither requirement.
    """
    decision = runtime.verify_observations([_romano_here_we_go()])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_here_we_go_never_publishes_even_with_a_second_publisher(runtime):
    """NON-NEGOTIABLE: "here we go" is a journalist milestone, never official.

    A second independent publisher used to be enough to publish a HERE_WE_GO
    post. Under the official-confirmed-only mandate this must stay blocked no
    matter how many non-official outlets corroborate it.
    """
    corroboration = observation(
        title="Danny Welbeck to join Chelsea from Brighton after fee agreed",
        source_id="media.the_athletic",
        url="https://www.nytimes.com/athletic/welbeck-here-we-go",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([_romano_here_we_go(), corroboration])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_ben_duckett_cricket_failure_is_rejected_generically(runtime):
    text = (
        "Rockets stay top of Hundred table as Duckett stars. Another fine "
        "half-century from Ben Duckett and crucial late wickets helped Trent "
        "Rockets stay top with a win over SunRisers Leeds."
    )
    obs = observation(
        title="Rockets stay top of Hundred table as Duckett stars",
        source_id="media.sky_sports",
        url="https://www.skysports.com/cricket/news/example",
        story={
            "player": "Ben Duckett", "event": "stay", "to_key": "Leeds",
            "to_club": "Leeds", "stage": 1,
        },
        declared_sport=None,
    )
    obs["document"]["summary"] = text
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT
    assert not decision.may_publish
    assert decision.gate("entity_validation").state.value == "FAIL"
    assert decision.gate("sport_validation").state.value == "FAIL"


def test_cross_sport_injury_is_rejected_without_blacklist(runtime):
    obs = observation(
        title="Jack Draper ruled out with an arm injury",
        source_id="media.sky_sports",
        url="https://www.skysports.com/tennis/news/example",
        story={"player": "Jack Draper", "event": "injury", "stage": 3},
        declared_sport=None,
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT
    assert decision.gate("sport_validation").state.value == "FAIL"
    assert decision.gate("league_validation").state.value == "FAIL"


def test_official_structured_fpl_injury_publishes(runtime):
    text = "Danny Welbeck: Hamstring injury - Expected back 15 August"
    obs = observation(
        title=text,
        source_id="official.fpl",
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        story={
            "player": "Danny Welbeck", "event": "injury", "from_key": "Brighton",
            "from_club": "Brighton", "diagnosis": "Hamstring injury - Expected back 15 August",
            "stage": 3,
        },
        transport="DIRECT_API",
        structured=True,
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert "OFFICIAL INJURY UPDATE" in decision.rendered_text
    assert len(decision.rendered_text.splitlines()) <= 4
    assert "http" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text
    assert "#InjuryNews" in decision.rendered_text
    assert "#PremierLeague" in decision.rendered_text
    assert "Exact return date" not in decision.rendered_text


def test_new_official_injury_status_is_material_progression(runtime):
    first_text = "Danny Welbeck: Hamstring injury - ruled out"
    first_obs = observation(
        title=first_text,
        source_id="official.fpl",
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        story={
            "player": "Danny Welbeck", "event": "injury", "from_key": "Brighton",
            "from_club": "Brighton", "diagnosis": "Hamstring injury - ruled out", "stage": 3,
        },
        transport="DIRECT_API", structured=True, published_at=now_iso(hours=10),
    )
    first = runtime.verify_observations([first_obs])
    assert first.may_publish
    runtime.repository.mark_published(first)

    second_text = "Injury update: Danny Welbeck has returned to full training"
    second_obs = observation(
        title=second_text,
        source_id="official.fpl",
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        story={
            "player": "Danny Welbeck", "event": "injury", "from_key": "Brighton",
            "from_club": "Brighton", "diagnosis": "Returned to full training", "stage": 1,
        },
        transport="DIRECT_API", structured=True,
    )
    second = runtime.verify_observations([second_obs])
    assert second.decision == DecisionType.PUBLISH, second.reasons
    assert second.verified_facts["injury_status"] == "Returned to full training"


def test_official_contract_extension_is_rejected_out_of_scope(runtime):
    # Strict policy: only TRANSFER / INJURY / SUSPENSION may publish. Even an
    # official club confirmation of a contract extension must be rejected.
    obs = observation(
        title="Brighton confirm Danny Welbeck has signed a new contract",
        source_id="club.brighton-and-hove-albion",
        url="https://www.brightonandhovealbion.com/pages/en/media-article/welbeck-new-contract",
        story={
            "player": "Danny Welbeck", "event": "renewal", "to_key": "Brighton",
            "to_club": "Brighton", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT, decision.reasons
    assert not decision.may_publish


def test_official_club_statement_is_rejected_out_of_scope(runtime):
    # Strict policy: club statements are not a publishable category.
    obs = observation(
        title="Official club statement: stadium redevelopment update",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/official-club-statement",
        story={"player": None, "event": "official_statement", "stage": 4},
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT, decision.reasons
    assert not decision.may_publish


def test_governing_body_suspension_publishes(runtime):
    obs = observation(
        title="Danny Welbeck has been suspended for three matches",
        source_id="official.fa",
        url="https://www.thefa.com/news/disciplinary/danny-welbeck-suspension",
        story={
            "player": "Danny Welbeck", "event": "suspension",
            "from_key": "Brighton", "from_club": "Brighton",
            "diagnosis": "Suspended for three matches", "stage": 3,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.event_type == EventType.SUSPENSION


def test_manager_appointment_is_rejected_out_of_scope(runtime):
    # Strict policy: manager changes are not a publishable category, even when
    # confirmed by the club and grounded on an official source.
    obs = observation(
        title="Chelsea appoint Marco Silva as head coach",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/marco-silva-appointed",
        story={
            "player": "Marco Silva", "event": "manager", "to_key": "Chelsea",
            "to_club": "Chelsea", "staff_role": "head coach",
            "staff_action": "appointment", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT, decision.reasons
    assert not decision.may_publish


def test_non_premier_league_club_news_is_rejected(runtime):
    # Strict PL-only policy: a story involving no active Premier League club
    # and no PL player must fail the league-validation gate even when it is
    # well-grounded football news from a reliable source.
    obs = observation(
        title="PSG confirm Vitinha ruled out with hamstring injury",
        source_id="media.espn",
        url="https://www.espn.com/soccer/report?id=example",
        story={
            "player": "Vitinha", "event": "injury",
            "from_key": "PSG", "from_club": "PSG",
            "diagnosis": "Hamstring injury", "stage": 3,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT, decision.reasons
    assert not decision.may_publish
    assert decision.gate("league_validation").state.value == "FAIL"


def test_conflicting_official_destinations_hold_story(runtime):
    chelsea = observation(
        title="Chelsea sign Example Player from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/example-player-signs",
        story=transfer_story(player="Example Player", destination="Chelsea"),
    )
    arsenal = observation(
        title="Arsenal sign Example Player from Brighton",
        source_id="club.arsenal",
        url="https://www.arsenal.com/news/example-player-signs",
        story=transfer_story(player="Example Player", destination="Arsenal"),
    )
    decision = runtime.verify_observations([chelsea, arsenal])
    assert decision.decision == DecisionType.PENDING
    assert "club_to_id" in decision.gate("fact_consensus").reason


def test_duplicate_publication_is_not_reposted(runtime):
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/chelsea-sign-danny-welbeck",
        story=transfer_story(),
    )
    first = runtime.verify_observations([obs])
    assert first.may_publish
    runtime.repository.mark_published(first)
    second = runtime.verify_observations([obs])
    assert second.decision == DecisionType.DUPLICATE
    assert not second.may_publish


def test_status_only_upgrade_for_same_transfer_does_not_repost(runtime):
    first_obs = observation(
        title="Chelsea officially confirm Danny Welbeck signing from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-confirmed",
        story=transfer_story(),
    )
    first = runtime.verify_observations([first_obs])
    assert first.may_publish, first.reasons
    assert first.status.value == "OFFICIAL"
    runtime.repository.mark_published(first)

    second_obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="official.premier_league",
        url="https://www.premierleague.com/news/welbeck-signs",
        story=transfer_story(),
    )
    second = runtime.verify_observations([second_obs])
    assert second.decision == DecisionType.DUPLICATE
    assert not second.may_publish
    assert second.gate("story_progression").reason == "no new verified milestone or material fact"


def test_transfer_reposts_when_new_fee_and_contract_are_verified(runtime):
    first_obs = observation(
        title="Chelsea officially confirm Danny Welbeck signing from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-confirmed",
        story=transfer_story(),
    )
    first = runtime.verify_observations([first_obs])
    assert first.may_publish, first.reasons
    runtime.repository.mark_published(first)

    updated_story = transfer_story()
    updated_story["fee"] = "£20m"
    updated_story["contract"] = "five-year contract"
    second_obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton on a five-year contract for £20m",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-five-year-contract",
        story=updated_story,
    )
    second = runtime.verify_observations([second_obs])
    assert second.decision == DecisionType.PUBLISH, second.reasons
    assert second.verified_facts["fee"] == "£20m"
    assert second.verified_facts["contract_length"] == "five-year contract"
    assert "new verified material facts" in second.gate("story_progression").reason


def test_freshly_republished_historical_feature_is_not_new_event(runtime):
    obs = observation(
        title="On this day 10 years ago: Chelsea signed Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/on-this-day-welbeck",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.REJECT
    assert decision.gate("article_category").state.value == "FAIL"


def test_stale_official_confirmation_does_not_publish(runtime):
    # max_confirmation_age_hours is 72 (see config/verification.json) — a
    # confirmation just past that boundary should still be rejected as stale.
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/old-signing",
        story=transfer_story(),
        published_at=now_iso(hours=80),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert decision.gate("temporal_consistency").state.value == "WAIT"


def test_low_status_media_claim_does_not_block_later_official(runtime):
    media = observation(
        title="Talks continue for Example Player to join Chelsea from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/example-talks",
        story=transfer_story(player="Example Player", destination="Chelsea"),
    )
    first = runtime.verify_observations([media])
    assert first.decision == DecisionType.PENDING

    official = observation(
        title="Arsenal sign Example Player from Brighton",
        source_id="club.arsenal",
        url="https://www.arsenal.com/news/example-player-signs",
        story=transfer_story(player="Example Player", destination="Arsenal"),
    )
    second = runtime.verify_observations([official])
    assert second.decision == DecisionType.PUBLISH, second.reasons


def test_deal_agreed_transfer_never_publishes(runtime):
    """NON-NEGOTIABLE: "deal agreed" is not an official confirmation."""
    bbc = observation(
        title="Deal agreed for Danny Welbeck to join Chelsea from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-deal-agreed",
        story=transfer_story(),
    )
    athletic = observation(
        title="Danny Welbeck to join Chelsea from Brighton after fee agreed",
        source_id="media.the_athletic",
        url="https://www.nytimes.com/athletic/welbeck-fee-agreed",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([bbc, athletic])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_low_status_media_talks_remain_pending(runtime):
    media = observation(
        title="Talks continue for Danny Welbeck to join Chelsea from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-talks",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([media])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_structured_fotmob_completed_transfer_never_publishes(runtime):
    """NON-NEGOTIABLE: FotMob is a third-party table, never an official source.

    A structured FotMob "completed" row used to be treated as publication
    authority. It must now always stay pending, exactly like free-text FotMob
    coverage, until a genuine first-party official source confirms the move.
    """
    obs = observation(
        title="Danny Welbeck has joined Chelsea from Brighton. FotMob listed the transfer as completed.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story=transfer_story(),
    )
    obs["document"]["source_handle"] = "fotmob"
    obs["document"]["metadata"] = {"structured_fotmob_transfer": True}
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_fotmob_text_without_structured_table_flag_stays_pending(runtime):
    obs = observation(
        title="Danny Welbeck has joined Chelsea from Brighton. FotMob listed the transfer as completed.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story=transfer_story(),
    )
    obs["document"]["source_handle"] = "fotmob"
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish
