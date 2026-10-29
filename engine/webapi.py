"""Точка входа для браузера (Pyodide): тот же движок и те же маршруты, но без сервера.
Состояние живёт в странице и сбрасывается при обновлении. ИИ работает в демо-режиме: ключей в браузере нет."""
from __future__ import annotations

import json
from datetime import date

from .api import dispatch
from .llm import DemoLLM
from .service import Assistant

_assistant = None


def handle(method: str, path: str, body_json: str = "") -> str:
    global _assistant
    if _assistant is None:
        _assistant = Assistant(today=date.today(), llm=DemoLLM())
    body = json.loads(body_json) if body_json else None
    status, payload = dispatch(_assistant, method, path, body)
    return json.dumps({"status": status, "payload": payload}, ensure_ascii=False)
