# Mon 23 / Tue 24 Nov: confirm the two held items

## Red Oak (Ellis County, TX). Held: trade press only
- [ ] Find Red Oak City Council's official agenda page; add its domain to `red-oak-council` in the allowlist and set status `active`
- [ ] Open the 17 Nov 2026 packet or adopted minutes
- [ ] Confirm: 4-1 vote, Compass Datacenters parcels added, ~830 to ~886 acres
- [ ] Keep the pending-lawsuit note on the item
- [ ] Paste the official link into the ledger item as `source_url`, set `source_type: official`
- Result: confirmed / held / dropped, link: ______

## Rosenberg (Fort Bend County, TX). Unverified: AI-written summary
- [ ] Find Rosenberg City Council's official agenda page; add its domain to `rosenberg-council`
- [ ] Open the 17 Nov 2026 packet or minutes
- [ ] Confirm: 6-0 vote, Union Pacific development agreement, 2,040 acres, Mainline Texas Industrial Park
- [ ] Keep ~20M sq ft and ~$2.5B labeled as projections
- [ ] If confirmed: replace the myopentexas.org link with the official one
- Result: confirmed / held / dropped, link: ______

## Aspen (CO). Needs the official link copied in
- [ ] Copy the official ballot text and results link for Ballot Issue 2A from the 20 Nov PM watch
- [ ] Add the domain to `aspen-city` and set status `active`

## Source confirm queue (TX/CO, from the 22 Nov ledger dry run)
Ranked by held ledger items each source would verify on the next import. For each:
open the site, find the official page type (packet, minutes, filing, release), read
the terms and robots.txt, then in `config/allowlist.json` fill `terms_checked` with
the date and set `status` to `active`. 42 of the 184 held TX/CO items are reachable
this way; the other 142 cite news, press releases or EIA and need stage c.

| # | Source id | Domain | Items | Note |
|---|---|---|---|---|
| 1 | `tdlr-tabs` | tdlr.texas.gov | 26 | Approving the links is enough; automating TABS is a separate decision |
| 2 | `tx-econ-dev` | gov.texas.gov | 5 | Confirm businessintexas.com too |
| 3 | `arvada-city` | arvadaco.gov | 2 | |
| 4 | `san-antonio-permits` | data.sanantonio.gov | 1 | Also unlocks the SA permit collector once its block is filled in |
| 5 | `co-oedit` | oedit.colorado.gov | 1 | |
| 6 | `codot` | codot.gov | 1 | |
| 7 | `tx-attorney-general` | texasattorneygeneral.gov | 1 | Bond approvals count; opinions may not |
| 8 | `taylor-tx-city` | taylortx.gov | 1 | |
| 9 | `cameron-county-tx` | cameroncountytx.gov | 1 | |
| 10 | `hudspeth-county-tx` | co.hudspeth.tx.us | 1 | |
| 11 | `briscoe-county-tx` | co.briscoe.tx.us | 1 | |
| 12 | `aubrey-tx-city` | aubrey.city | 1 | Not .gov: confirm it is the city's site first |

Not on this list on purpose:
- EIA (10 TX/CO items): context data, never a job source. Fix on the growth-watch side.
- Ben Taub (Houston): sourced from construction.com (Dodge). Blocked; the routine should stop reading it.
- Other states' .gov domains (gov.georgia.gov, governor.ny.gov, ...): waiting on the footprint decision.
