"""Delivery dates in the project register (brand_meta._deliveries): which PDF is the delivery."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "conta"))
import brand_meta  # noqa: E402


def pdf(name, day, **meta):
    t = datetime.strptime(day + " 12:00", "%Y-%m-%d %H:%M").timestamp()
    return (f"Client/Job/{name}.pdf", t, {"meta": meta}, ".pdf")


START = datetime(2024, 3, 1)


class Deliveries(unittest.TestCase):
    def test_pdfx_with_a_final_name_is_a_high_confidence_final(self):
        first, final, conf = brand_meta._deliveries([pdf("job_bozza", "2024-03-05"), pdf("job_stampa", "2024-03-12", pdfx="PDF/X-4")], START)
        self.assertEqual((first.date().isoformat(), final.date().isoformat(), conf), ("2024-03-05", "2024-03-12", "high"))

    def test_pdfx_alone_is_medium(self):
        _, final, conf = brand_meta._deliveries([pdf("job", "2024-03-12", pdfx="PDF/X-1a")], START)
        self.assertEqual((final.date().isoformat(), conf), ("2024-03-12", "medium"))

    def test_a_final_name_alone_is_not_enough(self):
        first, final, conf = brand_meta._deliveries([pdf("job_stampa", "2024-03-12")], START)
        self.assertEqual((first.date().isoformat(), final, conf), ("2024-03-12", None, None))

    def test_proof_names_are_never_final(self):
        _, final, _ = brand_meta._deliveries([pdf("job_bozza_stampa", "2024-03-12", pdfx="PDF/X-4")], START)
        self.assertIsNone(final)

    def test_the_earliest_final_wins_and_the_best_score_that_day_sets_the_confidence(self):
        _, final, conf = brand_meta._deliveries([pdf("job_x", "2024-03-15", pdfx="PDF/X-4"),
                                                  pdf("job_a", "2024-03-12", pdfx="PDF/X-4"),
                                                  pdf("job_def", "2024-03-12", pdfx="PDF/X-4")], START)
        self.assertEqual((final.date().isoformat(), conf), ("2024-03-12", "high"))

    def test_pdfs_before_the_start_are_not_deliveries(self):
        first, final, _ = brand_meta._deliveries([pdf("old_stampa", "2023-01-10", pdfx="PDF/X-4"), pdf("job", "2024-03-20")], START)
        self.assertEqual((first.date().isoformat(), final), ("2024-03-20", None))
