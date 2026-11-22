"""SAM.gov Opportunities API collector.

    GET https://api.sam.gov/prod/opportunities/v2/search
        ?api_key=...&postedFrom=MM/dd/yyyy&postedTo=MM/dd/yyyy&state=TX&limit=1000&offset=0

Key: env SAM_API_KEY (free at sam.gov, Profile, Public API Key). Never logged or stored.

Query design: one query per place-of-performance state, then keep NAICS 236/237/238 on
our side. The API's NAICS parameter takes one 6-digit code per request, and the three
construction sectors span about 30 codes; per-code queries would burn the daily request
allowance (low for a personal key) in a single run. Per-state queries cost 1 to 2
requests a run.

Each item links to its sam.gov notice page (an official API record). Amendments to one
solicitation share a solicitation number, so they merge in the store instead of repeating.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, Mapping, Optional

from .base import (CollectResult, ConfigError, SchemaError, clean, collector_config, tidy_case,
                   to_day, to_float, trade_tags_for_naics)
from .http import Fetcher, host_ok

API = "https://api.sam.gov/prod/opportunities/v2/search"

DEFAULTS: Dict[str, Any] = {
    "states": ["TX", "CO"],
    "naics_prefixes": ["236", "237", "238"],
    "lookback_days": 3,
    "page_size": 1000,
    "max_pages": 3,
    "skip_types": ["Sale of Surplus Property"],
}

STATE_NAMES = {"TX": "Texas", "CO": "Colorado", "NM": "New Mexico", "OK": "Oklahoma",
               "AZ": "Arizona", "UT": "Utah", "WY": "Wyoming", "KS": "Kansas",
               "NE": "Nebraska", "LA": "Louisiana", "AR": "Arkansas"}


def collect(source: Dict[str, Any], fetcher: Fetcher, today: date,
            env: Optional[Mapping[str, str]] = None) -> CollectResult:
    cfg = {**DEFAULTS, **collector_config(source)}
    key = (env if env is not None else os.environ).get("SAM_API_KEY", "").strip()
    if not key:
        raise ConfigError("SAM_API_KEY is not set")

    posted_from = (today - timedelta(days=int(cfg["lookback_days"]))).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")
    size, res, seen = int(cfg["page_size"]), CollectResult(), set()

    for st in cfg["states"]:
        st = st.upper()
        offset, total = 0, 0
        for _ in range(int(cfg["max_pages"])):
            data = fetcher.get_json(API, {"api_key": key, "postedFrom": posted_from,
                                          "postedTo": posted_to, "state": st,
                                          "limit": size, "offset": offset})
            if not isinstance(data, dict) or "opportunitiesData" not in data:
                keys = ", ".join(sorted(data)) if isinstance(data, dict) else type(data).__name__
                raise SchemaError("SAM.gov response has no opportunitiesData (got: %s)" % keys)
            rows = data.get("opportunitiesData") or []
            total = int(data.get("totalRecords") or 0)
            res.fetched += len(rows)
            for raw in rows:
                it = to_item(raw, st, cfg)
                if it and it["external_id"] not in seen:
                    seen.add(it["external_id"])
                    res.items.append(it)
            offset += size
            if offset >= total or not rows:
                break
        else:
            if offset < total:
                res.warnings.append("SAM.gov %s: stopped at %d of %d records (max_pages=%s); "
                                    "raise max_pages or shorten lookback_days"
                                    % (st, offset, total, cfg["max_pages"]))
    res.note = "%d notices fetched, %d construction in %s" % (
        res.fetched, len(res.items), "/".join(cfg["states"]))
    return res


def to_item(raw: Dict[str, Any], queried_state: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One opportunitiesData record -> ledger item, or None if it is not ours."""
    naics = clean(raw.get("naicsCode"))
    if not any(naics.startswith(p) for p in cfg["naics_prefixes"]):
        return None
    typ = clean(raw.get("type"))
    if typ in cfg["skip_types"]:
        return None
    nid = clean(raw.get("noticeId"))
    if not nid:
        return None

    pop = raw.get("placeOfPerformance") or {}
    st = clean(((pop.get("state") or {}).get("code"))).upper()
    if st != queried_state:
        return None  # the state filter is the API's; check it here too
    city = tidy_case(clean((pop.get("city") or {}).get("name")))
    place = city or STATE_NAMES.get(st, st)

    # "DEPT OF DEFENSE.DEPT OF THE ARMY.W076 ENDIST FT WORTH" -> "DEPT OF THE ARMY, W076 ENDIST FT WORTH"
    path = [clean(x) for x in (raw.get("fullParentPathName") or "").split(".") if clean(x)]
    agency = ", ".join(path[1:] if len(path) > 1 else path)
    sol = clean(raw.get("solicitationNumber"))
    title = clean(raw.get("title")) or "Untitled notice"
    due = to_day(raw.get("responseDeadLine"))

    action = "%s posted by %s" % (typ or "Notice", agency or "a federal agency")  # names kept as SAM has them
    if due:
        action += "; responses due %s" % due.isoformat()
    award = raw.get("award") if isinstance(raw.get("award"), dict) else {}
    amount = to_float(award.get("amount"))
    awardee = clean((award.get("awardee") or {}).get("name"))
    if awardee:
        action += "; awarded to %s" % awardee

    notes = []
    if sol:
        notes.append("Solicitation %s" % sol)
    notes.append("NAICS %s" % naics)
    set_aside = clean(raw.get("typeOfSetAsideDescription"))
    if set_aside and set_aside.lower() != "none":
        notes.append("Set-aside: %s" % set_aside)
    if amount is not None:
        notes.append("Amount is the award amount on the notice")
    if not city:
        notes.append("No city given for the place of performance")

    posted = to_day(raw.get("postedDate"))
    url = clean(raw.get("uiLink"))
    if not host_ok(url, "sam.gov"):
        url = "https://sam.gov/opp/%s/view" % nid

    return {
        "date": posted.isoformat() if posted else "",  # empty -> store alerts schema_change
        "state": st,
        "place": place,
        "project": "%s (%s)" % (title, sol) if sol else title,
        "action": action,
        "source_name": "SAM.gov",
        "source_url": url,
        "source_type": "official",
        "notes": "; ".join(notes),
        "trade_tags": trade_tags_for_naics(naics),
        "size_usd": amount,
        "external_id": "sam:%s" % nid,
    }
