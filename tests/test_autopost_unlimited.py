from __future__ import annotations

from datetime import datetime, timezone
import json

import main


def test_check_daily_limit_zero_means_unlimited(monkeypatch):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monkeypatch.setattr(main, "DAILY_POST_LIMIT", 0)
    data = {"daily": {"date": today, "count": 999, "limit": 15}}

    assert main.check_daily_limit(data) is True
    assert data["daily"]["limit"] == 0
    assert data["daily"]["count"] == 999


def test_positive_cap_treats_zero_and_negative_as_unlimited():
    assert main._positive_cap(25) == 25
    assert main._positive_cap(0) is None
    assert main._positive_cap(-1) is None
    assert main._cap_label(0) == "unlimited"
    assert main._cap_label(-5) == "unlimited"
    assert main._cap_label(7) == "7"


def test_live_event_scope_is_transfer_injury_suspension_only():
    assert main._live_event_allowed({"event": "transfer"}) is True
    assert main._live_event_allowed({"event": "loan"}) is True
    assert main._live_event_allowed({"event": "injury"}) is True
    assert main._live_event_allowed({"event": "suspension"}) is True
    assert main._live_event_allowed({"event": "manager"}) is False
    assert main._live_event_allowed({"event": "renewal"}) is False
    assert main._live_event_allowed({"event": "official_statement"}) is False


def test_write_run_status_persists_summary(monkeypatch, tmp_path):
    path = tmp_path / "last_run_status.json"
    monkeypatch.setattr(main, "RUN_STATUS_FILE", path)

    main.write_run_status({"run_exit": "no_post", "auth_expired": True})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_exit"] == "no_post"
    assert payload["auth_expired"] is True
    assert "updated_at" in payload
