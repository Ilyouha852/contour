from __future__ import annotations

import json
import math
import re
import statistics
from difflib import get_close_matches
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any

from .config import load_scoring_config
from .registries import RNP_SEARCH_URL, lookup_msp, lookup_msp_by_profile, lookup_rnp, msp_search_url
from .store import connection
from .text import text_similarity, tokenize

MAX_CANDIDATES = 300
MAX_TEXT_LOTS = 100
HISTORY_PER_SUPPLIER = 100
MANUFACTURER_WORDS = ("завод", "фабрик", "комбинат", "производств")
DISTRIBUTOR_WORDS = ("торг", "снаб", "опт", "дистриб")


def code_similarity(left: str, right: str) -> float:
    a, b = (left or "").strip().rstrip("."), (right or "").strip().rstrip(".")
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_parts, b_parts = a.split("."), b.split(".")
    if len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[:2] == b_parts[:2]:
        return 0.7
    if a_parts[0][:2] == b_parts[0][:2]:
        first_fraction_a = a_parts[1][:1] if len(a_parts) > 1 else ""
        first_fraction_b = b_parts[1][:1] if len(b_parts) > 1 else ""
        if first_fraction_a and first_fraction_a == first_fraction_b:
            return 0.5
        return 0.25
    return 0.0


def weighted_code_similarity(item_weights: dict[str, float], history_codes: list[str]) -> float:
    total_weight = sum(max(0.0, weight) for weight in item_weights.values())
    if total_weight <= 0 or not history_codes:
        return 0.0
    covered_weight = sum(
        max(0.0, weight)
        * max((code_similarity(target, code) for code in history_codes), default=0.0)
        for target, weight in item_weights.items()
    )
    return min(1.0, covered_weight / total_weight)


def _msp_item_coverage(item_weights: dict[str, float], okved: str) -> float:
    total_weight = sum(max(0.0, weight) for weight in item_weights.values())
    if total_weight <= 0:
        return 0.0
    okved_codes = [part.strip() for part in (okved or "").replace("|", ",").split(",")]
    matched_weight = sum(
        max(0.0, weight)
        for target, weight in item_weights.items()
        if target
        and any(code.startswith(target[:2]) for code in okved_codes if code)
    )
    return min(1.0, matched_weight / total_weight)


def _effective_date(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return date.today()


def _years_since(published: str | None, target: date) -> float:
    if not published:
        return 8.0
    try:
        age = (target - date.fromisoformat(published[:10])).days
        return max(0.0, age / 365.25)
    except ValueError:
        return 8.0


def _candidate_sources(
    conn,
    lot: dict[str, Any],
    item_codes: list[str],
    title: str,
    limit: int,
    *,
    include_msp_candidates: bool = True,
):
    target_date = _effective_date(lot.get("publish_date"))
    where_parts: list[str] = []
    params: list[Any] = [lot["lot_id"], target_date.isoformat()]
    for code in item_codes:
        if not code:
            continue
        where_parts.append("(i.okpd2_code=? OR substr(i.okpd2_code,1,5)=? OR substr(i.okpd2_code,1,2)=?)")
        params.extend((code, code[:5], code[:2]))
    exact_rows = []
    exact_codes = item_codes[:500]
    if exact_codes:
        marks = ",".join("?" for _ in exact_codes)
        exact_rows = conn.execute(
            f"""SELECT matching.supplier_inn,COUNT(*) AS category_lots,
                SUM(matching.is_winner) AS category_wins
            FROM (
                SELECT DISTINCT p.supplier_inn,p.lot_id,p.is_winner
                FROM participations p
                JOIN lots l ON l.lot_id=p.lot_id
                JOIN lot_items i ON i.lot_id=p.lot_id
                WHERE p.lot_id<>? AND l.publish_date IS NOT NULL AND l.publish_date<?
                  AND i.okpd2_code IN ({marks})
            ) AS matching
            GROUP BY matching.supplier_inn
            ORDER BY category_wins DESC,category_lots DESC,matching.supplier_inn
            LIMIT ?""",
            (lot["lot_id"], target_date.isoformat(), *exact_codes, limit),
        ).fetchall()
    category_rows = []
    if where_parts:
        category_rows = conn.execute(
            f"""SELECT matching.supplier_inn, COUNT(*) AS category_lots,
                SUM(matching.is_winner) AS category_wins
            FROM (
                SELECT DISTINCT p.supplier_inn,p.lot_id,p.is_winner
                FROM participations p
                JOIN lots l ON l.lot_id=p.lot_id
                JOIN lot_items i ON i.lot_id=p.lot_id
                WHERE p.lot_id<>? AND l.publish_date IS NOT NULL AND l.publish_date<?
                  AND ({' OR '.join(where_parts)})
            ) AS matching
            GROUP BY matching.supplier_inn
            ORDER BY category_wins DESC, category_lots DESC
            LIMIT ?""",
            (*params, limit),
        ).fetchall()

    target_tokens = sorted(set(tokenize(title, 12)))
    text_weights: dict[str, float] = {}
    if target_tokens:
        marks = ",".join("?" for _ in target_tokens)
        before_date = target_date.isoformat()
        document_count = conn.execute(
            """SELECT COUNT(DISTINCT t.lot_id)
            FROM lot_tokens t JOIN lots l ON l.lot_id=t.lot_id
            WHERE l.publish_date IS NOT NULL AND l.publish_date<?""",
            (before_date,),
        ).fetchone()[0]
        if document_count:
            document_frequencies = {
                row["token"]: int(row["document_frequency"])
                for row in conn.execute(
                    f"""SELECT t.token,COUNT(DISTINCT t.lot_id) AS document_frequency
                    FROM lot_tokens t JOIN lots l ON l.lot_id=t.lot_id
                    WHERE t.token IN ({marks}) AND l.publish_date IS NOT NULL AND l.publish_date<?
                    GROUP BY t.token""",
                    (*target_tokens, before_date),
                )
            }
            token_weights = {
                token: math.log1p(
                    (document_count - document_frequencies.get(token, 0) + 0.5)
                    / (document_frequencies.get(token, 0) + 0.5)
                )
                for token in target_tokens
            }
            total_token_weight = sum(token_weights.values())
            weighted_terms = " ".join("WHEN ? THEN ?" for _ in target_tokens)
            case_parameters = [
                value for token in target_tokens for value in (token, token_weights[token])
            ]
            matched = conn.execute(
                f"""SELECT t.lot_id,
                    SUM(CASE t.token {weighted_terms} ELSE 0 END) AS overlap_weight,
                    l.publish_date
                FROM lot_tokens t JOIN lots l ON l.lot_id=t.lot_id
                WHERE t.token IN ({marks}) AND t.lot_id<>?
                  AND l.publish_date IS NOT NULL AND l.publish_date<?
                GROUP BY t.lot_id,l.publish_date
                ORDER BY overlap_weight DESC,l.publish_date DESC,t.lot_id DESC
                LIMIT ?""",
                (*case_parameters, *target_tokens, lot["lot_id"], before_date, MAX_TEXT_LOTS),
            ).fetchall()
            for row in matched:
                text_weights[row["lot_id"]] = min(
                    1.0, float(row["overlap_weight"]) / max(1e-9, total_token_weight)
                )
    text_supplier_inns: set[str] = set()
    text_lot_ids = list(text_weights)
    for start in range(0, len(text_lot_ids), 800):
        chunk = text_lot_ids[start : start + 800]
        marks = ",".join("?" for _ in chunk)
        text_supplier_inns.update(
            row["supplier_inn"]
            for row in conn.execute(
                f"SELECT DISTINCT supplier_inn FROM participations WHERE lot_id IN ({marks})", chunk
            )
        )

    candidates: dict[str, dict[str, Any]] = {}
    for row in exact_rows:
        candidates[row["supplier_inn"]] = {
            "category_lots": int(row["category_lots"] or 0),
            "category_wins": int(row["category_wins"] or 0),
            "source_msp": False,
        }
    for row in category_rows:
        if row["supplier_inn"] not in candidates and len(candidates) < limit:
            candidates[row["supplier_inn"]] = {
                "category_lots": int(row["category_lots"] or 0),
                "category_wins": int(row["category_wins"] or 0),
                "source_msp": False,
            }
    for inn in sorted(text_supplier_inns):
        if inn not in candidates and len(candidates) < limit:
            candidates[inn] = {"category_lots": 0, "category_wins": 0, "source_msp": False}

    profile_msp: dict[str, dict] = {}
    registry_error = None
    win_total = sum(int(row["category_wins"] or 0) for row in category_rows)
    leading_wins = int(category_rows[0]["category_wins"] or 0) if category_rows else 0
    weak_category = len(category_rows) <= 5 or (win_total > 0 and leading_wins / win_total > 0.6)
    if include_msp_candidates and weak_category and item_codes:
        prefixes = sorted({code[:2] for code in item_codes if len(code) >= 2})
        region = (lot.get("customer_kpp") or "")[:2]
        profile_msp, registry_error = lookup_msp_by_profile(prefixes, region, min(100, limit))
        for inn in profile_msp:
            if inn not in candidates:
                candidates[inn] = {
                    "category_lots": 0,
                    "category_wins": 0,
                    "source_msp": True,
                }
                if len(candidates) >= limit:
                    break

    return candidates, text_weights, text_supplier_inns, profile_msp, registry_error


def _supplier_history(conn, inns: list[str], lot: dict[str, Any], text_lot_ids: list[str]):
    if not inns:
        return {}, {}
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total_lots: dict[str, int] = {}
    target_date = _effective_date(lot.get("publish_date"))

    for start in range(0, len(inns), 300):
        chunk = inns[start : start + 300]
        marks = ",".join("?" for _ in chunk)
        counts = conn.execute(
            f"""SELECT p.supplier_inn,COUNT(DISTINCT p.lot_id) AS n_lots
            FROM participations p JOIN lots l ON l.lot_id=p.lot_id
            WHERE p.supplier_inn IN ({marks}) AND l.publish_date IS NOT NULL AND l.publish_date<?
            GROUP BY p.supplier_inn""",
            (*chunk, target_date.isoformat()),
        )
        total_lots.update({row["supplier_inn"]: int(row["n_lots"]) for row in counts})
        rows = conn.execute(
            f"""WITH ranked AS (
                SELECT p.supplier_inn,p.lot_id,p.supplier_kpp,p.is_winner,
                    l.publish_date,l.start_price,l.customer_inn,l.procedure_name,l.subject,
                    ROW_NUMBER() OVER (PARTITION BY p.supplier_inn ORDER BY l.publish_date DESC, p.lot_id DESC) AS position
                FROM participations p JOIN lots l ON l.lot_id=p.lot_id
                WHERE p.supplier_inn IN ({marks}) AND l.publish_date IS NOT NULL
                  AND l.publish_date<?
            )
            SELECT ranked.*, GROUP_CONCAT(DISTINCT i.okpd2_code) AS codes,
                   GROUP_CONCAT(DISTINCT i.product_name) AS products
            FROM ranked LEFT JOIN lot_items i ON i.lot_id=ranked.lot_id
            WHERE ranked.position<=? OR ranked.lot_id IN ({",".join("?" for _ in text_lot_ids) or "NULL"})
            GROUP BY ranked.supplier_inn,ranked.lot_id
            ORDER BY ranked.supplier_inn,ranked.publish_date DESC""",
            (*chunk, target_date.isoformat(), HISTORY_PER_SUPPLIER, *text_lot_ids),
        ).fetchall()
        for row in rows:
            event = dict(row)
            event["codes_list"] = [code for code in (row["codes"] or "").split(",") if code]
            histories[row["supplier_inn"]].append(event)
    return histories, total_lots


def _role(name: str | None, okved: str, class_count: int, wins: int) -> tuple[str, float]:
    normalized_name = (name or "").lower()
    codes = [part.strip().split(".")[0] for part in (okved or "").replace("|", ",").split(",") if part.strip()]
    industrial = any(code.isdigit() and 10 <= int(code) <= 33 for code in codes)
    trade = any(code.startswith("46") for code in codes)
    if any(word in normalized_name for word in MANUFACTURER_WORDS) or industrial:
        return "производитель", 0.82 if industrial else 0.78
    if any(word in normalized_name for word in DISTRIBUTOR_WORDS) or trade:
        return "дистрибьютор", 0.80 if trade else 0.76
    if class_count >= 12:
        return "дистрибьютор", min(0.74, 0.55 + class_count / 100)
    if 1 <= class_count <= 4 and wins >= 3:
        return "производитель", 0.63
    if name or class_count:
        return "поставщик", 0.58
    return "не определена", 0.35


def _role_signals(name: str | None, okved: str, class_count: int, wins: int) -> list[str]:
    normalized_name = (name or "").lower()
    okved_codes = [
        part.strip().split(".")[0]
        for part in (okved or "").replace("|", ",").split(",")
        if part.strip()
    ]
    signals = []
    if any(code.isdigit() and 10 <= int(code) <= 33 for code in okved_codes):
        signals.append("промышленный ОКВЭД 10-33")
    if any(code.startswith("46") for code in okved_codes):
        signals.append("торговый ОКВЭД 46")
    if any(word in normalized_name for word in MANUFACTURER_WORDS):
        signals.append("производственный маркер в названии")
    if any(word in normalized_name for word in DISTRIBUTOR_WORDS):
        signals.append("торговый маркер в названии")
    if class_count >= 12:
        signals.append(f"широкий профиль: {class_count} классов ОКПД2")
    elif 1 <= class_count <= 4 and wins >= 3:
        signals.append(f"узкий профиль: {class_count} классов, {wins} побед")
    return signals or ["недостаточно сигналов для уверенной классификации"]


def _score_supplier(
    inn: str,
    info: dict[str, Any],
    history: list[dict[str, Any]],
    total_lots: int,
    item_codes: list[str],
    item_weights: dict[str, float],
    title: str,
    lot: dict[str, Any],
    text_weights: dict[str, float],
    category_total_wins: int,
    is_risk: bool | None,
    msp_status: str,
    msp: dict[str, Any] | None,
    weights: dict[str, float],
    parameters: dict[str, float],
) -> dict[str, Any]:
    target_date = _effective_date(lot.get("publish_date"))
    loss_weight = parameters["loss_weight"]
    gamma = parameters["gamma"]
    experience = 0.0
    similar_wins = 0
    similar_events = 0
    customer_wins = 0
    price_samples: list[float] = []
    price_sample_evidence: list[dict[str, Any]] = []
    most_recent_similar_win: str | None = None
    most_recent_similar_win_lot_id: str | None = None
    unique_classes: set[str] = set()
    evidence: list[tuple[float, dict[str, Any]]] = []
    experience_evidence: list[dict[str, Any]] = []
    text_evidence: list[dict[str, Any]] = []
    customer_evidence: list[dict[str, Any]] = []
    text_numerator = 0.0
    text_denominator = max(1e-9, sum(text_weights.values()))
    own_lots = {event["lot_id"] for event in history}
    best_item_coverage = 0.0
    target_region = (lot.get("customer_kpp") or "")[:2]
    supplier_region = ""
    median_price: float | None = None
    days_old: int | None = None

    if info.get("source_msp") and not history:
        okved = (msp or {}).get("okved", "")
        best_item_coverage = _msp_item_coverage(item_weights, okved)
        raw_scores = {
            "F1": 0.35 * best_item_coverage,
            "F2": 0.0,
            "F3": 0.0,
            "F4": 0.0,
            "F5": 0.0,
            "F6": 0.0,
            "F7": 1.0 if (msp or {}).get("region", "")[:2] in {"78", "47"} else 0.5,
            "F8": 1.0 if lot.get("is_smp") == 1 else 0.5,
        }
    else:
        for event in history:
            codes = event["codes_list"]
            similarity = weighted_code_similarity(item_weights, codes)
            best_item_coverage = max(best_item_coverage, similarity)
            published = event.get("publish_date")
            age_years = _years_since(published, target_date)
            participation_weight = 1.0 if event["is_winner"] else loss_weight
            recency_weight = gamma**age_years
            experience_contribution = participation_weight * similarity * recency_weight
            experience += experience_contribution
            if similarity > 0:
                similar_events += 1
                experience_evidence.append(
                    {
                        "lot_id": event["lot_id"],
                        "publish_date": published,
                        "is_winner": bool(event["is_winner"]),
                        "item_coverage": round(similarity, 4),
                        "recency_weight": round(recency_weight, 4),
                        "contribution": round(experience_contribution, 4),
                    }
                )
                if event["is_winner"]:
                    similar_wins += 1
                    if event.get("start_price") and event["start_price"] > 0:
                        price_samples.append(float(event["start_price"]))
                        price_sample_evidence.append(
                            {
                                "lot_id": event["lot_id"],
                                "publish_date": published,
                                "price": float(event["start_price"]),
                            }
                        )
                    if event.get("customer_inn") and event["customer_inn"] == lot.get("customer_inn"):
                        customer_wins += 1
                        customer_evidence.append(
                            {
                                "lot_id": event["lot_id"],
                                "publish_date": published,
                                "title": event.get("procedure_name") or event.get("subject") or "",
                            }
                        )
                    if published and (most_recent_similar_win is None or published > most_recent_similar_win):
                        most_recent_similar_win = published
                        most_recent_similar_win_lot_id = event["lot_id"]
            if event["is_winner"]:
                product_text = event.get("products") or ""
                event_title = " ".join([event.get("procedure_name") or "", event.get("subject") or "", product_text])
                text_score = text_similarity(title, event_title)
                if event["lot_id"] in text_weights:
                    text_contribution = text_weights[event["lot_id"]] * text_score
                    text_numerator += text_contribution
                    if text_contribution > 0:
                        text_evidence.append(
                            {
                                "lot_id": event["lot_id"],
                                "publish_date": published,
                                "title": event.get("procedure_name") or event.get("subject") or "",
                                "text_similarity": round(text_score, 4),
                                "retrieval_weight": round(text_weights[event["lot_id"]], 4),
                                "contribution": round(text_contribution, 4),
                            }
                        )
                if similarity > 0 or (event["lot_id"] in text_weights and text_score >= 0.25):
                    evidence_score = similarity * 0.65 + text_score * 0.35
                    evidence.append(
                        (
                            evidence_score,
                            {
                                "lot_id": event["lot_id"],
                                "publish_date": event["publish_date"],
                                "title": event.get("procedure_name") or event.get("subject") or "",
                                "okpd2": codes[:5],
                                "item_coverage": round(similarity, 4),
                                "text_similarity": round(text_score, 4),
                                "customer_inn": event.get("customer_inn"),
                                "is_winner": True,
                                "price": event.get("start_price"),
                            },
                        )
                    )
            for code in codes:
                if code:
                    parts = code.split(".")
                    unique_classes.add(".".join(parts[:2]) if len(parts) > 1 else parts[0])

        raw_scores = {
            "F1": 1.0 - math.exp(-experience / max(0.1, parameters["tau"])),
            "F2": min(1.0, text_numerator / text_denominator),
            "F3": (similar_wins + parameters["alpha"] * parameters["prior_win_rate"])
            / (similar_events + parameters["alpha"]),
            "F4": 1.0 - math.exp(-customer_wins / 2.0),
            "F5": 0.0,
            "F6": 0.0,
            "F7": 0.3,
            "F8": 1.0,
        }
        median_price = statistics.median(price_samples) if price_samples else None
        if median_price is not None and lot.get("start_price") and lot["start_price"] > 0:
            raw_scores["F5"] = math.exp(
                -abs(math.log(float(lot["start_price"])) - math.log(median_price))
                / max(0.05, parameters["price_sigma"])
            )
        if most_recent_similar_win:
            days_old = max(0, (target_date - date.fromisoformat(most_recent_similar_win[:10])).days)
            raw_scores["F6"] = math.exp(-days_old / 730.0)

        candidate_kpp = next((event.get("supplier_kpp") for event in history if event.get("supplier_kpp")), "")
        target_region = (lot.get("customer_kpp") or "")[:2]
        supplier_region = (candidate_kpp or "")[:2]
        if supplier_region in {"78", "47"}:
            raw_scores["F7"] = 1.0 if target_region in {"78", "47"} else 0.5
        if lot.get("is_smp") == 1:
            raw_scores["F8"] = {"member": 1.0, "not_member": 0.0, "unknown": 0.5}[msp_status]
        else:
            raw_scores["F8"] = 1.0

    factors = {key: round(weights[key] * max(0.0, min(1.0, value)), 4) for key, value in raw_scores.items()}
    multiplier = 0.0 if is_risk is True else min(1.0, 0.6 + 0.1 * total_lots)
    if info.get("source_msp") and not history:
        multiplier = min(multiplier, 0.8)
    score = round(100.0 * sum(factors.values()) * multiplier, 2)
    role_name = (msp or {}).get("name")
    role_okved = (msp or {}).get("okved", "")
    role, role_conf = _role(role_name, role_okved, len(unique_classes), similar_wins)
    role_signals = _role_signals(role_name, role_okved, len(unique_classes), similar_wins)

    if is_risk is True or multiplier < 0.5:
        status = "Риск"
    elif similar_wins >= 3 and most_recent_similar_win:
        recent_days = (target_date - date.fromisoformat(most_recent_similar_win[:10])).days
        status = "Проверенный" if recent_days <= 365 else "Участник"
    elif total_lots > 0:
        status = "Участник"
    elif msp_status == "member":
        status = "Новый (реестр МСП)"
    else:
        status = "Участник"

    evidence_out = [
        item
        for _, item in sorted(evidence, key=lambda value: value[0], reverse=True)[:3]
    ]
    recent_win_evidence = (
        [
            {
                "lot_id": most_recent_similar_win_lot_id,
                "publish_date": most_recent_similar_win,
                "days_old": days_old,
            }
        ]
        if most_recent_similar_win
        else []
    )
    if info.get("source_msp") and not history:
        factor_formulas = {
            "F1": "0.35 × покрытие ОКПД2 из ОКВЭД",
            "F2": "Нет текстовой истории",
            "F3": "Нет истории побед",
            "F4": "Нет побед у этого заказчика",
            "F5": "Нет исторических цен",
            "F6": "Нет даты похожей победы",
            "F7": "Оценка региона по записи МСП",
            "F8": (
                "Участник реестра = 100%"
                if lot.get("is_smp") == 1
                else "50% для кандидата, найденного через профиль МСП, вне закупки МСП"
            ),
        }
        factor_inputs = {
            "F1": [
                {"label": "Покрытие позиций по ОКПД2 из ОКВЭД", "value": round(best_item_coverage, 4)},
                {"label": "Коэффициент для нового поставщика", "value": 0.35},
            ],
            "F2": [{"label": "Текстовая история", "value": "нет"}],
            "F3": [
                {"label": "Победы в похожих лотах", "value": 0},
                {"label": "Похожие участия", "value": 0},
            ],
            "F4": [{"label": "Победы у заказчика", "value": 0}],
            "F5": [{"label": "Исторические цены", "value": 0}],
            "F6": [{"label": "Похожие победы", "value": "нет"}],
            "F7": [
                {"label": "Регион поставщика", "value": (msp or {}).get("region") or "не указан"},
                {"label": "Регион заказчика", "value": target_region or "не указан"},
            ],
            "F8": [
                {"label": "Статус МСП", "value": msp_status},
                {"label": "Закупка для МСП", "value": lot.get("is_smp") == 1},
            ],
        }
        factor_evidence: dict[str, list[dict[str, Any]]] = {}
    else:
        factor_formulas = {
            "F1": "1 − exp(−взвешенный опыт / τ)",
            "F2": "min(1, сумма текстовых совпадений / сумма весов выборки)",
            "F3": "(похожие победы + α × базовая доля побед) / (похожие участия + α)",
            "F4": "1 − exp(−победы у заказчика / 2)",
            "F5": "exp(−|ln(НМЦК лота) − ln(медиана цен побед)| / σ цены)",
            "F6": "exp(−дней с последней похожей победы / 730)",
            "F7": "Оценка совпадения регионов по КПП поставщика и заказчика",
            "F8": (
                "Для закупки МСП: участник реестра = 100%, не найден = 0%, неизвестно = 50%"
                if lot.get("is_smp") == 1
                else "100%, так как целевой лот не ограничен для МСП"
            ),
        }
        factor_inputs = {
            "F1": [
                {"label": "Взвешенный опыт с учётом давности", "value": round(experience, 4)},
                {"label": "Похожие участия", "value": similar_events},
                {"label": "τ", "value": parameters["tau"]},
                {"label": "Затухание давности γ", "value": gamma},
                {"label": "Вес проигранного участия", "value": loss_weight},
            ],
            "F2": [
                {"label": "Сумма текстовых совпадений", "value": round(text_numerator, 4)},
                {"label": "Сумма весов выборки", "value": round(text_denominator, 4)},
            ],
            "F3": [
                {"label": "Победы в похожих лотах", "value": similar_wins},
                {"label": "Похожие участия", "value": similar_events},
                {"label": "α сглаживания", "value": parameters["alpha"]},
                {"label": "Базовая доля побед", "value": parameters["prior_win_rate"]},
            ],
            "F4": [{"label": "Победы у этого заказчика", "value": customer_wins}],
            "F5": [
                {"label": "НМЦК целевого лота", "value": lot.get("start_price")},
                {"label": "Медиана цен похожих побед", "value": median_price},
                {"label": "Число исторических цен", "value": len(price_samples)},
                {"label": "σ цены", "value": parameters["price_sigma"]},
            ],
            "F6": [
                {"label": "Дата последней похожей победы", "value": most_recent_similar_win or "нет"},
                {"label": "Дней с победы", "value": days_old if days_old is not None else "—"},
            ],
            "F7": [
                {"label": "Регион поставщика по КПП", "value": supplier_region or "не указан"},
                {"label": "Регион заказчика по КПП", "value": target_region or "не указан"},
            ],
            "F8": [
                {"label": "Статус МСП", "value": msp_status},
                {"label": "Закупка для МСП", "value": lot.get("is_smp") == 1},
            ],
        }
        factor_evidence = {
            "F1": sorted(experience_evidence, key=lambda row: row["contribution"], reverse=True)[:5],
            "F2": sorted(text_evidence, key=lambda row: row["contribution"], reverse=True)[:5],
            "F3": evidence_out,
            "F4": customer_evidence[:5],
            "F5": price_sample_evidence[:5],
            "F6": recent_win_evidence,
        }
    factor_details = {
        key: {
            "formula": factor_formulas[key],
            "weight": weights[key],
            "weighted_contribution": factors[key],
            "final_contribution": round(100.0 * factors[key] * multiplier, 2),
            "inputs": factor_inputs[key],
            "evidence": factor_evidence.get(key, []),
        }
        for key in raw_scores
    }
    limitation_reasons = {
        "F1": (
            "У поставщика нет истории похожих лотов."
            if not experience_evidence
            else f"Покрытие позиций по ОКПД2 составляет {best_item_coverage:.0%}; похожих участий: {similar_events}."
        ),
        "F2": (
            "Не найдено текстовых совпадений с историей побед."
            if text_numerator <= 0
            else f"Текстовые совпадения покрывают {min(1.0, text_numerator / text_denominator):.0%} взвешенной выборки."
        ),
        "F3": f"Победы в похожих закупках: {similar_wins} из {similar_events} участий.",
        "F4": (
            "Нет побед у этого заказчика в похожих закупках."
            if customer_wins == 0
            else f"Побед у этого заказчика: {customer_wins}."
        ),
        "F5": (
            "Нет исторических цен похожих побед для сравнения."
            if median_price is None
            else f"НМЦК {lot.get('start_price')} сопоставлена с медианой исторических цен {median_price:.2f}."
        ),
        "F6": (
            "В истории нет похожих побед."
            if most_recent_similar_win is None
            else f"Последняя похожая победа была {days_old} дн. назад."
        ),
        "F7": (
            "Не удалось определить регион поставщика по КПП."
            if not (info.get("source_msp") and not history) and supplier_region not in {"78", "47"}
            else f"Регион поставщика: {(msp or {}).get('region') or supplier_region}; регион заказчика: {target_region or 'не указан'}."
        ),
        "F8": (
            f"Статус МСП: {msp_status}; для закупки МСП этот признак влияет на оценку."
            if lot.get("is_smp") == 1 and msp_status != "member"
            else ""
        ),
    }
    score_limitations = [
        {
            "factor": factor,
            "score": round(max(0.0, min(1.0, value)), 4),
            "lost_points": round(100.0 * weights[factor] * (1.0 - max(0.0, min(1.0, value))) * multiplier, 2),
            "reason": limitation_reasons[factor],
        }
        for factor, value in raw_scores.items()
        if limitation_reasons[factor]
        and 100.0 * weights[factor] * (1.0 - max(0.0, min(1.0, value))) * multiplier >= 0.1
    ]
    score_limitations.sort(key=lambda item: item["lost_points"], reverse=True)
    modifier_reason = (
        "Оценка обнулена из-за активной записи в РНП."
        if is_risk is True
        else "Применён коэффициент за короткую историю участия."
        if multiplier < 1.0
        else "Дополнительный коэффициент не применялся."
    )
    codes = [code for event in history for code in event["codes_list"]]
    close_codes = Counter(
        code for code in codes if any(code_similarity(target, code) > 0 for target in item_codes)
    )
    explained_wins = sum(
        1
        for event in history
        if event["is_winner"]
        and weighted_code_similarity(item_weights, event["codes_list"]) > 0
    )
    leading_codes = ", ".join(code for code, _ in close_codes.most_common(3))
    fragments = []
    if item_weights and best_item_coverage > 0:
        fragments.append(f"максимальное покрытие позиций по ОКПД2: {best_item_coverage:.0%}")
    if text_numerator > 0:
        fragments.append("найдены победы в текстово-похожих закупках")
    if explained_wins:
        fragments.append(f"победил в {explained_wins} закупках по близким ОКПД2 {leading_codes}".strip())
    elif info.get("source_msp"):
        fragments.append("найден в реестре МСП по совпадающему профилю ОКВЭД")
    elif history:
        fragments.append("участвовал в похожих закупках")
    if customer_wins:
        fragments.append(f"{customer_wins} побед у этого заказчика")
    if msp_status == "member":
        fragments.append("включён в реестр МСП")
    elif msp_status == "unknown":
        fragments.append("статус МСП не проверен")
    if is_risk is True:
        fragments.append("значится в РНП")
    elif is_risk is False:
        fragments.append("в действующем РНП не найден")
    else:
        fragments.append("статус РНП не проверен")
    explanation = "; ".join(fragments) + "."

    return {
        "inn": inn,
        "name": (msp or {}).get("name"),
        "score": score,
        "role": role,
        "role_conf": round(role_conf, 2),
        "role_source": "ФНС: ОКВЭД/название" if msp else "история закупок",
        "role_signals": role_signals,
        "status": status,
        "msp": msp_status == "member",
        "msp_status": msp_status,
        "risk": is_risk is True,
        "risk_status": "listed" if is_risk is True else "clear" if is_risk is False else "unknown",
        "factors": factors,
        "factor_scores": {key: round(value, 4) for key, value in raw_scores.items()},
        "factor_details": factor_details,
        "score_limitations": score_limitations,
        "score_modifier": {
            "value": round(multiplier, 4),
            "lost_points": round(100.0 * sum(factors.values()) * (1.0 - multiplier), 2),
            "reason": modifier_reason,
        },
        "evidence": evidence_out,
        "explain": explanation,
        "_wins": similar_wins,
        "_lots": total_lots,
        "_category_wins": explained_wins,
        "_source_msp": bool(info.get("source_msp")),
    }


def get_lot(lot_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            return None
        lot = dict(row)
        items = conn.execute(
            "SELECT pos,product_name,okpd2_code,weight FROM lot_items WHERE lot_id=? ORDER BY pos", (lot_id,)
        ).fetchall()
        lot["items"] = [dict(item) for item in items]
        return lot


def search_lots(query: str, limit: int = 20) -> list[dict[str, Any]]:
    tokens = list(dict.fromkeys(tokenize(query, limit=8)))
    if not tokens:
        return []

    placeholders = ",".join("?" for _ in tokens)
    with connection() as conn:
        rows = conn.execute(
            f"""SELECT l.lot_id,l.publish_date,l.start_price,l.procedure_name,l.subject,
                       l.is_smp,l.customer_inn,
                       (SELECT COUNT(*) FROM lot_items i WHERE i.lot_id=l.lot_id) AS item_count,
                       COUNT(DISTINCT t.token) AS matched_terms,
                       CAST(COUNT(DISTINCT t.token) AS REAL)/? AS match_ratio
                FROM lot_tokens t
                JOIN lots l ON l.lot_id=t.lot_id
                WHERE t.token IN ({placeholders})
                GROUP BY l.lot_id
                ORDER BY match_ratio DESC,matched_terms DESC,
                         CASE WHEN l.publish_date IS NULL THEN 1 ELSE 0 END,
                         l.publish_date DESC,l.lot_id DESC
                LIMIT ?""",
            (len(tokens), *tokens, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def suggest_lot_search(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = list(dict.fromkeys(tokenize(query, limit=8)))
    suggestions: list[dict[str, Any]] = []

    if results:
        lot_ids = [row["lot_id"] for row in results]
        placeholders = ",".join("?" for _ in lot_ids)
        with connection() as conn:
            code_rows = conn.execute(
                f"""SELECT lot_id,okpd2_code,COUNT(*) AS occurrences
                FROM lot_items WHERE lot_id IN ({placeholders}) AND okpd2_code<>''
                GROUP BY lot_id,okpd2_code ORDER BY occurrences DESC,okpd2_code""",
                lot_ids,
            ).fetchall()
        codes_by_lot: dict[str, list[str]] = {}
        for row in code_rows:
            codes_by_lot.setdefault(row["lot_id"], []).append(row["okpd2_code"])

        for row in results:
            phrase = " ".join((row.get("procedure_name") or row.get("subject") or "").split())[:160]
            if not phrase or phrase.casefold() == query.strip().casefold():
                continue
            suggestions.append(
                {
                    "phrase": phrase,
                    "reason": f"Формулировка из похожего лота {row['lot_id']}",
                    "matched_lots": 1,
                    "okpd2_codes": codes_by_lot.get(row["lot_id"], [])[:5],
                }
            )
            if len(suggestions) >= 3:
                break
        return suggestions

    if not tokens:
        return []

    with connection() as conn:
        vocabulary = [
            row["token"]
            for row in conn.execute(
                """SELECT token FROM lot_tokens GROUP BY token
                HAVING COUNT(*)>=2 ORDER BY COUNT(*) DESC LIMIT 4000"""
            )
        ]

    for index, token in enumerate(tokens):
        close = get_close_matches(token, vocabulary, n=2, cutoff=0.68)
        for replacement in close:
            if replacement == token:
                continue
            candidate_tokens = tokens.copy()
            candidate_tokens[index] = replacement
            phrase = " ".join(candidate_tokens)
            matching = search_lots(phrase, limit=5)
            if not matching:
                continue
            codes = sorted(
                {
                    code
                    for item in matching
                    for code in (
                        item.get("okpd2_codes") or []
                    )
                }
            )
            if not codes:
                ids = [item["lot_id"] for item in matching]
                marks = ",".join("?" for _ in ids)
                with connection() as conn:
                    codes = [
                        row["okpd2_code"]
                        for row in conn.execute(
                            f"SELECT DISTINCT okpd2_code FROM lot_items WHERE lot_id IN ({marks}) AND okpd2_code<>'' ORDER BY okpd2_code LIMIT 5",
                            ids,
                        )
                    ]
            suggestions.append(
                {
                    "phrase": phrase,
                    "reason": f"Близкая формулировка; совпадений в базе: {len(matching)}",
                    "matched_lots": len(matching),
                    "okpd2_codes": codes[:5],
                }
            )
            if len(suggestions) >= 3:
                return suggestions
    return suggestions


def recommend(
    lot_id: str,
    top_k: int = 20,
    loss_weight: float | None = None,
    *,
    persist: bool = True,
    run_id: str | None = None,
    check_registries: bool = True,
) -> dict[str, Any] | None:
    started = datetime.now(timezone.utc)
    run_id = run_id or __import__("uuid").uuid4().hex
    with connection() as conn:
        lot_row = conn.execute("SELECT * FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot_row:
            return None
        lot = dict(lot_row)
        items = conn.execute(
            "SELECT product_name,okpd2_code,weight FROM lot_items WHERE lot_id=? ORDER BY pos", (lot_id,)
        ).fetchall()
        item_weights: dict[str, float] = defaultdict(float)
        for row in items:
            item_weights[row["okpd2_code"] or ""] += float(row["weight"] or 0.0)
        item_codes = sorted(
            (code for code in item_weights if code),
            key=lambda code: (-item_weights[code], code),
        )
        item_names = " ".join(row["product_name"] for row in items)
        title = " ".join((lot.get("procedure_name") or "", lot.get("subject") or "", item_names))
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM metadata")}
        weights, parameters = load_scoring_config()
        if loss_weight is not None:
            parameters["loss_weight"] = loss_weight

        candidates, text_weights, _, profile_msp, profile_error = _candidate_sources(
            conn,
            lot,
            item_codes,
            title,
            MAX_CANDIDATES,
            include_msp_candidates=check_registries,
        )
        inns = list(candidates)[:MAX_CANDIDATES]
        if check_registries:
            msp_by_inn, msp_error = lookup_msp(inns)
            msp_by_inn = {**profile_msp, **msp_by_inn}
            rnp_by_inn = lookup_rnp(inns)
            msp_checked_at = next(
                (record.get("checked_at") for record in msp_by_inn.values() if record.get("checked_at")),
                datetime.now(timezone.utc).isoformat(),
            )
            rnp_checked_at = datetime.now(timezone.utc).isoformat()
        else:
            msp_by_inn, msp_error, rnp_by_inn = {}, None, {}
            msp_checked_at = rnp_checked_at = None
        histories, total_lots_by_supplier = _supplier_history(conn, inns, lot, list(text_weights))

        records = []
        for inn in inns:
            info = candidates[inn]
            msp_status = (
                "unknown"
                if not check_registries or msp_error
                else "member"
                if inn in msp_by_inn
                else "not_member"
            )
            msp_record = msp_by_inn.get(inn)
            rnp_record = rnp_by_inn.get(inn)
            rnp_status = (
                True if rnp_record and rnp_record["status"] == "listed" else
                False if rnp_record and rnp_record["status"] == "clear" else None
            )
            item = _score_supplier(
                inn=inn,
                info=info,
                history=histories.get(inn, []),
                total_lots=total_lots_by_supplier.get(inn, 0),
                item_codes=item_codes,
                item_weights=dict(item_weights),
                title=title,
                lot=lot,
                text_weights=text_weights,
                category_total_wins=info.get("category_wins", 0),
                is_risk=rnp_status,
                msp_status=msp_status,
                msp=msp_record,
                weights=weights,
                parameters=parameters,
            )
            item["enrichment"] = {
                "msp": {
                    "status": msp_status,
                    "source": (msp_record or {}).get("source", "Реестр МСП ФНС"),
                    "source_url": (msp_record or {}).get("source_url", msp_search_url(inn)),
                    "checked_at": (msp_record or {}).get("checked_at") or msp_checked_at,
                    "name": (msp_record or {}).get("name"),
                    "category": (msp_record or {}).get("category"),
                    "okved": (msp_record or {}).get("okved"),
                    "region": (msp_record or {}).get("region"),
                    "included_at": (msp_record or {}).get("included_at"),
                },
                "rnp": rnp_record
                or {
                    "status": "unknown",
                    "source": "Единая информационная система закупок (ЕИС), реестр РНП",
                    "source_url": RNP_SEARCH_URL,
                    "checked_at": rnp_checked_at,
                },
            }
            records.append(item)
        records.sort(key=lambda item: (-item["score"], -item["_category_wins"], -item["_wins"], item["inn"]))
        results = []
        for rank, item in enumerate(records[:top_k], start=1):
            results.append({key: value for key, value in item.items() if not key.startswith("_")} | {"rank": rank})
        took_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        now = datetime.now(timezone.utc).isoformat()
        data_version = metadata.get("data_version", "unversioned")
        if persist:
            conn.execute(
                """INSERT INTO runs(run_id,kind,status,created_at,updated_at,weights_json,data_version,n_lots,completed_lots)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    "recommendation",
                    "done",
                    now,
                    now,
                    json.dumps({**weights, **parameters}, ensure_ascii=False),
                    data_version,
                    1,
                    1,
                ),
            )
            conn.executemany(
                """INSERT INTO recommendations
                (run_id,lot_id,rank,supplier_inn,supplier_name,score,role,role_conf,role_source,role_signals_json,
                 status,is_msp,risk,
                 msp_status,risk_status,enrichment_json,factors_json,factor_scores_json,evidence_json,explanation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        run_id,
                        lot_id,
                        result["rank"],
                        result["inn"],
                        result["name"],
                        result["score"],
                        result["role"],
                        result["role_conf"],
                        result["role_source"],
                        json.dumps(result["role_signals"], ensure_ascii=False),
                        result["status"],
                        int(result["msp"]),
                        int(result["risk"]),
                        result["msp_status"],
                        result["risk_status"],
                        json.dumps(result["enrichment"], ensure_ascii=False),
                        json.dumps(result["factors"], ensure_ascii=False),
                        json.dumps(result["factor_scores"], ensure_ascii=False),
                        json.dumps(result["evidence"], ensure_ascii=False),
                        result["explain"],
                    )
                    for result in results
                ),
            )
        return {
            "lot_id": lot_id,
            "run_id": run_id,
            "took_ms": took_ms,
            "data_version": data_version,
            "lot": lot,
            "items": results,
            "registry_checks": {
                "msp": "not_run" if not check_registries else "unavailable" if msp_error else "available",
                "msp_candidate_search": (
                    "not_run" if not check_registries else "unavailable" if profile_error else "available"
                ),
                "rnp": (
                    "not_run"
                    if not check_registries
                    else "not_needed"
                    if not rnp_by_inn
                    else "unavailable"
                    if rnp_by_inn and all(value is None for value in rnp_by_inn.values())
                    else "partial"
                    if any(value is None for value in rnp_by_inn.values())
                    else "available"
                ),
                "errors": {
                    key: value
                    for key, value in (
                        ("msp", msp_error),
                        ("msp_candidate_search", profile_error),
                        (
                            "rnp",
                            "ЕИС недоступна или TLS-сертификат источника не прошёл проверку"
                            if any(value is None for value in rnp_by_inn.values())
                            else None,
                        ),
                    )
                    if value
                },
            },
        }


def supplier_profile(inn: str) -> dict[str, Any] | None:
    with connection() as conn:
        participations = conn.execute(
            """SELECT p.lot_id,p.is_winner,p.supplier_kpp,l.publish_date,l.procedure_name,l.subject,
                    l.start_price,l.customer_inn,GROUP_CONCAT(DISTINCT i.okpd2_code) AS codes
            FROM participations p JOIN lots l ON l.lot_id=p.lot_id
            LEFT JOIN lot_items i ON i.lot_id=p.lot_id
            WHERE p.supplier_inn=?
            GROUP BY p.lot_id,p.is_winner,p.supplier_kpp,l.publish_date,l.procedure_name,l.subject,
                     l.start_price,l.customer_inn
            ORDER BY l.publish_date DESC""",
            (inn,),
        ).fetchall()

    msp_by_inn, msp_error = lookup_msp([inn])
    msp = msp_by_inn.get(inn)
    rnp_record = lookup_rnp([inn]).get(inn)
    rnp_status = (
        True if rnp_record and rnp_record["status"] == "listed" else
        False if rnp_record and rnp_record["status"] == "clear" else None
    )
    msp_status = "member" if msp else "unknown" if msp_error else "not_member"
    checked_at = datetime.now(timezone.utc).isoformat()
    if not msp and not participations and rnp_status is not True:
        return None

    category_counts: Counter[str] = Counter()
    for row in participations:
        for code in (row["codes"] or "").split(","):
            if code:
                category_counts[code[:5]] += int(row["is_winner"])
    winner_count = sum(int(row["is_winner"]) for row in participations)
    kpp = next((row["supplier_kpp"] for row in participations if row["supplier_kpp"]), None)
    role_name = (msp or {}).get("name")
    role_okved = (msp or {}).get("okved", "")
    role, role_conf = _role(role_name, role_okved, len(category_counts), winner_count)
    role_signals = _role_signals(role_name, role_okved, len(category_counts), winner_count)
    return {
        "inn": inn,
        "name": (msp or {}).get("name"),
        "is_msp": msp_status == "member",
        "msp_status": msp_status,
        "risk_status": "listed" if rnp_status is True else "clear" if rnp_status is False else "unknown",
        "role": role,
        "role_conf": role_conf,
        "role_source": "ФНС: ОКВЭД/название" if msp else "история закупок",
        "role_signals": role_signals,
        "region": (msp or {}).get("region") or ((kpp or "")[:2] or None),
        "stats": {
            "lots": len(participations),
            "wins": winner_count,
            "win_rate": round(winner_count / len(participations), 3) if participations else None,
            "categories": len(category_counts),
        },
        "categories": [
            {"okpd2": code, "wins": wins} for code, wins in category_counts.most_common(20)
        ],
        "history": [
            {
                "lot_id": row["lot_id"],
                "publish_date": row["publish_date"],
                "title": row["procedure_name"] or row["subject"],
                "okpd2": (row["codes"] or "").split(","),
                "is_winner": bool(row["is_winner"]),
                "customer_inn": row["customer_inn"],
                "price": row["start_price"],
            }
            for row in participations[:100]
        ],
        "registry_checks": {
            "msp": "unavailable" if msp_error else "available",
            "rnp": "unavailable" if rnp_status is None else "available",
            "errors": {
                key: value
                for key, value in (
                    ("msp", msp_error),
                    (
                        "rnp",
                        "ЕИС недоступна или TLS-сертификат источника не прошёл проверку"
                        if rnp_status is None
                        else None,
                    ),
                )
                if value
            },
        },
        "enrichment": {
            "msp": {
                "status": msp_status,
                "source": (msp or {}).get("source", "Реестр МСП ФНС"),
                "source_url": (msp or {}).get("source_url", msp_search_url(inn)),
                "checked_at": (msp or {}).get("checked_at") or checked_at,
                "name": (msp or {}).get("name"),
                "category": (msp or {}).get("category"),
                "okved": (msp or {}).get("okved"),
                "region": (msp or {}).get("region"),
                "included_at": (msp or {}).get("included_at"),
            },
            "rnp": rnp_record
            or {
                "status": "unknown",
                "source": "Единая информационная система закупок (ЕИС), реестр РНП",
                "source_url": RNP_SEARCH_URL,
                "checked_at": checked_at,
            },
        },
    }
