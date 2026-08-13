# Verification Engine V2

## 1. Purpose

V2 separates **candidate discovery** from **publication authority**. RSS, Google
News, social feeds, and the legacy parser may be noisy. They can only create source
documents and claim hints. A story reaches X only after every critical V2 gate is
`PASS`.

No score or source reputation can compensate for a failed gate.

## 2. Strict default publication policy

| Event | Required authority |
|---|---|
| Transfer | Related club or Premier League official announcement |
| Injury | Related club, official FPL data, or configured governing source |
| Suspension | FA/league/related club official confirmation |
| Manager | Related club official appointment/departure |
| Contract | Related club official extension announcement |
| Official statement | Verified issuing club/league domain or account |

Only `OFFICIAL` and `COMPLETED` publish. All earlier transfer milestones remain
pending. `HERE_WE_GO` support exists behind `policy.allow_here_we_go`, but is false
in the shipped configuration.

## 3. Mandatory decision gates

1. Configuration schema health
2. Dynamic entity-registry health
3. Eligible article category
4. Event classification and certainty
5. Canonical entity and relationship validation
6. Positive football sport validation from independent signal classes
7. Active Premier League ecosystem validation
8. Verified publisher provenance
9. Publishable event status
10. Official/configured authority
11. Grounded mandatory facts
12. Fact-level consensus with no credible conflict
13. Fresh, parseable official timestamp
14. Dynamic source-reliability threshold
15. Database consistency
16. New milestone or material progression
17. Noncompensatory weighted-confidence floor

`FAIL` rejects an invalid candidate. `WAIT` keeps a plausible story pending.
`DUPLICATE` records that no new milestone exists. Only all-`PASS` produces
`PUBLISH`.

## 4. Source provenance

A Google News query label is never source identity. For example, a result from a
query containing “Fabrizio Romano” is attributed to the actual `<source>` publisher
domain, not to Romano.

Direct social evidence must come from the exact configured handle. Direct RSS/API
hints are accepted only for configured direct transports. Publisher independence
uses `publisher_group`, so repeated URLs and sister domains cannot manufacture
source count.

## 5. Sport and entity validation

V2 uses positive evidence rather than sport blacklists. Signals include:

- Football-specific feed metadata
- A canonical football player/manager
- A canonical football club
- An explicit football competition
- A football-only official/specialist source
- Structured official football data

The configured minimum is two independent signal classes. Two signals derived from
the same mistaken club match are not counted separately.

Current PL clubs and players come from FPL every run. A failed FPL fetch makes the
entity registry unhealthy and blocks publication. New players/managers not yet in a
provider may be established only by a related, verified first-party official
announcement with a grounded person name.

## 6. Fact consensus

Every source is extracted separately. V2 compares canonical facts such as:

```text
subject_id
club_from_id
club_to_id
manager_action
injury_status
return_date
suspension_length
contract_length
transfer_kind
```

Different destination clubs are competing facts within the same subject/event
family, not separate stories. Claims from the same publisher group count once.
Pending claims are persisted and replayed during the configured active-story
window, so a conflict does not disappear merely because an article leaves an RSS
feed.

Verified output facts come from the authoritative claim. Optional media-only facts
are not added to an official post.

## 7. Dynamic reliability

The reliability model combines configured priors with outcomes learned only from
later official ground truth:

- Historical confirmation accuracy
- Official status
- False-positive/contradiction rate
- Correction rate
- Verification history

Unknown outcomes do not train the model. Reliability ranks/filters evidence but
never creates official authority or bypasses a gate.

## 8. Temporal and progression rules

V2 stores source publication time, first observation, claims, decisions, and final
publication separately. Unknown or malformed official timestamps wait. Old official
confirmation waits. Identical fingerprints are duplicates.

A new post requires a higher milestone or a changed configured material fact. A
recent publication that conflicts on a critical fact is held rather than replaced.

## 9. Output generation

Fact-only caption templates remain available for verification. All previous
image-card renderers and graphic designs have been removed. The image boundary
raises `RendererNotInstalledError`, so `post_item()` cannot contact X until an
approved replacement renderer is installed. Legacy items and invalid V2
fingerprints remain blocked independently of rendering.

## 10. Persistence

SQLite tables store:

```text
sources
documents
claims
stories
story_claims
decisions
publications
source_outcomes
```

The database performs an integrity check at startup. Corruption or schema mismatch
disables V2 instead of silently forgetting publication history. GitHub Actions
persists the database through `actions/cache` rather than adding a changing SQLite
binary to Git history. The compatibility JSON records whether V2 was previously
initialized; if the cached database later disappears, publishing stops. An operator
may rebuild only with the explicit one-time acknowledgement
`VERIFICATION_V2_ALLOW_DB_REBUILD=I_ACCEPT_HISTORY_RESET` after auditing existing X
and JSON publication history. The old JSON state also fails closed if unreadable.

## 11. Historical regression coverage

The tests prove that V2 blocks, without player-specific production rules:

- The Ben Duckett cricket article interpreted as a Leeds contract
- Tennis injury content entering a football feed
- `France World Cup` interpreted as a player
- Unofficial rumours from major media
- `HERE_WE_GO` while the option is disabled
- Stale official confirmation
- Conflicting official destination clubs
- Cross-run conflicts preserved in the database
- Duplicate official stories

Positive tests cover official transfers, FPL injuries, FA suspensions, manager
appointments, contracts, official club statements, and a future promoted club loaded
dynamically from FPL.

## 12. Deployment procedure

1. Keep `BOT_PAUSED=true` while merging and reviewing.
2. Run the full test suite.
3. Unpause the workflow but leave `ENABLE_AUTOPOST` false.
4. Let shadow mode collect decisions for several days.
5. Run `python tools/v2_report.py` and inspect every proposed `PUBLISH` decision.
6. Correct source/entity configuration if needed; do not add incident-specific
   production rules.
7. Only after review, set:

   ```text
   ENABLE_AUTOPOST=true
   VERIFICATION_V2_LIVE=I_ACCEPT_STRICT_V2
   BOT_PAUSED=false
   ```

8. Continue monitoring decisions and official outcomes. Use `BOT_PAUSED=true` as the
   immediate kill switch.

## 13. Operational trade-off

A confirmed-only system must accept lower recall and slower publication. Some true
breaking reports will remain pending until official confirmation. It is not possible
to guarantee both “never block a true story” and “never post a false story.” V2
chooses precision and transparent pending states over speed.
