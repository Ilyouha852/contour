import unittest

from app.engine import code_similarity
from app.ingestion import normalize_inn, parse_bool, parse_date
from app.text import text_similarity, tokenize


class IngestionTests(unittest.TestCase):
    def test_scientific_notation_inn_stays_a_text_identifier(self):
        self.assertEqual(normalize_inn("6.362E+11"), "636200000000")
        self.assertEqual(normalize_inn("7804428656"), "7804428656")

    def test_invalid_inn_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_inn("12345")

    def test_csv_boolean_and_date_formats(self):
        self.assertEqual(parse_bool("true", filename="x.csv", column="is_winner", row_number=2), 1)
        self.assertEqual(parse_bool("false", filename="x.csv", column="is_winner", row_number=2), 0)
        self.assertEqual(parse_date("11.07.2024"), "2024-07-11")
        self.assertIsNone(parse_date(""))


class MatchingTests(unittest.TestCase):
    def test_okpd_hierarchy(self):
        self.assertEqual(code_similarity("33.12.1", "33.12.1"), 1.0)
        self.assertEqual(code_similarity("33.12.1", "33.12.18.000"), 0.7)
        self.assertEqual(code_similarity("33.12.1", "33.19.4"), 0.5)
        self.assertEqual(code_similarity("33.12.1", "33.29.4"), 0.25)
        self.assertEqual(code_similarity("33.12.1", "61.10.11.110"), 0.0)

    def test_text_similarity_ignores_common_procurement_words(self):
        self.assertEqual(tokenize("Поставка медицинского оборудования"), ["медицинского", "оборудования"])
        self.assertEqual(
            text_similarity("Поставка медицинского оборудования", "Медицинского оборудования"),
            1.0,
        )


if __name__ == "__main__":
    unittest.main()