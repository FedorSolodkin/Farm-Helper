"""Журнал аудита: только добавление, каждая запись хранит хэш предыдущей.
Любая правка или удаление записи ломает цепочку, и verify() это покажет."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

GENESIS = "0" * 64
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, clock: Clock = utcnow, path: Optional[Path] = None):
        self.entries: List[dict] = []
        self._clock = clock
        self._path = Path(path) if path else None
        if self._path:
            self._path.write_text("", encoding="utf-8")

    def append(self, farmer: str, event: str, action: Optional[str] = None,
               change_hash: Optional[str] = None, details: Optional[dict] = None) -> dict:
        body = {
            "seq": len(self.entries) + 1,
            "ts": self._clock().isoformat(timespec="seconds"),
            "farmer": farmer,
            "event": event,
            "action": action,
            "change_hash": change_hash,
            "details": details or {},
            "prev": self.entries[-1]["hash"] if self.entries else GENESIS,
        }
        entry = {**body, "hash": _digest(body)}
        self.entries.append(entry)
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def verify(self) -> Tuple[bool, Optional[int]]:
        """(цепочка цела, номер первой испорченной записи)"""
        prev = GENESIS
        for i, entry in enumerate(self.entries, 1):
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry["seq"] != i or entry["prev"] != prev or _digest(body) != entry["hash"]:
                return False, i
            prev = entry["hash"]
        return True, None

    def for_farmer(self, farmer: str) -> List[dict]:
        return [e for e in self.entries if e["farmer"] == farmer]
