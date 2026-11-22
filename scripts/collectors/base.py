"""Shared pieces for collectors: result type, errors, dates, permit items, trade tags.

A collector is a function collect(source, fetcher, today) -> CollectResult. It does not
touch the store; scripts/collect.py writes runs, alerts and items. Collectors return
ledger-format items (date, place, state, project, action, source_url, source_type, ...)
so the one gate in signal_feed.classify() decides them like any ledger row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

STALE_AFTER_DAYS = 14


class CollectorError(Exception):
    """Collector could not run. Becomes a collector_error alert."""


class ConfigError(CollectorError):
    """Allowlist collector block or env is incomplete."""


class SchemaError(CollectorError):
    """The source's response no longer has the shape we read. Becomes a schema_change alert."""


@dataclass
class CollectResult:
    items: List[Dict[str, Any]] = field(default_factory=list)
    documents: List[Dict[str, Any]] = field(default_factory=list)
    fetched: int = 0                      # raw records seen before filtering
    tracks_freshness: bool = False        # permit datasets: True; the runner applies the 14-day rule
    newest_record: Optional[date] = None
    warnings: List[str] = field(default_factory=list)  # become collector_error alerts
    note: str = ""


def collector_config(source: Dict[str, Any]) -> Dict[str, Any]:
    cfg = (source.get("config") or {}).get("collector") or {}
    return dict(cfg)


def to_day(value: Any) -> Optional[date]:
    """ISO date/timestamp, MM/DD/YYYY, or epoch milliseconds -> date. None if unreadable."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
    s = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date(int(m[3]), int(m[1]), int(m[2]))
        except ValueError:
            return None
    return None


def to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", str(value)))
    except ValueError:
        return None


def require_fields(rows: List[Dict[str, Any]], fields: Iterable[str], what: str) -> None:
    """A required field missing from every row means the dataset changed under us.

    Socrata leaves null fields out of individual rows, so one row missing a field is normal;
    all rows missing it is not.
    """
    if not rows:
        return
    missing = [f for f in fields if f and not any(f in r for r in rows)]
    if missing:
        seen = sorted({k for r in rows[:20] for k in r})
        raise SchemaError("%s: field(s) %s not in any record; fields seen: %s"
                          % (what, ", ".join(missing), ", ".join(seen)[:400]))


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def tidy_case(s: str) -> str:
    """SAN ANTONIO -> San Antonio; leaves mixed-case text alone."""
    return s.title() if s.isupper() else s


# Trade tags for stage d's Trade Watch tier. Only the five named tags; anything else is untagged.
NAICS_TRADE_TAGS = {
    "238110": ["concrete"], "238120": ["steel", "concrete"], "238210": ["electrical"],
    "238220": ["MEP"], "238910": ["site work"], "237110": ["site work"], "237310": ["site work"],
    "237990": ["site work"],
}


def trade_tags_for_naics(code: str) -> List[str]:
    return list(NAICS_TRADE_TAGS.get(str(code or "")[:6], []))


def matches(row: Dict[str, Any], rules: Dict[str, List[str]]) -> bool:
    """Every rule field must equal one of its values (case-insensitive). Empty rules match all."""
    for fld, allowed in (rules or {}).items():
        v = clean(row.get(fld)).lower()
        if v not in {a.lower() for a in allowed}:
            return False
    return True


def permit_item(cfg: Dict[str, Any], row: Dict[str, Any], day: date, url: str) -> Optional[Dict[str, Any]]:
    """One permit row -> ledger-format item. None if it falls under the size floor."""
    f = cfg.get("fields", {})
    pid = clean(row.get(cfg["id_field"]))
    address = tidy_case(clean(row.get(f.get("address", ""))))
    value = to_float(row.get(f.get("value", ""))) if f.get("value") else None
    if value is not None and value <= 0:
        value = None  # 0 means not filed in most permit datasets, not a $0 job
    floor = cfg.get("min_value")
    if floor and value is not None and value < floor:
        return None  # only drop when the value is known; unknown value stays in

    work = clean(row.get(f.get("work_class", "")))
    pclass = clean(row.get(f.get("permit_class", "")))
    desc = clean(row.get(f.get("description", "")))[:240]
    head = " ".join(x for x in (work, pclass) if x) or "Building"
    action = "%s permit issued" % head + (": %s" % desc if desc else "")

    notes = ["Permit %s" % pid]
    contractor = clean(row.get(f.get("contractor", "")))
    if contractor:
        notes.append("Contractor of record: %s" % tidy_case(contractor))
    if value is not None:
        notes.append("Valuation is the applicant's declared value on the permit")

    return {
        "date": day.isoformat(),
        "state": cfg["state"],
        "place": cfg["place"],
        "project": address or "Permit %s" % pid,
        "action": action,
        "source_name": cfg.get("source_name") or cfg["place"] + " open data",
        "source_url": url,
        "source_type": "official",
        "notes": "; ".join(notes),
        "size_usd": value,
        "external_id": "%s:%s" % (cfg.get("dataset") or cfg.get("resource_id"), pid),
    }
