"""Единая ошибка слоя исполнения: код для логики, сообщение для фермера, HTTP-статус для API."""
from __future__ import annotations

from typing import Optional


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.details}}
