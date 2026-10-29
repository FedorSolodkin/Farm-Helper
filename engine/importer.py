"""Загрузка данных фермера по шаблону: CSV-файлы → проверка → предприятие для помощника.

Шаблонов пять: товары, остатки и продажи обязательны, просмотры карточек и акции — по желанию.
Файл читается устойчиво (кодировка UTF-8 с BOM, разделитель «;», «,» или табуляция, десятичная запятая, даты ДД.ММ.ГГГГ),
а ошибки возвращаются списком с номерами строк на языке фермера. Коды клиентов сразу заменяются необратимым хэшем."""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .benchmarks import BENCHMARKS
from .models import Batch, Farm, Order, OrderLine, Product, Promo
from .privacy import pseudonymize, usable_benchmarks

MAX_FILE_CHARS = 4_000_000
MAX_PRODUCTS = 300
MAX_SALES_ROWS = 120_000
MAX_ERRORS_SHOWN = 40
MIN_HISTORY_DAYS = 14
OTHER = "Другое"
CUSTOM_ID = "mine"


def categories() -> List[str]:
    return sorted(usable_benchmarks(BENCHMARKS)) + [OTHER]


# (ключ, заголовок в шаблоне, синонимы, обязателен, пример, подсказка)
SPEC: Dict[str, dict] = {
    "products": {
        "title": "Товары", "required": True, "file": "tovary.csv",
        "columns": [
            ("sku", "артикул", ["sku", "id", "код", "артикул"], True, "СЫР-1", "любой уникальный код товара"),
            ("name", "название", ["name", "товар", "название"], True, "Сыр «Качотта»", "как товар называется в каталоге"),
            ("category", "категория", ["category", "категория"], False, "Сыры", "одна из: " + ", ".join(categories())),
            ("unit", "единица", ["unit", "ед", "ед_изм", "единица"], False, "кг", "кг, шт, л…"),
            ("price", "цена", ["price", "цена"], True, "1450", "розничная цена за единицу, ₽"),
            ("cost", "себестоимость", ["cost", "себестоимость"], True, "840", "себестоимость единицы, ₽"),
            ("shelf_life_days", "срок_годности_дней", ["shelf_life_days", "срок_годности_дней", "срок_годности", "срок"], False, "60", "на сколько дней товар годен после изготовления"),
            ("has_photo", "есть_фото", ["has_photo", "есть_фото", "фото"], False, "да", "да/нет: есть ли фото в карточке"),
            ("has_weight", "есть_вес", ["has_weight", "есть_вес", "вес_указан"], False, "да", "да/нет: указан ли вес"),
            ("has_expiry", "есть_срок", ["has_expiry", "есть_срок", "срок_указан"], False, "да", "да/нет: указан ли срок годности"),
            ("seasonal", "сезонный", ["seasonal", "сезонный"], False, "нет", "да, если урожай бывает раз в год"),
            ("cost_prev", "себестоимость_была", ["cost_prev", "себестоимость_была", "прежняя_себестоимость"], False, "700", "прежняя себестоимость, если она выросла (для совета про маржу)"),
            ("cost_changed", "себестоимость_изменена", ["cost_changed", "себестоимость_изменена"], False, "05.10.2026", "дата, когда себестоимость изменилась"),
            ("price_changed", "цена_изменена", ["price_changed", "цена_изменена"], False, "01.09.2026", "дата последнего изменения цены (если не помните, оставьте пустым)"),
        ],
    },
    "stock": {
        "title": "Остатки", "required": True, "file": "ostatki.csv",
        "columns": [
            ("sku", "артикул", ["sku", "id", "код", "артикул"], True, "СЫР-1", "код товара из файла «Товары»"),
            ("qty", "количество", ["qty", "количество", "остаток"], True, "20", "сколько единиц на складе"),
            ("expires", "годен_до", ["expires", "годен_до", "срок_до", "срок_годности_до"], False, "15.11.2026", "дата, до которой годна партия (пусто, если срок не ограничен)"),
        ],
    },
    "sales": {
        "title": "Продажи", "required": True, "file": "prodazhi.csv",
        "columns": [
            ("date", "дата", ["date", "дата"], True, "14.10.2026", "дата продажи"),
            ("customer", "клиент", ["customer", "клиент", "код_клиента", "телефон"], True, "клиент-17", "любой код клиента; сразу заменяется хэшем, имена и телефоны не нужны"),
            ("sku", "артикул", ["sku", "id", "код", "артикул"], True, "СЫР-1", "код товара"),
            ("qty", "количество", ["qty", "количество"], True, "0,8", "сколько куплено"),
            ("price", "цена", ["price", "цена"], True, "1450", "по какой цене продано за единицу, ₽"),
            ("order_id", "заказ", ["order_id", "заказ", "номер_заказа"], False, "1024", "номер заказа (если пусто, строки одного клиента за день считаются одним заказом)"),
        ],
    },
    "views": {
        "title": "Просмотры карточек", "required": False, "file": "prosmotry.csv",
        "columns": [
            ("date", "дата", ["date", "дата"], True, "14.10.2026", "дата"),
            ("sku", "артикул", ["sku", "id", "код", "артикул"], True, "СЫР-1", "код товара"),
            ("views", "просмотры", ["views", "просмотры"], True, "180", "сколько раз открывали карточку за день"),
        ],
    },
    "promos": {
        "title": "Акции", "required": False, "file": "akcii.csv",
        "columns": [
            ("sku", "артикул", ["sku", "id", "код", "артикул"], True, "СЫР-1", "код товара"),
            ("start", "начало", ["start", "начало"], True, "10.10.2026", "первый день акции"),
            ("end", "конец", ["end", "конец"], True, "13.10.2026", "последний день акции"),
            ("discount", "скидка_процент", ["discount", "скидка", "скидка_процент"], True, "20", "размер скидки, % (число от 1 до 90)"),
        ],
    },
}
ALIASES = {name: {alias: key for key, _, aliases, *_ in spec["columns"] for alias in aliases} for name, spec in SPEC.items()}
TRUE, FALSE = {"да", "yes", "y", "1", "true", "+", "есть"}, {"нет", "no", "n", "0", "false", "-", "нету"}


@dataclass
class Report:
    errors: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    total_errors: int = 0

    def error(self, file: str, row: Optional[int], message: str) -> None:
        self.total_errors += 1
        if len(self.errors) < MAX_ERRORS_SHOWN:
            self.errors.append({"file": SPEC[file]["title"] if file in SPEC else file, "row": row, "message": message})

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < 15:
            self.warnings.append(message)

    @property
    def ok(self) -> bool:
        return self.total_errors == 0

    def public(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "total_errors": self.total_errors,
                "warnings": self.warnings, "summary": self.summary}


# ---------- шаблоны ----------

def _csv(rows: List[List[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter=";", lineterminator="\n").writerows(rows)
    return "﻿" + buf.getvalue()  # BOM: Excel сразу открывает кириллицу


def template(name: str) -> dict:
    spec = SPEC[name]
    header = [c[1] for c in spec["columns"]]
    example = ["ПРИМЕР " + spec["columns"][0][4]] + [c[4] for c in spec["columns"][1:]]
    hint = ["" for _ in header]
    hint[0] = "ПРИМЕР: эту строку удалите"
    return {"name": name, "title": spec["title"], "filename": spec["file"],
            "content": _csv([header, example]), "required": spec["required"],
            "columns": [{"header": c[1], "required": c[3], "example": c[4], "hint": c[5]} for c in spec["columns"]]}


def templates() -> List[dict]:
    return [template(n) for n in SPEC]


def export_example(farm: Farm) -> Dict[str, str]:
    """Заполненные шаблоны из демо-предприятия: образец формата и способ проверить загрузку."""
    d = lambda x: x.strftime("%d.%m.%Y")
    num = lambda x: f"{x:g}".replace(".", ",")
    yn = lambda b: "да" if b else "нет"
    products = [[c[1] for c in SPEC["products"]["columns"]]] + [
        [p.id, p.name, p.category, p.unit, num(p.price), num(p.cost), str(p.shelf_life_days), yn(p.has_photo), yn(p.has_weight),
         yn(p.has_expiry), yn(p.seasonal), num(p.cost_prev) if p.cost_prev else "", d(p.cost_changed) if p.cost_changed else "",
         d(p.last_price_change) if p.last_price_change else ""] for p in farm.products]
    stock = [[c[1] for c in SPEC["stock"]["columns"]]] + [
        [b.product_id, num(b.qty), d(b.expires) if b.expires else ""] for b in farm.batches]
    sales = [[c[1] for c in SPEC["sales"]["columns"]]]
    for o in sorted(farm.orders, key=lambda o: o.day):
        for l in o.lines:
            sales.append([d(o.day), o.customer, l.product_id, num(l.qty), num(l.unit_price), o.id])
    views = [[c[1] for c in SPEC["views"]["columns"]]]
    for pid, days in farm.views.items():
        for day, n in sorted(days.items()):
            views.append([d(day), pid, str(n)])
    promos = [[c[1] for c in SPEC["promos"]["columns"]]] + [
        [p.product_id, d(p.start), d(p.end), num(round(p.discount * 100))] for p in farm.promos]
    return {"products": _csv(products), "stock": _csv(stock), "sales": _csv(sales), "views": _csv(views), "promos": _csv(promos)}


# ---------- разбор ----------

def _norm(header: str) -> str:
    return re.sub(r"\s+", "_", header.strip().lower().replace("*", "").replace("ё", "е")).strip("_")


def _read(name: str, text: str, rep: Report) -> List[Tuple[int, dict]]:
    spec = SPEC[name]
    if not text or not text.strip():
        rep.error(name, None, "файл пустой")
        return []
    if len(text) > MAX_FILE_CHARS:
        rep.error(name, None, "файл слишком большой (больше 4 МБ)")
        return []
    text = text.lstrip("﻿")
    first = text.splitlines()[0]
    delim = max((";", ",", "\t"), key=lambda d: first.count(d))
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    header = [_norm(h) for h in rows[0]]
    alias = {_norm(a): k for a, k in ALIASES[name].items()}
    index: Dict[str, int] = {}
    for i, h in enumerate(header):
        if h in alias and alias[h] not in index:
            index[alias[h]] = i
    missing = [c[1] for c in spec["columns"] if c[3] and c[0] not in index]
    if missing:
        rep.error(name, 1, "нет столбцов: " + ", ".join(f"«{m}»" for m in missing) + ". Скачайте шаблон и проверьте заголовки")
        return []
    out = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        first_cell = row[0].strip().upper() if row else ""
        if first_cell.startswith("ПРИМЕР") or first_cell.startswith("EXAMPLE"):
            continue
        out.append((line_no, {k: (row[i].strip() if i < len(row) else "") for k, i in index.items()}))
    return out


def _num(raw: str) -> Optional[float]:
    s = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"(?i)(₽|руб\.?|р\.?)$", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _date(raw: str) -> Optional[date]:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _flag(raw: str, default: bool) -> Optional[bool]:
    s = raw.strip().lower()
    if not s:
        return default
    return True if s in TRUE else False if s in FALSE else None


def _clean_text(s: str, limit: int = 60) -> str:
    return re.sub(r"[\x00-\x1f\x7f<>]", " ", s).strip()[:limit]


def parse(files: Dict[str, str], profile: dict) -> Tuple[Optional[Farm], Report]:
    """(предприятие или None, отчёт проверки). Ошибки не прерывают разбор: фермер видит их все сразу."""
    rep = Report()
    for name, spec in SPEC.items():
        if spec["required"] and not (files.get(name) or "").strip():
            rep.error(name, None, "файл не загружен")
    tables = {n: _read(n, files[n], rep) for n in SPEC if (files.get(n) or "").strip()}
    if not rep.ok:
        return None, rep

    # --- товары ---
    known = set(categories())
    products: Dict[str, Product] = {}
    unknown_cats = set()
    if len(tables["products"]) > MAX_PRODUCTS:
        rep.error("products", None, f"слишком много товаров (больше {MAX_PRODUCTS})")
    for line, r in tables["products"]:
        sku = _clean_text(r["sku"], 40)
        if not sku:
            rep.error("products", line, "пустой артикул")
            continue
        if sku in products:
            rep.error("products", line, f"артикул «{sku}» повторяется")
            continue
        price, cost = _num(r["price"]), _num(r["cost"])
        if price is None or price <= 0:
            rep.error("products", line, f"цена «{r['price']}» не похожа на число больше нуля")
            continue
        if cost is None or cost < 0:
            rep.error("products", line, f"себестоимость «{r['cost']}» не похожа на число")
            continue
        if cost >= price:
            rep.warn(f"У товара «{r['name']}» себестоимость не ниже цены: советы по скидкам для него работать не будут")
        name = _clean_text(r["name"])
        if not name:
            rep.error("products", line, "пустое название")
            continue
        cat = r.get("category", "").strip() or OTHER
        if cat not in known:
            unknown_cats.add(cat)
            cat = OTHER
        shelf = _num(r.get("shelf_life_days", "") or "365")
        if shelf is None or shelf <= 0:
            rep.error("products", line, f"срок годности «{r['shelf_life_days']}» не похож на число дней")
            continue
        extra = {}
        bad_extra = False
        for key in ("cost_prev",):
            if r.get(key):
                val = _num(r[key])
                if val is None or val <= 0:
                    rep.error("products", line, f"прежняя себестоимость «{r[key]}» не похожа на число")
                    bad_extra = True
                extra[key] = val
        for key in ("cost_changed", "price_changed"):
            if r.get(key):
                val = _date(r[key])
                if val is None:
                    rep.error("products", line, f"дата «{r[key]}» не распознана (нужно ДД.ММ.ГГГГ)")
                    bad_extra = True
                extra["last_price_change" if key == "price_changed" else key] = val
        if bad_extra:
            continue
        flags = {}
        for key, default in (("has_photo", True), ("has_weight", True), ("has_expiry", True), ("seasonal", False)):
            val = _flag(r.get(key, ""), default)
            if val is None:
                rep.error("products", line, f"в столбце «{key}» ожидается да/нет, а не «{r[key]}»")
                break
            flags[key] = val
        else:
            products[sku] = Product(id=sku, name=name, category=cat, unit=_clean_text(r.get("unit", ""), 12) or "шт", price=price,
                                    cost=cost, shelf_life_days=int(shelf), **flags, **extra)
    if unknown_cats:
        rep.warn("Категории не из списка отнесены к «Другое» (для них нет сравнения с рынком): " + ", ".join(sorted(unknown_cats)))
    if not products and rep.ok:
        rep.error("products", None, "нет ни одного товара")

    # товары с ошибкой уже названы в отчёте: зависимые строки не засоряют список «артикул не найден»
    bad_skus = {_clean_text(r["sku"], 40) for _, r in tables["products"]} - set(products) - {""}

    # --- остатки ---
    batches: List[Batch] = []
    for line, r in tables["stock"]:
        sku = r["sku"].strip()
        if sku in bad_skus:
            continue
        if sku not in products:
            rep.error("stock", line, f"артикул «{sku}» не найден в файле «Товары»")
            continue
        qty = _num(r["qty"])
        if qty is None or qty < 0:
            rep.error("stock", line, f"количество «{r['qty']}» не похоже на число")
            continue
        exp = None
        if r.get("expires"):
            exp = _date(r["expires"])
            if exp is None:
                rep.error("stock", line, f"дата «{r['expires']}» не распознана (нужно ДД.ММ.ГГГГ)")
                continue
        if qty > 0:
            batches.append(Batch(sku, qty, exp))

    # --- продажи ---
    orders_by: Dict[tuple, List[OrderLine]] = defaultdict(list)
    customers_of: Dict[tuple, str] = {}
    days_seen = set()
    if len(tables["sales"]) > MAX_SALES_ROWS:
        rep.error("sales", None, f"слишком много строк продаж (больше {MAX_SALES_ROWS:,}): загрузите последние 3 месяца".replace(",", " "))
    else:
        for line, r in tables["sales"]:
            day, sku = _date(r["date"]), r["sku"].strip()
            qty, price = _num(r["qty"]), _num(r["price"])
            if sku in bad_skus:
                continue
            if day is None:
                rep.error("sales", line, f"дата «{r['date']}» не распознана (нужно ДД.ММ.ГГГГ)")
            elif sku not in products:
                rep.error("sales", line, f"артикул «{sku}» не найден в файле «Товары»")
            elif qty is None or qty <= 0:
                rep.error("sales", line, f"количество «{r['qty']}» должно быть числом больше нуля")
            elif price is None or price <= 0:
                rep.error("sales", line, f"цена «{r['price']}» должна быть числом больше нуля")
            elif not r["customer"].strip():
                rep.error("sales", line, "не указан код клиента (подойдёт любой, например номер)")
            else:
                key = (day, r.get("order_id") or r["customer"].strip())
                orders_by[key].append(OrderLine(sku, qty, price))
                customers_of.setdefault(key, r["customer"].strip())
                days_seen.add(day)

    if days_seen:
        span = (max(days_seen) - min(days_seen)).days + 1
        if span < MIN_HISTORY_DAYS:
            rep.error("sales", None, f"в продажах всего {span} дн. истории, нужно минимум {MIN_HISTORY_DAYS}: иначе помощник не оценит обычный темп продаж")
    elif rep.ok:
        rep.error("sales", None, "нет ни одной строки продаж")

    # --- просмотры и акции (по желанию) ---
    views: Dict[str, Dict[date, int]] = {sku: {} for sku in products}
    for line, r in tables.get("views", []):
        day, n = _date(r["date"]), _num(r["views"])
        if r["sku"].strip() in bad_skus:
            continue
        if r["sku"].strip() not in products:
            rep.warn(f"В файле «Просмотры карточек» есть артикулы, которых нет в «Товарах» (например, «{r['sku']}»): строки пропущены")
        elif day is None or n is None or n < 0:
            rep.error("views", line, "дата или число просмотров не распознаны")
        else:
            views[r["sku"].strip()][day] = int(n)
    promos: List[Promo] = []
    for i, (line, r) in enumerate(tables.get("promos", [])):
        sku, start, end, disc = r["sku"].strip(), _date(r["start"]), _date(r["end"]), _num(r["discount"])
        if sku in bad_skus:
            continue
        if sku not in products:
            rep.error("promos", line, f"артикул «{sku}» не найден в файле «Товары»")
        elif start is None or end is None or end < start:
            rep.error("promos", line, "даты акции не распознаны или конец раньше начала")
        elif disc is None or not 1 <= disc <= 90:
            rep.error("promos", line, f"скидка «{r['discount']}» должна быть числом от 1 до 90")
        else:
            promos.append(Promo(f"promo-{i}", sku, start, end, disc / 100))

    if not rep.ok:
        rep.summary = {}
        return None, rep

    last, first = max(days_seen), min(days_seen)
    today = last + timedelta(days=1)
    if (date.today() - last).days > 45:
        rep.warn(f"Последняя продажа — {last.strftime('%d.%m.%Y')}. Помощник работает «на следующий день» после неё, а не на сегодняшнюю дату")
    expired = [b for b in batches if b.expires is not None and b.expires < today]
    if expired:
        rep.warn(f"{len(expired)} партий с истёкшим сроком в остатках пропущены")
        batches = [b for b in batches if b not in expired]
    orders = [Order(f"o-{i}", day, pseudonymize(f"{CUSTOM_ID}:{customers_of[(day, ident)]}"), lines)
              for i, ((day, ident), lines) in enumerate(sorted(orders_by.items(), key=lambda kv: kv[0][0]))]
    n_lines = sum(len(o.lines) for o in orders)
    if n_lines < 100:
        rep.warn("Продаж мало (меньше 100 строк): сигналов будет немного, точность оценок невысокая")
    if "views" not in tables:
        rep.warn("Файл просмотров не загружен: совет про «слабые карточки» работать не будет")
    farm = Farm(id=f"farm-{CUSTOM_ID}", name=_clean_text(profile.get("farm_name", ""), 80) or "Моё хозяйство",
                owner=_clean_text(profile.get("owner", ""), 40) or "друг", today=today,
                products=list(products.values()), orders=orders, views=views, promos=promos, reviews=[], batches=batches)
    rep.summary = {"products": len(products), "sales_rows": n_lines, "orders": len(orders),
                   "customers": len({o.customer for o in orders}), "days": (last - first).days + 1,
                   "from": first.isoformat(), "to": last.isoformat(), "today": today.isoformat(),
                   "batches": len(batches), "views_rows": sum(len(v) for v in views.values()), "promos": len(promos)}
    return farm, rep
