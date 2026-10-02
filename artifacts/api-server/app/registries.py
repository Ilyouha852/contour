from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import truststore

truststore.inject_into_ssl()

FNS_SEARCH_URL = "https://rmsp.nalog.ru/search-proc.json"
FNS_PUBLIC_URL = "https://rmsp.nalog.ru/search.html"
RNP_SEARCH_URL = "https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html"
REQUEST_TIMEOUT = 8
FNS_MAX_INNS = 100
RNP_WORKERS = 8
USER_AGENT = "CounterpartyRecommender/1.0 (public registry lookup)"
_rnp_executor = ThreadPoolExecutor(max_workers=RNP_WORKERS)


def msp_search_url(inn: str) -> str:
    return f"{FNS_PUBLIC_URL}?{urlencode({'mode': 'quick', 'query': inn})}"


def _request_fns(payload: dict[str, str], page_size: int | None = None) -> dict:
    request = Request(
        FNS_SEARCH_URL,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://rmsp.nalog.ru/search.html?mode=inn-list",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("Unexpected response from FNS SME registry")
    if page_size:
        tokens = (result.get("pageNav") or {}).get("pageSizes") or []
        page_token = next(
            (
                token
                for size, token in tokens
                if token and size.isdigit() and int(size) >= page_size
            ),
            None,
        )
        if page_token and payload.get("page") != page_token:
            return _request_fns(payload | {"page": page_token})
    checked_at = result.get("dtQueryEnd") or datetime.now(timezone.utc).isoformat()
    for row in result["data"]:
        if isinstance(row, dict):
            row["_checked_at"] = checked_at
    return result


def _msp_record(row: dict) -> dict | None:
    if not row.get("inn") or not row.get("is_active"):
        return None
    inn = str(row["inn"])
    return {
        "inn": inn,
        "name": row.get("name_ex"),
        "category": row.get("category"),
        "okved": row.get("okved1") or "",
        "region": row.get("regioncode") or "",
        "included_at": row.get("dtregistry"),
        "source": "Реестр МСП ФНС",
        "source_url": msp_search_url(inn),
        "checked_at": row.get("_checked_at"),
    }


def lookup_msp(inns: list[str]) -> tuple[dict[str, dict], str | None]:
    found: dict[str, dict] = {}
    unique_inns = list(dict.fromkeys(inn for inn in inns if inn))
    try:
        for start in range(0, len(unique_inns), FNS_MAX_INNS):
            chunk = unique_inns[start : start + FNS_MAX_INNS]
            result = _request_fns(
                {
                    "mode": "inn-list",
                    "innList": "\n".join(chunk),
                    "sortField": "NAME_EX",
                    "sort": "ASC",
                    "page": "",
                },
                page_size=len(chunk),
            )
            for row in result["data"]:
                if not isinstance(row, dict):
                    continue
                record = _msp_record(row)
                if record:
                    found[record["inn"]] = record
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return found, f"{type(exc).__name__}: {exc}"
    return found, None


def lookup_msp_by_profile(
    okved_prefixes: list[str], region: str | None, limit: int = 100
) -> tuple[dict[str, dict], str | None]:
    prefixes = list(dict.fromkeys(prefix for prefix in okved_prefixes if prefix.isdigit()))
    if not prefixes:
        return {}, None
    payload = {
        "mode": "extended",
        "okved1": ",".join(prefixes),
        "sortField": "NAME_EX",
        "sort": "ASC",
        "page": "",
    }
    if region in {"78", "47"}:
        payload["region"] = region
    try:
        result = _request_fns(payload, page_size=100)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    found = {}
    for row in result["data"]:
        if not isinstance(row, dict):
            continue
        record = _msp_record(row)
        if record:
            found[record["inn"]] = record
            if len(found) >= limit:
                break
    return found, None


def search_msp(query: str, limit: int = 20) -> tuple[list[dict], str | None]:
    try:
        result = _request_fns(
            {
                "mode": "quick",
                "query": query,
                "sortField": "NAME_EX",
                "sort": "ASC",
                "page": "",
            },
            page_size=min(100, max(1, limit)),
        )
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    records = []
    for row in result["data"]:
        if isinstance(row, dict):
            record = _msp_record(row)
            if record:
                records.append(record)
                if len(records) >= limit:
                    break
    return records, None


def _lookup_rnp_one(inn: str) -> dict[str, str] | None:
    query = urlencode({"searchString": inn, "strictEqual": "true"})
    request = Request(
        f"{RNP_SEARCH_URL}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return None

    checked_at = datetime.now(timezone.utc).isoformat()
    source_url = f"{RNP_SEARCH_URL}?{query}"
    results_start = html.find('class="search-results')
    if results_start < 0:
        return None
    results = html[results_start:]
    if re.search(r'class=["\'][^"\']*\bnoRecords\b', results, re.IGNORECASE):
        status = "clear"
    elif re.search(rf"(?<!\d){re.escape(inn)}(?!\d)", results):
        status = "listed"
    else:
        return None
    return {
        "status": status,
        "source": "Единая информационная система закупок (ЕИС), реестр РНП",
        "source_url": source_url,
        "checked_at": checked_at,
    }


def lookup_rnp(inns: list[str]) -> dict[str, dict[str, str] | None]:
    unique_inns = list(dict.fromkeys(inn for inn in inns if inn))
    futures = {_rnp_executor.submit(_lookup_rnp_one, inn): inn for inn in unique_inns}
    results: dict[str, dict[str, str] | None] = {inn: None for inn in unique_inns}
    for future in as_completed(futures):
        inn = futures[future]
        try:
            results[inn] = future.result()
        except Exception:
            results[inn] = None
    return results