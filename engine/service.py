"""Слой приложения: рекомендации + подтверждение + исполнение. Не зависит от HTTP.

Что видит фермер: preview → confirm (выдаётся токен) → execute (по токену) → при желании rollback.
Изменение всегда берётся из серверного расчёта, а не из запроса клиента, и сверяется по хэшу с тем,
что фермер видел в превью."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import calibration
from . import delivery
from .audit import AuditLog, Clock, utcnow
from .benchmarks import BENCHMARKS
from .errors import ApiError
from .executor import IRREVERSIBLE, Executor
from .generate import SPECS, TODAY, build_farm
from .importer import CUSTOM_ID, SPEC as IMPORT_SPEC, export_example, parse as parse_import, templates as import_templates
from .llm import DemoLLM, provider_from_env
from .narrator import Narrator
from .privacy import usable_benchmarks
from .render import action_to_dict
from .report import build_report, measure, verdict
from .simulate import advance, build_profile, counterfactual, simulate
from .scoring import prioritize, snooze, validate
from .signals import Action, detect_all, retune_price, retune_spoilage
from .tokens import TokenService, change_hash

DAILY_ACTIONS = 3
REASONS = {"later": 1, "no_stock": 14, "no_discounts": 14, "already_done": 14, "bad_data": 14, "premium_price": 60}


class Assistant:
    def __init__(self, today: date = TODAY, secret: Optional[bytes] = None, clock: Clock = utcnow,
                 audit_path: Optional[Path] = None, llm="env", world_seed: str = "c"):
        self.today = today
        self.clock = clock
        self.audit_path = audit_path
        self.world_seed = world_seed  # зерно «мира» симулятора: одна и та же неделя при каждом показе
        self.llm = provider_from_env() if llm == "env" else llm
        self.profile: Optional[dict] = None  # кто работает и с каким предприятием (без паролей: вход банка подключается в пилоте)
        self.active: Optional[str] = None
        self._custom_files: Optional[Dict[str, str]] = None  # загруженные таблицы: по ним предприятие пересобирается при сбросе
        self.bench = usable_benchmarks(BENCHMARKS)
        self.reset(secret)

    def reset(self, secret: Optional[bytes] = None) -> None:
        self.audit = AuditLog(self.clock, self.audit_path)
        self.tokens = TokenService(secret, self.clock)
        self.farms = {k: build_farm(k, self.today) for k in SPECS}
        self.executors = {k: Executor(f, self.farmer(k), self.tokens, self.audit, self.clock) for k, f in self.farms.items()}
        self.snoozed: Dict[str, dict] = {k: {} for k in SPECS}
        self.cal: Dict[str, Dict[str, List[float]]] = {k: {} for k in SPECS}  # факт/прогноз по типам дел
        self.reports: Dict[str, List[dict]] = {k: [] for k in SPECS}
        self.tracking: Dict[str, Dict[str, dict]] = {k: {} for k in SPECS}  # дело -> накопленный факт и число прожитых дней
        self.chat_ctx: Dict[str, dict] = {k: {} for k in SPECS}  # о каком деле шла речь в чате
        self.narrator = Narrator(self.llm, self.clock, self._llm_event)
        if self._custom_files:  # своё предприятие после сброса возвращается к загруженным данным
            farm, _ = parse_import(self._custom_files, self.profile or {})
            if farm:
                self._install(farm)

    def _install(self, farm) -> None:
        key = CUSTOM_ID
        self.farms[key] = farm
        self.executors[key] = Executor(farm, self.farmer(key), self.tokens, self.audit, self.clock)
        self.snoozed[key], self.cal[key], self.reports[key], self.chat_ctx[key] = {}, {}, [], {}
        self.tracking[key] = {}

    @staticmethod
    def _text(value, limit: int) -> str:
        return re.sub(r"[\x00-\x1f\x7f<>]", " ", str(value or "")).strip()[:limit]

    def owner_name(self, key: str) -> str:
        return self.profile["owner"] if self.profile else self._farm(key).owner

    def farm_list(self) -> List[dict]:
        out = [{"key": k, "name": s["name"], "custom": False} for k, s in SPECS.items()]
        if CUSTOM_ID in self.farms:
            out.append({"key": CUSTOM_ID, "name": self.farms[CUSTOM_ID].name, "custom": True})
        return out

    def profile_view(self) -> dict:
        return {"profile": self.profile, "active": self.active, "farms": self.farm_list(), "has_custom": CUSTOM_ID in self.farms}

    def register(self, data: dict) -> dict:
        """Знакомство: имя, хозяйство и выбор готового демо-предприятия. Загрузка своих данных идёт через import_data."""
        owner, name = self._text(data.get("owner"), 40), self._text(data.get("farm_name"), 80)
        if not owner:
            raise ApiError("bad_request", "Укажите, как к вам обращаться", 400)
        if not name:
            raise ApiError("bad_request", "Укажите название хозяйства", 400)
        demo_key = data.get("demo_key")
        if demo_key not in SPECS:
            raise ApiError("bad_request", "Выберите одно из готовых предприятий", 400)
        self._set_profile(data, owner, name)
        self.active = demo_key
        self.audit.append(self.farmer(demo_key), "registered", None, None, {"source": "demo", "farm": demo_key})
        return self.profile_view()

    def _set_profile(self, data: dict, owner: str, name: str) -> None:
        interests = [self._text(i, 40) for i in (data.get("interests") or []) if isinstance(i, str)][:6]
        self.profile = {"owner": owner, "farm_name": name, "region": self._text(data.get("region"), 60), "interests": interests}

    def import_data(self, files: dict, profile: dict, dry_run: bool = False) -> dict:
        """Проверка (dry_run) и загрузка своих таблиц. Предприятие живёт только в этой сессии."""
        if not isinstance(files, dict) or sum(len(v) for v in files.values() if isinstance(v, str)) > 10_000_000:
            raise ApiError("too_large", "Файлы слишком большие", 413)
        files = {k: v for k, v in files.items() if k in IMPORT_SPEC and isinstance(v, str)}
        owner, name = self._text((profile or {}).get("owner"), 40), self._text((profile or {}).get("farm_name"), 80)
        farm, report = parse_import(files, {"owner": owner, "farm_name": name})
        result = report.public()
        if farm is None or dry_run:
            result["imported"] = False
            return result
        if not owner or not name:
            raise ApiError("bad_request", "Укажите имя и название хозяйства", 400)
        self._custom_files = files
        self._set_profile(profile, owner, name)
        self._install(farm)
        self.active = CUSTOM_ID
        self.audit.append(self.farmer(CUSTOM_ID), "registered", None, None, {"source": "upload", **{k: v for k, v in report.summary.items() if k in ("products", "orders", "days")}})
        result["imported"] = True
        return {**result, **self.profile_view()}

    def snapshot(self) -> dict:
        """То, что кабинет хранит на диске: профиль, выбранное предприятие и загруженные таблицы."""
        return {"profile": self.profile, "active": self.active, "custom_files": self._custom_files}

    def restore(self, profile: Optional[dict], active: Optional[str], custom_files: Optional[Dict[str, str]]) -> None:
        self.profile, self._custom_files = profile, custom_files
        self.active = active
        if custom_files:
            farm, _ = parse_import(custom_files, profile or {})
            if farm:
                self._install(farm)
            else:
                self._custom_files = None
        if self.active == CUSTOM_ID and CUSTOM_ID not in self.farms:
            self.active = None

    def clear_profile(self) -> dict:
        self.profile, self.active, self._custom_files = None, None, None
        for store in (self.farms, self.executors, self.snoozed, self.cal, self.reports, self.chat_ctx, self.tracking):
            store.pop(CUSTOM_ID, None)
        return self.profile_view()

    def templates_view(self) -> dict:
        return {"templates": import_templates()}

    def example_files(self, farm_key: str) -> dict:
        if farm_key not in SPECS:
            raise ApiError("not_found", "Пример для этого предприятия недоступен", 404)
        return {"files": export_example(build_farm(farm_key, self.today))}

    def _llm_event(self, kind: str, action_key: str, details: dict) -> None:
        farm = details.pop("farm", "farm-?").replace("farm-", "")
        self.audit.append(self.farmer(farm), kind, action_key, None, details)

    # --- доступ ---
    @staticmethod
    def farmer(key: str) -> str:
        return f"farmer-{key}"  # в пилоте — SSO банка; здесь один фермер на ферму

    def _farm(self, key: str):
        if key not in self.farms:
            raise ApiError("not_found", "Ферма не найдена", 404)
        return self.farms[key]

    def _candidates(self, key: str) -> List[Action]:
        actions = detect_all(self._farm(key), self.bench)
        calibration.apply(actions, self.cal[key])  # прогнозы уже поправлены по прошлым проверкам
        return actions

    def _retune(self, key: str, action: Action, params: Optional[dict]) -> Action:
        """Дело с параметрами фермера (скидка, цена): пересчитывается в коде, проверяется теми же стоп-правилами."""
        if not params:
            return action
        farm = self._farm(key)
        scale = calibration.stats(self.cal[key], action.type)[1]
        try:
            if action.type == "spoilage" and "discount" in params:
                d = round(float(params["discount"]), 2)
                if not 0 < d < 1:
                    raise ValueError("скидка вне диапазона")
                return retune_spoilage(farm, action, d, scale)
            if action.type == "price_off" and "price" in params:
                return retune_price(farm, self.bench, action, float(params["price"]), scale)
        except (ValueError, TypeError) as exc:
            raise ApiError("bad_params", f"Параметры не подходят: {exc}", 422)
        raise ApiError("bad_params", "Для этого дела параметры не поддерживаются", 422)

    def _find(self, key: str, action_key: str, params: Optional[dict] = None) -> Action:
        farm = self._farm(key)
        action = next((a for a in self._candidates(key) if a.key == action_key), None)
        if action is None:
            raise ApiError("not_found", "Это дело уже неактуально. Обновите список", 404)
        action = self._retune(key, action, params)
        violations = validate(action, farm)
        if violations:
            raise ApiError("blocked", "Стоп-правила не позволяют это сделать: " + "; ".join(violations), 422,
                           {"reasons": violations})
        return action

    # --- представление ---
    @staticmethod
    def _public(a: Action) -> dict:
        d = action_to_dict(a)
        op = d.pop("change")["op"]
        d["op"] = op
        d["irreversible"] = op in IRREVERSIBLE
        if "segment" in a.change:
            d["recipients"] = len(a.change["segment"])
        return d

    def delivery(self, key: str) -> dict:
        """Открытые заказы на карте, ранжирование и план рейсов. Только расчёт: клиентам ничего не отправляется."""
        return delivery.build(self._farm(key))

    def today_view(self, key: str) -> dict:
        farm = self._farm(key)
        done = self.executors[key].active()
        open_slots = max(0, DAILY_ACTIONS - len(done))
        sel = prioritize(self._candidates(key), farm, self.snoozed[key], top_n=open_slots)
        note = None
        if not open_slots:
            note = "Все дела на сегодня выполнены. Завтра пришлю новые."
        elif not sel.selected and not done:
            note = sel.note
        narrations = self.narrator.narrate_many(farm, sel.selected)
        actions = []
        for a in sel.selected:
            pub = self._public(a)
            pub["narration"] = narrations[a.key].public()
            pub["track"] = calibration.track(self.cal[key], a.type)
            actions.append(pub)
        ok, _ = self.audit.verify()
        return {
            "farm": {"key": key, "name": farm.name, "owner": self.owner_name(key)},
            "date": farm.today.isoformat(),
            "summary": {
                "total": len(done) + len(sel.selected),
                "confirmed": len(done),
                "effect": round(sum(e.effect_mid for e in done) + sel.total_effect),
                "minutes": sel.total_minutes,
                "note": note,
            },
            "actions": actions,
            "done": [e.public() for e in done],
            "can_rewind": bool(self._unchecked(key)),
            "results": self._results(key),
            "last_report": self._brief(self.reports[key][-1]) if self.reports[key] else None,
            "deferred": [{"key": a.key, "title": a.title, "effect_mid": round(a.effect_mid)} for a in sel.deferred],
            "audit": {"entries": len(self.audit.for_farmer(self.farmer(key))), "chain_ok": ok},
        }

    @staticmethod
    def _brief(r: dict) -> dict:
        return {"forecast": r["totals"]["forecast"], "fact": r["totals"]["fact"], "count": r["totals"]["count"],
                "from": r["period"]["from"], "to": r["period"]["to"]}

    # --- перемотка и отчёт ---
    def rewind(self, key: str, days: int = 7) -> dict:
        farm = self._farm(key)
        tracked = self.executors[key].tracked()
        pending = self._unchecked(key)
        if not pending:
            raise ApiError("nothing_to_check", "Нечего проверять: сначала подтвердите хотя бы одно дело", 409)
        profile = build_profile(farm)
        seed = f"{self.world_seed}-{farm.id}-{farm.today.isoformat()}"
        base = simulate(farm, profile, days, seed)
        each = {}
        for ex in tracked:
            try:
                each[ex.id] = simulate(counterfactual(farm, [ex], ex), profile, days, seed)
            except Exception:  # дело нельзя «убрать» из мира: оно выпадает из отчёта
                continue
        prior = {ex.id: dict(self.tracking[key].get(ex.id, {"fact": 0.0, "days": 0})) for ex in tracked}
        self._accumulate(key, profile, days, seed, base, each)
        measured = [ex for ex in pending if ex.id in each]
        nothing = simulate(counterfactual(farm, measured, None), profile, days, seed)
        start = farm.today
        report = build_report(farm, measured, base, each, nothing, self.cal[key], start, days, prior)
        advance(farm, base)
        self.chat_ctx[key] = {}  # новый день: о делах прошлой недели чат больше не помнит
        self.reports[key].append(report)
        self.audit.append(self.farmer(key), "rewind", None, None, {
            "from": start.isoformat(), "to": farm.today.isoformat(),
            "forecast": report["totals"]["forecast"], "fact": report["totals"]["fact"]})
        return {"report": report, "date": farm.today.isoformat()}

    def next_day(self, key: str) -> dict:
        """Только для теста: прокручивает один день без отчёта. Дела, подтверждённые вчера, в отчёт «прогноз против факта» уже не попадут."""
        farm = self._farm(key)
        profile = build_profile(farm)
        start = farm.today
        seed = f"{self.world_seed}-{farm.id}-{start.isoformat()}-day"
        sim = simulate(farm, profile, 1, seed)
        self._accumulate(key, profile, 1, seed, sim, {})
        advance(farm, sim)
        self.chat_ctx[key] = {}
        self.audit.append(self.farmer(key), "next_day", None, None, {"from": start.isoformat(), "to": farm.today.isoformat()})
        return {"date": farm.today.isoformat(), "orders": len(sim.orders)}

    def _unchecked(self, key: str) -> list:
        """Принятые дела, которые ещё не получили недельный вердикт: именно их проверит отчёт."""
        return [ex for ex in self.executors[key].tracked() if self.tracking[key].get(ex.id, {"days": 0})["days"] < 7]

    def _accumulate(self, key: str, profile, days: int, seed: str, base, each: dict) -> None:
        """Добавляет к каждому принятому делу, сколько оно принесло за прожитые дни (разница с миром, где его не запускали)."""
        farm = self._farm(key)
        for ex in self.executors[key].tracked():
            try:
                alt = each.get(ex.id) or simulate(counterfactual(farm, [ex], ex), profile, days, seed)
                fact = measure(farm, ex, base, alt)["fact"]
            except Exception:  # дело нельзя «убрать» из мира (данные уже изменились): считаем его эффект неизвестным
                continue
            rec = self.tracking[key].setdefault(ex.id, {"fact": 0.0, "days": 0})
            rec["fact"] += fact
            rec["days"] += days

    def _results(self, key: str) -> dict:
        """Сводка для экрана: что принесли принятые дела с момента подтверждения."""
        items = []
        for ex in reversed(self.executors[key].tracked()):
            rec = self.tracking[key].get(ex.id, {"fact": 0.0, "days": 0})
            low, mid, high = ex.effect
            if not rec["days"]:
                status = "wait"
            elif rec["days"] >= 7:
                status = verdict(rec["fact"], low, high)
            else:
                status = "works" if rec["fact"] > 0 else "none"
            items.append({"title": ex.summary, "type": ex.type, "days": rec["days"], "fact": round(rec["fact"]),
                          "forecast": round(mid), "status": status})
        measured = [i for i in items if i["days"]]
        return {"items": items[:8], "total_fact": sum(i["fact"] for i in measured),
                "total_forecast": sum(i["forecast"] for i in measured), "days": max((i["days"] for i in measured), default=0)}

    def report(self, key: str) -> dict:
        self._farm(key)
        if not self.reports[key]:
            raise ApiError("not_found", "Отчёта пока нет: перемотайте неделю после подтверждённых дел", 404)
        return {"report": self.reports[key][-1], "reports": len(self.reports[key])}

    # --- поток подтверждения ---
    def preview(self, key: str, action_key: str, params: Optional[dict] = None) -> dict:
        a = self._find(key, action_key, params)
        h = change_hash(a.change)
        self.audit.append(self.farmer(key), "preview", a.key, h)
        op = a.change["op"]
        out = {"action_key": a.key, "title": a.title, "preview": [{"what": w, "before": b, "after": af} for w, b, af in a.preview],
               "draft": self.narrator.narrate(self._farm(key), a).draft or a.draft, "change_hash": h, "reversible": op not in IRREVERSIBLE,
               "ttl_minutes": self.tokens.ttl // 60, "params": params or {}}
        if a.type == "spoilage":
            out["adjustable"] = {"kind": "discount", "options": [0.10, 0.15, 0.20, 0.25, 0.30], "value": a.change["discount"]}
        if op in IRREVERSIBLE:
            out["recipients"] = len(a.change["segment"])
            out["irreversible_note"] = "Сообщение нельзя отозвать после отправки."
        return out

    def confirm(self, key: str, action_key: str, seen_hash: str, second_confirmation: bool = False,
                params: Optional[dict] = None) -> dict:
        a = self._find(key, action_key, params)
        h = change_hash(a.change)
        if h != seen_hash:
            raise ApiError("changed", "Данные изменились с момента просмотра. Откройте превью заново", 409)
        if a.change["op"] in IRREVERSIBLE and not second_confirmation:
            raise ApiError("second_confirmation_required",
                           "Это действие нельзя отменить. Подтвердите ещё раз, прочитав текст и число получателей", 428,
                           {"recipients": len(a.change["segment"])})
        token, expires, jti = self.tokens.issue(self.farmer(key), a.key, h)
        self.audit.append(self.farmer(key), "confirmed", a.key, h,
                          {"token": jti, "expires": expires.isoformat(timespec="seconds"),
                           "second_confirmation": second_confirmation})
        return {"token": token, "expires_at": expires.isoformat(timespec="seconds")}

    def execute(self, key: str, action_key: str, token: str, params: Optional[dict] = None) -> dict:
        farm = self._farm(key)
        action = next((a for a in self._candidates(key) if a.key == action_key), None)
        if action is None:
            raise ApiError("not_found", "Это дело уже неактуально. Обновите список", 404)
        action = self._retune(key, action, params)
        ex = self.executors[key].execute(token, action)
        return {"execution": ex.public()}

    def rollback(self, key: str, execution_id: str) -> dict:
        self._farm(key)
        return {"execution": self.executors[key].rollback(execution_id).public()}

    def dismiss(self, key: str, action_key: str, reason: str) -> dict:
        farm = self._farm(key)
        if reason not in REASONS:
            raise ApiError("bad_request", "Неизвестная причина", 400)
        action = next((a for a in self._candidates(key) if a.key == action_key), None)
        if action is None:
            raise ApiError("not_found", "Это дело уже неактуально. Обновите список", 404)
        self.snoozed[key].update(snooze(action, farm.today, REASONS[reason]))
        if reason == "premium_price":  # фермер сказал: цена выбрана осознанно, больше не советуем её менять
            premium = farm.state.setdefault("premium_products", [])
            premium += [pid for pid in action.product_ids if pid not in premium]
        self.audit.append(self.farmer(key), "dismissed", action.key, change_hash(action.change),
                          {"reason": reason, "days": REASONS[reason]})
        return {"snoozed_days": REASONS[reason]}

    def chat(self, key: str, text: str) -> dict:
        from .chat import answer
        self._farm(key)
        return answer(self, key, text)

    def quality(self) -> dict:
        return self.narrator.quality()

    def llm_info(self) -> dict:
        demo = isinstance(self.llm, DemoLLM)
        return {"provider": self.llm.name if self.llm else None, "demo": demo,
                "mode": self.llm.mode if demo else None,
                "modes": [{"key": k, "label": v} for k, v in DemoLLM.MODES.items()] if demo else []}

    def set_llm_mode(self, mode: str) -> dict:
        if not isinstance(self.llm, DemoLLM) or mode not in DemoLLM.MODES:
            raise ApiError("bad_request", "Режим недоступен", 400)
        self.llm.mode = mode
        self.narrator.clear_cache()
        return self.llm_info()

    def audit_view(self, key: str) -> dict:
        self._farm(key)
        ok, bad = self.audit.verify()
        return {"entries": self.audit.for_farmer(self.farmer(key)), "chain_ok": ok, "first_bad": bad}
