from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from typing import Any

from .config import load_scoring_config
from .engine import recommend
from .store import connection

MAX_CANDIDATES = 300


def ranking_metrics(ranked_inns: list[str], winners: set[str], k: int = 10) -> dict[str, float]:
    if not winners or k <= 0:
        return {"hit_rate": 0.0, "recall": 0.0, "ndcg": 0.0, "mrr": 0.0}
    top = ranked_inns[:k]
    relevant_ranks = [rank for rank, inn in enumerate(top, start=1) if inn in winners]
    dcg = sum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(k, len(winners)) + 1))
    return {
        "hit_rate": float(bool(relevant_ranks)),
        "recall": len(relevant_ranks) / len(winners),
        "ndcg": dcg / ideal_dcg if ideal_dcg else 0.0,
        "mrr": 1.0 / relevant_ranks[0] if relevant_ranks else 0.0,
    }


def _even_sample(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    if len(rows) <= limit:
        return rows
    if limit <= 1:
        return [rows[0]]
    indices = [round(index * (len(rows) - 1) / (limit - 1)) for index in range(limit)]
    return [rows[index] for index in indices]


def _baseline_ranking(lot_id: str, publish_date: str, code_weights: dict[str, float], limit: int) -> list[str]:
    target_codes = [code for code, _ in sorted(code_weights.items(), key=lambda item: (-item[1], item[0]))[:200]]
    if not target_codes:
        return []

    supplier_lots: dict[str, dict[str, int]] = defaultdict(dict)
    with connection() as conn:
        for start in range(0, len(target_codes), 200):
            chunk = target_codes[start : start + 200]
            conditions = []
            parameters: list[Any] = [lot_id, publish_date]
            for code in chunk:
                conditions.append(
                    "(i.okpd2_code=? OR substr(i.okpd2_code,1,5)=? OR substr(i.okpd2_code,1,2)=?)"
                )
                parameters.extend((code, code[:5], code[:2]))
            rows = conn.execute(
                f"""SELECT DISTINCT p.supplier_inn,p.lot_id,p.is_winner
                FROM participations p
                JOIN lots l ON l.lot_id=p.lot_id
                JOIN lot_items i ON i.lot_id=p.lot_id
                WHERE p.lot_id<>? AND l.publish_date IS NOT NULL AND l.publish_date<?
                  AND ({' OR '.join(conditions)})""",
                parameters,
            )
            for row in rows:
                supplier_lots[row["supplier_inn"]][row["lot_id"]] = int(row["is_winner"])

    counts = [
        (inn, sum(lot_wins.values()), len(lot_wins))
        for inn, lot_wins in supplier_lots.items()
    ]
    counts.sort(key=lambda row: (-row[1], -row[2], row[0]))
    return [inn for inn, _, _ in counts[:limit]]


def run_backtest(
    *,
    start_date: str,
    end_date: str,
    max_lots: int = 50,
    top_k: int = 10,
) -> dict[str, Any]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT l.lot_id,l.publish_date
            FROM lots l
            WHERE l.publish_date>=? AND l.publish_date<?
              AND EXISTS (SELECT 1 FROM participations p WHERE p.lot_id=l.lot_id AND p.is_winner=1)
              AND EXISTS (SELECT 1 FROM lot_items i WHERE i.lot_id=l.lot_id AND i.okpd2_code<>'')
            ORDER BY l.publish_date,l.lot_id""",
            (start_date, end_date),
        ).fetchall()
    targets = _even_sample([dict(row) for row in rows], max_lots)
    sample_hash = hashlib.sha256(
        "\n".join(target["lot_id"] for target in targets).encode("utf-8")
    ).hexdigest()
    scoring_weights, scoring_parameters = load_scoring_config()
    with connection() as conn:
        version_row = conn.execute("SELECT value FROM metadata WHERE key='data_version'").fetchone()
    data_version = version_row["value"] if version_row else "unversioned"

    recommendation_totals: Counter[str] = Counter()
    baseline_totals: Counter[str] = Counter()
    candidate_recall_total = 0.0
    historical_winner_coverage_total = 0.0
    examples = []
    evaluated = 0
    for target in targets:
        lot_id = target["lot_id"]
        with connection() as conn:
            winners = {
                row["supplier_inn"]
                for row in conn.execute(
                    "SELECT supplier_inn FROM participations WHERE lot_id=? AND is_winner=1",
                    (lot_id,),
                )
            }
            historical_winners = {
                row["supplier_inn"]
                for row in conn.execute(
                    """SELECT DISTINCT current.supplier_inn
                    FROM participations current
                    JOIN participations prior ON prior.supplier_inn=current.supplier_inn
                    JOIN lots prior_lot ON prior_lot.lot_id=prior.lot_id
                    WHERE current.lot_id=? AND current.is_winner=1
                      AND prior_lot.publish_date IS NOT NULL AND prior_lot.publish_date<?""",
                    (lot_id, target["publish_date"]),
                )
            }
            rows = conn.execute(
                "SELECT okpd2_code,weight FROM lot_items WHERE lot_id=? AND okpd2_code<>''",
                (lot_id,),
            ).fetchall()
        if not winners:
            continue
        code_weights: dict[str, float] = defaultdict(float)
        for row in rows:
            code_weights[row["okpd2_code"]] += float(row["weight"] or 0.0)

        result = recommend(
            lot_id,
            top_k=MAX_CANDIDATES,
            persist=False,
            check_registries=False,
        )
        all_predictions = [item["inn"] for item in (result or {}).get("items", [])]
        predicted = all_predictions[:top_k]
        baseline_pool = _baseline_ranking(lot_id, target["publish_date"], code_weights, MAX_CANDIDATES)
        baseline = baseline_pool[:top_k]
        candidate_recall_total += ranking_metrics(all_predictions, winners, MAX_CANDIDATES)["recall"]
        historical_winner_coverage_total += len(historical_winners) / len(winners)
        for name, metrics in (
            ("recommendation", ranking_metrics(predicted, winners, top_k)),
            ("baseline", ranking_metrics(baseline, winners, top_k)),
        ):
            for metric, value in metrics.items():
                (recommendation_totals if name == "recommendation" else baseline_totals)[metric] += value
        evaluated += 1
        if len(examples) < 5:
            examples.append(
                {
                    "lot_id": lot_id,
                    "publish_date": target["publish_date"],
                    "winner_count": len(winners),
                    "historical_winner_coverage": len(historical_winners) / len(winners),
                    "candidate_recall_at_300": ranking_metrics(
                        all_predictions, winners, MAX_CANDIDATES
                    )["recall"],
                    "recommendation_top": predicted[:top_k],
                    "baseline_top": baseline[:top_k],
                }
            )

    if not evaluated:
        return {
            "period": {"start": start_date, "end_exclusive": end_date},
            "data_version": data_version,
            "sample_hash": sample_hash,
            "sampled_lots": len(targets),
            "evaluated_lots": 0,
            "top_k": top_k,
            "weights": scoring_weights,
            "parameters": scoring_parameters,
            "recommendation": ranking_metrics([], set(), top_k),
            "baseline": ranking_metrics([], set(), top_k),
            "examples": [],
            "message": "В выбранном периоде нет лотов с победителями и кодами ОКПД2.",
        }

    recommendation_metrics = {key: value / evaluated for key, value in recommendation_totals.items()}
    baseline_metrics = {key: value / evaluated for key, value in baseline_totals.items()}
    return {
        "period": {"start": start_date, "end_exclusive": end_date},
        "data_version": data_version,
        "sample_hash": sample_hash,
        "sampled_lots": len(targets),
        "evaluated_lots": evaluated,
        "top_k": top_k,
        "weights": scoring_weights,
        "parameters": scoring_parameters,
        "recommendation": recommendation_metrics,
        "baseline": baseline_metrics,
        "candidate_recall_at_300": candidate_recall_total / evaluated,
        "historical_winner_coverage": historical_winner_coverage_total / evaluated,
        "delta_ndcg": recommendation_metrics["ndcg"] - baseline_metrics["ndcg"],
        "examples": examples,
        "registry_checks": "disabled for deterministic historical evaluation",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Temporal backtest against actual procurement winners")
    parser.add_argument("--start", default="2025-07-01", help="Inclusive YYYY-MM-DD")
    parser.add_argument("--end", default="2026-01-01", help="Exclusive YYYY-MM-DD")
    parser.add_argument("--max-lots", type=int, default=50)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    report = run_backtest(
        start_date=args.start,
        end_date=args.end,
        max_lots=max(1, args.max_lots),
        top_k=max(1, args.top_k),
    )
    if args.summary_only:
        report.pop("examples", None)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
