"""Summarize delivery and fail the Actions check on an actual posting error."""
from __future__ import annotations

import json
import os
from pathlib import Path


def posting_failed(status: dict) -> bool:
    return bool(
        status.get("auth_expired")
        or status.get("x_backoff") not in {None, "", "cooldown"}
        or status.get("posting_failures")
        or status.get("no_post_reason") in {
            "missing_posting_credentials", "posting_client_init_failed",
        }
    )


def main() -> int:
    status_path = Path("data/last_run_status.json")
    if not status_path.exists():
        print("::error::The bot did not write its run status.")
        return 1
    status = json.loads(status_path.read_text(encoding="utf-8"))
    lines = [
        f"Confirmed X posts this run: **{status.get('posted_count', 0)}**",
        f"Run result: `{status.get('no_post_reason') or status.get('run_exit', 'unknown')}`",
        "",
    ]
    lines.extend(f"- [View confirmed post]({url})" for url in status.get("published_posts", []))
    if status.get("posting_failures"):
        lines.append(f"Posting errors: {len(status['posting_failures'])}")
    summary = "\n".join(lines) + "\n"
    print(summary)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    if posting_failed(status):
        print("::error::X delivery failed or was not confirmed. See the bot log and run status.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
