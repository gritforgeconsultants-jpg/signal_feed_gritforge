# Handoff: GritForge Signal Feed build (as of 2026-11-22)

Read this first, then `README.md`, `docs/verification-rule.md`, `scripts/signal_feed.py` and `scripts/store.py`.

## What this is

A paid weekly email of public construction signals (Texas and Colorado first) for subs, suppliers, rental shops and estimators. GritForge Consultants product. **Keep it fully separate from Clark Iron**: no Clark Iron files, rates, contacts, bid lists, data or code imports. The Clark Iron bid engine (`gritforgeconsultants-jpg/clean_web_engine`) may be read for patterns only; copy logic over, never import from it.

## Where things are

- Repo: `gritforgeconsultants-jpg/signal_feed_gritforge`
- Work branch: `claude/wizardly-thompson-932ype` (not merged to `main` yet; `main` has only the original README)
- Tests: `python -m unittest discover -s tests -v` (stdlib only, Python 3.9+). 67 passing. CI: `.github/workflows/tests.yml`, 3.9 and 3.12, every push.

## Build order (stop after each stage for Arron's review)

| Stage | Scope | Status |
|---|---|---|
| a | SQLite store + tests | **built, awaiting review** |
| b | Collectors: SAM.gov, Austin/San Antonio open data | next |
| c | Confirmation helper + admin queue | |
| d | Issue builder tiers | |
| e | Web app (Next.js on Vercel) + Stripe test mode | |
| f | Delivery in REVIEW mode + alerts + schedule | |

## The core rule (do not change)

Only sources with `tier=official` AND `status=active` in `config/allowlist.json` can put an item in a paid issue. Trade press, local news and AI summaries are leads only; their items stay Unverified until an official packet, minutes, filing, release or official API record confirms them. Never touch Dodge, ConstructConnect, BuildingConnected, PlanHub.

## Stage a: what was built

- `scripts/signal_feed.py` is the stage 1 kit (verify + draft). One change: `FIELD_ALIASES` now maps the growth-watch ledger's `observed_at`, `metro_or_submarket`, `name`; canonical fields win over aliases; ISO timestamps are cut to the day. Before this, every real-ledger item was held as "missing date, place" and distinct items collapsed to the same dedupe key.
- `scripts/store.py`: SQLite store (`signal_feed.db`, env `SIGNAL_FEED_DB`). Tables items, sources, runs, alerts, subscribers, issues, sends. CLI: `import LEDGER`, `stats`, `export OUT.jsonl` (audit copy).
  - **One gate:** `signal_feed.classify()` decides. The store only tightens: hard-coded block of the four paid networks (allowlist cannot unblock), caller verdicts are downgrade-only, blocked is sticky across merges.
  - `paid_eligible_items()` is the **only** query subscriber copy may use. It re-checks each `official_url` against the live sources table, so pausing a source drops its items.
  - Dedupe: normalized (state, place, project), 14-day window anchored on earliest sighting; no project falls back to first 6 words of action. A later official link upgrades a held item and keeps the lead URL as `lead_url`.
  - `confirm_item(id, official_url)` exists for stage c; refuses anything not official+active.
  - Alerts written: `schema_change` (unstorable row), `collector_error` (import failed), `marked_confirmed_unverified`.

## Findings Arron has been told about

1. Only **SAM.gov** is official+active. The other 16 official sources are `to_confirm`, so a real dry run today yields federal items only. Confirming sources is the product bottleneck.
2. Kit `verify` re-run into the same `out/` marks everything as seen via `out/seen_keys.txt` and produces an empty issue. Stage d should build from the store, which removes this.
3. Dedupe normalization splits abbreviations ("H.Q." vs "HQ"); pinned in a test, acceptable for now.

## Open items (ask Arron if not yet answered)

1. **Real ledger** (`/workspace/commercial-growth/04-ledger/events.jsonl`) not yet provided; needed for the stage a dry run and the final "done" check. Only one real example of the format has been seen.
2. **`tools/sam_pull.py`** is referenced in the allowlist as "already wired" but was not in the kit. If absent, build the SAM.gov collector from patterns in `clean_web_engine/cwe/intel/sam_gov.py` (query logic only).
3. Stage a review sign-off.
4. Dedupe window anchored on first sighting (a vote 15+ days after the announcement is a new item). Arron has not objected.
5. Clark Iron repo still has the Signal Feed commits on its unmerged `claude/wizardly-thompson-932ype` branch (force-push was blocked). Arron is deleting that branch or leaving it unmerged. Do not touch it from here.

## Stage b spec (next)

Collectors, one module per source type, each returning ledger-format items with `source_type` set:
- **SAM.gov Opportunities API**: key from env `SAM_API_KEY`; construction NAICS 236, 237, 238; TX/CO places of performance.
- **Socrata/CKAN permits**: Austin (`data.austintexas.gov`), San Antonio (`data.sanantonio.gov`) first. Per-dataset freshness check: newest record older than 14 days raises a `stale_dataset` alert and that source does not publish.
- **Agenda/minutes link finder** for allowlisted councils (Legistar, CivicPlus, Granicus): record agenda and packet URLs only, no bulk scraping, respect robots.txt, 1 request per 5 seconds per host.
- A source only runs when its allowlist status is `active`. Both permit sources are currently `to_confirm`: build and test them, but they will not run until Arron confirms terms and flips them.
- Write collector runs to `runs`, failures to `alerts`, items through `Store.upsert_item`.
- Tests with recorded fixtures, no live network in CI.

## Later stages (full spec)

- **c. Confirmation helper:** for each held item, search the matching allowlisted official domain for the place and date and propose a candidate official URL into `out/confirmations.jsonl` (`approve` true/false). Approved items get `source_type=official` and the official URL; lead URL kept as secondary (`Store.confirm_item`). Admin queue in stage e writes the same file/table.
- **d. Issue builder:** HTML and plain text per tier. Regional Digest ($49 est.): one state, weekly. Trade Watch ($99 est.): adds trade tags (steel, concrete, site work, electrical, MEP) and per-subscriber keyword filters. National Desk ($199 est.): all states plus same-day alert for items over a size threshold. Every item shows its official source link and a "public information, not a bid invitation, confirm before acting" footer. Voice: plain, semicolons not em dashes, no AI filler, signed "Regards, Arron Fain, GritForge Consultants"; phone 720-205-2859 in the signature only. Use `templates/friday-email.txt` as the voice reference.
- **e. Web app (Next.js on Vercel):** landing page from `drafts/landing-page.html`, sample issue page, signup with Stripe Checkout (three subscription prices, **test mode only**). Stripe webhook adds/updates/cancels subscriber (tier, states, trade tags). Preferences page via magic link, no passwords. Admin page behind Vercel Authentication: held queue with approve/reject writing the confirmation, run history, alerts.
- **f. Delivery:** Resend or Postmark (env key). Default **REVIEW** mode: Friday builds the issue, emails the review copy to Arron only, and waits; a send happens only on "Approve and send" in admin. AUTO flag off by default; may only be enabled after 4 straight weeks of approved issues with no corrections. List-Unsubscribe headers and a physical mailing address field (CAN-SPAM). Schedule (Vercel Cron or GitHub Actions): collect weekdays 9 AM and 3 PM America/Denver; verify and draft Friday after the 3 PM run. Failure alerts to Arron same day: collector errors, stale datasets, zero items 2 runs in a row, schema changes, failed sends, Stripe webhook failures. Open alerts on Friday block the issue.
- **Tests throughout:** keep the fixtures passing; cover every gate path, dedupe, freshness, tier filters, the webhook, and that blocked and `to_confirm` sources can never reach a subscriber issue.

## Constraints

- Stripe test mode and a staging domain only. Do not go live, send to real subscribers, or contact anyone.
- Secrets in env vars only; nothing hard-coded or committed.
- Monthly running cost under about $50 at launch (Vercel hobby/pro, SQLite or Vercel Postgres, Resend free tier).
- Small, reviewable commits. Do not merge to `main` or open a PR unless Arron asks.

## Done means

- A dry run on the real ledger produces the review copy, the subscriber copy per tier, and an alerts file, with zero unconfirmed items in any subscriber copy.
- Landing page and checkout work end to end in Stripe test mode on staging.
- README explains setup, env vars, how to approve an issue, how to turn a source active.
- Report per stage: what was built, test results, env vars needed, open decisions.

## Env vars so far

- `SIGNAL_FEED_DB` (optional). Stage b adds `SAM_API_KEY`.
