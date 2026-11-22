# GritForge Signal Feed

GritForge Signal Feed finds new commercial construction work before it hits the bid boards. It pulls public signals such as SAM.gov opportunities, city permit data, and council agendas and minutes, dedupes them, and runs every item through a verification gate.

Only items confirmed by an official packet, filing, release, or official data record go out. Anything from trade press, local news, or AI-written summaries stays held as unverified until it's confirmed.

Subscribers get a Friday digest by tier and state, and every item links to its official source. Each issue goes to review before it's sent.

## Build kit (Sunday 22 Nov 2026)

Build only. Nothing here sends mail, signs up for anything, or contacts anyone.
GritForge Consultants work; keep it out of Clark Iron systems and files.

### Drop-in
Clone this repo to the box (suggested: `/workspace/signal-feed/`). Python 3.9+,
standard library only.

### Files
- `config/allowlist.json` - TX / CO / federal source allowlist. Only `official` + `active` sources can put an item in a paid issue. Most are `to_confirm` until you open each site and check its terms.
- `docs/verification-rule.md` - the one-paragraph rule the routine follows.
- `docs/qc-checklist.md` - the weekly hour plus same-day failure alerts.
- `docs/confirm-worksheet.md` - Mon/Tue checklist for Red Oak, Rosenberg, Aspen.
- `scripts/signal_feed.py` - `verify` (gate + dedupe + alerts) and `draft` (subscriber issue + internal review copy).
- `templates/friday-email.txt` - Friday email, your voice.
- `samples/sample-items.jsonl` - Red Oak, Rosenberg, Aspen from the 20 Nov watches.
- `samples/test-fixtures.jsonl` - fake TEST items that exercise every gate path.
- `drafts/landing-page.html`, `drafts/outreach-drafts.md` - not published, not sent.
- `out/` - the sample run (see below).

### Run against the sample
    python scripts/signal_feed.py verify --ledger samples/sample-items.jsonl --out out/
    python scripts/signal_feed.py draft --out out/ --week-of 2026-11-27

### Run against the real ledger
    python scripts/signal_feed.py verify --ledger /workspace/commercial-growth/04-ledger/events.jsonl --since 2026-11-16 --out out/
    python scripts/signal_feed.py draft --out out/

If the ledger uses different field names, add them to `FIELD_ALIASES` at the top
of the script. Items need: date, place, state, action, source_url (plus
source_type when you know it).

### Hook into the Grok Bot routine
After the PM watch on Fridays: run `verify`, then `draft`, then email
`out/issue-review.txt` to yourself. You press Send on `issue-subscriber.txt`
until the send path is trusted. If `out/alerts.txt` is not empty, stop.

---

## Production build (stages a to f)

| Stage | Scope | Status |
|---|---|---|
| a | SQLite store + tests | built |
| b | Collectors: SAM.gov, Austin/San Antonio open data | not started |
| c | Confirmation helper + admin queue | not started |
| d | Issue builder tiers | not started |
| e | Web app + Stripe test mode | not started |
| f | Delivery (REVIEW mode), alerts, schedule | not started |

### Store (stage a): `scripts/store.py`

SQLite (`signal_feed.db`, override with `SIGNAL_FEED_DB`) with tables items,
sources, runs, alerts, subscribers, issues, sends. Standard library only.

    python scripts/store.py import /workspace/commercial-growth/04-ledger/events.jsonl
    python scripts/store.py stats
    python scripts/store.py export out/audit.jsonl     # JSONL audit copy

`import` re-syncs `sources` from `config/allowlist.json` on every run, then
loads the ledger. It reads the growth-watch field names directly
(`observed_at`, `metro_or_submarket`, `name`) through `FIELD_ALIASES`.

**One gate.** `signal_feed.classify()` decides verified or held. The store
only tightens it:
- Dodge, ConstructConnect, BuildingConnected and PlanHub are blocked in code;
  an allowlist edit cannot unblock them, and a blocked item stays blocked.
- `paid_eligible_items()` is the only query subscriber copy may use. It
  re-checks each official link against the live allowlist, so setting a
  source back to `to_confirm` pulls its items from the next issue.

**Dedupe.** Normalized (state, place, project); a sighting within 14 days of
the first one merges into it. With no project, the first six words of the
action stand in. When an official link arrives for a held item, the item is
verified and the lead link is kept as the secondary link. This replaces
`out/seen_keys.txt` for the store path.

**Turning a source active.** In `config/allowlist.json`, fill in `domains`,
`terms_checked` (the date you read the terms and robots.txt) and set
`"status": "active"`. The next `import` picks it up.

### Tests

    python -m unittest discover -s tests -v

CI runs this on every push (`.github/workflows/tests.yml`, Python 3.9
and 3.12).
