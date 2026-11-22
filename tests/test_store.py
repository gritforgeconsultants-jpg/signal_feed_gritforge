"""Stage (a) tests: SQLite store, dedupe, gate backstop, JSONL audit export.

Run: python -m unittest discover -s signal-feed/tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import signal_feed as SF  # noqa: E402
import store as S  # noqa: E402

REAL_ALLOWLIST = ROOT / "config" / "allowlist.json"

ALLOWLIST = {"sources": [
    {"id": "sam-gov", "name": "SAM.gov", "region": "US", "tier": "official",
     "domains": ["sam.gov", "api.sam.gov"], "status": "active"},
    {"id": "austin-council", "name": "Austin council", "region": "TX", "tier": "official",
     "domains": ["austintexas.gov"], "status": "active"},
    {"id": "aurora", "name": "Aurora", "region": "CO", "tier": "official",
     "domains": ["auroragov.org"], "status": "active", "agenda_url": "https://auroragov.org/agendas"},
    {"id": "sa-council", "name": "San Antonio council", "region": "TX", "tier": "official",
     "domains": ["sanantonio.gov"], "status": "to_confirm"},
    {"id": "red-oak-council", "name": "Red Oak", "region": "TX", "tier": "official",
     "domains": [], "status": "to_confirm"},
    {"id": "co-sun", "name": "Colorado Sun", "region": "CO", "tier": "lead",
     "domains": ["coloradosun.com"], "status": "active"},
    {"id": "myopentexas", "name": "myopentexas", "region": "TX", "tier": "ai_summary",
     "domains": ["myopentexas.org"], "status": "active"},
    {"id": "eia", "name": "EIA", "region": "US", "tier": "context",
     "domains": ["eia.gov"], "status": "active"},
    # An allowlist mistake: a paid bid network marked official. The store must still block it.
    {"id": "oops", "name": "PlanHub", "region": "US", "tier": "official",
     "domains": ["planhub.com"], "status": "active"},
]}

AUSTIN = "https://www.austintexas.gov/edims/document.cfm?id=1"
LEAD = "https://coloradosun.com/2026/11/16/x"


def row(**kw):
    """A row in the growth-watch ledger's field names."""
    base = {"observed_at": "2026-11-16T09:05:00-07:00", "metro_or_submarket": "Austin",
            "name": "Northeast Service Center", "state": "TX", "action": "council approved",
            "source_url": AUSTIN}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.allow = self.dir / "allowlist.json"
        self.allow.write_text(json.dumps(ALLOWLIST))
        self.store = S.Store(":memory:")
        self.store.load_allowlist(self.allow)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def only(self):
        items = self.store.items()
        self.assertEqual(len(items), 1)
        return items[0]


class TestSchema(Base):
    def test_all_tables_exist(self):
        names = {r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"items", "sources", "runs", "alerts", "subscribers", "issues", "sends"} <= names)

    def test_init_idempotent_and_versioned(self):
        self.store.init()
        self.assertEqual(self.store.conn.execute("PRAGMA user_version").fetchone()[0], S.SCHEMA_VERSION)

    def test_newer_schema_refused(self):
        self.store.conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            self.store.init()

    def test_gate_status_constraint(self):
        with self.assertRaises(Exception):
            self.store.conn.execute(
                "INSERT INTO items (match_key, date, last_seen_at, state, place, gate_status, raw_json, "
                "created_at, updated_at) VALUES ('k','2026-01-01','2026-01-01','TX','a','approved','{}','x','x')")


class TestSources(Base):
    def test_extra_allowlist_keys_kept_in_config(self):
        cfg = self.store.conn.execute("SELECT config_json FROM sources WHERE id='aurora'").fetchone()[0]
        self.assertEqual(json.loads(cfg)["agenda_url"], "https://auroragov.org/agendas")

    def test_removed_source_is_deleted_on_resync(self):
        trimmed = {"sources": [s for s in ALLOWLIST["sources"] if s["id"] != "austin-council"]}
        self.allow.write_text(json.dumps(trimmed))
        self.store.load_allowlist(self.allow)
        self.assertIsNone(self.store.source_for_url(AUSTIN))

    def test_paid_network_cannot_be_allowlisted_as_official(self):
        self.assertEqual(self.store.conn.execute("SELECT tier FROM sources WHERE id='oops'").fetchone()[0],
                         "blocked")

    def test_hard_block_without_allowlist_entry(self):
        for url in ("https://dodge.construction.com/p", "https://constructconnect.com/p",
                    "https://app.buildingconnected.com/p"):
            self.assertTrue(self.store.is_blocked_url(url), url)

    def test_real_allowlist_loads(self):
        n = self.store.load_allowlist(REAL_ALLOWLIST)
        self.assertEqual(n, len(json.loads(REAL_ALLOWLIST.read_text())["sources"]))
        self.assertTrue(self.store.is_official_active("https://sam.gov/opp/1"))
        self.assertFalse(self.store.is_official_active("https://data.austintexas.gov/x"))  # to_confirm


class TestGatePaths(Base):
    """Every path ends in exactly the status the kit plus backstop should give it."""

    def gate(self, **kw):
        return self.store.gate(SF.apply_aliases(row(**kw)))[0]

    def test_official_active(self):
        self.assertEqual(self.gate(), S.GATE_VERIFIED)

    def test_subdomain(self):
        self.assertEqual(self.gate(source_url="https://services.austintexas.gov/x"), S.GATE_VERIFIED)

    def test_lookalike(self):
        self.assertEqual(self.gate(source_url="https://evilaustintexas.gov/x"), S.GATE_UNVERIFIED)

    def test_to_confirm(self):
        self.assertEqual(self.gate(source_url="https://sanantonio.gov/x"), S.GATE_UNVERIFIED)

    def test_lead_ai_summary_context(self):
        for url in (LEAD, "https://myopentexas.org/x", "https://www.eia.gov/x"):
            self.assertEqual(self.gate(source_url=url), S.GATE_UNVERIFIED, url)

    def test_non_official_source_type_on_official_domain(self):
        self.assertEqual(self.gate(source_type="trade_press"), S.GATE_UNVERIFIED)

    def test_missing_url_and_unknown_domain(self):
        self.assertEqual(self.gate(source_url=""), S.GATE_UNVERIFIED)
        self.assertEqual(self.gate(source_url="https://example.com"), S.GATE_UNVERIFIED)

    def test_blocked(self):
        self.assertEqual(self.gate(source_url="https://planhub.com/x"), S.GATE_BLOCKED)
        self.assertEqual(self.gate(source_url="https://constructconnect.com/x", source_type="trade_press"),
                         S.GATE_BLOCKED)


class TestUpsertAndDedupe(Base):
    def test_ledger_aliases_and_timestamp(self):
        self.assertEqual(self.store.upsert_item(row()), "new")
        it = self.only()
        self.assertEqual((it["date"], it["place"], it["project"], it["state"]),
                         ("2026-11-16", "Austin", "Northeast Service Center", "TX"))
        self.assertEqual((it["gate_status"], it["official_url"], it["source_type"]),
                         (S.GATE_VERIFIED, AUSTIN, "official"))

    def test_within_window_merges(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(observed_at="2026-11-30",
                                                    source_url="https://austintexas.gov/b")), "merged")
        it = self.only()
        self.assertEqual((it["date"], it["last_seen_at"]), ("2026-11-16", "2026-11-30"))
        self.assertEqual(len(json.loads(it["source_urls"])), 2)
        self.assertEqual(it["official_url"], AUSTIN)

    def test_outside_window_is_new(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(observed_at="2026-12-01")), "new")

    def test_earlier_sighting_moves_anchor(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(observed_at="2026-11-05"))
        self.assertEqual(self.only()["date"], "2026-11-05")

    def test_normalization(self):
        self.store.upsert_item(row(name="Police & Fire HQ"))
        self.assertEqual(self.store.upsert_item(row(name="POLICE and fire  H.Q.", metro_or_submarket="austin",
                                                    state="tx")), "new")  # H.Q. -> "h q", not "hq"
        self.assertEqual(self.store.upsert_item(row(name="police and fire-hq")), "merged")

    def test_different_state_distinct(self):
        self.store.upsert_item(row())
        self.assertEqual(self.store.upsert_item(row(state="CO")), "new")

    def test_no_project_falls_back_to_action_head(self):
        self.store.upsert_item(row(name="", action="Council approved the Rosenberg rail park deal today"))
        self.assertEqual(self.store.upsert_item(row(
            name="", action="Council approved the Rosenberg rail park agreement, 6-0")), "merged")
        self.assertEqual(self.store.upsert_item(row(name="", action="Zoning denied")), "new")

    def test_unstorable_rows_raise(self):
        for bad in (row(metro_or_submarket=""), row(state=None), row(observed_at=""),
                    row(observed_at="next tuesday")):
            with self.assertRaises(ValueError):
                self.store.upsert_item(bad)

    def test_lead_then_official_upgrades_and_keeps_lead(self):
        self.store.upsert_item(row(state="CO", metro_or_submarket="Aurora", source_url=LEAD))
        self.assertEqual(self.only()["gate_status"], S.GATE_UNVERIFIED)
        self.store.upsert_item(row(state="CO", metro_or_submarket="Aurora",
                                   source_url="https://auroragov.org/a/1", observed_at="2026-11-18"))
        it = self.only()
        self.assertEqual((it["gate_status"], it["official_url"], it["lead_url"], it["source_type"]),
                         (S.GATE_VERIFIED, "https://auroragov.org/a/1", LEAD, "official"))
        self.assertTrue(it["gate_reason"].startswith("official"))

    def test_official_then_lead_stays_verified(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(source_url=LEAD))
        it = self.only()
        self.assertEqual((it["gate_status"], it["official_url"], it["lead_url"]),
                         (S.GATE_VERIFIED, AUSTIN, LEAD))

    def test_blocked_is_sticky_in_both_orders(self):
        self.store.upsert_item(row(source_url="https://planhub.com/p/1"))
        self.store.upsert_item(row())
        self.assertEqual(self.only()["gate_status"], S.GATE_BLOCKED)
        self.assertIsNone(self.only()["official_url"])
        self.store.upsert_item(row(name="Other"))
        self.store.upsert_item(row(name="Other", source_url="https://planhub.com/p/2"))
        other = [i for i in self.store.items() if i["project"] == "Other"][0]
        self.assertEqual((other["gate_status"], other["official_url"]), (S.GATE_BLOCKED, None))

    def test_tags_size_and_kit_fields(self):
        self.store.upsert_item(row(trade_tags=["steel"], value_usd="$1,200,000", county="Travis County",
                                   why="Big job", source_name="Austin council"))
        self.store.upsert_item(row(trade_tags=["concrete", "steel"], notes="Lawsuit pending"))
        it = self.only()
        self.assertEqual(json.loads(it["trade_tags"]), ["concrete", "steel"])
        self.assertEqual((it["size_usd"], it["county"], it["why"], it["notes"], it["source_name"]),
                         (1200000.0, "Travis County", "Big job", "Lawsuit pending", "Austin council"))


class TestNeverReachesSubscriber(Base):
    """blocked and to_confirm sources can never come out of paid_eligible_items()."""

    def test_caller_cannot_upgrade(self):
        self.store.upsert_item(row(source_url=LEAD), gate=S.GATE_VERIFIED)
        self.store.upsert_item(row(name="B", source_url="https://constructconnect.com/x"), gate=S.GATE_VERIFIED)
        self.assertEqual({i["gate_status"] for i in self.store.items()}, {S.GATE_UNVERIFIED, S.GATE_BLOCKED})
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_backstop_holds_if_kit_gate_regresses(self):
        from unittest import mock
        with mock.patch.object(S.SF, "classify", return_value=(True, "official: bug")):
            for url in (LEAD, "https://sanantonio.gov/x", "https://planhub.com/x", ""):
                self.assertNotEqual(self.store.gate(SF.apply_aliases(row(source_url=url)))[0],
                                    S.GATE_VERIFIED, url)

    def test_caller_can_downgrade(self):
        self.store.upsert_item(row(), gate=S.GATE_UNVERIFIED)
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_to_confirm_never_eligible(self):
        self.store.upsert_item(row(metro_or_submarket="San Antonio", source_url="https://sanantonio.gov/x"))
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_source_paused_after_verify_drops_item(self):
        self.store.upsert_item(row())
        self.assertEqual(len(self.store.paid_eligible_items()), 1)
        self.store.set_source_status("austin-council", "to_confirm")
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_raw_sql_tampering_not_eligible(self):
        self.store.upsert_item(row(source_url=LEAD))
        self.store.conn.execute("UPDATE items SET gate_status='verified'")
        self.assertEqual(self.store.paid_eligible_items(), [])
        self.store.conn.execute("UPDATE items SET official_url=lead_url")
        self.assertEqual(self.store.paid_eligible_items(), [])
        self.store.conn.execute("UPDATE items SET official_url='https://planhub.com/x'")
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_state_and_since_filters(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(state="CO", metro_or_submarket="Aurora", source_url="https://auroragov.org/a"))
        self.assertEqual([r["state"] for r in self.store.paid_eligible_items(states=["co"])], ["CO"])
        self.assertEqual(self.store.paid_eligible_items(since="2026-11-17"), [])


class TestConfirmItem(Base):
    def test_confirm_with_official_url(self):
        self.store.upsert_item(row(source_url=LEAD))
        iid = self.store.held_items()[0]["id"]
        self.assertTrue(self.store.confirm_item(iid, "https://austintexas.gov/packet.pdf"))
        it = self.only()
        self.assertEqual((it["gate_status"], it["lead_url"]), (S.GATE_VERIFIED, LEAD))
        self.assertEqual(len(self.store.paid_eligible_items()), 1)

    def test_confirm_refuses_non_official(self):
        self.store.upsert_item(row(source_url=LEAD))
        iid = self.store.held_items()[0]["id"]
        for bad in ("https://coloradosun.com/y", "https://sanantonio.gov/z", "https://planhub.com/q", ""):
            self.assertFalse(self.store.confirm_item(iid, bad), bad)
        self.assertEqual(self.store.paid_eligible_items(), [])

    def test_confirm_refuses_blocked_item(self):
        self.store.upsert_item(row(source_url="https://planhub.com/x"))
        self.assertFalse(self.store.confirm_item(self.only()["id"], AUSTIN))


class TestImportExport(Base):
    def ledger(self, lines):
        p = self.dir / "ledger.jsonl"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_counts_and_run_record(self):
        p = self.ledger([json.dumps(row()), json.dumps(row(observed_at="2026-11-20")), ""])
        self.assertEqual(self.store.import_ledger(p), {"new": 1, "merged": 1, "bad": 0})
        run = self.store.conn.execute("SELECT * FROM runs").fetchone()
        self.assertEqual((run["kind"], run["status"], run["items_new"], run["items_merged"]), ("import", "ok", 1, 1))

    def test_bad_row_alerts_without_aborting(self):
        p = self.ledger([json.dumps(row(observed_at="garbage")), json.dumps(row())])
        self.assertEqual(self.store.import_ledger(p)["bad"], 1)
        self.assertEqual([a["kind"] for a in self.store.open_alerts()], ["schema_change"])

    def test_malformed_json_aborts_with_error_run(self):
        p = self.ledger([json.dumps(row()), "{not json"])
        with self.assertRaises(ValueError):
            self.store.import_ledger(p)
        self.assertEqual(self.store.conn.execute("SELECT status FROM runs").fetchone()[0], "error")
        self.assertEqual(self.store.open_alerts()[0]["kind"], "collector_error")

    def test_marked_confirmed_but_unverified_alerts(self):
        p = self.ledger([json.dumps(row(source_url=LEAD, status="confirmed")), json.dumps(row(status="confirmed"))])
        self.store.import_ledger(p)
        self.assertEqual([a["kind"] for a in self.store.open_alerts()], ["marked_confirmed_unverified"])

    def test_resolve_alert(self):
        aid = self.store.add_alert("stale_dataset", "newest record 20 days old", source="austin-permits")
        self.store.resolve_alert(aid)
        self.assertEqual(self.store.open_alerts(), [])

    def test_export_round_trip(self):
        self.store.upsert_item(row())
        self.store.upsert_item(row(name="Other", source_url=LEAD))
        out = self.dir / "audit.jsonl"
        self.assertEqual(self.store.export_jsonl(out), 2)
        recs = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual({r["gate_status"] for r in recs}, {"verified", "unverified"})
        self.assertEqual(recs[0]["raw_json"]["metro_or_submarket"], "Austin")

    def test_file_db_persists(self):
        path = self.dir / "signal_feed.db"
        s = S.Store(path)
        s.load_allowlist(self.allow)
        s.upsert_item(row())
        s.close()
        s2 = S.Store(path)
        self.assertEqual(len(s2.paid_eligible_items()), 1)
        s2.close()

    def test_cli(self):
        db = self.dir / "cli.db"
        p = self.ledger([json.dumps(row())])
        self.assertEqual(S.main(["--db", str(db), "import", str(p), "--allowlist", str(self.allow)]), 0)
        self.assertEqual(S.main(["--db", str(db), "stats"]), 0)
        self.assertEqual(S.main(["--db", str(db), "export", str(self.dir / "a.jsonl")]), 0)


class TestAgreesWithKit(unittest.TestCase):
    """On the kit's own allowlist and samples, the store reaches the same verdicts as verify."""

    def setUp(self):
        self.store = S.Store(":memory:")
        self.store.load_allowlist(REAL_ALLOWLIST)

    def tearDown(self):
        self.store.close()

    def test_fixtures(self):
        counts = self.store.import_ledger(ROOT / "samples" / "test-fixtures.jsonl")
        self.assertEqual(counts, {"new": 4, "merged": 1, "bad": 0})
        by_place = {i["place"]: i["gate_status"] for i in self.store.items()}
        self.assertEqual(by_place, {"TEST Denver": "verified", "TEST Phoenix": "verified",
                                    "TEST Austin": "unverified", "TEST Houston": "blocked"})
        self.assertEqual([a["kind"] for a in self.store.open_alerts()], ["marked_confirmed_unverified"])
        self.assertEqual([i["place"] for i in self.store.paid_eligible_items(states=["TX", "CO"])],
                         ["TEST Denver"])

    def test_sample_items_all_held(self):
        self.store.import_ledger(ROOT / "samples" / "sample-items.jsonl")
        self.assertEqual(len(self.store.held_items()), 3)
        self.assertEqual(self.store.paid_eligible_items(), [])


if __name__ == "__main__":
    unittest.main()
