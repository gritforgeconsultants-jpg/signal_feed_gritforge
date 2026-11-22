"""Builds tests/fixtures/collectors/*.json: synthetic records in each API's documented shape.

Run from the repo root: python tests/fixtures/make_collector_fixtures.py
"""
import json, sys
from pathlib import Path
from urllib.parse import urlencode
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from collectors.http import build_url, canonical
OUT = ROOT / "tests/fixtures/collectors"
NOTE = ("Hand-built from the documented response shape, not a live recording (the build "
        "container had no network to these hosts). Replace with `collect.py run --record` "
        "output once the source is active. Records are synthetic TEST data.")

def req(url, params=None, body=None, status=200):
    return {"url": canonical(build_url(url, params)), "status": status, "body": body}

def save(name, reqs):
    (OUT / name).write_text(json.dumps({"_note": NOTE, "requests": reqs}, indent=1) + "\n")

# ── SAM.gov ──
SAM = "https://api.sam.gov/prod/opportunities/v2/search"
def samq(state, offset=0):
    return {"api_key": "***", "postedFrom": "11/17/2026", "postedTo": "11/20/2026", "state": state,
            "limit": 1000, "offset": offset}
def opp(nid, title, naics, state, city=None, typ="Solicitation", sol="", posted="2026-11-18",
        due="2026-12-15T14:00:00-06:00", agency="DEPT OF DEFENSE.DEPT OF THE ARMY.W076 ENDIST FT WORTH",
        award=None, ui=True, set_aside="Total Small Business Set-Aside (FAR 19.5)"):
    pop = {"state": {"code": state, "name": state}}
    if city:
        pop["city"] = {"code": "0000", "name": city}
    r = {"noticeId": nid, "title": title, "solicitationNumber": sol, "fullParentPathName": agency,
         "postedDate": posted, "type": typ, "baseType": typ, "naicsCode": naics,
         "typeOfSetAsideDescription": set_aside, "responseDeadLine": due, "placeOfPerformance": pop,
         "active": "Yes", "award": award,
         "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=%s" % nid,
         "uiLink": "https://sam.gov/opp/%s/view" % nid if ui else "https://example.com/mirror/%s" % nid}
    return r
tx = [
    opp("TEST0001", "TEST Barracks Renovation, Fort Hood", "236220", "TX", "FORT CAVAZOS", sol="W9126G26R0001"),
    opp("TEST0002", "TEST Electrical Distribution Upgrade", "238210", "TX", "SAN ANTONIO", typ="Award Notice",
        sol="FA301626C0002", due=None, agency="DEPT OF DEFENSE.DEPT OF THE AIR FORCE.FA3016 502 CONS",
        award={"amount": "1845000.00", "date": "2026-11-17", "awardee": {"name": "TEST ELECTRIC LLC"}}),
    opp("TEST0003", "TEST Engineering Study", "541330", "TX", "AUSTIN", sol="X1"),             # not construction
    opp("TEST0004", "TEST Hangar Roof", "238160", "OK", "ALTUS", sol="X2"),                    # wrong state
    opp("TEST0005", "TEST Surplus Modular Buildings", "236220", "TX", "WACO", typ="Sale of Surplus Property", sol="X3"),
    opp("TEST0006", "TEST Barracks Renovation, Fort Hood", "236220", "TX", "FORT CAVAZOS", sol="W9126G26R0001",
        typ="Solicitation", posted="2026-11-19"),                                             # amendment of 0001
]
co = [opp("TEST0101", "TEST US 50 Bridge Deck Repair", "237310", "CO", None, typ="Presolicitation",
          sol="69056726R0101", agency="TRANSPORTATION, DEPARTMENT OF.FEDERAL HIGHWAY ADMINISTRATION.CFLHD",
          ui=False, set_aside="")]
save("sam_gov.json", [req(SAM, samq("TX"), {"totalRecords": len(tx), "limit": 1000, "offset": 0, "opportunitiesData": tx}),
                      req(SAM, samq("CO"), {"totalRecords": 1, "limit": 1000, "offset": 0, "opportunitiesData": co})])

# ── Austin Socrata ──
AUS = "https://data.austintexas.gov/resource/3syk-w9eu.json"
WHERE = ("issue_date >= '2026-11-13T00:00:00' AND (permit_class_mapped in('Commercial','Multi-Family','Mixed Use') "
         "AND work_class in('New','Addition'))")
def permit(num, day, addr, wc="New", pc="Commercial", desc="TEST new 3-story office building", val=None, gc="TEST BUILDERS INC"):
    r = {"permit_number": num, "permit_type_desc": "Building Permit", "permit_class_mapped": pc, "work_class": wc,
         "description": desc, "issue_date": day + "T00:00:00.000", "status_current": "Active",
         "original_address1": addr, "contractor_full_name": gc}
    if val is not None:
        r["total_job_valuation"] = val
    return r
rows = [
    permit("2026-TEST-001 BP", "2026-11-19", "1200 E TEST BLVD", val="4200000"),
    permit("2026-TEST-002 BP", "2026-11-18", "1200 E TEST BLVD", wc="Addition", desc="TEST shell addition"),  # same site
    permit("2026-TEST-003 BP", "2026-11-17", "88 TEST LN", pc="Multi-Family", desc="TEST 240-unit apartments"),  # no value: kept
    permit("2026-TEST-004 BP", "2026-11-16", "5 SMALL ST", val="90000", desc="TEST tenant finish"),  # under floor
]
fresh_q = {"$select": "max(issue_date) AS newest"}
win_q = {"$where": WHERE, "$order": "issue_date DESC", "$limit": 1000}
save("socrata_austin.json", [req(AUS, fresh_q, [{"newest": "2026-11-19T00:00:00.000"}]), req(AUS, win_q, rows)])
save("socrata_austin_stale.json", [req(AUS, fresh_q, [{"newest": "2026-10-01T00:00:00.000"}])])
renamed = [{("site_address" if k == "original_address1" else k): v for k, v in r.items()} for r in rows]
save("socrata_austin_renamed.json", [req(AUS, fresh_q, [{"newest": "2026-11-19T00:00:00.000"}]), req(AUS, win_q, renamed)])

# ── San Antonio CKAN (test config; real resource id and columns not yet known) ──
SA = "https://data.sanantonio.gov/api/3/action/datastore_search"
def sarow(n, day, addr, wt="New", val="1500000"):
    return {"_id": n, "PERMIT #": "TEST-SA-%03d" % n, "DATE ISSUED": day, "ADDRESS": addr, "WORK TYPE": wt,
            "PERMIT TYPE": "Comm New Building Permit", "PROJECT NAME": "TEST distribution center",
            "DECLARED VALUATION": val, "CONTRACTOR": "TEST GC LP"}
sa_rows = [sarow(1, "2026-11-19", "100 TEST PKWY"), sarow(2, "2026-11-18", "200 TEST RD", wt="Repair"),
           sarow(3, "2026-11-15", "300 TEST AVE", val="0"), sarow(4, "2026-11-01", "400 OLD ST")]
sa_q = {"resource_id": "test-sa-permits", "sort": '"DATE ISSUED" desc', "limit": 500, "offset": 0}
save("ckan_san_antonio.json", [req(SA, sa_q, {"success": True, "result": {"records": sa_rows, "total": 4}})])
save("ckan_san_antonio_stale.json", [req(SA, sa_q, {"success": True, "result": {"records": [sarow(9, "2026-09-30", "9 OLD ST")], "total": 1}})])

# ── Legistar ──
LEG = "https://webapi.legistar.com/v1/testcity/events"
leg_q = {"$filter": "EventDate ge datetime'2026-10-06T00:00:00' and EventDate le datetime'2026-12-04T00:00:00'",
         "$orderby": "EventDate"}
events = [
    {"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-11-17T00:00:00",
     "EventAgendaFile": "https://testcity.legistar.com/View.ashx?M=A&ID=1001&GUID=TEST",
     "EventMinutesFile": "https://testcity.legistar.com/View.ashx?M=M&ID=1001&GUID=TEST",
     "EventInSiteURL": "https://testcity.legistar.com/MeetingDetail.aspx?ID=1001&GUID=TEST"},
    {"EventId": 2, "EventBodyName": "Planning Commission", "EventDate": "2026-12-01T00:00:00",
     "EventAgendaFile": "https://testcity.legistar.com/View.ashx?M=A&ID=1002&GUID=TEST",
     "EventMinutesFile": None, "EventInSiteURL": None},
    {"EventId": 3, "EventBodyName": "Library Board", "EventDate": "2026-11-18T00:00:00",
     "EventAgendaFile": "https://testcity.legistar.com/View.ashx?M=A&ID=1003&GUID=TEST",
     "EventMinutesFile": None, "EventInSiteURL": None},
]
save("legistar.json", [req(LEG, leg_q, events)])

# ── CivicPlus RSS ──
CP = "https://www.testcity-tx.gov/RSSFeed.aspx?ModID=65&CID=All-0"
cp_rss = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>TEST City Agenda Center</title><link>https://www.testcity-tx.gov/AgendaCenter</link>
<item><title>City Council - Regular Meeting - Nov 17, 2026</title><link>https://www.testcity-tx.gov/AgendaCenter/ViewFile/Agenda/_11172026-501</link><pubDate>Fri, 13 Nov 2026 17:00:00 -0600</pubDate><category>City Council</category></item>
<item><title>City Council - Regular Meeting - Nov 17, 2026 (Packet)</title><link>https://www.testcity-tx.gov/AgendaCenter/ViewFile/Agenda/_11172026-501?packet=true</link><pubDate>Fri, 13 Nov 2026 17:00:00 -0600</pubDate><category>City Council</category></item>
<item><title>City Council - Regular Meeting - Nov 3, 2026</title><link>https://www.testcity-tx.gov/AgendaCenter/ViewFile/Minutes/_11032026-480</link><pubDate>Wed, 18 Nov 2026 09:00:00 -0600</pubDate><category>City Council</category></item>
<item><title>City Council - Regular Meeting - Sep 1, 2026</title><link>https://www.testcity-tx.gov/AgendaCenter/ViewFile/Agenda/_09012026-300</link><pubDate>Fri, 28 Aug 2026 17:00:00 -0500</pubDate><category>City Council</category></item>
</channel></rss>"""
robots_ok = "User-agent: *\nDisallow: /admin/\n"
save("civicplus.json", [req("https://www.testcity-tx.gov/robots.txt", None, robots_ok), req(CP, None, cp_rss)])
save("civicplus_disallowed.json", [req("https://www.testcity-tx.gov/robots.txt", None, "User-agent: *\nDisallow: /RSSFeed.aspx\n"),
                                   req(CP, None, cp_rss)])

# ── Granicus RSS ──
GR = "https://testcity.granicus.com/ViewPublisherRSS.php?view_id=2&mode=agendas"
gr_rss = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>TEST City - Agendas</title>
<item><title>City Council - Nov 19, 2026</title><link>https://testcity.granicus.com/AgendaViewer.php?view_id=2&amp;event_id=777</link><pubDate>Mon, 16 Nov 2026 12:00:00 -0700</pubDate></item>
<item><title>City Council - Nov 5, 2026</title><link>https://testcity.granicus.com/MinutesViewer.php?view_id=2&amp;clip_id=4321</link><pubDate>Thu, 19 Nov 2026 12:00:00 -0700</pubDate></item>
</channel></rss>"""
save("granicus.json", [req("https://testcity.granicus.com/robots.txt", None, "", status=404), req(GR, None, gr_rss)])
print("ok", sorted(p.name for p in OUT.iterdir()))
