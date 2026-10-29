"""Девять детекторов сигналов. Каждый возвращает готовые к показу действия:
факты для блока «Почему», эффект в ₽ с диапазоном, срочность, трудоёмкость и структурированное
изменение (`change`), которое после подтверждения исполнит Action Executor.
Все цифры считает код; текст на их основе позже пишет LLM, а фактчекер сверяет числа.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean, median
from typing import Dict, List, Optional, Tuple

from .benchmarks import Bench
from .models import Farm

MIN_MARGIN = 0.05  # цена после скидки не ниже себестоимости + 5%
DEFAULT_PROMO_SENSITIVITY = 3.0  # +60% к продажам на каждые 20% скидки, если своих акций мало
CHURN_RETURN_PROB = 0.35  # доля ушедших, которую возвращает персональное предложение; калибруется на пилоте
CHURN_OFFER_DISCOUNT = 0.10

TYPES = {
    "spoilage": "Риск списания",
    "stockout": "Хит заканчивается",
    "leaky_card": "Карточка не конвертирует",
    "churn": "Уходят постоянные клиенты",
    "price_off": "Цена выше рынка",
    "seasonal": "Сезонный спрос",
    "promo_loss": "Акция работает в минус",
    "bundle": "Товары покупают вместе",
    "hygiene": "Гигиена карточек",
    "glut": "Сезонный излишек",
    "margin": "Маржа сжалась",
}

# априорная точность типа сигнала; в пилоте заменяется фактом «прогноз против результата»
PRIOR = {
    "spoilage": 0.95, "stockout": 0.90, "leaky_card": 0.80, "churn": 0.70, "price_off": 0.70,
    "seasonal": 0.60, "promo_loss": 0.90, "bundle": 0.60, "hygiene": 0.90, "glut": 0.60, "margin": 0.70,
}
GLUT_DISCOUNT = 0.25  # оптовая скидка; снижается, если упирается в себестоимость
GLUT_MIN_VALUE = 3000  # партия дешевле не стоит звонков
GLUT_MIN_DAYS = 8  # ближе — это уже «риск списания»
GLUT_MIN_BUYERS = 3
WHOLESALE_TAKE = 0.5  # какую долю партии берут за неделю; калибруется по результатам
MARGIN_DROP = 0.05  # падение маржи в долях цены, с которого сигналим
MARGIN_MAX_RAISE = 0.10  # не повышаем цену больше чем на 10% за раз
MARKET_CEILING = 1.30  # и не выше 130% от 75-го перцентиля категории


@dataclass
class Action:
    key: str
    type: str
    product_ids: Tuple[str, ...]
    title: str
    why: List[str]
    effect: Tuple[float, float, float]  # низ, середина, верх; ₽ за 7 дней
    effect_note: str
    confidence: float
    confidence_basis: str
    urgency: int  # 1 — без дедлайна, 2 — в течение недели, 3 — за 2–3 дня
    effort: int  # 1 — один клик … 5 — большая работа
    minutes: int
    cta: str
    change: dict
    preview: List[Tuple[str, str, str]] = field(default_factory=list)  # (что меняем, было, станет)
    draft: Optional[dict] = None
    deadline: Optional[date] = None

    @property
    def label(self) -> str:
        return TYPES[self.type]

    @property
    def effect_mid(self) -> float:
        return self.effect[1]

    @property
    def priority(self) -> float:
        return self.effect_mid * self.confidence * self.urgency / self.effort


# ---------- форматирование ----------

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def rub(x: float) -> str:
    return f"{round(x / 100) * 100:,.0f}".replace(",", " ") + " ₽"


def rub_exact(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def ru_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day:02d}.{d.month:02d}"


def qty(x: float) -> str:
    return f"{x:.0f}" if x >= 10 else f"{x:.1f}".rstrip("0").rstrip(".")


def plural(n: int, forms: Tuple[str, str, str]) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return forms[1]
    return forms[2]


def confidence_label(c: float) -> str:
    return "высокая" if c >= 0.75 else "средняя" if c >= 0.55 else "низкая"


def _range(mid: float, conf: float) -> Tuple[float, float, float]:
    spread = 0.25 + 0.5 * (1 - conf)  # ширина по разбросу недельных замеров на небольших фермах
    return (mid * (1 - spread), mid, mid * (1 + spread))


def _conf(kind: str, data_factor: float) -> float:
    return max(0.3, min(1.0, PRIOR[kind] * data_factor))


# ---------- общие расчёты ----------

def promo_sensitivity(farm: Farm) -> Tuple[float, Optional[float], int]:
    """Насколько растут продажи на единицу скидки по прошлым акциям этого фермера."""
    obs = []
    for pr in farm.promos:
        if pr.end >= farm.today:
            continue
        days = (pr.end - pr.start).days + 1
        before = farm.units(pr.product_id, pr.start - timedelta(days=14), pr.start - timedelta(days=1)) / 14
        during = farm.units(pr.product_id, pr.start, pr.end) / days
        if before > 0:
            obs.append((during / before, pr.discount))
    if len(obs) < 2:
        return DEFAULT_PROMO_SENSITIVITY, None, len(obs)
    k = mean((lift - 1) / d for lift, d in obs)
    return max(0.5, min(6.0, k)), mean(lift for lift, _ in obs), len(obs)


# ---------- что-если: те же формулы, что в детекторах ----------

def spoilage_net(farm: Farm, p, d: float) -> Optional[dict]:
    """Эффект скидки d на товар с истекающей партией; None, если срок не поджимает."""
    k, _, _ = promo_sensitivity(farm)
    rate = farm.rate(p.id)
    batches = farm.batches_of(p.id)
    if not batches or batches[0].expires is None or rate <= 0:
        return None
    batch = batches[0]
    days = (batch.expires - farm.today).days
    if days < 1:
        return None
    baseline = rate * days
    excess = max(0.0, batch.qty - baseline)
    recovered = min(excess, rate * k * d * days)
    return {"net": recovered * p.price * (1 - d) - baseline * p.price * d, "recovered": recovered, "excess": excess,
            "days": days, "rate": rate, "until": batch.expires - timedelta(days=1)}


def retune_spoilage(farm: Farm, action: "Action", d: float, scale: float = 1.0) -> "Action":
    """То же дело с другой скидкой: пересчитанные эффект, заголовок и превью."""
    p = farm.product(action.product_ids[0])
    out = spoilage_net(farm, p, d)
    if out is None:
        raise ValueError("срок годности не поджимает")
    a = copy.deepcopy(action)
    pct = round(d * 100)
    a.change["discount"] = d
    a.title = re.sub(r"скидку \d+%", f"скидку {pct}%", a.title)
    a.effect = _range(out["net"] * scale, a.confidence)
    a.effect_note = f"−{qty(out['recovered'])} {p.unit} списаний, с учётом цены скидки"
    a.preview = [("Цена", f"{p.price:.0f} ₽/{p.unit}", f"{round(p.price * (1 - d))} ₽/{p.unit}"),
                 ("Акция", "нет", f"−{pct}% до {ru_date(out['until'])}"), ("Бейдж в каталоге", "—", f"−{pct}%")]
    return a


def price_outcome(farm: Farm, bench: Dict[str, Bench], p, new_price: float) -> Optional[dict]:
    b = bench.get(p.category)
    if not b:
        return None
    prev_weekly = farm.units(p.id, farm.ago(35), farm.ago(8)) / 4
    last_weekly = farm.units(p.id, farm.ago(7), farm.ago(1))
    lost = max(0.0, prev_weekly - last_weekly)
    recovered = min(lost, last_weekly * b.elasticity * max(0.0, p.price - new_price) / p.price)
    return {"delta": (last_weekly + recovered) * new_price - last_weekly * p.price, "recovered": recovered,
            "prev_weekly": prev_weekly, "last_weekly": last_weekly}


def retune_price(farm: Farm, bench: Dict[str, Bench], action: "Action", price: float, scale: float = 1.0) -> "Action":
    p = farm.product(action.product_ids[0])
    out = price_outcome(farm, bench, p, price)
    if out is None:
        raise ValueError("нет бенчмарка категории")
    a = copy.deepcopy(action)
    a.change["price"] = price
    a.title = re.sub(r"→ \d+ ₽", f"→ {price:.0f} ₽", a.title)
    a.effect = _range(out["delta"] * scale, a.confidence)
    a.preview = [("Цена", f"{p.price:.0f} ₽/{p.unit}", f"{price:.0f} ₽/{p.unit}")]
    return a


# ---------- 1. Риск списания ----------

def spoilage_risk(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    k, mean_lift, n_promos = promo_sensitivity(farm)
    out = []
    for p in farm.products:
        rate = farm.rate(p.id)
        batches = farm.batches_of(p.id)
        if not batches or batches[0].expires is None or rate <= 0:
            continue
        batch = batches[0]
        days = (batch.expires - farm.today).days
        if not 1 <= days <= 7:
            continue
        baseline = rate * days
        excess = batch.qty - baseline
        if excess <= 0:
            continue
        options = []
        for d in (0.10, 0.15, 0.20, 0.25, 0.30):
            if p.price * (1 - d) < p.cost * (1 + MIN_MARGIN):
                continue
            recovered = min(excess, rate * k * d * days)
            net = recovered * p.price * (1 - d) - baseline * p.price * d  # спасённое минус цена скидки на то, что и так бы продалось
            options.append((d, net, recovered))
        if not options or max(o[1] for o in options) <= 0:
            continue
        top = max(o[1] for o in options)
        # не отдаём лишнего: берём наименьшую скидку, которая даёт ≥90% лучшего эффекта
        d, net, recovered = next(o for o in options if o[1] >= 0.9 * top)
        until = batch.expires - timedelta(days=1)
        conf = _conf("spoilage", 0.7 + 0.075 * min(n_promos, 4))
        basis = (f"на основе {n_promos} {plural(n_promos, ('вашей акции', 'ваших акций', 'ваших акций'))}"
                 if mean_lift else "по типичной реакции покупателей: своих акций мало")
        why = [
            f"На складе {qty(batch.qty)} {p.unit}, срок годности истекает через {days} {plural(days, ('день', 'дня', 'дней'))} ({ru_date(batch.expires)})",
            f"Продаётся ~{qty(rate)} {p.unit}/день → без акции спишется ~{qty(excess)} {p.unit} (≈ {rub(excess * p.price)} выручки)",
        ]
        if mean_lift:
            why.append(f"Ваши прошлые скидки давали +{(mean_lift - 1) * 100:.0f}% к продажам ({n_promos} {plural(n_promos, ('акция', 'акции', 'акций'))})")
        new_price = round(p.price * (1 - d))
        out.append(Action(
            key=f"spoilage:{p.id}", type="spoilage", product_ids=(p.id,),
            title=f"Запустите скидку {d * 100:.0f}% на «{p.name}» до {ru_date(until)}",
            why=why, effect=_range(net, conf),
            effect_note=f"−{qty(recovered)} {p.unit} списаний, с учётом цены скидки",
            confidence=conf, confidence_basis=basis,
            urgency=3 if days <= 3 else 2, effort=1, minutes=1,
            cta="Посмотреть и запустить",
            change={"op": "create_promo", "product_id": p.id, "discount": d,
                    "start": farm.today.isoformat(), "end": until.isoformat()},
            preview=[("Цена", f"{p.price:.0f} ₽/{p.unit}", f"{new_price} ₽/{p.unit}"),
                     ("Акция", "нет", f"−{d * 100:.0f}% до {ru_date(until)}"),
                     ("Бейдж в каталоге", "—", f"−{d * 100:.0f}%")],
            deadline=until,
        ))
    return out


# ---------- 2. Хит заканчивается ----------

def hit_stockout(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    ranked = sorted(farm.products, key=lambda p: farm.revenue(p.id, farm.ago(30), farm.ago(1)), reverse=True)
    total = sum(farm.revenue(p.id, farm.ago(30), farm.ago(1)) for p in ranked) or 1
    top = ranked[:max(1, math.ceil(0.2 * len(ranked)))]
    out = []
    for rank, p in enumerate(top, 1):
        rate = farm.rate(p.id)
        if rate <= 0 or p.id in farm.state.get("preorders", {}):
            continue
        stock = farm.stock(p.id)
        cover = stock / rate
        if cover >= 3:
            continue
        lost_days = farm.lead_days - cover
        if lost_days <= 0:
            continue
        rev_day = farm.revenue(p.id, farm.ago(14), farm.ago(1)) / 14
        share = farm.revenue(p.id, farm.ago(30), farm.ago(1)) / total
        conf = _conf("stockout", 1.0)
        out.append(Action(
            key=f"stockout:{p.id}", type="stockout", product_ids=(p.id,),
            title=f"Пополните «{p.name}»: остатка хватит на ~{qty(cover)} {plural(round(cover), ('день', 'дня', 'дней'))}",
            why=[
                f"«{p.name}» — №{rank} по выручке, {share * 100:.0f}% всех продаж за 30 дней",
                f"Остаток {qty(stock)} {p.unit} при продаже ~{qty(rate)} {p.unit}/день",
                f"Пополнение занимает ~{farm.lead_days} дн. → ~{qty(lost_days)} {plural(round(lost_days), ('день', 'дня', 'дней'))} без товара",
            ],
            effect=_range(rev_day * lost_days, conf),
            effect_note="выручка, которую не потеряете из-за пустой полки",
            confidence=conf, confidence_basis="по продажам за последние 14 дней",
            urgency=3 if cover <= 2 else 2, effort=2, minutes=3,
            cta="Открыть предзаказ",
            change={"op": "open_preorder", "product_id": p.id,
                    "until": (farm.today + timedelta(days=farm.lead_days)).isoformat()},
            preview=[("Статус в каталоге", f"В наличии {qty(stock)} {p.unit}", "Предзаказ"),
                     ("Срок поставки", "—", f"до {ru_date(farm.today + timedelta(days=farm.lead_days))}")],
            deadline=farm.today + timedelta(days=max(1, int(cover))),
        ))
    return out


# ---------- 3. «Дырявая» карточка ----------

def leaky_card(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    out = []
    for p in farm.products:
        b = bench.get(p.category)
        if not b or p.id in farm.state.get("card_work", {}):
            continue
        views = farm.view_count(p.id, farm.ago(30), farm.ago(1))
        lines = farm.lines(p.id, farm.ago(30), farm.ago(1))
        if views < b.median_views_30d or lines < 10:
            continue
        cr = lines / views
        if cr >= 0.5 * b.median_cr:
            continue
        views7 = farm.view_count(p.id, farm.ago(7), farm.ago(1))
        avg_line = farm.revenue(p.id, farm.ago(30), farm.ago(1)) / lines
        mid = views7 * (b.median_cr - cr) * avg_line * 0.5  # консервативно: закрываем половину разрыва
        conf = _conf("leaky_card", min(1.0, lines / 30))
        out.append(Action(
            key=f"leaky_card:{p.id}", type="leaky_card", product_ids=(p.id,),
            title=f"Обновите карточку «{p.name}»: фото и описание",
            why=[
                f"{views:,} просмотров за 30 дней — выше медианы категории ({b.median_views_30d:,.0f})".replace(",", " "),
                f"Конверсия {cr * 100:.1f}% при медиане {b.median_cr * 100:.1f}% — в {b.median_cr / cr:.1f} раза ниже",
                "Покупатели смотрят, но не заказывают: вероятно, не хватает фото или понятного описания",
            ],
            effect=_range(mid, conf),
            effect_note="консервативно: закрываем половину разрыва в конверсии",
            confidence=conf, confidence_basis=f"по {lines} заказам за 30 дней",
            urgency=1, effort=4, minutes=15,
            cta="Открыть черновик",
            change={"op": "update_card", "product_id": p.id, "fields": ["photo", "description"]},
            preview=[("Фото", "текущее", "новое (загрузите)"), ("Описание", "текущее", "черновик ниже")],
            draft={"source": "template", "checklist": [
                "Фото при дневном свете, светлый фон; в кадре срез и целая голова",
                "В первых строках описания: вкус, срок созревания, вес, как хранить",
                "Укажите, с чем подавать: покупатели ищут идею, а не характеристики",
                "Только то, что подтверждено: состав, срок годности, способ производства",
            ]},
        ))
    return out


# ---------- 4. Уходят постоянные клиенты ----------

def churn_risk(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    by_customer: Dict[str, List] = {}
    for o in farm.orders:
        by_customer.setdefault(o.customer, []).append(o)
    at_risk = []
    for customer, orders in by_customer.items():
        days = sorted({o.day for o in orders})
        if len(days) < 3:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        typical = median(gaps)
        pause = (farm.today - days[-1]).days
        if typical >= 1 and 2 * typical < pause <= 6 * typical:
            check = mean(sum(l.qty * l.unit_price for l in o.lines) for o in orders)
            at_risk.append((customer, typical, pause, check))
    if len(at_risk) < 3:
        return []
    recent = [o for o in farm.state.get("offers", []) if (farm.today - date.fromisoformat(o["day"])).days < 14]
    if recent:
        return []  # предложение уже ушло, ждём реакцию
    n = len(at_risk)
    avg_check = mean(c for *_, c in at_risk)
    typical = median(t for _, t, _, _ in at_risk)
    pause = median(p for _, _, p, _ in at_risk)
    mid = n * avg_check * CHURN_RETURN_PROB * (1 - CHURN_OFFER_DISCOUNT)
    conf = _conf("churn", min(1.0, n / 8))
    return [Action(
        key="churn:regulars", type="churn", product_ids=(),
        title=f"Верните {n} {plural(n, ('постоянного клиента', 'постоянных клиентов', 'постоянных клиентов'))}: персональное предложение",
        why=[
            f"{n} клиентов с 3+ заказами перестали заказывать",
            f"Обычно они заказывали раз в ~{typical:.0f} дн., сейчас пауза ~{pause:.0f} дн., больше чем в 2 раза дольше привычного",
            f"Средний чек этих клиентов — {rub(avg_check)}",
        ],
        effect=_range(mid, conf),
        effect_note=f"ожидаем возврат {CHURN_RETURN_PROB * 100:.0f}% клиентов, с учётом скидки {CHURN_OFFER_DISCOUNT * 100:.0f}%",
        confidence=conf, confidence_basis="доля возврата — типичная, уточнится по первой рассылке",
        urgency=1, effort=2, minutes=5,
        cta="Подготовить предложение",
        change={"op": "send_offer", "segment": [c for c, *_ in at_risk],
                "discount": CHURN_OFFER_DISCOUNT, "channel": "push"},
        preview=[("Получатели", "—", f"{n} клиентов (без имён и контактов)"),
                 ("Предложение", "—", f"−{CHURN_OFFER_DISCOUNT * 100:.0f}% на следующий заказ, 7 дней"),
                 ("Текст", "—", "«Давно не виделись! Скидка на ваш любимый заказ»")],
    )]


# ---------- 5. Цена вне рынка ----------

def price_off_market(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    out = []
    premium = set(farm.state.get("premium_products", []))
    for p in farm.products:
        b = bench.get(p.category)
        if not b or p.price <= b.p75_price or p.id in premium:
            continue
        prev_weekly = farm.units(p.id, farm.ago(35), farm.ago(8)) / 4
        last_weekly = farm.units(p.id, farm.ago(7), farm.ago(1))
        if prev_weekly < 2 or last_weekly > 0.8 * prev_weekly:
            continue
        lost = prev_weekly - last_weekly
        floor = max(b.p75_price, p.cost * (1 + MIN_MARGIN))
        best = None
        price = floor
        while price < p.price:
            recovered = min(lost, last_weekly * b.elasticity * (p.price - price) / p.price)
            delta = (last_weekly + recovered) * price - last_weekly * p.price
            if best is None or delta > best[1]:
                best = (price, delta, recovered)
            price += 10
        if not best or best[1] <= 0:
            continue
        new_price, delta, recovered = best
        drop = 1 - last_weekly / prev_weekly
        conf = _conf("price_off", 0.8)
        out.append(Action(
            key=f"price_off:{p.id}", type="price_off", product_ids=(p.id,),
            title=f"Проверьте цену «{p.name}»: можно снизить {p.price:.0f} → {new_price:.0f} ₽/{p.unit}",
            why=[
                f"Цена {p.price:.0f} ₽ выше 75-го перцентиля категории «{p.category}» ({b.p75_price:.0f} ₽)",
                f"Продажи упали на {drop * 100:.0f}%: {qty(last_weekly)} {p.unit} за неделю против {qty(prev_weekly)} {p.unit} раньше",
                f"При эластичности категории вернётся ~{qty(recovered)} {p.unit}/нед.",
                "Если цена отражает качество, вместо снижения усильте карточку и упаковку: нажмите «Не подходит → Цена осознанная»",
            ],
            effect=_range(delta, conf),
            effect_note="выручка в неделю с учётом более низкой цены",
            confidence=conf, confidence_basis=f"эластичность категории по {b.n_farms} фермерам региона",
            urgency=2, effort=1, minutes=1,
            cta="Посмотреть и изменить цену",
            change={"op": "set_price", "product_id": p.id, "price": new_price},
            preview=[("Цена", f"{p.price:.0f} ₽/{p.unit}", f"{new_price:.0f} ₽/{p.unit}")],
        ))
    return out


# ---------- 6. Сезонный пик ----------

def seasonal_peak(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    out = []
    for category, b in bench.items():
        if b.demand_growth < 0.30:
            continue
        mine = [p for p in farm.products if p.category == category]
        if not mine:
            if category in farm.state.get("new_categories", []):
                continue
            if not any(bench[p.category].group == b.group for p in farm.products if p.category in bench):
                continue  # смежной категории у фермера нет: подсказка была бы не по профилю
            conf = _conf("seasonal", 0.8)
            out.append(Action(
                key=f"seasonal:{category}", type="seasonal", product_ids=(),
                title=f"Добавьте в каталог «{category.lower()}»: спрос растёт",
                why=[
                    f"Спрос на «{category}» в вашем регионе вырос на {b.demand_growth * 100:.0f}% за 4 недели",
                    "У вас нет товаров в этой категории",
                    f"Средний фермер категории зарабатывает на ней {rub(b.avg_farm_weekly_rev)} в неделю",
                ],
                effect=_range(b.avg_farm_weekly_rev * b.demand_growth * 0.5, conf),
                effect_note="половина прироста спроса у среднего фермера категории",
                confidence=conf, confidence_basis=f"по {b.n_farms} фермерам региона",
                urgency=2, effort=3, minutes=20,
                cta="Создать карточку",
                change={"op": "create_product", "category": category},
                preview=[("Каталог", "нет категории", f"новая карточка в «{category}»")],
            ))
            continue
        for p in mine:
            rate = farm.rate(p.id)
            if rate <= 0 or p.id in farm.state.get("restock_plans", {}):
                continue
            cover = farm.stock(p.id) / rate
            if cover >= 7 or cover < 3:  # меньше 3 дней — это сигнал «хит заканчивается»
                continue
            weekly = farm.weekly_revenue(p.id)
            conf = _conf("seasonal", 0.9)
            mid = weekly * b.demand_growth * min(1.0, (7 - cover) / 7)
            out.append(Action(
                key=f"seasonal:{p.id}", type="seasonal", product_ids=(p.id,),
                title=f"Увеличьте запас «{p.name}»: спрос на «{category}» растёт",
                why=[
                    f"Спрос на «{category}» в регионе вырос на {b.demand_growth * 100:.0f}% за 4 недели",
                    f"Остатка «{p.name}» хватит на ~{qty(cover)} {plural(round(cover), ('день', 'дня', 'дней'))} при текущих продажах",
                    f"Растущий спрос съест запас быстрее: недельная выручка по товару — {rub(weekly)}",
                ],
                effect=_range(mid, conf), effect_note="прирост спроса, который иначе упрётся в остаток",
                confidence=conf, confidence_basis=f"по {b.n_farms} фермерам региона",
                urgency=2, effort=2, minutes=5,
                cta="Запланировать пополнение",
                change={"op": "plan_restock", "product_id": p.id, "target_cover_days": 14},
                preview=[("Запас", f"{qty(farm.stock(p.id))} {p.unit} (~{qty(cover)} дн.)",
                          f"{qty(rate * 1.35 * 14)} {p.unit} (~14 дн. при росте спроса)")],
            ))
    return out


# ---------- 7. Акция работает в минус ----------

def promo_loss(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    out = []
    for pr in farm.promos:
        if not pr.start <= farm.today <= pr.end:
            continue
        elapsed = (farm.today - pr.start).days
        remaining = (pr.end - farm.today).days
        if elapsed < 4 or remaining < 1:
            continue
        p = farm.product(pr.product_id)
        base_rate = farm.units(p.id, pr.start - timedelta(days=14), pr.start - timedelta(days=1)) / 14
        promo_rate = farm.units(p.id, pr.start, farm.today - timedelta(days=1)) / elapsed
        if base_rate <= 0:
            continue
        margin_base = base_rate * (p.price - p.cost)
        margin_promo = promo_rate * (p.price * (1 - pr.discount) - p.cost)
        loss_day = margin_base - margin_promo
        if loss_day <= 0:
            continue
        lift = promo_rate / base_rate
        mid = loss_day * min(7, remaining)
        conf = _conf("promo_loss", min(1.0, elapsed / 7) * min(1.0, base_rate * 14 / 10))
        out.append(Action(
            key=f"promo_loss:{p.id}", type="promo_loss", product_ids=(p.id,),
            title=f"Остановите акцию −{pr.discount * 100:.0f}% на «{p.name}»: она убыточна",
            why=[
                f"Скидка идёт {elapsed} {plural(elapsed, ('день', 'дня', 'дней'))}: продажи {qty(promo_rate)} {p.unit}/день против {qty(base_rate)} до неё ({(lift - 1) * 100:+.0f}%)",
                f"Чтобы скидка окупилась, продажи должны вырасти минимум в {(p.price - p.cost) / (p.price * (1 - pr.discount) - p.cost):.1f} раза",
                f"Маржа в день упала с {rub(margin_base)} до {rub(margin_promo)}",
            ],
            effect=_range(mid, conf),
            effect_note=f"сохранённая маржа за оставшиеся {min(7, remaining)} {plural(min(7, remaining), ('день', 'дня', 'дней'))} акции",
            confidence=conf, confidence_basis=f"по {elapsed} дням акции и 14 дням до неё",
            urgency=2, effort=1, minutes=1,
            cta="Остановить акцию",
            change={"op": "end_promo", "promo_id": pr.id, "product_id": p.id},
            preview=[("Акция", f"−{pr.discount * 100:.0f}% до {ru_date(pr.end)}", "завершена сегодня"),
                     ("Цена", f"{p.price * (1 - pr.discount):.0f} ₽/{p.unit}", f"{p.price:.0f} ₽/{p.unit}")],
        ))
    return out


# ---------- 8. Товары покупают вместе ----------

def bundle(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    since = farm.ago(60)
    recent = [o for o in farm.orders if o.day >= since]
    n = len(recent)
    if n < 100:
        return []
    count: Dict[str, int] = {}
    pair: Dict[Tuple[str, str], int] = {}
    for o in recent:
        ids = sorted({l.product_id for l in o.lines})
        for a in ids:
            count[a] = count.get(a, 0) + 1
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                pair[(a, b)] = pair.get((a, b), 0) + 1
    best = None
    for (a, b), ab in pair.items():
        support = ab / n
        lift = support / ((count[a] / n) * (count[b] / n))
        if support >= 0.15 and lift > 2 and (best is None or support * lift > best[0]):
            best = (support * lift, a, b, support, lift)
    if not best:
        return []
    _, a, b, support, lift = best
    if sorted([a, b]) in farm.state.get("bundles", []):
        return []
    # A — товар с большей аудиторией: его покупателям и предлагаем пару
    if count[a] < count[b]:
        a, b = b, a
    pa, pb = farm.product(a), farm.product(b)
    weeks = 60 / 7
    a_without_b = sum(1 for o in recent if a in {l.product_id for l in o.lines} and b not in {l.product_id for l in o.lines}) / weeks
    b_lines = [l for o in recent for l in o.lines if l.product_id == b]
    b_value = mean(l.qty * l.unit_price for l in b_lines)
    mid = a_without_b * 0.15 * b_value
    conf = _conf("bundle", 0.9)
    return [Action(
        key=f"bundle:{a}+{b}", type="bundle", product_ids=(a, b),
        title=f"Соберите набор «{pa.name} + {pb.name}» и показывайте «с этим покупают»",
        why=[
            f"«{pa.name}» и «{pb.name}» вместе в {support * 100:.0f}% заказов за 60 дней",
            f"Покупают вместе в {lift:.1f} раза чаще, чем если бы выбирали случайно",
            f"~{a_without_b:.0f} заказов в неделю с «{pa.name}» без «{pb.name}» — им можно предложить пару",
            "Скидка не нужна: пара и так продаётся вместе, маржа сохранится",
        ],
        effect=_range(mid, conf), effect_note="15% покупателей «только одного» берут пару",
        confidence=conf, confidence_basis="по 60 дням заказов",
        urgency=1, effort=2, minutes=5,
        cta="Создать набор",
        change={"op": "create_bundle", "product_ids": [a, b], "discount": 0.0},
        preview=[("Каталог", "—", f"набор «{pa.name} + {pb.name}»"),
                 ("Карточка «" + pa.name + "»", "—", f"блок «С этим покупают: {pb.name}»")],
    )]


# ---------- 9. Гигиена карточек ----------

def card_hygiene(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    fixes = []  # (текст, ₽ в неделю)
    for p in farm.products:
        weekly = farm.weekly_revenue(p.id)
        if not p.has_photo:
            fixes.append((f"«{p.name}»: добавьте фото", weekly * 0.04, "photo", p.id))
        if not p.has_weight:
            fixes.append((f"«{p.name}»: укажите вес", weekly * 0.015, "weight", p.id))
        if not p.has_expiry:
            fixes.append((f"«{p.name}»: укажите срок годности", weekly * 0.015, "expiry", p.id))
    unanswered = [r for r in farm.reviews if not r.answered and (farm.today - r.day).days <= 30]
    for r in unanswered:
        weekly = farm.weekly_revenue(r.product_id)
        fixes.append((f"«{farm.product(r.product_id).name}»: ответьте на отзыв ({r.rating}★)", weekly * 0.01, "review", r.product_id))
    if not fixes or farm.state.get("hygiene_in_progress"):
        return []
    mid = sum(f[1] for f in fixes)
    conf = _conf("hygiene", 1.0)
    return [Action(
        key="hygiene:cards", type="hygiene", product_ids=tuple(sorted({f[3] for f in fixes})),
        title=f"Наведите порядок в карточках: {len(fixes)} {plural(len(fixes), ('правка', 'правки', 'правок'))}",
        why=[f[0] for f in fixes[:5]] + ([f"…и ещё {len(fixes) - 5}"] if len(fixes) > 5 else []),
        effect=_range(mid, conf), effect_note="карточки с фото, весом и сроком конвертируют лучше",
        confidence=conf, confidence_basis="типичный эффект заполненной карточки",
        urgency=1, effort=2, minutes=10,
        cta="Открыть список правок",
        change={"op": "fix_cards", "fixes": [{"kind": f[2], "product_id": f[3]} for f in fixes]},
        preview=[("Карточек к правке", "—", str(len({f[3] for f in fixes})))],
    )]


# ---------- 10. Сезонный излишек → оптовая партия ----------

def _buyers(farm: Farm, pid: str, limit: int = 15) -> List[str]:
    """Покупатели товара (хэши), больше всех купившие за историю."""
    total: Dict[str, float] = {}
    for o in farm.orders:
        for l in o.lines:
            if l.product_id == pid:
                total[o.customer] = total.get(o.customer, 0.0) + l.qty
    return [c for c, _ in sorted(total.items(), key=lambda kv: -kv[1])[:limit]]


def seasonal_glut(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    out = []
    for p in farm.products:
        if not p.seasonal or any(o["product_id"] == p.id and (farm.today - date.fromisoformat(o["day"])).days < 14
                                 for o in farm.state.get("wholesale", [])):
            continue
        batches = farm.batches_of(p.id)
        rate = farm.rate(p.id)
        if not batches or rate <= 0 or batches[0].expires is None:
            continue
        days = (batches[0].expires - farm.today).days
        if days < GLUT_MIN_DAYS:
            continue
        stock = farm.stock(p.id)
        excess = stock - rate * days
        if excess <= 0:
            continue
        d = GLUT_DISCOUNT
        while d > 0.05 and p.price * (1 - d) < p.cost * (1 + MIN_MARGIN):
            d -= 0.05
        if p.price * (1 - d) < p.cost * (1 + MIN_MARGIN):
            continue
        lot = float(int(excess))
        value = lot * p.price * (1 - d)
        buyers = _buyers(farm, p.id)
        if lot < 1 or value < GLUT_MIN_VALUE or len(buyers) < GLUT_MIN_BUYERS:
            continue
        price_w = round(p.price * (1 - d))
        conf = _conf("glut", 0.8)
        out.append(Action(
            key=f"glut:{p.id}", type="glut", product_ids=(p.id,),
            title=f"Предложите оптом {qty(lot)} {p.unit} «{p.name}» по {price_w} ₽/{p.unit}",
            why=[
                f"Сезонный товар: на складе {qty(stock)} {p.unit}, срок годности — через {days} {plural(days, ('день', 'дня', 'дней'))} ({ru_date(batches[0].expires)})",
                f"В розницу за это время уйдёт ~{qty(rate * days)} {p.unit} (продаёте ~{qty(rate)} {p.unit}/день)",
                f"Остальные ~{qty(lot)} {p.unit} (≈ {rub(lot * p.price)} по розничной цене) не успеют продаться",
                f"Оптовая цена на {d * 100:.0f}% ниже розничной и всё равно выше себестоимости",
            ],
            effect=_range(value * WHOLESALE_TAKE, conf),
            effect_note=f"оптовая партия: излишек уходит сразу; считаем, что за неделю возьмут {WHOLESALE_TAKE * 100:.0f}% партии",
            confidence=conf, confidence_basis=f"по остатку, срокам и продажам; доля партии, которую возьмут, уточнится после первых продаж",
            urgency=2 if days <= 14 else 1, effort=3, minutes=10,
            cta="Подготовить оптовое предложение",
            change={"op": "offer_wholesale", "product_id": p.id, "qty": lot, "discount": round(d, 2), "segment": buyers},
            preview=[("Партия", f"{qty(stock)} {p.unit} на складе", f"{qty(lot)} {p.unit} в опт"),
                     ("Цена", f"{p.price:.0f} ₽/{p.unit}", f"{price_w} ₽/{p.unit}"),
                     ("Получатели", "—", f"{len(buyers)} покупателей (без имён и контактов)")],
            deadline=batches[0].expires - timedelta(days=2),
        ))
    return out


# ---------- 11. Маржа сжалась ----------

def margin_squeeze(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    out = []
    premium = set(farm.state.get("premium_products", []))
    for p in farm.products:
        if p.cost_prev is None or p.cost_changed is None or p.cost <= p.cost_prev or p.id in premium:
            continue
        if (farm.today - p.cost_changed).days > 90 or (p.last_price_change and p.last_price_change >= p.cost_changed):
            continue  # давно или цену уже пересматривали после роста
        m_now, m_before = (p.price - p.cost) / p.price, (p.price - p.cost_prev) / p.price
        if m_before - m_now < MARGIN_DROP:
            continue
        b = bench.get(p.category)
        e = b.elasticity if b else 1.3
        new_price = min(p.price * (1 + MARGIN_MAX_RAISE), p.cost / (1 - m_before))
        new_price = round(new_price / 10) * 10
        if new_price <= p.price or (b and new_price > b.p75_price * MARKET_CEILING):
            continue
        q = farm.units(p.id, farm.ago(28), farm.ago(1)) / 4
        if q <= 0:
            continue
        delta = q * (new_price / p.price) ** (-e) * (new_price - p.cost) - q * (p.price - p.cost)
        if delta <= 0:
            continue  # покупатели уйдут быстрее, чем вырастет маржа: не повышаем
        m_new = (new_price - p.cost) / new_price
        conf = _conf("margin", 0.8)
        out.append(Action(
            key=f"margin:{p.id}", type="margin", product_ids=(p.id,),
            title=f"Пересмотрите цену «{p.name}»: себестоимость выросла, {p.price:.0f} → {new_price:.0f} ₽/{p.unit}",
            why=[
                f"Себестоимость выросла с {p.cost_prev:.0f} до {p.cost:.0f} ₽/{p.unit} ({ru_date(p.cost_changed)})",
                f"Цена не менялась: маржа упала с {m_before * 100:.0f}% до {m_now * 100:.0f}%",
                f"При цене {new_price:.0f} ₽ маржа будет {m_new * 100:.0f}% (повышение на {(new_price / p.price - 1) * 100:.0f}%)",
                f"Часть покупателей уйдёт (эластичность категории {e:.1f}): это уже учтено в эффекте",
            ],
            effect=_range(delta, conf), effect_note="дополнительная маржа в неделю с учётом ухода части покупателей",
            confidence=conf, confidence_basis=f"по продажам за 4 недели и эластичности категории",
            urgency=2, effort=1, minutes=1, cta="Посмотреть и изменить цену",
            change={"op": "set_price", "product_id": p.id, "price": float(new_price)},
            preview=[("Цена", f"{p.price:.0f} ₽/{p.unit}", f"{new_price:.0f} ₽/{p.unit}")],
        ))
    return out


DETECTORS = [spoilage_risk, hit_stockout, leaky_card, churn_risk, price_off_market,
             seasonal_peak, promo_loss, bundle, card_hygiene, seasonal_glut, margin_squeeze]


def detect_all(farm: Farm, bench: Dict[str, Bench]) -> List[Action]:
    actions: List[Action] = []
    for detector in DETECTORS:
        actions.extend(detector(farm, bench))
    return actions
