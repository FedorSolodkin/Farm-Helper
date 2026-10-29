"""Доставка: открытые заказы на карте, ранжирование по срочности и план рейсов.

Идея из интервью: «фермер — не курьер, но возит сам». Помощник собирает заказы одного района в один рейс там, где
покупатель готов подождать, и показывает разницу с тем, как возят сейчас (по рейсу на каждый день пожеланий).

Данные синтетические и детерминированные (зависят от фермы и даты). В пилоте заказы и координаты приходят от платформы:
помощник получает клиента только хэшем, а точку на карте округлённой до района, точный адрес остаётся у платформы.
Расчёт чисто арифметический, ИИ в нём не участвует; страница ничего не отправляет клиентам.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta
from typing import Dict, List, Tuple

from .models import Farm
from .privacy import pseudonymize

KM_PER_UNIT = 0.45  # одна клетка схемы в километрах
ROAD_FACTOR = 1.3  # дороги длиннее прямой
SPEED_KMH = 42
STOP_MIN = 10  # передать заказ
LOAD_MIN = 30  # погрузка перед рейсом
START_HOUR = 10
COST_PER_KM = 14  # бензин и износ, ₽ за км (допущение, меняется в пилоте)
PERISHABLE_DAYS = 7
MAX_TRIP_MIN = 240  # рейс дольше четырёх часов не склеиваем
MAX_STOPS = 6
MIN_GAIN_KM = 2  # склеиваем, только если экономим хотя бы столько километров

FARM_POINT = (48.0, 40.0)
MAP_SIZE = (100, 72)
# район: (название, центр x, центр y, разброс)
DISTRICTS = [
    ("Центр города", 74, 20, 5.0),
    ("Заречный", 86, 44, 5.5),
    ("Северный", 40, 9, 5.0),
    ("Лесной", 17, 22, 5.5),
    ("Посёлок Ключи", 20, 56, 4.5),
    ("Берёзки", 56, 62, 4.5),
]
PRIORITY_LABEL = {"urgent": "Срочно", "soon": "На этой неделе", "later": "Можно позже"}


def _rng(farm: Farm) -> random.Random:
    seed = hashlib.sha256(f"delivery:{farm.id}:{farm.today.isoformat()}".encode("utf-8")).hexdigest()
    return random.Random(int(seed[:12], 16))


def _km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) * KM_PER_UNIT * ROAD_FACTOR


def _route_len(points: List[Tuple[float, float]]) -> float:
    path = [FARM_POINT] + points + [FARM_POINT]
    return sum(_km(path[i], path[i + 1]) for i in range(len(path) - 1))


def _order_stops(stops: List[dict]) -> List[dict]:
    """Ближайший сосед от фермы, затем 2-opt: для десятка точек этого хватает, чтобы маршрут не путался."""
    left, route, cur = list(stops), [], FARM_POINT
    while left:
        nxt = min(left, key=lambda s: _km(cur, (s["x"], s["y"])))
        left.remove(nxt)
        route.append(nxt)
        cur = (nxt["x"], nxt["y"])
    pts = lambda r: [(s["x"], s["y"]) for s in r]
    improved = len(route) > 3
    while improved:
        improved = False
        for i in range(len(route) - 1):
            for j in range(i + 1, len(route)):
                cand = route[:i] + route[i:j + 1][::-1] + route[j + 1:]
                if _route_len(pts(cand)) + 1e-9 < _route_len(pts(route)):
                    route, improved = cand, True
    return route


def _trip(day: date, stops: List[dict]) -> dict:
    route = _order_stops(stops)
    cur, clock, km = FARM_POINT, START_HOUR * 60 + LOAD_MIN, 0.0
    out = []
    for s in route:
        leg = _km(cur, (s["x"], s["y"]))
        km += leg
        clock += leg / SPEED_KMH * 60
        out.append({"order_id": s["id"], "district": s["district"], "x": s["x"], "y": s["y"], "leg_km": round(leg, 1),
                    "eta": f"{int(clock // 60):02d}:{int(clock % 60):02d}", "sum": s["sum"]})
        clock += STOP_MIN
        cur = (s["x"], s["y"])
    back = _km(cur, FARM_POINT)
    km += back
    clock += back / SPEED_KMH * 60
    minutes = round(clock - START_HOUR * 60)
    return {"date": day.isoformat(), "stops": out, "km": round(km, 1), "minutes": minutes,
            "back": f"{int(clock // 60):02d}:{int(clock % 60):02d}", "sum": round(sum(s["sum"] for s in stops)),
            "districts": sorted({s["district"] for s in stops})}


def _summary(trips: List[dict]) -> dict:
    km = sum(t["km"] for t in trips)
    return {"trips": len(trips), "days": len({t["date"] for t in trips}), "km": round(km, 1), "minutes": sum(t["minutes"] for t in trips), "cost": round(km * COST_PER_KM)}


def make_orders(farm: Farm) -> List[dict]:
    """Открытые заказы на доставку: у каждого район, точка, желаемый день и сколько дней покупатель готов подождать."""
    rng = _rng(farm)
    foods = [p for p in farm.products if farm.stock(p.id) > 0] or farm.products
    n = rng.randint(15, 18)
    orders = []
    for i in range(n):
        name, cx, cy, spread = rng.choices(DISTRICTS, weights=[3, 2, 2, 2, 2, 1.5])[0]
        picked = rng.sample(foods, k=min(len(foods), rng.randint(1, 3)))
        lines, total, perishable = [], 0.0, False
        for p in picked:
            if p.unit in ("кг", "л"):
                qty = float(rng.choice([5, 8, 10, 15]) if p.price < 300 else rng.choice([1, 2, 3, 5]))  # овощи берут ящиками
            else:
                qty = float(rng.choice([2, 3, 4, 6]))
            lines.append(f"{p.name} {qty:g} {p.unit}")
            total += qty * p.price
            perishable = perishable or p.shelf_life_days <= PERISHABLE_DAYS
        wish_in = rng.choices(range(0, 7), weights=[2, 3, 3, 2, 2, 1, 1])[0]
        flex = 0 if perishable and rng.random() < 0.7 else rng.choice([0, 1, 2, 2, 3])
        if perishable:
            flex = min(flex, 1)
        wish = farm.today + timedelta(days=wish_in)
        code = f"Д-{1040 + i}"
        orders.append({
            "id": code,
            "customer": "Клиент " + pseudonymize(f"{farm.id}:{i}")[:4],
            "district": name,
            "x": round(min(MAP_SIZE[0] - 3, max(3, rng.gauss(cx, spread))), 1),
            "y": round(min(MAP_SIZE[1] - 3, max(3, rng.gauss(cy, spread * 0.8))), 1),
            "items": lines,
            "sum": round(total),
            "perishable": perishable,
            "wish": wish,
            "deadline": wish + timedelta(days=flex),
            "flex": flex,
        })
    return orders


def _priority(o: dict, today: date) -> str:
    days = (o["deadline"] - today).days
    if days <= 1 or (o["perishable"] and days <= 2):
        return "urgent"
    return "soon" if days <= 4 else "later"


def _plan(orders: List[dict], today: date) -> List[dict]:
    """Меньше рейсов и километров: в районе один рейс на всех, кто готов ждать; затем склеиваем рейсы, если это экономит путь
    и рейс остаётся посильным по времени."""
    groups = []
    for district in sorted({o["district"] for o in orders}):
        left = sorted((o for o in orders if o["district"] == district), key=lambda o: o["deadline"])
        while left:
            day = left[0]["deadline"]
            take = [o for o in left if o["wish"] <= day]
            groups.append({"orders": take, "lo": max(o["wish"] for o in take), "hi": min(o["deadline"] for o in take)})
            left = [o for o in left if o not in take]
    for g in groups:
        g["trip"] = _trip(g["lo"], g["orders"])
    while True:
        best = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                lo, hi = max(a["lo"], b["lo"]), min(a["hi"], b["hi"])
                if lo > hi:
                    continue
                merged = _trip(lo, a["orders"] + b["orders"])
                gain = a["trip"]["km"] + b["trip"]["km"] - merged["km"]
                if merged["minutes"] <= MAX_TRIP_MIN and len(merged["stops"]) <= MAX_STOPS and (gain > MIN_GAIN_KM or (a["trip"]["date"] == b["trip"]["date"] and gain > 0)) and (best is None or gain > best[0]):
                    best = (gain, i, j, {"orders": a["orders"] + b["orders"], "lo": lo, "hi": hi, "trip": merged})
        if not best:
            break
        _, i, j, g = best
        groups = [x for k, x in enumerate(groups) if k not in (i, j)] + [g]
    return sorted((g["trip"] for g in groups), key=lambda t: t["date"])


def _as_is(orders: List[dict]) -> List[dict]:
    by_day: Dict[date, List[dict]] = {}
    for o in orders:
        by_day.setdefault(o["wish"], []).append(o)
    return [_trip(d, os_) for d, os_ in sorted(by_day.items())]


def build(farm: Farm) -> dict:
    orders = make_orders(farm)
    plan = _plan(orders, farm.today)
    as_is = _as_is(orders)
    planned_day = {s["order_id"]: t["date"] for t in plan for s in t["stops"]}
    plan_stop = {s["order_id"]: (ti, si + 1) for ti, t in enumerate(plan) for si, s in enumerate(t["stops"])}
    ranked = sorted(orders, key=lambda o: (o["deadline"], o["wish"], -o["sum"]))
    out = []
    for rank, o in enumerate(ranked, 1):
        pr = _priority(o, farm.today)
        out.append({
            "rank": rank, "id": o["id"], "customer": o["customer"], "district": o["district"], "x": o["x"], "y": o["y"],
            "items": o["items"], "sum": o["sum"], "perishable": o["perishable"],
            "wish": o["wish"].isoformat(), "deadline": o["deadline"].isoformat(), "flex": o["flex"],
            "priority": pr, "priority_label": PRIORITY_LABEL[pr],
            "planned": planned_day[o["id"]], "trip": plan_stop[o["id"]][0], "stop": plan_stop[o["id"]][1],
            "shifted": planned_day[o["id"]] != o["wish"].isoformat(),
        })
    a, b = _summary(as_is), _summary(plan)
    return {
        "date": farm.today.isoformat(),
        "farm": {"x": FARM_POINT[0], "y": FARM_POINT[1], "name": farm.name},
        "map": {"w": MAP_SIZE[0], "h": MAP_SIZE[1],
                "districts": [{"name": n, "x": x, "y": y, "r": round(s * 2.4, 1)} for n, x, y, s in DISTRICTS]},
        "orders": out,
        "plan": plan, "as_is": as_is,
        "summary": {
            "orders": len(out), "sum": sum(o["sum"] for o in out),
            "urgent": sum(1 for o in out if o["priority"] == "urgent"),
            "as_is": a, "plan": b,
            "saved_km": round(a["km"] - b["km"], 1), "saved_minutes": a["minutes"] - b["minutes"],
            "share_as_is": round(a["cost"] / max(1, sum(o["sum"] for o in out)) * 100), "share_plan": round(b["cost"] / max(1, sum(o["sum"] for o in out)) * 100),
            "saved_cost": a["cost"] - b["cost"], "saved_days": a["days"] - b["days"],
            "shifted": sum(1 for o in out if o["shifted"]),
        },
        "assumptions": f"Скорость {SPEED_KMH} км/ч, {STOP_MIN} мин на передачу заказа, {LOAD_MIN} мин на погрузку, {COST_PER_KM} ₽ за км. "
                       "Заказы синтетические. Экономия считается относительно развозки в тот день, на который просят каждый заказ.",
    }
