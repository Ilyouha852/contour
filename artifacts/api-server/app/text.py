from __future__ import annotations

import math
import re
from collections import Counter

TOKEN_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
STOP_WORDS = {
    "для", "при", "или", "что", "как", "по", "на", "из", "от", "до", "без",
    "над", "под", "и", "в", "во", "к", "ко", "с", "со", "у", "о", "об",
    "за", "не", "это", "его", "ее", "их", "всех", "иное", "прочее",
    "оказание", "оказанию", "услуги", "услуг", "работы", "работ", "поставка",
    "поставки", "товаров", "товара", "закупка", "закупки", "государственных",
    "муниципальных",
}


def tokenize(text: str, limit: int | None = None) -> list[str]:
    tokens = [t.lower().replace("ё", "е") for t in TOKEN_RE.findall(text or "")]
    useful = [token for token in tokens if token not in STOP_WORDS]
    if limit is not None:
        counts = Counter(useful)
        return [token for token, _ in counts.most_common(limit)]
    return useful


def text_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = set(tokenize(left)), set(tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    # Sørensen-Dice is robust for short procurement titles with partial overlap.
    return min(1.0, 2.0 * intersection / (len(left_tokens) + len(right_tokens)))


def cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    common = left.keys() & right.keys()
    dot = sum(left[token] * right[token] for token in common)
    norm_left = math.sqrt(sum(value * value for value in left.values()))
    norm_right = math.sqrt(sum(value * value for value in right.values()))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0