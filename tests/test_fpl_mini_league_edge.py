from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from src import fpl_mini_league_edge as edge


class EdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {"players": [{"id": 7, "now_cost": 156, "selected_by_percent": "73.8"}],
                         "sources": ["https://fantasy.premierleague.com/api/bootstrap-static/"]}
        self.post = {
            "publishable": True, "reason": "Supported", "headline": "SAVE THE CHIP SLOT",
            "decision": "Target GW7 if fit", "qualifier": "Unused Triple Captain only",
            "evidence": ["73.8% owned"], "player_ids": [7], "analysis": "Strategic inference",
            "caption": "Wait for fitness before committing. #FPL #FPLVortex #GW7 #FantasyPL #MiniLeague",
            "sources": self.snapshot["sources"],
            "claims": [{"player_id": 7, "metric": "selected_by_percent", "value": 73.8}],
        }

    def test_scheduled_slot_survives_delayed_execution(self) -> None:
        now = datetime(2026, 9, 30, 18, 15, tzinfo=timezone.utc)
        name, target = edge.slot_for(now, schedule="0 12 * * *")
        self.assertEqual(name, "lunch")
        self.assertEqual(target.hour, 12)

    def test_summer_and_winter_local_schedule(self) -> None:
        for date, utc_hour in [("2026-09-30", 12), ("2026-12-01", 13)]:
            now = datetime.fromisoformat(f"{date}T{utc_hour:02d}:03:00+00:00")
            name, target = edge.slot_for(now, schedule="0 8 * * *")
            self.assertEqual((name, target.hour), ("morning", 8))

    def test_wrong_or_stale_schedule_rejected(self) -> None:
        now = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
        for cron in ["0 8 * * *", "0 9 * * *"]:
            with self.assertRaises(ValueError):
                edge.slot_for(now, schedule=cron)

    def test_all_topic_windows(self) -> None:
        for day, topic in [(28, 3), (29, 4), (30, 5)]:
            target = datetime(2026, 9, day, 17, tzinfo=edge.TZ)
            self.assertEqual(edge.topic_for("morning", target), 1)
            self.assertEqual(edge.topic_for("lunch", target), 2)
            self.assertEqual(edge.topic_for("evening", target), topic)

    def test_valid_post(self) -> None:
        edge.validate_post(self.post, self.snapshot, set(self.post["sources"]))

    def test_bad_statistics_and_nonfinite_values_rejected(self) -> None:
        for value in [65.1, float("nan"), float("inf")]:
            post = copy.deepcopy(self.post)
            post["claims"][0]["value"] = value
            with self.assertRaises(ValueError):
                edge.validate_post(post, self.snapshot, set(post["sources"]))

    def test_unknown_player_source_and_missing_metric_rejected(self) -> None:
        for change in [lambda p: p.update(player_ids=[999]),
                       lambda p: p.update(sources=["https://invented.example/news"]),
                       lambda p: p["claims"][0].update(metric="shots", value=12),
                       lambda p: p["claims"][0].update(metric="minutes", value=90)]:
            post = copy.deepcopy(self.post)
            change(post)
            with self.assertRaises(ValueError):
                edge.validate_post(post, self.snapshot, set(self.post["sources"]))

    def test_caption_is_not_truncated_and_has_exact_hashtags(self) -> None:
        for caption in ["x" * 200 + " #FPL #FPLVortex #GW7 #FantasyPL #MiniLeague",
                        "#FPL #FPLVortex #GW7 #FantasyPL #MiniLeague trailing words",
                        "Text #FPL #FPLVortex #GW7 #FantasyPL #FPL"]:
            post = {**self.post, "caption": caption}
            with self.assertRaises(ValueError):
                edge.validate_post(post, self.snapshot, set(post["sources"]))

    def test_brand_overlay_is_exactly_1080_square(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logo = root / "logo.png"
            Image.new("RGBA", (1536, 768), (0, 200, 0, 255)).save(logo)
            buffer = io.BytesIO()
            Image.new("RGB", (1024, 1024), "white").save(buffer, format="PNG")
            output = root / "graphic.png"
            edge.brand_image(buffer.getvalue(), logo, output)
            with Image.open(output) as result:
                self.assertEqual(result.size, (1080, 1080))
                self.assertEqual(result.getpixel((160, 80)), (0, 200, 0))
                self.assertEqual(result.getpixel((0, 0)), (16, 19, 22))
                self.assertEqual(result.getpixel((500, 500)), (255, 255, 255))

    def test_response_refusal_or_incomplete_never_accepted(self) -> None:
        for payload in [{"status": "incomplete"}, {"status": "completed", "output": []}]:
            with self.assertRaises(RuntimeError):
                edge.response_text(payload)

    def test_source_extraction(self) -> None:
        payload = {"output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://club.example"}]}},
            {"type": "message", "content": [{"annotations": [
                {"type": "url_citation", "url": "https://league.example"}]}]},
        ]}
        self.assertEqual(edge.research_urls(payload), {"https://club.example", "https://league.example"})

    def exercise_delivery(self, *, fail_reservation: bool = False) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            logo = root / "logo.png"
            logo.touch()
            target = datetime(2026, 9, 30, 8, tzinfo=edge.TZ)
            operations = []
            def checkpoint(state_root: Path, edition: Path, message: str) -> None:
                status = json.loads((edition / "post.json").read_text())["status"]
                operations.append(status)
                if fail_reservation and status == "publishing":
                    raise RuntimeError("Git write rejected")
            def graphic(snapshot, post, destination):
                image = destination / "graphic.png"
                image.write_bytes(b"fixture")
                return image
            async def publish(image_path, caption):
                self.assertEqual(operations[-1], "publishing")
                operations.append("x_request")
                return "https://x.com/i/web/status/123"
            with patch.multiple(edge, ROOT=root, OUTPUT=root / "output", LOGO=logo), \
                 patch.object(edge, "prepare_state", return_value=state), \
                 patch.object(edge, "live_snapshot", return_value=self.snapshot), \
                 patch.object(edge, "create_post", return_value=(self.post, "Research")), \
                 patch.object(edge, "create_graphic", side_effect=graphic), \
                 patch.object(edge, "checkpoint", side_effect=checkpoint), \
                 patch.object(edge, "publish", side_effect=publish) as publisher, \
                 patch.dict(os.environ, {"OPENAI_API_KEY": "test", "X_POST_AUTH_TOKEN": "test",
                                         "X_POST_CT0_TOKEN": "test"}):
                if fail_reservation:
                    with self.assertRaises(RuntimeError):
                        edge.run("morning", target, dry_run=False, sync_state=True)
                    publisher.assert_not_called()
                else:
                    self.assertEqual(edge.run("morning", target, dry_run=False, sync_state=True), 0)
                    self.assertEqual(operations, ["ready", "publishing", "x_request", "published"])
                    edge.run("morning", target, dry_run=False, sync_state=True)
                    self.assertEqual(publisher.call_count, 1)

    def test_reservation_before_x_and_retries_do_not_duplicate(self) -> None:
        self.exercise_delivery()

    def test_failed_durable_reservation_prevents_x_request(self) -> None:
        self.exercise_delivery(fail_reservation=True)

    def test_ambiguous_states_never_repeat_x_request(self) -> None:
        for status in ["published", "publishing", "delivery_uncertain"]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = root / "fpl-edge-posts/2026-09-30/lunch/post.json"
                edge.save_json(record, {"status": status})
                with patch.multiple(edge, ROOT=root, OUTPUT=root / "output"), \
                     patch.object(edge, "publish", new_callable=AsyncMock) as publisher:
                    edge.run("lunch", datetime(2026, 9, 30, 12, tzinfo=edge.TZ),
                             dry_run=True, sync_state=False)
                    publisher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
