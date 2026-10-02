from contextlib import contextmanager
from io import BytesIO
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from openpyxl import load_workbook
from fastapi import HTTPException, UploadFile

from app import dataset_backups, store
from app.batch import create_batch_run, execute_batch
from app.engine import (
    _candidate_sources,
    _msp_item_coverage,
    _role,
    _role_signals,
    _supplier_history,
    code_similarity,
    recommend,
    search_lots,
    suggest_lot_search,
    weighted_code_similarity,
)
from app.evaluation import ranking_metrics, run_backtest
from app.exports import export_csv, export_passport_html, export_xlsx
from app.ingestion import import_csv_bytes, normalize_inn, parse_bool, parse_date
from app.main import get_dataset_backup, import_dataset, rollback_dataset
from app.registries import _lookup_rnp_one, lookup_msp, lookup_msp_by_profile
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
            + ";" * 9
            + "\n"
        ).encode()
        items = ("lot_id;product_name;okpd2_code\n10;Тест;33.12.1\n;;\n").encode()
        suppliers = (
            "lot_id;supplier_inn;supplier_kpp;is_winner\n"
            "10;6.362E+11;780601001;false\n"
            "10;7804428656;780601001;true\n"
            ";;;\n"
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
            self.assertEqual(
                result["ingestion_summary"]["files"]["notices"],
                {"rows_read": 2, "blank_rows": 1, "imported_rows": 1},
            )
            self.assertEqual(result["ingestion_summary"]["files"]["items"]["blank_rows"], 1)
            self.assertEqual(result["ingestion_summary"]["files"]["suppliers"]["blank_rows"], 1)
            with store.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM participations").fetchone()[0], 1)
                self.assertTrue(conn.execute("SELECT value FROM metadata WHERE key='data_version'").fetchone())
                self.assertTrue(
                    conn.execute("SELECT value FROM metadata WHERE key='ingestion_summary'").fetchone()
                )

    def test_import_skips_malformed_inns_and_preserves_all_lot_positions(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "01.01.2024;1;10;100;Тест;Тест;false;7804105239;780401001;АИС ГЗ\n"
        ).encode()
        items = ("lot_id;product_name;okpd2_code\n" + "".join(
            f"10;Позиция {index};33.12.1\n" for index in range(51)
        )).encode()
        suppliers = (
            "lot_id;supplier_inn;supplier_kpp;is_winner\n"
            "10;UJ65120100;780601001;false\n"
            "10;7804428656;780601001;true\n"
        ).encode()
        with temporary_database():
            result = import_csv_bytes(
                ("notices.csv", notices),
                ("items.csv", items),
                ("suppliers.csv", suppliers),
            )
            with store.connection() as conn:
                item_count = conn.execute("SELECT COUNT(*) FROM lot_items").fetchone()[0]
                participation_count = conn.execute("SELECT COUNT(*) FROM participations").fetchone()[0]
                weight_sum = conn.execute("SELECT SUM(weight) FROM lot_items WHERE lot_id='10'").fetchone()[0]
            self.assertEqual(item_count, 51)
            self.assertAlmostEqual(weight_sum, 1.0)
            self.assertEqual(participation_count, 1)
            self.assertEqual(result["counts"]["skipped_invalid_inn"], 1)
            self.assertEqual(result["ingestion_summary"]["files"]["suppliers"]["invalid_inn_rows"], 1)

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

    def test_database_does_not_create_registry_storage(self):
        with temporary_database():
            with store.connection() as conn:
                tables = {
                    row["name"]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            self.assertNotIn("msp", tables)
            self.assertNotIn("rnp", tables)

    def test_successful_dataset_replacement_can_be_rolled_back(self):
        notices_header = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
        )
        items_header = "lot_id;product_name;okpd2_code\n"
        backup_dir = Path(tempfile.mkdtemp()) / "backups"
        self.addCleanup(shutil.rmtree, backup_dir.parent, ignore_errors=True)
        manifest = backup_dir / "last-successful-replacement.json"
        with (
            temporary_database(),
            patch.object(dataset_backups, "DATASET_BACKUP_DIR", backup_dir),
            patch.object(dataset_backups, "_MANIFEST", manifest),
        ):
            import_csv_bytes(
                ("old-notices.csv", (notices_header + "01.01.2024;1;old;100;Старый лот;Старый;false;;;").encode()),
                ("old-items.csv", (items_header + "old;Старый товар;33.12.1\n").encode()),
                None,
            )
            with store.connection() as conn:
                old_version = conn.execute("SELECT value FROM metadata WHERE key='data_version'").fetchone()[0]

            snapshot = dataset_backups.create_dataset_snapshot()
            import_csv_bytes(
                ("new-notices.csv", (notices_header + "01.01.2025;2;new;200;Новый лот;Новый;false;;;").encode()),
                ("new-items.csv", (items_header + "new;Новый товар;61.10.1\n").encode()),
                None,
                replace=True,
            )
            dataset_backups.activate_dataset_snapshot(snapshot)
            self.assertTrue(dataset_backups.dataset_snapshot_status()["available"])

            restored = dataset_backups.restore_last_dataset_snapshot()

            with store.connection() as conn:
                lot_ids = [row[0] for row in conn.execute("SELECT lot_id FROM lots")]
                restored_version = conn.execute(
                    "SELECT value FROM metadata WHERE key='data_version'"
                ).fetchone()[0]
            self.assertEqual(lot_ids, ["old"])
            self.assertEqual(restored_version, old_version)
            self.assertEqual(restored["data_version"], old_version)

    def test_invalid_replacement_keeps_current_dataset(self):
        notices_header = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
        )
        items_header = "lot_id;product_name;okpd2_code\n"
        with temporary_database():
            import_csv_bytes(
                ("old-notices.csv", (notices_header + "01.01.2024;1;old;100;Старый лот;Старый;false;;;").encode()),
                ("old-items.csv", (items_header + "old;Старый товар;33.12.1\n").encode()),
                None,
            )
            with self.assertRaises(HTTPException):
                import_csv_bytes(
                    ("bad-notices.csv", b"wrong;headers\ninvalid;row\n"),
                    ("new-items.csv", (items_header + "new;Новый товар;61.10.1\n").encode()),
                    None,
                    replace=True,
                )
            with store.connection() as conn:
                lot_ids = [row[0] for row in conn.execute("SELECT lot_id FROM lots")]
            self.assertEqual(lot_ids, ["old"])

    def test_dataset_import_route_and_rollback_route(self):
        notices_header = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
        )
        items_header = "lot_id;product_name;okpd2_code\n"
        backup_dir = Path(tempfile.mkdtemp()) / "backups"
        self.addCleanup(shutil.rmtree, backup_dir.parent, ignore_errors=True)
        manifest = backup_dir / "last-successful-replacement.json"

        def upload(filename: str, payload: bytes) -> UploadFile:
            return UploadFile(file=BytesIO(payload), filename=filename, size=len(payload))

        with (
            temporary_database(),
            patch.object(dataset_backups, "DATASET_BACKUP_DIR", backup_dir),
            patch.object(dataset_backups, "_MANIFEST", manifest),
            patch.dict("os.environ", {"ADMIN_TOKEN": ""}),
        ):
            import_csv_bytes(
                ("old-notices.csv", (notices_header + "01.01.2024;1;old;100;Старый лот;Старый;false;;;").encode()),
                ("old-items.csv", (items_header + "old;Старый товар;33.12.1\n").encode()),
                None,
            )
            result = asyncio.run(
                import_dataset(
                    notices_file=upload(
                        "new-notices.csv",
                        (notices_header + "01.01.2025;2;new;200;Новый лот;Новый;false;;;").encode(),
                    ),
                    items_file=upload("new-items.csv", (items_header + "new;Новый товар;61.10.1\n").encode()),
                    suppliers_file=upload(
                        "new-suppliers.csv",
                        b"lot_id;supplier_inn;supplier_kpp;is_winner\nnew;7804428656;;true\n",
                    ),
                )
            )
            self.assertTrue(result["rollback_available"])
            self.assertTrue(get_dataset_backup()["available"])

            rollback_result = rollback_dataset(None)

            with store.connection() as conn:
                lot_ids = [row[0] for row in conn.execute("SELECT lot_id FROM lots")]
            self.assertEqual(rollback_result["status"], "rolled_back")
            self.assertEqual(lot_ids, ["old"])

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
            "01.01.2023;1;9;100;Тест;Тест;false;7804105239;780401001;АИС ГЗ\n"
            "01.01.2024;2;10;100;Тест;Тест;false;7804105239;780401001;АИС ГЗ\n"
        ).encode()
        items = "lot_id;product_name;okpd2_code\n9;Тест;33.12.1\n10;Тест;33.12.1\n".encode()
        suppliers = "lot_id;supplier_inn;supplier_kpp;is_winner\n9;7804428656;780601001;true\n".encode()
        with temporary_database():
            import_csv_bytes(
                ("notices.csv", notices),
                ("items.csv", items),
                ("suppliers.csv", suppliers),
            )
            run = create_batch_run(["10"], top_k=5, loss_weight=None)
            with (
                patch("app.engine.lookup_msp_by_profile", return_value=({}, None)),
                patch("app.engine.lookup_msp", return_value=({}, None)),
                patch(
                    "app.engine.lookup_rnp",
                    return_value={
                        "7804428656": {
                            "status": "clear",
                            "source": "ЕИС",
                            "source_url": "https://example.test/rnp",
                            "checked_at": "2026-10-01T12:00:00+00:00",
                        }
                    },
                ),
            ):
                execute_batch(run["run_id"], run["lot_ids"], top_k=5, loss_weight=None)
                single = recommend("10", top_k=5)
            with store.connection() as conn:
                saved = conn.execute(
                    "SELECT status,n_lots,completed_lots FROM runs WHERE run_id=?",
                    (run["run_id"],),
                ).fetchone()
                recommendation = conn.execute(
                    "SELECT msp_status,risk_status,enrichment_json,role_source,role_signals_json "
                    "FROM recommendations WHERE run_id=?",
                    (run["run_id"],),
                ).fetchone()
                single_recommendation = conn.execute(
                    "SELECT role_source,role_signals_json,enrichment_json FROM recommendations WHERE run_id=?",
                    (single["run_id"],),
                ).fetchone()
            self.assertEqual((saved["status"], saved["n_lots"], saved["completed_lots"]), ("done", 1, 1))
            self.assertEqual((recommendation["msp_status"], recommendation["risk_status"]), ("not_member", "clear"))
            enrichment = json.loads(recommendation["enrichment_json"])
            self.assertEqual(enrichment["rnp"]["source_url"], "https://example.test/rnp")
            self.assertEqual(enrichment["rnp"]["checked_at"], "2026-10-01T12:00:00+00:00")
            self.assertEqual(recommendation["role_source"], "история закупок")
            self.assertTrue(json.loads(recommendation["role_signals_json"]))
            self.assertEqual(single_recommendation["role_source"], "история закупок")
            self.assertTrue(json.loads(single_recommendation["role_signals_json"]))
            self.assertEqual(json.loads(single_recommendation["enrichment_json"])["rnp"]["status"], "clear")
            csv_content = export_csv(run["run_id"])
            self.assertTrue(csv_content.startswith(b"\xef\xbb\xbf"))
            csv_content = csv_content.decode("utf-8-sig")
            self.assertIn("enrichment", csv_content.splitlines()[0])
            self.assertIn("role_signals", csv_content.splitlines()[0])
            xlsx_content = export_xlsx(run["run_id"])
            self.assertTrue(xlsx_content.startswith(b"PK"))
            workbook = load_workbook(BytesIO(xlsx_content), read_only=True)
            self.assertIn("Источник классификации", [cell.value for cell in workbook["Объяснения"][1]])
            passport = export_passport_html(run["run_id"])
            self.assertIn("ПАСПОРТ ДОКАЗАТЕЛЬСТВ", passport)
            self.assertIn("Факторы и формулы", passport)
            self.assertIn("Вклад, баллы", passport)
            self.assertIn("Лот 10", passport)
            self.assertIn("2026-10-01T12:00:00+00:00", passport)

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
    def test_item_similarity_uses_normalized_position_weights(self):
        item_weights = {"33.12.1": 0.5, "61.10.11.110": 0.5}
        self.assertEqual(weighted_code_similarity(item_weights, ["33.12.1"]), 0.5)
        self.assertEqual(weighted_code_similarity(item_weights, ["33.12.18.000"]), 0.35)
        self.assertEqual(weighted_code_similarity(item_weights, ["33.12.1", "61.10.11.110"]), 1.0)
        self.assertEqual(weighted_code_similarity({"33.12.1": 0.5, "": 0.5}, ["33.12.1"]), 0.5)

    def test_new_msp_candidate_coverage_is_weighted_by_target_items(self):
        item_weights = {"33.12.1": 0.25, "61.10.11.110": 0.75}
        self.assertEqual(_msp_item_coverage(item_weights, "33.12"), 0.25)
        self.assertEqual(_msp_item_coverage(item_weights, "33.12,61.10"), 1.0)

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

    def test_roles_use_okved_and_supplier_history_signals(self):
        self.assertEqual(_role(None, "10.11", 0, 0)[0], "производитель")
        self.assertEqual(_role(None, "46.90", 0, 0)[0], "дистрибьютор")
        self.assertEqual(_role(None, "", 12, 1)[0], "дистрибьютор")
        self.assertEqual(_role(None, "", 0, 0)[0], "не определена")
        self.assertIn("промышленный ОКВЭД 10-33", _role_signals(None, "10.11", 0, 0))
        self.assertIn("торговый ОКВЭД 46", _role_signals(None, "46.90", 0, 0))
        self.assertIn("недостаточно сигналов", _role_signals(None, "", 0, 0)[0])

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
                candidates, text_weights, _, _, _ = _candidate_sources(
                    conn,
                    {"lot_id": "target", "publish_date": "2024-12-31"},
                    [],
                    "rareterm commonterm",
                    300,
                )
            self.assertGreater(text_weights["rare-match"], text_weights["common-match"])
            self.assertIn("7804428656", candidates)

    def test_exact_okpd_candidates_precede_broader_prefix_matches(self):
        with temporary_database():
            with store.connection() as conn:
                conn.execute("INSERT INTO lots(lot_id,publish_date) VALUES('target','2024-12-31')")
                history = [(f"broad-{index}", f"2024-01-{index + 1:02d}") for index in range(3)]
                history.append(("exact", "2024-02-01"))
                conn.executemany("INSERT INTO lots(lot_id,publish_date) VALUES(?,?)", history)
                conn.executemany(
                    "INSERT INTO lot_items(lot_id,pos,product_name,okpd2_code) VALUES(?,?,?,?)",
                    [(f"broad-{index}", 1, "Тест", "33.19.1") for index in range(3)]
                    + [("exact", 1, "Тест", "33.12.1")],
                )
                conn.executemany(
                    "INSERT INTO participations(lot_id,supplier_inn,is_winner) VALUES(?,?,1)",
                    [(f"broad-{index}", "7810000000") for index in range(3)]
                    + [("exact", "7804428656")],
                )
                candidates, _, _, _, _ = _candidate_sources(
                    conn,
                    {"lot_id": "target", "publish_date": "2024-12-31"},
                    ["33.12.1"],
                    "",
                    2,
                    include_msp_candidates=False,
                )
            self.assertEqual(list(candidates)[0], "7804428656")

    def test_keyword_lot_search_ranks_full_token_matches_first(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "2024-01-01;1;older;100;Обслуживание бассейнов;Работы;false;;780401001;АИС ГЗ\n"
            "2025-01-01;2;best;200;Комплексное обслуживание бассейнов;Услуги;false;;780401001;АИС ГЗ\n"
            "2025-06-01;3;partial;300;Обслуживание школы;Услуги;false;;780401001;АИС ГЗ\n"
        ).encode()
        items = (
            "lot_id;product_name;okpd2_code\n"
            "older;Обслуживание бассейна;33.12.1\n"
            "best;Ремонт оборудования бассейнов;33.12.1\n"
            "partial;Обслуживание;33.12.1\n"
        ).encode()
        with temporary_database():
            import_csv_bytes(("notices.csv", notices), ("items.csv", items), None)
            results = search_lots("обслуживание бассейнов", limit=10)
            suggestions = suggest_lot_search("обслуживание бассейнов", results)
            fuzzy_suggestions = suggest_lot_search("обслуживание бассейно", [])

        self.assertEqual([row["lot_id"] for row in results], ["best", "older", "partial"])
        self.assertEqual(results[0]["matched_terms"], 2)
        self.assertEqual(results[0]["match_ratio"], 1.0)
        self.assertEqual(results[0]["item_count"], 1)
        self.assertEqual(len(suggestions), 2)
        self.assertIn("бассейнов", suggestions[0]["phrase"].lower())
        self.assertIn("33.12.1", suggestions[0]["okpd2_codes"])
        self.assertEqual(fuzzy_suggestions[0]["phrase"], "обслуживание бассейнов")

    def test_ranking_metrics_measure_hit_recall_ndcg_and_mrr(self):
        metrics = ranking_metrics(["other", "winner-a", "winner-b"], {"winner-a", "winner-b"}, k=3)
        self.assertEqual(metrics["hit_rate"], 1.0)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 0.5)
        self.assertGreater(metrics["ndcg"], 0.0)

    def test_temporal_backtest_uses_only_pre_target_history(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "01.01.2025;1;9;100;Тест;Тест;false;;780401001;АИС ГЗ\n"
            "02.07.2025;2;10;100;Тест;Тест;false;;780401001;АИС ГЗ\n"
        ).encode()
        items = "lot_id;product_name;okpd2_code\n9;Тест;33.12.1\n10;Тест;33.12.1\n".encode()
        suppliers = (
            "lot_id;supplier_inn;supplier_kpp;is_winner\n"
            "9;7804428656;780401001;true\n"
            "10;7804428656;780401001;true\n"
        ).encode()
        with temporary_database():
            import_csv_bytes(
                ("notices.csv", notices),
                ("items.csv", items),
                ("suppliers.csv", suppliers),
            )
            result = run_backtest(
                start_date="2025-07-01",
                end_date="2025-08-01",
                max_lots=5,
                top_k=5,
            )
        self.assertEqual(result["evaluated_lots"], 1)
        self.assertEqual(result["recommendation"]["hit_rate"], 1.0)
        self.assertEqual(result["baseline"]["hit_rate"], 1.0)

    def test_text_match_recommends_supplier_when_okpd2_is_new(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "2024-01-01;1;old;100;medical software procurement;software support for clinic;false;;780401001;AIS\n"
            "2025-07-01;2;target;100;medical software procurement;software support for clinic;false;;780401001;AIS\n"
        ).encode()
        items = (
            "lot_id;product_name;okpd2_code\n"
            "old;software support and maintenance;21.20.1\n"
            "target;software support and maintenance;62.02.20.190\n"
        ).encode()
        suppliers = "lot_id;supplier_inn;supplier_kpp;is_winner\nold;7804428656;780401001;true\n".encode()
        with temporary_database():
            import_csv_bytes(
                ("notices.csv", notices),
                ("items.csv", items),
                ("suppliers.csv", suppliers),
            )
            result = recommend("target", top_k=5, persist=False, check_registries=False)
        candidate = next(item for item in result["items"] if item["inn"] == "7804428656")
        self.assertEqual(candidate["factor_scores"]["F1"], 0.0)
        self.assertGreater(candidate["factor_scores"]["F2"], 0.0)
        self.assertEqual(candidate["evidence"][0]["item_coverage"], 0.0)
        self.assertGreater(candidate["evidence"][0]["text_similarity"], 0.0)
        self.assertEqual(candidate["enrichment"]["msp"]["source_url"], "https://rmsp.nalog.ru/search.html?mode=quick&query=7804428656")
        text_detail = candidate["factor_details"]["F2"]
        self.assertEqual(text_detail["formula"], "min(1, сумма текстовых совпадений / сумма весов выборки)")
        self.assertEqual(text_detail["weight"], 0.2)
        self.assertEqual(text_detail["evidence"][0]["lot_id"], "old")
        f1_limitation = next(row for row in candidate["score_limitations"] if row["factor"] == "F1")
        expected_lost_points = round(
            100
            * candidate["factor_details"]["F1"]["weight"]
            * (1 - candidate["factor_scores"]["F1"])
            * candidate["score_modifier"]["value"],
            2,
        )
        self.assertEqual(f1_limitation["lost_points"], expected_lost_points)
        self.assertIn("истории похожих лотов", f1_limitation["reason"])

    def test_msp_only_candidate_has_factor_details_without_history(self):
        notices = (
            "publish_date;procedure_id;lot_id;start_price;procedure_name;subject;is_smp;"
            "customer_inn;customer_kpp;is_eshop_or_aisgz\n"
            "2025-07-01;1;target;100;Тестовая закупка;Тест;true;;780401001;АИС ГЗ\n"
        ).encode()
        items = "lot_id;product_name;okpd2_code\ntarget;Тест;33.12.1\n".encode()
        suppliers = "lot_id;supplier_inn;supplier_kpp;is_winner\n".encode()
        msp_record = {
            "inn": "7804428656",
            "name": "Компания из МСП",
            "okved": "33.12",
            "region": "78",
            "source": "Реестр МСП ФНС",
            "source_url": "https://rmsp.nalog.ru/search.html?mode=quick&query=7804428656",
            "checked_at": "2026-10-01T12:00:00+00:00",
        }
        with temporary_database():
            import_csv_bytes(("notices.csv", notices), ("items.csv", items), ("suppliers.csv", suppliers))
            with (
                patch("app.engine.lookup_msp_by_profile", return_value=({"7804428656": msp_record}, None)),
                patch("app.engine.lookup_msp", return_value=({}, None)),
                patch("app.engine.lookup_rnp", return_value={"7804428656": None}),
            ):
                result = recommend("target", top_k=5, persist=False)

        candidate = result["items"][0]
        self.assertEqual(candidate["msp_status"], "member")
        self.assertEqual(candidate["factor_scores"]["F1"], 0.35)
        self.assertEqual(candidate["factor_details"]["F1"]["inputs"][0]["value"], 1.0)
        self.assertEqual(candidate["factor_details"]["F5"]["inputs"][0]["value"], 0)


class RegistryLookupTests(unittest.TestCase):
    def test_fns_lookup_returns_active_registry_rows(self):
        payload = json.dumps(
            {
                "dtQueryEnd": "01.10.2026 12:00:00",
                "data": [
                    {
                        "inn": "7804428656",
                        "is_active": 1,
                        "name_ex": "Компания",
                        "category": 1,
                        "okved1": "33.12",
                        "regioncode": "78",
                    }
                ]
            }
        ).encode()
        with patch("app.registries.urlopen", return_value=BytesIO(payload)):
            records, error = lookup_msp(["7804428656"])
        self.assertIsNone(error)
        self.assertEqual(records["7804428656"]["name"], "Компания")
        self.assertEqual(records["7804428656"]["region"], "78")
        self.assertEqual(records["7804428656"]["source"], "Реестр МСП ФНС")
        self.assertEqual(records["7804428656"]["checked_at"], "01.10.2026 12:00:00")
        self.assertEqual(
            records["7804428656"]["source_url"],
            "https://rmsp.nalog.ru/search.html?mode=quick&query=7804428656",
        )

    def test_fns_lookup_chunks_large_inn_lists_to_fit_one_page(self):
        inns = [str(7800000000 + index) for index in range(101)]
        inns[-1] = "7802174011"
        requests = []

        def fake_request(payload, page_size=None):
            batch = payload["innList"].splitlines()
            requests.append((batch, page_size))
            return {
                "data": [
                    {
                        "inn": inn,
                        "is_active": 1,
                        "name_ex": f"Компания {inn}",
                    }
                    for inn in batch
                ]
            }

        with patch("app.registries._request_fns", side_effect=fake_request):
            records, error = lookup_msp(inns)

        self.assertIsNone(error)
        self.assertEqual([len(batch) for batch, _ in requests], [100, 1])
        self.assertEqual([page_size for _, page_size in requests], [100, 1])
        self.assertIn("7802174011", records)
        self.assertEqual(records["7802174011"]["name"], "Компания 7802174011")

    def test_fns_profile_lookup_uses_page_size_token(self):
        first_page = json.dumps(
            {"data": [], "pageNav": {"pageSizes": [["100", "page-token"]]}}
        ).encode()
        second_page = json.dumps(
            {
                "dtQueryEnd": "2026-10-01T12:00:00Z",
                "data": [
                    {
                        "inn": "7804428656",
                        "is_active": 1,
                        "name_ex": "Компания",
                        "category": 1,
                        "okved1": "33.12",
                        "regioncode": "78",
                    }
                ],
            }
        ).encode()
        with patch("app.registries.urlopen", side_effect=[BytesIO(first_page), BytesIO(second_page)]) as request:
            records, error = lookup_msp_by_profile(["33"], "78", 100)
        self.assertIsNone(error)
        self.assertIn("7804428656", records)
        self.assertEqual(request.call_count, 2)

    def test_rnp_public_search_reports_clear_and_unknown(self):
        no_records = b'<div class="search-results"><p class="noRecords">No records</p></div>'
        with patch("app.registries.urlopen", return_value=BytesIO(no_records)):
            self.assertEqual(_lookup_rnp_one("7804428656")["status"], "clear")
        listed = b'<div class="search-results"><div class="registry-entry">7804428656</div></div>'
        with patch("app.registries.urlopen", return_value=BytesIO(listed)):
            self.assertEqual(_lookup_rnp_one("7804428656")["status"], "listed")
        with patch("app.registries.urlopen", side_effect=URLError("TLS verification failed")):
            self.assertIsNone(_lookup_rnp_one("7804428656"))


if __name__ == "__main__":
    unittest.main()