from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from .batch import MAX_BATCH_LOTS, create_batch_run, execute_batch
from .config import SOURCE_DIR
from .engine import get_lot, recommend, supplier_profile
from .exports import export_csv, export_xlsx, parse_run_row
from .ingestion import import_csv_bytes, read_upload
from .registries import search_msp
from .schemas import BatchRunRequest, RecommendRequest
from .store import connection, initialize_database

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("counterparty-api")


def seed_dataset_if_empty() -> None:
    with connection() as conn:
        n_lots = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    if n_lots:
        return
    notice_path = next(SOURCE_DIR.glob("*Извещения*.csv"), None) if SOURCE_DIR.exists() else None
    item_path = next(SOURCE_DIR.glob("*ТРУ*.csv"), None) if SOURCE_DIR.exists() else None
    supplier_path = next(SOURCE_DIR.glob("*Поставщики*.csv"), None) if SOURCE_DIR.exists() else None
    if not notice_path or not item_path:
        logger.warning("No bundled CSV data found; start with an empty database and use /api/v1/datasets/import")
        return
    try:
        result = import_csv_bytes(
            (notice_path.name, notice_path.read_bytes()),
            (item_path.name, item_path.read_bytes()),
            (supplier_path.name, supplier_path.read_bytes()) if supplier_path else None,
        )
        logger.info("Seed dataset imported from attached CSV files")
        if result.get("warnings"):
            logger.warning(
                "Seed import skipped %s participation rows because their INNs could not be recovered: %s",
                result["counts"].get("skipped_unrecoverable_inn", 0),
                result["warnings"],
            )
    except Exception:
        logger.exception("Could not import the bundled dataset")
        raise


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    seed_dataset_if_empty()
    yield


app = FastAPI(
    title="Сервис подбора релевантных контрагентов",
    description=(
        "FastAPI backend для ранжирования поставщиков закупки по победам в похожих лотах, "
        "ОКПД2, тексту, цене, заказчику и региону. Внешние API нейросетей не используются."
    ),
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/api", include_in_schema=False)
def api_root() -> dict[str, str]:
    return {
        "service": "counterparty-recommender",
        "documentation": "/api/docs",
        "openapi": "/api/openapi.json",
        "health": "/api/healthz",
        "api": "/api/v1",
    }


@app.get("/api/healthz", tags=["health"])
def health() -> dict[str, Any]:
    with connection() as conn:
        counts = {
            "lots": conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0],
            "suppliers": conn.execute("SELECT COUNT(DISTINCT supplier_inn) FROM participations").fetchone()[0],
        }
    return {"status": "ok", "database": "ready", "counts": counts}


@app.get("/api/v1/stats", tags=["data"])
def dataset_stats() -> dict[str, Any]:
    with connection() as conn:
        counts = {
            "lots": conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0],
            "lots_with_items": conn.execute("SELECT COUNT(DISTINCT lot_id) FROM lot_items").fetchone()[0],
            "lots_with_participations": conn.execute(
                "SELECT COUNT(DISTINCT lot_id) FROM participations"
            ).fetchone()[0],
            "items": conn.execute("SELECT COUNT(*) FROM lot_items").fetchone()[0],
            "participations": conn.execute("SELECT COUNT(*) FROM participations").fetchone()[0],
            "suppliers": conn.execute("SELECT COUNT(DISTINCT supplier_inn) FROM participations").fetchone()[0],
        }
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM metadata")}
        last_date = conn.execute("SELECT MAX(publish_date) FROM lots").fetchone()[0]
    return {
        "counts": counts,
        "registry_sources": {"msp": "configured", "rnp": "configured"},
        "coverage": {
            "lots_with_items_percent": round(100 * counts["lots_with_items"] / counts["lots"], 2)
            if counts["lots"]
            else 0.0,
            "lots_with_participations_percent": round(
                100 * counts["lots_with_participations"] / counts["lots"], 2
            )
            if counts["lots"]
            else 0.0,
        },
        "data_version": metadata.get("data_version"),
        "last_publish_date": last_date,
        "source_hashes": json.loads(metadata["source_hashes"]) if metadata.get("source_hashes") else {},
        "ingestion_warnings": json.loads(metadata["ingestion_warnings"])
        if metadata.get("ingestion_warnings")
        else {"warnings": [], "omitted_warning_count": 0},
        "last_ingestion_summary": json.loads(metadata["ingestion_summary"])
        if metadata.get("ingestion_summary")
        else None,
    }


@app.post("/api/v1/datasets/import", tags=["data"])
async def import_dataset(
    notices_file: UploadFile = File(..., description="CSV извещений, разделитель ;"),
    items_file: UploadFile = File(..., description="CSV товаров, работ и услуг"),
    suppliers_file: UploadFile | None = File(default=None, description="CSV участников, необязательный"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    _check_admin(x_admin_token)
    notices = await read_upload(notices_file)
    items = await read_upload(items_file)
    suppliers = await read_upload(suppliers_file, required=False)
    assert notices is not None and items is not None
    return import_csv_bytes(notices, items, suppliers)


@app.post("/api/v1/recommendations", tags=["recommendations"])
async def get_recommendations(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    loss_weight: float | None = None
    top_k = 20

    if "application/json" in content_type:
        try:
            body = RecommendRequest.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        lot_id, top_k, loss_weight = body.lot_id, body.top_k, body.loss_weight
    elif "multipart/form-data" in content_type:
        _check_admin(request.headers.get("X-Admin-Token"))
        form = await request.form()
        notices_file = form.get("notices_file") or form.get("notice_file")
        items_file = form.get("items_file") or form.get("tru_file")
        suppliers_file = form.get("suppliers_file") or form.get("supplier_file")
        if not hasattr(notices_file, "read") or not hasattr(items_file, "read"):
            raise HTTPException(
                status_code=400,
                detail="Для multipart загрузите notices_file и items_file; suppliers_file можно не передавать.",
            )
        notices = await read_upload(notices_file)
        items = await read_upload(items_file)
        suppliers = await read_upload(suppliers_file, required=False) if hasattr(suppliers_file, "read") else None
        assert notices is not None and items is not None
        import_result = import_csv_bytes(notices, items, suppliers, replace=False)
        requested_lot_id = str(form.get("lot_id") or "").strip()
        if requested_lot_id:
            lot_id = requested_lot_id
        else:
            lot_id = import_result.get("single_lot_id")
            if not lot_id:
                raise HTTPException(
                    status_code=422,
                    detail="Передайте lot_id в форме: загруженный набор содержит несколько лотов.",
                )
        try:
            top_k = min(100, max(1, int(form.get("top_k", 20))))
            if form.get("loss_weight") not in (None, ""):
                loss_weight = float(str(form["loss_weight"]))
                if not 0 <= loss_weight <= 1:
                    raise ValueError()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="top_k: 1–100; loss_weight: 0–1") from exc
    else:
        raise HTTPException(status_code=415, detail="Ожидается application/json или multipart/form-data")

    result = await asyncio.to_thread(recommend, lot_id, top_k, loss_weight)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Лот {lot_id} не найден")
    return result


@app.get("/api/v1/lots/{lot_id}", tags=["lots"])
def lot_detail(lot_id: str) -> dict[str, Any]:
    lot = get_lot(lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail=f"Лот {lot_id} не найден")
    return lot


@app.get("/api/v1/suppliers", tags=["suppliers"])
def search_suppliers(q: str = Query(min_length=1), limit: int = Query(default=20, ge=1, le=100)) -> dict:
    needle = f"%{q.strip()}%"
    with connection() as conn:
        historical = conn.execute(
            """SELECT p.supplier_inn AS inn,NULL AS name,NULL AS category,substr(MAX(p.supplier_kpp),1,2) AS region,
                COUNT(DISTINCT p.lot_id) AS lots,SUM(p.is_winner) AS wins
            FROM participations p
            WHERE p.supplier_inn LIKE ?
            GROUP BY p.supplier_inn ORDER BY wins DESC,lots DESC LIMIT ?""",
            (needle, limit),
        ).fetchall()
    msp_rows, msp_error = search_msp(q, limit)
    historical_by_inn = {row["inn"]: dict(row) for row in historical}
    for record in msp_rows:
        existing = historical_by_inn.get(record["inn"])
        if existing:
            existing.update(record)
        else:
            historical_by_inn[record["inn"]] = record | {"lots": 0, "wins": 0}
    results = sorted(
        historical_by_inn.values(),
        key=lambda row: (-int(row.get("wins") or 0), -int(row.get("lots") or 0), row["inn"]),
    )[:limit]
    return {
        "items": results,
        "registry_check": "unavailable" if msp_error else "available",
        "registry_error": msp_error,
    }


@app.get("/api/v1/suppliers/{inn}", tags=["suppliers"])
def supplier_detail(inn: str) -> dict[str, Any]:
    profile = supplier_profile(inn)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Контрагент {inn} не найден")
    return profile


@app.post("/api/v1/batch/runs", status_code=202, tags=["batch"])
def start_batch(body: BatchRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        created = create_batch_run(body.lot_ids, body.top_k, body.loss_weight)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background_tasks.add_task(
        execute_batch,
        created["run_id"],
        created.pop("lot_ids"),
        body.top_k,
        body.loss_weight,
    )
    return {key: value for key, value in created.items() if key != "loss_weight"}


@app.get("/api/v1/batch/runs/{run_id}", tags=["batch"])
def get_batch_status(run_id: str) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Пакетный запуск не найден")
    return parse_run_row(row)


@app.get("/api/v1/batch/runs/{run_id}/export", tags=["batch"])
def download_batch_export(run_id: str, format: str = Query(default="xlsx", pattern="^(xlsx|csv)$")) -> Response:
    with connection() as conn:
        run = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if run["status"] not in {"done", "failed"}:
        raise HTTPException(status_code=409, detail="Выгрузка появится после завершения запуска")
    if format == "csv":
        content = export_csv(run_id)
        media_type = "text/csv; charset=utf-8"
        filename = f"recommendations_{run_id}.csv"
    else:
        content = export_xlsx(run_id)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"recommendations_{run_id}.xlsx"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _check_admin(token: str | None) -> None:
    expected = os.getenv("ADMIN_TOKEN")
    if expected and (token is None or not hmac.compare_digest(token, expected)):
        raise HTTPException(status_code=401, detail="Требуется действительный X-Admin-Token")
