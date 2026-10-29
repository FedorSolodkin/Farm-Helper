"""Маршруты API без привязки к HTTP: один и тот же код работает в сервере и прямо в браузере (Pyodide)."""
from __future__ import annotations

from typing import Optional, Tuple

from .errors import ApiError
from .service import Assistant


def _route(a: Assistant, method: str, path: str, b: dict) -> dict:
    parts = [p for p in path.split("?")[0].split("/") if p][1:]  # без «api»
    if method == "GET" and parts == ["farms"]:
        return {"farms": a.farm_list()}
    if method == "GET" and parts == ["auth", "me"]:
        return {"required": False, "account": None}  # кабинеты включает только сервер; в браузерной версии входа нет
    if method == "GET" and parts == ["profile"]:
        return a.profile_view()
    if method == "POST" and parts == ["register"]:
        return a.register(b)
    if method == "POST" and parts == ["profile", "clear"]:
        return a.clear_profile()
    if method == "GET" and parts == ["templates"]:
        return a.templates_view()
    if method == "GET" and len(parts) == 2 and parts[0] == "example":
        return a.example_files(parts[1])
    if method == "POST" and parts == ["import"]:
        return a.import_data(b.get("files") or {}, b.get("profile") or {}, bool(b.get("dry_run")))
    if method == "GET" and parts == ["quality"]:
        return a.quality()
    if method == "GET" and parts == ["llm"]:
        return a.llm_info()
    if method == "POST" and parts == ["llm"]:
        return a.set_llm_mode(b.get("mode", ""))
    if method == "POST" and parts == ["reset"]:
        a.reset()
        return {"ok": True}
    if len(parts) == 3 and parts[0] == "farms":
        key, verb = parts[1], parts[2]
        if method == "GET" and verb == "today":
            return a.today_view(key)
        if method == "GET" and verb == "report":
            return a.report(key)
        if method == "GET" and verb == "delivery":
            return a.delivery(key)
        if method == "GET" and verb == "audit":
            return a.audit_view(key)
        if method == "POST":
            params = b.get("params") if isinstance(b.get("params"), dict) else None
            if verb == "preview":
                return a.preview(key, b.get("key", ""), params)
            if verb == "confirm":
                return a.confirm(key, b.get("key", ""), b.get("change_hash", ""), bool(b.get("second_confirmation")), params)
            if verb == "execute":
                return a.execute(key, b.get("key", ""), b.get("token", ""), params)
            if verb == "rollback":
                return a.rollback(key, b.get("execution_id", ""))
            if verb == "rewind":
                return a.rewind(key)
            if verb == "nextday":
                return a.next_day(key)
            if verb == "dismiss":
                return a.dismiss(key, b.get("key", ""), b.get("reason", ""))
            if verb == "chat":
                return a.chat(key, str(b.get("text", "")))
    raise ApiError("not_found", "Нет такого метода", 404)


def dispatch(a: Assistant, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict]:
    """(HTTP-статус, JSON-ответ). Ошибки приложения превращаются в понятный ответ, внутренние детали не раскрываются."""
    try:
        return 200, _route(a, method, path, body or {})
    except ApiError as err:
        return err.status, err.to_dict()
    except Exception:
        return 500, {"error": {"code": "internal", "message": "Внутренняя ошибка"}}
