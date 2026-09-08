"""Manual story-specific posting entrypoint.

This never bypasses V2 verification. It runs the normal discovery/verification
pipeline, then keeps only the exact verified story requested by the user.
"""
from __future__ import annotations

import argparse
import asyncio

import main as bot


def _target_key(story_id: str) -> str:
    value = str(story_id or "").strip()
    if not value:
        raise ValueError("story_id is required")
    return value if value.startswith("v2_") else f"v2_{value}"


async def run(story_id: str) -> int:
    target = _target_key(story_id)
    original_scrape = bot.scrape

    async def targeted_scrape(*args, **kwargs):
        ready = await original_scrape(*args, **kwargs)
        matched = [item for item in ready if str(item.get("key") or "") == target]
        if matched:
            print(f"[MANUAL] Exact story {target} re-verified and is eligible to post.")
        else:
            print(
                f"[MANUAL] Exact story {target} is not publishable now. "
                "Nothing will be posted; verification/dedup rules were not bypassed."
            )
        return matched

    bot.scrape = targeted_scrape
    try:
        result = await bot.main(post=True)
        return int(result or 0)
    finally:
        bot.scrape = original_scrape


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-id", required=True)
    args = parser.parse_args()
    return asyncio.run(run(args.story_id))


if __name__ == "__main__":
    raise SystemExit(main())
