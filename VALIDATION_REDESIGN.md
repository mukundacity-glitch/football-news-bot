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
