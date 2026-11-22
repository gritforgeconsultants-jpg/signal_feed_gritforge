"""Stage (b) collectors: HTTP politeness, SAM.gov, Socrata, CKAN, agenda finder, runner rules.

No network: every response comes from tests/fixtures/collectors (ReplayFetcher).
Run: python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect as C  # noqa: E402
import store as S  # noqa: E402
from collectors import agendas, ckan, sam_gov, socrata  # noqa: E402
from collectors.base import ConfigError, SchemaError, to_day  # noqa: E402
from collectors.http import (Fetcher, FetchError, RecordingFetcher, ReplayFetcher,  # noqa: E402
                             RobotsDisallowed, canonical)

FIX = ROOT / "tests" / "fixtures" / "collectors"
REAL_ALLOWLIST = json.loads((ROOT / "config" / "allowlist.json").read_text())
TODAY = date(2026, 11, 20)
KEY = "sk-test-DO-NOT-LEAK-1234"


def fixture(*names):
    reqs = []
    for n in names:
        reqs += json.loads((FIX / n).read_text())["requests"]
    return {"requests": reqs}


def real_source(source_id, **overrides):
    src = next(s for s in REAL_ALLOWLIST["sources"] if s["id"] == source_id)
    return json.loads(json.dumps({**src, **overrides}))  # deep copy: tests edit nested blocks


SA_TEST_COLLECTOR = {
    "type": "ckan", "domain": "data.sanantonio.gov", "resource_id": "test-sa-permits",
    "place": "San Antonio", "state": "TX", "date_field": "DATE ISSUED", "id_field": "PERMIT #",
    "lookback_days": 7, "limit": 500, "min_value": 250000, "match": {"WORK TYPE": ["New", "Addition"]},
    "required": ["ADDRESS"],
    "fields": {"address": "ADDRESS", "description": "PROJECT NAME", "work_class": "WORK TYPE",
               "permit_class": "PERMIT TYPE", "contractor": "CONTRACTOR", "value": "DECLARED VALUATION"}}

COUNCIL = {"id": "test-council", "name": "TEST City Council", "region": "TX", "tier": "official",
           "status": "active", "domains": ["testcity.legistar.com"]}


def as_store_source(src):
    """The shape Store.sources() returns, for calling collectors directly."""
    src = json.loads(json.dumps(src))
    extra = {k: v for k, v in src.items() if k not in S._SOURCE_COLS}
    return {**src, "config": extra}


class FakeClock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


# ── HTTP ────────────────────────────────────────────────────────────────
class TestHttp(unittest.TestCase):
    def test_canonical_redacts_secrets_and_sorts(self):
        u = canonical("https://api.sam.gov/x?state=TX&api_key=%s&limit=1" % KEY)
        self.assertNotIn(KEY, u)
        self.assertEqual(u, "https://api.sam.gov/x?api_key=%2A%2A%2A&limit=1&state=TX")
        self.assertNotIn("secret", canonical("https://w.legistar.com/v1/c/events?token=secret"))

    def test_one_request_per_five_seconds_per_host(self):
        fc = FakeClock()
        f = ReplayFetcher(fixture(), min_interval=5, sleep=fc.sleep, clock=fc.clock)
        for u in ("https://api.sam.gov/a", "https://api.sam.gov/b", "https://other.gov/a"):
            f.routes[canonical(u)] = {"status": 200, "body": "{}"}
        f._get("https://api.sam.gov/a")
        f._get("https://other.gov/a")      # different host: no wait
        fc.t += 1
        f._get("https://api.sam.gov/b")    # same host 1s later: waits 4s
        self.assertEqual(fc.slept, [4.0])

    def test_robots_disallow_blocks_before_any_request_to_the_page(self):
        f = ReplayFetcher(fixture("civicplus_disallowed.json"))
        with self.assertRaises(RobotsDisallowed):
            f.get_text("https://www.testcity-tx.gov/RSSFeed.aspx?ModID=65&CID=All-0", robots=True)
        self.assertEqual(len(f.calls), 1)
        self.assertTrue(f.calls[0].endswith("/robots.txt"))

    def test_robots_404_allows_and_5xx_disallows(self):
        f = ReplayFetcher({"requests": [
            {"url": "https://a.gov/robots.txt", "status": 404, "body": ""},
            {"url": "https://b.gov/robots.txt", "status": 503, "body": ""}]})
        self.assertTrue(f.allowed("https://a.gov/feed"))
        self.assertFalse(f.allowed("https://b.gov/feed"))
        self.assertFalse(f.allowed("https://c.gov/feed"))  # unreachable counts as disallow

    def test_errors_never_carry_the_key(self):
        f = ReplayFetcher({"requests": [{"url": sam_gov.API + "?api_key=x", "status": 401, "body": "no"}]})
        with self.assertRaises(FetchError) as cm:
            f.get_json(sam_gov.API, {"api_key": KEY})
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(FetchError) as cm:
            f.get_json(sam_gov.API, {"api_key": KEY, "state": "ZZ"})  # no recording
        self.assertNotIn(KEY, str(cm.exception))

    def test_recording_stores_redacted_urls(self):
        rf = RecordingFetcher(min_interval=0)
        with mock.patch.object(Fetcher, "_raw", return_value=(200, '{"ok": 1}')):
            rf.get_json(sam_gov.API, {"api_key": KEY, "state": "TX"})
        with tempfile.TemporaryDirectory() as d:
            rf.save(Path(d) / "f.json")
            text = (Path(d) / "f.json").read_text()
        self.assertNotIn(KEY, text)
        self.assertEqual(json.loads(text)["requests"][0]["body"], {"ok": 1})

    def test_fixtures_hold_no_secrets(self):
        for p in FIX.glob("*.json"):
            for r in json.loads(p.read_text())["requests"]:
                self.assertEqual(r["url"], canonical(r["url"]), p.name)
                self.assertNotRegex(r["url"], r"api_key=(?!%2A%2A%2A)", p.name)


class TestToDay(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(to_day("2026-11-19T00:00:00.000"), date(2026, 11, 19))
        self.assertEqual(to_day("11/19/2026"), date(2026, 11, 19))
        self.assertEqual(to_day(1795046400000), date(2026, 11, 19))
        self.assertIsNone(to_day("soon"))
        self.assertIsNone(to_day("2026-13-40"))


# ── SAM.gov ─────────────────────────────────────────────────────────────
class TestSamGov(unittest.TestCase):
    def collect(self, **cfg):
        src = as_store_source(real_source("sam-gov"))
        src["config"]["collector"].update(cfg)
        f = ReplayFetcher(fixture("sam_gov.json"))
        return sam_gov.collect(src, f, TODAY, env={"SAM_API_KEY": KEY}), f

    def test_key_required(self):
        with self.assertRaises(ConfigError):
            sam_gov.collect(as_store_source(real_source("sam-gov")), ReplayFetcher(fixture()), TODAY, env={})

    def test_one_query_per_state(self):
        _, f = self.collect()
        self.assertEqual(len(f.calls), 2)
        self.assertTrue(all("api_key=%2A%2A%2A" in c for c in f.calls))

    def test_keeps_construction_in_state_only(self):
        res, _ = self.collect()
        self.assertEqual(res.fetched, 7)
        ids = [i["external_id"] for i in res.items]
        # 0003 engineering NAICS, 0004 Oklahoma place of performance, 0005 surplus sale: dropped.
        self.assertEqual(ids, ["sam:TEST0001", "sam:TEST0002", "sam:TEST0006", "sam:TEST0101"])

    def test_item_shape(self):
        res, _ = self.collect()
        by = {i["external_id"]: i for i in res.items}
        a = by["sam:TEST0001"]
        self.assertEqual((a["date"], a["state"], a["place"]), ("2026-11-18", "TX", "Fort Cavazos"))
        self.assertEqual(a["project"], "TEST Barracks Renovation, Fort Hood (W9126G26R0001)")
        self.assertEqual(a["action"], "Solicitation posted by DEPT OF THE ARMY, W076 ENDIST FT WORTH; "
                                      "responses due 2026-12-15")
        self.assertEqual(a["source_url"], "https://sam.gov/opp/TEST0001/view")
        self.assertEqual(a["source_type"], "official")
        self.assertIn("Set-aside: Total Small Business", a["notes"])
        award = by["sam:TEST0002"]
        self.assertEqual(award["size_usd"], 1845000.0)
        self.assertIn("awarded to TEST ELECTRIC LLC", award["action"])
        self.assertEqual(award["trade_tags"], ["electrical"])
        self.assertEqual(award["place"], "San Antonio")
        co = by["sam:TEST0101"]
        self.assertEqual(co["place"], "Colorado")  # no city given
        self.assertEqual(co["source_url"], "https://sam.gov/opp/TEST0101/view")  # off-site uiLink replaced
        self.assertEqual(co["trade_tags"], ["site work"])

    def test_pagination_and_truncation_warning(self):
        fx = fixture("sam_gov.json")
        tx = fx["requests"][0]
        tx["body"]["totalRecords"] = 1500
        page2 = {**tx, "url": tx["url"].replace("offset=0", "offset=1000"),
                 "body": {"totalRecords": 1500, "opportunitiesData": []}}
        fx["requests"].append(page2)
        src = as_store_source(real_source("sam-gov"))
        f = ReplayFetcher(fx)
        res = sam_gov.collect(src, f, TODAY, env={"SAM_API_KEY": KEY})
        self.assertEqual(len(f.calls), 3)
        self.assertEqual(res.warnings, [])
        src["config"]["collector"]["max_pages"] = 1
        res = sam_gov.collect(src, ReplayFetcher(fx), TODAY, env={"SAM_API_KEY": KEY})
        self.assertEqual(len(res.warnings), 1)
        self.assertIn("1000 of 1500", res.warnings[0])

    def test_schema_change(self):
        fx = fixture("sam_gov.json")
        fx["requests"][0]["body"] = {"totalRecords": 0, "results": []}
        with self.assertRaises(SchemaError):
            sam_gov.collect(as_store_source(real_source("sam-gov")), ReplayFetcher(fx), TODAY,
                            env={"SAM_API_KEY": KEY})


# ── Permits ─────────────────────────────────────────────────────────────
class TestSocrata(unittest.TestCase):
    def collect(self, name):
        src = as_store_source(real_source("austin-permits"))
        f = ReplayFetcher(fixture(name))
        return socrata.collect(src, f, TODAY), f

    def test_items(self):
        res, f = self.collect("socrata_austin.json")
        self.assertEqual(res.newest_record, date(2026, 11, 19))
        self.assertTrue(res.tracks_freshness)
        # 004 has a known $90k value: under the floor. 003 has no value: kept.
        self.assertEqual([i["external_id"] for i in res.items],
                         ["3syk-w9eu:2026-TEST-001 BP", "3syk-w9eu:2026-TEST-002 BP", "3syk-w9eu:2026-TEST-003 BP"])
        a = res.items[0]
        self.assertEqual((a["place"], a["state"], a["project"]), ("Austin", "TX", "1200 E Test Blvd"))
        self.assertEqual(a["action"], "New Commercial permit issued: TEST new 3-story office building")
        self.assertEqual(a["size_usd"], 4200000.0)
        self.assertEqual(a["source_url"],
                         "https://data.austintexas.gov/resource/3syk-w9eu.json?permit_number=2026-TEST-001+BP")
        self.assertIn("Contractor of record: Test Builders Inc", a["notes"])
        self.assertIsNone(res.items[2]["size_usd"])

    def test_stale_dataset_pulls_no_rows(self):
        res, f = self.collect("socrata_austin_stale.json")
        self.assertEqual(res.newest_record, date(2026, 10, 1))
        self.assertEqual(res.items, [])
        self.assertEqual(len(f.calls), 1)  # only the freshness query

    def test_renamed_column_is_a_schema_change(self):
        with self.assertRaises(SchemaError) as cm:
            self.collect("socrata_austin_renamed.json")
        self.assertIn("original_address1", str(cm.exception))
        self.assertIn("site_address", str(cm.exception))

    def test_incomplete_block(self):
        src = as_store_source(real_source("austin-permits"))
        del src["config"]["collector"]["dataset"]
        with self.assertRaises(ConfigError):
            socrata.collect(src, ReplayFetcher(fixture()), TODAY)


class TestCkan(unittest.TestCase):
    def src(self, **cfg):
        return as_store_source(real_source("san-antonio-permits",
                                           collector={**SA_TEST_COLLECTOR, **cfg}))

    def test_real_block_refuses_to_run_until_filled_in(self):
        with self.assertRaises(ConfigError) as cm:
            ckan.collect(as_store_source(real_source("san-antonio-permits")), ReplayFetcher(fixture()), TODAY)
        for k in ("resource_id", "date_field", "id_field"):
            self.assertIn(k, str(cm.exception))

    def test_items(self):
        res = ckan.collect(self.src(), ReplayFetcher(fixture("ckan_san_antonio.json")), TODAY)
        self.assertEqual(res.newest_record, date(2026, 11, 19))
        # 2 is a Repair (match rule), 4 is before the 7-day window. 3 has value 0 = not filed: kept.
        self.assertEqual([i["external_id"] for i in res.items],
                         ["test-sa-permits:TEST-SA-001", "test-sa-permits:TEST-SA-003"])
        self.assertIsNone(res.items[1]["size_usd"])
        self.assertIn("filters=%7B%22PERMIT+%23%22%3A+%22TEST-SA-001%22%7D", res.items[0]["source_url"])

    def test_stale(self):
        res = ckan.collect(self.src(), ReplayFetcher(fixture("ckan_san_antonio_stale.json")), TODAY)
        self.assertEqual(res.newest_record, date(2026, 9, 30))
        self.assertEqual(res.items, [])

    def test_ckan_error(self):
        fx = fixture("ckan_san_antonio.json")
        fx["requests"][0]["body"] = {"success": False, "error": {"message": "Not found: Resource"}}
        with self.assertRaises(Exception) as cm:
            ckan.collect(self.src(), ReplayFetcher(fx), TODAY)
        self.assertIn("Not found", str(cm.exception))


# ── Agenda finder ───────────────────────────────────────────────────────
class TestAgendas(unittest.TestCase):
    def test_legistar(self):
        src = as_store_source({**COUNCIL, "collector": {"type": "legistar", "client": "testcity",
                                                        "bodies": ["City Council", "Planning Commission"]}})
        res = agendas.collect(src, ReplayFetcher(fixture("legistar.json")), TODAY)
        self.assertEqual(res.items, [])
        got = [(d["body"], d["meeting_date"], d["kind"]) for d in res.documents]
        self.assertEqual(got, [("City Council", "2026-11-17", "agenda"), ("City Council", "2026-11-17", "minutes"),
                               ("City Council", "2026-11-17", "meeting"),
                               ("Planning Commission", "2026-12-01", "agenda")])

    def test_civicplus_kinds_dates_and_window(self):
        src = as_store_source({**COUNCIL, "domains": ["testcity-tx.gov"], "collector": {
            "type": "civicplus", "rss_url": "https://www.testcity-tx.gov/RSSFeed.aspx?ModID=65&CID=All-0"}})
        res = agendas.collect(src, ReplayFetcher(fixture("civicplus.json")), TODAY)
        got = [(d["meeting_date"], d["kind"], d["body"]) for d in res.documents]
        # The Sep 1 agenda is outside the 45-day window; Nov 3 minutes posted Nov 18 are inside.
        self.assertEqual(got, [("2026-11-17", "agenda", "City Council"), ("2026-11-17", "packet", "City Council"),
                               ("2026-11-03", "minutes", "City Council")])

    def test_civicplus_respects_robots(self):
        src = as_store_source({**COUNCIL, "collector": {
            "type": "civicplus", "rss_url": "https://www.testcity-tx.gov/RSSFeed.aspx?ModID=65&CID=All-0"}})
        with self.assertRaises(RobotsDisallowed):
            agendas.collect(src, ReplayFetcher(fixture("civicplus_disallowed.json")), TODAY)

    def test_granicus(self):
        src = as_store_source({**COUNCIL, "collector": {"type": "granicus", "rss_urls": [
            "https://testcity.granicus.com/ViewPublisherRSS.php?view_id=2&mode=agendas"]}})
        res = agendas.collect(src, ReplayFetcher(fixture("granicus.json")), TODAY)
        got = [(d["meeting_date"], d["kind"], d["body"]) for d in res.documents]
        self.assertEqual(got, [("2026-11-19", "agenda", "City Council"), ("2026-11-05", "minutes", "City Council")])

    def test_not_rss(self):
        fx = fixture("granicus.json")
        fx["requests"][1]["body"] = "<html><body>maintenance</body></html>"
        src = as_store_source({**COUNCIL, "collector": {"type": "granicus", "rss_urls": [
            "https://testcity.granicus.com/ViewPublisherRSS.php?view_id=2&mode=agendas"]}})
        with self.assertRaises(SchemaError):
            agendas.collect(src, ReplayFetcher(fx), TODAY)


# ── Runner: the rules ───────────────────────────────────────────────────
class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = S.Store(":memory:")
        self.env = mock.patch.dict(os.environ, {"SAM_API_KEY": KEY})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.tmp.cleanup()

    def load(self, sources=None, **status):
        """Real allowlist, with optional status overrides by id and extra sources."""
        allow = json.loads(json.dumps(REAL_ALLOWLIST))
        for s in allow["sources"]:
            if s["id"] in status:
                s["status"] = status[s["id"]]
        allow["sources"] += sources or []
        p = self.dir / "allow.json"
        p.write_text(json.dumps(allow))
        self.store.load_allowlist(p)

    def run_all(self, *names, only=None):
        f = ReplayFetcher(fixture(*names))
        return {r["source"]: r for r in C.run_all(self.store, f, TODAY, only)}, f

    def alerts(self):
        return [(a["kind"], a["source"]) for a in self.store.open_alerts()]


class TestRunner(RunnerBase):
    def test_real_allowlist_runs_only_sam(self):
        self.load()
        res, f = self.run_all("sam_gov.json")
        self.assertEqual(res["sam-gov"]["status"], "ok")
        self.assertEqual(res["austin-permits"]["status"], "skipped")
        self.assertEqual(res["san-antonio-permits"]["status"], "skipped")
        self.assertTrue(all("api.sam.gov" in c for c in f.calls))  # no request to a to_confirm source
        self.assertEqual(self.alerts(), [])

    def test_sam_items_verified_amendment_merged_and_run_recorded(self):
        self.load()
        res, _ = self.run_all("sam_gov.json", only=["sam-gov"])
        self.assertEqual((res["sam-gov"]["new"], res["sam-gov"]["merged"]), (3, 1))
        paid = self.store.paid_eligible_items()
        self.assertEqual(len(paid), 3)
        barracks = next(r for r in paid if "Barracks" in r["project"])
        self.assertEqual(barracks["date"], "2026-11-18")        # anchored on the first notice
        self.assertEqual(barracks["last_seen_at"], "2026-11-19")  # amendment seen later
        run = self.store.conn.execute("SELECT * FROM runs WHERE source_id='sam-gov'").fetchone()
        self.assertEqual((run["status"], run["items_in"], run["items_new"], run["items_merged"]), ("ok", 7, 3, 1))

    def test_missing_key_alerts(self):
        self.load()
        with mock.patch.dict(os.environ, {"SAM_API_KEY": ""}):
            res, f = self.run_all("sam_gov.json", only=["sam-gov"])
        self.assertEqual(res["sam-gov"]["status"], "error")
        self.assertEqual(self.alerts(), [("collector_error", "sam-gov")])
        self.assertIn("SAM_API_KEY", self.store.open_alerts()[0]["message"])
        self.assertEqual(f.calls, [])

    def test_fetch_failure_alerts_without_key(self):
        self.load()
        res, _ = self.run_all(only=["sam-gov"])  # no recordings at all
        self.assertEqual(res["sam-gov"]["status"], "error")
        msg = self.store.open_alerts()[0]["message"]
        self.assertNotIn(KEY, msg)

    def test_active_permits_flow_through_the_gate(self):
        self.load(**{"austin-permits": "active"})
        res, _ = self.run_all("socrata_austin.json", only=["austin-permits"])
        self.assertEqual((res["austin-permits"]["new"], res["austin-permits"]["merged"]), (2, 1))  # same address
        self.assertEqual(len(self.store.paid_eligible_items(states=["TX"])), 2)
        # Pausing the source pulls its items from the next issue.
        self.load()
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_stale_dataset_alerts_and_stores_nothing(self):
        self.load(**{"austin-permits": "active"})
        res, _ = self.run_all("socrata_austin_stale.json", only=["austin-permits"])
        self.assertEqual(res["austin-permits"]["status"], "stale")
        self.assertEqual(self.alerts(), [("stale_dataset", "austin-permits")])
        self.assertIn("50 days old", self.store.open_alerts()[0]["message"])
        self.assertEqual(self.store.items(), [])

    def test_schema_change_alerts_and_stores_nothing(self):
        self.load(**{"austin-permits": "active"})
        res, _ = self.run_all("socrata_austin_renamed.json", only=["austin-permits"])
        self.assertEqual(res["austin-permits"]["status"], "error")
        self.assertEqual(self.alerts(), [("schema_change", "austin-permits")])
        self.assertEqual(self.store.items(), [])

    def test_unstorable_item_alerts_and_rest_still_stored(self):
        self.load()
        fx = fixture("sam_gov.json")
        fx["requests"][0]["body"]["opportunitiesData"][0]["postedDate"] = None
        C.run_all(self.store, ReplayFetcher(fx), TODAY, ["sam-gov"])
        self.assertEqual(self.alerts(), [("schema_change", "sam-gov")])
        self.assertEqual(len(self.store.items()), 3)

    def test_zero_items_two_runs_in_a_row(self):
        self.load()
        empty = fixture("sam_gov.json")
        for r in empty["requests"]:
            r["body"] = {"totalRecords": 0, "opportunitiesData": []}
        C.run_all(self.store, ReplayFetcher(empty), TODAY, ["sam-gov"])
        self.assertEqual(self.alerts(), [])
        C.run_all(self.store, ReplayFetcher(empty), TODAY, ["sam-gov"])
        self.assertEqual(self.alerts(), [("zero_items", "sam-gov")])

    def test_blocked_and_hard_blocked_never_run(self):
        evil = [{"id": "planhub-feed", "name": "x", "tier": "official", "status": "active",
                 "domains": ["planhub.com"], "collector": {"type": "socrata", "domain": "planhub.com",
                                                          "dataset": "x", "date_field": "d", "id_field": "i",
                                                          "place": "Austin", "state": "TX"}},
                {"id": "blocked-feed", "name": "y", "tier": "blocked", "status": "active",
                 "domains": ["example-bids.com"], "collector": {"type": "sam_gov"}}]
        self.load(evil)
        res, f = self.run_all(only=["planhub-feed", "blocked-feed"])
        self.assertEqual({r["status"] for r in res.values()}, {"skipped"})
        self.assertEqual(f.calls, [])
        self.assertEqual(self.store.items(), [])

    def test_unknown_collector_type_alerts(self):
        self.load([{**COUNCIL, "collector": {"type": "ftp"}}])
        res, _ = self.run_all(only=["test-council"])
        self.assertEqual(res["test-council"]["status"], "skipped")
        self.assertEqual(self.alerts(), [("collector_error", "test-council")])

    def test_agenda_links_go_to_documents_never_items(self):
        self.load([{**COUNCIL, "collector": {"type": "legistar", "client": "testcity"}}])
        res, _ = self.run_all("legistar.json", only=["test-council"])
        self.assertEqual(res["test-council"]["docs_new"], 5)
        self.assertEqual(self.store.items(), [])
        self.assertEqual(len(self.store.documents("test-council")), 5)
        self.assertNotIn("host not listed", res["test-council"]["note"])
        # Re-running stores no duplicates.
        res, _ = self.run_all("legistar.json", only=["test-council"])
        self.assertEqual(res["test-council"]["docs_new"], 0)

    def test_agenda_links_on_unlisted_host_are_flagged(self):
        self.load([{**COUNCIL, "domains": ["testcity.gov"], "collector": {"type": "legistar", "client": "testcity"}}])
        res, _ = self.run_all("legistar.json", only=["test-council"])
        self.assertIn("5 link(s) are on a host not listed", res["test-council"]["note"])


class TestCli(unittest.TestCase):
    def run_cli(self, *args, **env):
        with tempfile.TemporaryDirectory() as d:
            e = {**os.environ, "SIGNAL_FEED_DB": str(Path(d) / "t.db"), **env}
            return subprocess.run([sys.executable, str(ROOT / "scripts" / "collect.py"), *args],
                                  cwd=ROOT, env=e, capture_output=True, text=True)

    def test_list(self):
        out = self.run_cli("list").stdout
        self.assertRegex(out, r"sam-gov\s+sam_gov\s+official\s+active\s+runs")
        self.assertRegex(out, r"austin-permits\s+socrata\s+official\s+to_confirm\s+skips")

    def test_replay_run(self):
        p = self.run_cli("run", "--today", "2026-11-20", "--replay", str(FIX / "sam_gov.json"),
                         SAM_API_KEY=KEY)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
        sam = next(r for r in rows if r["source"] == "sam-gov")
        self.assertEqual((sam["new"], sam["merged"]), (3, 1))
        self.assertNotIn(KEY, p.stdout + p.stderr)

    def test_error_exit_code(self):
        p = self.run_cli("run", "--today", "2026-11-20", "--source", "sam-gov", SAM_API_KEY="")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ALERT [collector_error] sam-gov: SAM_API_KEY is not set", p.stdout)


if __name__ == "__main__":
    unittest.main()
