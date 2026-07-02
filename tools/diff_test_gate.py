"""
diff_test_gate.py

Compares OLD behavior (separate validate_story -> verify_card_data ->
classify_post calls, as main.py does today) against NEW behavior
(master_story_gate) on real cached stories, WITHOUT touching main.py's
call sites.

Usage:
    python diff_test_gate.py

Reads stories from data.json's "extracted" / "pending" / "stories" caches
if present. Prints a diff for any story where old and new disagree on
accept/reject, on classify_post's label, on intermediate field mutations,
or on final field values.

Run this BEFORE swapping any call site over to master_story_gate.
"""
import copy
import json
import sys
from pathlib import Path

# add repo root so GitHub Actions can find main.py
sys.path.append(str(Path(__file__).resolve().parents[1]))

# add repo root so GitHub Actions can find main.py
sys.path.append(str(Path(__file__).resolve().parents[1]))

from main import (
    validate_story,
    verify_card_data,
    classify_post,
    fetch_fpl_data,
)

# optional imports (keep safe)
try:
    from src.parser import passes_safety_gate
    from main import source_tier
except Exception:
    passes_safety_gate = None
    source_tier = None

# only import this if the file exists in repo root
from master_story_gate_v6_fixed import master_story_gate

KEY_FIELDS = ("from_key", "to_key", "display_name", "mode", "player")


def summarize(story):
    return {k: story.get(k) for k in KEY_FIELDS}


def resolve_sources(story, override):
    """Single place that decides what 'sources' means for a story, so both
    old and new paths always use the identical value -- no per-path guessing
    of story['sources'] vs [username] vs an override that only one side sees."""
    if override is not None:
        return override
    return story.get("sources") or ["unknown"]


# ── DRAFT-TIME PATH (mirrors post_item / build_draft) ──────────────────────
# Production reality: post_item/build_draft NEVER call classify_post. The
# mode a story posts under was already decided earlier, at scrape time, on
# the PRE-verify story, and is cached on story["mode"] by the time these
# functions run. So the fair comparison is:
#   OLD: classify_post on the pre-verify snapshot (what actually decides
#        production's label today)
#   NEW: classify_post on the post-verify snapshot (what master_story_gate
#        would produce if run_classify=True moves classification after
#        card-data mutation)
# A mismatch between these two is exactly the risk this diff test exists to
# catch -- reordering silently changing rumour/confirmed.

def old_path_draft_time(story, fpl, sources=None):
    src = resolve_sources(story, sources)
    stages = {}
    s = copy.deepcopy(story)
    stages["input"] = summarize(s)

    stages["classify_pre_verify"] = classify_post(copy.deepcopy(s), src)

    ok, why = validate_story(s, fpl)
    stages["after_validate"] = summarize(s)
    if not ok:
        return False, f"validate:{why}", s, stages

    ok2, why2, _ = verify_card_data(s, fpl)
    stages["after_verify"] = summarize(s)
    if not ok2:
        return False, f"verify:{why2}", s, stages

    return True, "ok", s, stages


def new_path_draft_time(story, fpl, sources=None):
    src = resolve_sources(story, sources)
    stages = {}
    s = copy.deepcopy(story)
    stages["input"] = summarize(s)

    ok, why = master_story_gate(s, s.get("text", ""), fpl,
                                 sources=src, run_safety_gate=False,
                                 run_classify=True)
    stages["after_gate"] = summarize(s)
    stages["classify_post_verify"] = s.get("mode")
    return ok, why, s, stages


# ── SCRAPE-TIME PATH (mirrors _read_pass / dry-run) ─────────────────────────
# classify_post is deliberately NOT run inside either path here: in
# production it runs once, in bulk, after the whole story_map is built --
# not per-story alongside safety/validate. That bulk step is covered by the
# draft-time comparison above instead (same underlying function, same
# question: does reordering change the label).

def old_path_scrape_time(story, text, fpl, sources=None):
    src = resolve_sources(story, sources)
    s = copy.deepcopy(story)
    if passes_safety_gate:
        safe, why = passes_safety_gate(s, text, fpl, sources=src,
                                        source_tier_func=source_tier)
        if not safe:
            return False, f"safety:{why}", s
    ok, why = validate_story(s, fpl, sources=src)
    if not ok:
        return False, f"validate:{why}", s
    return True, "ok", s


def new_path_scrape_time(story, text, fpl, sources=None):
    src = resolve_sources(story, sources)
    s = copy.deepcopy(story)
    ok, why = master_story_gate(s, text, fpl, sources=src,
                                 source_tier_func=source_tier,
                                 run_safety_gate=bool(passes_safety_gate),
                                 run_classify=False)
    return ok, why, s


# ── DIFF RUNNERS ─────────────────────────────────────────────────────────

def run_diff_draft_time(stories, fpl):
    label = "DRAFT-TIME (validate+verify; classify compared pre-verify vs post-verify)"
    print(f"\n=== {label}: {len(stories)} stories ===")
    disagreements = 0
    for i, story in enumerate(stories):
        sources = resolve_sources(story, None)
        old_ok, old_why, old_s, old_stages = old_path_draft_time(story, fpl, sources)
        new_ok, new_why, new_s, new_stages = new_path_draft_time(story, fpl, sources)

        if old_ok != new_ok:
            disagreements += 1
            print(f"  [DISAGREE-ACCEPT] #{i} {story.get('player')!r}: "
                  f"old={old_ok}({old_why}) new={new_ok}({new_why})")
            continue

        old_label = old_stages.get("classify_pre_verify")
        new_label = new_stages.get("classify_post_verify")
        if old_label != new_label:
            disagreements += 1
            print(f"  [DISAGREE-LABEL] #{i} {story.get('player')!r}: "
                  f"pre-verify classify={old_label!r} vs "
                  f"post-verify classify={new_label!r} "
                  f"(reordering changed the rumour/confirmed label)")

        if "after_verify" in old_stages and "after_gate" in new_stages:
            if old_stages["after_verify"] != new_stages["after_gate"]:
                disagreements += 1
                print(f"  [DISAGREE-STAGE:post-verify-fields] #{i} "
                      f"{story.get('player')!r}: "
                      f"old={old_stages['after_verify']} "
                      f"new={new_stages['after_gate']}")

        if old_ok and new_ok and summarize(old_s) != summarize(new_s):
            disagreements += 1
            print(f"  [DISAGREE-FINAL-FIELDS] #{i} {story.get('player')!r}: "
                  f"old={summarize(old_s)} new={summarize(new_s)}")

    print(f"  -> {disagreements} disagreement(s) out of {len(stories)}")
    return disagreements


def run_diff_scrape_time(stories, fpl):
    label = "SCRAPE-TIME (safety+validate only; classify covered separately above)"
    print(f"\n=== {label}: {len(stories)} stories ===")
    disagreements = 0
    for i, story in enumerate(stories):
        text = story.get("text", "")
        sources = resolve_sources(story, None)
        old_ok, old_why, old_s = old_path_scrape_time(story, text, fpl, sources)
        new_ok, new_why, new_s = new_path_scrape_time(story, text, fpl, sources)

        if old_ok != new_ok:
            disagreements += 1
            print(f"  [DISAGREE-ACCEPT] #{i} {story.get('player')!r}: "
                  f"old={old_ok}({old_why}) new={new_ok}({new_why})")
        elif old_ok and new_ok and summarize(old_s) != summarize(new_s):
            disagreements += 1
            print(f"  [DISAGREE-FINAL-FIELDS] #{i} {story.get('player')!r}: "
                  f"old={summarize(old_s)} new={summarize(new_s)}")

    print(f"  -> {disagreements} disagreement(s) out of {len(stories)}")
    return disagreements


def load_cached_stories():
    data_path = Path("data.json")
    stories = []
    if data_path.exists():
        try:
            data = json.loads(data_path.read_text())
        except Exception as e:
            print(f"[WARN] could not parse data.json: {e}")
            data = {}
        for bucket in ("extracted", "pending", "stories"):
            for v in (data.get(bucket) or {}).values():
                if isinstance(v, dict) and v.get("player"):
                    stories.append(v)
    if not stories:
        print("[WARN] no cached stories found in data.json — "
              "run this from the same directory main.py runs from, "
              "after the bot has scraped at least once.")
    return stories


def main():
    fpl = fetch_fpl_data()
    stories = load_cached_stories()
    if not stories:
        return

    total = 0
    total += run_diff_draft_time(stories, fpl)
    total += run_diff_scrape_time(stories, fpl)

    print(f"\n=== TOTAL disagreements: {total} ===")
    if total == 0:
        print("No behavior differences detected on this sample. This still "
              "does NOT cover post_item's EVENT_PRIORITY/status_label/cooldown "
              "logic, which never calls classify_post/verify_card_data and so "
              "is a separate question from what this script tests. Test "
              "manually on at least one loan, one injury, and one "
              "destination-less transfer story before deploying.")
    else:
        print("Do NOT deploy master_story_gate until every disagreement above "
              "is understood and intentional.")


if __name__ == "__main__":
    main()
