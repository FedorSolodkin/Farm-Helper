"""Текстовый вывод «3 дела на сегодня» и JSON для будущего экрана."""
from __future__ import annotations

import textwrap
from typing import List

from .models import Farm
from .scoring import Selection
from .signals import Action, confidence_label, rub, ru_date

WIDTH = 74
URGENCY = {3: "СРОЧНО", 2: "НА ЭТОЙ НЕДЕЛЕ", 1: "БЕЗ СПЕШКИ"}


def _wrap(text: str, indent: str = "", first: str = "") -> List[str]:
    return textwrap.wrap(text, WIDTH - 4, initial_indent=first or indent, subsequent_indent=indent) or [""]


def card(a: Action, idx: int, total: int) -> str:
    body: List[str] = []
    body += _wrap(a.title)
    body += ["", "ПОЧЕМУ"]
    for fact in a.why:
        body += _wrap(fact, indent="  ", first="• ")
    body += ["", "ЭФФЕКТ ЗА 7 ДНЕЙ"]
    low, mid, high = a.effect
    body += [f"+{rub(mid)} (диапазон {rub(low)} — {rub(high)})"]
    body += _wrap(a.effect_note)
    body += _wrap(f"Уверенность: {confidence_label(a.confidence)} — {a.confidence_basis}")
    body += ["", "ЧТО ИЗМЕНИТСЯ ПОСЛЕ ВАШЕГО ПОДТВЕРЖДЕНИЯ"]
    for what, before, after in a.preview:
        body += _wrap(f"{what}: {before} → {after}", indent="  ", first="• ")
    if a.draft:
        body += ["", "ЧЕРНОВИК (шаблон; позже подготовит ИИ)"]
        for item in a.draft["checklist"]:
            body += _wrap(item, indent="  ", first="• ")
    body += ["", f"[ {a.cta} ]  [ Изменить ]  [ Не сейчас ]",
             f"Не подходит ▾ · ~{a.minutes} мин"]
    head = f" {idx} из {total} · {URGENCY[a.urgency]} · {a.label} "
    top = "┌" + head + "─" * (WIDTH - 2 - len(head)) + "┐"
    lines = [top] + ["│ " + ln.ljust(WIDTH - 4) + " │" for ln in body] + ["└" + "─" * (WIDTH - 2) + "┘"]
    return "\n".join(lines)


def report(farm: Farm, sel: Selection, audit: bool = False) -> str:
    n = len(sel.selected)
    out = [f"{farm.name} · {ru_date(farm.today)}", ""]
    if n:
        out.append(f"Здравствуйте, {farm.owner}! {n} {'дело' if n == 1 else 'дела'} на сегодня · "
                   f"≈ +{rub(sel.total_effect)} за неделю · ~{sel.total_minutes} мин")
    if sel.note:
        out.append(sel.note)
    out.append("")
    for i, a in enumerate(sel.selected, 1):
        out += [card(a, i, n), ""]
    if audit:
        out.append("— Аудит отбора —")
        for a in sel.deferred:
            out.append(f"  отложено   {a.key:30} эффект {rub(a.effect_mid):>9}  приоритет {a.priority:7.0f}")
        for a, why in sel.blocked:
            out.append(f"  заблокировано {a.key}: {'; '.join(why)}")
        for a in sel.weak:
            out.append(f"  слабый эффект {a.key:28} эффект {rub(a.effect_mid):>9}")
        for a in sel.snoozed:
            out.append(f"  отклонено ранее {a.key}")
        out.append("")
    return "\n".join(out)


def action_to_dict(a: Action) -> dict:
    return {
        "key": a.key, "type": a.type, "label": a.label, "title": a.title, "why": a.why,
        "effect": {"low": round(a.effect[0]), "mid": round(a.effect[1]), "high": round(a.effect[2]),
                   "note": a.effect_note, "horizon_days": 7},
        "confidence": {"value": round(a.confidence, 2), "label": confidence_label(a.confidence), "basis": a.confidence_basis},
        "urgency": a.urgency, "effort": a.effort, "minutes": a.minutes, "priority": round(a.priority),
        "cta": a.cta, "change": a.change,
        "preview": [{"what": w, "before": b, "after": af} for w, b, af in a.preview],
        "draft": a.draft, "deadline": a.deadline.isoformat() if a.deadline else None,
        "product_ids": list(a.product_ids),
        "requires_confirmation": True,  # исполнение только через Action Executor с токеном фермера
    }


def selection_to_dict(farm: Farm, sel: Selection) -> dict:
    return {
        "farm": {"id": farm.id, "name": farm.name, "owner": farm.owner},
        "date": farm.today.isoformat(),
        "summary": {"actions": len(sel.selected), "total_effect": round(sel.total_effect),
                    "total_minutes": sel.total_minutes, "note": sel.note},
        "actions": [action_to_dict(a) for a in sel.selected],
        "deferred": [action_to_dict(a) for a in sel.deferred],
        "blocked": [{"key": a.key, "reasons": why} for a, why in sel.blocked],
    }
