"""Самообучение: подбор порога уверенности для автопубликации.

Модель для каждой новости предсказывает approve_prob — вероятность (0..100),
что вы её одобрите. Мы сравниваем эти прогнозы с вашими реальными решениями
и ищем минимальный порог T, при котором среди новостей с прогнозом >= T
вы одобряли не меньше target_precision (по умолчанию 95%).

Пока данных мало или такого порога нет — всё «обычное» идёт к вам на проверку.
"""

MIN_SUPPORT = 10  # минимум примеров выше порога, чтобы ему доверять


def calibrate_threshold(
    labeled: list[tuple[int, int]],
    min_decisions: int,
    target_precision: float,
) -> int | None:
    if len(labeled) < min_decisions:
        return None
    for t in range(50, 100):
        above = [label for prob, label in labeled if prob >= t]
        if len(above) < MIN_SUPPORT:
            return None
        if sum(above) / len(above) >= target_precision:
            return t
    return None


def agreement(labeled: list[tuple[int, int]]) -> float | None:
    """Доля случаев, когда прогноз (>=50 → одобрят) совпал с вашим решением."""
    if not labeled:
        return None
    hits = sum(1 for prob, label in labeled if (prob >= 50) == bool(label))
    return hits / len(labeled)
