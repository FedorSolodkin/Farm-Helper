"""Цикл обучения: сравнение факта с прогнозом поправляет следующие прогнозы и уверенность.

Для каждого типа дел у фермера хранится история отношений «факт / прогноз». Поправка к эффекту сглажена
(мало проверок — почти не меняем), уверенность растёт или падает по точности прошлых прогнозов."""
from __future__ import annotations

from statistics import mean
from typing import Dict, List, Tuple

from .signals import PRIOR, TYPES, Action, _range, confidence_label

SMOOTHING = 2  # столько «воображаемых» проверок с отношением 1,0 добавляем к фактическим
SCALE_BOUNDS = (0.5, 1.5)


def record(cal: Dict[str, List[float]], type_: str, forecast: float, fact: float) -> None:
    cal.setdefault(type_, []).append(fact / forecast if forecast > 0 else 0.0)


def stats(cal: Dict[str, List[float]], type_: str) -> Tuple[int, float, float]:
    """(число проверок, поправка к эффекту, средняя точность)"""
    ratios = cal.get(type_, [])
    n = len(ratios)
    if not n:
        return 0, 1.0, PRIOR[type_]
    scale = (sum(ratios) + SMOOTHING) / (n + SMOOTHING)
    acc = mean(min(r, 1 / r) if r > 0 else 0.0 for r in ratios)
    return n, max(SCALE_BOUNDS[0], min(SCALE_BOUNDS[1], scale)), acc


def track(cal: Dict[str, List[float]], type_: str) -> dict:
    """История таких советов для карточки: сколько раз проверяли и какую долю прогноза дал факт."""
    ratios = cal.get(type_, [])
    if not ratios:
        return {"n": 0, "avg_pct": None, "hit": 0, "prior_pct": round(PRIOR[type_] * 100)}
    return {"n": len(ratios), "avg_pct": round(mean(ratios) * 100), "hit": sum(1 for r in ratios if 0.75 <= r <= 1.25),
            "prior_pct": round(PRIOR[type_] * 100)}


def apply(actions: List[Action], cal: Dict[str, List[float]]) -> None:
    for a in actions:
        n, scale, acc = stats(cal, a.type)
        if not n:
            continue
        w = n / (n + SMOOTHING)
        a.confidence = max(0.3, min(1.0, a.confidence * ((1 - w) + w * acc / PRIOR[a.type])))
        a.effect = _range(a.effect[1] * scale, a.confidence)
        a.confidence_basis += f" · прогноз поправлен ×{scale:.2f} по {n} вашим проверкам".replace(".", ",")
