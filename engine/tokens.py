"""Одноразовый токен подтверждения.

Подписан HMAC, привязан к фермеру, действию и хэшу конкретного изменения, живёт 10 минут и
срабатывает один раз. Если изменение поменялось, старый токен к нему не подойдёт."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Set, Tuple

from .audit import Clock, utcnow
from .errors import ApiError

TOKEN_TTL_SECONDS = 600


def change_hash(change: dict) -> str:
    raw = json.dumps(change, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TokenService:
    def __init__(self, secret: bytes = None, clock: Clock = utcnow, ttl: int = TOKEN_TTL_SECONDS):
        self._secret = secret or secrets.token_bytes(32)
        self._clock = clock
        self.ttl = ttl
        self._used: Set[str] = set()

    def _sign(self, raw: str) -> str:
        return _b64(hmac.new(self._secret, raw.encode("ascii"), hashlib.sha256).digest())

    def issue(self, farmer_id: str, action_key: str, h: str) -> Tuple[str, datetime, str]:
        exp = int(self._clock().timestamp()) + self.ttl
        payload = {"fid": farmer_id, "act": action_key, "h": h, "exp": exp, "jti": uuid.uuid4().hex[:16]}
        raw = _b64(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return f"{raw}.{self._sign(raw)}", datetime.fromtimestamp(exp, tz=timezone.utc), payload["jti"]

    def consume(self, token: str, farmer_id: str, action_key: str, h: str) -> str:
        """Проверяет токен и гасит его. Возвращает id токена или бросает ApiError."""
        try:
            raw, sig = token.split(".")
            payload = json.loads(_unb64(raw))
        except Exception:
            raise ApiError("token_malformed", "Подтверждение повреждено", 403)
        if not hmac.compare_digest(sig, self._sign(raw)):
            raise ApiError("token_bad_signature", "Подпись подтверждения неверна", 403)
        if self._clock().timestamp() > payload["exp"]:
            raise ApiError("token_expired", "Подтверждение истекло (10 минут). Подтвердите ещё раз", 410)
        if payload["fid"] != farmer_id:
            raise ApiError("token_wrong_farmer", "Подтверждение выдано другому пользователю", 403)
        if payload["act"] != action_key or payload["h"] != h:
            raise ApiError("token_wrong_change", "Изменение отличается от того, что вы подтверждали", 409)
        if payload["jti"] in self._used:
            raise ApiError("token_used", "Это подтверждение уже использовано", 409)
        self._used.add(payload["jti"])
        return payload["jti"]
