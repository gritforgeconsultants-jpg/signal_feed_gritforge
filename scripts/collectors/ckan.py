"""CKAN DataStore permit collector. San Antonio first: data.sanantonio.gov.

San Antonio runs CKAN, not Socrata, so the Socrata-style /resource/ URLs 404 there.
One request a run (more only if the window spans several pages):
  /api/3/action/datastore_search?resource_id=...&sort="{date_field}" desc&limit=...

The newest row's date is the freshness check (14 days, as for Socrata). Rows are cut to
the lookback window and to the "match" rules on our side, since datastore_search cannot
filter a date range.

Each item links to its record through the API (an official data record):
  .../datastore_search?resource_id=...&filters={"<id_field>": "<permit id>"}
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List
from urllib.parse import urlencode

from .base import (STALE_AFTER_DAYS, CollectResult, CollectorError, ConfigError, SchemaError,
                   clean, collector_config, matches, permit_item, require_fields, to_day)
from .http import Fetcher

NEEDED = ("domain", "resource_id", "date_field", "id_field", "place", "state")


def collect(source: Dict[str, Any], fetcher: Fetcher, today: date) -> CollectResult:
    cfg = collector_config(source)
    missing = [k for k in NEEDED if not cfg.get(k)]
    if missing:
        raise ConfigError("%s collector block is missing %s (find it on %s)"
                          % (source["id"], ", ".join(missing), cfg.get("domain", "the portal")))
    api = "https://%s/api/3/action/datastore_search" % cfg["domain"]
    dfield, limit = cfg["date_field"], int(cfg.get("limit", 500))
    cutoff = today - timedelta(days=int(cfg.get("lookback_days", 7)))
    res = CollectResult(tracks_freshness=True)

    rows: List[Dict[str, Any]] = []
    for page in range(int(cfg.get("max_pages", 4))):
        data = fetcher.get_json(api, {"resource_id": cfg["resource_id"], "sort": '"%s" desc' % dfield,
                                      "limit": limit, "offset": page * limit})
        if not isinstance(data, dict) or not data.get("success"):
            err = data.get("error") if isinstance(data, dict) else data
            raise CollectorError("CKAN datastore_search failed: %s" % json.dumps(err)[:300])
        result = data.get("result") or {}
        if "records" not in result:
            raise SchemaError("CKAN result has no records (got: %s)" % ", ".join(sorted(result)))
        batch = result["records"]
        if page == 0:
            require_fields(batch, [cfg["id_field"], dfield, *cfg.get("required", [])],
                           cfg["resource_id"])
            res.newest_record = to_day(batch[0].get(dfield)) if batch else None
            if res.newest_record is None or (today - res.newest_record).days > STALE_AFTER_DAYS:
                return res
        rows.extend(batch)
        oldest = to_day(batch[-1].get(dfield)) if batch else None
        if len(batch) < limit or oldest is None or oldest < cutoff:
            break
    else:
        res.warnings.append("%s: window not reached after %s pages; raise max_pages"
                            % (cfg["resource_id"], cfg.get("max_pages", 4)))

    res.fetched = len(rows)
    for row in rows:
        pid, day = clean(row.get(cfg["id_field"])), to_day(row.get(dfield))
        if not pid or day is None or day < cutoff or not matches(row, cfg.get("match", {})):
            continue
        url = api + "?" + urlencode({"resource_id": cfg["resource_id"],
                                     "filters": json.dumps({cfg["id_field"]: pid})})
        it = permit_item(cfg, row, day, url)
        if it:
            res.items.append(it)
    res.note = "newest record %s; %d rows read, %d kept" % (res.newest_record, len(rows), len(res.items))
    return res
