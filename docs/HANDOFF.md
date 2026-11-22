# Handoff: GritForge Signal Feed build (as of 2026-11-22, after stage b)

Read this first, then `README.md`, `docs/verification-rule.md`, `scripts/signal_feed.py` and `scripts/store.py`.

## What this is

A paid weekly email of public construction signals (Texas and Colorado first) for subs, suppliers, rental shops and estimators. GritForge Consultants product. **Keep it fully separate from Clark Iron**: no Clark Iron files, rates, contacts, bid lists, data or code imports. The Clark Iron bid engine (`gritforgeconsultants-jpg/clean_web_engine`) may be read for patterns only; copy logic over, never import from it.

## Where things are

- Repo: `gritforgeconsultants-jpg/signal_feed_gritforge`
- Work branch: `claude/wizardly-thompson-932ype` (not merged to `main` yet; `main` has only the original README)
- Tests: `python -m unittest discover -s tests -v` (stdlib only, Python 3.9+). 120 passing (checked on 3.9, 3.12 and 3.13). CI: `.github/workflows/tests.yml`, 3.9 and 3.12, every push.

## Build order (stop after each stage for Arron's review)

| Stage | Scope | Status |
|---|---|---|
| a | SQLite store + tests | approved |
| b | Collectors: SAM.gov, Austin/San Antonio open data, agenda finder | **built, awaiting review** |
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

## Stage b: what was built

- `scripts/collect.py` runner; `scripts/collectors/` one module per source type (`sam_gov`, `socrata`, `ckan`, `agendas`), plus `http.py` and `base.py`. A collector returns ledger-format items; only the runner writes to the store.
- Runner rules: active-only (else a `skipped` run, no request); blocked and hard-blocked never run; alerts `collector_error`, `schema_change`, `stale_dataset`, `zero_items`; stale permit runs store nothing; exit 1 on error/stale.
- HTTP: 1 request / 5 s / host on every call; robots.txt on crawled feeds (RFC 9309: 4xx allow, 5xx or unreachable disallow); keys redacted everywhere; `RecordingFetcher` / `ReplayFetcher`.
- SAM.gov: per-state queries, NAICS filtered client-side, amendments merge via solicitation number in `project`. Award amount goes to `size_usd` and is labeled as the award amount.
- Permits: dataset details live in the allowlist `collector` block. Freshness query first. Valuation 0 means not filed; the $250k floor only drops permits with a known value. Project = site address, so building and addition permits on one site merge.
- Agenda finder: Legistar Web API and CivicPlus/Granicus RSS; links go to the new `documents` table (schema v2), never items. 45-day lookback on meeting date because minutes lag.
- Gate fix in `signal_feed.match_source()`: most specific domain wins (it used to be first-listed). The store now refuses bare vendor domains (`legistar.com` etc.) with an `allowlist_error` alert.
- Fixtures in `tests/fixtures/collectors/` are hand-built from documented response shapes, not live recordings (no network to these hosts from the build box). Regenerate with `tests/fixtures/make_collector_fixtures.py`; replace with `collect.py run --record` output once each source is live.

## Findings Arron has been told about

1. Only **SAM.gov** is official+active. The other 16 official sources are `to_confirm`, so a real dry run today yields federal items only. Confirming sources is the product bottleneck.
2. Kit `verify` re-run into the same `out/` marks everything as seen via `out/seen_keys.txt` and produces an empty issue. Stage d should build from the store, which removes this.
3. Dedupe normalization splits abbreviations ("H.Q." vs "HQ"); pinned in a test, acceptable for now.
4. (stage b) First-match domain lookup could have let to_confirm Austin permits pass through the council entry; fixed.
5. (stage b) San Antonio is CKAN, not Socrata; the Socrata URL in the Clark Iron permits module is why it 404'd there. Resource id and columns still unknown.
6. (stage b) The Clark Iron SAM module sends NAICS as `naicsCode=` with a comma list; the documented parameter is `ncode`, one code. Worth checking on that side; not touched from here.
7. (stage b) Alerts are not deduplicated: a source failing every run adds an alert each run. Stage f should collapse repeats.

## Open items (ask Arron if not yet answered)

1. **Real ledger** (`/workspace/commercial-growth/04-ledger/events.jsonl`) still not provided; stage a dry run not done. Needed for the final "done" check.
2. **`tools/sam_pull.py`** was not provided; the SAM collector was built new from the query pattern in `clean_web_engine` (copied, not imported). If the script exists, compare its query before retiring it.
3. Stage b review sign-off.
3a. **First live SAM run** needs `SAM_API_KEY` and network to api.sam.gov; run with `--record` and compare to the fixture. Also set `terms_checked` on sam-gov (active with an empty date today).
3b. Austin and San Antonio stay `to_confirm` until Arron reads terms. San Antonio also needs its resource id and columns.
3c. No council has a collector block yet: each needs its platform and vendor subdomain found by hand.
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

- `SIGNAL_FEED_DB` (optional), `SAM_API_KEY` (sam-gov collector). Optional: a Legistar token via the env var named in a block's `token_env`.
