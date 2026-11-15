"""The craft vocabulary (conta/kinds.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "conta"))
import kinds  # noqa: E402


class Kinds(unittest.TestCase):
    def test_italian_and_english_words(self):
        self.assertEqual(kinds.kinds_of("Catalogo prodotti"), ["catalogue"])
        self.assertEqual(kinds.kinds_of("Product catalog"), ["catalogue"])

    def test_more_than_one_craft(self):
        self.assertEqual(kinds.kinds_of("Listino e catalogo"), ["catalogue", "price-list"])

    def test_accents_case_and_separators(self):
        self.assertEqual(kinds.kinds_of("PUBBLICITÀ_stampa"), ["advertising"])
        self.assertEqual(kinds.kinds_of("stand-fiera"), ["event"])

    def test_whole_words_only(self):
        self.assertEqual(kinds.kinds_of("standard"), [])        # contains "stand"
        self.assertEqual(kinds.kinds_of("catalogazione"), [])   # contains "catalog"
        self.assertEqual(kinds.kinds_of("postale"), [])         # contains "post"
        self.assertEqual(kinds.kinds_of(""), [])

    def test_nineteen_crafts(self):   # the published vocabulary: adding a craft is a deliberate change
        self.assertEqual(len(kinds.KINDS), 19)

    def test_years_of(self):
        self.assertEqual(kinds.years_of("Agenda 2020 e 2024, ordine 120245"), [2020, 2024])   # not inside a longer number
