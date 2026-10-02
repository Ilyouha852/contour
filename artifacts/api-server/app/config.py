from __future__ import annotations

import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
SOURCE_DIR = DATA_DIR / "source"
WEIGHTS_PATH = BASE_DIR / "config" / "weights.yaml"
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "counterparties.sqlite3"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DATASET_FILE_BYTES = 512 * 1024 * 1024
MAX_DATASET_TOTAL_BYTES = 1024 * 1024 * 1024
DATASET_BACKUP_DIR = DATA_DIR / "backups"
MAX_RECOMMENDATIONS = 100

DEFAULT_WEIGHTS: dict[str, float] = {
    "F1": 0.30,
    "F2": 0.20,
    "F3": 0.15,
    "F4": 0.10,
    "F5": 0.08,
    "F6": 0.07,
    "F7": 0.05,
    "F8": 0.05,
}

DEFAULT_PARAMETERS: dict[str, float] = {
    "tau": 3.0,
    "gamma": 0.85,
    "alpha": 2.0,
    "prior_win_rate": 0.35,
    "price_sigma": 1.0,
    "loss_weight": 0.25,
}


def load_scoring_config() -> tuple[dict[str, float], dict[str, float]]:
    """Load and validate the reproducible scoring weights and parameters."""
    import yaml

    try:
        raw: dict[str, Any] = yaml.safe_load(WEIGHTS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Не удалось прочитать конфигурацию скоринга {WEIGHTS_PATH}: {exc}") from exc
    weights = {**DEFAULT_WEIGHTS, **{key: float(value) for key, value in raw.get("weights", {}).items()}}
    parameters = {
        **DEFAULT_PARAMETERS,
        **{key: float(value) for key, value in raw.get("parameters", {}).items()},
    }
    if set(weights) != set(DEFAULT_WEIGHTS) or any(value < 0 for value in weights.values()):
        raise RuntimeError("В config/weights.yaml должны быть неотрицательные веса F1–F8")
    total = sum(weights.values())
    if total <= 0:
        raise RuntimeError("Сумма весов F1–F8 должна быть больше нуля")
    weights = {key: value / total for key, value in weights.items()}
    if not 0 <= parameters["loss_weight"] <= 1:
        raise RuntimeError("parameters.loss_weight должен быть в диапазоне 0–1")
    if parameters["tau"] <= 0 or parameters["alpha"] < 0 or parameters["price_sigma"] <= 0:
        raise RuntimeError("tau и price_sigma должны быть больше нуля, alpha не может быть отрицательным")
    if not 0 <= parameters["prior_win_rate"] <= 1 or not 0 <= parameters["gamma"] <= 1:
        raise RuntimeError("prior_win_rate и gamma должны быть в диапазоне 0–1")
    return weights, parameters