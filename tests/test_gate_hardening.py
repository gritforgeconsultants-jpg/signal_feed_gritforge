"""Stage (b) gate and store hardening: most-specific domain match, shared vendor domains,
schema v2 (documents), run history.

Run: python -m unittest discover -s tests -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import signal_feed as SF  # noqa: E402
import store as S  # noqa: E402

PERMITS = {"id": "austin-permits", "name": "Austin permits", "tier": "official",
           "domains": ["data.austintexas.gov"], "status": "to_confirm"}
COUNCIL = {"id": "austin-council", "name": "Austin council", "tier": "official",
           "domains": ["austintexas.gov"], "status": "active"}
PERMIT_URL = "https://data.austintexas.gov/resource/3syk-w9eu.json?permit_number=1"


def item(url):
    return {"date": "2026-11-18", "place": "Austin", "state": "TX", "project": "P",
            "action": "permit issued", "source_url": url}


class TestMostSpecificDomain(unittest.TestCase):
    def test_subdomain_source_wins_in_either_order(self):
        # Before the fix, putting the council first let to_confirm permit items pass.
        for order in ([PERMITS, COUNCIL], [COUNCIL, PERMITS]):
            src = SF.match_source("data.austintexas.gov", order)
            self.assertEqual(src["id"], "austin-permits")
            ok, reason = SF.classify(item(PERMIT_URL), order)
            self.assertFalse(ok)
            self.assertIn("to_confirm", reason)

    def test_parent_domain_still_matches_its_own_pages(self):
        src = SF.match_source("www.austintexas.gov".removeprefix("www."), [PERMITS, COUNCIL])
        self.assertEqual(src["id"], "austin-council")

    def test_tie_goes_to_the_stricter_source(self):
        dup = {**COUNCIL, "id": "dup", "status": "to_confirm"}
        lead = {**COUNCIL, "id": "lead", "tier": "lead"}
        for order in ([COUNCIL, dup], [dup, COUNCIL]):
            self.assertEqual(SF.match_source("austintexas.gov", order)["id"], "dup")
        self.assertEqual(SF.match_source("austintexas.gov", [COUNCIL, lead])["id"], "lead")


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = S.Store(":memory:")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def allow(self, sources):
        p = self.dir / "allow.json"
        p.write_text(json.dumps({"sources": sources}))
        return self.store.load_allowlist(p)


class TestSharedVendorDomains(StoreBase):
    def test_vendor_apex_is_dropped_and_alerted(self):
        self.allow([{"id": "sa-council", "name": "SA", "tier": "official", "status": "active",
                     "domains": ["sanantonio.gov", "legistar.com", "www.granicus.com"]}])
        self.assertEqual(self.store.source("sa-council")["domains"], ["sanantonio.gov"])
        kinds = [a["kind"] for a in self.store.open_alerts()]
        self.assertEqual(kinds, ["allowlist_error", "allowlist_error"])
        # Another city's Legistar file can no longer pass as San Antonio's.
        self.assertFalse(self.store.is_official_active("https://dallas.legistar.com/View.ashx?ID=1"))

    def test_city_subdomain_on_vendor_is_kept(self):
        self.allow([{"id": "sa-council", "name": "SA", "tier": "official", "status": "active",
                     "domains": ["sanantonio.legistar.com"]}])
        self.assertTrue(self.store.is_official_active("https://sanantonio.legistar.com/View.ashx?ID=1"))
        self.assertFalse(self.store.is_official_active("https://dallas.legistar.com/View.ashx?ID=1"))
        self.assertEqual(self.store.open_alerts(), [])

    def test_store_gate_uses_most_specific_match(self):
        self.allow([COUNCIL, PERMITS])
        status, reason = self.store.gate(item(PERMIT_URL))
        self.assertEqual(status, S.GATE_UNVERIFIED)
        self.assertIn("to_confirm", reason)

    def test_sources_expose_config(self):
        self.allow([{**PERMITS, "collector": {"type": "socrata", "dataset": "x"}}])
        self.assertEqual(self.store.source("austin-permits")["config"]["collector"]["type"], "socrata")


class TestSchemaV2(StoreBase):
    def test_v1_database_upgrades_in_place(self):
        path = self.dir / "v1.db"
        conn = sqlite3.connect(path)
        v1 = S.SCHEMA.split("CREATE TABLE IF NOT EXISTS documents")[0]
        v1 = "\n".join(l for l in v1.split("\n") if "-- v3" not in l).replace(
            "updated_at TEXT NOT NULL,\n);", "updated_at TEXT NOT NULL\n);")
        conn.executescript(v1)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO items (match_key, date, last_seen_at, state, place, raw_json, created_at, "
                     "updated_at) VALUES ('k','2026-01-01','2026-01-01','TX','a','{}','x','x')")
        conn.commit()
        conn.close()
        st = S.Store(path)
        try:
            self.assertEqual(st.conn.execute("PRAGMA user_version").fetchone()[0], S.SCHEMA_VERSION)
            cols = {r[1] for r in st.conn.execute("PRAGMA table_info(items)")}
            self.assertTrue({"event_id", "supersedes", "superseded_by"} <= cols)
            self.assertEqual(len(st.held_items()), 1)  # old rows survive and are not superseded
            self.assertEqual(st.documents(), [])
        finally:
            st.close()

    def test_documents_dedupe_on_url(self):
        doc = {"source_id": "sa-council", "kind": "agenda", "url": "https://x.gov/a.pdf",
               "meeting_date": "2026-11-17", "body": "City Council"}
        self.assertTrue(self.store.upsert_document(doc))
        self.assertFalse(self.store.upsert_document(doc))
        self.assertEqual(len(self.store.documents("sa-council")), 1)
        self.assertEqual(self.store.items(), [])  # documents never become items

    def test_recent_runs_ignores_skipped_and_unfinished(self):
        for status in ("ok", "skipped", "ok"):
            rid = self.store.start_run("collect", "s")
            self.store.finish_run(rid, status)
        self.store.start_run("collect", "s")  # still running
        runs = self.store.recent_runs("s", n=5)
        self.assertEqual([r["status"] for r in runs], ["ok", "ok"])


if __name__ == "__main__":
    unittest.main()
