"""SQLite store for GritForge Signal Feed (stage a).

Standard library only, Python 3.9+. Replaces the flat JSONL files as the
working store; JSONL stays as the audit export.

Tables: items, sources, runs, alerts, subscribers, issues, sends, documents.

One gate: signal_feed.classify() decides verified vs held, using the
sources table (synced from config/allowlist.json). The store adds a backstop
that can only make things stricter:
  - Dodge, ConstructConnect, BuildingConnected, PlanHub are blocked in code;
    the allowlist cannot unblock them.
  - A caller-supplied verdict can downgrade, never upgrade.
  - paid_eligible_items() re-checks each official_url against the live
    sources table, so pausing a source pulls its items from the next issue.

CLI (run from signal-feed/):
    python scripts/store.py import LEDGER.jsonl [--allowlist config/allowlist.json]
    python scripts/store.py export out/audit.jsonl
    python scripts/store.py stats
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import signal_feed as SF  # noqa: E402  (the stage 1 kit: aliases + gate)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("SIGNAL_FEED_DB", ROOT / "signal_feed.db"))
DEFAULT_ALLOWLIST = ROOT / "config" / "allowlist.json"
# The growth-watch ledger lives on the Grok Bot machine and is owned by that routine.
# Signal Feed only ever reads it (opened "r"); never move, edit or copy it into the repo.
LEDGER_ENV = "LEDGER_PATH"

SCHEMA_VERSION = 2  # v2: documents table (agenda/minutes links from stage b)
DEDUPE_WINDOW_DAYS = 14

HARD_BLOCKED_DOMAINS = ("construction.com", "constructconnect.com",
                        "buildingconnected.com", "planhub.com")

# Hosting vendors that serve many cities from one apex domain. Listing the apex on a
# source would make every city on that vendor pass as that source, so the store drops
# it. List the city's own subdomain instead (sanantonio.legistar.com, not legistar.com).
SHARED_HOST_APEXES = ("legistar.com", "granicus.com", "civicplus.com", "civicweb.net",
                      "municode.com", "novusagenda.com", "boarddocs.com", "primegov.com",
                      "arcgis.com", "socrata.com", "amazonaws.com", "cloudfront.net",
                      "googleusercontent.com", "box.com", "dropbox.com")

GATE_VERIFIED = "verified"
GATE_UNVERIFIED = "unverified"
GATE_BLOCKED = "blocked"
GATE_STATUSES = (GATE_VERIFIED, GATE_UNVERIFIED, GATE_BLOCKED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,                       -- allowlist id, e.g. sam-gov
    name TEXT NOT NULL,
    region TEXT,
    tier TEXT NOT NULL,                        -- official | lead | ai_summary | context | blocked
    status TEXT NOT NULL,                      -- active | to_confirm | ...
    domains TEXT NOT NULL DEFAULT '[]',
    method TEXT,
    cadence TEXT,
    terms_checked TEXT,
    notes TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',    -- any other allowlist keys (dataset ids, agenda urls)
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                        -- import | collect | verify | draft | send
    source_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',    -- running | ok | error
    items_in INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_merged INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    match_key TEXT NOT NULL,                   -- state|place|project, normalized
    date TEXT NOT NULL,                        -- YYYY-MM-DD, earliest sighting (dedupe anchor)
    last_seen_at TEXT NOT NULL,
    state TEXT NOT NULL,
    place TEXT NOT NULL,
    county TEXT,
    project TEXT,
    action TEXT,
    why TEXT,
    notes TEXT,
    source_name TEXT,
    source_type TEXT,
    gate_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (gate_status IN ('verified','unverified','blocked')),
    gate_reason TEXT,                          -- verify_note or hold_reason from the gate
    official_url TEXT,                         -- only ever set from an official+active source
    lead_url TEXT,                             -- first non-official link, kept as secondary
    source_urls TEXT NOT NULL DEFAULT '[]',
    trade_tags TEXT NOT NULL DEFAULT '[]',
    size_usd REAL,
    raw_json TEXT NOT NULL,                    -- first ledger row as received
    first_run_id INTEGER REFERENCES runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_match ON items(match_key, date);
CREATE INDEX IF NOT EXISTS ix_items_gate ON items(gate_status, state);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    kind TEXT NOT NULL,   -- collector_error | stale_dataset | zero_items | schema_change | send_failed
                          -- | webhook_failed | marked_confirmed_unverified
    source TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL CHECK (tier IN ('regional','trade','national')),
    states TEXT NOT NULL DEFAULT '[]',
    trade_tags TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',     -- active | past_due | canceled
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    issue_date TEXT NOT NULL,
    tier TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',            -- '' for national
    status TEXT NOT NULL DEFAULT 'draft',      -- draft | review_sent | approved | sent | blocked
    item_ids TEXT NOT NULL DEFAULT '[]',
    html TEXT,
    text TEXT,
    approved_by TEXT,
    approved_at TEXT,
    corrections INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (issue_date, tier, state)
);

CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    subscriber_id INTEGER REFERENCES subscribers(id),
    to_email TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'subscriber',   -- review | subscriber | alert
    status TEXT NOT NULL DEFAULT 'queued',     -- queued | sent | failed
    provider_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,                    -- agenda, minutes and packet links; never items
    source_id TEXT NOT NULL,
    platform TEXT,                             -- legistar | civicplus | granicus
    body TEXT,                                 -- e.g. City Council
    meeting_date TEXT,                         -- YYYY-MM-DD
    kind TEXT NOT NULL,                        -- agenda | minutes | packet | meeting
    title TEXT,
    url TEXT NOT NULL UNIQUE,
    first_run_id INTEGER REFERENCES runs(id),
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_src ON documents(source_id, meeting_date);
"""

_PUNCT = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")
_SOURCE_COLS = ("id", "name", "region", "tier", "status", "domains", "method",
                "cadence", "terms_checked", "notes")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_text(value: Any) -> str:
    s = str(value or "").lower().replace("&", " and ")
    return _WS.sub(" ", _PUNCT.sub(" ", s)).strip()


def match_key(state: str, place: str, project: str) -> str:
    return "|".join((norm_text(state), norm_text(place), norm_text(project)))


def parse_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s:
        raise ValueError("missing date")
    try:
        return date.fromisoformat(s[:10])
    except ValueError as exc:
        raise ValueError("unparseable date: %r" % s) from exc


def _hard_blocked(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in HARD_BLOCKED_DOMAINS)


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return None


class Store:
    def __init__(self, path: Any = DEFAULT_DB):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.init()

    def init(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("db schema v%d is newer than code v%d" % (version, SCHEMA_VERSION))
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    # ── sources ──────────────────────────────────────────────────────────
    def upsert_source(self, src: Dict[str, Any]) -> List[str]:
        """Insert or update one allowlist entry (kit format). Returns any domains it refused."""
        domains = [d.lower().strip() for d in src.get("domains", []) or []]
        refused = [d for d in domains if d.removeprefix("www.") in SHARED_HOST_APEXES]
        domains = [d for d in domains if d not in refused]
        tier, status = src.get("tier", "lead"), src.get("status", "to_confirm")
        if any(_hard_blocked(d) for d in domains):
            tier = "blocked"
        extra = {k: v for k, v in src.items() if k not in _SOURCE_COLS}
        with self.tx() as c:
            c.execute(
                """INSERT INTO sources (id, name, region, tier, status, domains, method, cadence,
                     terms_checked, notes, config_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, region=excluded.region,
                     tier=excluded.tier, status=excluded.status, domains=excluded.domains,
                     method=excluded.method, cadence=excluded.cadence,
                     terms_checked=excluded.terms_checked, notes=excluded.notes,
                     config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (src["id"], src.get("name", src["id"]), src.get("region"), tier, status,
                 json.dumps(domains), src.get("method"), src.get("cadence"),
                 src.get("terms_checked"), src.get("notes"), json.dumps(extra), _now()))
        return refused

    def set_source_status(self, source_id: str, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sources SET status=?, updated_at=? WHERE id=?", (status, _now(), source_id))

    def load_allowlist(self, path: Any = DEFAULT_ALLOWLIST) -> int:
        """Sync the sources table from allowlist.json. Sources removed from the file are deleted."""
        sources = json.loads(Path(path).read_text(encoding="utf-8"))["sources"]
        for s in sources:
            for d in self.upsert_source(s):
                self.add_alert("allowlist_error",
                               "%s lists the shared vendor domain %s; it was ignored. List the "
                               "city's own subdomain instead (e.g. cityname.%s)" % (s["id"], d, d),
                               source=str(path))
        ids = [s["id"] for s in sources]
        with self.tx() as c:
            c.execute("DELETE FROM sources WHERE id NOT IN (%s)" % ",".join("?" * len(ids)), ids)
        return len(sources)

    def sources(self) -> List[Dict[str, Any]]:
        """Sources in the shape signal_feed.classify() expects."""
        out = []
        for r in self.conn.execute("SELECT * FROM sources ORDER BY id"):
            d = dict(r)
            d["domains"] = json.loads(d["domains"])
            d["config"] = json.loads(d.pop("config_json") or "{}")
            out.append(d)
        return out

    def source(self, source_id: str) -> Optional[Dict[str, Any]]:
        return next((s for s in self.sources() if s["id"] == source_id), None)

    def source_for_url(self, url: Optional[str]) -> Optional[Dict[str, Any]]:
        return SF.match_source(SF.host_of(url or ""), self.sources())

    def is_blocked_url(self, url: Optional[str]) -> bool:
        host = SF.host_of(url or "")
        if host and _hard_blocked(host):
            return True
        src = self.source_for_url(url)
        return bool(src and src["tier"] == "blocked")

    def is_official_active(self, url: Optional[str]) -> bool:
        if not url or self.is_blocked_url(url):
            return False
        src = self.source_for_url(url)
        return bool(src and src["tier"] == "official" and src["status"] == "active")

    def gate(self, it: Dict[str, Any]) -> Tuple[str, str]:
        """(gate_status, reason) for an aliased ledger item. The kit decides; the store only tightens."""
        if self.is_blocked_url(it.get("source_url")):
            return GATE_BLOCKED, "blocked source (unlicensed); drop this item"
        ok, reason = SF.classify(it, self.sources())
        if ok and self.is_official_active(it.get("source_url")):
            return GATE_VERIFIED, reason
        return GATE_UNVERIFIED, reason

    # ── runs and alerts ──────────────────────────────────────────────────
    def start_run(self, kind: str, source_id: str = None, note: str = None) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO runs (kind, source_id, started_at, note) VALUES (?,?,?,?)",
                             (kind, source_id, _now(), note)).lastrowid

    _RUN_FIELDS = ("items_in", "items_new", "items_merged", "note")

    def finish_run(self, run_id: int, status: str = "ok", **counts: Any) -> None:
        bad = set(counts) - set(self._RUN_FIELDS)
        if bad:
            raise ValueError("unknown run field(s): %s" % ", ".join(sorted(bad)))
        sets = "".join(", %s=?" % k for k in counts)
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?%s WHERE id=?" % sets,
                      (_now(), status, *counts.values(), run_id))

    def add_alert(self, kind: str, message: str, *, source: str = None, run_id: int = None) -> int:
        with self.tx() as c:
            return c.execute(
                "INSERT INTO alerts (run_id, kind, source, message, created_at) VALUES (?,?,?,?,?)",
                (run_id, kind, source, message, _now())).lastrowid

    def recent_runs(self, source_id: str, kind: str = "collect", n: int = 2) -> List[sqlite3.Row]:
        """Last n finished runs for a source, newest first. Skipped runs are not counted."""
        return self.conn.execute(
            """SELECT * FROM runs WHERE source_id=? AND kind=? AND finished_at IS NOT NULL
                 AND status != 'skipped' ORDER BY id DESC LIMIT ?""", (source_id, kind, n)).fetchall()

    # ── documents (agenda and minutes links; stage c reads these) ────────
    def upsert_document(self, doc: Dict[str, Any], *, run_id: int = None) -> bool:
        """Record one agenda/minutes/packet link. Returns True if it was new."""
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO documents (source_id, platform, body, meeting_date, kind,
                     title, url, first_run_id, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (doc["source_id"], doc.get("platform"), doc.get("body"), doc.get("meeting_date"),
                 doc["kind"], doc.get("title"), doc["url"], run_id, _now()))
            return cur.rowcount == 1

    def documents(self, source_id: str = None) -> List[sqlite3.Row]:
        if source_id:
            return self.conn.execute("SELECT * FROM documents WHERE source_id=? ORDER BY meeting_date, id",
                                     (source_id,)).fetchall()
        return self.conn.execute("SELECT * FROM documents ORDER BY meeting_date, id").fetchall()

    def open_alerts(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id").fetchall()

    def resolve_alert(self, alert_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE alerts SET resolved_at=? WHERE id=?", (_now(), alert_id))

    # ── items ────────────────────────────────────────────────────────────
    def upsert_item(self, raw: Dict[str, Any], *, run_id: int = None,
                    gate: Optional[str] = None) -> str:
        """Insert or merge one ledger row. Returns 'new' or 'merged'; raises ValueError on a row
        that cannot be stored (no state, place or date).

        Dedupe: same normalized (state, place, project) with an earliest sighting within
        DEDUPE_WINDOW_DAYS merges. With no project, the first six words of action stand in,
        as in the kit's dedupe_key.
        """
        it = SF.apply_aliases(raw)
        state = str(it.get("state") or "").strip().upper()
        place = str(it.get("place") or "").strip()
        if not (state and place):
            raise ValueError("missing state or place")
        day = parse_day(it.get("date"))
        project = str(it.get("project") or "").strip()
        key = match_key(state, place, project or " ".join(str(it.get("action", "")).split()[:6]))
        url = str(it.get("source_url") or "").strip()

        status, reason = self.gate(it)
        if gate in GATE_STATUSES and GATE_STATUSES.index(gate) > GATE_STATUSES.index(status):
            status = gate  # downgrade only: verified -> unverified -> blocked

        official_url = url if status == GATE_VERIFIED else None
        lead_url = url if (status == GATE_UNVERIFIED and url) else None
        source_type = "official" if status == GATE_VERIFIED else it.get("source_type")
        tags = it.get("trade_tags") or []
        size = _to_float(it.get("size_usd", it.get("value_usd")))
        now = _now()

        existing = self.conn.execute(
            """SELECT * FROM items WHERE match_key=? AND date BETWEEN ? AND ?
               ORDER BY ABS(julianday(date) - julianday(?)) LIMIT 1""",
            (key, (day - timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat(),
             (day + timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat(), day.isoformat())).fetchone()

        with self.tx() as c:
            if existing is None:
                c.execute(
                    """INSERT INTO items (match_key, date, last_seen_at, state, place, county, project,
                         action, why, notes, source_name, source_type, gate_status, gate_reason,
                         official_url, lead_url, source_urls, trade_tags, size_usd, raw_json,
                         first_run_id, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, day.isoformat(), day.isoformat(), state, place, it.get("county"),
                     project or None, it.get("action"), it.get("why"), it.get("notes"),
                     it.get("source_name"), source_type, status, reason, official_url, lead_url,
                     json.dumps([url] if url else []), json.dumps(sorted(set(tags))), size,
                     json.dumps(raw, sort_keys=True, ensure_ascii=False), run_id, now, now))
                return "new"

            old = existing["gate_status"]
            if old == GATE_BLOCKED or status == GATE_BLOCKED:
                merged = GATE_BLOCKED
            elif GATE_VERIFIED in (old, status):
                merged = GATE_VERIFIED
            else:
                merged = GATE_UNVERIFIED
            urls = json.loads(existing["source_urls"])
            if url and url not in urls:
                urls.append(url)
            c.execute(
                """UPDATE items SET date=MIN(date, ?), last_seen_at=MAX(last_seen_at, ?), gate_status=?,
                     gate_reason=CASE WHEN ? THEN ? ELSE gate_reason END,
                     official_url=CASE WHEN ?='verified' THEN COALESCE(official_url, ?) ELSE NULL END,
                     lead_url=COALESCE(lead_url, ?),
                     source_type=CASE WHEN ?='verified' THEN 'official' ELSE COALESCE(source_type, ?) END,
                     county=COALESCE(county, ?), project=COALESCE(project, ?), why=COALESCE(why, ?),
                     notes=COALESCE(notes, ?), source_urls=?, trade_tags=?,
                     size_usd=COALESCE(?, size_usd), updated_at=?
                   WHERE id=?""",
                (day.isoformat(), day.isoformat(), merged, merged != old, reason, merged, official_url, lead_url, merged, it.get("source_type"),
                 it.get("county"), project or None, it.get("why"), it.get("notes"),
                 json.dumps(urls), json.dumps(sorted(set(json.loads(existing["trade_tags"])) | set(tags))),
                 size, now, existing["id"]))
            return "merged"

    def confirm_item(self, item_id: int, official_url: str) -> bool:
        """Attach a human-approved official URL (stage c). Refuses anything not official+active."""
        if not self.is_official_active(official_url):
            return False
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None or row["gate_status"] == GATE_BLOCKED:
            return False
        urls = json.loads(row["source_urls"])
        if official_url not in urls:
            urls.append(official_url)
        src = self.source_for_url(official_url)
        with self.tx() as c:
            c.execute("""UPDATE items SET gate_status='verified', source_type='official', official_url=?,
                           gate_reason=?, source_urls=?, updated_at=? WHERE id=?""",
                      (official_url, "official: %s (confirmed by hand)" % src["name"],
                       json.dumps(urls), _now(), item_id))
        return True

    def import_ledger(self, path: Any) -> Dict[str, int]:
        run_id = self.start_run("import", note=str(path))
        counts = {"new": 0, "merged": 0, "bad": 0}
        try:
            for raw in read_jsonl(path):
                try:
                    result = self.upsert_item(raw, run_id=run_id)
                except ValueError as exc:
                    counts["bad"] += 1
                    self.add_alert("schema_change", "ledger row not stored (%s): %s"
                                   % (exc, json.dumps(raw)[:200]), source=str(path), run_id=run_id)
                    continue
                counts[result] += 1
                it = SF.apply_aliases(raw)
                if str(it.get("status", "")).lower() == "confirmed" and self.gate(it)[0] != GATE_VERIFIED:
                    self.add_alert("marked_confirmed_unverified",
                                   "item marked confirmed but not verified: %s / %s (%s)"
                                   % (it.get("place"), it.get("project") or str(it.get("action", ""))[:60],
                                      self.gate(it)[1]), source=str(path), run_id=run_id)
        except Exception as exc:
            self.finish_run(run_id, "error")
            self.add_alert("collector_error", "import failed: %s" % exc, source=str(path), run_id=run_id)
            raise
        self.finish_run(run_id, "ok", items_in=sum(counts.values()),
                        items_new=counts["new"], items_merged=counts["merged"])
        return counts

    def items(self, gate_status: str = None) -> List[sqlite3.Row]:
        if gate_status:
            return self.conn.execute("SELECT * FROM items WHERE gate_status=? ORDER BY date, id",
                                     (gate_status,)).fetchall()
        return self.conn.execute("SELECT * FROM items ORDER BY date, id").fetchall()

    def held_items(self) -> List[sqlite3.Row]:
        return self.items(GATE_UNVERIFIED)

    def paid_eligible_items(self, *, states: Iterable[str] = None, since: Any = None) -> List[sqlite3.Row]:
        """The only query an issue builder may use for subscriber copy."""
        sql = "SELECT * FROM items WHERE gate_status='verified' AND official_url IS NOT NULL"
        args: List[Any] = []
        if states:
            states = [s.upper() for s in states]
            sql += " AND state IN (%s)" % ",".join("?" * len(states))
            args += states
        if since:
            sql += " AND last_seen_at >= ?"
            args.append(parse_day(since).isoformat())
        rows = self.conn.execute(sql + " ORDER BY state, date, id", args).fetchall()
        return [r for r in rows if self.is_official_active(r["official_url"])]

    def export_jsonl(self, path: Any) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.items():
                rec = dict(r)
                for k in ("source_urls", "trade_tags", "raw_json"):
                    rec[k] = json.loads(rec[k])
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
                n += 1
        return n

    def stats(self) -> Dict[str, Any]:
        q = lambda sql: self.conn.execute(sql).fetchall()  # noqa: E731
        return {
            "items_by_gate": {r[0]: r[1] for r in q("SELECT gate_status, COUNT(*) FROM items GROUP BY 1")},
            "sources_by_tier_status": {"%s/%s" % (r[0], r[1]): r[2]
                                       for r in q("SELECT tier, status, COUNT(*) FROM sources GROUP BY 1, 2")},
            "open_alerts": len(self.open_alerts()),
            "documents": q("SELECT COUNT(*) FROM documents")[0][0],
            "runs": q("SELECT COUNT(*) FROM runs")[0][0],
        }


def read_jsonl(path: Any) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("line %d: %s" % (lineno, exc)) from exc


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(description="Signal Feed SQLite store")
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    imp = sub.add_parser("import")
    imp.add_argument("ledger", nargs="?", default=os.environ.get(LEDGER_ENV, ""),
                     help="ledger JSONL (default: env %s)" % LEDGER_ENV)
    imp.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    exp = sub.add_parser("export")
    exp.add_argument("out")
    sub.add_parser("stats")
    a = p.parse_args(argv)

    store = Store(a.db)
    try:
        if a.cmd == "import":
            if not a.ledger:
                p.error("no ledger: pass a path or set %s" % LEDGER_ENV)
            if not Path(a.ledger).is_file():
                p.error("ledger not found: %s" % a.ledger)
            print("sources synced: %d" % store.load_allowlist(a.allowlist))
            print(json.dumps(store.import_ledger(a.ledger)))
            for al in store.open_alerts():
                print("ALERT [%s] %s" % (al["kind"], al["message"]))
        elif a.cmd == "export":
            print("exported %d items" % store.export_jsonl(a.out))
        elif a.cmd == "stats":
            print(json.dumps(store.stats(), indent=2))
        else:
            print("initialized %s" % a.db)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
