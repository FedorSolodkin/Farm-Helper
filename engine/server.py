"""python3 -m engine.server [--port 8765] — экран и API на одном порту (только стандартная библиотека).

GET  /healthz
GET  /api/farms
GET  /api/farms/{ферма}/today | audit | report
POST /api/farms/{ферма}/preview   {"key", "params"?}
POST /api/farms/{ферма}/confirm   {"key", "change_hash", "second_confirmation", "params"?}
POST /api/farms/{ферма}/execute   {"key", "token", "params"?}
POST /api/farms/{ферма}/rollback  {"execution_id"}
POST /api/farms/{ферма}/dismiss   {"key", "reason"}
POST /api/farms/{ферма}/rewind    перемотка на 7 дней, возвращает отчёт
POST /api/farms/{ферма}/nextday   тест: перейти на следующий день без отчёта
POST /api/farms/{ферма}/chat      {"text"} — вопрос помощнику
GET  /api/profile, POST /api/register, POST /api/profile/clear   знакомство и выбор предприятия
GET  /api/templates, GET /api/example/{ферма}, POST /api/import  шаблоны, пример и загрузка своих данных
GET  /api/quality                 контроль качества ИИ-текстов
GET  /api/llm, POST /api/llm {"mode"}   только демо-ИИ: good | hallucinate | unsafe | down
POST /api/reset                   сбрасывает демо (в режиме сессий — только вашу сессию)
GET  /api/auth/me, POST /api/auth/register|login|logout|password   кабинеты (по умолчанию включены, --no-auth отключает)

Переменные окружения: HOST, PORT, SESSIONS=1 (у каждого посетителя своё состояние), LLM_*.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import date
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .accounts import AccountStore, Sessions
from .api import dispatch
from .errors import ApiError
from .hub import Hub, RateLimiter
from .llm import DemoLLM
from .service import Assistant

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
if not WEB.exists():  # «плоская» раскладка: все файлы в одной папке
    WEB = HERE
ENV_FILES = [HERE / "secrets.env", HERE.parent / "secrets.env"]
PUBLIC_PAGES = {"/": "index.html", "/index.html": "index.html", "/delivery": "delivery.html", "/delivery.html": "delivery.html"}  # статикой отдаём только экраны: исходники и секреты недоступны


def load_env_file(paths=None, environ=os.environ) -> list:
    """Подхватывает KEY=VALUE из secrets.env (или файла из ENV_FILE), не перезаписывая уже заданное окружение."""
    candidates = [Path(environ["ENV_FILE"])] if environ.get("ENV_FILE") else list(paths or ENV_FILES)
    loaded = []
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in environ:
                environ[key] = value
                loaded.append(key)
    return loaded
MAX_BODY = 16 * 1024
MAX_IMPORT = 12 * 1024 * 1024  # загрузка таблиц — единственный запрос, которому нужно больше
COOKIE_TTL = 7200


PERSIST = ("/api/register", "/api/import", "/api/profile/clear")  # после них профиль и загруженные данные сохраняются в кабинет


@dataclass
class AuthState:
    store: AccountStore
    sessions: Sessions
    limiter: RateLimiter  # входы и регистрации с одного адреса


class Handler(SimpleHTTPRequestHandler):
    hub: Hub
    limiter: RateLimiter
    chat_limiter: RateLimiter
    import_limiter: RateLimiter
    auth: Optional[AuthState] = None
    _new_sid: Optional[str] = None
    _cookies: Optional[list] = None
    _static = False

    def log_message(self, *args) -> None:  # тихий лог
        pass

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._static:
            self.send_header("Cache-Control", "no-cache")  # после обновления браузер сразу берёт свежую страницу
        super().end_headers()

    # --- служебное ---
    def _client(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def _sid(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "sid" in cookie and 8 <= len(cookie["sid"].value) <= 64:
            return cookie["sid"].value
        self._new_sid = secrets.token_urlsafe(18)
        return self._new_sid

    def _secure(self) -> str:
        return "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""

    def _token(self) -> Optional[str]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["auth"].value if "auth" in cookie and 20 <= len(cookie["auth"].value) <= 128 else None

    def _set_cookie(self, token: str, max_age: int) -> None:
        if self._cookies is None:
            self._cookies = []
        self._cookies.append(f"auth={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{self._secure()}")

    def _account(self) -> Optional[dict]:
        acct_id = self.auth.sessions.resolve(self._token())
        return self.auth.store.get(acct_id) if acct_id else None

    def _check_origin(self) -> None:
        """Запросы, меняющие данные, принимаем только со страницы этого же сервера и только в формате JSON."""
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            raise ApiError("forbidden", "Запрос с чужой страницы отклонён", 403)
        if int(self.headers.get("Content-Length") or 0) and "json" not in self.headers.get("Content-Type", ""):
            raise ApiError("bad_request", "Ожидался JSON", 415)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in self._cookies or []:
            self.send_header("Set-Cookie", cookie)
        if self._new_sid:
            self.send_header("Set-Cookie", f"sid={self._new_sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_TTL}{self._secure()}")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > (MAX_IMPORT if self.path.endswith("/import") else MAX_BODY):
            raise ApiError("too_large", "Запрос слишком большой", 413)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError("bad_request", "Некорректный JSON", 400)
        if not isinstance(data, dict):
            raise ApiError("bad_request", "Ожидался объект JSON", 400)
        return data

    # --- кабинеты ---
    def _auth_route(self, method: str, route: str, body: dict, who: str) -> None:
        a = self.auth
        if route == "/api/auth/me" and method == "GET":
            acct = self._account()
            return self._send(200, {"required": True, "account": a.store.public(acct) if acct else None})
        if method != "POST":
            raise ApiError("not_found", "Нет такого метода", 404)
        if route in ("/api/auth/login", "/api/auth/register") and not a.limiter.allow(who):
            raise ApiError("rate_limited", "Слишком много попыток. Подождите минуту", 429)
        if route == "/api/auth/logout":
            a.sessions.revoke(self._token())
            self._set_cookie("", 0)
            return self._send(200, {"required": True, "account": None})
        if route == "/api/auth/password":
            acct = self._account()
            if not acct:
                raise ApiError("unauthorized", "Войдите в кабинет", 401)
            a.store.change_password(acct["id"], body.get("old", ""), body.get("new", ""))
            a.sessions.revoke_account(acct["id"])  # старые входы с других устройств закрываются
        elif route == "/api/auth/register":
            acct = a.store.register(body.get("email", ""), body.get("password", ""))
        elif route == "/api/auth/login":
            acct = a.store.login(body.get("email", ""), body.get("password", ""))
        else:
            raise ApiError("not_found", "Нет такого метода", 404)
        a.sessions.revoke(self._token())  # новый вход — новый токен (защита от подмены сессии)
        self._set_cookie(a.sessions.create(acct["id"]), a.sessions.ttl)
        self._send(200, {"required": True, "account": a.store.public(acct)})

    def _api_auth(self, method: str) -> None:
        who = self._client()
        route = self.path.split("?")[0]
        if not self.limiter.allow(who):
            raise ApiError("rate_limited", "Слишком много запросов. Подождите минуту", 429)
        if method == "POST":
            self._check_origin()
        body = self._body() if method == "POST" else {}
        if route.startswith("/api/auth/"):
            return self._auth_route(method, route, body, who)
        acct = self._account()
        if acct is None:
            raise ApiError("unauthorized", "Войдите в кабинет", 401)
        if (route.endswith("/chat") and not self.chat_limiter.allow(who)) or (route.endswith("/import") and not self.import_limiter.allow(who)):
            raise ApiError("rate_limited", "Слишком много запросов. Подождите минуту", 429)
        assistant, lock = self.hub.get(acct["id"], init=lambda a: a.restore(acct.get("profile"), acct.get("active"), acct.get("custom_files")))
        with lock:
            status, payload = dispatch(assistant, method, self.path, body)
            if status == 200 and route in PERSIST and not (route == "/api/import" and body.get("dry_run")):
                self.auth.store.update(acct["id"], **assistant.snapshot())
        self._send(status, payload)

    # --- маршруты ---
    def _api(self, method: str) -> None:
        try:
            if self.auth:
                return self._api_auth(method)
            who = self._client()
            if (not self.limiter.allow(who) or (self.path.endswith("/chat") and not self.chat_limiter.allow(who))
                    or (self.path.endswith("/import") and not self.import_limiter.allow(who))):
                raise ApiError("rate_limited", "Слишком много запросов. Подождите минуту", 429)
            body = self._body() if method == "POST" else {}
            assistant, lock = self.hub.get(self._sid())
            with lock:
                status, payload = dispatch(assistant, method, self.path, body)
            self._send(status, payload)
        except ApiError as err:
            self._send(err.status, err.to_dict())
        except Exception:  # не раскрываем внутренности клиенту
            self._send(500, {"error": {"code": "internal", "message": "Внутренняя ошибка"}})

    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/"):
            return self._api("GET")
        route = self.path.split("?")[0]
        if route not in PUBLIC_PAGES:
            return self._send(404, {"error": {"code": "not_found", "message": "Нет такой страницы"}})
        self.path = "/" + PUBLIC_PAGES[route]
        self._static = True
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass  # клиент закрыл страницу, не дождавшись ответа

    def do_POST(self) -> None:
        if self.path.startswith("/api/"):
            return self._api("POST")
        self._send(404, {"error": {"code": "not_found", "message": "Нет такого метода"}})


def make_server(source, host: str = "127.0.0.1", port: int = 8765, limit: int = 240, chat_limit: int = 40,
                auth: Optional[AuthState] = None) -> ThreadingHTTPServer:
    """source — готовый Assistant (одно общее состояние) или Hub (сессии)."""
    hub = source if isinstance(source, Hub) else Hub(lambda: source)
    handler = type("BoundHandler", (Handler,), {"hub": hub, "limiter": RateLimiter(limit), "chat_limiter": RateLimiter(chat_limit),
                                                  "import_limiter": RateLimiter(12), "auth": auth})
    server = ThreadingHTTPServer((host, port), functools.partial(handler, directory=str(WEB)))
    server.hub = hub
    return server


def main() -> None:
    loaded = load_env_file()
    if loaded:
        print("Из secrets.env прочитано:", ", ".join(loaded))  # только имена, значения не печатаем
    parser = argparse.ArgumentParser(description="Экран «3 дела» и API помощника")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--sessions", action="store_true", default=os.environ.get("SESSIONS") == "1",
                        help="у каждого посетителя своё состояние (для публичного хостинга)")
    parser.add_argument("--audit-file", help="дублировать журнал аудита в файл JSONL (только без --sessions)")
    parser.add_argument("--no-auth", action="store_true", default=os.environ.get("AUTH") == "0",
                        help="без кабинетов: все заходят как гости (по умолчанию вход по почте и паролю)")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR"), help="папка для кабинетов (по умолчанию data/ рядом с проектом)")
    parser.add_argument("--llm", choices=["demo", "off", "env"], default=os.environ.get("LLM_MODE", "demo"),
                        help="demo — имитация модели, off — только шаблоны, env — модель из LLM_BASE_URL/LLM_API_KEY/LLM_MODEL")
    args = parser.parse_args()

    audit_path = Path(args.audit_file) if args.audit_file and not args.sessions else None

    # дата демо: сегодняшняя; DEMO_DATE=ГГГГ-ММ-ДД фиксирует её для повторяемого показа
    demo_day = date.fromisoformat(os.environ["DEMO_DATE"]) if os.environ.get("DEMO_DATE") else date.today()

    def factory() -> Assistant:
        llm = {"demo": DemoLLM(), "off": None, "env": "env"}[args.llm]
        return Assistant(today=demo_day, audit_path=audit_path, llm=llm)

    auth = None
    if not args.no_auth:
        data_dir = Path(args.data_dir) if args.data_dir else (HERE.parent / "data" if (HERE.parent / "web").exists() else HERE / "data")
        auth = AuthState(AccountStore(data_dir / "accounts.json"), Sessions(), RateLimiter(20))
        audit_path = None
        print(f"Кабинеты включены: данные в {data_dir / 'accounts.json'}. Забыли пароль: python3 -m engine.accounts reset почта@example.com")
    hub = Hub(factory, sessions=args.sessions or auth is not None)
    server = make_server(hub, host=args.host, port=args.port, auth=auth)
    print({"demo": "ИИ: демо-режим (имитация, не настоящая модель)", "off": "ИИ выключен: только шаблоны",
           "env": "ИИ: модель из переменных окружения"}[args.llm])
    print("Режим:", "кабинеты (у каждого пользователя своё состояние)" if auth else "сессии (у каждого посетителя своё состояние)" if args.sessions else "общий (все видят одно состояние)")
    print(f"Открой http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}  (Ctrl+C — остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
