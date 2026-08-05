# FPL Vortex — Verified Premier League News Bot

This repository collects Premier League news candidates and publishes only stories
that pass **Verification Engine V2**.

## Safety policy

The shipped policy is deliberately strict:

- **Only Premier League–related TRANSFERS, INJURIES, and SUSPENSIONS are
  eligible.** Manager changes, contract extensions, club statements, and all
  other categories are rejected even when officially confirmed.
- Only `OFFICIAL` or `COMPLETED` events can publish.
- First-party club, Premier League, FPL, FA, or other configured governing-body
  evidence is required, and the story must involve an active Premier League
  club/player relationship (league validation is a hard gate — non-PL news,
  e.g. Real Madrid or Saudi clubs, is rejected).
- Journalist/media reports remain pending until official confirmation.
- `HERE_WE_GO` auto-publication is implemented but disabled in configuration.
- Missing, stale, ambiguous, cross-sport, conflicting, or ungrounded facts fail
  closed and cannot reach X.
- Post text is deterministic and uses verified facts only. Missing fee, contract,
  diagnosis, return date, etc. are omitted rather than guessed.

## Emergency stop

Set this GitHub Actions repository variable:

```text
BOT_PAUSED=true
```

You can also disable the `FPL Vortex Bot` workflow in the Actions UI.

## Production behavior

The scheduled GitHub workflow is configured for **set-and-forget live posting**:

- the main bot checks for new official confirmed stories every **15 minutes**
  so transfer, injury, and suspension confirmations are not stale. X only sees
  actual posts, not source checks.
- live posting is enabled automatically on every scheduled run
- every V2-verified **confirmed transfer, injury, and suspension** story is eligible to post
- confirmed manager, contract, and other non-target items are intentionally skipped from live posting
- safety caps default to **2 posts per run, 4 per hour, 16 per day**. Posts are
  spaced by human-like jitter (90–240s) one at a time — that pacing, not
  throttling, is the anti-flag mechanism — and the X-safety cooldown stops the
  run instantly if X signals automation/spam/rate-limit (codes 226/326/429),
  backing off 3 hours (flagged) or 30 minutes (rate-limited) before any further
  attempt
- X captions are concise non-premium-safe posts: no more than four visible
  lines, no long source URL, and relevant club/event/FPL hashtags. The verified
  image card carries the larger visual detail.
- every run writes a clear GitHub Actions summary explaining why it posted or did not post
- if X posting cookies expire, the run fails with an actionable `X-AUTH` message
  pointing at `X_POST_AUTH_TOKEN` / `X_POST_CT0_TOKEN`
- `BOT_PAUSED=true` remains the single emergency kill switch

### FPL deadline Top-5 team-news run

A second workflow, `FPL Deadline Top-5 News`, polls every 15 minutes but only runs
inside the calculated pre-deadline window:

- official FPL deadline timestamp = first Premier League kickoff minus 90 minutes
- bot target = deadline minus 30 minutes (effectively kickoff minus 2 hours)
- teams = current official Premier League table Top 5; if the table is still all
  played-0 before GW1, it falls back to FPL team strength to avoid alphabetical
  placeholder standings
- source = official FPL player availability data only
- content = at most three concise text cards for critical Top-5 player news:
  confirmed out/suspended, confirmed fit/available return, or major doubt
- no lineup guesses, no fake starters, no deadline-only alert, and no post at all
  if there are zero critical items
- one post per gameweek deadline; the shared state ledger blocks duplicate
  deadline posts and counts the tweet toward the daily cap

Required GitHub Actions secrets for X posting:

```text
X_POST_AUTH_TOKEN
X_POST_CT0_TOKEN
```

If you prefer maintaining only one X cookie pair, the bot will fall back to:

```text
X_AUTH_TOKEN
X_CT0_TOKEN
```

when the `X_POST_*` secrets are absent.

## Architecture

- `src/verification/documents.py` — immutable source documents and configured feeds
- `src/verification/source_registry.py` — actual publisher/domain/handle provenance
- `src/verification/entities.py` — dynamic FPL player and active-club registry
- `src/verification/extractor.py` — grounded entity/event/status/fact extraction
- `src/verification/consensus.py` — fact agreement and conflict detection
- `src/verification/reliability.py` — dynamic source outcomes and reliability
- `src/verification/repository.py` — SQLite documents, claims, stories, decisions,
  source outcomes, and publications
- `src/verification/engine.py` — mandatory noncompensatory publication gates
- `src/verification/renderer.py` — verified-facts-only X templates
- `src/verification/card.py` — verified-facts-only image cards

The legacy regex parser in `main.py` supplies candidate hints only. It has no live
publication authority; `post_item()` rejects every item without a serialized V2
decision whose critical gates all remain `PASS`.

## Configuration

All V2 policy, thresholds, weights, status ontology, and rollout controls live in:

```text
config/verification.json
```

Source domains, handles, official scope, publisher independence groups, and
reliability priors live in:

```text
config/sources.json
```

Feeds and declared sport metadata live in:

```text
config/feeds.json
```

Current Premier League players/clubs are loaded dynamically from the official FPL
API. Non-PL club references are data in `data/clubs_extended.json`, not executable
player-specific logic.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

The GitHub bot workflow runs the complete suite before it can run the bot.
Historical regression tests include the Ben Duckett cricket/Leeds false post and
the `France World Cup` entity failure.

## Shadow report

After one or more shadow runs:

```bash
python tools/v2_report.py
```

V2 stores structured state in `data/verification.sqlite3`; GitHub Actions persists it
through `actions/cache` rather than committing a changing binary on every run. A
corrupt or mismatched database disables publication rather than silently starting
with empty dedup data. If an Actions cache is lost after initialization, restore it;
only an audited reset may temporarily set
`VERIFICATION_V2_ALLOW_DB_REBUILD=I_ACCEPT_HISTORY_RESET`.

See [`VERIFICATION_V2.md`](VERIFICATION_V2.md) for decision rules, migration, and
operational details.
