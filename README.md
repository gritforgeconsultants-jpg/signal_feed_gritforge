# GritForge Signal Feed: build kit (Sunday 22 Nov 2026)

Build only. Nothing here sends mail, signs up for anything, or contacts anyone.
GritForge Consultants work; keep it out of Clark Iron systems and files.

## Drop-in
Copy this folder to the box (suggested: `/workspace/signal-feed/`). Python 3.9+,
standard library only.

## Files
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

## Run against the sample
    python scripts/signal_feed.py verify --ledger samples/sample-items.jsonl --out out/
    python scripts/signal_feed.py draft --out out/ --week-of 2026-11-27

## Run against the real ledger
    python scripts/signal_feed.py verify --ledger /workspace/commercial-growth/04-ledger/events.jsonl --since 2026-11-16 --out out/
    python scripts/signal_feed.py draft --out out/

If the ledger uses different field names, add them to `FIELD_ALIASES` at the top
of the script. Items need: date, place, state, action, source_url (plus
source_type when you know it).

## Hook into the Grok Bot routine
After the PM watch on Fridays: run `verify`, then `draft`, then email
`out/issue-review.txt` to yourself. You press Send on `issue-subscriber.txt`
until the send path is trusted. If `out/alerts.txt` is not empty, stop.
