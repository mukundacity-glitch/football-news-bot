"""Confirm X delivery from the CreateTweet response, before parsing profiles."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class UnconfirmedPostError(RuntimeError):
    """The call has no confirmed tweet ID; do not mark it posted or retry blindly."""


@dataclass(frozen=True)
class PostReceipt:
    tweet_id: str

    @property
    def url(self) -> str:
        return f"https://x.com/i/web/status/{self.tweet_id}"


def creation_receipt(payload: Any) -> PostReceipt | None:
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    try:
        result = payload["data"]["create_tweet"]["tweet_results"]["result"]
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result["tweet"]
        if result.get("__typename") not in (None, "Tweet"):
            return None
        tweet_id = str(result.get("rest_id") or "")
    except (KeyError, TypeError, AttributeError):
        return None
    return PostReceipt(tweet_id) if tweet_id.isdigit() and int(tweet_id) > 0 else None


async def create_tweet_confirmed(client, *, text: str, media_ids: list) -> PostReceipt:
    """Keep Twikit's request/error handling, but tolerate optional profile fields.

    Twikit 2.3.3 can raise KeyError('urls') while constructing the returned User.
    Capture only the response to this client's creation call; the exception's
    name alone never proves that X accepted a post. Clients post sequentially.
    """
    response = None
    create = client.gql.create_tweet

    async def capture(*args, **kwargs):
        nonlocal response
        response, http_response = await create(*args, **kwargs)
        return response, http_response

    client.gql.create_tweet = capture
    try:
        try:
            await client.create_tweet(text=text, media_ids=media_ids)
        except (KeyError, TypeError, AttributeError) as exc:
            receipt = creation_receipt(response)
            if receipt is None:
                raise UnconfirmedPostError(
                    f"X delivery unconfirmed after {type(exc).__name__}; no tweet ID received"
                ) from exc
            return receipt
        receipt = creation_receipt(response)
        if receipt is None:
            raise UnconfirmedPostError("X delivery unconfirmed: no tweet ID received")
        return receipt
    finally:
        client.gql.create_tweet = create


def timeline_receipts(payload: Any, user_id: str) -> list[dict]:
    """Read only this user's own top-level tweets from a raw timeline response."""
    found = {}

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if value.get("__typename") == "Tweet":
                legacy = value.get("legacy") or {}
                tweet_id = str(value.get("rest_id") or "")
                if (tweet_id.isdigit() and str(legacy.get("user_id_str")) == user_id
                        and not legacy.get("retweeted_status_result")):
                    found[tweet_id] = {
                        "tweet_id": tweet_id,
                        "url": PostReceipt(tweet_id).url,
                        "created_at": legacy.get("created_at"),
                        "text": str(legacy.get("full_text") or ""),
                    }
                return  # Do not mistake embedded quoted tweets for timeline posts.
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(payload)
    return sorted(found.values(), key=lambda row: int(row["tweet_id"]), reverse=True)
