"""Обезличивание: хэш клиента и k-анонимность бенчмарков."""
from __future__ import annotations

import hashlib

SALT = "agro-demo-v1"  # в продукте соль хранится в секретнице банка и ротируется
K_MIN = 5  # бенчмарк категории строится минимум по 5 фермерам


def pseudonymize(raw_id: str, salt: str = SALT) -> str:
    """Необратимый идентификатор клиента: имя, телефон и адрес в движок не попадают."""
    return hashlib.sha256(f"{salt}:{raw_id}".encode("utf-8")).hexdigest()[:12]


def usable_benchmarks(benchmarks: dict) -> dict:
    """Оставляет только категории, где достаточно фермеров, чтобы нельзя было вычислить одного."""
    return {name: b for name, b in benchmarks.items() if b.n_farms >= K_MIN}
