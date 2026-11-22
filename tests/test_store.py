"""Stage (a) tests: SQLite store, dedupe, gate invariants, JSONL audit export.

Run: python -m unittest discover -s signal-feed/tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import store as S  # noqa: E402

ALLOWLIST = {
    "sources": [
        {"domain": "austintexas.gov", "tier": "official", "status": "active", "state": "TX"},
        {"domain": "data.austintexas.gov", "tier": "official", "status": "active", "kind": "socrata"},
        {"domain": "sanantonio.gov", "tier": "official", "status": "to_confirm", "state": "TX"},
        {"domain": "auroragov.org", "tier": "official", "status": "active", "state": "CO"},
        {"domain": "coloradosun.com", "tier": "lead", "status": "active"},
        {"domain": "planhub.com", "tier": "official", "status": "active"},  # must be forced blocked
    ]
}


def row(**kw):
    base = {
        "observed_at": "2026-11-16",
        "metro_or_submarket": "Austin",
        "name": "Northeast Service Center",
        "state": "TX",
        "action": "council approved",
        "source_url": "https://www.austintexas.gov/edims/document.cfm?id=1",
    }
    base.update(kw)
    return base


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        allow = self.dir / "allowlist.json"
        allow.write_text(json.dumps(ALLOWLIST))
        self.store = S.Store(":memory:")
        self.store.load_allowlist(allow)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestSchema(StoreTestCase):
    def test_all_tables_exist(self):
        names = {r[0] for r in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"items", "sources", "runs", "alerts", "subscribers", "issues", "sends"} <= names)

    def test_init_is_idempotent_and_versioned(self):
        self.store.init()
        self.assertEqual(self.store.conn.execute("PRAGMA user_version").fetchone()[0], S.SCHEMA_VERSION)

    def test_newer_schema_refused(self):
        self.store.conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            self.store.init()

    def test_gate_status_check_constraint(self):
        with self.assertRaises(Exception):
            self.store.conn.execute(
                "INSERT INTO items (match_key, observed_at, last_seen_at, state, place, project, "
                "gate_status, raw_json, created_at, updated_at) VALUES "
                "('k','2026-01-01','2026-01-01','TX','a','b','approved','{}','x','x')")


class TestAllowlistAndClassify(StoreTestCase):
    def test_blocked_vendor_cannot_be_allowlisted(self):
        src = self.store.source_for_url("https://planhub.com/x")
        self.assertEqual((src["tier"], src["status"]), ("blocked", "blocked"))

    def test_blocked_list_holds_even_without_allowlist_entry(self):
        for url in ("https://www.constructconnect.com/p/1", "https://app.buildingconnected.com/x",
                    "https://dodge.construction.com/y"):
            self.assertEqual(self.store.classify(url), S.GATE_BLOCKED, url)

    def test_official_active_verifies(self):
        self.assertEqual(self.store.classify("https://austintexas.gov/a"), S.GATE_VERIFIED)

    def test_subdomain_matches_parent(self):
        self.assertEqual(self.store.classify("https://services.austintexas.gov/a"), S.GATE_VERIFIED)

    def test_lookalike_domain_does_not_match(self):
        self.assertEqual(self.store.classify("https://evilaustintexas.gov/a"), S.GATE_UNVERIFIED)

    def test_official_to_confirm_is_held(self):
        self.assertEqual(self.store.classify("https://sanantonio.gov/a"), S.GATE_UNVERIFIED)

    def test_lead_source_is_held(self):
        self.assertEqual(self.store.classify("https://coloradosun.com/2026/x"), S.GATE_UNVERIFIED)

    def test_unknown_and_missing_urls_are_held(self):
        self.assertEqual(self.store.classify("https://example.com"), S.GATE_UNVERIFIED)
        self.assertEqual(self.store.classify(""), S.GATE_UNVERIFIED)
        self.assertEqual(self.store.classify(None), S.GATE_UNVERIFIED)

    def test_grouped_allowlist_shape(self):
        p = self.dir / "grouped.json"
        p.write_text(json.dumps({"official": [{"domain": "denvergov.org", "status": "active"}],
                                 "leads": ["bizjournals.com"], "blocked": ["dodgedata.com"]}))
        self.store.load_allowlist(p)
        self.assertEqual(self.store.classify("https://denvergov.org/x"), S.GATE_VERIFIED)
        self.assertEqual(self.store.classify("https://bizjournals.com/x"), S.GATE_UNVERIFIED)
        self.assertEqual(self.store.classify("https://dodgedata.com/x"), S.GATE_BLOCKED)

    def test_official_without_status_defaults_to_to_confirm(self):
        p = self.dir / "nostatus.json"
        p.write_text(json.dumps([{"domain": "pueblo.us", "tier": "official"}]))
        self.store.load_allowlist(p)
        self.assertEqual(self.store.classify("https://pueblo.us/x"), S.GATE_UNVERIFIED)


class TestUpsertAndDedupe(StoreTestCase):
    def test_aliases_mapped(self):
        self.assertEqual(self.store.upsert_item(row()), "new")
        it = self.store.items()[0]
        self.assertEqual((it["place"], it["project"], it["state"]), ("Austin", "Northeast Service Center", "TX"))
        self.assertEqual(it["source_type"], "official")

    def test_same_key_within_window_merges(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(observed_at="2026-11-30",
                                                    source_url="https://austintexas.gov/b")), "merged")
        it = self.store.items()
        self.assertEqual(len(it), 1)
        self.assertEqual(it[0]["observed_at"], "2026-11-16")
        self.assertEqual(it[0]["last_seen_at"], "2026-11-30")
        self.assertEqual(len(json.loads(it[0]["source_urls"])), 2)

    def test_outside_window_is_new(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(observed_at="2026-12-01")), "new")
        self.assertEqual(len(self.store.items()), 2)

    def test_earlier_sighting_within_window_merges_and_moves_anchor(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(observed_at="2026-11-05T15:16:00Z")), "merged")
        self.assertEqual(self.store.items()[0]["observed_at"], "2026-11-05")

    def test_normalization_ignores_case_punctuation_and_ampersand(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(
            name="NORTHEAST  service-center", metro_or_submarket="austin", state="tx")), "merged")

    def test_and_vs_ampersand(self):
        self.store.upsert_item(row(name="Police & Fire HQ"))
        self.assertEqual(self.store.upsert_item(row(name="Police and Fire HQ")), "merged")

    def test_different_state_is_distinct(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(state="CO")), "new")

    def test_missing_required_fields_skipped(self):
        self.assertEqual(self.store.upsert_item(row(name="")), "skipped")
        self.assertEqual(self.store.upsert_item(row(state=None)), "skipped")

    def test_bad_date_raises(self):
        with self.assertRaises(ValueError):
            self.store.upsert_item(row(observed_at="next tuesday"))

    def test_lead_then_official_upgrades_and_keeps_lead(self):
        lead = "https://coloradosun.com/2026/11/16/aurora"
        official = "https://auroragov.org/agenda/1"
        self.store.upsert_item(row(state="CO", metro_or_submarket="Aurora", source_url=lead))
        self.assertEqual(self.store.items()[0]["gate_status"], S.GATE_UNVERIFIED)
        self.store.upsert_item(row(state="CO", metro_or_submarket="Aurora", source_url=official,
                                   observed_at="2026-11-18"))
        it = self.store.items()[0]
        self.assertEqual((it["gate_status"], it["official_url"], it["lead_url"]),
                         (S.GATE_VERIFIED, official, lead))
        self.assertEqual(it["source_type"], "official")

    def test_official_then_lead_stays_verified(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(source_url="https://coloradosun.com/x"))
        it = self.store.items()[0]
        self.assertEqual(it["gate_status"], S.GATE_VERIFIED)
        self.assertEqual(it["lead_url"], "https://coloradosun.com/x")

    def test_blocked_item_stays_blocked_even_if_official_arrives(self):
        self.store.upsert_item(row(source_url="https://planhub.com/p/1"))
        self.store.upsert_item(row())
        self.assertEqual(self.store.items()[0]["gate_status"], S.GATE_BLOCKED)

    def test_trade_tags_union_and_size(self):
        self.store.upsert_item(row(trade_tags=["steel"], value_usd="$1,200,000"))
        self.store.upsert_item(row(trade_tags=["concrete", "steel"]))
        it = self.store.items()[0]
        self.assertEqual(json.loads(it["trade_tags"]), ["concrete", "steel"])
        self.assertEqual(it["size_usd"], 1200000.0)


class TestGateCannotBeLaundered(StoreTestCase):
    """blocked and to_confirm sources can never reach paid_eligible_items."""

    def test_caller_cannot_verify_a_lead(self):
        self.store.upsert_item(row(source_url="https://coloradosun.com/x"), gate=S.GATE_VERIFIED)
        self.assertEqual(self.store.items()[0]["gate_status"], S.GATE_UNVERIFIED)
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_caller_cannot_verify_blocked(self):
        self.store.upsert_item(row(source_url="https://constructconnect.com/x"), gate=S.GATE_VERIFIED)
        self.assertEqual(self.store.items()[0]["gate_status"], S.GATE_BLOCKED)

    def test_caller_can_downgrade(self):
        self.store.upsert_item(row(), gate=S.GATE_UNVERIFIED)
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_to_confirm_official_never_eligible(self):
        self.store.upsert_item(row(metro_or_submarket="San Antonio", source_url="https://sanantonio.gov/x"))
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_source_paused_after_verify_drops_item(self):
        self.store.upsert_item(row())
        self.assertEqual(len(self.store.paid_eligible_items()), 1)
        self.store.upsert_source("austintexas.gov", tier="official", status="paused")
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_source_blocked_after_verify_drops_item(self):
        self.store.upsert_item(row())
        self.store.upsert_source("austintexas.gov", tier="blocked", status="blocked")
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_raw_sql_verified_without_official_url_not_eligible(self):
        self.store.upsert_item(row(source_url="https://coloradosun.com/x"))
        self.store.conn.execute("UPDATE items SET gate_status='verified'")
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_raw_sql_verified_with_lead_as_official_not_eligible(self):
        self.store.upsert_item(row(source_url="https://coloradosun.com/x"))
        self.store.conn.execute("UPDATE items SET gate_status='verified', official_url=lead_url")
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_state_and_since_filters(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(state="CO", metro_or_submarket="Aurora",
                                   source_url="https://auroragov.org/a"))
        self.assertEqual([r["state"] for r in self.store.paid_eligible_items(states=["co"])], ["CO"])
        self.assertEqual(self.store.paid_eligible_items(since="2026-11-17"), [])


class TestConfirmItem(StoreTestCase):
    def test_confirm_with_official_url(self):
        self.store.upsert_item(row(source_url="https://coloradosun.com/x"))
        item_id = self.store.held_items()[0]["id"]
        self.assertTrue(self.store.confirm_item(item_id, "https://austintexas.gov/packet.pdf"))
        it = self.store.items()[0]
        self.assertEqual((it["gate_status"], it["lead_url"]), (S.GATE_VERIFIED, "https://coloradosun.com/x"))
        self.assertEqual(len(self.store.paid_eligible_items()), 1)

    def test_confirm_refuses_non_official_url(self):
        self.store.upsert_item(row(source_url="https://coloradosun.com/x"))
        item_id = self.store.held_items()[0]["id"]
        for bad in ("https://coloradosun.com/y", "https://sanantonio.gov/z", "https://planhub.com/q"):
            self.assertFalse(self.store.confirm_item(item_id, bad), bad)
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_confirm_refuses_blocked_item(self):
        self.store.upsert_item(row(source_url="https://planhub.com/x"))
        item_id = self.store.items()[0]["id"]
        self.assertFalse(self.store.confirm_item(item_id, "https://austintexas.gov/a"))


class TestImportExportRunsAlerts(StoreTestCase):
    def _ledger(self, lines):
        p = self.dir / "ledger.jsonl"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_import_counts_and_run_record(self):
        p = self._ledger([json.dumps(row()), json.dumps(row(observed_at="2026-11-20")),
                          "", "# comment", json.dumps(row(name=""))])
        self.assertEqual(self.store.import_ledger(p), {"new": 1, "merged": 1, "skipped": 1, "bad": 0})
        run = self.store.conn.execute("SELECT * FROM runs").fetchone()
        self.assertEqual((run["kind"], run["status"], run["items_new"], run["items_merged"]),
                         ("import", "ok", 1, 1))

    def test_bad_row_raises_schema_alert_without_aborting(self):
        p = self._ledger([json.dumps(row(observed_at="garbage")), json.dumps(row())])
        self.assertEqual(self.store.import_ledger(p)["bad"], 1)
        self.assertEqual([a["kind"] for a in self.store.open_alerts()], ["schema_change"])

    def test_malformed_json_aborts_with_error_run_and_alert(self):
        p = self._ledger([json.dumps(row()), "{not json"])
        with self.assertRaises(ValueError):
            self.store.import_ledger(p)
        self.assertEqual(self.store.conn.execute("SELECT status FROM runs").fetchone()[0], "error")
        self.assertEqual(self.store.open_alerts()[0]["kind"], "collector_error")

    def test_resolve_alert(self):
        aid = self.store.add_alert("stale_dataset", "newest record 20 days old", source="data.austintexas.gov")
        self.store.resolve_alert(aid)
        self.assertEqual(self.store.open_alerts(), [])

    def test_export_round_trip(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(name="Other", source_url="https://coloradosun.com/x"))
        out = self.dir / "audit.jsonl"
        self.assertEqual(self.store.export_jsonl(out), 2)
        recs = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual({r["gate_status"] for r in recs}, {"verified", "unverified"})
        self.assertIsInstance(recs[0]["raw_json"], dict)

    def test_file_db_persists(self):
        path = self.dir / "signal_feed.db"
        s = S.Store(path)
        s.upsert_source("austintexas.gov", tier="official", status="active")
        s.upsert_item(row())
        s.close()
        s2 = S.Store(path)
        self.assertEqual(len(s2.paid_eligible_items()), 1)
        s2.close()

    def test_cli_import_and_stats(self):
        db = self.dir / "cli.db"
        allow = self.dir / "allowlist.json"
        p = self._ledger([json.dumps(row())])
        self.assertEqual(S.main(["--db", str(db), "import", str(p), "--allowlist", str(allow)]), 0)
        self.assertEqual(S.main(["--db", str(db), "stats"]), 0)


if __name__ == "__main__":
    unittest.main()
