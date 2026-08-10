# FPL VORTEX — Card Specification

Every published card is **3840 × 2160 (4K UHD, 16:9)**.

> **A card is a view of verified facts, never a place where a fact is invented.**

The renderer receives a verified story object only. If the safety gate rejects the story, no card is rendered and nothing reaches X.

## 1. Approved visual system

### TRANSFER — confirmed

- Heading: **TRANSFER CONFIRMED**
- Heading/accent: `#00FF5A` Vortex green
- Green confirmation/check icon
- Dark stadium/football background with subtle green glow
- Player image is the primary hero visual, centered and large
- Origin crest on the left
- Direction arrow in the center
- Destination crest on the right
- Player name in a large high-contrast nameplate
- Confirmation strip: `CONFIRMED BY: <actual approved source>`
- Three information tiles:
  - `CONTRACT TERM`
  - `CONTRACT FEE`
  - `CONTRACT TYPE`
- Source footer: `SOURCE: <actual source>`
- FPL VORTEX branding footer

The visual hierarchy is deliberately simple: **player → direction → confirmation → contract facts → source**.

### INJURY

- Heading: **INJURY UPDATE**
- Heading/accent: `#FF3333` / red
- Prominent medical/injury symbol (cross/medical icon)
- Dark stadium/medical-tech background with restrained red glow
- Player image is the primary hero visual
- Club crest beside the player identity
- Right-side fact panel containing:
  - `INJURY`
  - `EXPECTED RETURN` — only when explicitly sourced
  - `STATUS`
- Confirmation strip: `CONFIRMED BY: <actual approved source>`
- Source footer: `SOURCE: <actual source>`
- FPL VORTEX branding footer

An injury card must never invent a diagnosis, return date, or availability status. Missing values render as `NOT REPORTED` or `NOT OFFICIALLY CONFIRMED`.

## 2. Universal rules

1. Output exactly **3840 × 2160**, 16:9.
2. Player image must resolve through the verified player identity. Prefer the canonical FPL player asset; use the existing approved fallback chain only when the identity can still be verified.
3. Club crests must come from canonical club resolution, never from arbitrary text matching.
4. Source text must be the actual source that produced the verification decision.
5. Never render placeholder values such as `unknown`, `null`, `n/a`, `[source]`, `[player]`, or invented numbers.
6. Never render `OFFICIAL`, `CONFIRMED`, or `COMPLETED` unless the corresponding verification policy has passed.
7. Contract fee and term are optional facts. If absent from the verified evidence, display `NOT DISCLOSED`; never estimate them.
8. Injury return dates are optional facts. If absent from the verified evidence, display `NOT OFFICIALLY CONFIRMED`; never calculate them.
9. The image and X caption must use the same verified player and club facts.
10. Rendering is deterministic and contains no model calls.

## 3. TRANSFER publication policy

A transfer card is publishable only after the fail-closed transfer gate returns `ALLOW`.

Required:

- verified player;
- canonical origin club with `RESOLVED` status;
- canonical destination club with `RESOLVED` status;
- no club conflict;
- approved confirmation source;
- explicit completion evidence;
- no speculation/pending language;
- final `validate_before_publish()` result is `ALLOW`.

The following **never** count as completed-transfer evidence by themselves:

- expected to join
- set to join
- close to joining
- likely to join
- could join
- interested in
- targeting
- bid / bid submitted
- talks / negotiations
- personal terms
- verbal agreement
- agreement reached
- medical booked
- medical scheduled
- medical pending
- here we go

A regex keyword alone can never promote a story to confirmed.

### Destination resolution

Destination resolution has exactly three states:

- `RESOLVED`
- `AMBIGUOUS`
- `UNKNOWN`

Only `RESOLVED` may continue.

Canonical examples:

| Source text | Required interpretation |
|---|---|
| `Inter` | `Inter Milan` only when context/database resolves it confidently |
| `Inter Milan` | `Inter Milan` |
| `Inter Miami` | `Inter Miami` |
| `PSG` | `Paris Saint-Germain` |
| `Barça` | `Barcelona` |
| `Barcelona` | `Barcelona` |
| `Milan` | `AMBIGUOUS` unless independently resolved |
| `United` | `AMBIGUOUS` unless independently resolved |
| unknown proper noun | `UNKNOWN` |

**Never choose the nearest Premier League club.** A club mentioned as interest, opposition, previous employer, or background context is not automatically a destination.

## 4. INJURY publication policy

Injury cards remain separate from transfer classification.

Required:

- verified player identity;
- verified current club;
- approved injury source;
- attributable factual injury/availability evidence;
- no invented diagnosis or return date;
- final publication safety gate passes.

Manager comments or media speculation cannot silently become a confirmed injury. If the source only says a player will be assessed, the card must say exactly that or be rejected; it must not invent a diagnosis.

## 5. Approved transfer card example

```text
TRANSFER CONFIRMED

[PLAYER IMAGE]

[ARSENAL CREST]  ARSENAL  →  INTER MILAN  [INTER CREST]

JOHN STONES

CONFIRMED BY: INTER MILAN

CONTRACT TERM     CONTRACT FEE       CONTRACT TYPE
5 YEARS           €28M + ADD-ONS     PERMANENT

SOURCE: INTER MILAN

FPL VORTEX
```

If contract term or fee is not present in verified evidence:

```text
CONTRACT TERM     NOT DISCLOSED
CONTRACT FEE      NOT DISCLOSED
```

## 6. Approved injury card example

```text
INJURY UPDATE   ✚

[PLAYER IMAGE]       [CLUB CREST] BUKAYO SAKA

INJURY
Hamstring injury

EXPECTED RETURN
NOT OFFICIALLY CONFIRMED

STATUS
Rehabilitation

CONFIRMED BY: ARSENAL FC
SOURCE: ARSENAL FC

FPL VORTEX
```

The exact facts shown above are illustrative only. Production values must come from the verified story object.

## 7. Rendering pipeline

```text
verified story
    ↓
HTML/CSS template
    ↓
Playwright at device scale
    ↓
3840 × 2160 PNG
    ↓
blank/size/upload-safety checks
    ↓
X posting gate
```

No X posting is performed by rendering tests. Tests use mocks/dry-run only.
