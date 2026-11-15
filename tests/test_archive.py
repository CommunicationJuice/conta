"""End to end on the made-up archive (tests/fixture.py): the scanner as a command, then the count."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "conta"))
sys.path.insert(0, str(HERE))
import count  # noqa: E402
import fixture  # noqa: E402


class Archive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        fixture.build(base / "archive")
        r = subprocess.run([sys.executable, "-I", str(ROOT / "conta" / "nas_scan.py"), "--root", str(base / "archive"),
                            "--out", str(base / "scan")], capture_output=True, text=True)
        if r.returncode:
            raise AssertionError(f"scanner failed:\n{r.stderr}")
        cls.out = base / "scan"
        cls.projects = {}
        for f in sorted((cls.out / "clients").glob("*/*.nas-scan.json")):
            scan = json.loads(f.read_text())
            for p in scan.get("projects") or []:
                cls.projects[(scan["folder"], p["project"])] = p
        t = count.Tally()
        for p in cls.projects.values():
            if not p["received"]:
                t.add(count.named_year_floor(p))
        meta = {"as_of": fixture.AS_OF, "generated": "test", "provisional": False, "note": "test"}
        cls.summary, cls.now, cls.starts = t.documents(fixture.AS_OF, meta, 0, {"visible"})

    def p(self, client, project):
        return self.projects[(client, project)]

    def test_one_project_per_folder(self):
        self.assertEqual(sorted(self.projects), [
            ("Client A", "Catalogo 2019"), ("Client A", "Materiale cliente"), ("Client A", "Stand Sicam 2023"),
            ("Client B", "Brochure"), ("Client B", "Listino 2026"), ("Client B", "Logo"),
            ("Client B", "Newsletter giugno"), ("Client C", "Proposte 2014")])

    def test_register_dates_and_confidence(self):
        cat = self.p("Client A", "Catalogo 2019")
        self.assertEqual((cat["started"], cat["dates"]["started"]["confidence"]), ("2019-03-04", "high"))
        self.assertEqual((cat["last_design_edit"], cat["first_delivery"]), ("2019-03-20", "2019-04-02"))
        for name in (("Client A", "Stand Sicam 2023"), ("Client B", "Logo"), ("Client B", "Newsletter giugno")):
            self.assertEqual(self.p(*name)["dates"]["started"]["confidence"], "high", name)   # versions of one piece
        lst = self.p("Client B", "Listino 2026")
        self.assertEqual((lst["started"], lst["dates"]["started"]["confidence"], lst["first_delivery"]),
                         ("2026-06-28", "medium", None))

    def test_register_kinds(self):
        self.assertEqual(self.p("Client A", "Catalogo 2019")["kind"], ["advertising", "catalogue"])
        self.assertEqual(self.p("Client A", "Stand Sicam 2023")["kind"], ["event"])
        self.assertEqual(self.p("Client B", "Logo")["kind"], ["identity"])

    def test_contracts_are_never_read(self):
        self.assertFalse(any("Contratti" in project for _, project in self.projects))

    def test_received_material_is_flagged(self):
        self.assertTrue(self.p("Client A", "Materiale cliente")["received"])   # by name
        self.assertTrue(self.p("Client B", "Brochure")["received"])            # a lone file long before CJ's work
        self.assertFalse(self.p("Client C", "Proposte 2014")["received"])
        self.assertFalse(self.p("Client B", "Logo")["received"])

    def test_totals(self):
        s = self.summary
        self.assertEqual(s["projects_per_year"], {2014: 1, 2019: 1, 2021: 1, 2023: 1, 2026: 2})   # 2008 moved to 2014
        self.assertEqual(s["events_per_year"], {2023: 1})
        self.assertEqual(s["disciplines"], {"advertising": 1, "catalogue": 1, "event": 1, "identity": 1,
                                            "banner-digital": 1, "price-list": 1})
        self.assertEqual((s["current_year"], s["calendar_years_worked"]), (2026, 5))

    def test_last_thirty_days(self):
        self.assertEqual(self.now["delivered_by_day"][24], 1)    # the newsletter PDF, 2026-06-25
        self.assertEqual((self.now["delivered"], self.now["on_table"]), (1, 1))   # and the price list in progress

    def test_starts_by_month(self):
        y = self.starts["years"]
        self.assertEqual((y["2019"][2], y["2021"][4], y["2023"][8], y["2026"][5]), (1, 1, 1, 2))
        self.assertEqual(y["2014"], [0] * 12)    # the moved start is low confidence
        self.assertEqual((self.starts["dated_projects"], self.starts["total_projects"]), (5, 6))

    def test_example_script_runs(self):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "examples" / "count_archive.py"), str(self.out)],
                           check=True, capture_output=True, text=True)
        got = json.loads(r.stdout)["summary"]["projects_per_year"]   # the real caller: received left out, floor applied
        self.assertEqual(got, {"2014": 1, "2019": 1, "2021": 1, "2023": 1, "2026": 2})


if __name__ == "__main__":
    unittest.main()
