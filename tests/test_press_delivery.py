"""The separate press owner must distinguish delivery from a quiet/dry run."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools import press_publish as press


@pytest.mark.parametrize("result,ledger,dry_run,expected,locked", [
    (True, True, False, 0, True),
    (False, True, False, 0, True),  # X duplicate receipt, no new post.
    (False, False, False, 1, False),
    (False, False, True, 0, False),
    (KeyError("private-response"), False, False, 1, False),
])
def test_press_delivery_outcome_is_honest_and_retryable(
    monkeypatch, tmp_path, capsys, result, ledger, dry_run, expected, locked,
):
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(minutes=45)
    decision = SimpleNamespace(may_publish=True, fingerprint="press-fingerprint",
                               source_url="https://www.premierleague.com/en/news/test",
                               verified_facts={"roundup": ["A verified manager update"]})
    repository = Mock()
    repository.has_publication_fingerprint.side_effect = [False, ledger]
    runtime = SimpleNamespace(
        live_enabled=True, database_was_empty=False, repository=repository,
        config=SimpleNamespace(rollout_config={
            "database_rebuild_environment_variable": "TEST_PRESS_REBUILD",
            "database_rebuild_required_value": "ACK",
        }), sources=Mock(), verify_observations=Mock(return_value=decision), close=Mock(),
    )
    client = SimpleNamespace(set_cookies=Mock(), http=SimpleNamespace(aclose=AsyncMock()))
    status_path, lock_path = tmp_path / "run.json", tmp_path / "lock.json"
    collection = tmp_path / "collection.json"
    collection.write_text(json.dumps({"event_id": "5", "items": [{"id": "article"}]}))
    monkeypatch.setattr(press, "COLLECTION_PATH", collection)
    monkeypatch.setattr(press, "RUN_STATUS_PATH", status_path)
    monkeypatch.setattr(press, "LOCK_PATH", lock_path)
    monkeypatch.setattr(press, "utcnow", lambda: now)
    monkeypatch.setattr(press, "fetch_fpl_bootstrap", lambda: {
        "events": [{"id": 5, "name": "Gameweek 5", "deadline_time": deadline.isoformat()}],
    })
    monkeypatch.setattr(press, "VerificationRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(press, "_candidate", lambda raw, rt: (1, now.isoformat(), {}, {}))
    monkeypatch.setattr(press, "validate_official_press_conference", lambda *args: SimpleNamespace(ok=True))
    monkeypatch.setattr(press.bot, "_VERIFICATION_RUNTIME", None)
    monkeypatch.setattr(press.bot, "_v2_project_verified_facts", lambda *args: None)
    monkeypatch.setattr(press.bot, "load_data", lambda: {})
    monkeypatch.setattr(press.bot, "save_data", Mock())
    monkeypatch.setattr(press.bot, "ENABLE_AUTOPOST", True)
    monkeypatch.setattr(press.bot, "DRY_RUN", dry_run)
    monkeypatch.setattr(press.bot, "in_cooldown", lambda data: False)
    monkeypatch.setattr(press.bot, "check_daily_limit", lambda data: True)
    monkeypatch.setattr(press.bot, "_recent_post_count", lambda *args: 0)
    monkeypatch.setattr(press.bot, "build_draft", AsyncMock(return_value={}))
    monkeypatch.setattr(press.bot, "Client", lambda language: client)
    monkeypatch.setattr(press.bot, "X_POST_AUTH_TOKEN", "test-only")
    monkeypatch.setattr(press.bot, "X_POST_CT0_TOKEN", "test-only")
    monkeypatch.setattr(press.bot, "POST_JITTER_RANGE_S", (0, 0))
    post = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(press.bot, "post_item", post)

    assert asyncio.run(press.run()) == expected
    status = json.loads(status_path.read_text())
    assert status["posted_count"] == int(result is True)
    assert lock_path.exists() is locked
    assert bool(status.get("posting_failures")) == (expected == 1)
    assert "private-response" not in capsys.readouterr().out
    client.http.aclose.assert_awaited_once()
    runtime.close.assert_called_once()
    post.assert_awaited_once()
