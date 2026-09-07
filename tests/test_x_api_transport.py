from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.x_api_client import (
    CREATE_POST_URL,
    MEDIA_UPLOAD_URL,
    XApiClient,
    XApiError,
)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json, headers, timeout):
        self.calls.append({
            "url": url,
            "json": json,
            "headers": headers,
            "timeout": timeout,
        })
        return self.responses.pop(0)


def test_official_x_api_upload_then_create_post(tmp_path: Path):
    image = tmp_path / "card.png"
    image.write_bytes(b"not-a-real-png-but-enough-for-transport-test")
    session = FakeSession([
        FakeResponse(200, {"data": {"id": "12345", "media_key": "3_12345"}}),
        FakeResponse(201, {"data": {"id": "98765", "text": "hello"}}),
    ])
    client = XApiClient("user-oauth-token", session=session)

    media_id = asyncio.run(client.upload_media(image))
    created = asyncio.run(client.create_tweet(text="hello", media_ids=[media_id]))

    assert media_id == "12345"
    assert created["id"] == "98765"
    assert session.calls[0]["url"] == MEDIA_UPLOAD_URL
    assert session.calls[0]["json"]["media_category"] == "tweet_image"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer user-oauth-token"
    assert session.calls[1]["url"] == CREATE_POST_URL
    assert session.calls[1]["json"] == {
        "text": "hello",
        "media": {"media_ids": ["12345"]},
    }


def test_official_x_api_error_is_actionable_without_exposing_token():
    session = FakeSession([
        FakeResponse(
            401,
            {
                "title": "Unauthorized",
                "detail": "Invalid or expired token",
                "status": 401,
            },
        ),
    ])
    client = XApiClient("super-secret-token", session=session)

    with pytest.raises(XApiError) as raised:
        asyncio.run(client.create_tweet(text="hello"))

    message = str(raised.value)
    assert "HTTP 401" in message
    assert "Unauthorized" in message
    assert "super-secret-token" not in message


def test_live_workflows_use_only_official_api_for_posting():
    bot_workflow = Path(".github/workflows/bot.yml").read_text(encoding="utf-8")
    press_workflow = Path(".github/workflows/fpl-deadline-news.yml").read_text(
        encoding="utf-8"
    )
    main_code = Path("main.py").read_text(encoding="utf-8")
    press_code = Path("tools/press_publish.py").read_text(encoding="utf-8")

    for workflow in (bot_workflow, press_workflow):
        assert "X_API_ACCESS_TOKEN" in workflow
        assert "X_POST_AUTH_TOKEN" not in workflow
        assert "X_POST_CT0_TOKEN" not in workflow

    assert "post_client = XApiClient(X_API_ACCESS_TOKEN)" in main_code
    assert "client = bot.XApiClient(bot.X_API_ACCESS_TOKEN)" in press_code
    assert "post_client = Client(" not in main_code
    assert "client = bot.Client(" not in press_code
