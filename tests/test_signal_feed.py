"""Kit gate tests: fixtures, every classify() path, real-ledger aliases, draft safety.

Run: python -m unittest discover -s signal-feed/tests -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import signal_feed as SF  # noqa: E402

ALLOWLIST = ROOT / "config" / "allowlist.json"
SOURCES = json.loads(ALLOWLIST.read_text())["sources"]


def run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "signal_feed.py"), *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout


def jl(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def item(**kw):
    base = {"date": "2026-11-18", "place": "Austin", "state": "TX", "project": "P",
            "action": "approved", "source_url": "https://sam.gov/opp/1/view"}
    base.update(kw)
    return base


class TestClassifyPaths(unittest.TestCase):
    def check(self, it, ok, reason_part):
        got_ok, reason = SF.classify(it, SOURCES)
        self.assertEqual(got_ok, ok, reason)
        self.assertIn(reason_part, reason)

    def test_official_active(self):
        self.check(item(), True, "official: SAM.gov")

    def test_subdomain_of_official(self):
        self.check(item(source_url="https://api.sam.gov/x"), True, "official")

    def test_lookalike_domain_not_matched(self):
        self.check(item(source_url="https://notsam.gov/x"), False, "not on the allowlist")

    def test_non_official_source_type_beats_official_domain(self):
        for st in sorted(SF.NON_OFFICIAL_TYPES):
            self.check(item(source_type=st), False, "needs the official packet")

    def test_missing_each_required_field(self):
        for f in SF.REQUIRED:
            self.check(item(**{f: ""}), False, "missing " + f)

    def test_not_on_allowlist(self):
        self.check(item(source_url="https://example.com/x"), False, "not on the allowlist")

    def test_bad_link(self):
        self.check(item(source_url="http://[bad"), False, "(bad link)")

    def test_blocked_every_vendor(self):
        for url in ("https://dodge.construction.com/p", "https://constructconnect.com/p",
                    "https://app.buildingconnected.com/p", "https://planhub.com/p"):
            self.check(item(source_url=url, source_type="official"), False, "blocked")

    def test_lead_tier(self):
        self.check(item(source_url="https://myopentexas.org/x"), False, "ai_summary source; lead only")

    def test_context_tier(self):
        self.check(item(source_url="https://www.eia.gov/x"), False, "context source; lead only")

    def test_official_to_confirm(self):
        self.check(item(source_url="https://data.austintexas.gov/x"), False, "still to_confirm")


class TestAliases(unittest.TestCase):
    def test_growth_watch_fields_map(self):
        it = SF.apply_aliases({"observed_at": "2026-11-18T15:16:00-07:00", "metro_or_submarket": "Aurora",
                               "name": "CO2TX", "state": "CO", "action": "award",
                               "source_url": "https://sam.gov/opp/1"})
        self.assertEqual((it["date"], it["place"], it["project"]), ("2026-11-18", "Aurora", "CO2TX"))
        self.assertTrue(SF.classify(it, SOURCES)[0])

    def test_canonical_field_wins_over_alias(self):
        it = SF.apply_aliases({"date": "2026-11-20", "observed_at": "2026-11-18", "place": "A",
                               "city": "B", "project": "P", "name": "N"})
        self.assertEqual((it["date"], it["place"], it["project"]), ("2026-11-20", "A", "P"))

    def test_distinct_real_items_do_not_collide_as_dupes(self):
        a = SF.apply_aliases({"observed_at": "2026-11-18", "metro_or_submarket": "Aurora",
                              "name": "One", "state": "CO", "action": "approved"})
        b = dict(a, project="Two")
        self.assertNotEqual(SF.dedupe_key(a), SF.dedupe_key(b))


class TestFixtureRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, ledger, *extra):
        return run("verify", "--ledger", str(ROOT / "samples" / ledger), "--out", str(self.out), *extra)

    def test_fixtures_regional(self):
        self.assertIn("verified 1 | held 2 | dupes 1 | alerts 1", self.verify("test-fixtures.jsonl"))
        self.assertEqual([i["place"] for i in jl(self.out / "verified.jsonl")], ["TEST Denver"])
        held = {i["place"]: i["hold_reason"] for i in jl(self.out / "held.jsonl")}
        self.assertIn("ai summary", held["TEST Austin"])
        self.assertIn("blocked", held["TEST Houston"])
        self.assertIn("marked confirmed but not verified", (self.out / "alerts.txt").read_text())

    def test_fixtures_national_keeps_out_of_region(self):
        self.assertIn("verified 2", self.verify("test-fixtures.jsonl", "--national"))
        self.assertIn("TEST Phoenix", [i["place"] for i in jl(self.out / "verified.jsonl")])

    def test_sample_items_all_held_and_never_in_subscriber_copy(self):
        self.assertIn("verified 0 | held 3", self.verify("sample-items.jsonl"))
        run("draft", "--out", str(self.out), "--week-of", "2026-11-27")
        sub = (self.out / "issue-subscriber.txt").read_text()
        review = (self.out / "issue-review.txt").read_text()
        self.assertIn("Quiet week", sub)
        for place in ("Red Oak", "Rosenberg", "Aspen", "UNVERIFIED"):
            self.assertNotIn(place, sub)
            self.assertIn(place, review)

    def test_fixtures_subscriber_copy_has_only_verified(self):
        self.verify("test-fixtures.jsonl")
        run("draft", "--out", str(self.out), "--week-of", "2026-11-27")
        sub = (self.out / "issue-subscriber.txt").read_text()
        self.assertIn("TEST Denver", sub)
        for bad in ("TEST Austin", "TEST Houston", "TEST Phoenix", "planhub", "myopentexas"):
            self.assertNotIn(bad, sub)
        self.assertIn("not a bid invitation", sub)


if __name__ == "__main__":
    unittest.main()
