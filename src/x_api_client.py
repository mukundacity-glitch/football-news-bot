"""Official X API v2 publishing client.

Live publication uses documented X API endpoints only:
- POST /2/media/upload
- POST /2/tweets

The existing Twikit client may still be used for read-only discovery/corroboration,
but this class never falls back to browser-session posting.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Iterable

import requests


MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
CREATE_POST_URL = "https://api.x.com/2/tweets"


class XApiError(RuntimeError):
    """An X API HTTP/response error safe to surface in workflow logs."""

    def __init__(self, status_code: int, endpoint: str, payload: Any):
        self.status_code = int(status_code)
        self.endpoint = endpoint
        self.payload = payload
        code = None
        title = None
        detail = None
        if isinstance(payload, dict):
            code = payload.get("code")
            title = payload.get("title")
            detail = payload.get("detail") or payload.get("message")
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0] if isinstance(errors[0], dict) else {}
                code = code or first.get("code") or first.get("status")
                title = title or first.get("title")
                detail = detail or first.get("detail") or first.get("message")
        parts = [f"X API HTTP {self.status_code}", f"endpoint={endpoint}"]
        if code is not None:
            parts.append(f"code={code}")
        if title:
            parts.append(f"title={title}")
        if detail:
            parts.append(f"detail={str(detail)[:300]}")
        super().__init__("; ".join(parts))


class XApiClient:
    """Minimal async-compatible official X API posting transport."""

    def __init__(
        self,
        access_token: str,
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("X API user access token is required")
        self._access_token = token
        self._timeout = float(timeout)
        self._session = session or requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _response_payload(response: requests.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return {"detail": str(getattr(response, "text", ""))[:500]}

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            url,
            json=payload,
            headers={**self.headers, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        body = self._response_payload(response)
        if not 200 <= int(response.status_code) < 300:
            raise XApiError(response.status_code, url, body)
        if not isinstance(body, dict):
            raise XApiError(response.status_code, url, {"detail": "non-object JSON response"})
        return body

    async def upload_media(
        self,
        media_path: str | Path,
        media_type: str = "image/png",
    ) -> str:
        path = Path(media_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        if str(media_type).lower() not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
            raise ValueError(f"unsupported X image media type: {media_type}")
        raw = await asyncio.to_thread(path.read_bytes)
        encoded = base64.b64encode(raw).decode("ascii")
        body = await asyncio.to_thread(
            self._post_json,
            MEDIA_UPLOAD_URL,
            {
                "media": encoded,
                "media_category": "tweet_image",
            },
        )
        data = body.get("data") if isinstance(body, dict) else None
        media_id = data.get("id") if isinstance(data, dict) else None
        if not media_id:
            raise XApiError(200, MEDIA_UPLOAD_URL, {"detail": "media upload response missing data.id"})
        return str(media_id)

    async def create_tweet(
        self,
        *,
        text: str,
        media_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": str(text)}
        ids = [str(value) for value in (media_ids or []) if str(value)]
        if ids:
            payload["media"] = {"media_ids": ids}
        body = await asyncio.to_thread(self._post_json, CREATE_POST_URL, payload)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("id"):
            raise XApiError(201, CREATE_POST_URL, {"detail": "create-post response missing data.id"})
        return data
