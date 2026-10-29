"""Отчёт «прогноз против факта»: что обещали, что получилось, чему научился помощник."""
from __future__ import annotations

from datetime import timedelta
from typing import Dict, List, Optional

from . import calibration
from .calibration import stats
from .executor import Execution
from .models import Farm
from .signals import TYPES, confidence_label, rub
from .simulate import Sim, ProductResult


def _pids(ex: Execution) -> List[str]:
    ch = ex.change
    if "product_id" in ch:
        return [ch["product_id"]]
    if "product_ids" in ch:
        return list(ch["product_ids"])
    return sorted({f["product_id"] for f in ch.get("fixes", [])})


def _sum(sim: Sim, pids: List[str], attr: str) -> float:
    return sum(getattr(sim.products[p], attr) for p in pids)


def measure(farm: Farm, ex: Execution, a: Sim, b: Sim) -> dict:
    """Факт действия = разница между неделей с ним и неделей без него (при одинаковых случайных числах)."""
    kind = ex.action_key.split(":")[0]
    pids = _pids(ex)
    unit = farm.product(pids[0]).unit if pids else ""
    name = farm.product(pids[0]).name if pids else ""
    fact = _sum(a, pids, "revenue") - _sum(b, pids, "revenue")
    lines: List[str] = []
    if kind == "spoilage":
        lines = [f"Продано {_sum(a, pids, 'units'):.0f} {unit} (без акции — {_sum(b, pids, 'units'):.0f} {unit})",
                 f"Списано {_sum(a, pids, 'writeoff'):.0f} {unit} (без акции — {_sum(b, pids, 'writeoff'):.0f} {unit})"]
    elif kind == "stockout":
        lines = [f"Предзаказы удержали {_sum(a, pids, 'preorder'):.1f} {unit}",
                 f"Потеряно из-за пустой полки: {_sum(a, pids, 'lost'):.1f} {unit} (без действия — {_sum(b, pids, 'lost'):.1f} {unit})"]
    elif kind == "leaky_card":
        lines = [f"Заказов с «{name}»: {_sum(a, pids, 'lines'):.0f} (без правок — {_sum(b, pids, 'lines'):.0f})"]
    elif kind == "price_off":
        lines = [f"Продано {_sum(a, pids, 'units'):.1f} {unit} (при прежней цене — {_sum(b, pids, 'units'):.1f} {unit})"]
    elif kind == "promo_loss":
        fact = _sum(a, pids, "margin") - _sum(b, pids, "margin")
        lines = [f"Маржа по «{name}»: {rub(_sum(a, pids, 'margin'))} (если бы акция шла — {rub(_sum(b, pids, 'margin'))})"]
    elif kind == "glut":
        lines = [f"Оптом продано {_sum(a, pids, 'wholesale'):.0f} {unit} из {ex.change['qty']:.0f} {unit}",
                 f"Списано {_sum(a, pids, 'writeoff'):.0f} {unit} (без оптовой партии — {_sum(b, pids, 'writeoff'):.0f} {unit})"]
    elif kind == "margin":
        fact = _sum(a, pids, "margin") - _sum(b, pids, "margin")
        lines = [f"Маржа по «{name}»: {rub(_sum(a, pids, 'margin'))} (по прежней цене — {rub(_sum(b, pids, 'margin'))})",
                 f"Продано {_sum(a, pids, 'units'):.1f} {unit} против {_sum(b, pids, 'units'):.1f} {unit}"]
    elif kind == "churn":
        fact = a.revenue - b.revenue
        seg = set(ex.change.get("segment", []))
        back = len({o.customer for o in a.orders} & seg)
        lines = [f"Вернулись {back} из {len(seg)} клиентов"]
    elif kind == "seasonal" and ex.change.get("op") == "create_product":
        fact = sum(a.extra_revenue.values()) - sum(b.extra_revenue.values())
        lines = [f"Новая категория «{ex.change['category']}» принесла {rub(fact)}"]
    elif kind == "seasonal":
        lines = [f"Потеряно из-за нехватки товара: {_sum(a, pids, 'lost'):.1f} {unit} (без запаса — {_sum(b, pids, 'lost'):.1f} {unit})"]
    elif kind == "bundle":
        lines = [f"Заказов с парой: {_sum(a, pids, 'lines'):.0f} (без набора — {_sum(b, pids, 'lines'):.0f})"]
    elif kind == "hygiene":
        lines = [f"Продажи по {len(pids)} исправленным карточкам выросли на {rub(fact)}"]
    return {"fact": fact, "details": lines}


def verdict(fact: float, low: float, high: float) -> str:
    if fact <= 0:
        return "not_worked"
    if fact < low:
        return "below"
    if fact > high:
        return "above"
    return "in_range"


def build_report(farm: Farm, executions: List[Execution], base: Sim, without_each: Dict[str, Sim], without_all: Sim,
                 cal: Dict[str, List[float]], start, days: int) -> dict:
    """Считает отчёт и обновляет калибровку по результатам."""
    rows = []
    for ex in executions:
        m = measure(farm, ex, base, without_each[ex.id])
        low, mid, high = ex.effect
        v = verdict(m["fact"], low, high)
        kind = ex.action_key.split(":")[0]
        n_before, scale_before, _ = stats(cal, kind)
        calibration.record(cal, kind, mid, m["fact"])
        n, scale, acc = stats(cal, kind)
        rows.append({
            "key": ex.action_key, "type": ex.type, "title": ex.summary,
            "forecast": {"low": round(low), "mid": round(mid), "high": round(high)},
            "fact": round(m["fact"]), "ratio": round(m["fact"] / mid, 2) if mid else None,
            "verdict": v, "details": m["details"],
            "learning": {"checks": n, "scale_before": round(scale_before, 2), "scale_after": round(scale, 2),
                         "accuracy": round(acc, 2)},
        })
    forecast_total = sum(r["forecast"]["mid"] for r in rows)
    fact_total = sum(r["fact"] for r in rows)
    in_range = sum(1 for r in rows if r["verdict"] == "in_range")
    return {
        "farm": farm.name,
        "period": {"from": start.isoformat(), "to": (start + timedelta(days=days - 1)).isoformat(), "days": days},
        "actions": rows,
        "totals": {"forecast": forecast_total, "fact": fact_total,
                   "accuracy": round(max(0.0, 1 - abs(fact_total - forecast_total) / forecast_total), 2) if forecast_total else None,
                   "in_range": in_range, "count": len(rows)},
        "week": {"revenue_with": round(base.revenue), "revenue_without": round(without_all.revenue),
                 "writeoff_with": round(base.writeoff_rub(farm)), "writeoff_without": round(without_all.writeoff_rub(farm))},
    }
