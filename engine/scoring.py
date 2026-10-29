"""Стоп-правила, приоритизация и отбор трёх дел.

Приоритет = Эффект₽ × Уверенность × Срочность ÷ Трудоёмкость.
Стоп-правила проверяют итоговое изменение (`change`) независимо от детектора:
даже ошибка в детекторе не даст показать фермеру нарушающее правила действие.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .models import Farm
from .signals import MIN_MARGIN, Action

MAX_DISCOUNT = 0.30
PRICE_COOLDOWN_DAYS = 7
MIN_EFFECT = 500.0  # слабее — не отвлекаем фермера
MIN_PRIORITY = 200.0
SNOOZE_DAYS = 14
QUICK_EFFORT = 2  # «быстрое дело»: не дольше пары минут


def validate(action: Action, farm: Farm) -> List[str]:
    """Нарушения стоп-правил. Пустой список — действие допустимо."""
    return validate_change(action.change, farm)


def validate_change(ch: dict, farm: Farm) -> List[str]:
    """Те же правила на уровне изменения: их повторно проверяет Executor перед исполнением."""
    violations = []
    op = ch.get("op")
    if op == "create_promo":
        p = farm.product(ch["product_id"])
        if ch["discount"] > MAX_DISCOUNT + 1e-9:
            violations.append(f"скидка {ch['discount'] * 100:.0f}% выше лимита {MAX_DISCOUNT * 100:.0f}%")
        if p.price * (1 - ch["discount"]) < p.cost * (1 + MIN_MARGIN):
            violations.append("цена со скидкой ниже себестоимости + минимальная маржа")
        if farm.active_promo(p.id):
            violations.append("на товар уже действует акция")
    if op == "offer_wholesale":
        p = farm.product(ch["product_id"])
        if ch["discount"] > MAX_DISCOUNT + 1e-9:
            violations.append(f"скидка {ch['discount'] * 100:.0f}% выше лимита {MAX_DISCOUNT * 100:.0f}%")
        if p.price * (1 - ch["discount"]) < p.cost * (1 + MIN_MARGIN):
            violations.append("оптовая цена ниже себестоимости + минимальная маржа")
        if ch["qty"] <= 0 or ch["qty"] > farm.stock(p.id) + 1e-9:
            violations.append("партия больше остатка на складе")
        if not ch["segment"]:
            violations.append("пустой список получателей")
    if op == "send_offer":
        if ch["discount"] > MAX_DISCOUNT + 1e-9:
            violations.append(f"скидка {ch['discount'] * 100:.0f}% выше лимита {MAX_DISCOUNT * 100:.0f}%")
        if not ch["segment"]:
            violations.append("пустой список получателей")
    if op == "set_price":
        p = farm.product(ch["product_id"])
        if ch["price"] < p.cost * (1 + MIN_MARGIN):
            violations.append("новая цена ниже себестоимости + минимальная маржа")
        if p.last_price_change and (farm.today - p.last_price_change).days < PRICE_COOLDOWN_DAYS:
            violations.append(f"цену меняли меньше {PRICE_COOLDOWN_DAYS} дней назад")
    return violations


@dataclass
class Selection:
    selected: List[Action]
    deferred: List[Action] = field(default_factory=list)  # годятся, но не вошли в тройку
    blocked: List[Tuple[Action, List[str]]] = field(default_factory=list)  # нарушили стоп-правила
    weak: List[Action] = field(default_factory=list)  # слишком слабый эффект
    snoozed: List[Action] = field(default_factory=list)  # фермер недавно отклонил

    @property
    def total_effect(self) -> float:
        return sum(a.effect_mid for a in self.selected)

    @property
    def total_minutes(self) -> int:
        return sum(a.minutes for a in self.selected)

    @property
    def note(self) -> Optional[str]:
        if not self.selected:
            return "Сегодня срочных дел нет — всё в порядке. Загляните завтра."
        if len(self.selected) < 3:
            return "Сильных сигналов только " + str(len(self.selected)) + ": остальные дела добивать не будем."
        return None


def prioritize(
    actions: List[Action],
    farm: Farm,
    snoozed: Optional[Dict[str, dict]] = None,
    top_n: int = 3,
) -> Selection:
    """snoozed: {ключ действия: {"until": date, "effect": ₽ на момент отказа}}"""
    snoozed = snoozed or {}
    sel = Selection(selected=[])
    eligible = []
    for a in actions:
        violations = validate(a, farm)
        if violations:
            sel.blocked.append((a, violations))
        elif a.effect_mid < MIN_EFFECT or a.priority < MIN_PRIORITY:
            sel.weak.append(a)
        elif a.key in snoozed and farm.today < snoozed[a.key]["until"] and a.effect_mid < 2 * snoozed[a.key]["effect"]:
            sel.snoozed.append(a)
        else:
            eligible.append(a)
    eligible.sort(key=lambda a: a.priority, reverse=True)

    used_types, used_products = set(), set()
    for a in eligible:
        if len(sel.selected) == top_n:
            sel.deferred.append(a)
            continue
        if a.type in used_types or used_products & set(a.product_ids):
            sel.deferred.append(a)  # не повторяем тип и товар: три разных дела
            continue
        sel.selected.append(a)
        used_types.add(a.type)
        used_products.update(a.product_ids)

    # хотя бы одно быстрое дело в тройке
    if sel.selected and not any(a.effort <= QUICK_EFFORT for a in sel.selected):
        for a in sel.deferred:
            if a.effort <= QUICK_EFFORT and not (used_products - set(sel.selected[-1].product_ids)) & set(a.product_ids):
                dropped = sel.selected.pop()
                sel.deferred.remove(a)
                sel.deferred.append(dropped)
                sel.selected.append(a)
                break
    sel.selected.sort(key=lambda a: a.priority, reverse=True)
    sel.deferred.sort(key=lambda a: a.priority, reverse=True)
    return sel


def snooze(action: Action, today: date, days: int = SNOOZE_DAYS) -> Dict[str, dict]:
    """Отклонённое действие не возвращается `days` дней, если эффект не вырос вдвое."""
    return {action.key: {"until": today + timedelta(days=days), "effect": action.effect_mid}}
