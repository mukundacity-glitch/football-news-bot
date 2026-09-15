"""Regression coverage for false success without changing news or rendering."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from twikit import Client
from twikit.errors import CouldNotTweet

import main
from src.verification import VerificationRuntime
from src.x_delivery import (
    UnconfirmedPostError, create_tweet_confirmed, creation_receipt, timeline_receipts,
)
from tools.check_posting_result import posting_failed


TWEET_ID = "2099618515955405199"


def _response(*, sparse_profile=False):
    result = {"__typename": "Tweet", "rest_id": TWEET_ID}
    if sparse_profile:
        result.update({
            "legacy": {"full_text": "Verified news"},
            "core": {"user_results": {"result": {
                "rest_id": "123",
                "legacy": {
                    "created_at": "Mon Sep 14 21:57:17 +0000 2026",
                    "name": "Test account", "screen_name": "test_account",
                    "profile_image_url_https": "https://example.com/avatar.png",
                    "location": "", "description": "",
                    "entities": {"description": {}},  # Twikit assumes urls exists.
                },
            }}},
        })
    return {"data": {"create_tweet": {"tweet_results": {"result": result}}}}


def _client(payload):
    client = Client("en-US")
    client.gql.create_tweet = AsyncMock(return_value=(payload, None))
    client.upload_media = AsyncMock(return_value="uploaded-image")
    return client


def test_native_twikit_profile_parse_failure_recovers_only_with_confirmed_id():
    client = _client(_response(sparse_profile=True))
    with pytest.raises(KeyError, match="urls"):
        asyncio.run(client.create_tweet(text="Verified news", media_ids=["image"]))
    native_create = client.gql.create_tweet
    receipt = asyncio.run(create_tweet_confirmed(
        client, text="Verified news", media_ids=["image"],
    ))
    assert receipt.tweet_id == TWEET_ID
    assert receipt.url == f"https://x.com/i/web/status/{TWEET_ID}"
    assert client.gql.create_tweet is native_create


def test_native_twikit_none_return_is_not_a_success():
    client = _client({"data": {"create_tweet": {"tweet_results": {}}}})
    assert asyncio.run(client.create_tweet(text="Verified news")) is None
    native_create = client.gql.create_tweet
    with pytest.raises(UnconfirmedPostError, match="no tweet ID"):
        asyncio.run(create_tweet_confirmed(client, text="Verified news", media_ids=[]))
    assert client.gql.create_tweet is native_create


def test_parse_error_before_receiving_a_response_never_proves_delivery():
    client = _client(_response())
    client.create_tweet = AsyncMock(side_effect=KeyError("urls"))
    with pytest.raises(UnconfirmedPostError):
        asyncio.run(create_tweet_confirmed(client, text="Verified news", media_ids=[]))
    client.gql.create_tweet.assert_not_awaited()


def test_x_error_response_is_never_converted_into_success():
    payload = _response()
    payload["errors"] = [{"code": 999, "message": "Could not create tweet"}]
    client = _client(payload)
    with pytest.raises(CouldNotTweet):
        asyncio.run(create_tweet_confirmed(client, text="Verified news", media_ids=[]))
    assert creation_receipt(payload) is None


@pytest.mark.parametrize("result", [
    None, {}, {"rest_id": ""}, {"rest_id": "not-a-tweet"},
    {"rest_id": "0"}, {"__typename": "TweetTombstone", "rest_id": TWEET_ID},
])
def test_missing_or_unavailable_tweet_has_no_receipt(result):
    assert creation_receipt({
        "data": {"create_tweet": {"tweet_results": {"result": result}}},
    }) is None


def test_visibility_wrapper_preserves_the_created_tweet_id():
    payload = _response()
    wrapped = payload["data"]["create_tweet"]["tweet_results"]
    wrapped["result"] = {"__typename": "TweetWithVisibilityResults", "tweet": wrapped["result"]}
    assert creation_receipt(payload).tweet_id == TWEET_ID


@pytest.fixture
def publication(monkeypatch, tmp_path):
    fpl = {
        "teams": [{"id": 1, "name": "Chelsea", "short_name": "CHE"},
                  {"id": 2, "name": "Brighton", "short_name": "BHA"}],
        "elements": [{"id": 10, "first_name": "Danny", "second_name": "Welbeck",
                      "web_name": "Welbeck", "team": 2}],
    }
    runtime = VerificationRuntime(fpl_data=fpl, database_path=tmp_path / "v2.sqlite3")
    decision = runtime.verify_observations([{
        "document": {
            "title": "Chelsea sign Danny Welbeck from Brighton",
            "summary": "Chelsea sign Danny Welbeck from Brighton",
            "source_url": "https://www.chelseafc.com/en/news/article/welbeck",
            "source_id": "club.chelsea", "source_hint": "club.chelsea",
            "transport": "DIRECT_RSS", "configured_direct_feed": True,
            "declared_sport": "football", "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "legacy_story": {"player": "Danny Welbeck", "event": "transfer",
                         "from_club": "Brighton", "to_club": "Chelsea", "stage": 4},
    }])
    assert decision.may_publish
    image = tmp_path / "existing-card.png"
    image.write_bytes(b"existing verified graphic" * 100)
    monkeypatch.setattr(main, "_VERIFICATION_RUNTIME", runtime)
    monkeypatch.setattr(main, "DRY_RUN", False)
    monkeypatch.setattr(main, "POSTED_FILE", tmp_path / "posted.json")
    monkeypatch.setattr(main, "resync_dedup_state_from_origin", lambda data: data)
    monkeypatch.setattr(main, "image_is_blank", lambda path: False)
    monkeypatch.setattr(main, "move_to_posted", lambda item: None)
    item = {
        "id": "verified-source-item", "key": "v2_" + decision.story_id,
        "player": "Danny Welbeck", "event": "transfer", "stage": 4,
        "sources": ["club.chelsea"], "draft_image": str(image),
        "_v2_verified": True, "_v2_decision": decision.to_dict(),
    }
    data = main.load_data()
    yield runtime, decision, item, data
    runtime.close()


def test_post_item_records_real_id_and_preserves_caption_and_graphic(publication):
    runtime, decision, item, data = publication
    client = _client(_response(sparse_profile=True))
    assert asyncio.run(main.post_item(client, item, data)) is True
    assert data["daily"]["count"] == 1
    assert data["stories"][item["key"]]["x_post_id"] == TWEET_ID
    saved = runtime.repository.conn.execute(
        "SELECT platform_post_id FROM publications WHERE fingerprint = ?", (decision.fingerprint,),
    ).fetchone()
    assert saved[0] == TWEET_ID
    client.upload_media.assert_awaited_once_with(item["draft_image"], media_type="image/png")
    assert client.gql.create_tweet.call_args.args[1] == runtime.renderer.render(decision)
    assert asyncio.run(main.post_item(client, item, data)) is False
    assert client.gql.create_tweet.await_count == 1


def test_post_item_never_marks_an_empty_response_published(publication):
    runtime, decision, item, data = publication
    client = _client({"data": {"create_tweet": {"tweet_results": {}}}})
    with pytest.raises(main.XBackoffError) as error:
        asyncio.run(main.post_item(client, item, data))
    assert error.value.kind == "unconfirmed"
    assert data["daily"]["count"] == 0
    assert item["id"] not in data["posted_ids"]
    assert item["key"] not in data["stories"]
    assert not runtime.repository.has_publication_fingerprint(decision.fingerprint)


def test_upload_parse_error_cannot_be_mistaken_for_a_created_tweet(publication):
    runtime, decision, item, data = publication
    client = _client(_response())
    client.upload_media = AsyncMock(side_effect=KeyError("urls"))
    with pytest.raises(KeyError, match="urls"):
        asyncio.run(main.post_item(client, item, data))
    client.gql.create_tweet.assert_not_awaited()
    assert data["daily"]["count"] == 0
    assert item["id"] not in data["posted_ids"]
    assert not runtime.repository.has_publication_fingerprint(decision.fingerprint)


@pytest.mark.parametrize("code, kind", [(187, "duplicate"), (226, "flagged"), (88, "rate_limited"), (32, "auth")])
def test_existing_x_duplicate_and_account_backoff_handling_is_preserved(publication, code, kind):
    runtime, decision, item, data = publication
    client = _client({"errors": [{"code": code, "message": "X rejected this request"}]})
    if kind == "duplicate":
        assert asyncio.run(main.post_item(client, item, data)) is False
        assert runtime.repository.has_publication_fingerprint(decision.fingerprint)
    else:
        with pytest.raises(main.XBackoffError) as error:
            asyncio.run(main.post_item(client, item, data))
        assert error.value.kind == kind
        assert not runtime.repository.has_publication_fingerprint(decision.fingerprint)
    assert data["daily"]["count"] == 0
    assert client.gql.create_tweet.await_count == 1


@pytest.mark.parametrize("failure, attempts", [("empty_creation", 1), ("upload_error", 2), (None, 1)])
def test_posting_run_never_consumes_failed_stories(publication, monkeypatch, tmp_path, failure, attempts):
    runtime, decision, item, data = publication
    item.update({"mode": "confirmed", "confidence_decision": main._conf.AUTO_POST})
    payload = ({"data": {"create_tweet": {"tweet_results": {}}}}
               if failure == "empty_creation" else _response(sparse_profile=True))
    client = _client(payload)
    if failure == "upload_error":
        client.upload_media = AsyncMock(side_effect=KeyError("urls"))
    monkeypatch.setattr(main, "_VERIFICATION_RUNTIME", None)
    monkeypatch.setattr(main, "VerificationRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(main, "init_club_data", lambda: None)
    monkeypatch.setattr(main, "fetch_fpl_data", lambda: {})
    monkeypatch.setattr(main, "load_data", lambda: data)
    monkeypatch.setattr(main, "scrape", AsyncMock(return_value=[item]))
    monkeypatch.setattr(main, "build_draft", AsyncMock(return_value=item))
    monkeypatch.setattr(main, "Client", lambda language: client)
    monkeypatch.setattr(main, "X_POST_AUTH_TOKEN", "test-only")
    monkeypatch.setattr(main, "X_POST_CT0_TOKEN", "test-only")
    monkeypatch.setattr(main, "ENABLE_AUTOPOST", True)
    rollout = runtime.config.rollout_config
    monkeypatch.setenv(rollout["live_environment_variable"], rollout["live_required_value"])
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    status_path = tmp_path / "run-status.json"
    monkeypatch.setattr(main, "RUN_STATUS_FILE", status_path)

    asyncio.run(main.main())

    status = json.loads(status_path.read_text())
    assert client.upload_media.await_count == attempts
    if failure:
        assert item["id"] not in data["posted_ids"]
        assert not runtime.repository.has_publication_fingerprint(decision.fingerprint)
        assert status["posted_count"] == 0
        assert posting_failed(status)
    else:
        assert status["posted_count"] == 1
        assert status["published_posts"] == [f"https://x.com/i/web/status/{TWEET_ID}"]
        assert not posting_failed(status)


def test_diagnostic_excludes_retweets_and_embedded_quotes():
    def tweet(id_, author, **extra):
        return {"__typename": "Tweet", "rest_id": id_,
                "legacy": {"user_id_str": author, "full_text": "text", **extra}}
    own = tweet("3", "123")
    own["quoted_status_result"] = {"result": tweet("2", "123")}
    retweet = tweet("4", "123", retweeted_status_result={"result": tweet("1", "999")})
    assert [row["tweet_id"] for row in timeline_receipts([own, retweet], "123")] == ["3"]


@pytest.mark.parametrize("status, failed", [
    ({"posted_count": 1, "run_exit": "posted"}, False),
    ({"no_post_reason": "no_v2_publishable_stories"}, False),
    ({"no_post_reason": "daily_limit_reached"}, False),
    ({"no_post_reason": "x_cooldown_active", "x_backoff": "cooldown"}, False),
    ({"auth_expired": True}, True),
    ({"x_backoff": "unconfirmed"}, True),
    ({"posted_count": 1, "posting_failures": [{"error_type": "KeyError"}]}, True),
    ({"no_post_reason": "no_post_succeeded"}, False),  # Drafts can be duplicate/dry-run blocked.
])
def test_delivery_check_distinguishes_quiet_runs_from_posting_failures(status, failed):
    assert posting_failed(status) is failed
