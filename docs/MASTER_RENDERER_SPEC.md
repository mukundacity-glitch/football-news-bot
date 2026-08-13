# FPL VORTEX Master Renderer Specification

The four files under `assets/reference/` are the visual authority:

- `injury_reference.png`
- `press_conference_reference.png`
- `suspension_reference.png`
- `transfer_reference.png`

## Fixed framework

- RGB PNG, 3840×2160, 16:9.
- Header height approximately 365px.
- FPL VORTEX branding always left; Premier League branding always right.
- Category heading centered in an angled broadcast banner.
- Thin accented divider beneath the header.
- Footer height approximately 260px with fixed source, X, updated date,
  data-driven decisions and YouTube zones.
- Dark stadium atmosphere, near-black readability panels, broadcast lighting.

## Dynamic body

- No player, club, value, date, category fact or source is hardcoded.
- Data rows are calculated from available verified facts.
- Generic row structure: icon | cyan uppercase label | white value.
- Row count, height and spacing adapt to supplied fields.
- Long text is fitted, wrapped or truncated inside its allocated region.
- No compressed details paragraph.

## Transfer invariants

- `club_from_name` is rendered on the left as FROM.
- `club_to_name` is rendered on the right as TO.
- Direction is never inferred from nationality, fixture data or visual assets.
- FROM and TO club crests are resolved independently.
- Player position is displayed in large white text at the foot of the visual area.

## Image safety

Priority:

1. FPL API
2. Structured official/provider identity
3. FotMob provider ID
4. Identity-matched Wikipedia footballer page

No fuzzy image search is used. Missing images degrade to a neutral silhouette and
verified club crest rather than a guessed face or incorrect shirt.

## Quality gates

- Exact 3840×2160 RGB canvas.
- Non-flat image.
- Fixed header/footer preserved.
- No text outside allocated regions.
- No unauthorized decision can render.
- Live workflow remains disabled until preview approval.
