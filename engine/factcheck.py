"""Фактчекер: все числа, даты и названия в тексте ИИ должны быть в фактах действия.

Движок считает цифры, ИИ только пересказывает. Любое число, которого нет среди фактов, —
выдумка: текст отклоняется, и фермеру показывается шаблон."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Set

from .signals import Action, rub, rub_exact

MAX_LEN = 480
HORIZON_DAYS = 7  # «за неделю» — не выдумка

NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](\d+))?(?:\s*(тыс|млн)\b\.?)?")
DATE = re.compile(r"\b\d{1,2}\.\d{2}\b")
QUOTED = re.compile(r"«([^»]+)»")
WORDS = {"один": 1, "одно": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
         "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40,
         "пятьдесят": 50, "сто": 100}
WORD_NUM = re.compile(r"\b(" + "|".join(WORDS) + r")\s+(?:дн|недел|раз|кг|клиент|процент|рубл|час|мин|штук|заказ|акци|товар|просмотр)",
                      re.IGNORECASE)


@dataclass(frozen=True)
class Issue:
    kind: str  # number | date | name | style | claim
    token: str
    message: str


@dataclass
class Check:
    issues: List[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass
class Allowed:
    numbers: Set[float] = field(default_factory=set)
    dates: Set[str] = field(default_factory=set)
    names: Set[str] = field(default_factory=set)

    @property
    def stems(self) -> Set[tuple]:
        return {_stems(n) for n in self.names}


def _stems(name: str) -> tuple:
    """Название без падежных окончаний: «Творога 9%» и «Творог 9%» — один товар, «Сулугуни» — другой."""
    return tuple(w[:5] for w in re.findall(r"[\w%]+", name.lower()))


def _numbers(text: str, dates: Set[str] = frozenset()) -> List[float]:
    text = DATE.sub(lambda m: " " if m.group(0) in dates else m.group(0), text)
    out = []
    for m in NUM.finditer(text):
        whole = re.sub(r"[   ]", "", m.group(1))
        value = float(whole + ("." + m.group(2) if m.group(2) else ""))
        if m.group(3):
            value *= 1000 if m.group(3) == "тыс" else 1_000_000
        out.append(value)
    return out


def allowed_from_texts(texts: Iterable[str], extra_numbers: Iterable[float] = ()) -> Allowed:
    blob = " \n ".join(texts)
    allowed = Allowed(dates=set(DATE.findall(blob)), names={n.lower() for n in QUOTED.findall(blob)})
    allowed.numbers = set(_numbers(blob)) | set(extra_numbers) | {HORIZON_DAYS}
    return allowed


def allowed_from_action(action: Action) -> Allowed:
    texts = [action.title, action.effect_note, action.confidence_basis]
    texts += action.why
    texts += [f"{w} {b} {a}" for w, b, a in action.preview]
    low, mid, high = action.effect
    texts += [rub(low), rub(mid), rub(high)]
    return allowed_from_texts(texts, [round(low), round(mid), round(high)])


def facts_text(action: Action) -> List[str]:
    """Факты в том виде, в каком они попадают в промпт."""
    low, mid, high = action.effect
    return [action.title, *action.why, f"эффект за неделю: {rub(mid)} (от {rub(low)} до {rub(high)})"]


def _matches(x: float, allowed: Set[float]) -> bool:
    return any(abs(x - a) <= max(0.051, 0.02 * a) for a in allowed)


def check_text(text: str, allowed: Allowed, max_len: int = MAX_LEN) -> Check:
    issues: List[Issue] = []
    if not text.strip():
        return Check([Issue("style", "", "пустой текст")])
    if len(text) > max_len:
        issues.append(Issue("style", str(len(text)), f"длиннее {max_len} символов"))
    if not re.search(r"[а-яА-ЯёЁ]", text):
        issues.append(Issue("style", "", "текст не на русском"))
    for token in DATE.findall(text):
        if token not in allowed.dates and not _matches(float(token), allowed.numbers):
            issues.append(Issue("date", token, f"дата или число {token} нет в фактах"))
    seen = set()
    for value in _numbers(text, allowed.dates):
        if value in seen:
            continue
        seen.add(value)
        if not _matches(value, allowed.numbers):
            issues.append(Issue("number", f"{value:g}", f"число {value:g} нет в фактах"))
    for m in WORD_NUM.finditer(text):
        value = WORDS[m.group(1).lower()]
        if not _matches(value, allowed.numbers):
            issues.append(Issue("number", m.group(0), f"число прописью «{m.group(1)}» нет в фактах"))
    stems = allowed.stems
    for name in QUOTED.findall(text):
        if name.lower() not in allowed.names and _stems(name) not in stems:
            issues.append(Issue("name", name, f"название «{name}» нет в фактах"))
    return Check(issues)
