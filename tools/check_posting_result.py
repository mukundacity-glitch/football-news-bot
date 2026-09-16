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


def pipeline_summary(status: dict) -> list[str]:
    pipeline = status.get("pipeline") or {}
    if not pipeline:
        return []
    outcomes = pipeline.get("outcomes") or {}
    lines = [
        "", "News collection:",
        f"- Read {pipeline.get('items_read', 0)} items; verified {pipeline.get('groups_verified', 0)} story groups; {pipeline.get('ready_count', 0)} ready before posting limits.",
        f"- Already posted items: {outcomes.get('already_posted_item', 0)}; stale: {outcomes.get('stale_item', 0)}; undated: {outcomes.get('unknown_publication_time', 0)}.",
        f"- Official articles enriched: {outcomes.get('official_enriched', 0)}; unavailable: {outcomes.get('official_enrichment_unavailable', 0)}; evidence searches: {outcomes.get('cross_verified_groups', 0)} groups.",
    ]
    for key, label in (("decisions", "Verification outcomes"),
                       ("items_by_source", "Source intake"),
                       ("ready_authority_sources", "Authorities for ready stories")):
        counts = pipeline.get(key) or {}
        if counts:
            lines.append(f"- {label}: " + "; ".join(
                f"{name}={count}" for name, count in sorted(counts.items())
            ) + ".")
    return lines


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
    lines.extend(pipeline_summary(status))
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
