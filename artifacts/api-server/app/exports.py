from __future__ import annotations

import csv
import io
import json
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .store import connection


def _rows_for_run(run_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT lot_id,rank,supplier_inn,supplier_name,score,role,role_conf,status,is_msp,risk,
                      factors_json,factor_scores_json,evidence_json,explanation
            FROM recommendations WHERE run_id=? ORDER BY lot_id,rank""",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def export_csv(run_id: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "lot_id",
            "rank",
            "supplier_inn",
            "name",
            "score",
            "role",
            "role_conf",
            "status",
            "msp",
            "risk",
            "factors",
            "factor_scores",
            "evidence",
            "explanation",
        ]
    )
    for row in _rows_for_run(run_id):
        writer.writerow(
            [
                row["lot_id"],
                row["rank"],
                row["supplier_inn"],
                row["supplier_name"] or "",
                row["score"],
                row["role"],
                row["role_conf"],
                row["status"],
                "true" if row["is_msp"] else "false",
                "true" if row["risk"] else "false",
                row["factors_json"],
                row["factor_scores_json"],
                row["evidence_json"],
                row["explanation"],
            ]
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def export_xlsx(run_id: str) -> bytes:
    rows = _rows_for_run(run_id)
    workbook = Workbook()
    recommendations = workbook.active
    recommendations.title = "Рекомендации"
    recommendations.append(
        ["Лот", "Ранг", "ИНН", "Наименование", "Оценка", "Роль", "Уверенность", "Статус", "МСП", "Риск"]
    )
    explanations = workbook.create_sheet("Объяснения")
    explanations.append(["Лот", "Ранг", "ИНН", "Факторы (вклад)", "Оценки факторов", "Лоты-доказательства", "Объяснение"])

    for row in rows:
        recommendations.append(
            [
                row["lot_id"],
                row["rank"],
                row["supplier_inn"],
                row["supplier_name"],
                row["score"],
                row["role"],
                row["role_conf"],
                row["status"],
                bool(row["is_msp"]),
                bool(row["risk"]),
            ]
        )
        explanations.append(
            [
                row["lot_id"],
                row["rank"],
                row["supplier_inn"],
                row["factors_json"],
                row["factor_scores_json"],
                row["evidence_json"],
                row["explanation"],
            ]
        )

    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="243B53")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for column_cells in sheet.columns:
            letter = get_column_letter(column_cells[0].column)
            sample = [str(cell.value or "") for cell in list(column_cells)[:100]]
            width = min(60, max(12, max((len(value) for value in sample), default=12) + 2))
            sheet.column_dimensions[letter].width = width
        sheet.row_dimensions[1].height = 30

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def parse_run_row(row) -> dict[str, Any]:
    result = dict(row)
    result["weights"] = json.loads(result.pop("weights_json"))
    total = result["n_lots"]
    result["progress_percent"] = round(100 * result["completed_lots"] / total, 1) if total else 100.0
    return result