"""End-to-end tests for the fail-closed verification engine v2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.verification import DecisionType, VerificationRuntime
from src.verification.ingestion import _fotmob_legacy_story, _fotmob_transfer_text
from src.verification.models import EventType, GateState
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
            {"id": 6, "name": "Liverpool", "short_name": "LIV"},
            {"id": 7, "name": "Man City", "short_name": "MCI"},
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


# -- dynamic-registry recovery must not substitute an unrelated player -----
# Real, reproduced incident: the shared fpl_data fixture below has a player
# whose web_name is the bare word "Player" (id 11, "Example Player") -- a
# stand-in for any real player whose FPL web_name happens to be short or
# generic. Before this fix, ANY claimed subject_name containing that word
# as a token (a completely fabricated name, unrelated to this player) would
# fail to resolve via resolve_player, fall through to the single-mention
# "recovery" fallback, find exactly one known-registry player mentioned in
# the text (this one, via its own alias), and silently substitute it in as
# the subject -- with entity_validation then PASSING and the claim reaching
# transfer_publication_safety=PASS. The fix requires the recovered player's
# name/aliases to share a real word-token with the CLAIMED subject_name, not
# merely "this is the only known player mentioned anywhere in the document."

def test_dynamic_registry_recovery_rejects_unrelated_player(runtime):
    obs = observation(
        title="Marcus Delgado signs for Chelsea",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/marcus-delgado-signs",
        story={
            "player": "Marcus Delgado", "event": "transfer",
            "from_club": "Brighton", "to_club": "Chelsea", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.verified_facts.get("subject_id") is None
    assert decision.verified_facts.get("subject_name") is None
    assert decision.decision != DecisionType.PUBLISH


def test_dynamic_registry_recovery_still_finds_garbled_real_name(runtime):
    # "Welbeck" alone is a genuine partial/garbled read of the real player
    # (id 10, "Danny Welbeck") already in the shared fixture -- this must
    # still recover, confirming the fix narrows the fallback rather than
    # disabling the legitimate case it exists for.
    obs = observation(
        title="Chelsea sign Welbeck from Brighton in club-record deal",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-signs",
        story={
            "player": "Welbeck", "event": "transfer",
            "from_club": "Brighton", "to_club": "Chelsea", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.verified_facts.get("subject_name") == "Danny Welbeck"


def test_official_transfer_publishes_and_renders_only_verified_facts(runtime):
    """Official transfers use the owner-approved master caption template."""
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/chelsea-sign-danny-welbeck",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert "🚨 REPORTED TRANSFER: Danny Welbeck" in decision.rendered_text
    assert "Brighton → Chelsea" in decision.rendered_text
    assert "STATUS: OFFICIAL" in decision.rendered_text
    assert "#TransferNews #Chelsea #DannyWelbeck #fpl" in decision.rendered_text
    assert "http" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text


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


def test_medical_transfer_publishes_as_reported_from_two_elite_outlets(runtime):
    """Two independent approved outlets may authorize a REPORTED medical.

    It must remain separate from first-party confirmation and must never use
    CONFIRMED wording.
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
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.authority_kind == "tier_one_reported_transfer"
    assert "REPORTED" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text


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


def test_here_we_go_publishes_as_reported_with_a_second_publisher(runtime):
    """HERE_WE_GO needs two approved independent sources and stays REPORTED."""
    corroboration = observation(
        title="Danny Welbeck to join Chelsea from Brighton after fee agreed",
        source_id="media.the_athletic",
        url="https://www.nytimes.com/athletic/welbeck-here-we-go",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([_romano_here_we_go(), corroboration])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.authority_kind == "tier_one_reported_transfer"
    assert set(decision.authority_source_ids) == {
        "journalist.fabrizio_romano", "media.the_athletic"
    }
    assert "REPORTED" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text


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
    assert "🚑 INJURY UPDATE: Danny Welbeck" in decision.rendered_text
    assert "INJURY: Hamstring injury - Expected back 15 August" in decision.rendered_text
    assert "STATUS: RETURNING" in decision.rendered_text
    assert "#FPL #FPLNews #Brighton #Injury" in decision.rendered_text
    assert "http" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text


def test_official_match_reaction_cannot_be_misread_as_completed_transfer(runtime):
    """Regression for an official match interview parsed as a player move.

    Noun "signs", a team's on-pitch "move", and "complete a late turnaround"
    must never combine into a transfer confirmation, even on an official club
    domain and even when the noisy legacy parser supplies a transfer hint.
    """
    text = (
        "Danny Welbeck: We can take positives from the City performance. "
        "Danny Welbeck believes Brighton showed encouraging signs of the work "
        "being done against Manchester City. City then struck in stoppage time "
        "to complete a late turnaround. Welbeck said the move demonstrated the "
        "style Brighton have developed."
    )
    obs = observation(
        title="Welbeck: We can take positives from City performance",
        source_id="club.brighton-and-hove-albion",
        url="https://www.brightonandhovealbion.com/pages/en/media-article/welbeck-reacts-city-defeat",
        story={
            "player": "Danny Welbeck",
            "event": "transfer",
            "from_club": "Brighton",
            "from_key": "Brighton",
            "to_club": "Man City",
            "to_key": "Man_City",
            "stage": 4,
            "raw_text": text,
        },
    )
    obs["document"]["summary"] = text
    decision = runtime.verify_observations([obs])

    assert decision.decision != DecisionType.PUBLISH
    category = decision.gate("article_category")
    assert category is not None
    assert category.state == GateState.FAIL
    safety = decision.gate("transfer_publication_safety")
    assert safety is not None
    assert safety.state == GateState.FAIL


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


def test_deal_agreed_transfer_publishes_as_reported_with_two_sources(runtime):
    """Two approved publishers may report an agreement, never confirm it."""
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
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.authority_kind == "tier_one_reported_transfer"
    assert "REPORTED" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text


def test_one_approved_source_can_publish_talks_as_reported(runtime):
    media = observation(
        title="Chelsea are in talks with Brighton over the transfer of Danny Welbeck",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-talks",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([media])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.status.value == "TALKS"
    assert decision.authority_source_ids == ["media.bbc_sport"]
    assert "REPORTED TRANSFER" in decision.rendered_text
    assert "Source:" not in decision.rendered_text
    assert "http" not in decision.rendered_text


def test_player_terms_wording_is_agreement_and_requires_two_sources(runtime):
    romano = observation(
        title=("Chelsea keep talks active with Brighton for Danny Welbeck; "
               "there is agreement on player terms"),
        source_id="journalist.fabrizio_romano",
        url="https://x.com/FabrizioRomano/status/998",
        story=transfer_story(),
        transport="X",
    )
    romano["document"]["source_handle"] = "FabrizioRomano"
    romano["document"]["configured_direct_feed"] = False
    one = runtime.verify_observations([romano])
    assert one.status.value == "AGREEMENT"
    assert one.decision == DecisionType.PENDING
    assert not one.may_publish

    bbc = observation(
        title="Danny Welbeck to Chelsea from Brighton with player terms agreed",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-player-terms",
        story=transfer_story(),
    )
    two = runtime.verify_observations([romano, bbc])
    assert two.decision == DecisionType.PUBLISH, two.reasons
    assert two.may_publish
    assert two.verified_facts["reported_status_detail"] == "PLAYER TERMS AGREED"
    assert "STATUS: REPORTED" in two.rendered_text
    assert "Brighton → Chelsea" in two.rendered_text
    assert "CONFIRMED" not in two.rendered_text


def test_incoming_non_fpl_player_can_publish_only_through_approved_report_lane(runtime):
    story = {
        "player": "Bradley Barcola",
        "event": "transfer",
        "from_key": "Paris Saint-Germain",
        "from_club": "Paris Saint-Germain",
        "to_key": "Liverpool",
        "to_club": "Liverpool",
        "stage": 1,
    }
    romano = observation(
        title="Liverpool are in talks with Paris Saint-Germain over the transfer of Bradley Barcola",
        source_id="journalist.fabrizio_romano",
        url="https://x.com/FabrizioRomano/status/999",
        story=story,
        transport="X",
    )
    romano["document"]["source_handle"] = "FabrizioRomano"
    romano["document"]["configured_direct_feed"] = False
    decision = runtime.verify_observations([romano])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.verified_facts["subject_name"] == "Bradley Barcola"
    assert decision.authority_kind == "tier_one_reported_transfer"
    assert "REPORTED" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text


def test_unapproved_media_cannot_publish_reported_talks(runtime):
    media = observation(
        title="Chelsea are in talks with Brighton over the transfer of Danny Welbeck",
        source_id="media.the_guardian",
        url="https://www.theguardian.com/football/welbeck-talks",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([media])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_interest_is_not_reportable_even_from_approved_source(runtime):
    media = observation(
        title="Chelsea are interested in a transfer for Danny Welbeck from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-interest",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([media])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_ornstein_and_athletic_count_as_one_newsroom(runtime):
    ornstein = observation(
        title="Agreement reached for Danny Welbeck to join Chelsea from Brighton",
        source_id="journalist.david_ornstein",
        url="https://x.com/David_Ornstein/status/123",
        story=transfer_story(),
        transport="X",
    )
    ornstein["document"]["source_handle"] = "David_Ornstein"
    athletic = observation(
        title="Danny Welbeck to join Chelsea from Brighton after fee agreed",
        source_id="media.the_athletic",
        url="https://www.nytimes.com/athletic/welbeck-fee-agreed",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([ornstein, athletic])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_structured_fotmob_completed_transfer_publishes_as_reported(runtime):
    """A real structured table row may publish, but never as CONFIRMED."""
    obs = observation(
        title="Danny Welbeck has joined Chelsea from Brighton. FotMob listed the transfer as completed.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story=transfer_story(),
    )
    obs["document"]["source_handle"] = "fotmob"
    obs["document"]["metadata"] = {
        "structured_fotmob_transfer": True,
        "fotmob_row": {"playerId": 10},
    }
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.authority_kind == "structured_fotmob_reported_transfer"
    assert decision.authority_source_ids == ["media.fotmob"]
    assert decision.verified_facts["structured_source"] == "fotmob_transfer_table"
    assert "REPORTED TRANSFER" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text
    assert "http" not in decision.rendered_text


def test_structured_fotmob_lane_uses_its_configured_prior_not_free_text_history(runtime, monkeypatch):
    obs = observation(
        title="Danny Welbeck has joined Chelsea from Brighton. FotMob listed the transfer as completed.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story=transfer_story(),
    )
    obs["document"]["source_handle"] = "fotmob"
    obs["document"]["metadata"] = {
        "structured_fotmob_transfer": True,
        "fotmob_row": {"playerId": 10},
    }
    monkeypatch.setattr(
        runtime.engine.reliability, "evaluate",
        lambda source_id: SimpleNamespace(score=0.1),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.gate("source_reliability").value == pytest.approx(0.9)


def test_real_fotmob_row_preserves_own_format_and_incoming_player_identity(runtime):
    row = {
        "name": "Gerónimo Rulli", "playerId": 245555,
        "position": {"label": "GK"},
        "transferDate": now_iso(),
        "fromClub": "Marseille", "fromClubFullName": "Marseille", "fromClubId": 8592,
        "toClub": "Man City", "toClubFullName": "Manchester City", "toClubId": 8456,
        "fee": {"feeText": "fee", "value": 2_000_000},
        "transferType": {"text": "contract"}, "onLoan": False,
        "toDate": "2028-06-30T00:00:00Z", "marketValue": 3_834_094,
    }
    text = _fotmob_transfer_text(row)
    story = _fotmob_legacy_story(row)
    assert story["fee"] == "€2m"
    assert story["contract"] == "Jun 2028"
    assert story["market_value"] == "€3.8m"
    obs = observation(
        title=text,
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story=story,
        transport="FOTMOB",
        published_at=row["transferDate"],
    )
    obs["document"]["source_handle"] = "fotmob"
    obs["document"]["configured_direct_feed"] = False
    obs["document"]["metadata"] = {
        "structured_fotmob_transfer": True, "fotmob_row": row
    }
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.verified_facts["subject_name"] == "Gerónimo Rulli"
    assert decision.verified_facts["club_from_name"] == "Marseille"
    assert decision.verified_facts["club_to_name"] == "Man City"
    assert decision.verified_facts["fee"] == "€2m"
    assert decision.verified_facts["contract_length"] == "Jun 2028"
    assert decision.verified_facts["market_value"] == "€3.8m"
    assert "CONFIRMED" not in decision.rendered_text
    assert "Source:" not in decision.rendered_text

def test_current_fotmob_route_ignores_stale_unpublished_destination_claim(runtime):
    stale_claim = observation(
        title="Deal agreed for Danny Welbeck to join Arsenal from Brighton",
        source_id="media.the_guardian",
        url="https://www.theguardian.com/football/welbeck-arsenal",
        story=transfer_story(destination="Arsenal"),
    )
    first = runtime.verify_observations([stale_claim])
    assert not first.may_publish

    current = observation(
        title="Danny Welbeck has joined Chelsea from Brighton. FotMob listed the transfer as completed.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story=transfer_story(destination="Chelsea"),
    )
    current["document"]["source_handle"] = "fotmob"
    current["document"]["metadata"] = {
        "structured_fotmob_transfer": True,
        "fotmob_row": {"playerId": 10},
    }
    decision = runtime.verify_observations([current])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.verified_facts["club_to_name"] == "Chelsea"


def test_structured_fotmob_row_older_than_48_hours_stays_pending(runtime):
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    obs = observation(
        title="Danny Welbeck has joined Chelsea from Brighton. FotMob listed the transfer as completed.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
        story={**transfer_story(), "event_time": old},
        published_at=old,
    )
    obs["document"]["source_handle"] = "fotmob"
    obs["document"]["metadata"] = {"structured_fotmob_transfer": True}
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish
    assert "FotMob listing is" in "; ".join(decision.reasons)


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


# -- unrelated unresolved-subject claims must never share a story family ---
# Real production incident, 13 Aug 2026: two claims about two entirely
# different, unrelated players (neither resolves a subject_id -- both are
# outside the FPL-known roster) arrived in separate run-cycles from two
# different official club feeds. _story_ids previously fell back to the
# CONSTANT string "unknown" whenever no subject resolved, so both claims
# hashed to the same family_id, and claims_for_family's plain family_id
# match then pooled them together on the second run. fact_consensus then
# reported a "conflict" between two clubs (e.g. Chelsea vs Fulham) that were
# never actually claims about the same transfer -- they just happened to
# share the unresolved-subject bucket. The fail-closed gate blocked
# publication either way, but the conflict reason was meaningless, and nearby
# unrelated stories piggybacked on it every subsequent run. Reproduced here:
# this exact scenario regenerates the identical story_id the real log showed
# (544060e49e943f51bc155175f85e75d9) against the pre-fix code.

def test_unrelated_unresolved_subjects_do_not_share_a_story_family(runtime):
    first = observation(
        title="Marcus Delgado Junior signs for Chelsea",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/marcus-delgado-junior-signs",
        story=transfer_story(player="Marcus Delgado Junior", destination="Chelsea"),
    )
    second = observation(
        title="Tobias Okonkwo Reyes signs for Fulham",
        source_id="club.fulham",
        url="https://www.fulhamfc.com/news/tobias-okonkwo-reyes-signs",
        story=transfer_story(player="Tobias Okonkwo Reyes", destination="Fulham"),
    )
    # Separate calls, matching how the real bot invokes verify_observations
    # once per run-cycle (~20 min apart) -- the collapse only manifests via
    # claims_for_family's historical lookup across calls, not within one.
    first_decision = runtime.verify_observations([first])
    second_decision = runtime.verify_observations([second])
    assert first_decision.story_id != second_decision.story_id
    fact_consensus_gate = second_decision.gate("fact_consensus")
    if fact_consensus_gate is not None:
        assert "club_to_id" not in fact_consensus_gate.reason
