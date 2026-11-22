"""SQLite store for GritForge Signal Feed (stage a).

Standard library only, Python 3.9+. Replaces the flat JSONL ledger as the
working store; JSONL stays as the audit export.

Tables: items, sources, runs, alerts, subscribers, issues, sends.

Core rule, enforced here as a second line of defense behind the gate in
signal_feed.py: an item can only be selected for a paid issue when it is
gate_status='verified' AND its official_url resolves to a source with
tier='official' AND status='active'. Blocked domains can never be stored as
anything but 'blocked'.

CLI:
    python store.py init
    python store.py import LEDGER.jsonl [--allowlist config/allowlist.json]
    python store.py export OUT.jsonl
    python store.py stats
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
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("SIGNAL_FEED_DB", ROOT / "signal_feed.db"))
DEFAULT_ALLOWLIST = ROOT / "config" / "allowlist.json"

SCHEMA_VERSION = 1
DEDUPE_WINDOW_DAYS = 14

# Hard block list. Mirrors the prompt's never-touch list; allowlist.json can
# add more, it can never remove these.
BLOCKED_DOMAINS = frozenset(
    {"dodge.construction.com", "construction.com", "constructconnect.com",
     "buildingconnected.com", "planhub.com"}
)

GATE_VERIFIED = "verified"
GATE_UNVERIFIED = "unverified"
GATE_BLOCKED = "blocked"
GATE_STATUSES = (GATE_VERIFIED, GATE_UNVERIFIED, GATE_BLOCKED)

# Field aliases: prefer the kit's map so there is one source of truth.
try:  # pragma: no cover - depends on the stage 1 kit being present
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from signal_feed import FIELD_ALIASES as _KIT_ALIASES  # type: ignore

    FIELD_ALIASES: Dict[str, str] = dict(_KIT_ALIASES)
except Exception:  # kit not importable; documented ledger fields only
    FIELD_ALIASES = {
        "metro_or_submarket": "place",
        "name": "project",
        "observed_at": "observed_at",
        "state": "state",
        "action": "action",
        "source_url": "source_url",
        "source_type": "source_type",
    }

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    name TEXT,
    tier TEXT NOT NULL DEFAULT 'lead',        -- official | lead | blocked
    status TEXT NOT NULL DEFAULT 'to_confirm', -- active | to_confirm | paused | blocked
    kind TEXT,                                 -- sam | socrata | ckan | legistar | civicplus | granicus | press
    state TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                        -- import | collect | verify | draft | send
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
    observed_at TEXT NOT NULL,                 -- YYYY-MM-DD, first sighting (dedupe anchor)
    last_seen_at TEXT NOT NULL,
    state TEXT NOT NULL,
    place TEXT NOT NULL,
    project TEXT NOT NULL,
    action TEXT,
    source_type TEXT,                          -- official | press | news | ai | api ...
    gate_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (gate_status IN ('verified','unverified','blocked')),
    official_url TEXT,                         -- only set from an official source
    lead_url TEXT,                             -- first non-official link, kept as secondary
    source_urls TEXT NOT NULL DEFAULT '[]',    -- every URL seen, JSON array
    trade_tags TEXT NOT NULL DEFAULT '[]',
    size_usd REAL,
    raw_json TEXT NOT NULL,
    first_run_id INTEGER REFERENCES runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_match ON items(match_key, observed_at);
CREATE INDEX IF NOT EXISTS ix_items_gate ON items(gate_status, state);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    kind TEXT NOT NULL,                        -- collector_error | stale_dataset | zero_items | schema_change | send_failed | webhook_failed
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
    state TEXT,                                -- NULL for national
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
"""

_PUNCT = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def domain_from_url(url: Optional[str]) -> str:
    """Host without www., lowercased. Borrowed from cwe/intel/normalize.py."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower().strip()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def norm_text(value: Optional[str]) -> str:
    s = (value or "").lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def match_key(state: str, place: str, project: str) -> str:
    return "|".join((norm_text(state), norm_text(place), norm_text(project)))


def parse_day(value: Any) -> date:
    """Accept YYYY-MM-DD, full ISO timestamps (Z or offset), or a date."""
    if isinstance(value, date):
        return value if not isinstance(value, datetime) else value.date()
    s = str(value or "").strip()
    if not s:
        raise ValueError("observed_at is empty")
    try:
        return date.fromisoformat(s[:10])
    except ValueError as exc:
        raise ValueError("unparseable observed_at: %r" % s) from exc


def apply_aliases(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, val in raw.items():
        out.setdefault(FIELD_ALIASES.get(key, key), val)
    return out


class Store:
    def __init__(self, path: Any = DEFAULT_DB):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.init()

    # ── schema ───────────────────────────────────────────────────────────
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
    def upsert_source(self, domain: str, *, tier: str, status: str, name: str = None,
                      kind: str = None, state: str = None, config: dict = None) -> None:
        domain = domain_from_url("https://" + domain) if "://" not in domain else domain_from_url(domain)
        if domain in BLOCKED_DOMAINS or any(_domain_matches(domain, b) for b in BLOCKED_DOMAINS):
            tier, status = "blocked", "blocked"
        with self.tx() as c:
            c.execute(
                """INSERT INTO sources (domain, name, tier, status, kind, state, config_json, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET name=excluded.name, tier=excluded.tier,
                     status=excluded.status, kind=excluded.kind, state=excluded.state,
                     config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (domain, name, tier, status, kind, state, json.dumps(config or {}), _now()),
            )

    def load_allowlist(self, path: Any = DEFAULT_ALLOWLIST) -> int:
        """Sync sources from allowlist.json.

        Accepts a list of source objects, {"sources": [...]}, or
        {"official": [...], "leads": [...], "blocked": [...]}. Each entry needs
        a domain (or url); tier and status default conservatively.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries: List[dict] = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict) and isinstance(data.get("sources"), list):
            entries = data["sources"]
        elif isinstance(data, dict):
            tier_by_group = {"official": "official", "leads": "lead", "lead": "lead", "blocked": "blocked"}
            for group, tier in tier_by_group.items():
                for e in data.get(group, []) or []:
                    e = {"domain": e} if isinstance(e, str) else dict(e)
                    e.setdefault("tier", tier)
                    entries.append(e)
        count = 0
        for e in entries:
            domain = e.get("domain") or domain_from_url(e.get("url"))
            if not domain:
                continue
            tier = e.get("tier", "lead")
            status = e.get("status", "blocked" if tier == "blocked" else "to_confirm")
            extra = {k: v for k, v in e.items()
                     if k not in {"domain", "url", "tier", "status", "name", "kind", "type", "state"}}
            self.upsert_source(domain, tier=tier, status=status, name=e.get("name"),
                               kind=e.get("kind") or e.get("type"), state=e.get("state"), config=extra)
            count += 1
        return count

    def source_for_url(self, url: Optional[str]) -> Optional[sqlite3.Row]:
        """Most specific source whose domain covers the URL's host."""
        host = domain_from_url(url)
        if not host:
            return None
        rows = self.conn.execute("SELECT * FROM sources").fetchall()
        hits = [r for r in rows if _domain_matches(host, r["domain"])]
        return max(hits, key=lambda r: len(r["domain"])) if hits else None

    def is_blocked_url(self, url: Optional[str]) -> bool:
        host = domain_from_url(url)
        if any(_domain_matches(host, b) for b in BLOCKED_DOMAINS):
            return True
        src = self.source_for_url(url)
        return bool(src and (src["tier"] == "blocked" or src["status"] == "blocked"))

    def is_official_active(self, url: Optional[str]) -> bool:
        src = self.source_for_url(url)
        return bool(src and src["tier"] == "official" and src["status"] == "active"
                    and not self.is_blocked_url(url))

    def classify(self, url: Optional[str]) -> str:
        """Default gate by domain. signal_feed.py's verify can pass its own verdict."""
        if self.is_blocked_url(url):
            return GATE_BLOCKED
        return GATE_VERIFIED if self.is_official_active(url) else GATE_UNVERIFIED

    # ── runs and alerts ──────────────────────────────────────────────────
    def start_run(self, kind: str, note: str = None) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO runs (kind, started_at, note) VALUES (?,?,?)",
                             (kind, _now(), note)).lastrowid

    def finish_run(self, run_id: int, status: str = "ok", **counts: int) -> None:
        sets = ", ".join("%s=?" % k for k in counts)
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?%s WHERE id=?" % (", " + sets if sets else ""),
                      (_now(), status, *counts.values(), run_id))

    def add_alert(self, kind: str, message: str, *, source: str = None, run_id: int = None) -> int:
        with self.tx() as c:
            return c.execute(
                "INSERT INTO alerts (run_id, kind, source, message, created_at) VALUES (?,?,?,?,?)",
                (run_id, kind, source, message, _now())).lastrowid

    def open_alerts(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id").fetchall()

    def resolve_alert(self, alert_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE alerts SET resolved_at=? WHERE id=?", (_now(), alert_id))

    # ── items ────────────────────────────────────────────────────────────
    def upsert_item(self, raw: Dict[str, Any], *, run_id: int = None,
                    gate: Optional[str] = None) -> str:
        """Insert or merge one ledger item. Returns 'new', 'merged', or 'skipped'.

        Dedupe: same normalized (state, place, project) whose first sighting is
        within DEDUPE_WINDOW_DAYS of this one merges into the existing row.
        `gate` lets the kit's verify step pass its verdict; it can only
        downgrade the domain-based verdict, never upgrade past it.
        """
        item = apply_aliases(raw)
        state = str(item.get("state") or "").strip().upper()
        place = str(item.get("place") or "").strip()
        project = str(item.get("project") or "").strip()
        if not (state and place and project):
            return "skipped"
        day = parse_day(item.get("observed_at"))
        url = item.get("source_url") or ""

        domain_gate = self.classify(url)
        if gate not in GATE_STATUSES:
            gate = domain_gate
        elif domain_gate == GATE_BLOCKED or (gate == GATE_VERIFIED and domain_gate != GATE_VERIFIED):
            gate = domain_gate  # caller cannot launder a lead or blocked link

        official_url = url if gate == GATE_VERIFIED else None
        lead_url = url if gate == GATE_UNVERIFIED else None
        source_type = item.get("source_type") or ("official" if gate == GATE_VERIFIED else None)
        key = match_key(state, place, project)
        tags = item.get("trade_tags") or []
        size = item.get("size_usd", item.get("value_usd"))
        now = _now()

        lo, hi = (day - timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat(), \
                 (day + timedelta(days=DEDUPE_WINDOW_DAYS)).isoformat()
        existing = self.conn.execute(
            """SELECT * FROM items WHERE match_key=? AND observed_at BETWEEN ? AND ?
               ORDER BY ABS(julianday(observed_at) - julianday(?)) LIMIT 1""",
            (key, lo, hi, day.isoformat())).fetchone()

        with self.tx() as c:
            if existing is None:
                c.execute(
                    """INSERT INTO items (match_key, observed_at, last_seen_at, state, place, project,
                         action, source_type, gate_status, official_url, lead_url, source_urls,
                         trade_tags, size_usd, raw_json, first_run_id, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, day.isoformat(), day.isoformat(), state, place, project,
                     item.get("action"), source_type, gate, official_url, lead_url,
                     json.dumps([url] if url else []), json.dumps(sorted(set(tags))),
                     _to_float(size), json.dumps(raw, sort_keys=True), run_id, now, now))
                return "new"

            urls = json.loads(existing["source_urls"])
            if url and url not in urls:
                urls.append(url)
            merged_gate = existing["gate_status"]
            if merged_gate != GATE_BLOCKED and gate == GATE_VERIFIED:
                merged_gate = GATE_VERIFIED
            merged_tags = sorted(set(json.loads(existing["trade_tags"])) | set(tags))
            c.execute(
                """UPDATE items SET observed_at=MIN(observed_at, ?), last_seen_at=MAX(last_seen_at, ?),
                     action=COALESCE(?, action), gate_status=?,
                     official_url=COALESCE(official_url, ?), lead_url=COALESCE(lead_url, ?),
                     source_type=CASE WHEN ?='verified' THEN 'official' ELSE COALESCE(source_type, ?) END,
                     source_urls=?, trade_tags=?, size_usd=COALESCE(?, size_usd), updated_at=?
                   WHERE id=?""",
                (day.isoformat(), day.isoformat(), item.get("action"), merged_gate,
                 official_url, lead_url, merged_gate, source_type, json.dumps(urls),
                 json.dumps(merged_tags), _to_float(size), now, existing["id"]))
            return "merged"

    def confirm_item(self, item_id: int, official_url: str) -> bool:
        """Attach an approved official URL (stage c). Refuses non-official domains."""
        if not self.is_official_active(official_url):
            return False
        with self.tx() as c:
            row = c.execute("SELECT source_urls, gate_status FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None or row["gate_status"] == GATE_BLOCKED:
                return False
            urls = json.loads(row["source_urls"])
            if official_url not in urls:
                urls.append(official_url)
            c.execute("""UPDATE items SET gate_status='verified', source_type='official',
                           official_url=?, source_urls=?, updated_at=? WHERE id=?""",
                      (official_url, json.dumps(urls), _now(), item_id))
        return True

    def import_ledger(self, path: Any, *, classify: Callable[[dict], Optional[str]] = None) -> Dict[str, int]:
        run_id = self.start_run("import", note=str(path))
        counts = {"new": 0, "merged": 0, "skipped": 0, "bad": 0}
        try:
            for raw in read_jsonl(path):
                try:
                    counts[self.upsert_item(raw, run_id=run_id, gate=classify(raw) if classify else None)] += 1
                except ValueError as exc:
                    counts["bad"] += 1
                    self.add_alert("schema_change", "bad ledger row: %s" % exc, source=str(path), run_id=run_id)
        except Exception as exc:
            self.finish_run(run_id, "error")
            self.add_alert("collector_error", "import failed: %s" % exc, source=str(path), run_id=run_id)
            raise
        total = sum(counts.values())
        self.finish_run(run_id, "ok", items_in=total, items_new=counts["new"], items_merged=counts["merged"])
        return counts

    def items(self, gate_status: str = None) -> List[sqlite3.Row]:
        if gate_status:
            return self.conn.execute("SELECT * FROM items WHERE gate_status=? ORDER BY observed_at, id",
                                     (gate_status,)).fetchall()
        return self.conn.execute("SELECT * FROM items ORDER BY observed_at, id").fetchall()

    def held_items(self) -> List[sqlite3.Row]:
        return self.items(GATE_UNVERIFIED)

    def paid_eligible_items(self, *, states: Iterable[str] = None, since: Any = None) -> List[sqlite3.Row]:
        """The only query an issue builder may use.

        Re-checks the official URL against the live sources table, so a source
        that is paused or blocked after an item was verified drops it.
        """
        sql = "SELECT * FROM items WHERE gate_status='verified' AND official_url IS NOT NULL"
        args: List[Any] = []
        if states:
            states = [s.upper() for s in states]
            sql += " AND state IN (%s)" % ",".join("?" * len(states))
            args += states
        if since:
            sql += " AND last_seen_at >= ?"
            args.append(parse_day(since).isoformat())
        rows = self.conn.execute(sql + " ORDER BY state, observed_at, id", args).fetchall()
        return [r for r in rows if self.is_official_active(r["official_url"])]

    def export_jsonl(self, path: Any) -> int:
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for r in self.items():
                rec = dict(r)
                for k in ("source_urls", "trade_tags", "raw_json"):
                    rec[k] = json.loads(rec[k])
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                n += 1
        return n

    def stats(self) -> Dict[str, Any]:
        q = lambda sql: self.conn.execute(sql).fetchall()
        return {
            "items_by_gate": {r[0]: r[1] for r in q("SELECT gate_status, COUNT(*) FROM items GROUP BY 1")},
            "sources_by_tier_status": {"%s/%s" % (r[0], r[1]): r[2]
                                       for r in q("SELECT tier, status, COUNT(*) FROM sources GROUP BY 1,2")},
            "open_alerts": len(self.open_alerts()),
            "runs": q("SELECT COUNT(*) FROM runs")[0][0],
        }


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def read_jsonl(path: Any) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
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
    imp.add_argument("ledger")
    imp.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    exp = sub.add_parser("export")
    exp.add_argument("out")
    sub.add_parser("stats")
    a = p.parse_args(argv)

    store = Store(a.db)
    try:
        if a.cmd == "import":
            if Path(a.allowlist).exists():
                print("sources synced: %d" % store.load_allowlist(a.allowlist))
            else:
                print("warning: no allowlist at %s; every item will be held" % a.allowlist)
            print(json.dumps(store.import_ledger(a.ledger)))
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
