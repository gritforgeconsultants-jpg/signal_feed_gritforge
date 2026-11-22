"""Socrata (SODA) permit collector. Austin first: data.austintexas.gov.

Two requests a run:
  1. freshness:  /resource/{dataset}.json?$select=max({date_field}) AS newest
     If the newest record is older than 14 days, stop: the runner raises stale_dataset
     and nothing from this source is stored.
  2. window:     /resource/{dataset}.json?$where={date_field} >= '...' AND ({where})
                 &$order={date_field} DESC&$limit={limit}

Each item links to its own record through the API (an official data record):
  https://data.austintexas.gov/resource/{dataset}.json?{id_field}={permit id}

Everything dataset-specific lives in the allowlist entry's "collector" block, so a
renamed column is a config fix, not a code change. A required column missing from every
row raises schema_change instead of quietly producing empty items.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict
from urllib.parse import urlencode

from .base import (STALE_AFTER_DAYS, CollectResult, ConfigError, SchemaError, clean,
                   collector_config, matches, permit_item, require_fields, to_day)
from .http import Fetcher

NEEDED = ("domain", "dataset", "date_field", "id_field", "place", "state")


def collect(source: Dict[str, Any], fetcher: Fetcher, today: date) -> CollectResult:
    cfg = collector_config(source)
    missing = [k for k in NEEDED if not cfg.get(k)]
    if missing:
        raise ConfigError("%s collector block is missing %s" % (source["id"], ", ".join(missing)))
    base = "https://%s/resource/%s.json" % (cfg["domain"], cfg["dataset"])
    dfield, limit = cfg["date_field"], int(cfg.get("limit", 1000))
    res = CollectResult(tracks_freshness=True)

    fresh = fetcher.get_json(base, {"$select": "max(%s) AS newest" % dfield})
    if not isinstance(fresh, list):
        raise SchemaError("%s freshness query did not return a list" % cfg["dataset"])
    res.newest_record = to_day(fresh[0].get("newest")) if fresh else None
    if res.newest_record is None or (today - res.newest_record).days > STALE_AFTER_DAYS:
        return res  # the runner raises stale_dataset; do not pull rows from a stale dataset

    cutoff = today - timedelta(days=int(cfg.get("lookback_days", 7)))
    where = "%s >= '%sT00:00:00'" % (dfield, cutoff.isoformat())
    if cfg.get("where"):
        where += " AND (%s)" % cfg["where"]
    rows = fetcher.get_json(base, {"$where": where, "$order": "%s DESC" % dfield, "$limit": limit})
    if not isinstance(rows, list):
        raise SchemaError("%s window query did not return a list" % cfg["dataset"])
    require_fields(rows, [cfg["id_field"], dfield, *cfg.get("required", [])], cfg["dataset"])
    if len(rows) >= limit:
        res.warnings.append("%s: hit the %d-row limit; raise limit or shorten lookback_days"
                            % (cfg["dataset"], limit))

    res.fetched = len(rows)
    for row in rows:
        pid, day = clean(row.get(cfg["id_field"])), to_day(row.get(dfield))
        if not pid or day is None or not matches(row, cfg.get("match", {})):
            continue
        url = base + "?" + urlencode({cfg["id_field"]: pid})
        it = permit_item(cfg, row, day, url)
        if it:
            res.items.append(it)
    res.note = "newest record %s; %d rows in window, %d kept" % (
        res.newest_record, len(rows), len(res.items))
    return res
