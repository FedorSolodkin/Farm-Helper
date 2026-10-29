"""Action Executor — единственное место, где что-то меняется.

Движок и ИИ только читают. Executor исполняет изменение, если выполнены все условия:
  1. есть одноразовый токен, выданный фермеру именно на это изменение;
  2. стоп-правила снова проверены в момент исполнения (мир мог измениться после подтверждения);
  3. такое же изменение ещё не применено.
Каждый шаг, включая отказ, пишется в журнал аудита. Обратимое изменение откатывается одним вызовом.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .audit import AuditLog, Clock, utcnow
from .errors import ApiError
from .models import Farm, Promo
from .scoring import validate_change
from .signals import Action, ru_date
from .tokens import TokenService, change_hash

IRREVERSIBLE = {"send_offer", "offer_wholesale"}  # отправленное сообщение не вернуть


class InMemoryPlatform:
    """Имитация API платформы. В пилоте заменяется адаптером к настоящему API (write-API, idempotency-key)."""

    def __init__(self, farm: Farm):
        self.farm = farm

    def apply(self, change: dict) -> Tuple[str, Optional[dict]]:
        handler = getattr(self, "_do_" + change["op"], None)
        if handler is None:
            raise ApiError("unknown_op", f"Операция «{change['op']}» не поддерживается", 422)
        return handler(change)

    def undo(self, record: dict) -> None:
        getattr(self, "_undo_" + record["op"])(record)

    # --- операции ---
    def _name(self, pid: str) -> str:
        return self.farm.product(pid).name

    def _do_create_promo(self, ch):
        promo = Promo(f"promo-{uuid.uuid4().hex[:8]}", ch["product_id"], date.fromisoformat(ch["start"]),
                      date.fromisoformat(ch["end"]), ch["discount"])
        self.farm.promos.append(promo)
        return (f"Акция −{ch['discount'] * 100:.0f}% на «{self._name(ch['product_id'])}» до {ru_date(promo.end)}",
                {"op": "create_promo", "promo_id": promo.id})

    def _undo_create_promo(self, rec):
        self.farm.promos = [p for p in self.farm.promos if p.id != rec["promo_id"]]

    def _do_end_promo(self, ch):
        promo = next((p for p in self.farm.promos if p.id == ch["promo_id"]), None)
        if promo is None:
            raise ApiError("not_found", "Акция не найдена", 404)
        prev_end = promo.end
        promo.end = self.farm.today - timedelta(days=1)
        return (f"Акция на «{self._name(promo.product_id)}» остановлена",
                {"op": "end_promo", "promo_id": promo.id, "prev_end": prev_end.isoformat()})

    def _undo_end_promo(self, rec):
        promo = next(p for p in self.farm.promos if p.id == rec["promo_id"])
        promo.end = date.fromisoformat(rec["prev_end"])

    def _do_set_price(self, ch):
        p = self.farm.product(ch["product_id"])
        undo = {"op": "set_price", "product_id": p.id, "price": p.price,
                "changed": p.last_price_change.isoformat() if p.last_price_change else None}
        p.price = ch["price"]
        p.last_price_change = self.farm.today
        return f"Цена «{p.name}»: {undo['price']:.0f} → {p.price:.0f} ₽/{p.unit}", undo

    def _undo_set_price(self, rec):
        p = self.farm.product(rec["product_id"])
        p.price = rec["price"]
        p.last_price_change = date.fromisoformat(rec["changed"]) if rec["changed"] else None

    def _flag(self, bucket: str, key, value, summary: str):
        store = self.farm.state.setdefault(bucket, {})
        store[key] = value
        return summary, {"op": "flag", "bucket": bucket, "key": key}

    def _undo_flag(self, rec):
        bucket = self.farm.state.get(rec["bucket"], {})
        bucket.pop(rec["key"], None)
        if not bucket:
            self.farm.state.pop(rec["bucket"], None)

    def _do_open_preorder(self, ch):
        return self._flag("preorders", ch["product_id"], ch["until"],
                          f"Предзаказ на «{self._name(ch['product_id'])}» открыт")

    def _do_plan_restock(self, ch):
        return self._flag("restock_plans", ch["product_id"], ch["target_cover_days"],
                          f"Пополнение «{self._name(ch['product_id'])}» запланировано")

    def _do_update_card(self, ch):
        return self._flag("card_work", ch["product_id"], self.farm.today.isoformat(),
                          f"Черновик карточки «{self._name(ch['product_id'])}» открыт для правок")

    def _do_create_product(self, ch):
        lst = self.farm.state.setdefault("new_categories", [])
        lst.append(ch["category"])
        return f"Категория «{ch['category']}» добавлена в каталог", {"op": "list_pop", "bucket": "new_categories", "value": ch["category"]}

    def _do_create_bundle(self, ch):
        ids = sorted(ch["product_ids"])
        self.farm.state.setdefault("bundles", []).append(ids)
        return (f"Набор «{' + '.join(self._name(i) for i in ids)}» создан",
                {"op": "list_pop", "bucket": "bundles", "value": ids})

    def _undo_list_pop(self, rec):
        lst = self.farm.state.get(rec["bucket"], [])
        if rec["value"] in lst:
            lst.remove(rec["value"])
        if not lst:
            self.farm.state.pop(rec["bucket"], None)

    def _do_fix_cards(self, ch):
        prev = self.farm.state.get("hygiene_in_progress", False)
        self.farm.state["hygiene_in_progress"] = True
        return f"Список правок открыт: {len(ch['fixes'])} шт.", {"op": "set_state", "key": "hygiene_in_progress", "value": prev}

    def _undo_set_state(self, rec):
        if rec["value"]:
            self.farm.state[rec["key"]] = rec["value"]
        else:
            self.farm.state.pop(rec["key"], None)

    def _do_offer_wholesale(self, ch):
        p = self.farm.product(ch["product_id"])
        n = len(ch["segment"])
        self.farm.state.setdefault("wholesale", []).append(
            {"product_id": p.id, "qty": ch["qty"], "discount": ch["discount"], "day": self.farm.today.isoformat(),
             "recipients": n, "segment": list(ch["segment"])})
        return (f"Оптовое предложение: {ch['qty']:.0f} {p.unit} «{p.name}» со скидкой {ch['discount'] * 100:.0f}% отправлено {n} покупателям", None)

    def _do_send_offer(self, ch):
        n = len(ch["segment"])
        self.farm.state.setdefault("offers", []).append(
            {"day": self.farm.today.isoformat(), "recipients": n, "discount": ch["discount"],
             "segment": list(ch["segment"])})
        return f"Предложение −{ch['discount'] * 100:.0f}% отправлено {n} клиентам", None  # отозвать нельзя


@dataclass
class Execution:
    id: str
    action_key: str
    type: str
    title: str
    summary: str
    effect_mid: float
    executed_at: str
    change_hash: str
    reversible: bool
    undo: Optional[dict]
    change: dict
    effect: tuple
    executed_on: date
    rolled_back: bool = False

    def public(self) -> dict:
        return {"id": self.id, "action_key": self.action_key, "type": self.type, "title": self.title,
                "summary": self.summary, "effect_mid": round(self.effect_mid), "executed_at": self.executed_at,
                "reversible": self.reversible, "rolled_back": self.rolled_back}


class Executor:
    def __init__(self, farm: Farm, farmer_id: str, tokens: TokenService, audit: AuditLog, clock: Clock = utcnow):
        self.farm = farm
        self.farmer_id = farmer_id
        self.tokens = tokens
        self.audit = audit
        self.clock = clock
        self.platform = InMemoryPlatform(farm)
        self.executions: List[Execution] = []

    def active(self) -> List[Execution]:
        """Подтверждённые сегодня и не откатанные: именно их проверит перемотка."""
        return [e for e in self.executions if not e.rolled_back and e.executed_on == self.farm.today]

    def tracked(self) -> List[Execution]:
        """Все подтверждённые и не откатанные дела: по ним копится «что принесло»."""
        return [e for e in self.executions if not e.rolled_back]

    def execute(self, token: str, action: Action) -> Execution:
        h = change_hash(action.change)
        try:
            jti = self.tokens.consume(token, self.farmer_id, action.key, h)
        except ApiError as err:
            self.audit.append(self.farmer_id, "rejected", action.key, h, {"reason": err.code})
            raise
        violations = validate_change(action.change, self.farm)
        if violations:
            self.audit.append(self.farmer_id, "blocked", action.key, h, {"reasons": violations})
            raise ApiError("blocked", "Стоп-правила не позволяют это сделать: " + "; ".join(violations), 422,
                           {"reasons": violations})
        if any(e.action_key == action.key and e.change_hash == h for e in self.active()):
            self.audit.append(self.farmer_id, "rejected", action.key, h, {"reason": "duplicate"})
            raise ApiError("duplicate", "Это изменение уже выполнено", 409)
        summary, undo = self.platform.apply(action.change)
        ex = Execution(
            id="ex-" + uuid.uuid4().hex[:8], action_key=action.key, type=action.label, title=action.title,
            summary=summary, effect_mid=action.effect_mid, executed_at=self.clock().isoformat(timespec="seconds"),
            change_hash=h, reversible=undo is not None, undo=undo, change=dict(action.change),
            effect=tuple(action.effect), executed_on=self.farm.today,
        )
        self.executions.append(ex)
        self.audit.append(self.farmer_id, "executed", action.key, h,
                          {"execution": ex.id, "summary": summary, "reversible": ex.reversible, "token": jti})
        return ex

    def rollback(self, execution_id: str) -> Execution:
        ex = next((e for e in self.executions if e.id == execution_id), None)
        if ex is None:
            raise ApiError("not_found", "Выполненное действие не найдено", 404)
        if ex.rolled_back:
            raise ApiError("already_rolled_back", "Действие уже отменено", 409)
        if ex.executed_on != self.farm.today:
            raise ApiError("archived", "Откат возможен только в день подтверждения", 409)
        if not ex.reversible:
            raise ApiError("irreversible", "Это действие нельзя откатить: сообщение уже отправлено", 409)
        self.platform.undo(ex.undo)
        ex.rolled_back = True
        self.audit.append(self.farmer_id, "rolled_back", ex.action_key, ex.change_hash,
                          {"execution": ex.id, "summary": ex.summary})
        return ex
