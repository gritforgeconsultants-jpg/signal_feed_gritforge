"""The growth-watch ledger (events.jsonl): field mapping, gate, read-only import, LEDGER_PATH.

tests/fixtures/ledger/events-sample.jsonl is a copied real line. The real file lives on
the Grok Bot machine and is never read by tests.
Run: python -m unittest discover -s tests -v
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import signal_feed as SF  # noqa: E402
import store as S  # noqa: E402

SAMPLE = ROOT / "tests" / "fixtures" / "ledger" / "events-sample.jsonl"
ALLOWLIST = ROOT / "config" / "allowlist.json"
SOURCES = json.loads(ALLOWLIST.read_text())["sources"]


def sample_row():
    return json.loads(SAMPLE.read_text().splitlines()[0])


class TestMapping(unittest.TestCase):
    def test_sample_maps_to_gate_fields(self):
        it = SF.apply_aliases(sample_row())
        self.assertEqual(it["date"], "2026-11-20")
        self.assertEqual(it["place"], "Aspen / Pitkin County CO")
        self.assertTrue(it["project"].startswith("Aspen voters approve Ballot Issue 2A"))
        self.assertEqual(it["action"], it["project"])  # no action field in the ledger

    def test_sample_is_held_for_its_source_not_for_missing_fields(self):
        ok, reason = SF.classify(SF.apply_aliases(sample_row()), SOURCES)
        self.assertFalse(ok)
        self.assertEqual(reason, "source voterdecoder.org is not on the allowlist")

    def test_ledger_row_on_official_active_domain_verifies(self):
        # Before the name -> action fallback, this was held as "missing action".
        row = dict(sample_row(), source_url="https://sam.gov/opp/TEST/view")
        self.assertTrue(SF.classify(SF.apply_aliases(row), SOURCES)[0])

    def test_explicit_action_still_wins(self):
        it = SF.apply_aliases(dict(sample_row(), action="bond approved"))
        self.assertEqual(it["action"], "bond approved")


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / "events.jsonl"
        self.ledger.write_bytes(SAMPLE.read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, script="store.py", **env):
        e = {k: v for k, v in os.environ.items() if k != "LEDGER_PATH"}
        e.update(SIGNAL_FEED_DB=str(self.dir / "t.db"), **env)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], cwd=ROOT,
                              env=e, capture_output=True, text=True)

    def test_import_holds_sample_and_keeps_ledger_fields(self):
        st = S.Store(":memory:")
        try:
            st.load_allowlist(ALLOWLIST)
            self.assertEqual(st.import_ledger(self.ledger), {"new": 1, "merged": 0, "bad": 0})
            row = st.items()[0]
            self.assertEqual(row["gate_status"], "unverified")
            self.assertEqual(row["size_usd"], 100000000.0)
            self.assertEqual(row["lead_url"], sample_row()["source_url"])
            raw = json.loads(row["raw_json"])
            self.assertEqual(raw["event_id"], "co-aspen-water-treatment-bond-100m")
            self.assertEqual(raw["first_seen_at"], "2026-11-03")
            self.assertEqual(st.open_alerts(), [])
            self.assertEqual(st.paid_eligible_items(), [])
        finally:
            st.close()

    def test_import_never_modifies_the_ledger(self):
        before = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        mtime = self.ledger.stat().st_mtime_ns
        self.assertEqual(self.cli("import", str(self.ledger)).returncode, 0)
        self.assertEqual(hashlib.sha256(self.ledger.read_bytes()).hexdigest(), before)
        self.assertEqual(self.ledger.stat().st_mtime_ns, mtime)

    def test_ledger_path_env(self):
        p = self.cli("import", LEDGER_PATH=str(self.ledger))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"new": 1', p.stdout)

    def test_missing_ledger_is_a_clear_error(self):
        p = self.cli("import")
        self.assertEqual(p.returncode, 2)
        self.assertIn("set LEDGER_PATH", p.stderr)
        p = self.cli("import", LEDGER_PATH=str(self.dir / "nope.jsonl"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("ledger not found", p.stderr)

    def test_kit_verify_reads_ledger_path(self):
        out = self.dir / "out"
        p = self.cli("verify", "--out", str(out), script="signal_feed.py", LEDGER_PATH=str(self.ledger))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("verified 0 | held 1", p.stdout)
        held = json.loads((out / "held.jsonl").read_text().splitlines()[0])
        self.assertEqual(held["hold_reason"], "source voterdecoder.org is not on the allowlist")
        p = self.cli("verify", "--out", str(out), script="signal_feed.py")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("LEDGER_PATH", p.stderr)


if __name__ == "__main__":
    unittest.main()
