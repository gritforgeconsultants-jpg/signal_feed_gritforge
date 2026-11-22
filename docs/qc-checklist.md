# Weekly QC hour (Friday, before the digest goes out)

1. Run `verify` then `draft` (see README) and open `out/issue-review.txt`.
2. Pick five items from the subscriber issue at random and open each official
   link. If two fail, stop the send and review the whole issue.
3. Clear the Unverified queue (`out/held.jsonl`): confirm, hold, or drop.
4. Skim `out/dupes.jsonl`. If two names for the same vote slipped through,
   tighten the dedupe key.
5. Check every item has a date, a place, and a link.
6. Read any subscriber replies.
7. Monthly: re-read the allowlist and each site's terms and robots.txt.

# Failure alerts (fix the same day)

- A collection run failed, or returned zero items from a source that was up yesterday
- An Unverified item went out marked confirmed (`out/alerts.txt`)
- A subscriber was mailed twice in one cycle
- A source started blocking the collector
- Delivery bounced or landed in spam for more than one subscriber
- Ledger write failed
- SAM.gov or TDLR pull errored
