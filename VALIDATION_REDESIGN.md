# Validation Pipeline: Root Cause Analysis & Redesign (2026-07-13)

Scope note up front: this bot's only data source is X/Twitter (via `twikit`/Nitter,
reading a fixed list of journalist and club accounts in `src/constants.py`) plus the
official `fantasy.premierleague.com` API for player data. It does **not** crawl
official club websites, FIFA/UEFA, Reuters, or any other web page, and has no CMS/
draft-state visibility into a source article. Several items in the original ask
(crawling official sites, detecting "staging"/"noindex" pages) describe a different
architecture and are called out below as out of scope rather than silently skipped.

## 1. Root cause analysis

Each failure was traced to an actual tweet already sitting in `queue/posted/` — not
guessed from the description.

### Failure 1 — Johan Manzambi published as an Aston Villa player
Real story: Freiburg → Newcastle, with Villa only circling as a hijack risk. Two
tweets, both scoring `confidence_decision: REVIEW` (75–89, below the 90 AUTO_POST
floor), both went **live**. Root cause, in order:
- Freiburg wasn't recognized by any club grammar (no literal `"from X"`), so a
  merely-interested third club (Aston Villa) got promoted into the from/to slot by
  word order alone — direction flipped between the two reports.
- **The confidence floor was bypassable.** `.github/workflows/bot.yml` runs
  `python main.py --allow-rumours` on *every* scheduled run — unconditionally, not as
  an opt-in. `--allow-rumours` was coded to unlock both "genuinely rumour-staged
  news" (`mode=rumour`) *and* "REVIEW-tier confidence" (`confidence_decision=REVIEW`)
  as a single flag. REVIEW means the pipeline itself isn't sure the extraction is
  right — a different concern from "is this event confirmed yet" — and conflating
  them meant every run implicitly disabled the review-tier safety net.
- Fixed in the previous session (direction/entity extraction) plus this session
  (confidence floor is no longer bypassable — see §3.1).

### Failure 2 — Youri Tielemans → Man Utd (never published)
No trace of this story exists anywhere in the repo (`queue/`, `data/`, `fixtures/`),
so it can't be root-caused from evidence the way the other three could — I won't
guess at a mechanism I can't verify. The stated root cause ("trusted a draft/
unpublished source") and the stated symptom ("never published") point in opposite
directions, which reads like the failure description itself may be describing two
different incidents. What **is** verifiable and fixed regardless: §3.2 below removes
a structural reason genuinely-confirmed news can get stuck unpublished (a `to_key`
requirement that only PL clubs can satisfy). If this specific case resurfaces,
capturing the actual tweet text would let it be root-caused the same way the other
three were.

### Failure 3 — Hugo Oliveira published as Fulham head coach (real hire: Arbeloa)
Real tweet: *"Strasbourg confirm Hugo Oliveira has joined as head coach... Was in
the running for the Fulham job and now likely to move to Ligue 1 with Strasbourg."*
Two compounding bugs:
- Strasbourg (the real destination) is invisible to the PL-only club lexicon, same
  failure class as #1 — and the manager/staff pipeline has *no* equivalent to
  `direction.py`'s foreign-club grammar at all (it only ever existed for
  transfer/loan events), so there was no fallback once Strasbourg dropped out.
- "Was in the running for the Fulham job" is a **rejected candidacy** — a different
  phrasing pattern from Manzambi's "also interested" (still-active interest). The
  interest-only exclusion list didn't cover past/rejected-candidacy language, so
  Fulham — the only PL club the text mentions — got promoted into the destination
  slot by elimination.
- Even with a role identified, the tweet-body generator had no hedge path for "role
  known, action unconfirmed" — it rendered as a flat assertion
  ("HUGO OLIVEIRA — HEAD COACH AT FULHAM") regardless of `mode`/confidence.

### Failure 4 — genuine PL loans/transfers never published (Jesse Derry, et al.)
Real tweet (ChelseaFC, official account): *"Jesse Derry has joined Sporting Lisbon...
on loan..."* — scored **confidence 100 / AUTO_POST**, yet rendered as
`"LINKED WITH A LOAN MOVE"` (rumour wording) instead of confirmed. Root cause:
`classify_post`/`status_label` required `to_key` — a match against the **20-club PL
alias table** — before a story could ever be labelled CONFIRMED/OFFICIAL. A PL
club's own official announcement of a player leaving *to* a non-PL club (loans out,
the single most common movement for fringe/academy players) has no PL `to_key` by
definition, so it could never clear the bar no matter how certain the source. This
is very likely the dominant mechanism behind most of the named failure-4 examples
(young/fringe players moving between a PL club and a non-PL one) — the other names
listed (Tchaouna, Cordero, Monga, Nypan, Murray-Campbell, Joseph, Charles, Meslier,
Young, Ashby, Jota Silva) have no corresponding tweet in the repo to verify
individually, but they fit the exact same profile: a resolved origin/destination
`*_club` (raw name) that never got credit because only `*_key` was checked.
*(A large, separate backlog of `*_unknown_injury` files in `queue/pending` — several
hundred — turned out to be output from a now-removed FPL-API "player news" ingestion
path with no corresponding code left in the repo. That's stale data, not a live bug;
flagged as a housekeeping item, not fixed as logic.)*

## 2. What changed, mapped to the requested validation layers

| Requested layer | What exists / changed | File |
|---|---|---|
| Confidence engine, weighted score | Already existed (`src/confidence.py`): additive scoring, AUTO_POST ≥ 90, REVIEW 75–89, SKIP < 75. **Fixed**: the 90-point floor is now a hard gate — no flag bypasses REVIEW into auto-publish. | `main.py` (`_conf_ok`) |
| Source reliability tiers | Already existed: `OFFICIAL_ACCOUNTS` / `ELITE_TRUSTED` / `TRUSTED_MEDIA` tiers in `src/constants.py`, scored via `source_tier()`. Not changed this pass — see §4 for the "automatic" reputation idea. | `src/constants.py` |
| Multi-source verification | Already existed: `classify_post` requires official/elite-tier corroboration for CONFIRMED. **Added**: contradiction-aware merge — a second source naming a genuinely different club is no longer silently counted as corroboration. | `main.py` (`scrape()` merge block) |
| Event classification granularity | Existing 4-stage model (rumour → advanced → official, `stage` 1–4) plus `mode` (rumour/confirmed) and per-event labels. **Fixed**: the "is this wording official" cue list was inconsistent between the stage-grading and confirmation-gate layers (duplicated, drifted); consolidated into one constant. **Fixed**: event classification (transfer/injury/loan/manager/etc.) now picks whichever cue occurs *earliest* in the text instead of a fixed priority order, so a trailing aside can't outrank the actual headline. | `src/constants.py` (`STRONG_OFFICIAL_CUES`), `src/parser.py` |
| Speculative-language detection | Extended the "this club is not actually party to the move" cue list to cover **rejected/past candidacy** phrasing ("was in the running for", "shortlisted for", "missed out on", ...) in addition to active-interest/hijack phrasing. Narrowed two cues (`monitoring`, `tracking`) that were catching benign "we'll monitor his development" farewell language as false positives. | `src/parser.py` |
| Draft/preview detection | Reinterpreted for this architecture: the bot ingests tweets, not web pages, so there's no CMS/staging state to check. What exists and is real: template-placeholder leakage (`"player name"`, `"[Duration & Details]"`, `"TBD"`, etc.) is already rejected pre-render (`_CARD_PLACEHOLDERS`/`PLACEHOLDERS` in `main.py`). URL-based draft/staging detection is **out of scope** — there is no web-crawl step to apply it to. |  |
| Contradiction detection | **New**: when two reports resolve to the same story key but name genuinely different clubs (not just a reversed direction on the same pair), the second is flagged `contradicted` and the story is held in `pending` — never auto-published, and the disagreeing source is not counted toward the corroboration total. | `main.py` (`scrape()`, `ready` loop) |
| Temporal validation | Already existed and unchanged: `tweet_too_old()` fails closed (unparseable date ⇒ too old), 3-day cutoff enforced in both the scrape loop and `validate_story`. |  |
| Entity validation | Already existed (`src/entity_guard.py`): player/coach/manager/agent/director/journalist/media/company/stadium/club classifier, independent of any per-name list. Unchanged this pass — no evidence surfaced a gap here. |  |
| Tournament logic | Not present and not added — no evidence tied any of the four failures to tournament-timing confusion specifically. Flagged as a gap if a concrete case surfaces. |  |
| Fail-safe / "refuse to publish" | Directly strengthened by the confidence-floor fix and the contradiction hold — both now fail closed (block/hold) rather than fail open. |  |

## 3. Key fixes, in detail

### 3.1 Confidence floor is now unconditional
`main.py`, the live-posting gate:
```python
def _conf_ok(d):
    return d.get("confidence_decision", "AUTO_POST") == _conf.AUTO_POST
```
`--allow-rumours` still controls whether `mode="rumour"` stories (accurately
extracted, merely unconfirmed-event) are included at all — that's a legitimate,
separate product decision. It can no longer also let a REVIEW-tier (uncertain
*extraction*) story through. This is the single highest-leverage fix: it would have
independently blocked both the Manzambi and Hugo Oliveira posts from ever going
live, regardless of any extraction accuracy.

### 3.2 CONFIRMED/OFFICIAL no longer requires a Premier-League-specific key
`classify_post` and `status_label` previously gated on `story.get("to_key")` (a hit
against the 20-club PL alias table). Now: `story.get("to_key") or story.get("to_club")`
— any resolved destination, PL or not. A PL club's official announcement of an
outbound loan/transfer to a foreign or EFL club can now be labelled CONFIRMED/
OFFICIAL. `validate_story`'s `no_resolved_club` check was fixed the same way.

### 3.3 Contradiction detection on source merge
Before, merging a second source onto an existing candidate story just appended it to
`sources` unconditionally. Now, if the new report names a genuinely different club
than the already-merged story (not just a reversed to/from on the same pair — that's
already absorbed by the unordered-pair story key from the previous fix round), it's
marked `contradicted` and the story is held in `data["pending"]` — never scored,
never posted, and the disagreeing source doesn't inflate the corroboration count.

### 3.4 Interest-only / rejected-candidacy language, broadened
Added phrasing for candidacies that did **not** happen ("was in the running for",
"shortlisted for", "missed out on", "in the frame for", ...) alongside the existing
active-interest/hijack cues. Narrowed two cues (bare `monitoring`, `tracking`) that
were ambiguous enough to false-positive on benign language.

### 3.5 Staff/manager wording now hedges when the action is unconfirmed
A known role (e.g. "head coach") with no confirmed appointment/departure action now
renders as `"LINKED WITH A ... ROLE"` instead of a flat, unhedged assertion.

### 3.6 Earliest-cue event classification (carried over, re-verified this pass)
Classification picks whichever event cue occurs earliest in the text, not a fixed
category-priority order — a trailing "currently injured" aside can no longer
outrank a leading transfer/loan headline. Fixed a real regression this introduced
for `"has joined X on loan"` phrasing (the generic "joined" transfer-cue was racing
the more specific loan-cue and sometimes winning); the loan-cue now anchors at the
same earliest word so it wins the tie it should always win.

### 3.7 One canonical "official wording" list
`STRONG_OFFICIAL_CUES` now lives once, in `src/constants.py`, used by both the
stage-grading step (`parser.py`) and the confirmation gate (`main.py`). Previously
these were two independently-maintained lists that had already drifted (one was
missing `"joined"`, `"signed"`, `"medical"`, etc.), which is exactly the kind of
inconsistency that can leave a genuinely-official-sounding report understaged.

## 4. Explicitly out of scope this pass (and why)

- **Crawling official club sites / FIFA / UEFA / Reuters / BBC as independent
  sources.** The bot has no web-scraping or API integration for any of these today
  — only Twitter/X accounts (some of which *are* those orgs' social handles) and the
  FPL API. Building real integrations is a legitimate next step but is a new-data-
  source project (credentials, rate limits, ToS), not a validation-logic fix.
- **CMS draft/staging/noindex detection.** No web pages are fetched, so there is no
  such state to detect. The equivalent risk that does exist — a source's own
  graphic-template placeholder text leaking into a tweet — is already handled by the
  placeholder-blob checks.
- **A dynamic, self-learning source-reputation ledger** (score a journalist by how
  often their early claims are later corroborated vs. contradicted, instead of a
  static tier list). This is a good idea and directly answers the "automatically
  calculate source reliability" ask, but it's a genuinely new subsystem (persistent
  history, decay, cold-start handling) rather than a bug fix — recommended as a
  follow-up, not attempted here.
- **Granular event sub-states** ("Bid Submitted", "Medical Scheduled", "Registration
  Complete", etc.). The existing 4-stage + mode(rumour/confirmed) + label model
  covers the same decisions (post/hold, how to word it) with less surface area to
  keep consistent. Expanding it is a wording/product change, not something any of
  the four failures required.
- **Full modular re-architecture** into named classes (Evidence Collector, Source
  Ranking, Contradiction Detector, Draft Detector, Entity Resolver, Confidence
  Calculator, Publication Decision Engine as separate modules/classes). The existing
  file split (`parser.py` / `direction.py` / `entity_guard.py` / `confidence.py` /
  `main.py`) already maps to most of these responsibilities; a big-bang rewrite
  would touch far more surface area for the same behavioral outcome and materially
  raises the risk of a new class of regression. Extending the existing modules (done
  here) was the higher-confidence path.

## 5. Housekeeping observed, not fixed

`queue/pending/` contains several hundred `*_unknown_injury_s3.json` files (and
`queue/posted/` a couple dozen more) with a schema (`confidence`, `fpl_official`,
`id: "fpl_<n>_<timestamp>"`) that no code in the current repo produces — evidence of
a since-removed ingestion path (an FPL-API "player news" feed). These are inert
under the current code but represent real disk/repo clutter. Recommend a separate,
explicit cleanup pass (not bundled into this fix, since deleting queue history
wasn't asked for and is easy to get wrong silently).

## 6. Test coverage added this pass

- `tests/test_publish_gates.py` — Hugo Oliveira (wrong-club rejection, hedge
  wording), Jesse Derry (confirmed foreign-destination loan), and the confidence-
  floor-is-unconditional behavior, all using the actual incident text.
- `tests/test_dedup.py` — added a genuinely-conflicting-clubs contradiction case
  alongside the existing direction-flip dedup tests.
- Full suite: 66/66 passing (`python -m pytest tests/`).

## 7. Missing entity taxonomy: COUNTRY / COMPETITION (2026-07-30)

Real false post that reached publish:

> 🔵 TRANSFER - FRANCE WORLD CUP CONFIRMED PERMANENT TRANSFER FROM CRYSTAL
> PALACE TO CHELSEA. 💰 FEE — £52M 📊 STAGE — IN PROGRESS

### Root cause

`src/entity_guard.py`'s taxonomy (the single gate every extracted "player"
name passes through) had **no COUNTRY or COMPETITION category at all** —
only PLAYER / COACH / MANAGER / DIRECTOR / EXECUTIVE / AGENT / JOURNALIST /
MEDIA / COMPANY / BRAND / SPONSOR / STADIUM / CLUB / UNKNOWN. A capitalized
run of words that wasn't a club, journalist, staff role, company, or known
junk fragment fell through to the `PLAYER` default at the bottom of
`classify_entity_detailed` — exactly what happened to "France World Cup", a
country name and a tournament name glued into one fragment by the
capitalized-word-sequence extractor in `src/parser.py`.

A dead, never-wired attempt at the same problem already existed in
`main.py` (`COUNTRY_NAMES` / `_build_country_block`, built from FPL player
nationalities) — defined, populated, and never called from anywhere. Removed
as part of this fix; it was strictly weaker than the new mechanism anyway
(only covered nationalities of currently-rostered FPL players, not
countries/tournaments in general, and had no COMPETITION concept at all).

Once classified as `PLAYER` by default, the story sailed through every
downstream gate that *should* have caught it, because they all check
"is this a real club/does the fee look right", never "is the subject
actually a person":
- `clubs_verified` / `validate_direction` (`src/confidence.py`) passed —
  Crystal Palace and Chelsea are both real, resolvable PL clubs.
- The confidence engine's `elite_source_confirmed_language` **override**
  (`src/confidence.py: evaluate()`) then bypassed the additive score
  entirely: any official/elite source + "confirmed"/"official"/"here we
  go" wording + `etype == "PLAYER"` force-set the decision to `AUTO_POST`,
  regardless of the actual score. A tournament reference worded like an
  official announcement (as this one was) is exactly the shape that
  triggers it.

### Fix — three independent, non-bypassable layers

No part of this fix names "France", "World Cup", or the specific incident
text anywhere in code — it is entirely knowledge/taxonomy-based, the same
pattern already used for clubs/journalists/media/stadiums, so it covers
every country and every tournament/league, past and future.

1. **New knowledge bases**: `data/countries.json` (country/national-team
   names, ~100 entries) and `data/competitions.json` (tournaments/leagues/
   cups, exact names + significant tokens), following the exact structure
   of the existing `data/clubs_extended.json`.
2. **New taxonomy entries** (`src/entity_guard.py`): `COUNTRY` and
   `COMPETITION` join the hard-reject set (`_HARD_REJECT`), checked
   *before* the `PLAYER` default via `detect_country_entity()` /
   `detect_competition_entity()` — exact match for countries (never a
   substring, so a real surname sharing a fragment with a country name is
   never caught by accident), exact match OR ≥2-significant-token-overlap
   OR a strong single-token indicator for competitions (mirrors
   `detect_club_entity`'s existing strategy, and is what catches "France
   World Cup" — tokens `{france, world, cup}` hit the `world cup`
   tokenset/the `cup` token directly, no matter what else is glued to it).
3. **Extraction-time defence** (`src/parser.py: _is_bad_name`): a
   country/competition candidate is rejected *before* it is ever assigned
   to `story["player"]`, so extraction tries the next capitalized run in
   the text instead of settling on the bad one. For the reported post there
   was no other candidate, so `player` correctly comes back `None` and
   `validate_story` discards the story as `missing_player` — never a guess.
4. **Confidence override hardened** (`src/confidence.py: evaluate()`): the
   `elite_source_confirmed_language` override now requires `etype ==
   "PLAYER"` **and** a zero `entity_penalty`, not `etype == "PLAYER"` alone
   — so no future hard-reject category can silently re-open this exact
   bypass by being added to `_HARD_REJECT` without also being wired into
   every override condition individually.
5. **Missing-name no longer defaults to PLAYER**: `classify_entity_detailed("")`
   previously returned `("PLAYER", "empty_name")` "for downstream checks to
   handle" — itself a guess-when-missing bug. It now returns `UNKNOWN` (hard
   reject), closing the same class of failure for any code path that scores
   confidence before (or without) a `validate_story` call.

### Rejection audit logging (new: `src/rejection_log.py`)

Every hard rejection now writes a durable, structured record — not just a
console `print()` — to `queue/debug/rejections.jsonl` (append-only, capped
at 5,000 lines) and `queue/debug/rejected_latest.json` (rolling snapshot of
the last 300). Each record carries exactly what's needed to debug a
rejection after the fact without re-running anything by hand: the original
source text verbatim, every field the extractor produced, the entity
classification (type + reason) for the subject, the confidence
score/breakdown when one was computed, and the exact gate + reason string
that rejected it. Wired into all four rejection points: `passes_safety_gate`
and `validate_story` failures and confidence `SKIP` in `scrape()`, genuine
(not merely unconfirmed) `contradiction` holds, and the pre-draft
`validate_story`/`verify_card_data` double-check in `build_draft()`.
Logging is best-effort by design (wrapped, never raises) so a disk/logging
fault can never block or crash the posting pipeline it audits.

### Test coverage added this pass

- `tests/test_false_news_prevention.py` — the exact reported post end-to-end
  (extraction → `validate_story` → confidence override), the COUNTRY/
  COMPETITION taxonomy on ~15 country/tournament names, a real-player
  false-positive sanity check, five *different* country+competition
  fragments (Brazil/Copa America, England/Euros, Premier League, Champions
  League, Ivory Coast/AFCON) to prove this generalizes rather than
  pattern-matching the one incident, and the empty-name-is-never-PLAYER fix.
- `tests/test_rejection_log.py` — required-field coverage of a rejection
  record, append-not-overwrite behavior, and "logging never raises."
- Full suite: 88/90 passing (`python -m pytest tests/`) — the 2 failures
  (`test_gates.py::test_manager_appointment_can_confirm`,
  `test_parser.py::test_genuine_second_club_still_captured_when_not_interest_only`)
  pre-exist this change (confirmed by running the suite before touching any
  file) and are unrelated to entity classification; left as-is, out of scope
  for a false-news-prevention pass.

## 8. Transfer-recall + posting-rules pass, code cleanup (2026-07-30, same day)

Ask: reduce the false-post rate further, catch more genuinely-confirmed
transfers (the "Done Deal filter only catches ~10%" complaint), add explicit
✅ CONFIRMED / 🔄 RUMOUR labeling with source + link on every post, add a
settle-time delay before a lower-tier "confirmed" story goes live, and clean
up dead/residual code. Injury classification and wording were explicitly
out of scope ("do not touch injury") — every change below was checked
against that constraint directly, not just by intent.

**Resolved contradiction, explicitly:** the request included both "send
borderline cases to a Telegram/Discord review queue for a human to approve"
and, in the same message, "all auto no manual work." Implemented the second:
no human-approval step was added. The bot's existing `data["pending"]`
mechanism (a story that isn't confirmed yet is held and automatically
re-verified against fresh sources on the next scheduled run — no human
involved) already serves the "second mode" purpose the review queue was
meant to cover.

**Not implemented as literally specified:** the pasted `is_likely_confirmed()`
keyword function was not adopted verbatim. It classifies purely on keyword
presence with no club/entity/source validation — exactly the shape of bug
that let "France World Cup" post as a transfer (§7 above). Its keyword list
was instead folded into the existing, layered pipeline (entity guard, club/
direction validation, contradiction detection, confidence scoring), so
recall goes up without reopening that hole.

### 8.1 Broadened confirmed-transfer wording — deliberately NOT via the shared stage list

The obvious place to add recall was `STRONG_OFFICIAL_CUES` (the single
completion-wording list). Verified empirically that this is unsafe: that
list also drives `parser.py`'s universal `stage` field, which injury posts
read directly (`_avail_text`: stage 4 → "FIT AGAIN"). Adding, e.g.,
`"announced"` there flips a genuinely-fresh, still-ongoing injury to
"FIT AGAIN" whenever a club statement about the injury happens to use that
word ("Arsenal announced Saka is out for six weeks" → wrongly rendered
FIT AGAIN). This reproduces with the *original* list too via "confirmed"/
"official"/"medical" (a pre-existing, out-of-scope-to-fix issue, left alone
per "do not touch injury" — see the test suite's documented pre-existing
failures).

Fix: a new, separate constant, `TRANSFER_CONFIRM_CUES` (`src/constants.py`),
read *only* by `classify_post()`'s transfer/loan branch in `main.py` — never
by `parser.py`'s stage computation. `classify_post()`'s injury branch
returns before ever consulting it, so this cannot change injury behavior by
construction, not just by careful wording choice.

**Backtested against real data, not synthetic examples.** With no live
network access in this environment, the closest available substitute for
"last 7 days of production traffic" was the repository's own historical
corpus: 203 real transfer/loan items already sitting in `queue/posted/` and
`queue/pending/` from actual past runs. Every candidate cue was checked
against all 203 before being kept:

| Candidate cue | Real historical false positive found? |
|---|---|
| `completes`, `announces`, `announced`, `presented as`, `unveiling` | None found across all 203 items — kept. |
| `finalised` / `finalized` | **Yes.** Matched "verbal agreement in place with details **to be finalised** soon" (future tense — not done) and "discovery rights compensation has now **been finalised**" (an ancillary inter-club fee, not the transfer itself). Removed. |
| `contract until` | **Yes.** Matched "Bergvall is under **contract until** June 2031" describing his existing contract at his *current* club (cited as a reason he's hard to sign) — the opposite of evidence a new move is confirmed. Removed. |
| `permanent transfer`, `free transfer` | Not tried — these describe deal TYPE, not status, and already appear verbatim in `parser.py`'s stage-1 SPECULATION cue list; adding them would make a rumour match the confirmation list too. |
| `agreement reached` | Not tried — this codebase already has a distinct AGREED tier (stage 2-3) one step below OFFICIAL for exactly this wording. |

Net result on the 203-item historical corpus: wording-only match rate rose
from 92/203 (45.3%) to 93/203 (45.8%), with the single newly-caught item
already labelled `confirmed` historically (a Bournemouth official signing
announcement) — **zero new false positives** on real data. This is a modest,
evidence-bounded number, not the "far beyond 10%" framing in the original
ask — that figure couldn't be independently verified against this bot's
actual historical performance (no ground-truth "% caught vs missed" dataset
exists in the repo), so it isn't quoted here as if it were. The full
transfer-confirmation decision also depends on source tier, stage, and the
elite-source-confirmed-language override (§7, hardened) — this table
measures only the wording signal in isolation, which is what the historical
JSON records could actually support checking.

Also added: `"% chance"`, `"chance of joining"`, `"chance of a move"`,
`"likelihood of"` to `validate_story`'s stage-1 speculation cue list (from
the requested `rumour_indicators`) — scoped inside the existing
`if ev in ("transfer", "loan", "loan_option")` block, so structurally unable
to affect injury's own (unrelated) "% chance of playing" availability field.

### 8.2 Settle-time gate before a non-elite "confirmed" post goes live

New `settle_time_ok(story)` (`main.py`) + `MIN_CONFIRM_SETTLE_MINUTES`
(default 45, env-overridable): a transfer/loan story classified `confirmed`
from a source that is neither official (tier 1) nor elite (tier 2: Romano/
Ornstein/Sky/BBC/Athletic) must have been sitting in the pipeline for at
least this long — tracked via a new `first_seen` timestamp, carried forward
across runs in `data["pending"]` — before it's allowed to post. This is the
requested "wait 30-60 minutes and re-check before posting" behavior,
implemented without adding polling infrastructure: the bot already re-scores
every pending story each scheduled run, so a held story is automatically
re-verified, not just delayed. Official/elite sources bypass the wait
entirely — their confirmation language is already about as final as
football reporting gets, so an artificial delay would add latency without
reducing false positives for those tiers specifically. Non-transfer events
(injury, manager, renewal, ...) always return `True` — untouched.

### 8.3 Explicit labeling, source, and link on every transfer post

`build_tweet_body()`'s transfer/loan branch now always includes:
- `✅ STATUS — CONFIRMED` or `🔄 STATUS — RUMOUR`, driven directly by `mode`
  (the actual publish-decision variable), independent of the existing
  headline wording (which already varied by stage/label — "AGREEMENT
  REACHED", "LINKED WITH A", etc. — and was left alone since several tests
  depend on that exact hedge wording).
- `📡 SOURCE — @handle(s)`, plus the article/tweet link when one was
  actually captured from the feed (`source_url`, newly threaded through
  `fetch_rss_news()`/`fetch_bluesky_posts()` → `scrape()` → the story dict —
  never fabricated when the feed entry had no real link).
- The visual card (`src/renderer.py`) already renders its own prominent
  CONFIRMED/OFFICIAL/TRANSFER RUMOUR badge (unchanged, out of scope) — this
  section adds the same clarity to the tweet caption text itself, which is
  searchable/readable without opening the image.

### 8.4 Code cleanup (dead/residual code removed)

- `main.py`: `is_big_name_player()`, `is_big_club_name()`, and
  `BIG_NAMES_NON_FPL` — three stubs/constants with zero call sites anywhere
  in the codebase (confirmed via full-repo search before removal).
- `main.py`: local `is_big_player()` and `fpl_team_key()` definitions
  removed. Both were byte-for-byte duplicates of `src/fpl_feed.py`'s
  versions that main.py *also imported* — the local defs silently shadowed
  the import the moment the module loaded, making the import dead weight.
  Worse, they had actually drifted: `fpl_feed.fpl_team_key()`'s
  `resolve_club_key()` accent-strips before matching (`_strip()`); main.py's
  shadowing copy only lowercased. Removing the duplicate means the accent-
  safe canonical version is what actually runs now, not a silent second
  implementation nobody was calling by design.
- `main.py`: dead `COUNTRY_NAMES` / `_build_country_block()` — see §7,
  already covered by the new `data/countries.json`-backed entity guard.
- `is_big_player` dropped from the `src.fpl_feed` import list (unused
  everywhere, confirmed via search).

### 8.5 Test coverage added this pass

`tests/test_transfer_accuracy_improvements.py` (14 tests): broadened-wording
recall on 4 real phrasings; the exact 2 real historical false-positive
sentences (Doku/Bergvall) that justified removing `finalised`/`contract
until`, locked in as regression tests; the injury-stage-immunity check
(3 sentences that would have flipped stage to 4 under the old shared-list
approach, verified to stay at stage 1); `settle_time_ok()` behavior across
tier 1/2/3/0 sources and non-transfer events; unparseable-timestamp fails
closed; source+link rendering with and without a captured URL; the explicit
✅/🔄 status line; and presence checks confirming the removed dead code is
actually gone. Full suite: 102/104 passing — the same 2 pre-existing,
unrelated failures as §6/§7 (still unchanged, still out of scope).
