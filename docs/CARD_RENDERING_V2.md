# FPL VORTEX — Verified Card System V2

All publishable cards use **3840×2160 (4K UHD), 16:9** and are rendered only from a `VerificationDecision` that is already authorized for publication.

## Card families

| Category | Accent | Header | Required visual cue |
|---|---|---|---|
| Transfer | `#00FF5A` | `TRANSFER CONFIRMED` | green confirmation check |
| Injury | `#FF3333` | `INJURY UPDATE` | red medical cross |
| Suspension | `#FFAA00` | `SUSPENSION UPDATE` | amber suspension/clock symbol |
| Press conference | `#00BFFF` | `PRESS CONFERENCE` | cyan microphone symbol |

## Transfer

Hierarchy: **player → origin → destination → contract facts → actual source**.

Visible fields:

- large verified player image;
- canonical origin crest/name;
- canonical destination crest/name;
- contract term;
- contract fee;
- contract type;
- `CONFIRMED BY: <actual source>`;
- `SOURCE: <actual source>`.

A missing fee or contract term is displayed as `NOT DISCLOSED`, never guessed.

## Injury

Visible fields:

- large verified player image;
- club crest/name;
- diagnosis/status only when grounded in evidence;
- expected return only when explicitly grounded;
- actual confirming source.

Missing diagnosis is `NOT REPORTED`. Missing return information is `NOT OFFICIALLY CONFIRMED`.

## Suspension

Visible fields:

- verified player image;
- club crest/name;
- suspension status;
- suspension length when sourced;
- return date when sourced;
- actual source.

No injury language or medical diagnosis is inferred from a suspension.

## Press conference

Visible fields:

- verified player/manager subject;
- club crest/name;
- short verified `WHAT WAS SAID` summary;
- optional verified topic;
- actual source.

The renderer never copies arbitrary article text into the card.

## Universal safety rules

1. A rejected/pending decision cannot render.
2. The renderer never changes verification status.
3. The renderer never invents player, club, fee, contract, diagnosis, return date, suspension length, quote, or source.
4. Player imagery continues through the verified player-image pipeline; failure degrades to a neutral silhouette rather than an unverified face.
5. Club crests are resolved from canonical verified club facts.
6. Source text is derived from the decision's source IDs, not a hard-coded publisher.
7. All four families share the same canvas, brand position, typography hierarchy, footer and safe margins.
8. Transfer publication remains first-party official/completed only. Trusted journalists and media can provide evidence but cannot be promoted to publication authority.
