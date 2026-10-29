"""Адаптеры языковой модели. Движок от них не зависит: без модели работают шаблоны.

  OpenAICompatible — любой сервер с /chat/completions (open-source в закрытом контуре банка, GigaChat или
                     YandexGPT через совместимый шлюз). Настраивается переменными LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.
  DemoLLM          — имитация модели для показа без сети и ключей; умеет «ломаться» по команде, чтобы
                     показать, как фактчекер и модерация ловят выдумки. Это не настоящая модель.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Mapping, Optional



class LLMError(Exception):
    """Модель недоступна, ответила ошибкой или не уложилась в таймаут."""


class OpenAICompatible:
    name = "llm"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 8.0, temperature: float = 0.2,
                 project: str = "", auth_scheme: str = "Bearer"):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.timeout, self.temperature = timeout, temperature
        self.project, self.auth_scheme = project, auth_scheme

    def cache_token(self) -> str:
        return self.model

    def complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model, "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"{self.auth_scheme} {self.api_key}"}
        if self.project:  # Yandex AI Studio: идентификатор каталога
            headers["OpenAI-Project"] = self.project
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(str(exc)) from exc


class DemoLLM:
    name = "demo"
    MODES = {
        "good": "работает штатно",
        "hallucinate": "выдумывает числа",
        "unsafe": "пишет запрещённые обещания",
        "down": "недоступен",
    }

    def __init__(self, mode: str = "good"):
        self.mode = mode

    def cache_token(self) -> str:
        return self.mode

    def complete(self, system: str, user: str) -> str:
        if self.mode == "down":
            raise LLMError("демо: модель недоступна")
        facts = json.loads(user.split("\n", 1)[0])
        if "[TASK:DRAFT]" in system:
            return json.dumps(self._draft(facts), ensure_ascii=False)
        return self._explain(facts)

    def _explain(self, f: dict) -> str:
        why = [w.rstrip(".") for w in f["why"]]
        text = (f"Коротко: {why[0]}. " + (f"{why[1]}. " if len(why) > 1 else "") +
                f"Если сделать это сегодня, ожидаем около {f['effect']['mid']} за неделю.")
        if self.mode == "hallucinate":
            text += " Это на 43% больше, чем у ваших соседей по рынку, и может принести до 99 900 ₽."
        if self.mode == "unsafe":
            text += " Результат гарантирован, вы точно заработаете."
        return text

    def _draft(self, f: dict) -> dict:
        text = (f"{f['name']} — [опишите вкус и аромат]. Состав: [укажите состав]. Вес: [укажите вес упаковки]. "
                f"Срок годности: {f['shelf_life_days']} дн. Подойдёт [с чем подавать].")
        if self.mode == "unsafe":
            text = f"Самый лучший органический {f['name']}: лечебный, укрепляет иммунитет, без ГМО."
        if self.mode == "hallucinate":
            text += " Срок годности 45 дней, вес 2 кг."
        return {"title": f["name"], "description": text}


def provider_from_env(env: Mapping[str, str] = os.environ, default: str = "demo"):
    """LLM_BASE_URL → настоящая модель; LLM_PROVIDER=demo|off → демо или только шаблоны."""
    if env.get("LLM_BASE_URL"):
        return OpenAICompatible(env["LLM_BASE_URL"], env.get("LLM_API_KEY", ""), env.get("LLM_MODEL", "default"),
                                float(env.get("LLM_TIMEOUT", "8")), project=env.get("LLM_PROJECT", ""),
                                auth_scheme=env.get("LLM_AUTH_SCHEME", "Bearer"))
    kind = env.get("LLM_PROVIDER", default)
    return DemoLLM() if kind == "demo" else None
