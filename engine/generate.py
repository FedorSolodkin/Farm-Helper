"""Генератор синтетических ферм с заложенными проблемами (ответ известен заранее).

Три персоны, 90 дней истории, фиксированный seed. Заложено:
  cheese — риск списания творога, заканчивающаяся моцарелла (и подорожавшая себестоимость),  «дырявая» Качотта, 12 ушедших клиентов,
           акция на брынзу в минус, пара «Качотта + мёд», карточки без фото/срока, неотвеченные отзывы
  veg    — риск списания тепличных томатов, нет тыквы в сезон, ушедшие клиенты, урожай брокколи, который не уйдёт в розницу
  honey  — липовый мёд дороже рынка (продажи упали), гречишный на исходе при росте спроса, ушедшие клиенты
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Dict, List, Optional

from .benchmarks import BENCHMARKS
from .models import Batch, Farm, Order, OrderLine, Product, Promo, Review
from .privacy import pseudonymize

TODAY = date(2026, 10, 26)
HISTORY_DAYS = 90


def P(id, name, cat, unit, price, cost, shelf, pop, qty, **extra):
    return dict(id=id, name=name, cat=cat, unit=unit, price=price, cost=cost, shelf=shelf, pop=pop, qty=qty, **extra)


SPECS = {
    "cheese": dict(
        seed=11, name="Сыроварня «Зелёный луг»", owner="Анна", lam=8.0, active=28, churned=12,
        products=[
            P("kachotta", "Качотта", "Сыры", "кг", 1450, 0.58, 60, 0.12, 0.5, cr=0.015),
            P("mozzarella", "Моцарелла", "Сыры", "кг", 980, 0.60, 14, 0.19, 0.6, cost_change=(21, 0.45)),
            P("suluguni", "Сулугуни", "Сыры", "кг", 890, 0.60, 21, 0.09, 0.6),
            P("brynza", "Брынза", "Сыры", "кг", 780, 0.60, 30, 0.14, 0.8),
            P("adygei", "Адыгейский", "Сыры", "кг", 640, 0.60, 10, 0.06, 0.5, weight=False),
            P("tvorog", "Творог 9%", "Молочное", "кг", 600, 0.55, 7, 0.24, 1.5),
            P("yogurt", "Йогурт", "Молочное", "шт", 95, 0.55, 10, 0.09, 2),
            P("smetana", "Сметана", "Молочное", "шт", 160, 0.55, 10, 0.06, 1),
            P("kefir", "Кефир", "Молочное", "л", 140, 0.55, 7, 0.05, 1),
            P("maslo", "Масло сливочное", "Молочное", "кг", 1100, 0.60, 30, 0.04, 0.4, expiry=False),
            P("honey", "Мёд цветочный", "Мёд", "кг", 850, 0.60, 365, 0.09, 0.5, photo=False),
        ],
        past_promos=[("tvorog", 78, 2.3), ("suluguni", 60, 2.1), ("yogurt", 42, 2.4), ("tvorog", 24, 2.3)],
        active_promo=("brynza", 10, 6, 0.25, 1.0),  # продукт, начало (дней назад), осталось дней, скидка, подъём
        bundle=("kachotta", "honey", 0.65),
        stock={"tvorog": ("expire", 34, 3), "mozzarella": ("cover", 2.0)},
        unanswered=[("mozzarella", 6, 2), ("tvorog", 3, 4)],
    ),
    "veg": dict(
        seed=23, name="Огород Петровых", owner="Игорь", lam=12.0, active=26, churned=7,
        products=[
            P("potato", "Картофель", "Овощи", "кг", 45, 0.60, 120, 0.20, 5),
            P("carrot", "Морковь", "Овощи", "кг", 55, 0.60, 90, 0.13, 2),
            P("beet", "Свёкла", "Овощи", "кг", 50, 0.60, 90, 0.08, 2),
            P("cabbage", "Капуста белая", "Овощи", "кг", 40, 0.60, 60, 0.12, 3),
            P("onion", "Лук репчатый", "Овощи", "кг", 40, 0.60, 120, 0.10, 2),
            P("tomato", "Томаты тепличные", "Овощи", "кг", 220, 0.55, 6, 0.16, 1.8),
            P("cucumber", "Огурцы", "Овощи", "кг", 160, 0.55, 6, 0.10, 1.5),
            P("garlic", "Чеснок", "Овощи", "кг", 380, 0.55, 150, 0.05, 0.3, photo=False),
            P("zucchini", "Кабачки", "Овощи", "кг", 70, 0.60, 20, 0.08, 2),
            P("broccoli", "Брокколи", "Овощи", "кг", 300, 0.50, 12, 0.05, 0.8, seasonal=True),
        ],
        past_promos=[("cabbage", 70, 2.1), ("onion", 50, 2.2), ("tomato", 30, 2.2)],
        active_promo=None,
        bundle=None,
        stock={"tomato": ("expire", 45, 3), "broccoli": ("expire", 90, 12)},
        unanswered=[("cucumber", 5, 3)],
    ),
    "honey": dict(
        seed=37, name="Пасека у реки", owner="Мария", lam=5.0, active=22, churned=5,
        products=[
            P("lipa", "Мёд липовый", "Мёд", "кг", 1250, 0.48, 365, 0.30, 0.6,
              price_change=(14, 1000, 0.55)),  # цену подняли 14 дней назад, продажи упали
            P("grech", "Мёд гречишный", "Мёд", "кг", 900, 0.55, 365, 0.22, 0.6),
            P("raznotr", "Мёд разнотравье", "Мёд", "кг", 750, 0.55, 365, 0.20, 0.6),
            P("sot", "Сотовый мёд", "Мёд", "кг", 1400, 0.55, 365, 0.10, 0.4),
            P("propolis", "Настойка прополиса", "Продукты пчеловодства", "шт", 350, 0.50, 365, 0.08, 1),
            P("perga", "Перга", "Продукты пчеловодства", "кг", 1800, 0.50, 365, 0.05, 0.1),
        ],
        past_promos=[("raznotr", 60, 2.1), ("propolis", 40, 2.3)],
        active_promo=None,
        bundle=None,
        stock={"grech": ("cover", 5.0)},
        unanswered=[],
    ),
}

OWNERS = {k: v["owner"] for k, v in SPECS.items()}


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _pick(rng: random.Random, items: list, weights: List[float], k: int) -> list:
    pool = list(zip(items, weights))
    chosen = []
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        x = rng.random() * total
        for i, (item, w) in enumerate(pool):
            x -= w
            if x <= 0:
                chosen.append(item)
                pool.pop(i)
                break
    return chosen


def build_farm(key: str, today: date = TODAY, seed: Optional[int] = None) -> Farm:
    spec = SPECS[key]
    rng = random.Random(spec["seed"] if seed is None else seed)
    ps: Dict[str, dict] = {p["id"]: p for p in spec["products"]}

    # --- акции: прошлые и текущая ---
    promos: List[Promo] = []
    lift_of: Dict[str, float] = {}
    for n, (pid, start_ago, lift) in enumerate(spec["past_promos"]):
        start = today - timedelta(days=start_ago)
        promos.append(Promo(f"promo-{n}", pid, start, start + timedelta(days=3), 0.20))
        lift_of[f"promo-{n}"] = lift
    if spec["active_promo"]:
        pid, start_ago, left, discount, lift = spec["active_promo"]
        promos.append(Promo("promo-active", pid, today - timedelta(days=start_ago), today + timedelta(days=left), discount))
        lift_of["promo-active"] = lift

    total_pop = sum(p["pop"] for p in spec["products"])

    def weight_mult(pid: str, lift: float) -> float:
        """Подъём продаж размывается тем, что число позиций в заказе фиксировано: компенсируем."""
        share = ps[pid]["pop"] / total_pop
        return lift * (1 - share) / (1 - lift * share) if lift * share < 0.9 else lift * 3

    def promo_on(pid: str, day: date) -> Optional[Promo]:
        for pr in promos:
            if pr.product_id == pid and pr.start <= day <= pr.end:
                return pr
        return None

    def list_price(p: dict, day: date) -> float:
        change = p.get("price_change")
        if change and day < today - timedelta(days=change[0]):
            return change[1]
        return p["price"]

    def demand_mult(p: dict, day: date) -> float:
        mult = 1.0
        pr = promo_on(p["id"], day)
        pid_ = p["id"]
        if pr:
            mult *= weight_mult(pid_, lift_of[pr.id])
        change = p.get("price_change")
        if change and day >= today - timedelta(days=change[0]):
            mult *= change[2]
        return mult

    # --- клиенты ---
    regulars = []
    for i in range(spec["active"] + spec["churned"]):
        interval = rng.randint(7, 12)
        stop = None
        if i >= spec["active"]:
            stop = today - timedelta(days=rng.randint(26, 35))
        regulars.append(dict(
            id=pseudonymize(f"{key}-regular-{i}"), interval=interval, stop=stop,
            next=today - timedelta(days=HISTORY_DAYS) + timedelta(days=rng.randint(0, interval)),
        ))

    # --- заказы ---
    orders: List[Order] = []
    one_off = 0
    bundle = spec["bundle"]
    for offset in range(HISTORY_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        buyers = []
        for r in regulars:
            if r["stop"] and day > r["stop"]:
                continue
            if day >= r["next"]:
                buyers.append(r["id"])
                r["next"] = day + timedelta(days=r["interval"] + rng.randint(-1, 1))
        weekend = 1.3 if day.weekday() >= 5 else 1.0
        for _ in range(_poisson(rng, spec["lam"] * weekend)):
            buyers.append(pseudonymize(f"{key}-oneoff-{one_off}"))
            one_off += 1
        for customer in buyers:
            weights = [p["pop"] * demand_mult(p, day) for p in ps.values()]
            chosen = _pick(rng, list(ps.values()), weights, rng.choice([1, 2, 2, 3]))
            if bundle and any(c["id"] == bundle[0] for c in chosen) and not any(c["id"] == bundle[1] for c in chosen):
                if rng.random() < bundle[2]:
                    chosen.append(ps[bundle[1]])
            lines = []
            for p in chosen:
                if p["unit"] == "шт":
                    qty = max(1, round(rng.gauss(p["qty"], 0.7)))
                else:
                    qty = max(0.3, round(rng.gauss(p["qty"], p["qty"] * 0.3), 1))
                price = list_price(p, day)
                pr = promo_on(p["id"], day)
                if pr:
                    price = price * (1 - pr.discount)
                lines.append(OrderLine(p["id"], qty, round(price, 2)))
            orders.append(Order(f"o-{len(orders)}", day, customer, lines))

    # --- просмотры: конверсия категории (или заложенная) × шум ---
    base_lines: Dict[str, int] = {pid: 0 for pid in ps}
    warm = today - timedelta(days=HISTORY_DAYS - 60)
    for o in orders:
        if o.day <= warm:
            for ln in o.lines:
                base_lines[ln.product_id] += 1
    views: Dict[str, Dict[date, int]] = {}
    for pid, p in ps.items():
        cr = p.get("cr") or BENCHMARKS[p["cat"]].median_cr * rng.uniform(0.85, 1.2)
        per_day = base_lines[pid] / 60 / cr
        views[pid] = {
            today - timedelta(days=o): max(0, round(per_day * rng.gauss(1, 0.12)))
            for o in range(HISTORY_DAYS, 0, -1)
        }

    # --- отзывы ---
    reviews: List[Review] = []
    for _ in range(30):
        pid = rng.choice(list(ps))
        rating = rng.choices([5, 4, 3, 2], [0.6, 0.25, 0.1, 0.05])[0]
        reviews.append(Review(pid, today - timedelta(days=rng.randint(12, HISTORY_DAYS)), rating, True))
    for pid, days_ago, rating in spec["unanswered"]:
        reviews.append(Review(pid, today - timedelta(days=days_ago), rating, False))

    products = []
    for p in spec["products"]:
        change = p.get("price_change")
        products.append(Product(
            id=p["id"], name=p["name"], category=p["cat"], unit=p["unit"], price=p["price"],
            cost=round(p["price"] * p["cost"], 2), shelf_life_days=p["shelf"],
            has_photo=p.get("photo", True), has_weight=p.get("weight", True), has_expiry=p.get("expiry", True),
            last_price_change=(today - timedelta(days=change[0])) if change else today - timedelta(days=40),
            seasonal=p.get("seasonal", False),
            cost_prev=round(p["price"] * p["cost_change"][1], 2) if p.get("cost_change") else None,
            cost_changed=(today - timedelta(days=p["cost_change"][0])) if p.get("cost_change") else None,
        ))

    farm = Farm(
        id=f"farm-{key}", name=spec["name"], owner=spec["owner"], today=today,
        products=products, orders=orders, views=views, promos=promos, reviews=reviews,
    )

    # --- остатки считаем по реальным продажам, чтобы проблемы получились правдоподобными ---
    for p in products:
        rate = max(farm.rate(p.id), 0.05)
        plan = spec["stock"].get(p.id)
        if plan and plan[0] == "expire":
            farm.batches.append(Batch(p.id, plan[1], today + timedelta(days=plan[2])))
            continue
        cover = plan[1] if plan else rng.uniform(10, 20)
        expires = None if p.shelf_life_days >= 180 else today + timedelta(days=int(cover * 2 + 6))
        farm.batches.append(Batch(p.id, round(rate * cover, 1), expires))
    return farm
