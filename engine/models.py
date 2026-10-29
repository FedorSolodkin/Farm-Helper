"""Модели данных. Клиенты хранятся только как хэш (см. privacy.py)."""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional


@dataclass
class Product:
    id: str
    name: str
    category: str
    unit: str
    price: float
    cost: float
    shelf_life_days: int
    has_photo: bool = True
    has_weight: bool = True
    has_expiry: bool = True
    last_price_change: Optional[date] = None
    seasonal: bool = False  # урожай раз в год: продать нужно в сезон
    cost_prev: Optional[float] = None  # себестоимость до последнего изменения
    cost_changed: Optional[date] = None


@dataclass
class Batch:
    product_id: str
    qty: float
    expires: Optional[date]  # None — срок годности не ограничивает (мёд, крупы)


@dataclass
class OrderLine:
    product_id: str
    qty: float
    unit_price: float


@dataclass
class Order:
    id: str
    day: date
    customer: str  # хэш, не исходный ID
    lines: List[OrderLine]


@dataclass
class Promo:
    id: str
    product_id: str
    start: date
    end: date
    discount: float


@dataclass
class Review:
    product_id: str
    day: date
    rating: int
    answered: bool


@dataclass
class Farm:
    id: str
    name: str
    owner: str
    today: date
    products: List[Product]
    orders: List[Order]
    views: Dict[str, Dict[date, int]]
    promos: List[Promo]
    reviews: List[Review]
    batches: List[Batch] = field(default_factory=list)
    lead_days: int = 4  # за сколько дней фермер обычно пополняет остаток
    state: dict = field(default_factory=dict)  # флаги платформы: предзаказы, наборы, рассылки; меняет только Executor

    def __post_init__(self) -> None:
        self._by_id = {p.id: p for p in self.products}
        # product_id -> day -> [units, revenue, lines]
        self._sales: Dict[str, Dict[date, list]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
        for order in self.orders:
            for line in order.lines:
                cell = self._sales[line.product_id][order.day]
                cell[0] += line.qty
                cell[1] += line.qty * line.unit_price
                cell[2] += 1

    def fork(self) -> "Farm":
        """Копия состояния платформы для «что было бы, если…»; история заказов общая и только читается."""
        f = copy.copy(self)
        f.products = copy.deepcopy(self.products)
        f.promos = copy.deepcopy(self.promos)
        f.batches = copy.deepcopy(self.batches)
        f.state = copy.deepcopy(self.state)
        f._by_id = {p.id: p for p in f.products}
        return f

    def add_orders(self, orders: List[Order]) -> None:
        self.orders.extend(orders)
        for order in orders:
            for line in order.lines:
                cell = self._sales[line.product_id][order.day]
                cell[0] += line.qty
                cell[1] += line.qty * line.unit_price
                cell[2] += 1

    # --- справочники ---
    def product(self, pid: str) -> Product:
        return self._by_id[pid]

    def batches_of(self, pid: str) -> List[Batch]:
        """Партии по правилу FEFO: сначала те, что раньше истекают."""
        found = [b for b in self.batches if b.product_id == pid and b.qty > 0]
        return sorted(found, key=lambda b: (b.expires is None, b.expires or date.max))

    def stock(self, pid: str) -> float:
        return sum(b.qty for b in self.batches_of(pid))

    def active_promo(self, pid: str) -> Optional[Promo]:
        for promo in self.promos:
            if promo.product_id == pid and promo.start <= self.today <= promo.end:
                return promo
        return None

    # --- агрегаты по периоду [d0, d1] включительно ---
    def _sum(self, pid: str, d0: date, d1: date, idx: int) -> float:
        total = 0.0
        day = d0
        cells = self._sales.get(pid, {})
        while day <= d1:
            cell = cells.get(day)
            if cell:
                total += cell[idx]
            day += timedelta(days=1)
        return total

    def units(self, pid: str, d0: date, d1: date) -> float:
        return self._sum(pid, d0, d1, 0)

    def revenue(self, pid: str, d0: date, d1: date) -> float:
        return self._sum(pid, d0, d1, 1)

    def lines(self, pid: str, d0: date, d1: date) -> int:
        return int(self._sum(pid, d0, d1, 2))

    def view_count(self, pid: str, d0: date, d1: date) -> int:
        days = self.views.get(pid, {})
        return sum(v for d, v in days.items() if d0 <= d <= d1)

    def ago(self, n: int) -> date:
        return self.today - timedelta(days=n)

    def rate(self, pid: str, days: int = 14) -> float:
        """Средние продажи в день за последние `days` дней (без сегодняшнего)."""
        return self.units(pid, self.ago(days), self.ago(1)) / days

    def weekly_revenue(self, pid: str, days: int = 28) -> float:
        return self.revenue(pid, self.ago(days), self.ago(1)) / days * 7
