"""Read-only check of the posting account and its recent public tweets."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import main as bot
from src.x_delivery import timeline_receipts


async def run() -> int:
    if not (bot.X_POST_AUTH_TOKEN and bot.X_POST_CT0_TOKEN):
        print("X posting credentials are missing.")
        return 1
    client = bot.Client("en-US")
    client.set_cookies({"auth_token": bot.X_POST_AUTH_TOKEN, "ct0": bot.X_POST_CT0_TOKEN})
    try:
        settings, _ = await client.v11.settings()
        screen_name = settings["screen_name"]
        account, _ = await client.gql.user_by_screen_name(screen_name)
        user_id = str(account["data"]["user"]["result"]["rest_id"])
        timeline, _ = await client.gql.user_tweets(user_id, 20, None)
        if timeline.get("errors"):
            print("X returned an error while reading the posting account timeline.")
            return 1
        rows = timeline_receipts(timeline, user_id)[:10]
        report = {"posting_account": screen_name, "user_id": user_id, "recent_posts": rows}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            lines = [f"Posting account: **@{screen_name}**", "", "Recent public posts:", ""]
            lines.extend(f"- [{row['created_at']}]({row['url']})" for row in rows)
            if not rows:
                lines.append("No own tweets were returned by X.")
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
        return 0
    except Exception as exc:
        # Never dump request objects, response headers, or cookies in public logs.
        print(f"Posting account read failed: {type(exc).__name__} ({bot.classify_x_error(exc)})")
        return 1
    finally:
        await client.http.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
