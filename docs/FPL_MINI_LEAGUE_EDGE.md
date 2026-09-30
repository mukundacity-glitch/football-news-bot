# FPL Mini-League Edge

A separate workflow and editorial lane. It does not modify the existing news,
deadline or tactical workflows, their queues, or their posting state.

| New York time | Topic |
|---|---|
| 8 AM | The Mini-League Deciding Move (6–10 GW horizon) |
| Noon | The Ownership Trap of the Day |
| 5 PM Mon/Thu/Sun | The Hidden Fixture Swing |
| 5 PM Tue/Fri | The Chip Timing Edge |
| 5 PM Wed/Sat | The Structure Fix |

Native `America/New_York` scheduling follows daylight saving time. GitHub Actions
can delay or drop scheduled runs; these are requested start times, not guaranteed
publication times. Research and rendering finish before publication. A scheduled
run more than four hours late is rejected rather than posting the wrong edition.

## Setup

Add the `OPENAI_API_KEY` Actions secret with Responses/web-search and image API
access. This uses billed OpenAI API calls; the ChatGPT subscription is not an API
credential. Existing X secrets are reused: `X_POST_AUTH_TOKEN` plus
`X_POST_CT0_TOKEN`, or `X_AUTH_TOKEN` plus `X_CT0_TOKEN` as fallback. Values are
never stored in post files. The repository's established Twikit publishing and
confirmed receipt helpers are reused; this lane has no shared posting state.

Optional Actions variables:

- `FPL_EDGE_TEXT_MODEL`: defaults to `gpt-5.5`.
- `FPL_EDGE_IMAGE_MODEL`: defaults to `gpt-image-2`.
- `FPL_EDGE_PAUSED=true`: stop only this workflow's publishing job.

Open **Actions → FPL Mini-League Edge → Run workflow**, select a slot and keep
`dry_run=true` for the first end-to-end preview. Set it to false for a deliberate
live run. Scheduled runs publish automatically after validation. Ordinary code
pushes and pull requests run tests only and never publish.

## Packages and logo

Each successful package includes:

- `graphic.png`: exactly 1080×1080, relevant player portraits, charcoal/emerald/gold.
- `caption.txt`: one copyable description with exactly five ending hashtags,
  including #FPL and #FPLVortex, under 240 characters in total.
- `README.md`: image preview, download link and caption together in one code block.
- `post.json`, official snapshot, research memo, source URLs and review results.

The original supplied `44033(1).png` is copied to
`assets/branding/fpl_strategy_logo.png`. Its exact pixels are fitted proportionally
inside the top-left 320×160 area after generation; no generated replacement logo.
Player identity references are downloaded using current official FPL player codes.
The final graphic is reviewed for text correctness, identity, readability and fit.

Download the ZIP from the run's **Artifacts** section. Successful packages are also
committed under `fpl-edge-posts/YYYY-MM-DD/slot/` on the dedicated
`fpl-mini-league-edge-posts` branch, including full captions, images and X receipts.
No generated package commits go onto `main` or into another bot's queue.

## Delivery integrity

The script fetches official data every run, rejects stale deadlines, verifies
declared statistics against that snapshot, requires traceable research sources,
audits the decision and inspects the finished image. Unsupported insight or failed
validation stops publishing with an error rather than fabricating a post. No
automatic image-generation retries spend money after uncertain API outcomes.

Before X creation, `publishing` is durably pushed to the dedicated branch. A
confirmed receipt changes it to `published`. Failed or ambiguous delivery is
recorded as `delivery_uncertain` and is never blindly repeated. If the runner
stops after the reservation, the next run also stops for that edition. Reconcile
the account's timeline first, then edit that edition's `post.json` on the state
branch to `published` with the receipt, or `ready` ONLY after verifying no tweet
was accepted. X and Git are separate systems; this prioritises preventing a
duplicate over automatically retrying an uncertain publication.

Local verification:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -p test_fpl_mini_league_edge.py -v
python -m src.fpl_mini_league_edge --slot morning --dry-run
```

The local preview requires the OpenAI key and live network access, but never posts
to X. Use `--sync-state` only in a fresh checkout with repository write credentials.
