# FPL Vortex — Verified Premier League News Bot

This repository collects Premier League news candidates and publishes only stories
that pass **Verification Engine V2**.

## Safety policy

The shipped policy is deliberately strict:

- Transfers, injuries, suspensions, manager changes, contract extensions, and
  official club statements are eligible.
- Only `OFFICIAL` or `COMPLETED` events can publish.
- First-party club, Premier League, FPL, FA, or other configured governing-body
  evidence is required.
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

## Safe rollout

V2 starts in shadow mode. Live posting requires **all three** conditions:

```text
BOT_PAUSED=false
ENABLE_AUTOPOST=true
VERIFICATION_V2_LIVE=I_ACCEPT_STRICT_V2
```

Do not set the final two variables until shadow results have been reviewed.

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
