# Verification rule

An item can go to a paying subscriber only if its source link points to an
official record: a council or commission packet, adopted minutes, a filing, an
official government or EDC release, or an official API record. The source must
be on the allowlist with tier `official` and status `active`.

Everything else goes to the Unverified queue: trade press, AI-written meeting
summaries, secondary blogs, sources not on the allowlist, and any item missing a
date, a place, or a source link. An Unverified item is never sent as confirmed.
Arron clears the queue in the weekly hour: confirm (attach the official link),
hold, or drop.

Dollar figures and acreage from a developer or a summary are labeled as
projections. A rezoning vote is not a start-of-construction date.

The routine applies this with `scripts/signal_feed.py verify`. Passed items land
in `out/verified.jsonl`; held items land in `out/held.jsonl` with the reason. If
an item claims `status: confirmed` but its source is not official, the script
writes a failure alert to `out/alerts.txt`.
