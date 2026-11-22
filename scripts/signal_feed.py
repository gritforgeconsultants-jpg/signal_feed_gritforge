#!/usr/bin/env python3
"""GritForge Signal Feed: verification gate + digest drafter. Standard library only.

  python scripts/signal_feed.py verify --ledger 04-ledger/events.jsonl \
      --allowlist config/allowlist.json --out out/ [--since 2026-11-16] [--states TX,CO]
  python scripts/signal_feed.py draft --out out/ [--tier regional] [--week-of 2026-11-27]

verify writes  out/verified.jsonl  items that may go to a paid issue
               out/held.jsonl      Unverified queue, each with a hold_reason
               out/dupes.jsonl     dropped duplicates
               out/alerts.txt      failure alerts (empty file = none)
               out/seen_keys.txt   dedupe keys carried across runs
draft writes   out/issue-subscriber.txt  what a subscriber would get
               out/issue-review.txt      same, plus the held queue, for Arron only

Ledger fields read (rename in FIELD_ALIASES if the growth-watch ledger differs):
  date, place, county, state, project, action, why, source_name, source_url,
  source_type (official|trade_press|ai_summary|...), status, notes
Nothing here sends mail. Delivery stays a manual Send until you trust it.
"""
import argparse, json, re, sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

FIELD_ALIASES = {  # ledger name -> name used here
    "city": "place", "location": "place", "event_date": "date",
    "url": "source_url", "link": "source_url", "source": "source_name",
    "summary": "action", "title": "project",
    # growth-signal watch ledger (04-ledger/events.jsonl)
    "observed_at": "date", "metro_or_submarket": "place", "name": "project",
}
REQUIRED = ("date", "place", "state", "action", "source_url")
NON_OFFICIAL_TYPES = {"trade_press", "ai_summary", "blog", "secondary", "lead"}


def load_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            sys.exit(f"Ledger line {n} is not valid JSON: {e}")
        out.append(apply_aliases(raw))
    return out


def apply_aliases(raw):
    """Rename ledger fields. A field already using the canonical name wins over an alias."""
    it = {k: v for k, v in raw.items() if k not in FIELD_ALIASES}
    for k, v in raw.items():
        if k in FIELD_ALIASES:
            it.setdefault(FIELD_ALIASES[k], v)
    # observed_at is a timestamp in the watch ledger; the gate and dedupe use the day.
    d = str(it.get("date") or "")
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", d):
        it["date"] = d[:10]
    return it


def write_jsonl(path, items):
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def host_of(url):
    try:
        h = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return h.lower().removeprefix("www.")


def match_source(host, sources):
    for s in sources:
        for d in s.get("domains", []):
            d = d.lower()
            if host == d or host.endswith("." + d):
                return s
    return None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def dedupe_key(it):
    # place + project + action verb + date. Tighten here if repeats slip through.
    action_head = " ".join(str(it.get("action", "")).split()[:6])
    return "|".join(slug(x) for x in (it.get("state"), it.get("place"),
                                      it.get("project"), action_head, it.get("date")))


def classify(it, sources):
    """Return (verified: bool, reason: str)."""
    missing = [f for f in REQUIRED if not str(it.get(f, "")).strip()]
    stype = str(it.get("source_type", "")).lower()
    if stype in NON_OFFICIAL_TYPES:
        return False, f"source is {stype.replace('_', ' ')}; needs the official packet, minutes, filing or release"
    if missing:
        return False, "missing " + ", ".join(missing)
    host = host_of(it["source_url"])
    src = match_source(host, sources)
    if not src:
        return False, f"source {host or '(bad link)'} is not on the allowlist"
    tier, status = src.get("tier"), src.get("status")
    if tier == "blocked":
        return False, f"{src['name']} is blocked (unlicensed); drop this item"
    if tier != "official":
        return False, f"{src['name']} is a {tier} source; lead only"
    if status != "active":
        return False, f"{src['name']} is on the allowlist but still to_confirm"
    return True, f"official: {src['name']}"


def cmd_verify(a):
    allow = json.loads(Path(a.allowlist).read_text(encoding="utf-8"))
    sources = allow["sources"]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    states = {s.strip().upper() for s in a.states.split(",") if s.strip()}
    seen_path = out / "seen_keys.txt"
    seen = set(seen_path.read_text().split("\n")) if seen_path.exists() else set()
    seen.discard("")

    verified, held, dupes, alerts = [], [], [], []
    for it in load_jsonl(a.ledger):
        if a.since and str(it.get("date", "")) < a.since:
            continue
        if states and str(it.get("state", "")).upper() not in states and not a.national:
            continue
        key = dedupe_key(it)
        if key in seen:
            dupes.append({**it, "dedupe_key": key}); continue
        seen.add(key)
        ok, reason = classify(it, sources)
        claimed = str(it.get("status", "")).lower() == "confirmed"
        if ok:
            verified.append({**it, "status": "confirmed", "verify_note": reason, "dedupe_key": key})
        else:
            if claimed:
                alerts.append(f"ALERT: item marked confirmed but not verified: {it.get('place')} / "
                              f"{it.get('project') or it.get('action', '')[:60]} ({reason})")
            held.append({**it, "status": "unverified", "hold_reason": reason, "dedupe_key": key})

    write_jsonl(out / "verified.jsonl", verified)
    write_jsonl(out / "held.jsonl", held)
    write_jsonl(out / "dupes.jsonl", dupes)
    (out / "alerts.txt").write_text("\n".join(alerts) + ("\n" if alerts else ""))
    seen_path.write_text("\n".join(sorted(seen)) + "\n")
    print(f"verified {len(verified)} | held {len(held)} | dupes {len(dupes)} | alerts {len(alerts)}")
    for x in alerts:
        print(x)


def fmt_item(it, show_hold=False):
    where = it.get("place", "")
    if it.get("county"):
        where += f", {it['county']}"
    where += f", {it.get('state', '')}"
    lines = [f"{where}. {it.get('date', 'date unknown')}"]
    if it.get("project"):
        lines.append(f"Project: {it['project']}")
    lines.append(it.get("action", "").strip())
    if it.get("why"):
        lines.append(f"Why it matters: {it['why'].strip()}")
    if it.get("notes"):
        lines.append(f"Note: {it['notes'].strip()}")
    src = it.get("source_name") or ""
    url = it.get("source_url") or "(no link yet)"
    lines.append(f"Source: {src} {url}".replace("  ", " ").strip())
    if show_hold:
        lines.append(f"Status: UNVERIFIED. Held from paid send. Reason: {it.get('hold_reason')}")
    else:
        lines.append("Status: confirmed on the official record")
    return "\n".join(lines)


def cmd_draft(a):
    out = Path(a.out)
    verified = load_jsonl(out / "verified.jsonl")
    held = load_jsonl(out / "held.jsonl")
    tmpl = Path(a.template).read_text(encoding="utf-8")
    week = a.week_of or date.today().isoformat()

    if verified:
        body = "\n\n".join(fmt_item(i) for i in sorted(verified, key=lambda x: (x.get("state", ""), x.get("date", ""))))
    else:
        body = "Quiet week. Nothing new in Texas or Colorado tied to an official record. I'll pick it back up next Friday."
    sub = tmpl.replace("{{WEEK_OF}}", week).replace("{{ITEMS}}", body).replace("{{COUNT}}", str(len(verified)))
    (out / "issue-subscriber.txt").write_text(sub, encoding="utf-8")

    review = ["INTERNAL REVIEW COPY. Do not send. Send only issue-subscriber.txt.", "", sub, "",
              "=" * 60, f"HELD / UNVERIFIED QUEUE ({len(held)}). Confirm, hold, or drop in the QC hour.", "=" * 60, ""]
    review += [fmt_item(i, show_hold=True) + "\n" for i in held] or ["(empty)"]
    alerts = (out / "alerts.txt").read_text() if (out / "alerts.txt").exists() else ""
    if alerts.strip():
        review += ["", "FAILURE ALERTS", alerts]
    (out / "issue-review.txt").write_text("\n".join(review), encoding="utf-8")
    print(f"drafted: {len(verified)} items in subscriber issue, {len(held)} held")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    v = sp.add_parser("verify")
    v.add_argument("--ledger", required=True)
    v.add_argument("--allowlist", default="config/allowlist.json")
    v.add_argument("--out", default="out")
    v.add_argument("--since", default="")
    v.add_argument("--states", default="TX,CO")
    v.add_argument("--national", action="store_true", help="National Desk: keep all states")
    v.set_defaults(fn=cmd_verify)
    d = sp.add_parser("draft")
    d.add_argument("--out", default="out")
    d.add_argument("--template", default="templates/friday-email.txt")
    d.add_argument("--week-of", default="")
    d.set_defaults(fn=cmd_draft)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
