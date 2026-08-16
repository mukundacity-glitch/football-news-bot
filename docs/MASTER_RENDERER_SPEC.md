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
- Player cards use a large responsive name hero, with the final name segment in
  the category accent color and compact verified club/age metadata above it.
- Data rows are calculated from available verified facts and use a distinct
  high-contrast accent rail, icon, uppercase label and large white value.
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
2. Identity-matched Wikipedia footballer page
3. Reliable structured provider using an exact player ID
4. Generic team shirt built from the player's verified FPL/current club

No fuzzy image search is used. The final shirt is explicitly labelled as a team
shirt fallback and never presents a generated face or an unverified club identity.

## Quality gates

- Exact 3840×2160 RGB canvas.
- Non-flat image.
- Fixed header/footer preserved.
- No text outside allocated regions.
- No unauthorized decision can render.
- Live workflow remains disabled until preview approval.
