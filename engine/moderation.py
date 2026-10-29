"""Модерация текстов: что нельзя писать в карточке и в объяснениях без подтверждения."""
from __future__ import annotations

import re
from typing import Iterable, List

from .factcheck import Issue

# (тип, шаблон, тег подтверждения). Тег снимает правило, если фермер подтвердил характеристику документом.
RULES = [
    ("органик/эко", re.compile(r"органик|органическ|\bбио\w*|\bэко(?!ном)\w*", re.I), "organic"),
    ("медицинские обещания", re.compile(r"лечеб|лечит|излеч|исцел|целебн|иммунитет|простуд|болезн|диабет|онколог|похуд", re.I), None),
    ("«без…» без подтверждения", re.compile(r"без\s+(?:гмо|антибиотик\w*|консервант\w*|химии|добавок|пестицид\w*)", re.I), "free_from"),
    ("превосходная степень", re.compile(r"\bсам(?:ый|ая|ое|ые|ого|ому|ым|ых)\b|\bлучш(?:ий|ая|ее|ие|его|ей|им|ими|их)\b|№\s?1|номер один|единственн\w+|уникальн\w+|идеальн\w+", re.I), None),
    ("гарантии", re.compile(r"гаранти\w+|100\s?%|без риска|точно\s+(?:заработ|продад|вырастут)|обязательно\s+(?:вырастут|продад|заработ)", re.I), None),
    ("неточные сравнения", re.compile(r"\bвдвое\b|\bвтрое\b|\bв разы\b", re.I), None),
    ("контакты и ссылки", re.compile(r"https?://|www\.|@\w|(?:\+7|\b8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", re.I), None),
]


def moderate(text: str, confirmed: Iterable[str] = ()) -> List[Issue]:
    confirmed = set(confirmed)
    issues = []
    for label, pattern, tag in RULES:
        if tag and tag in confirmed:
            continue
        for m in pattern.finditer(text):
            issues.append(Issue("claim", m.group(0), f"{label}: «{m.group(0)}»"))
    return issues
