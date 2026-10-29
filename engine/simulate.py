"""Симулятор недели: «мир» для перемотки на 7 дней.

Это стенд, а не прогноз. Спрос и поведение клиентов берутся из истории фермы, а реакция на действия
задана скрытыми параметрами (TRUTH), которые движок не видит и оценивает по своим формулам, поэтому
прогноз и факт расходятся, как в жизни. В пилоте факт придёт из реальных продаж платформы,
а остальной конвейер (замер, отчёт, калибровка) останется тем же.

Одну и ту же неделю считаем несколько раз при одних и тех же случайных числах: с действиями и без
каждого из них. Разница между прогонами и есть факт, который принесло конкретное действие."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Dict, List, Optional, Set, Tuple

from .benchmarks import BENCHMARKS
from .models import Batch, Farm, Order, OrderLine
from .privacy import usable_benchmarks

DEFAULT_TRUTH = dict(
    promo_k=2.6,          # на сколько растёт спрос на единицу скидки
    promo_lift={},        # заранее известный подъём для конкретных товаров (скидка, которая не работает)
    preorder_capture=0.7,  # доля спроса, которую удерживает предзаказ, пока товара нет
    return_prob=0.28,     # доля ушедших, вернувшихся после предложения
    card_gain=0.55,       # какую часть разрыва в конверсии закрывает обновлённая карточка
    hygiene=0.6,          # реальный эффект заполненной карточки относительно типичного
    bundle_uptake=0.12,   # доля покупателей «только A», которые берут пару
    elasticity=1.7,       # эластичность спроса по цене
    season=0.8,           # какая часть роста спроса категории доходит до фермера
    newcat_capture=0.3,   # доля прироста спроса, которую получает новая категория
    wholesale_take=0.4,   # какую долю оптовой партии реально берут за неделю
)
TRUTH = {"cheese": dict(promo_k=2.6, promo_lift={"brynza": 1.15}), "veg": dict(promo_k=2.9), "honey": dict(promo_k=3.3)}
REORDER_TARGET_DAYS = 14   # на сколько дней пополняет
CARD_RAMP = [0.0, 0.33, 0.66, 1.0, 1.0, 1.0, 1.0]  # правки карточки действуют не сразу
MAX_INCLUDE = 0.95


def truth_for(farm: Farm) -> dict:
    return {**DEFAULT_TRUTH, **TRUTH.get(farm.id.replace("farm-", ""), {})}


def _rng(*parts) -> random.Random:
    return random.Random(":".join(str(p) for p in parts))


def _poisson(rng: random.Random, lam: float) -> int:
    import math
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


@dataclass
class Profile:
    """Спрос фермы по истории: не зависит от того, какие действия подтверждены."""
    incl: Dict[str, float]
    qty: Dict[str, float]
    price_ref: Dict[str, float]
    views_pd: Dict[str, float]
    lam_oneoff: float
    regulars: List[Tuple[str, float]]  # (клиент, типичный интервал между заказами)
    pair: Optional[Tuple[str, str, float]]  # (A, B, доля заказов с A, где есть B)


def build_profile(farm: Farm) -> Profile:
    t = farm.today
    since = t - timedelta(days=14)
    promo_days: Dict[str, Set[date]] = {p.id: set() for p in farm.products}
    for pr in farm.promos:
        d = pr.start
        while d <= pr.end:
            promo_days.setdefault(pr.product_id, set()).add(d)
            d += timedelta(days=1)
    orders_n = {p.id: 0 for p in farm.products}
    lines = dict(orders_n)
    qty_sum = {p.id: 0.0 for p in farm.products}
    prices: Dict[str, List[float]] = {p.id: [] for p in farm.products}
    for o in farm.orders:
        if o.day < since:
            continue
        for pid in orders_n:
            if o.day not in promo_days[pid]:
                orders_n[pid] += 1
        for ln in o.lines:
            if o.day not in promo_days[ln.product_id]:
                lines[ln.product_id] += 1
                qty_sum[ln.product_id] += ln.qty
                prices[ln.product_id].append(ln.unit_price)
    incl = {pid: lines[pid] / orders_n[pid] if orders_n[pid] else 0.0 for pid in lines}
    qty = {pid: qty_sum[pid] / lines[pid] if lines[pid] else 1.0 for pid in lines}
    price_ref = {pid: median(prices[pid]) if prices[pid] else farm.product(pid).price for pid in lines}
    views_pd = {p.id: farm.view_count(p.id, since, t - timedelta(days=1)) / 14 for p in farm.products}

    by_customer: Dict[str, Set[date]] = {}
    for o in farm.orders:
        by_customer.setdefault(o.customer, set()).add(o.day)
    regulars, oneoff_orders = [], 0
    for customer, days in by_customer.items():
        days = sorted(days)
        if len(days) >= 3:
            typical = median([(b - a).days for a, b in zip(days, days[1:])])
            if typical >= 1 and (t - days[-1]).days <= 2 * typical:
                regulars.append((customer, typical))
        else:
            oneoff_orders += sum(1 for d in days if d >= t - timedelta(days=28))
    return Profile(incl, qty, price_ref, views_pd, oneoff_orders / 28, regulars, _top_pair(farm, incl))


def _top_pair(farm: Farm, incl: Dict[str, float]) -> Optional[Tuple[str, str, float]]:
    recent = [o for o in farm.orders if o.day >= farm.ago(60)]
    if len(recent) < 100:
        return None
    count: Dict[str, int] = {}
    pair: Dict[Tuple[str, str], int] = {}
    for o in recent:
        ids = sorted({l.product_id for l in o.lines})
        for i, a in enumerate(ids):
            count[a] = count.get(a, 0) + 1
            for b in ids[i + 1:]:
                pair[(a, b)] = pair.get((a, b), 0) + 1
    n = len(recent)
    best = None
    for (a, b), ab in pair.items():
        support, lift = ab / n, (ab / n) / ((count[a] / n) * (count[b] / n))
        if support >= 0.15 and lift > 2 and (best is None or support * lift > best[0]):
            best = (support * lift, a, b, ab / count[a], ab / count[b])
    if not best:
        return None
    _, a, b, p_b_given_a, p_a_given_b = best
    return (a, b, p_b_given_a) if count[a] >= count[b] else (b, a, p_a_given_b)


@dataclass
class ProductResult:
    units: float = 0.0
    revenue: float = 0.0
    margin: float = 0.0
    lines: int = 0
    lost: float = 0.0
    writeoff: float = 0.0
    preorder: float = 0.0
    wholesale: float = 0.0


@dataclass
class Sim:
    days: int
    orders: List[Order] = field(default_factory=list)
    views: Dict[str, Dict[date, int]] = field(default_factory=dict)
    products: Dict[str, ProductResult] = field(default_factory=dict)
    extra_revenue: Dict[str, float] = field(default_factory=dict)  # выручка, не привязанная к заказам (новая категория)
    batches: List[Batch] = field(default_factory=list)

    @property
    def revenue(self) -> float:
        return sum(r.revenue for r in self.products.values()) + sum(self.extra_revenue.values())

    def writeoff_rub(self, farm: Farm) -> float:
        return sum(r.writeoff * farm.product(pid).price for pid, r in self.products.items())


def counterfactual(farm: Farm, executions, skip) -> Farm:
    """Мир, где действие `skip` (или все, если skip=None) не подтверждали."""
    from .executor import InMemoryPlatform
    world = farm.fork()
    platform = InMemoryPlatform(world)
    for ex in executions:
        if skip is not None and ex.id != skip.id:
            continue
        if ex.undo is not None:
            platform.undo(ex.undo)
        elif ex.change["op"] == "send_offer" and world.state.get("offers"):
            world.state["offers"].pop()
            if not world.state["offers"]:
                world.state.pop("offers")
        elif ex.change["op"] == "offer_wholesale" and world.state.get("wholesale"):
            world.state["wholesale"].pop()
            if not world.state["wholesale"]:
                world.state.pop("wholesale")
    return world


def simulate(farm: Farm, profile: Profile, days: int = 7, seed: str = "") -> Sim:
    truth = truth_for(farm)
    bench = usable_benchmarks(BENCHMARKS)
    sim = Sim(days=days, products={p.id: ProductResult() for p in farm.products})
    t0 = farm.today
    state = farm.state
    preorders, plans = state.get("preorders", {}), state.get("restock_plans", {})
    card_work, in_progress = state.get("card_work", {}), state.get("hygiene_in_progress", False)
    products = {p.id: p for p in farm.products}
    rates = {pid: max(farm.rate(pid, 14), 0.01) for pid in products}

    stock: Dict[str, List[list]] = {pid: [[b.qty, b.expires] for b in farm.batches_of(pid)] for pid in products}
    pending: Dict[str, Tuple[int, float]] = {}
    for pid in list(preorders) + list(plans):
        pending[pid] = (farm.lead_days, rates[pid] * plans.get(pid, REORDER_TARGET_DAYS))

    # доля фермерских «проблемных» карточек, которые будут исправлены (эффект относительно типичного)
    hygiene_gain: Dict[str, float] = {}
    if in_progress:
        for p in farm.products:
            g = (0.04 if not p.has_photo else 0) + (0.015 if not p.has_weight else 0) + (0.015 if not p.has_expiry else 0)
            g += 0.01 * sum(1 for r in farm.reviews if r.product_id == p.id and not r.answered)
            if g:
                hygiene_gain[p.id] = g * truth["hygiene"]

    def card_boost(pid: str, d: int) -> float:
        if pid not in card_work:
            return 1.0
        b = bench.get(products[pid].category)
        views = farm.view_count(pid, farm.ago(30), farm.ago(1))
        lines = farm.lines(pid, farm.ago(30), farm.ago(1))
        if not b or not views or not lines:
            return 1.0
        cr = lines / views
        return 1.0 + truth["card_gain"] * max(0.0, b.median_cr / cr - 1) * CARD_RAMP[min(d, 6)]

    def mult(pid: str, d: int, day: date) -> Tuple[float, float]:
        """(множитель спроса, доля скидки в этот день)"""
        p, m, disc = products[pid], 1.0, 0.0
        for pr in farm.promos:
            if pr.product_id == pid and pr.start <= day <= pr.end:
                disc = max(disc, pr.discount)
        if disc:
            m *= truth["promo_lift"].get(pid) or (1 + truth["promo_k"] * disc)
        ref = profile.price_ref[pid]
        if ref and abs(p.price / ref - 1) > 0.01:
            m *= (p.price / ref) ** (-truth["elasticity"])
        b = bench.get(p.category)
        if b:
            m *= 1 + truth["season"] * b.demand_growth
        m *= card_boost(pid, d)
        m *= 1 + hygiene_gain.get(pid, 0.0) * CARD_RAMP[min(d, 6)]
        return m, disc

    bundle_pairs = [tuple(x) for x in state.get("bundles", [])]
    offers = state.get("offers", [])
    returning: Dict[int, List[Tuple[str, float]]] = {}
    for offer in offers:
        for cust in offer.get("segment", []):
            r = _rng(seed, "return", cust)
            if r.random() < truth["return_prob"]:
                returning.setdefault(r.randrange(days), []).append((cust, offer["discount"]))

    # оптовые покупки: когда и сколько заберут, задано случайными числами недели и не зависит от розницы
    wholesale_days: Dict[int, List[Tuple[str, float, float]]] = {}
    for i, offer in enumerate(state.get("wholesale", [])):
        r = _rng(seed, "wholesale", i)
        units = min(offer["qty"], offer["qty"] * truth["wholesale_take"] * r.uniform(0.6, 1.4))
        wholesale_days.setdefault(r.randrange(days), []).append((offer["product_id"], units, offer["discount"]))

    order_no = 0
    for d in range(days):
        day = t0 + timedelta(days=d)
        for pid, (arrive, qty) in list(pending.items()):
            if arrive <= d:
                shelf = products[pid].shelf_life_days
                stock[pid].append([qty, None if shelf >= 180 else day + timedelta(days=int(shelf * 0.7))])
                del pending[pid]
        for pid in products:
            keep = []
            for qty, exp in stock[pid]:
                if exp is not None and exp <= day:
                    sim.products[pid].writeoff += qty
                else:
                    keep.append([qty, exp])
            stock[pid] = sorted(keep, key=lambda b: (b[1] is None, b[1] or date.max))
            horizon = day + timedelta(days=farm.lead_days)  # остаток, который переживёт срок поставки
            usable = sum(b[0] for b in stock[pid] if b[1] is None or b[1] > horizon)
            if pid not in pending and usable < farm.lead_days * rates[pid]:  # заказывает, когда остатка хватает лишь на срок поставки
                pending[pid] = (d + farm.lead_days, rates[pid] * REORDER_TARGET_DAYS)

        for pid, units, disc in wholesale_days.get(d, []):
            p, res, got = products[pid], sim.products[pid], 0.0
            for b in stock[pid]:
                take = min(b[0], units - got)
                b[0] -= take
                got += take
                if got >= units - 1e-9:
                    break
            if got > 0:
                price = round(p.price * (1 - disc), 2)
                res.units += got
                res.wholesale += got
                res.revenue += got * price
                res.margin += got * (price - p.cost)
                order_no += 1
                sim.orders.append(Order(f"sim-{order_no}", day, f"b2b-{day.isoformat()}-{pid}", [OrderLine(pid, round(got, 2), price)]))

        weekend = 1.3 if day.weekday() >= 5 else 1.0
        buyers: List[Tuple[str, float]] = []
        for cust, typical in profile.regulars:
            if _rng(seed, "reg", day, cust).random() < 1 / typical:
                buyers.append((cust, 0.0))
        buyers += returning.get(d, [])
        for i in range(_poisson(_rng(seed, "oneoff", day), profile.lam_oneoff * weekend / 1.0857)):
            buyers.append((f"sim-{day.isoformat()}-{i}", 0.0))
        _rng(seed, "shuffle", day).shuffle(buyers)

        for cust, offer_disc in buyers:
            lines: List[OrderLine] = []
            chosen = []
            for pid in products:
                m, _ = mult(pid, d, day)
                if _rng(seed, "inc", day, cust, pid).random() < min(MAX_INCLUDE, profile.incl[pid] * m):
                    chosen.append(pid)
            if profile.pair and profile.pair[0] in chosen and profile.pair[1] not in chosen:
                a, b, p_b_a = profile.pair
                boost = max(0.0, (p_b_a - profile.incl[b]) / (1 - profile.incl[b]))
                if (a, b) in bundle_pairs or (b, a) in bundle_pairs:
                    boost += truth["bundle_uptake"]
                if _rng(seed, "pair", day, cust).random() < boost:
                    chosen.append(b)
            if not chosen:
                continue
            for pid in chosen:
                p = products[pid]
                _, disc = mult(pid, d, day)
                r = _rng(seed, "qty", day, cust, pid)
                if p.unit == "шт":
                    want = max(1, round(r.gauss(profile.qty[pid], 0.7)))
                else:
                    want = max(0.3, round(r.gauss(profile.qty[pid], profile.qty[pid] * 0.3), 1))
                price = round(p.price * (1 - max(disc, offer_disc)), 2)
                res = sim.products[pid]
                got = 0.0
                for b in stock[pid]:
                    take = min(b[0], want - got)
                    b[0] -= take
                    got += take
                    if got >= want - 1e-9:
                        break
                if got < want - 1e-9:
                    miss = want - got
                    if pid in preorders and _rng(seed, "pre", day, cust, pid).random() < truth["preorder_capture"]:
                        res.preorder += miss
                        got += miss
                    else:
                        res.lost += miss
                if got > 0:
                    lines.append(OrderLine(pid, round(got, 2), price))
                    res.units += got
                    res.lines += 1
                    res.revenue += got * price
                    res.margin += got * (price - p.cost)
            if lines:
                order_no += 1
                sim.orders.append(Order(f"sim-{order_no}", day, cust, lines))
        for pid in products:
            sim.views.setdefault(pid, {})[day] = max(0, round(profile.views_pd[pid] * _rng(seed, "views", day, pid).gauss(1, 0.1)))

    for cat in state.get("new_categories", []):
        b = bench.get(cat)
        if b:
            sim.extra_revenue[cat] = (b.avg_farm_weekly_rev * b.demand_growth * truth["newcat_capture"] * days / 7
                                      * _rng(seed, "newcat", cat).uniform(0.7, 1.3))
    sim.batches = [Batch(pid, round(b[0], 2), b[1]) for pid, bs in stock.items() for b in bs if b[0] > 0.01]
    return sim


def advance(farm: Farm, sim: Sim) -> None:
    """Применяет прошедшую неделю к ферме: заказы, просмотры, остатки, окончание акций и предзаказов."""
    farm.add_orders(sim.orders)
    for pid, days in sim.views.items():
        farm.views.setdefault(pid, {}).update(days)
    farm.batches = sim.batches
    farm.today += timedelta(days=sim.days)
    state = farm.state
    state["preorders"] = {k: v for k, v in state.get("preorders", {}).items() if date.fromisoformat(v) >= farm.today}
    state.pop("restock_plans", None)
    for key in ("preorders",):
        if not state.get(key):
            state.pop(key, None)
    if state.pop("hygiene_in_progress", False):  # фермер за неделю заполнил карточки и ответил на отзывы
        for p in farm.products:
            p.has_photo = p.has_weight = p.has_expiry = True
        for r in farm.reviews:
            r.answered = True
