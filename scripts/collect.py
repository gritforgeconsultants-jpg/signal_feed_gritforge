#!/usr/bin/env python3
"""Run stage (b) collectors into the store. Standard library only.

    python scripts/collect.py list
    python scripts/collect.py run [--source sam-gov ...] [--today 2026-11-20]
    python scripts/collect.py run --record out/fixtures/      # live, and save responses
    python scripts/collect.py run --replay tests/fixtures/collectors/sam_gov.json --source sam-gov

Rules this runner enforces (collectors cannot bypass them):
  - A source runs only if its allowlist status is "active". Anything else is recorded
    as a skipped run and makes no request.
  - Blocked sources, and the four paid networks, never run.
  - A permit dataset whose newest record is older than 14 days raises stale_dataset and
    stores nothing from that run.
  - Failures become alerts: collector_error, schema_change, stale_dataset, zero_items
    (two finished runs in a row with no records).
  - Items go through Store.upsert_item, so the one gate decides them like ledger rows.

Env: SAM_API_KEY (sam-gov), SIGNAL_FEED_DB (optional).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store as S  # noqa: E402
from collectors import COLLECTORS  # noqa: E402
from collectors.base import STALE_AFTER_DAYS, CollectorError, SchemaError, collector_config  # noqa: E402
from collectors.http import Fetcher, FetchError, RecordingFetcher, ReplayFetcher  # noqa: E402


def run_source(store: S.Store, src: Dict[str, Any], fetcher: Fetcher, today: date) -> Dict[str, Any]:
    """Run one source's collector. Returns a summary; writes runs, alerts, items, documents."""
    cfg = collector_config(src)
    ctype = cfg.get("type")
    summary: Dict[str, Any] = {"source": src["id"], "type": ctype}
    fn = COLLECTORS.get(ctype)

    blocked = src["tier"] == "blocked" or any(S._hard_blocked(d) for d in src["domains"])
    if fn is None or blocked or src["status"] != "active":
        why = ("blocked source; never collected" if blocked else
               "unknown collector type %r" % ctype if fn is None else
               "status is %s, not active" % src["status"])
        rid = store.start_run("collect", src["id"], note=why)
        store.finish_run(rid, "skipped")
        if fn is None and not blocked:
            store.add_alert("collector_error", "%s: %s" % (src["id"], why), source=src["id"], run_id=rid)
        return {**summary, "status": "skipped", "note": why}

    rid = store.start_run("collect", src["id"])
    try:
        res = fn(src, fetcher, today)
    except SchemaError as exc:
        return _fail(store, rid, src, "schema_change", exc, summary)
    except (CollectorError, FetchError) as exc:
        return _fail(store, rid, src, "collector_error", exc, summary)
    except Exception as exc:  # a bug in a collector must still surface as an alert, not a silent gap
        return _fail(store, rid, src, "collector_error", "unexpected %s: %s" % (type(exc).__name__, exc),
                     summary)

    for w in res.warnings:
        store.add_alert("collector_error", "%s: %s" % (src["id"], w), source=src["id"], run_id=rid)

    if res.tracks_freshness:
        newest = res.newest_record
        if newest is None or (today - newest).days > STALE_AFTER_DAYS:
            msg = ("%s: newest record is %s (%s days old; limit %d). Nothing from this source "
                   "publishes until it is current." % (src["id"], newest or "unknown",
                                                       (today - newest).days if newest else "?",
                                                       STALE_AFTER_DAYS))
            store.add_alert("stale_dataset", msg, source=src["id"], run_id=rid)
            store.finish_run(rid, "stale")
            return {**summary, "status": "stale", "note": msg}

    counts = {"new": 0, "merged": 0, "bad": 0, "docs_new": 0}
    for it in res.items:
        try:
            counts[store.upsert_item(it, run_id=rid)] += 1
        except ValueError as exc:
            counts["bad"] += 1
            store.add_alert("schema_change", "%s: item not stored (%s): %s"
                            % (src["id"], exc, json.dumps(it, default=str)[:200]),
                            source=src["id"], run_id=rid)
    off_domain = 0
    for d in res.documents:
        counts["docs_new"] += store.upsert_document(d, run_id=rid)
        if not store.source_for_url(d["url"]) or store.source_for_url(d["url"])["id"] != src["id"]:
            off_domain += 1
    note = res.note
    if off_domain:
        note += ("; %d link(s) are on a host not listed for %s, so stage c cannot confirm "
                 "with them until that host is added" % (off_domain, src["id"]))

    store.finish_run(rid, "ok", items_in=res.fetched, items_new=counts["new"],
                     items_merged=counts["merged"], note=note)

    last = store.recent_runs(src["id"], n=2)
    if len(last) == 2 and all(r["status"] == "ok" and r["items_in"] == 0 for r in last):
        store.add_alert("zero_items", "%s returned no records two runs in a row" % src["id"],
                        source=src["id"], run_id=rid)
    return {**summary, "status": "ok", "fetched": res.fetched, "items": len(res.items),
            "documents": len(res.documents), **counts, "note": note}


def _fail(store: S.Store, rid: int, src: Dict[str, Any], kind: str, exc: Any,
          summary: Dict[str, Any]) -> Dict[str, Any]:
    msg = "%s: %s" % (src["id"], exc)
    store.add_alert(kind, msg, source=src["id"], run_id=rid)
    store.finish_run(rid, "error")
    return {**summary, "status": "error", "alert": kind, "note": msg}


def run_all(store: S.Store, fetcher: Fetcher, today: date,
            only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    out = []
    for src in store.sources():
        if only and src["id"] not in only:
            continue
        if not collector_config(src):
            continue
        out.append(run_source(store, src, fetcher, today))
    return out


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(S.DEFAULT_DB))
    p.add_argument("--allowlist", default=str(S.DEFAULT_ALLOWLIST))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="sources with a collector block and whether they would run")
    r = sub.add_parser("run")
    r.add_argument("--source", action="append", help="limit to these source ids")
    r.add_argument("--today", help="YYYY-MM-DD (default: today)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--record", metavar="DIR", help="save every response as a fixture under DIR")
    g.add_argument("--replay", metavar="FILE", help="serve responses from a fixture; no network")
    a = p.parse_args(argv)

    store = S.Store(a.db)
    try:
        store.load_allowlist(a.allowlist)
        if a.cmd == "list":
            for src in store.sources():
                cfg = collector_config(src)
                if cfg:
                    runs = "runs" if src["status"] == "active" and src["tier"] != "blocked" else "skips"
                    print("%-22s %-10s %-8s %-11s %s" % (src["id"], cfg.get("type"), src["tier"],
                                                         src["status"], runs))
            return 0

        today = date.fromisoformat(a.today) if a.today else date.today()
        fetcher: Fetcher = (ReplayFetcher(a.replay) if a.replay else
                            RecordingFetcher() if a.record else Fetcher())
        results = run_all(store, fetcher, today, a.source)
        for res in results:
            print(json.dumps(res, default=str))
        if a.record:
            out = Path(a.record) / ("collect-%s.json" % today.isoformat())
            fetcher.save(out)  # type: ignore[attr-defined]
            print("recorded %d responses to %s (keys redacted)" % (len(fetcher.records), out))  # type: ignore
        alerts = store.open_alerts()
        for al in alerts:
            print("ALERT [%s] %s" % (al["kind"], al["message"]))
        return 1 if any(r["status"] in ("error", "stale") for r in results) else 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
