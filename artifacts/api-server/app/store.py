from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DATABASE_PATH, DATA_DIR

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY,
    publish_date TEXT,
    procedure_id TEXT,
    start_price REAL,
    procedure_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    is_smp INTEGER,
    customer_inn TEXT,
    customer_kpp TEXT,
    channel TEXT
);
CREATE INDEX IF NOT EXISTS idx_lots_publish_date ON lots(publish_date);
CREATE INDEX IF NOT EXISTS idx_lots_customer ON lots(customer_inn);

CREATE TABLE IF NOT EXISTS lot_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    product_name TEXT NOT NULL DEFAULT '',
    okpd2_code TEXT NOT NULL DEFAULT '',
    weight REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_lot_items_code ON lot_items(okpd2_code);
CREATE INDEX IF NOT EXISTS idx_lot_items_lot ON lot_items(lot_id);

CREATE TABLE IF NOT EXISTS participations (
    lot_id TEXT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
    supplier_inn TEXT NOT NULL,
    supplier_kpp TEXT,
    is_winner INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lot_id, supplier_inn)
);
CREATE INDEX IF NOT EXISTS idx_participations_supplier ON participations(supplier_inn);
CREATE INDEX IF NOT EXISTS idx_participations_winner ON participations(is_winner, lot_id);

CREATE TABLE IF NOT EXISTS lot_tokens (
    lot_id TEXT NOT NULL REFERENCES lots(lot_id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    PRIMARY KEY (lot_id, token)
);
CREATE INDEX IF NOT EXISTS idx_lot_tokens_token ON lot_tokens(token);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    data_version TEXT NOT NULL,
    n_lots INTEGER NOT NULL DEFAULT 0,
    completed_lots INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    lot_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    supplier_inn TEXT NOT NULL,
    supplier_name TEXT,
    score REAL NOT NULL,
    role TEXT NOT NULL,
    role_conf REAL NOT NULL,
    role_source TEXT NOT NULL DEFAULT 'history',
    role_signals_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    is_msp INTEGER,
    risk INTEGER NOT NULL DEFAULT 0,
    msp_status TEXT NOT NULL DEFAULT 'unknown',
    risk_status TEXT NOT NULL DEFAULT 'unknown',
    enrichment_json TEXT NOT NULL DEFAULT '{}',
    factors_json TEXT NOT NULL,
    factor_scores_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    PRIMARY KEY (run_id, lot_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_recommendations_run_lot
    ON recommendations(run_id, lot_id, rank);
"""


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("DROP TABLE IF EXISTS rnp")
        conn.execute("DROP TABLE IF EXISTS msp")
        recommendation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(recommendations)")
        }
        if "msp_status" not in recommendation_columns:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN msp_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "risk_status" not in recommendation_columns:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN risk_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "enrichment_json" not in recommendation_columns:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN enrichment_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "role_source" not in recommendation_columns:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN role_source TEXT NOT NULL DEFAULT 'history'"
            )
        if "role_signals_json" not in recommendation_columns:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN role_signals_json TEXT NOT NULL DEFAULT '[]'"
            )