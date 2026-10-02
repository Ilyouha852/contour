from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATASET_BACKUP_DIR
from .store import connection

_MANIFEST = DATASET_BACKUP_DIR / "last-successful-replacement.json"


def create_dataset_snapshot() -> dict[str, Any]:
    DATASET_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_id = uuid.uuid4().hex
    snapshot_path = DATASET_BACKUP_DIR / f"dataset-{snapshot_id}.sqlite3"
    destination = sqlite3.connect(snapshot_path)
    try:
        with connection() as source:
            metadata = {row["key"]: row["value"] for row in source.execute("SELECT key,value FROM metadata")}
            source.backup(destination)
    finally:
        destination.close()
    return {
        "id": snapshot_id,
        "filename": snapshot_path.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_version": metadata.get("data_version", "unversioned"),
    }


def activate_dataset_snapshot(snapshot: dict[str, Any]) -> None:
    DATASET_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    temporary_manifest = DATASET_BACKUP_DIR / f".{uuid.uuid4().hex}.tmp"
    temporary_manifest.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary_manifest, _MANIFEST)

    for path in DATASET_BACKUP_DIR.glob("dataset-*.sqlite3"):
        if path.name != snapshot["filename"]:
            path.unlink(missing_ok=True)


def discard_dataset_snapshot(snapshot: dict[str, Any]) -> None:
    (DATASET_BACKUP_DIR / snapshot["filename"]).unlink(missing_ok=True)


def dataset_snapshot_status() -> dict[str, Any]:
    if not _MANIFEST.exists():
        return {"available": False}
    try:
        snapshot = json.loads(_MANIFEST.read_text(encoding="utf-8"))
        filename = snapshot.get("filename", "")
        if Path(filename).name != filename or not filename.startswith("dataset-"):
            return {"available": False}
        if not (DATASET_BACKUP_DIR / filename).is_file():
            return {"available": False}
        return {"available": True, **snapshot}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {"available": False}


def restore_last_dataset_snapshot() -> dict[str, Any]:
    snapshot = dataset_snapshot_status()
    if not snapshot["available"]:
        raise FileNotFoundError("Нет снимка базы для отката")
    snapshot_path = DATASET_BACKUP_DIR / snapshot["filename"]
    source = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
    try:
        with connection() as destination:
            source.backup(destination)
    finally:
        source.close()
    return snapshot
