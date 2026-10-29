"""Кабинеты: почта и пароль, хранение на диске, сессии и защита от подбора пароля.

Пароль нигде не хранится и не пишется в журналы: на диске только PBKDF2-HMAC-SHA256 с отдельной солью на каждый
пароль. Файл с аккаунтами создаётся с правами только для владельца. Письма пока не отправляются, поэтому
сброс пароля делается командой на этом же компьютере:

    python3 -m engine.accounts list
    python3 -m engine.accounts reset почта@example.com
    python3 -m engine.accounts delete почта@example.com
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .errors import ApiError

ITERATIONS = 600_000  # рекомендация OWASP для PBKDF2-SHA256; тесты временно снижают
MIN_PASSWORD, MAX_PASSWORD = 8, 128
MAX_ACCOUNTS = 500
SESSION_TTL = 7 * 24 * 3600
MAX_FAILS, FAIL_WINDOW, LOCK_SECONDS = 5, 900, 300  # 5 неверных попыток за 15 минут — блокировка на 5 минут
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,}$")
WEAK = {"12345678", "123456789", "1234567890", "password", "qwertyui", "qwerty123", "11111111", "00000000", "йцукенгш", "password1", "iloveyou"}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, iterations: Optional[int] = None) -> str:
    it = iterations or ITERATIONS
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, it)
    return f"pbkdf2_sha256${it}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, it, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        test = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(it))
        return hmac.compare_digest(test, base64.b64decode(digest))
    except Exception:
        return False


_dummy: Optional[str] = None


def _dummy_hash() -> str:
    """Для неизвестной почты тоже считаем хэш: по времени ответа нельзя понять, есть ли такой кабинет."""
    global _dummy
    if _dummy is None or int(_dummy.split("$")[1]) != ITERATIONS:
        _dummy = hash_password("не-существует")
    return _dummy


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ApiError("bad_email", "Проверьте почту: она должна выглядеть как имя@почта.ру", 400)
    return email


def check_password(password: str, email: str = "") -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ApiError("weak_password", f"Пароль короче {MIN_PASSWORD} символов", 400)
    if len(password) > MAX_PASSWORD:
        raise ApiError("weak_password", f"Пароль длиннее {MAX_PASSWORD} символов", 400)
    if password.lower() in WEAK or len(set(password)) < 4 or (email and password.lower() == email):
        raise ApiError("weak_password", "Слишком простой пароль: добавьте разные буквы и цифры", 400)


class AccountStore:
    """Аккаунты в одном JSON-файле: запись атомарная (временный файл → замена), доступ под замком."""

    def __init__(self, path: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._accounts: Dict[str, dict] = {}
        self._fails: Dict[str, List[float]] = {}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            if self.path.exists():
                self._accounts = json.loads(self.path.read_text(encoding="utf-8")).get("accounts", {})

    # --- хранение ---
    def _save(self) -> None:
        if not self.path:
            return
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".accounts-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"accounts": self._accounts}, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def public(acct: dict) -> dict:
        return {"id": acct["id"], "email": acct["email"], "created": acct["created"]}

    def _by_email(self, email: str) -> Optional[dict]:
        return next((a for a in self._accounts.values() if a["email"] == email), None)

    # --- операции ---
    def register(self, email: str, password: str) -> dict:
        email = normalize_email(email)
        check_password(password, email)
        with self._lock:
            if self._by_email(email):
                raise ApiError("email_taken", "Эта почта уже зарегистрирована: войдите или используйте другую", 409)
            if len(self._accounts) >= MAX_ACCOUNTS:
                raise ApiError("limit", "Достигнут предел числа кабинетов на этом компьютере", 409)
            acct_id = secrets.token_hex(6)
            acct = {"id": acct_id, "email": email, "pw": hash_password(password), "created": time.strftime("%Y-%m-%d", time.gmtime(self._clock())),
                    "profile": None, "active": None, "custom_files": None}
            self._accounts[acct_id] = acct
            self._save()
            return acct

    def login(self, email: str, password: str) -> dict:
        email = normalize_email(email) if email else ""
        now = self._clock()
        with self._lock:
            fails = [t for t in self._fails.get(email, []) if now - t < FAIL_WINDOW]
            self._fails[email] = fails
            if len(fails) >= MAX_FAILS and now - fails[-1] < LOCK_SECONDS:
                wait = int((LOCK_SECONDS - (now - fails[-1])) / 60) + 1
                raise ApiError("locked", f"Слишком много неверных попыток. Подождите {wait} мин и попробуйте снова", 429)
            acct = self._by_email(email)
            ok = verify_password(password if isinstance(password, str) else "", acct["pw"] if acct else _dummy_hash())
            if not (acct and ok):
                fails.append(now)
                raise ApiError("bad_credentials", "Неверная почта или пароль", 401)
            self._fails.pop(email, None)
            return acct

    def get(self, acct_id: str) -> Optional[dict]:
        with self._lock:
            return self._accounts.get(acct_id)

    def update(self, acct_id: str, **fields) -> None:
        with self._lock:
            if acct_id in self._accounts:
                self._accounts[acct_id].update(fields)
                self._save()

    def change_password(self, acct_id: str, old: str, new: str) -> None:
        with self._lock:
            acct = self._accounts.get(acct_id)
            if not acct or not verify_password(old if isinstance(old, str) else "", acct["pw"]):
                raise ApiError("bad_credentials", "Текущий пароль неверный", 401)
            check_password(new, acct["email"])
            acct["pw"] = hash_password(new)
            self._save()

    def reset_password(self, email: str, new: str) -> bool:
        with self._lock:
            acct = self._by_email(email.strip().lower())
            if not acct:
                return False
            check_password(new, acct["email"])
            acct["pw"] = hash_password(new)
            self._fails.pop(acct["email"], None)
            self._save()
            return True

    def delete(self, email: str) -> bool:
        with self._lock:
            acct = self._by_email(email.strip().lower())
            if not acct:
                return False
            del self._accounts[acct["id"]]
            self._save()
            return True

    def emails(self) -> List[str]:
        with self._lock:
            return sorted(a["email"] for a in self._accounts.values())


class Sessions:
    """Сессии входа: в памяти хранится только хэш токена, поэтому утечка памяти не даёт войти."""

    def __init__(self, ttl: int = SESSION_TTL, clock: Callable[[], float] = time.time):
        self.ttl, self._clock = ttl, clock
        self._items: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, acct_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[self._key(token)] = (acct_id, self._clock() + self.ttl)
        return token

    def resolve(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        now = self._clock()
        with self._lock:
            for k in [k for k, (_, exp) in self._items.items() if exp < now]:
                del self._items[k]
            item = self._items.get(self._key(token))
            return item[0] if item else None

    def revoke(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._items.pop(self._key(token), None)

    def revoke_account(self, acct_id: str) -> None:
        with self._lock:
            for k in [k for k, (a, _) in self._items.items() if a == acct_id]:
                del self._items[k]


# ---------- команды администратора ----------

def _default_path() -> Path:
    here = Path(__file__).resolve().parent
    base = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else (here.parent / "data" if (here.parent / "web").exists() else here / "data")
    return base / "accounts.json"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    store = AccountStore(_default_path())
    if not argv or argv[0] not in ("list", "reset", "delete") or (argv[0] != "list" and len(argv) < 2):
        print(__doc__)
        return 2
    if argv[0] == "list":
        emails = store.emails()
        print("\n".join(emails) if emails else "Кабинетов пока нет")
        return 0
    if argv[0] == "delete":
        print("Кабинет удалён" if store.delete(argv[1]) else "Такой почты нет")
        return 0
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    new = "".join(secrets.choice(alphabet) for _ in range(12))
    if not store.reset_password(argv[1], new):
        print("Такой почты нет")
        return 1
    print(f"Новый пароль для {argv[1]}: {new}\nСразу войдите и смените его в кабинете.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
