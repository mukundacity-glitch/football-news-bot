# FPL Vortex — Verified Premier League News Bot

This repository collects Premier League news candidates and publishes only stories
that pass **Verification Engine V2**.

## Safety policy

The shipped policy is deliberately strict:

- **Only Premier League–related TRANSFERS, INJURIES, SUSPENSIONS, and
  PRESS_CONFERENCE quotes are eligible.** Manager appointments/departures,
  contract extensions, club statements, and all other categories are
  rejected even when officially confirmed.
- First-party `OFFICIAL` or `COMPLETED` events publish as **CONFIRMED**.
- A separate transfer-only **REPORTED** lane accepts Fabrizio Romano,
  David Ornstein/The Athletic, BBC Sport, and Sky Sports: one independent
  source for `TALKS`/`NEGOTIATION`/`BID`; two for
  `AGREEMENT`/`MEDICAL`/`HERE_WE_GO`. It also accepts structured completed rows
  from FotMob's Premier League transfer table within 48 hours. `INTEREST`,
  `RUMOUR`, and unstructured third-party completion claims never publish.
- First-party club, Premier League, FPL, FA, or other configured governing-body
  evidence is still required for **CONFIRMED** wording, and every story must
  involve an active Premier League club/player relationship (league validation
  is a hard gate — non-PL news is rejected).
- Missing, stale, ambiguous, cross-sport, conflicting, or ungrounded facts fail
  closed and cannot reach X.
- Post text is deterministic and uses verified facts only. Missing fee, contract,
  diagnosis, return date, etc. are omitted rather than guessed.

### TRANSFER: separate CONFIRMED and REPORTED lanes

A transfer receives **CONFIRMED** wording only when a first-party official
source (buying club, selling club, official league site, or verified official
club account) states that the move is signed/joined/completed.

Reliable but non-official transfer updates use a distinct **REPORTED** lane:

- approved sources only: Fabrizio Romano, David Ornstein/The Athletic, BBC Sport,
  and Sky Sports;
- one approved source for `TALKS`, `NEGOTIATION`, or `BID`;
- two independent approved publishers for `AGREEMENT`, `MEDICAL`, or
  `HERE_WE_GO` (Ornstein and The Athletic count as one newsroom);
- structured FotMob rows may publish `COMPLETED` only as **REPORTED/LISTED**, are
  limited to 48 hours, and must carry a real FotMob player ID plus a current PL
  club on at least one side of the move;
- `INTEREST`, `RUMOUR`, unstructured FotMob text, and other non-official claims
  labelled `OFFICIAL` or `COMPLETED` remain blocked;
- player, FROM club, TO club, Premier League relevance, freshness, source URL,
  contradiction, duplicate, and confidence gates must still pass.

`src/verification/official_transfer_gate.py` protects official posts, while
`src/verification/reported_transfer_gate.py` independently re-checks reported
posts before rendering. A reported decision can never receive confirmed
wording.

### Renderer status

The previous rendering systems were completely removed. The replacement
`src/rendering/` package implements the owner's four uploaded master references:
transfer, injury, suspension and press conference. It renders RGB 3840×2160 PNGs
with fixed FPL VORTEX/PL header branding, a fixed five-zone footer, calculated
data rows, automatic text fitting, verified FROM → TO direction and identity-safe
image resolution. The approved renderer is used by verified production drafts
for every supported category.

### PRESS_CONFERENCE: official Premier League round-up only

The combined press-conference post is publishable only from a verified
PremierLeague.com round-up. One first-party Premier League article is sufficient
authority; media and journalist reports cannot enter this route. The article
must still provide a grounded manager, club, quote summary, key quotes and the
complete extracted round-up before `src/verification/press_conference_gate.py`
authorizes the reference-template graphic.

Caption and image generation remain fact-only. Live posting still requires a
fully publishable V2 decision and all event-specific authorization gates.

### Injury/suspension classification: ambiguous trigger phrases require corroboration

Some event trigger phrases are ambiguous out of context — "ruled out" fires
identically on a disallowed VAR goal ("Meunier then had a goal ruled out...")
and on a genuine injury. `config/verification.json`'s
`classification.corroboration_patterns` lists real medical/disciplinary
evidence terms (hamstring, scan, surgery, red card, ban, ...) that must also
appear in the document before INJURY/SUSPENSION classification is accepted;
otherwise classification fails closed to UNKNOWN instead of guessing. This
check is generic and config-driven, not keyed to any player, club, or match.

## Emergency stop

Set this GitHub Actions repository variable:

```text
BOT_PAUSED=true
```

You can also disable the `FPL Vortex Bot` workflow in the Actions UI.

## Production behavior

The scheduled GitHub workflow is configured for **set-and-forget live posting**:

- the main bot checks for new official confirmed stories every **20 minutes**
  so transfer, injury, and suspension confirmations are not stale. X only sees
  actual posts, not source checks.
- live posting is enabled automatically on every scheduled run
- every V2-verified **confirmed transfer, injury, and suspension** story is eligible to post
- press conferences are collected by the same verification engine but are
  reserved for the dedicated deadline round-up, preventing a partial article
  from being posted before all available manager sections are present
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

### FPL deadline press-conference round-up

The `FPL Deadline Press Conference Round-Up` workflow polls every 10 minutes and
opens a 30-minute posting window before the official FPL deadline:

- source = the verified PremierLeague.com combined press-conference article only
- content = one 3840×2160 reference-template graphic with latest news, key
  quotes, manager notes and **every extracted manager update** (up to all 20
  Premier League clubs); there is no Top-5 filter or hidden “more updates” row
- the normal 20-minute news workflow cannot publish press conferences early;
  this deadline workflow is the sole live owner of the combined round-up
- normal verification, source-domain checks, duplicate suppression, daily/hourly
  limits and X cooldowns remain mandatory
- manual dispatch can bypass the time window with `force=true`, but it cannot
  bypass verification or duplicate protection

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
- `src/verification/renderer.py` — verified-facts-only text templates
- `src/verification/card.py` — strict authorization boundary for image rendering
- `src/rendering/engine.py` — reusable 4K master broadcast compositor
- `src/rendering/layout.py` — calculated layout, icons, fitting and wrapping
- `src/rendering/assets.py` — identity-safe player/crest resolution
- `src/renderer.py` — legacy compatibility stubs and image-blank safety only

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
