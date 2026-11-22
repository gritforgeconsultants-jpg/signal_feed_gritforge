"""Agenda and minutes link finder for allowlisted councils.

Records links only (agenda, minutes, packet, meeting page) into the documents table.
It never downloads the documents, never creates items, and never crawls: each platform
exposes a structured index, so one request per feed is enough.

  legistar   Legistar Web API   https://webapi.legistar.com/v1/{client}/events?$filter=...
  civicplus  AgendaCenter RSS   https://{city site}/RSSFeed.aspx?ModID=65&CID=All-0
  granicus   ViewPublisher RSS  https://{client}.granicus.com/ViewPublisherRSS.php?view_id=N&mode=agendas

RSS feeds are fetched with robots.txt honored; every host is limited to 1 request per 5
seconds by the fetcher. Stage c uses these links to confirm held items, which only works
if the link's host is listed on the source (the city's own vendor subdomain, e.g.
sanantonio.legistar.com; the store refuses bare vendor domains).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from .base import CollectResult, ConfigError, SchemaError, clean, collector_config, require_fields, to_day
from .http import Fetcher

LEGISTAR_API = "https://webapi.legistar.com/v1/%s/events"
MAX_FEED_BYTES = 2 * 1024 * 1024


def collect(source: Dict[str, Any], fetcher: Fetcher, today: date) -> CollectResult:
    cfg = collector_config(source)
    platform = cfg.get("type")
    # Minutes are adopted weeks after the meeting, so look back further than for agendas.
    start = today - timedelta(days=int(cfg.get("lookback_days", 45)))
    end = today + timedelta(days=int(cfg.get("ahead_days", 14)))  # agendas post before the meeting
    if platform == "legistar":
        docs = _legistar(source, cfg, fetcher, start, end)
    elif platform in ("civicplus", "granicus"):
        feeds = cfg.get("rss_urls") or ([cfg["rss_url"]] if cfg.get("rss_url") else [])
        if not feeds:
            raise ConfigError("%s collector block needs rss_url or rss_urls" % source["id"])
        docs = []
        for feed in feeds:
            text = fetcher.get_text(feed, robots=True)
            docs += _rss(source, platform, text, feed)
    else:
        raise ConfigError("%s: unknown agenda platform %r" % (source["id"], platform))

    bodies = {b.lower() for b in cfg.get("bodies", [])}
    res = CollectResult(fetched=len(docs))
    for d in docs:
        day = to_day(d.get("meeting_date"))
        if day is None or not (start <= day <= end):
            continue
        if bodies and clean(d.get("body")).lower() not in bodies:
            continue
        res.documents.append(d)
    res.note = "%d links read, %d in window %s..%s" % (len(docs), len(res.documents), start, end)
    return res


def _legistar(source: Dict[str, Any], cfg: Dict[str, Any], fetcher: Fetcher,
              start: date, end: date) -> List[Dict[str, Any]]:
    client = cfg.get("client")
    if not client:
        raise ConfigError("%s collector block needs the Legistar client name" % source["id"])
    params = {"$filter": "EventDate ge datetime'%sT00:00:00' and EventDate le datetime'%sT00:00:00'"
                         % (start.isoformat(), end.isoformat()),
              "$orderby": "EventDate"}
    if cfg.get("token_env"):
        import os
        tok = os.environ.get(cfg["token_env"], "")
        if not tok:
            raise ConfigError("%s is not set" % cfg["token_env"])
        params["token"] = tok
    rows = fetcher.get_json(LEGISTAR_API % client, params)
    if not isinstance(rows, list):
        raise SchemaError("Legistar events did not return a list")
    require_fields(rows, ["EventDate", "EventBodyName"], "legistar:%s" % client)
    docs = []
    for r in rows:
        day = to_day(r.get("EventDate"))
        body = clean(r.get("EventBodyName"))
        for kind, fld in (("agenda", "EventAgendaFile"), ("minutes", "EventMinutesFile"),
                          ("meeting", "EventInSiteURL")):
            url = clean(r.get(fld))
            if url.startswith("https://") or url.startswith("http://"):
                docs.append(_doc(source, "legistar", body, day, kind, "%s %s" % (body, kind), url))
    return docs


_CIVIC_DATE = re.compile(r"/_(\d{2})(\d{2})(\d{4})-")


def _rss(source: Dict[str, Any], platform: str, text: str, feed: str) -> List[Dict[str, Any]]:
    if len(text) > MAX_FEED_BYTES:
        raise SchemaError("%s feed is larger than %d bytes" % (feed, MAX_FEED_BYTES))
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SchemaError("%s is not RSS/XML: %s" % (feed, exc))
    items = root.findall("./channel/item")
    if root.tag != "rss" or root.find("channel") is None:
        raise SchemaError("%s is not an RSS feed (root <%s>)" % (feed, root.tag))
    docs = []
    for it in items:
        title = clean(it.findtext("title"))
        link = clean(it.findtext("link"))
        if not link:
            continue
        kind = _kind(platform, link, feed)
        day = _meeting_day(platform, link, title, clean(it.findtext("pubDate")))
        body = clean(it.findtext("category")) or _body_from_title(title)
        docs.append(_doc(source, platform, body, day, kind, title, link))
    return docs


def _kind(platform: str, link: str, feed: str) -> str:
    l = link.lower()
    if "packet=true" in l:
        return "packet"
    if "/viewfile/minutes/" in l or "minutesviewer" in l:
        return "minutes"
    if "/viewfile/agenda/" in l or "agendaviewer" in l:
        return "agenda"
    return "minutes" if "mode=minutes" in feed.lower() else "agenda"


def _meeting_day(platform: str, link: str, title: str, pub: str) -> Optional[date]:
    m = _CIVIC_DATE.search(link)  # CivicPlus: /ViewFile/Agenda/_11172026-1234
    if m:
        try:
            return date(int(m[3]), int(m[1]), int(m[2]))
        except ValueError:
            pass
    m = re.search(r"([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})", title)  # "Nov 17, 2026"
    if m:
        try:  # first three letters covers Nov, November and Sept
            return datetime.strptime("%s %s %s" % (m[1][:3], m[2], m[3]), "%b %d %Y").date()
        except ValueError:
            pass
    if pub:
        try:
            return parsedate_to_datetime(pub).date()
        except (TypeError, ValueError):
            return None
    return None


def _body_from_title(title: str) -> str:
    # "Agenda - City Council - Nov 17, 2026" or "City Council - Nov 17, 2026"
    parts = [p.strip() for p in re.split(r"\s+[-|]\s+", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in ("agenda", "minutes", "packet")
             and not re.search(r"\d{4}", p)]
    return parts[0] if parts else ""


def _doc(source: Dict[str, Any], platform: str, body: str, day: Optional[date], kind: str,
         title: str, url: str) -> Dict[str, Any]:
    return {"source_id": source["id"], "platform": platform, "body": body,
            "meeting_date": day.isoformat() if day else None, "kind": kind,
            "title": title, "url": url}
