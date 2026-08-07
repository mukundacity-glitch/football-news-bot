# FPL VORTEX — Card Specification

Every card the bot publishes is **3840 × 2160 (4K UHD, 16:9)**.

This document is the contract between the data layer and the renderer. It exists
because the two cards that embarrassed us most — "Manchester United Website has
joined Man Utd", and a Tyrique George transfer card showing Everton as his
previous club while the photo showed him in a Chelsea shirt — were both
*correctly rendered*. The renderer did its job. It was handed wrong data and had
no way to say no.

So the rule underneath everything here:

> **A card is a view of verified facts, never a place where a fact is invented.**
> If a field cannot be filled from verified data, the card says so in words. It
> never guesses, never leaves a blank box, and never renders at all if the
> *subject* is unverified.

---

## 1. Rendering pipeline (why Claude is not in it)

A language model cannot produce a 3840 × 2160 PNG. It should not be trying to.
The split is:

| Stage | Owner | Output |
|---|---|---|
| 1. Ingest | scrapers / feeds | raw documents |
| 2. Verify | `src/verification/`, `src/squad_registry.py` | a validated story object, or a rejection |
| 3. Render | `src/renderer.py` + Playwright | 4K PNG, deterministic |
| 4. Post | `main.py` | tweet text + image |

Stage 3 contains **no model calls**. Same story object in, byte-identical card
out, every time. That property is what makes a card reviewable: if a card is
wrong, the bug is in stage 2, and stage 2 is testable without a browser.

If a model is ever used, its only legitimate job is stage 2 → phrasing: turning
already-verified fields into a sentence. It must never be the source of a name,
a club, a fee, a date, or a status.

### How 4K is produced

Templates keep their own CSS design size (the legacy template is laid out at
1380 × 776). `_render_card()` sets Chromium's `device_scale_factor` to
`3840 / design_width`, so glyphs, crests and borders are **re-rasterised** at 4K
rather than upscaled, then `_normalise_card_size()` pins the output to exactly
3840 × 2160. A template does not need rewriting in 4K units to ship a 4K card.

Oversized files are stepped down only if X would reject them
(`_ensure_upload_safe`, 4.4 MB ceiling) — full 4K is kept whenever it fits.

---

## 2. Universal layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ HEADER  (top ~12%)                                                    │
│  ◄ lion logo · "FPL VORTEX" · "Verified Premier League News"          │
│                                    card-type pill, right-aligned ►    │
├───────────────────────────────┬──────────────────────────────────────┤
│ HERO  (left ~46%)             │ DETAIL  (right ~54%)                 │
│  player photo or silhouette   │  club node(s) + crest                │
│  ── first name (small)        │  fact tiles (2-up or stacked)        │
│  ── SURNAME (huge)            │  metadata strip (age/pos/price)      │
│  ── position chip             │  status bar                          │
├───────────────────────────────┴──────────────────────────────────────┤
│ FOOTER (bottom ~10%)   SOURCE: @handle │ ▶ @FPLVORTEX │ 𝕏 @FPLVORTEX │
└──────────────────────────────────────────────────────────────────────┘
```

**Accent colour is set by card type and never by mood:**

| Card type | Accent | Meaning |
|---|---|---|
| TRANSFER — official | `#00FF5A` green | confirmed by a first-party source |
| TRANSFER — agreed/reported | `#FFA500` amber | real but not yet official |
| INJURY | `#FF5555` red | availability reduced |
| SUSPENSION | `#FFA500` amber | availability removed, but not medical |
| PRESS CONFERENCE | `#00BFFF` blue | manager's words, pre-gameweek |

A viewer must be able to tell official from unofficial **at a glance, from the
colour alone**, before reading a word. Amber is not decoration.

---

## 3. Hard rules for every card type

These are enforced in code, not left to judgement.

1. **Subject gate.** The player must resolve in the squad registry
   (`src/squad_registry.py`: live FPL roster + vouched-for, expiring manual
   overrides). No registry match → no card. There is no "the source is trusted
   so it's probably a person" path. That path is exactly what published
   "Manchester United Website".
2. **Photo/crest agreement.** If the player photo and the club crest disagree
   about which club a player is at, the card is wrong — this is the Tyrique
   George failure. The crest comes from the registry's `club_key`, and the photo
   must be resolved for that same player code, never fetched by name search.
3. **Missing ≠ blank.** Every unknown field renders explicit copy:
   - fee → `NOT DISCLOSED`
   - contract → `NOT DISCLOSED`
   - return date → `NOT OFFICIALLY CONFIRMED`
   - anything else → `NOT REPORTED`
   A blank tile reads as a rendering bug; the words read as honesty.
4. **No placeholder leakage.** `verify_card_data()` rejects any card whose
   visible fields contain `n/a`, `null`, `undefined`, `[`, `tbd`, `player name`,
   `from club`, `to club` and friends.
5. **Status wording follows the evidence, not the excitement.** `OFFICIALLY
   CONFIRMED` requires a first-party source. Everything else is `REPORTED`,
   `AGREED` or `RUMOUR`. The word "OFFICIAL" is a factual claim about
   provenance and is load-bearing for trust.
6. **Card and tweet share one name.** Both read `display_name`, pinned once in
   `verify_card_data()`, so they can never disagree.

---

## 4. TRANSFER card

**Publish when:** subject resolves in the registry, both direction ends resolve,
and stage ≥ 2 (agreed) or a first-party confirmation exists.
**Never publish:** stage-1 speculation ("linked with", "monitoring", "% chance").

| Zone | Content | If missing |
|---|---|---|
| Pill | `TRANSFER CONFIRMED` / `TRANSFER AGREED` / `TRANSFER REPORTED` | — |
| Hero | Player photo (by FPL player code), first name + SURNAME, position chip | silhouette |
| Detail — flow | `FROM` crest+code `➜` `TO` crest+code | single node if only one end resolves |
| Detail — tiles | `TRANSFER FEE`, `CONTRACT LENGTH` | `NOT DISCLOSED` |
| Status bar | `OFFICIALLY CONFIRMED` (green) or `REPORTED / UNOFFICIAL` (amber) | — |
| Footer | `SOURCE: @handle` | required — no card without a source |

**Direction is the number-one error class.** `from_key` is taken from the
player's *registry club*, not from whichever club the headline mentioned first.
A headline naming two clubs is not evidence of which way the player is moving.
If origin and destination resolve to the same club, the card is rejected
(`destination_equals_current_club`) — a player cannot join the club he is at.

---

## 5. INJURY card

**Publish when:** subject resolves in the registry **and** the source is tier 1–2
or an approved injury source (`injury_source_not_approved` otherwise). Injury
news moves FPL team selections and money; a random aggregator is not good enough.

| Zone | Content | If missing |
|---|---|---|
| Pill | `INJURY` (red) | — |
| Hero | Photo, name, position chip | silhouette |
| Detail — club | Player's registry club crest + name | required |
| Detail — tiles | `DIAGNOSIS`, `AVAILABILITY`, `EXPECTED RETURN`, `NEXT MATCH` | `NOT REPORTED` / `NOT OFFICIALLY CONFIRMED` |
| Status bar | `OUT` (red) · `DOUBTFUL` (amber) · `FIT AGAIN` (green) | `TO BE ASSESSED` |
| Metadata | Age, position, FPL price | omit the tile entirely |
| Footer | `SOURCE: @handle` + last-verified date | required |

**Return dates are the most dangerous field on this card.** A date must come
from the source text. "Expected back 23 Aug" is a claim someone made — if no one
made it, the tile reads `NOT OFFICIALLY CONFIRMED`. Never compute a return date
from an injury type, and never carry a date forward from an earlier story.

---

## 6. SUSPENSION card

Distinct from injury: a suspension is an **administrative** fact with a known
length, not a medical estimate. It should never be styled red like an injury —
managers and FPL players treat the two differently.

| Zone | Content | If missing |
|---|---|---|
| Pill | `SUSPENSION` (amber) | — |
| Hero | Photo, name, position chip | silhouette |
| Detail — club | Registry club crest + name | required |
| Detail — tiles | `REASON` (red card / accumulation / FA charge), `MATCHES BANNED`, `EARLIEST RETURN` | `NOT CONFIRMED` |
| Status bar | `SUSPENDED` (amber) | — |
| Footer | `SOURCE: @handle` + last-verified date | required |

`MATCHES BANNED` is an integer or `NOT CONFIRMED`. Do not translate it into a
date unless the fixture list has been consulted — "banned for 3" and "out until
14 Sep" are different claims, and only one of them is in the source.

---

## 7. PRESS CONFERENCE card (pre-gameweek, ≥ 3 clubs)

One card per club, published in the window between the manager's presser and the
gameweek deadline. This card summarises *what a named manager said*, so its
sourcing rule is different from the others: the only acceptable source is the
club's own channel or a tier-1/2 outlet that attended.

| Zone | Content | If missing |
|---|---|---|
| Pill | `PRESS CONFERENCE` (blue) | — |
| Hero | Club crest (large) + manager name — **not** a player photo | — |
| Detail — header | `FIXTURE` (e.g. `GW3 vs CHE (H)`), `MANAGER` | required |
| Detail — lists | `OUT`, `DOUBTS`, `BACK` — player names, comma-separated | `NONE REPORTED` |
| Status bar | `TEAM NEWS` | — |
| Footer | `SOURCE: @club` + presser date | required |

Rules specific to this card:

- **Every name in OUT / DOUBTS / BACK must itself pass the registry gate.** A
  presser summary is three lists of players; each one is a subject.
- **`NONE REPORTED` is a real, valuable answer.** An empty list means the
  manager did not mention anyone, which is information. It does not mean go and
  find someone to put there.
- **No quotes, no paraphrase, no analysis.** Lists and fixture only. The moment
  the card carries a sentence a manager didn't say, it is a fabrication with a
  club crest on it.
- **Minimum three clubs per gameweek** — pick by fixture prominence, and skip a
  club rather than publishing a card with three empty lists.

---

## 8. Adding a player the FPL feed doesn't know yet

Real transfers land before the FPL bootstrap feed updates, usually by a day or
two. The sanctioned route is `data/squad_overrides.json`:

```json
{
  "name": "Player Name",
  "club": "man_utd",
  "position": "MID",
  "evidence_url": "https://www.manutd.com/en/news/detail/...",
  "added_by": "your-name",
  "added_at": "2026-08-07",
  "expires_at": "2026-09-04"
}
```

`evidence_url` and `expires_at` are **mandatory** — an entry missing either is
ignored at load time. Entries expire so the file cannot quietly become a stale
second roster: it is renewed on purpose, or the FPL feed takes over.

This is deliberately slower than the old behaviour, which published anything a
trusted account said. Slower is the feature. A human vouching for one player
with a link is the price of never publishing another invented one.
