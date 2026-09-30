"""Isolated FPL editorial lane: live research, branded packages and confirmed X delivery."""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageOps

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/fpl-mini-league-edge"
STATE_BRANCH = "fpl-mini-league-edge-posts"
STATE_ROOT = ROOT.parent / ".fpl-mini-league-edge-state"
LOGO = ROOT / "assets/branding/fpl_strategy_logo.png"
BRIEF = ROOT / "config/fpl_mini_league_edge_prompt.md"
TZ = ZoneInfo("America/New_York")
API = "https://api.openai.com/v1"
FPL = "https://fantasy.premierleague.com/api"
SLOTS = {"morning": 8, "lunch": 12, "evening": 17}
TOPICS = {1: "The Mini-League Deciding Move", 2: "The Ownership Trap of the Day",
          3: "The Hidden Fixture Swing", 4: "The Chip Timing Edge", 5: "The Structure Fix"}


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def request_json(url: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    response = requests.get(url, timeout=(15, 60), **kwargs)
    response.raise_for_status()
    return response.json()


def slot_for(now: datetime, requested: str = "", schedule: str = "") -> tuple[str, datetime]:
    local = now.astimezone(TZ)
    if schedule:
        mapped = {f"0 {hour} * * *": name for name, hour in SLOTS.items()}
        if schedule not in mapped:
            raise ValueError("Unknown scheduled edition")
        requested = mapped[schedule]
    if requested not in SLOTS:
        raise ValueError("Choose morning, lunch or evening")
    target = local.replace(hour=SLOTS[requested], minute=0, second=0, microsecond=0)
    if schedule and not timedelta(0) <= local - target < timedelta(hours=4):
        raise ValueError("Scheduled edition is stale or not yet due")
    return requested, target


def topic_for(slot: str, local_day: datetime) -> int:
    if slot == "morning":
        return 1
    if slot == "lunch":
        return 2
    return {0: 3, 1: 4, 2: 5, 3: 3, 4: 4, 5: 5, 6: 3}[local_day.weekday()]


def live_snapshot(now: datetime) -> dict[str, Any]:
    bootstrap = request_json(f"{FPL}/bootstrap-static/")
    fixtures = request_json(f"{FPL}/fixtures/")
    if not isinstance(bootstrap, dict) or not isinstance(fixtures, list):
        raise ValueError("Invalid official FPL response")
    events = bootstrap.get("events", [])
    next_event = next((event for event in events if event.get("is_next")), None)
    if not next_event:
        raise ValueError("No upcoming gameweek; do not publish stale-season advice")
    deadline = datetime.fromisoformat(next_event["deadline_time"].replace("Z", "+00:00"))
    if deadline <= now or deadline - now > timedelta(days=60):
        raise ValueError("Official FPL deadline is stale or outside the active season")
    players = bootstrap.get("elements", [])
    if not players or not bootstrap.get("teams"):
        raise ValueError("Official squad data is empty")
    return {"checked_at": now.isoformat(), "next_event": next_event,
            "events": events, "players": players, "teams": bootstrap["teams"],
            "fixtures": fixtures, "chips": bootstrap.get("chips", []),
            "sources": [f"{FPL}/bootstrap-static/", f"{FPL}/fixtures/"]}


def numeric(player: dict[str, Any], metric: str) -> float:
    if metric == "price":
        return float(player["now_cost"]) / 10
    value = player.get(metric)
    if value is None:
        raise ValueError(f"Missing official metric: {metric}")
    return float(value)


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    columns = ["id", "web_name", "team", "element_type", "now_cost", "selected_by_percent",
               "minutes", "total_points", "form", "status", "news", "chance_of_playing_next_round",
               "expected_goals", "expected_assists", "starts", "transfers_in_event", "transfers_out_event"]
    start = int(snapshot["next_event"]["id"])
    return {"checked_at": snapshot["checked_at"], "next_event": snapshot["next_event"],
            "teams": snapshot["teams"], "chips": snapshot["chips"],
            "player_columns": columns,
            "players": [[player.get(key) for key in columns] for player in snapshot["players"]],
            "fixtures": [{key: fixture.get(key) for key in
                          ["id", "event", "team_h", "team_a", "kickoff_time",
                           "team_h_difficulty", "team_a_difficulty"]}
                         for fixture in snapshot["fixtures"]
                         if fixture.get("event") and start <= fixture["event"] < start + 10]}


def schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


POST_SCHEMA = schema({
    "publishable": {"type": "boolean"}, "reason": {"type": "string"},
    "headline": {"type": "string"}, "decision": {"type": "string"},
    "qualifier": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}},
    "caption": {"type": "string"}, "analysis": {"type": "string"},
    "player_ids": {"type": "array", "items": {"type": "integer"}},
    "sources": {"type": "array", "items": {"type": "string"}},
    "claims": {"type": "array", "items": schema({
        "player_id": {"type": "integer"}, "metric": {"type": "string"},
        "value": {"type": "number"}})},
})
REVIEW_SCHEMA = schema({"approved": {"type": "boolean"}, "reason": {"type": "string"}})


def response_text(response: dict[str, Any]) -> str:
    if response.get("status") != "completed":
        raise RuntimeError("OpenAI response did not complete")
    text = "\n".join(part.get("text", "") for item in response.get("output", [])
                     if item.get("type") == "message" for part in item.get("content", [])
                     if part.get("type") == "output_text")
    if not text.strip():
        raise RuntimeError("OpenAI returned no usable text")
    return text


def openai(path: str, **kwargs: Any) -> dict[str, Any]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Add the OPENAI_API_KEY repository secret")
    # Do not retry paid requests automatically after ambiguous network failures.
    response = requests.post(f"{API}/{path}", headers={"Authorization": f"Bearer {key}"},
                             timeout=(20, 600), **kwargs)
    if not response.ok:
        raise RuntimeError(f"OpenAI {path} failed (HTTP {response.status_code})")
    return response.json()


def model_response(content: Any, output_schema: dict[str, Any] | None = None,
                   *, search: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": os.getenv("FPL_EDGE_TEXT_MODEL", "gpt-5.5"),
        "input": [{"role": "user", "content": content}], "store": False,
        "max_output_tokens": 10000,
    }
    if search:
        payload["tools"] = [{"type": "web_search"}]
        payload["tool_choice"] = "required"
        payload["include"] = ["web_search_call.action.sources"]
    if output_schema:
        payload["text"] = {"format": {"type": "json_schema", "name": "fpl_edge",
                                      "strict": True, "schema": output_schema}}
    return openai("responses", json=payload)


def research_urls(response: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for item in response.get("output", []):
        if item.get("type") == "web_search_call":
            found.update(source["url"] for source in item.get("action", {}).get("sources", [])
                         if isinstance(source.get("url"), str))
        for part in item.get("content", []):
            found.update(annotation["url"] for annotation in part.get("annotations", [])
                         if annotation.get("type") == "url_citation" and annotation.get("url"))
    return found


def validate_post(post: dict[str, Any], snapshot: dict[str, Any], allowed_urls: set[str]) -> None:
    if not post.get("publishable"):
        raise ValueError("No publishable insight: " + str(post.get("reason", "")))
    if not 1 <= len(post["headline"].split()) <= 7 or len(post["headline"]) > 65:
        raise ValueError("Headline must be at most seven words and 65 characters")
    if not post["decision"].strip() or len(post["decision"]) > 100:
        raise ValueError("Central decision is empty or too long")
    if not post["qualifier"].strip() or len(post["qualifier"]) > 110:
        raise ValueError("Essential qualifier is empty or too long")
    if not 1 <= len(post["evidence"]) <= 3 or any(len(line) > 80 for line in post["evidence"]):
        raise ValueError("Use one to three short supporting lines")
    caption = post["caption"].strip()
    hashtags = re.findall(r"(?<!\w)#[A-Za-z0-9_]+", caption)
    if len(caption) >= 240 or len(hashtags) != 5 or len(set(hashtags)) != 5:
        raise ValueError("Caption must be under 240 characters with five distinct hashtags")
    if not re.search(r"(?:#[A-Za-z0-9_]+\s+){4}#[A-Za-z0-9_]+$", caption):
        raise ValueError("All five hashtags must be together at the end")
    if not {"#FPL", "#FPLVortex"}.issubset(hashtags):
        raise ValueError("Include #FPL and #FPLVortex")
    players = {int(player["id"]): player for player in snapshot["players"]}
    if not 1 <= len(post["player_ids"]) <= 2 or any(pid not in players for pid in post["player_ids"]):
        raise ValueError("Choose one or two current official FPL player identities")
    metrics = {"price", "selected_by_percent", "minutes", "total_points", "form", "starts",
               "expected_goals", "expected_assists", "transfers_in_event", "transfers_out_event",
               "chance_of_playing_next_round"}
    for claim in post["claims"]:
        if claim["metric"] not in metrics or claim["player_id"] not in players:
            raise ValueError("Unsupported statistical claim")
        actual = numeric(players[claim["player_id"]], claim["metric"])
        expected = float(claim["value"])
        if not math.isfinite(actual) or not math.isfinite(expected) or abs(actual - expected) > 0.051:
            raise ValueError("Statistical claim differs from the official snapshot")
    if not post["sources"] or any(url not in allowed_urls for url in post["sources"]):
        raise ValueError("Source was not retrieved in this run")
    public_text = " ".join([post["headline"], post["decision"], post["qualifier"], caption, *post["evidence"]])
    if re.search(r"\bmodel\b", public_text, re.IGNORECASE):
        raise ValueError("Public wording violates the editorial brief")


def create_post(snapshot: dict[str, Any], topic: int, previous: list[dict[str, Any]],
                destination: Path) -> tuple[dict[str, Any], str]:
    context = json.dumps(compact_snapshot(snapshot), ensure_ascii=False)
    brief = BRIEF.read_text(encoding="utf-8")
    research = model_response(
        brief + f"\nToday's topic is {topic}: {TOPICS[topic]}.\n"
        + "Search current official sources; assess alternatives and give a sourced detailed research memo. "
        + "Verify injuries, minutes, fixtures and chip rules. Do not issue an unconditional injury gamble.\n"
        + "Previous posts to avoid repeating:\n" + json.dumps(previous, ensure_ascii=False)
        + "\nLIVE OFFICIAL SNAPSHOT (now_cost is in tenths of a million):\n" + context, search=True)
    memo = response_text(research)
    urls = research_urls(research) | set(snapshot["sources"])
    if not research_urls(research):
        raise ValueError("Research returned no traceable web sources")
    save_json(destination / "research.json", research)
    instructions = (brief + f"\nCreate Topic {topic}: {TOPICS[topic]}.\n"
                    + "Every statistical number in the graphic/caption must have a matching claims entry. "
                    + "Use ONLY these statistical claim metrics from the snapshot: price (now_cost/10), "
                    + "selected_by_percent, minutes, total_points, form, starts, expected_goals, "
                    + "expected_assists, transfers_in_event, transfers_out_event, chance_of_playing_next_round. "
                    + "Use research news qualitatively; omit unsupported external numeric statistics. "
                    + "Use only retrieved source URLs. Headline <=65 chars, decision <=100 chars, "
                    + "qualifier <=110 chars, 1–3 evidence lines each <=80 chars, 1–2 official player_ids. "
                    + "Caption <240 chars including exactly five ending hashtags, including #FPL and #FPLVortex. "
                    + "If the evidence does not support a useful recommendation, set publishable=false.\n"
                    + "RESEARCH:\n" + memo + "\nALLOWED SOURCES:\n" + json.dumps(sorted(urls))
                    + "\nOFFICIAL DATA:\n" + context)
    draft = json.loads(response_text(model_response(instructions, POST_SCHEMA)))
    validate_post(draft, snapshot, urls)
    review = json.loads(response_text(model_response(
        "Independently audit this FPL post against the research and official data. Reject numerical "
        "errors, misleading injury clearance, unsupported sell verdicts or chip certainty, wrong "
        "fixtures/season, invented expert plans, unlabelled forecasts and advice lacking budget/fitness "
        "conditions. Check that EVERY number in public text is supported, including fixture ratings.\n"
        + json.dumps(draft, ensure_ascii=False) + "\nRESEARCH:\n" + memo + "\nDATA:\n" + context,
        REVIEW_SCHEMA)))
    save_json(destination / "editorial-review.json", review)
    if not review["approved"]:
        raise ValueError("Editorial review rejected the post: " + review["reason"])
    return draft, memo


def player_references(snapshot: dict[str, Any], post: dict[str, Any]) -> tuple[list[tuple[str, bytes]], str]:
    players = {player["id"]: player for player in snapshot["players"]}
    teams = {team["id"]: team["name"] for team in snapshot["teams"]}
    references = []
    identities = []
    for player_id in post["player_ids"]:
        player = players[player_id]
        identities.append(f"{player['first_name']} {player['second_name']} ({teams[player['team']]})")
        code = int(player["code"])
        url = f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png"
        response = requests.get(url, timeout=(15, 45))
        response.raise_for_status()
        Image.open(io.BytesIO(response.content)).verify()
        references.append((f"player-{player_id}.png", response.content))
    return references, "; ".join(identities)


def brand_image(raw: bytes, logo_path: Path, destination: Path) -> None:
    with Image.open(io.BytesIO(raw)) as image:
        if image.width != image.height:
            raise ValueError("Generated graphic is not square")
        final = image.convert("RGB").resize((1080, 1080), Image.Resampling.LANCZOS)
    with Image.open(logo_path) as logo:
        fitted = ImageOps.contain(logo.convert("RGBA"), (304, 144), Image.Resampling.LANCZOS)
    # Paste the original asset after generation so lettering and proportions cannot drift.
    ImageDraw.Draw(final).rectangle((0, 0, 319, 159), fill=(16, 19, 22))
    final.paste(fitted, ((320 - fitted.width) // 2, (160 - fitted.height) // 2), fitted)
    final.save(destination, format="PNG", optimize=True)


def create_graphic(snapshot: dict[str, Any], post: dict[str, Any], destination: Path) -> Path:
    references, identities = player_references(snapshot, post)
    date = datetime.fromisoformat(snapshot["checked_at"]).astimezone(TZ).strftime("%d %b %Y %H:%M ET")
    prompt = (
        "Create a premium square 1024x1024 editorial FPL social graphic. Charcoal #101316, restrained "
        "emerald and gold, mobile-first high contrast, large clean typography, no clutter or overlap. "
        "Leave top-left rectangle x0–304 y0–152 completely empty charcoal for an exact logo overlay. "
        "All headline text must be below y180, player faces outside the reserved rectangle. "
        "Use the supplied real player photos as identity references; preserve facial identity and "
        "show their CURRENT clubs: " + identities + ". Do not copy outdated kits from references. "
        "Feature the portrait(s) on the right, decision and evidence on the left or lower clean cards. "
        "Reproduce this text EXACTLY without adding numerical claims or other wording:\n"
        + "HEADLINE (big bold white): " + post["headline"] + "\nCENTRE DECISION: " + post["decision"]
        + "\nESSENTIAL QUALIFIER: " + post["qualifier"] + "\nSUPPORTING LINES:\n"
        + "\n".join(post["evidence"]) + "\nSMALL READABLE FOOTER: Official FPL + club news • " + date
        + "\nNo invented crests, watermark or logo. All content inside 50px safe margins."
    )
    (destination / "image-prompt.txt").write_text(prompt, encoding="utf-8")
    files = [("image[]", (name, content, "image/png")) for name, content in references]
    payload = openai("images/edits", files=files, data={
        "model": os.getenv("FPL_EDGE_IMAGE_MODEL", "gpt-image-2"), "prompt": prompt,
        "size": "1024x1024", "quality": "high", "n": "1"})
    if not payload.get("data") or not payload["data"][0].get("b64_json"):
        raise RuntimeError("Image API did not return a PNG")
    image_path = destination / "graphic.png"
    brand_image(base64.b64decode(payload["data"][0]["b64_json"], validate=True), LOGO, image_path)
    review = json.loads(response_text(model_response([
        {"type": "input_text", "text": "Inspect this finished FPL graphic. Approve only if every "
         "headline, decision, qualifier and supporting line matches the supplied text, nothing is "
         "cut off or overlapping, all text is readable on mobile, the supplied player identities "
         "match the portraits, and the top-left original FPL VORTEX logo is visible. Text to check: "
         + json.dumps(post, ensure_ascii=False) + "\nPlayer identities: " + identities},
        {"type": "input_image", "image_url": "data:image/png;base64," +
         base64.b64encode(image_path.read_bytes()).decode("ascii")},
    ], REVIEW_SCHEMA)))
    save_json(destination / "graphic-review.json", review)
    if not review["approved"]:
        raise ValueError("Graphic review rejected the image: " + review["reason"])
    return image_path


def git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Git state operation failed; publishing is stopped")
    return result.stdout.strip()


def prepare_state() -> Path:
    if STATE_ROOT.exists():
        raise RuntimeError("Use a fresh checkout; a prior state worktree already exists")
    exists = git("ls-remote", "--heads", "origin", STATE_BRANCH)
    if exists:
        git("fetch", "origin", f"{STATE_BRANCH}:refs/remotes/origin/{STATE_BRANCH}")
        git("worktree", "add", "--detach", str(STATE_ROOT), f"origin/{STATE_BRANCH}")
    else:
        git("worktree", "add", "--detach", str(STATE_ROOT), "HEAD")
    git("config", "user.name", "fpl-mini-league-edge-bot", cwd=STATE_ROOT)
    git("config", "user.email", "bot@users.noreply.github.com", cwd=STATE_ROOT)
    return STATE_ROOT


def checkpoint(state_root: Path, edition: Path, message: str) -> None:
    relative = edition.relative_to(state_root)
    git("add", "--force", str(relative), cwd=state_root)
    if git("diff", "--cached", "--name-only", cwd=state_root):
        git("commit", "-m", message + " [skip ci]", cwd=state_root)
        # No force push: a competing state writer must stop before any X request.
        git("push", "origin", f"HEAD:refs/heads/{STATE_BRANCH}", cwd=state_root)


async def publish(image_path: Path, text: str) -> str:
    from twikit import Client
    from src.twikit_runtime import apply_twikit_transaction_patch
    from src.x_delivery import create_tweet_confirmed

    auth = (os.getenv("X_POST_AUTH_TOKEN") or os.getenv("X_AUTH_TOKEN") or "").strip()
    ct0 = (os.getenv("X_POST_CT0_TOKEN") or os.getenv("X_CT0_TOKEN") or "").strip()
    if not auth or not ct0:
        raise RuntimeError("X posting credentials are missing")
    apply_twikit_transaction_patch()
    client = Client("en-US")
    client.set_cookies({"auth_token": auth, "ct0": ct0})
    media_id = await client.upload_media(str(image_path), media_type="image/png")
    receipt = await create_tweet_confirmed(client, text=text, media_ids=[media_id])
    return receipt.url


def run(slot: str, target: datetime, *, dry_run: bool, sync_state: bool) -> int:
    edition_key = f"{target:%Y-%m-%d}-{slot}"
    output = OUTPUT / edition_key
    output.mkdir(parents=True, exist_ok=True)
    state_root = prepare_state() if sync_state else ROOT
    canonical = state_root / "fpl-edge-posts" / f"{target:%Y-%m-%d}" / slot
    record_path = canonical / "post.json"
    existing = load_json(record_path, {})
    if existing.get("status") in {"published", "publishing", "delivery_uncertain"}:
        if canonical.exists():
            shutil.copytree(canonical, output, dirs_exist_ok=True)
        print("Edition already delivered or requires reconciliation; no duplicate X request.")
        return 0
    if not dry_run and not sync_state:
        raise ValueError("Publishing requires durable --sync-state checkpoints")
    if not os.getenv("OPENAI_API_KEY", "").strip() or not LOGO.is_file():
        raise RuntimeError("OPENAI_API_KEY and the supplied logo are required")
    if not dry_run and not ((os.getenv("X_POST_AUTH_TOKEN") or os.getenv("X_AUTH_TOKEN"))
                           and (os.getenv("X_POST_CT0_TOKEN") or os.getenv("X_CT0_TOKEN"))):
        raise RuntimeError("X credentials are required for publishing")
    now = datetime.now(timezone.utc)
    snapshot = live_snapshot(now)
    save_json(output / "official-snapshot.json", snapshot)
    previous = [load_json(path, {}) for path in sorted((state_root / "fpl-edge-posts").glob("*/*/post.json"))[-21:]]
    previous = [{key: row.get(key) for key in ["edition", "topic", "headline", "decision", "caption"]}
                for row in previous]
    topic = topic_for(slot, target)
    post, memo = create_post(snapshot, topic, previous, output)
    if any(row.get("decision") == post["decision"] and row.get("headline") == post["headline"]
           for row in previous[-6:]):
        raise ValueError("Unchanged recent insight; choose a genuinely fresh angle")
    image_path = create_graphic(snapshot, post, output)
    if datetime.now(timezone.utc) - now > timedelta(minutes=30):
        raise ValueError("Package took too long; refresh data before publishing")
    record = {**post, "edition": edition_key, "topic": TOPICS[topic], "created_at": now.isoformat(),
              "status": "draft" if dry_run else "ready", "x_url": ""}
    save_json(output / "post.json", record)
    (output / "caption.txt").write_text(post["caption"].strip() + "\n", encoding="utf-8")
    (output / "analysis.md").write_text(post["analysis"] + "\n\n" + memo, encoding="utf-8")
    (output / "README.md").write_text(
        f"# {TOPICS[topic]} — {edition_key}\n\n[Download graphic](graphic.png)\n\n"
        + "![Finished graphic](graphic.png)\n\n```text\n" + post["caption"].strip() + "\n```\n\n"
        + "Sources:\n" + "\n".join(f"- {url}" for url in post["sources"]) + "\n", encoding="utf-8")
    shutil.copytree(output, canonical, dirs_exist_ok=True)
    if sync_state:
        checkpoint(state_root, canonical, f"save FPL edge package {edition_key}")
    if dry_run:
        print(f"Draft saved: {edition_key}")
        return 0
    record["status"] = "publishing"
    save_json(record_path, record)
    checkpoint(state_root, canonical, f"reserve FPL edge X delivery {edition_key}")
    try:
        record["x_url"] = asyncio.run(publish(image_path, post["caption"].strip()))
        record["status"] = "published"
    except Exception:
        # An uncertain delivery is never retried automatically: X may have accepted it.
        record["status"] = "delivery_uncertain"
        save_json(record_path, record)
        save_json(output / "post.json", record)
        checkpoint(state_root, canonical, f"record uncertain FPL edge delivery {edition_key}")
        raise RuntimeError("X delivery unconfirmed; reconcile this edition before retrying") from None
    save_json(record_path, record)
    save_json(output / "post.json", record)
    checkpoint(state_root, canonical, f"confirm FPL edge X delivery {edition_key}")
    print("Published: " + record["x_url"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=list(SLOTS), default=os.getenv("FPL_EDGE_SLOT", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync-state", action="store_true")
    args = parser.parse_args()
    dry_run = args.dry_run or os.getenv("FPL_EDGE_DRY_RUN", "").lower() == "true"
    try:
        slot, target = slot_for(datetime.now(timezone.utc), args.slot, os.getenv("FPL_EDGE_SCHEDULE", ""))
        return run(slot, target, dry_run=dry_run, sync_state=args.sync_state)
    except Exception as error:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        save_json(OUTPUT / "failure.json", {"error": message, "at": datetime.now(timezone.utc).isoformat()})
        print("FPL Mini-League Edge failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
