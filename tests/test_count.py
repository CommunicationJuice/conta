"""The counting rules (conta/count.py), one by one, on hand-made register rows."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "conta"))
import count  # noqa: E402

META = {"as_of": "2026-06-30", "generated": "2026-06-30T08:00:00+02:00", "provisional": False, "note": "test"}


def row(started, kind=("brochure",), conf="medium", first=None, final=None, edit=None, project="P"):
    return {"project": project, "kind": list(kind), "started": started, "first_delivery": first,
            "final_delivery": final, "last_design_edit": edit or started,
            "dates": {"started": {"source": "file_mtime", "confidence": conf}}}


def docs(rows, as_of="2026-06-30"):
    t = count.Tally()
    for r in rows:
        t.add(r)
    return t.documents(as_of, dict(META, as_of=as_of), active_clients=3, counts_include={"visible", "pending"})


class Dates(unittest.TestCase):
    def test_pre_2000_dates_are_artefacts(self):
        self.assertIsNone(count.real_date("1999-12-31"))
        self.assertIsNone(count.real_date("1904-01-01"))
        self.assertEqual(count.real_date("2000-01-01"), "2000-01-01")
        self.assertIsNone(count.real_date(None))

    def test_year(self):
        self.assertEqual(count.year("2019-03-04"), 2019)
        self.assertIsNone(count.year(""))
        self.assertIsNone(count.year("n/a"))


class NamedYearFloor(unittest.TestCase):
    def test_reused_asset_start_moves_to_first_delivery(self):
        p = count.named_year_floor(row("2008-02-01", conf="medium", first="2014-03-10", project="Proposte 2014"))
        self.assertEqual(p["started"], "2014-03-10")
        self.assertEqual(p["dates"]["started"]["source"], "named_year_floor")
        self.assertEqual(p["dates"]["started"]["raw"], "2008-02-01")

    def test_a_moved_start_is_low_confidence(self):
        p = count.named_year_floor(row("2008-02-01", conf="medium", first="2014-03-10", project="Proposte 2014"))
        self.assertEqual(p["dates"]["started"]["confidence"], "low")   # so it stays out of the months table

    def test_falls_back_to_last_design_edit(self):
        p = count.named_year_floor(row("2008-02-01", edit="2013-11-20", project="Proposte 2014"))
        self.assertEqual(p["started"], "2013-11-20")

    def test_certain_start_is_kept(self):
        p = count.named_year_floor(row("2008-02-01", conf="high", first="2014-03-10", project="Proposte 2014"))
        self.assertEqual(p["started"], "2008-02-01")

    def test_start_within_a_year_of_the_name_is_kept(self):
        p = count.named_year_floor(row("2013-06-01", first="2014-03-10", project="Proposte 2014"))
        self.assertEqual(p["started"], "2013-06-01")

    def test_anchor_too_early_keeps_the_start(self):
        p = count.named_year_floor(row("2008-02-01", first="2010-01-01", edit="2010-01-01", project="Proposte 2014"))
        self.assertEqual(p["started"], "2008-02-01")

    def test_no_year_in_the_name_keeps_the_start(self):
        p = count.named_year_floor(row("2008-02-01", first="2014-03-10", project="Proposte"))
        self.assertEqual(p["started"], "2008-02-01")


class Summary(unittest.TestCase):
    def test_one_project_once_in_its_start_year(self):
        s, _, _ = docs([row("2019-03-04"), row("2019-11-01"), row("2021-05-10")])
        self.assertEqual(s["projects_per_year"], {2019: 2, 2021: 1})
        self.assertEqual(s["calendar_years_worked"], 2)

    def test_years_before_since_year_are_not_counted_but_crafts_are(self):
        s, _, _ = docs([row("2005-01-01", kind=("flyer",)), row("2019-03-04")])
        self.assertEqual(s["projects_per_year"], {2019: 1})
        self.assertEqual(s["disciplines"]["flyer"], 1)

    def test_events_are_projects_too(self):
        s, _, _ = docs([row("2023-09-01", kind=("event",)), row("2023-10-01")])
        self.assertEqual(s["projects_per_year"], {2023: 2})
        self.assertEqual(s["events_per_year"], {2023: 1})

    def test_a_project_counts_in_every_craft_it_carries(self):
        s, _, _ = docs([row("2019-03-04", kind=("catalogue", "price-list")), row("2020-01-01", kind=())])
        self.assertEqual(s["disciplines"], {"catalogue": 1, "price-list": 1})   # no kind: "other", never listed
        self.assertEqual(sum(s["projects_per_year"].values()), 2)

    def test_crafts_are_listed_largest_first(self):
        s, _, _ = docs([row("2019-01-01", kind=("web",)), row("2019-02-01", kind=("identity",)),
                        row("2019-03-01", kind=("identity",))])
        self.assertEqual(list(s["disciplines"]), ["identity", "web"])

    def test_header_fields(self):
        s, _, _ = docs([row("2019-03-04")])
        self.assertEqual(s["schema"], "bi-public/v1")
        self.assertEqual((s["current_year"], s["current_year_complete"]), (2026, False))
        self.assertEqual(s["years_active"], 18)   # completed years since 2008
        self.assertEqual(s["active_clients"], 3)
        self.assertEqual(s["counts_include"], ["pending", "visible"])

    def test_the_year_is_complete_on_31_december(self):
        s, _, _ = docs([row("2019-03-04")], as_of="2026-12-31")
        self.assertTrue(s["current_year_complete"])


class LastThirtyDays(unittest.TestCase):
    def test_a_delivery_counts_on_its_day(self):
        _, n, _ = docs([row("2026-06-01", first="2026-06-25")])
        self.assertEqual(n["window_days"], 30)
        self.assertEqual(len(n["delivered_by_day"]), 30)
        self.assertEqual(n["delivered_by_day"][24], 1)     # 2026-06-01 is day 0, 2026-06-30 day 29
        self.assertEqual((n["delivered"], n["on_table"]), (1, 0))

    def test_the_final_delivery_beats_the_first(self):
        _, n, _ = docs([row("2026-05-01", first="2026-05-20", final="2026-06-03")])
        self.assertEqual(n["delivered_by_day"][2], 1)

    def test_the_final_delivery_beats_the_first_inside_the_window(self):
        _, n, _ = docs([row("2026-05-01", first="2026-06-05", final="2026-06-20")])
        self.assertEqual((n["delivered_by_day"][4], n["delivered_by_day"][19]), (0, 1))

    def test_work_without_a_delivery_in_the_window_is_open(self):
        _, n, _ = docs([row("2026-06-20", edit="2026-06-28"), row("2026-01-01", first="2026-01-10", edit="2026-06-15")])
        self.assertEqual((n["delivered"], n["on_table"]), (0, 2))

    def test_one_project_one_bucket(self):
        _, n, _ = docs([row("2026-06-01", first="2026-06-10", edit="2026-06-29")])
        self.assertEqual((n["delivered"], n["on_table"]), (1, 0))

    def test_outside_the_window_counts_nowhere(self):
        _, n, _ = docs([row("2026-04-01", first="2026-05-31", edit="2026-05-31")])
        self.assertEqual((n["delivered"], n["on_table"]), (0, 0))

    def test_window_edges(self):
        _, n, _ = docs([row("2026-05-01", first="2026-06-01"), row("2026-05-01", first="2026-06-30")])
        self.assertEqual((n["delivered_by_day"][0], n["delivered_by_day"][29]), (1, 1))


class StartsByMonth(unittest.TestCase):
    def test_only_trusted_starts_are_placed(self):
        _, _, m = docs([row("2019-03-04", conf="high"), row("2019-03-20", conf="medium"), row("2019-07-01", conf="low")])
        self.assertEqual(m["years"]["2019"], [0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual((m["dated_projects"], m["total_projects"]), (2, 3))
        self.assertEqual(m["confidence_floor"], "medium")

    def test_same_years_as_the_summary(self):
        s, _, m = docs([row("2005-03-04"), row("2019-03-04"), row("2021-05-10")])
        self.assertEqual(sorted(m["years"]), [str(y) for y in s["projects_per_year"]])


if __name__ == "__main__":
    unittest.main()
