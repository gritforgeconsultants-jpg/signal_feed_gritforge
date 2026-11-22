# GritForge Signal Feed

A paid weekly email of public construction signals (TX and CO first). Built in
stages; each stage stops for review.

| Stage | Scope | Status |
|---|---|---|
| a | SQLite store + tests | built |
| b | Collectors: SAM.gov, Austin/San Antonio open data | not started |
| c | Confirmation helper + admin queue | not started |
| d | Issue builder tiers | not started |
| e | Web app + Stripe test mode | not started |
| f | Delivery (REVIEW mode), alerts, schedule | not started |

## The rule

Only a source with `tier=official` AND `status=active` can put an item in a
paid issue. Trade press, local news and AI summaries are leads; their items
stay `unverified` until an official packet, minutes, filing, release or API
record confirms them. Dodge, ConstructConnect, BuildingConnected and PlanHub
are hard-blocked in code; the allowlist cannot unblock them.

The store enforces this a second time behind the gate:

- `upsert_item(..., gate=...)` takes the verify step's verdict but can only
  downgrade the domain verdict, never upgrade a lead or blocked link.
- `paid_eligible_items()` is the only query an issue builder should use. It
  re-checks each item's `official_url` against the live `sources` table, so
  pausing or blocking a source drops its items from the next issue.

## Store (stage a)

```bash
cd signal-feed
python scripts/store.py init
python scripts/store.py import path/to/events.jsonl --allowlist config/allowlist.json
python scripts/store.py stats
python scripts/store.py export out/audit.jsonl     # JSONL audit copy
```

The DB path defaults to `signal-feed/signal_feed.db`; override with
`SIGNAL_FEED_DB`. The DB and `out/` are gitignored.

Dedupe key: normalized `(state, place, project)`. A row whose first sighting
is within 14 days of an existing item merges into it (URLs, tags and size are
combined; a later official link upgrades a held item and the lead URL is kept
as the secondary link). Outside 14 days it is a new item.

### Turning a source active

Set `"status": "active"` on its entry in `config/allowlist.json` and re-run
`import` (which re-syncs sources). An official source without a status
defaults to `to_confirm`, which never publishes.

## Tests

```bash
python -m unittest discover -s signal-feed/tests -v
```

Standard library only, Python 3.9+. CI runs this on every push
(`.github/workflows/signal-feed.yml`).
