from __future__ import annotations

import json
from datetime import datetime, timezone

from .engine import recommend
from .store import connection
from .config import load_scoring_config

MAX_BATCH_LOTS = 40_000


def create_batch_run(lot_ids: list[str] | None, top_k: int, loss_weight: float | None) -> dict:
    run_id = __import__("uuid").uuid4().hex
    with connection() as conn:
        if lot_ids:
            lot_ids = list(dict.fromkeys(lot_ids))
            if len(lot_ids) > MAX_BATCH_LOTS:
                raise ValueError(f"За один пакет можно отправить не более {MAX_BATCH_LOTS} лотов")
            n_lots = 0
            for start in range(0, len(lot_ids), 900):
                chunk = lot_ids[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                n_lots += conn.execute(
                    f"SELECT COUNT(*) FROM lots WHERE lot_id IN ({placeholders})", chunk
                ).fetchone()[0]
            if n_lots != len(lot_ids):
                raise ValueError("В списке есть lot_id, которых нет в базе")
        else:
            n_lots = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
            if n_lots > MAX_BATCH_LOTS:
                raise ValueError(
                    f"В базе {n_lots} лотов; ограничьте пакет списком не более {MAX_BATCH_LOTS} lot_id"
                )
            lot_ids = [row["lot_id"] for row in conn.execute("SELECT lot_id FROM lots ORDER BY lot_id")]
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM metadata")}
        weights, parameters = load_scoring_config()
        if loss_weight is not None:
            parameters["loss_weight"] = loss_weight
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO runs(run_id,kind,status,created_at,updated_at,weights_json,data_version,n_lots,completed_lots)
            VALUES(?,?,?,?,?,?,?,?,0)""",
            (
                run_id,
                "batch",
                "queued",
                now,
                now,
                json.dumps({**weights, **parameters}, ensure_ascii=False),
                metadata.get("data_version", "unversioned"),
                n_lots,
            ),
        )
    return {
        "run_id": run_id,
        "status": "queued",
        "n_lots": n_lots,
        "top_k": top_k,
        "lot_ids": lot_ids,
        "loss_weight": loss_weight,
    }


def execute_batch(run_id: str, lot_ids: list[str], top_k: int, loss_weight: float | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection() as conn:
        conn.execute("UPDATE runs SET status='running',updated_at=? WHERE run_id=?", (now, run_id))
    try:
        for completed, lot_id in enumerate(lot_ids, start=1):
            result = recommend(lot_id, top_k=top_k, loss_weight=loss_weight, persist=False, run_id=run_id)
            if result is not None:
                with connection() as conn:
                    conn.executemany(
                        """INSERT OR REPLACE INTO recommendations
                        (run_id,lot_id,rank,supplier_inn,supplier_name,score,role,role_conf,status,is_msp,risk,
                         factors_json,factor_scores_json,evidence_json,explanation)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            (
                                run_id,
                                lot_id,
                                item["rank"],
                                item["inn"],
                                item["name"],
                                item["score"],
                                item["role"],
                                item["role_conf"],
                                item["status"],
                                int(item["msp"]),
                                int(item["risk"]),
                                json.dumps(item["factors"], ensure_ascii=False),
                                json.dumps(item["factor_scores"], ensure_ascii=False),
                                json.dumps(item["evidence"], ensure_ascii=False),
                                item["explain"],
                            )
                            for item in result["items"]
                        ),
                    )
            now = datetime.now(timezone.utc).isoformat()
            with connection() as conn:
                conn.execute(
                    "UPDATE runs SET completed_lots=?,updated_at=? WHERE run_id=?",
                    (completed, now, run_id),
                )
        with connection() as conn:
            conn.execute(
                "UPDATE runs SET status='done',updated_at=? WHERE run_id=?",
                (datetime.now(timezone.utc).isoformat(), run_id),
            )
    except Exception as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?",
                (str(exc)[:2000], datetime.now(timezone.utc).isoformat(), run_id),
            )