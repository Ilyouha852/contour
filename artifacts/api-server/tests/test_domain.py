from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import store
from app.batch import create_batch_run, execute_batch
from app.engine import _candidate_sources, _supplier_history, code_similarity
from app.exports import export_csv, export_xlsx
from app.ingestion import import_csv_bytes, import_registry_csv, normalize_inn, parse_bool, parse_date
from app.text import text_similarity, tokenize


@contextmanager
def temporary_database():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(store, "DATABASE_PATH", Path(directory) / "test.sqlite3"):
            store.initialize_database()
            yield


class IngestionTests(unittest.TestCase):
    def test_scientific_notation_with_lost_inn_digits_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "научной нотации"):
            normalize_inn("6.362E+11")
        with self.assertRaisesRegex(ValueError, "научной нотации"):
            normalize_inn("7.80602E+11")

    def test_import_reports_unrecoverable_inns_without_failing_valid_rows(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "01.01.2024;1;10;100;Тест;Тест;false;7804105239;780401001;АИС ГЗ\n"
        ).encode()
        items = "lot_id;product_name;okpd2_code\n10;Тест;33.12.1\n".encode()
        suppliers = (
            "lot_id;supplier_inn;supplier_kpp;is_winner\n"
            "10;6.362E+11;780601001;false\n"
            "10;7804428656;780601001;true\n"
        ).encode()
        with temporary_database():
            result = import_csv_bytes(
                ("notices.csv", notices),
                ("items.csv", items),
                ("suppliers.csv", suppliers),
            )
            self.assertEqual(result["counts"]["participations"], 1)
            self.assertEqual(result["counts"]["skipped_unrecoverable_inn"], 1)
            self.assertEqual(result["warnings"][0]["row"], 2)
            with store.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM participations").fetchone()[0], 1)
                self.assertTrue(conn.execute("SELECT value FROM metadata WHERE key='data_version'").fetchone())

    def test_append_import_identifies_its_single_lot_even_when_history_exists(self):
        notices_header = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
        )
        items_header = "lot_id;product_name;okpd2_code\n"
        suppliers_header = "lot_id;supplier_inn;supplier_kpp;is_winner\n"
        with temporary_database():
            import_csv_bytes(
                ("notices.csv", (notices_header + "01.01.2024;1;10;100;Первый;Первый;false;;;\n").encode()),
                ("items.csv", (items_header + "10;Тест;33.12.1\n").encode()),
                ("suppliers.csv", (suppliers_header + "10;7804428656;;true\n").encode()),
            )
            result = import_csv_bytes(
                ("new-notices.csv", (notices_header + "02.01.2024;2;20;100;Второй;Второй;false;;;\n").encode()),
                ("new-items.csv", (items_header + "20;Тест;33.12.1\n").encode()),
                ("new-suppliers.csv", (suppliers_header + "20;7802023485;;true\n").encode()),
                replace=False,
            )
            self.assertEqual(result["single_lot_id"], "20")

    def test_registry_refresh_replaces_snapshot_and_changes_data_version(self):
        with temporary_database():
            first = (
                "inn;name;okved\n7804428656;Первая компания;46.1\n"
                "7802023485;Вторая компания;46.2\n"
            ).encode()
            second = "inn;name;okved\n7804428656;Обновлённая компания;46.1\n".encode()
            import_registry_csv("msp", "msp-old.csv", first)
            with store.connection() as conn:
                old_version = conn.execute(
                    "SELECT value FROM metadata WHERE key='data_version'"
                ).fetchone()["value"]
            import_registry_csv("msp", "msp-current.csv", second)
            with store.connection() as conn:
                rows = conn.execute("SELECT inn,name FROM msp").fetchall()
                new_version = conn.execute(
                    "SELECT value FROM metadata WHERE key='data_version'"
                ).fetchone()["value"]
            self.assertEqual([(row["inn"], row["name"]) for row in rows], [("7804428656", "Обновлённая компания")])
            self.assertNotEqual(old_version, new_version)

    def test_supplier_history_excludes_later_participations(self):
        with temporary_database():
            with store.connection() as conn:
                conn.executemany(
                    "INSERT INTO lots(lot_id,publish_date) VALUES(?,?)",
                    [("before", "2024-01-01"), ("target", "2024-06-01"), ("after", "2024-12-01")],
                )
                conn.executemany(
                    "INSERT INTO participations(lot_id,supplier_inn,is_winner) VALUES(?,?,?)",
                    [("before", "7804428656", 1), ("after", "7804428656", 1)],
                )
            with store.connection() as conn:
                history, total_lots = _supplier_history(
                    conn,
                    ["7804428656"],
                    {"publish_date": "2024-06-01"},
                    [],
                )
            self.assertEqual(total_lots["7804428656"], 1)
            self.assertEqual([item["lot_id"] for item in history["7804428656"]], ["before"])

    def test_batch_run_finishes_and_persists_progress(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "01.01.2024;1;10;100;Тест;Тест;false;7804105239;780401001;АИС ГЗ\n"
        ).encode()
        items = "lot_id;product_name;okpd2_code\n10;Тест;33.12.1\n".encode()
        suppliers = "lot_id;supplier_inn;supplier_kpp;is_winner\n10;7804428656;780601001;true\n".encode()
        with temporary_database():
            import_csv_bytes(
                ("notices.csv", notices),
                ("items.csv", items),
                ("suppliers.csv", suppliers),
            )
            run = create_batch_run(["10"], top_k=5, loss_weight=None)
            execute_batch(run["run_id"], run["lot_ids"], top_k=5, loss_weight=None)
            with store.connection() as conn:
                saved = conn.execute(
                    "SELECT status,n_lots,completed_lots FROM runs WHERE run_id=?",
                    (run["run_id"],),
                ).fetchone()
            self.assertEqual((saved["status"], saved["n_lots"], saved["completed_lots"]), ("done", 1, 1))
            self.assertTrue(export_csv(run["run_id"]).startswith(b"\xef\xbb\xbf"))
            self.assertTrue(export_xlsx(run["run_id"]).startswith(b"PK"))

    def test_normal_inn_is_preserved_as_text(self):
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

    def test_rare_procurement_terms_receive_more_retrieval_weight(self):
        with temporary_database():
            with store.connection() as conn:
                prior_lots = [("rare-match", "2024-01-01"), ("common-match", "2024-01-02")]
                prior_lots.extend(
                    (f"common-{index}", f"2024-02-{index + 1:02d}") for index in range(10)
                )
                conn.executemany("INSERT INTO lots(lot_id,publish_date) VALUES(?,?)", prior_lots)
                conn.executemany(
                    "INSERT INTO lot_tokens(lot_id,token) VALUES(?,?)",
                    [("rare-match", "rareterm"), ("rare-match", "commonterm")]
                    + [("common-match", "commonterm")]
                    + [(f"common-{index}", "commonterm") for index in range(10)],
                )
                conn.executemany(
                    "INSERT INTO participations(lot_id,supplier_inn,is_winner) VALUES(?,?,1)",
                    [("rare-match", "7804428656"), ("common-match", "7802023485")],
                )
                candidates, text_weights, _ = _candidate_sources(
                    conn,
                    {"lot_id": "target", "publish_date": "2024-12-31"},
                    [],
                    "rareterm commonterm",
                    300,
                )
            self.assertGreater(text_weights["rare-match"], text_weights["common-match"])
            self.assertIn("7804428656", candidates)


if __name__ == "__main__":
    unittest.main()