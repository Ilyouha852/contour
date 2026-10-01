from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any

from .config import load_scoring_config
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


def _active_rnp(conn, inns: list[str], today: date) -> set[str]:
    if not inns:
        return set()
    placeholders = ",".join("?" for _ in inns)
    rows = conn.execute(
        f"""SELECT DISTINCT inn FROM rnp
        WHERE inn IN ({placeholders}) AND (date_out IS NULL OR date_out='' OR date_out>=?)""",
        (*inns, today.isoformat()),
    )
    return {row["inn"] for row in rows}


def _candidate_sources(conn, lot: dict[str, Any], item_codes: list[str], title: str, limit: int):
    target_date = _effective_date(lot.get("publish_date"))
    where_parts: list[str] = []
    params: list[Any] = [lot["lot_id"], target_date.isoformat()]
    for code in item_codes:
        if not code:
            continue
        where_parts.append("(i.okpd2_code=? OR substr(i.okpd2_code,1,5)=? OR substr(i.okpd2_code,1,2)=?)")
        params.extend((code, code[:5], code[:2]))
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
    for row in category_rows:
        candidates[row["supplier_inn"]] = {
            "category_lots": int(row["category_lots"] or 0),
            "category_wins": int(row["category_wins"] or 0),
            "source_msp": False,
        }
    for inn in sorted(text_supplier_inns):
        candidates.setdefault(inn, {"category_lots": 0, "category_wins": 0, "source_msp": False})

    # In a thin category, add only matching MСП companies from the local registry.
    win_total = sum(int(row["category_wins"] or 0) for row in category_rows)
    leading_wins = int(category_rows[0]["category_wins"] or 0) if category_rows else 0
    weak_category = len(category_rows) <= 5 or (win_total > 0 and leading_wins / win_total > 0.6)
    if weak_category and item_codes:
        prefixes = sorted({code[:2] for code in item_codes if len(code) >= 2})
        region = (lot.get("customer_kpp") or "")[:2]
        params: list[str] = []
        conditions: list[str] = []
        for prefix in prefixes:
            conditions.append("okved LIKE ?")
            params.append(f"{prefix}%")
        region_clause = ""
        if region in {"78", "47"}:
            region_clause = " AND (region=? OR region LIKE ?)"
            params.extend((region, f"{region}%"))
        if conditions:
            msp_rows = conn.execute(
                f"""SELECT inn FROM msp WHERE ({' OR '.join(conditions)}){region_clause}
                ORDER BY included_at DESC LIMIT 100""",
                params,
            ).fetchall()
            for row in msp_rows:
                if row["inn"] not in candidates:
                    candidates[row["inn"]] = {
                        "category_lots": 0,
                        "category_wins": 0,
                        "source_msp": True,
                    }
                    if len(candidates) >= limit:
                        break

    return candidates, text_weights, text_supplier_inns


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


def _active_msp(conn, inn: str):
    row = conn.execute("SELECT * FROM msp WHERE inn=?", (inn,)).fetchone()
    return dict(row) if row else None


def _score_supplier(
    inn: str,
    info: dict[str, Any],
    history: list[dict[str, Any]],
    total_lots: int,
    item_codes: list[str],
    title: str,
    lot: dict[str, Any],
    text_weights: dict[str, float],
    category_total_wins: int,
    is_risk: bool,
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
    most_recent_similar_win: str | None = None
    unique_classes: set[str] = set()
    evidence: list[tuple[float, dict[str, Any]]] = []
    text_numerator = 0.0
    text_denominator = max(1e-9, sum(text_weights.values()))
    own_lots = {event["lot_id"] for event in history}

    if info.get("source_msp") and not history:
        okved = (msp or {}).get("okved", "")
        code_match = any(
            any(code.strip().startswith(target[:2]) for code in okved.replace("|", ",").split(","))
            for target in item_codes
        )
        raw_scores = {
            "F1": 0.35 if code_match else 0.0,
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
            similarity = max((code_similarity(target, code) for target in item_codes for code in codes), default=0.0)
            published = event.get("publish_date")
            age_years = _years_since(published, target_date)
            participation_weight = 1.0 if event["is_winner"] else loss_weight
            experience += participation_weight * similarity * (gamma**age_years)
            if similarity > 0:
                similar_events += 1
                if event["is_winner"]:
                    similar_wins += 1
                    if event.get("start_price") and event["start_price"] > 0:
                        price_samples.append(float(event["start_price"]))
                    if event.get("customer_inn") and event["customer_inn"] == lot.get("customer_inn"):
                        customer_wins += 1
                    if published and (most_recent_similar_win is None or published > most_recent_similar_win):
                        most_recent_similar_win = published
            if similarity > 0 and event["is_winner"]:
                product_text = event.get("products") or ""
                event_title = " ".join([event.get("procedure_name") or "", event.get("subject") or "", product_text])
                text_score = text_similarity(title, event_title)
                if event["lot_id"] in text_weights:
                    text_numerator += text_weights[event["lot_id"]]
                evidence_score = similarity * 0.65 + text_score * 0.35
                evidence.append(
                    (
                        evidence_score,
                        {
                            "lot_id": event["lot_id"],
                            "publish_date": event["publish_date"],
                            "title": event.get("procedure_name") or event.get("subject") or "",
                            "okpd2": codes[:5],
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
        if price_samples and lot.get("start_price") and lot["start_price"] > 0:
            median = statistics.median(price_samples)
            raw_scores["F5"] = math.exp(
                -abs(math.log(float(lot["start_price"])) - math.log(median)) / max(0.05, parameters["price_sigma"])
            )
        if most_recent_similar_win:
            days_old = max(0, (target_date - date.fromisoformat(most_recent_similar_win[:10])).days)
            raw_scores["F6"] = math.exp(-days_old / 730.0)

        candidate_kpp = next((event.get("supplier_kpp") for event in history if event.get("supplier_kpp")), "")
        target_region = (lot.get("customer_kpp") or "")[:2]
        supplier_region = (candidate_kpp or "")[:2]
        if supplier_region in {"78", "47"}:
            raw_scores["F7"] = 1.0 if target_region in {"78", "47"} else 0.5
        raw_scores["F8"] = 1.0 if lot.get("is_smp") != 1 else (1.0 if msp else 0.0)
        if lot.get("is_smp") == 1 and msp is None:
            raw_scores["F8"] = 0.0

    factors = {key: round(weights[key] * max(0.0, min(1.0, value)), 4) for key, value in raw_scores.items()}
    multiplier = 0.0 if is_risk else min(1.0, 0.6 + 0.1 * total_lots)
    if info.get("source_msp") and not history:
        multiplier = min(multiplier, 0.8)
    score = round(100.0 * sum(factors.values()) * multiplier, 2)
    role, role_conf = _role((msp or {}).get("name"), (msp or {}).get("okved", ""), len(unique_classes), similar_wins)

    if is_risk or multiplier < 0.5:
        status = "Риск"
    elif similar_wins >= 3 and most_recent_similar_win:
        recent_days = (target_date - date.fromisoformat(most_recent_similar_win[:10])).days
        status = "Проверенный" if recent_days <= 365 else "Участник"
    elif total_lots > 0:
        status = "Участник"
    elif msp:
        status = "Новый (реестр МСП)"
    else:
        status = "Участник"

    evidence_out = [
        item
        for _, item in sorted(evidence, key=lambda value: value[0], reverse=True)[:3]
    ]
    codes = [code for event in history for code in event["codes_list"]]
    close_codes = Counter(
        code for code in codes if any(code_similarity(target, code) > 0 for target in item_codes)
    )
    explained_wins = sum(
        1
        for event in history
        if event["is_winner"]
        and any(code_similarity(target, code) > 0 for target in item_codes for code in event["codes_list"])
    )
    leading_codes = ", ".join(code for code, _ in close_codes.most_common(3))
    fragments = []
    if explained_wins:
        fragments.append(f"победил в {explained_wins} закупках по близким ОКПД2 {leading_codes}".strip())
    elif info.get("source_msp"):
        fragments.append("найден в реестре МСП по совпадающему профилю ОКВЭД")
    elif history:
        fragments.append("участвовал в похожих закупках")
    if customer_wins:
        fragments.append(f"{customer_wins} побед у этого заказчика")
    if msp:
        fragments.append("включён в реестр МСП")
    fragments.append("значится в РНП" if is_risk else "в действующем РНП не значится")
    explanation = "; ".join(fragments) + "."

    return {
        "inn": inn,
        "name": (msp or {}).get("name"),
        "score": score,
        "role": role,
        "role_conf": round(role_conf, 2),
        "status": status,
        "msp": msp is not None,
        "risk": is_risk,
        "factors": factors,
        "factor_scores": {key: round(value, 4) for key, value in raw_scores.items()},
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


def recommend(
    lot_id: str,
    top_k: int = 20,
    loss_weight: float | None = None,
    *,
    persist: bool = True,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    started = datetime.now(timezone.utc)
    run_id = run_id or __import__("uuid").uuid4().hex
    with connection() as conn:
        lot_row = conn.execute("SELECT * FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot_row:
            return None
        lot = dict(lot_row)
        items = conn.execute(
            "SELECT product_name,okpd2_code FROM lot_items WHERE lot_id=? ORDER BY pos", (lot_id,)
        ).fetchall()
        item_codes = sorted({row["okpd2_code"] for row in items if row["okpd2_code"]})
        item_names = " ".join(row["product_name"] for row in items)
        title = " ".join((lot.get("procedure_name") or "", lot.get("subject") or "", item_names))
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM metadata")}
        weights, parameters = load_scoring_config()
        if loss_weight is not None:
            parameters["loss_weight"] = loss_weight

        candidates, text_weights, _ = _candidate_sources(conn, lot, item_codes, title, MAX_CANDIDATES)
        inns = list(candidates)[:MAX_CANDIDATES]
        risky_inns = _active_rnp(conn, inns, date.today())
        histories, total_lots_by_supplier = _supplier_history(conn, inns, lot, list(text_weights))
        msp_by_inn = {
            row["inn"]: dict(row)
            for start in range(0, len(inns), 300)
            for row in conn.execute(
                f"SELECT * FROM msp WHERE inn IN ({','.join('?' for _ in inns[start:start+300])})",
                inns[start : start + 300],
            )
        } if inns else {}

        records = []
        for inn in inns:
            info = candidates[inn]
            records.append(
                _score_supplier(
                    inn=inn,
                    info=info,
                    history=histories.get(inn, []),
                    total_lots=total_lots_by_supplier.get(inn, 0),
                    item_codes=item_codes,
                    title=title,
                    lot=lot,
                    text_weights=text_weights,
                    category_total_wins=info.get("category_wins", 0),
                    is_risk=inn in risky_inns,
                    msp=msp_by_inn.get(inn),
                    weights=weights,
                    parameters=parameters,
                )
            )
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
                (run_id,lot_id,rank,supplier_inn,supplier_name,score,role,role_conf,status,is_msp,risk,
                 factors_json,factor_scores_json,evidence_json,explanation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                        result["status"],
                        int(result["msp"]),
                        int(result["risk"]),
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
        }


def supplier_profile(inn: str) -> dict[str, Any] | None:
    with connection() as conn:
        msp_row = conn.execute("SELECT * FROM msp WHERE inn=?", (inn,)).fetchone()
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
        if not msp_row and not participations:
            return None
        active_rnp = conn.execute(
            "SELECT * FROM rnp WHERE inn=? ORDER BY date_in DESC", (inn,)
        ).fetchall()
        category_counts: Counter[str] = Counter()
        for row in participations:
            for code in (row["codes"] or "").split(","):
                if code:
                    category_counts[code[:5]] += int(row["is_winner"])
        winner_count = sum(int(row["is_winner"]) for row in participations)
        kpp = next((row["supplier_kpp"] for row in participations if row["supplier_kpp"]), None)
        return {
            "inn": inn,
            "name": msp_row["name"] if msp_row else None,
            "is_msp": bool(msp_row),
            "role": _role(
                msp_row["name"] if msp_row else None,
                msp_row["okved"] if msp_row else "",
                len(category_counts),
                winner_count,
            )[0],
            "role_conf": _role(
                msp_row["name"] if msp_row else None,
                msp_row["okved"] if msp_row else "",
                len(category_counts),
                winner_count,
            )[1],
            "region": (msp_row["region"] if msp_row else None) or ((kpp or "")[:2] or None),
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
            "enrichment_sources": [
                {
                    "source": "Реестр МСП ФНС",
                    "source_file": msp_row["source_file"],
                    "fetched_at": msp_row["fetched_at"],
                    "included_at": msp_row["included_at"],
                }
                for _ in ([msp_row] if msp_row else [])
            ]
            + [
                {
                    "source": "РНП ФАС",
                    "source_file": row["source_file"],
                    "fetched_at": row["fetched_at"],
                    "date_in": row["date_in"],
                    "date_out": row["date_out"],
                }
                for row in active_rnp
            ],
        }
