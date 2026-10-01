from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterator

from fastapi import HTTPException, UploadFile

from .config import DEFAULT_PARAMETERS, DEFAULT_WEIGHTS, MAX_UPLOAD_BYTES
from .store import connection
from .text import tokenize

NOTICE_COLUMNS = {
    "publish_date",
    "procedure_id",
    "lot_id",
    "start_price",
    "procedure_name",
    "subject",
    "is_smp",
    "customer_inn",
    "customer_kpp",
    "is_eshop_or_aisgz",
}
ITEM_COLUMNS = {"lot_id", "product_name", "okpd2_code"}
PARTICIPATION_COLUMNS = {"lot_id", "supplier_inn", "supplier_kpp", "is_winner"}
INN_FIELDS = {"customer_inn", "supplier_inn", "inn"}
BOOL_TRUE = {"true", "1", "yes", "да", "истина"}
BOOL_FALSE = {"false", "0", "no", "нет", "ложь"}
INN_RE = re.compile(r"^\d{10}(?:\d{2})?$")


def normalize_inn(value: str | None) -> str | None:
    raw = (value or "").strip().replace(" ", "").replace("\u00a0", "")
    if not raw:
        return None
    # Spreadsheet exports sometimes turn a 12-digit INN into e.g. 6.362E+11.
    if "e" in raw.lower():
        try:
            raw = format(Decimal(raw), "f").split(".")[0]
        except InvalidOperation as exc:
            raise ValueError(f"неверный формат ИНН: {value}") from exc
    raw = raw.replace("'", "")
    if not INN_RE.fullmatch(raw):
        raise ValueError(f"ИНН должен содержать 10 или 12 цифр: {value}")
    return raw


def parse_bool(value: str | None, *, filename: str, column: str, row_number: int) -> int | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if normalized in BOOL_TRUE:
        return 1
    if normalized in BOOL_FALSE:
        return 0
    raise ValueError(f"{filename}: строка {row_number}, колонка {column}: ожидалось true/false")


def parse_date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"не удалось распознать дату: {raw}")


def parse_decimal(value: str | None) -> float | None:
    raw = (value or "").strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"неверное числовое значение: {value}") from exc
    if not parsed.is_finite():
        raise ValueError(f"неверное числовое значение: {value}")
    return float(parsed)


def _csv_rows(content: bytes, filename: str, required: set[str]) -> Iterator[tuple[int, dict[str, str]]]:
    try:
        text = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text, delimiter=";")
        headers = {str(header).strip() for header in (reader.fieldnames or [])}
        missing = sorted(required - headers)
        if missing:
            raise HTTPException(
                status_code=400,
                detail={"file": filename, "missing_columns": missing},
            )
        for line_number, raw in enumerate(reader, start=2):
            if raw is None or all(not (str(value or "").strip()) for value in raw.values()):
                continue
            if None in raw:
                raise HTTPException(
                    status_code=400,
                    detail={"file": filename, "row": line_number, "error": "лишние значения после последней колонки"},
                )
            yield line_number, {str(key).strip(): str(value or "").strip() for key, value in raw.items()}
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail={"file": filename, "error": "ожидалась кодировка UTF-8"}) from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail={"file": filename, "error": str(exc)}) from exc


async def read_upload(upload: UploadFile | None, required: bool = True) -> tuple[str, bytes] | None:
    if upload is None:
        if required:
            raise HTTPException(status_code=400, detail="Не загружен обязательный CSV-файл")
        return None
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Файл {upload.filename} превышает 25 МБ")
    if not data.strip():
        if required:
            raise HTTPException(status_code=400, detail=f"Файл {upload.filename} пуст")
        return upload.filename or "suppliers.csv", b""
    return upload.filename or "upload.csv", data


def import_csv_bytes(
    notices_file: tuple[str, bytes],
    items_file: tuple[str, bytes],
    suppliers_file: tuple[str, bytes] | None,
    *,
    replace: bool = True,
) -> dict[str, object]:
    notice_name, notice_bytes = notices_file
    item_name, item_bytes = items_file
    supplier_name, supplier_bytes = suppliers_file or ("", b"")
    counts: Counter[str] = Counter()
    item_positions: Counter[str] = Counter()
    file_hashes = {
        "notices": hashlib.sha256(notice_bytes).hexdigest(),
        "items": hashlib.sha256(item_bytes).hexdigest(),
        "suppliers": hashlib.sha256(supplier_bytes).hexdigest(),
    }
    data_version = hashlib.sha256("".join(file_hashes.values()).encode()).hexdigest()[:16]

    with connection() as conn:
        previous_metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM metadata")}
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS fresh_lots(lot_id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM fresh_lots")
        if replace:
            conn.execute("DELETE FROM recommendations")
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM lot_tokens")
            conn.execute("DELETE FROM participations")
            conn.execute("DELETE FROM lot_items")
            conn.execute("DELETE FROM lots")

        for line, row in _csv_rows(notice_bytes, notice_name, NOTICE_COLUMNS):
            try:
                lot_id = row["lot_id"].strip()
                if not lot_id:
                    raise ValueError("lot_id не может быть пустым")
                is_smp = parse_bool(row["is_smp"], filename=notice_name, column="is_smp", row_number=line)
                customer_inn = normalize_inn(row["customer_inn"])
                conn.execute(
                    """INSERT INTO lots
                    (lot_id,publish_date,procedure_id,start_price,procedure_name,subject,is_smp,
                     customer_inn,customer_kpp,channel)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(lot_id) DO UPDATE SET
                      publish_date=excluded.publish_date, procedure_id=excluded.procedure_id,
                      start_price=excluded.start_price, procedure_name=excluded.procedure_name,
                      subject=excluded.subject, is_smp=excluded.is_smp,
                      customer_inn=excluded.customer_inn, customer_kpp=excluded.customer_kpp,
                      channel=excluded.channel""",
                    (
                        lot_id,
                        parse_date(row["publish_date"]),
                        row["procedure_id"] or None,
                        parse_decimal(row["start_price"]),
                        row["procedure_name"],
                        row["subject"],
                        is_smp,
                        customer_inn,
                        row["customer_kpp"] or None,
                        row["is_eshop_or_aisgz"] or None,
                    ),
                )
                if not replace:
                    conn.execute("INSERT OR IGNORE INTO fresh_lots(lot_id) VALUES(?)", (lot_id,))
                counts["notices"] += 1
            except (ValueError, InvalidOperation) as exc:
                raise HTTPException(status_code=400, detail={"file": notice_name, "row": line, "error": str(exc)}) from exc

        if not replace:
            conn.execute("DELETE FROM lot_items WHERE lot_id IN (SELECT lot_id FROM fresh_lots)")
            conn.execute("DELETE FROM participations WHERE lot_id IN (SELECT lot_id FROM fresh_lots)")

        for line, row in _csv_rows(item_bytes, item_name, ITEM_COLUMNS):
            lot_id = row["lot_id"].strip()
            if not lot_id or lot_id not in_lot(conn, lot_id):
                counts["orphan_items"] += 1
                continue
            item_positions[lot_id] += 1
            conn.execute(
                "INSERT INTO lot_items(lot_id,pos,product_name,okpd2_code) VALUES (?,?,?,?)",
                (lot_id, item_positions[lot_id], row["product_name"], row["okpd2_code"].strip()),
            )
            counts["items"] += 1

        if supplier_bytes:
            for line, row in _csv_rows(supplier_bytes, supplier_name, PARTICIPATION_COLUMNS):
                lot_id = row["lot_id"].strip()
                if not lot_id or not in_lot(conn, lot_id):
                    counts["orphan_participations"] += 1
                    continue
                try:
                    inn = normalize_inn(row["supplier_inn"])
                    if not inn:
                        raise ValueError("supplier_inn не может быть пустым")
                    is_winner = parse_bool(
                        row["is_winner"], filename=supplier_name, column="is_winner", row_number=line
                    )
                    if is_winner is None:
                        raise ValueError("is_winner не может быть пустым")
                    conn.execute(
                        """INSERT INTO participations(lot_id,supplier_inn,supplier_kpp,is_winner)
                        VALUES (?,?,?,?) ON CONFLICT(lot_id,supplier_inn) DO UPDATE SET
                        supplier_kpp=COALESCE(excluded.supplier_kpp,participations.supplier_kpp),
                        is_winner=MAX(excluded.is_winner,participations.is_winner)""",
                        (lot_id, inn, row["supplier_kpp"] or None, is_winner),
                    )
                    counts["participations"] += 1
                except (ValueError, InvalidOperation) as exc:
                    raise HTTPException(status_code=400, detail={"file": supplier_name, "row": line, "error": str(exc)}) from exc

        # Keep no more than 50 positions per lot and normalize each item's share.
        conn.execute(
            """DELETE FROM lot_items WHERE item_id IN (
              SELECT item_id FROM (
                SELECT item_id, ROW_NUMBER() OVER (PARTITION BY lot_id ORDER BY pos, item_id) AS position
                FROM lot_items
              ) WHERE position > 50
            )"""
        )
        conn.execute(
            """UPDATE lot_items SET weight = 1.0 / (
              SELECT COUNT(*) FROM lot_items AS same_lot WHERE same_lot.lot_id = lot_items.lot_id
            )"""
        )
        if not replace and previous_metadata.get("data_version"):
            data_version = hashlib.sha256(
                f"{previous_metadata['data_version']}:{data_version}".encode()
            ).hexdigest()[:16]
        _rebuild_lot_tokens(conn)
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES('data_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (data_version,),
        )
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES('source_hashes',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                __import__("json").dumps(
                    {
                        **(
                            __import__("json").loads(previous_metadata["source_hashes"])
                            if not replace and previous_metadata.get("source_hashes")
                            else {}
                        ),
                        **file_hashes,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES('weights',?) ON CONFLICT(key) DO NOTHING",
            (__import__("json").dumps({**DEFAULT_WEIGHTS, **DEFAULT_PARAMETERS}, ensure_ascii=False),),
        )

    return {
        "data_version": data_version,
        "counts": dict(counts),
        "skipped_blank_rows": "пустые строки пропущены",
        "source_hashes": file_hashes,
    }


def in_lot(conn, lot_id: str) -> bool:
    return conn.execute("SELECT 1 FROM lots WHERE lot_id=?", (lot_id,)).fetchone() is not None


def _rebuild_lot_tokens(conn) -> None:
    conn.execute("DELETE FROM lot_tokens")
    lots = conn.execute(
        """SELECT l.lot_id,l.procedure_name,l.subject,GROUP_CONCAT(i.product_name,' ') AS products
        FROM lots l LEFT JOIN lot_items i ON i.lot_id=l.lot_id
        GROUP BY l.lot_id,l.procedure_name,l.subject"""
    )
    for row in lots:
        words = set(
            tokenize(
                " ".join([row["procedure_name"] or "", row["subject"] or "", row["products"] or ""]),
                12,
            )
        )
        conn.executemany(
            "INSERT OR IGNORE INTO lot_tokens(lot_id,token) VALUES (?,?)",
            ((row["lot_id"], token) for token in words),
        )


def import_registry_csv(
    kind: str,
    filename: str,
    content: bytes,
) -> dict[str, object]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    aliases = {
        "inn": {"inn", "supplier_inn", "идентификационный номер налогоплательщика"},
        "name": {"name", "company_name", "full_name", "наименование", "наименование организации"},
        "category": {"category", "категория"},
        "okved": {"okved", "okved_code", "оквэд", "код оквэд"},
        "region": {"region", "регион"},
        "included_at": {"included_at", "date_included", "дата включения"},
        "reg_number": {"reg_number", "number", "номер реестровой записи"},
        "date_in": {"date_in", "date_start", "дата включения в рнп"},
        "date_out": {"date_out", "date_end", "дата исключения из рнп"},
    }
    required = {"inn", "name", "okved"} if kind == "msp" else {"inn", "reg_number"}
    text = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text, delimiter=";")
    raw_headers = reader.fieldnames or []
    normalized_headers = {header.strip().lower(): header for header in raw_headers}
    mapping = {
        target: next((normalized_headers[candidate] for candidate in choices if candidate in normalized_headers), None)
        for target, choices in aliases.items()
    }
    missing = sorted(field for field in required if not mapping.get(field))
    if missing:
        raise HTTPException(status_code=400, detail={"file": filename, "missing_columns": missing})

    count = 0
    table = "msp" if kind == "msp" else "rnp"
    with connection() as conn:
        for row_number, raw in enumerate(reader, start=2):
            if not raw or all(not str(v or "").strip() for v in raw.values()):
                continue
            try:
                inn = normalize_inn(raw.get(mapping["inn"]) if mapping["inn"] else None)
                if not inn:
                    continue
                if kind == "msp":
                    okved_value = (raw.get(mapping["okved"]) if mapping["okved"] else "") or ""
                    conn.execute(
                        """INSERT INTO msp(inn,name,category,okved,region,included_at,source_file,fetched_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(inn) DO UPDATE SET name=excluded.name,
                        category=excluded.category,okved=excluded.okved,region=excluded.region,
                        included_at=excluded.included_at,source_file=excluded.source_file,fetched_at=excluded.fetched_at""",
                        (
                            inn,
                            (raw.get(mapping["name"]) if mapping["name"] else "") or None,
                            (raw.get(mapping["category"]) if mapping["category"] else "") or None,
                            okved_value.strip(),
                            (raw.get(mapping["region"]) if mapping["region"] else "") or None,
                            parse_date(raw.get(mapping["included_at"]) if mapping["included_at"] else None),
                            filename,
                            fetched_at,
                        ),
                    )
                else:
                    reg_number = (raw.get(mapping["reg_number"]) if mapping["reg_number"] else "") or ""
                    conn.execute(
                        """INSERT INTO rnp(inn,reg_number,date_in,date_out,source_file,fetched_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(inn,reg_number) DO UPDATE SET
                        date_in=excluded.date_in,date_out=excluded.date_out,
                        source_file=excluded.source_file,fetched_at=excluded.fetched_at""",
                        (
                            inn,
                            reg_number,
                            parse_date(raw.get(mapping["date_in"]) if mapping["date_in"] else None),
                            parse_date(raw.get(mapping["date_out"]) if mapping["date_out"] else None),
                            filename,
                            fetched_at,
                        ),
                    )
                count += 1
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"file": filename, "row": row_number, "error": str(exc)}) from exc
        conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{table}_source_date", fetched_at),
        )
    return {"source": table, "records_loaded": count, "source_file": filename, "fetched_at": fetched_at}