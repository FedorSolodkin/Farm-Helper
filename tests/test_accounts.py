"""Кабинеты: пароли, хранение на диске, сессии, защита от подбора и вход через сервер."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from engine import accounts
from engine.accounts import AccountStore, Sessions, check_password, hash_password, normalize_email, verify_password
from engine.errors import ApiError
from engine.generate import build_farm
from engine.hub import Hub, RateLimiter
from engine.importer import export_example
from engine.llm import DemoLLM
from engine.server import AuthState, make_server
from engine.service import Assistant

GOOD = "Mnogo-Moloka-77"


class Fast(unittest.TestCase):
    """Пароли в тестах хэшируются за миллисекунды; в работе число итераций — 600 000."""

    def setUp(self):
        patcher = mock.patch.object(accounts, "ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)


class Passwords(Fast):
    def test_hash_is_salted_and_verifiable(self):
        a, b = hash_password(GOOD), hash_password(GOOD)
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("pbkdf2_sha256$1000$"))
        self.assertTrue(verify_password(GOOD, a) and verify_password(GOOD, b))
        self.assertFalse(verify_password("другой-пароль-1", a))
        self.assertNotIn(GOOD, a)

    def test_broken_hashes_never_verify(self):
        for bad in ("", "x", "md5$1$a$b", "pbkdf2_sha256$x$y$z", None):
            self.assertFalse(verify_password(GOOD, bad))

    def test_production_strength(self):
        source = pathlib.Path(accounts.__file__).read_text(encoding="utf-8")
        self.assertIn("ITERATIONS = 600_000", source)  # в работе — рекомендуемое число итераций, а не тестовое

    def test_rules(self):
        for bad in ("short1", "12345678", "aaaaaaaaaa", "password", "x" * 200):
            with self.assertRaises(ApiError, msg=bad):
                check_password(bad)
        with self.assertRaises(ApiError):
            check_password("pochta@example.com", "pochta@example.com")
        check_password(GOOD)
        self.assertEqual(normalize_email("  Pochta@Example.COM "), "pochta@example.com")
        for bad in ("", "a", "a@b", "a b@c.ru", "@c.ru", "a@@c.ru"):
            with self.assertRaises(ApiError, msg=bad):
                normalize_email(bad)


class Store(Fast):
    def setUp(self):
        super().setUp()
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "data" / "accounts.json"
        self.clock = [1000.0]
        self.store = AccountStore(self.path, clock=lambda: self.clock[0])

    def test_register_login_and_case_insensitive_email(self):
        acct = self.store.register("Anna@Example.com", GOOD)
        self.assertEqual(self.store.login("anna@example.COM", GOOD)["id"], acct["id"])
        with self.assertRaises(ApiError) as cm:
            self.store.register("ANNA@example.com", GOOD)
        self.assertEqual(cm.exception.code, "email_taken")

    def test_wrong_and_unknown_look_the_same(self):
        self.store.register("anna@example.com", GOOD)
        errors = []
        for email in ("anna@example.com", "nobody@example.com"):
            with self.assertRaises(ApiError) as cm:
                self.store.login(email, "неверный-пароль-9")
            errors.append((cm.exception.code, cm.exception.message, cm.exception.status))
        self.assertEqual(errors[0], errors[1])

    def test_lockout_after_five_failures_and_recovery(self):
        self.store.register("anna@example.com", GOOD)
        for _ in range(5):
            with self.assertRaises(ApiError) as cm:
                self.store.login("anna@example.com", "неверный-пароль-9")
            self.assertEqual(cm.exception.code, "bad_credentials")
        with self.assertRaises(ApiError) as cm:  # даже верный пароль в блокировку не пускает
            self.store.login("anna@example.com", GOOD)
        self.assertEqual((cm.exception.code, cm.exception.status), ("locked", 429))
        self.clock[0] += accounts.LOCK_SECONDS + 1
        self.assertTrue(self.store.login("anna@example.com", GOOD))

    def test_success_resets_the_counter(self):
        self.store.register("anna@example.com", GOOD)
        for _ in range(4):
            with self.assertRaises(ApiError):
                self.store.login("anna@example.com", "неверный-пароль-9")
        self.store.login("anna@example.com", GOOD)
        with self.assertRaises(ApiError) as cm:
            self.store.login("anna@example.com", "неверный-пароль-9")
        self.assertEqual(cm.exception.code, "bad_credentials")

    def test_data_survives_restart_and_is_private(self):
        acct = self.store.register("anna@example.com", GOOD)
        self.store.update(acct["id"], profile={"owner": "Анна"})
        again = AccountStore(self.path)
        self.assertEqual(again.login("anna@example.com", GOOD)["profile"], {"owner": "Анна"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(GOOD, raw)
        self.assertEqual(oct(self.path.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(self.path.parent.stat().st_mode)[-3:], "700")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["accounts.json"])  # временных файлов не остаётся

    def test_change_and_reset_password(self):
        acct = self.store.register("anna@example.com", GOOD)
        with self.assertRaises(ApiError):
            self.store.change_password(acct["id"], "не-тот-пароль-1", "Novyi-Parol-55")
        self.store.change_password(acct["id"], GOOD, "Novyi-Parol-55")
        with self.assertRaises(ApiError):
            self.store.login("anna@example.com", GOOD)
        self.store.login("anna@example.com", "Novyi-Parol-55")
        self.assertTrue(self.store.reset_password("anna@example.com", "Sbros-Parol-12"))
        self.store.login("anna@example.com", "Sbros-Parol-12")
        self.assertFalse(self.store.reset_password("nobody@example.com", "Sbros-Parol-12"))
        self.assertTrue(self.store.delete("anna@example.com"))
        self.assertEqual(self.store.emails(), [])

    def test_account_cap(self):
        with mock.patch.object(accounts, "MAX_ACCOUNTS", 2):
            self.store.register("a@example.com", GOOD)
            self.store.register("b@example.com", GOOD)
            with self.assertRaises(ApiError) as cm:
                self.store.register("c@example.com", GOOD)
            self.assertEqual(cm.exception.code, "limit")

    def test_command_line(self):
        self.store.register("anna@example.com", GOOD)
        with mock.patch.object(accounts, "_default_path", return_value=self.path):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(accounts.main(["list"]), 0)
            self.assertIn("anna@example.com", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(accounts.main(["reset", "anna@example.com"]), 0)
            new = out.getvalue().split(": ")[1].split("\n")[0]
            self.assertGreaterEqual(len(new), 12)
            self.assertTrue(AccountStore(self.path).login("anna@example.com", new))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(accounts.main(["reset", "nobody@example.com"]), 1)
                self.assertEqual(accounts.main([]), 2)


class SessionTokens(unittest.TestCase):
    def test_lifecycle(self):
        now = [0.0]
        s = Sessions(ttl=100, clock=lambda: now[0])
        token = s.create("acct1")
        self.assertEqual(s.resolve(token), "acct1")
        self.assertNotIn(token, json.dumps(list(s._items)))  # токен в памяти не хранится
        self.assertIsNone(s.resolve("чужой-токен"))
        self.assertIsNone(s.resolve(None))
        now[0] = 101
        self.assertIsNone(s.resolve(token))
        t2, t3 = s.create("a"), s.create("a")
        s.revoke(t2)
        self.assertIsNone(s.resolve(t2))
        s.revoke_account("a")
        self.assertIsNone(s.resolve(t3))


class AssistantSnapshot(unittest.TestCase):
    def test_round_trip_with_uploaded_data(self):
        a = Assistant(secret=b"s", llm=DemoLLM())
        a.import_data(export_example(build_farm("cheese")), {"owner": "Анна", "farm_name": "Моя"})
        snap = json.loads(json.dumps(a.snapshot()))
        b = Assistant(secret=b"s", llm=DemoLLM())
        b.restore(snap["profile"], snap["active"], snap["custom_files"])
        self.assertEqual((b.active, b.profile["farm_name"]), ("mine", "Моя"))
        self.assertEqual([x["key"] for x in b.today_view("mine")["actions"]], [x["key"] for x in a.today_view("mine")["actions"]])

    def test_broken_snapshot_falls_back_to_empty(self):
        b = Assistant(secret=b"s", llm=DemoLLM())
        b.restore({"owner": "А", "farm_name": "Б"}, "mine", {"products": "мусор"})
        self.assertIsNone(b.active)
        self.assertNotIn("mine", b.farms)


class Web(Fast):
    def start(self, data_dir, limit=200):
        auth = AuthState(AccountStore(data_dir / "accounts.json"), Sessions(), RateLimiter(limit))
        hub = Hub(lambda: Assistant(secret=b"w", llm=DemoLLM()), sessions=True)
        server = make_server(hub, port=0, auth=auth)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return f"http://127.0.0.1:{server.server_address[1]}"

    def setUp(self):
        super().setUp()
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.base = self.start(self.dir)

    def call(self, path, body=None, cookie=None, headers=None, base=None):
        h = {"Content-Type": "application/json", "X-Forwarded-For": "4.4.4.4", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request((base or self.base) + path, method="POST" if data is not None else "GET", data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def register(self, email="anna@example.com", base=None):
        st, body, h = self.call("/api/auth/register", {"email": email, "password": GOOD}, base=base)
        self.assertEqual(st, 200, body)
        return h["Set-Cookie"].split(";")[0], h["Set-Cookie"]

    def test_api_needs_login_but_page_and_health_do_not(self):
        self.assertEqual(self.call("/api/farms")[0], 401)
        self.assertEqual(self.call("/api/farms/cheese/today")[0], 401)
        self.assertEqual(self.call("/api/farms/cheese/chat", {"text": "привет"})[0], 401)
        self.assertEqual(self.call("/healthz")[0], 200)
        self.assertEqual(self.call("/api/auth/me")[1], {"required": True, "account": None})
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("Создайте кабинет", r.read().decode("utf-8"))

    def test_cookie_is_httponly_lax_and_secure_behind_https(self):
        _, raw = self.register()
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Lax", raw)
        self.assertNotIn("Secure", raw)
        _, _, h = self.call("/api/auth/register", {"email": "b@example.com", "password": GOOD}, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", h["Set-Cookie"])

    def test_register_login_logout(self):
        ck, _ = self.register()
        me = self.call("/api/auth/me", cookie=ck)[1]
        self.assertEqual(me["account"]["email"], "anna@example.com")
        self.assertNotIn("pw", json.dumps(me))
        self.assertEqual(self.call("/api/farms", cookie=ck)[0], 200)
        st, _, h = self.call("/api/auth/logout", {}, cookie=ck)
        self.assertEqual(st, 200)
        self.assertIn("Max-Age=0", h["Set-Cookie"])
        self.assertEqual(self.call("/api/farms", cookie=ck)[0], 401)  # старый токен больше не работает
        st, body, h = self.call("/api/auth/login", {"email": "ANNA@example.com", "password": GOOD})
        self.assertEqual(st, 200)
        self.assertEqual(self.call("/api/farms", cookie=h["Set-Cookie"].split(";")[0])[0], 200)
        self.assertEqual(self.call("/api/auth/login", {"email": "anna@example.com", "password": "неверный-пароль-9"})[0], 401)

    def test_profile_and_uploaded_data_survive_a_server_restart(self):
        ck, _ = self.register()
        self.assertEqual(self.call("/api/register", {"owner": "Анна", "farm_name": "Х", "demo_key": "veg"}, cookie=ck)[0], 200)
        st, body, _ = self.call("/api/import", {"files": export_example(build_farm("cheese")), "profile": {"owner": "Анна", "farm_name": "Моя ферма"}}, cookie=ck)
        self.assertTrue(body["imported"])
        base2 = self.start(self.dir)  # «перезапуск»: новый сервер, те же файлы
        st, body, h = self.call("/api/auth/login", {"email": "anna@example.com", "password": GOOD}, base=base2)
        self.assertEqual(st, 200)
        ck2 = h["Set-Cookie"].split(";")[0]
        prof = self.call("/api/profile", cookie=ck2, base=base2)[1]
        self.assertEqual((prof["profile"]["farm_name"], prof["active"], prof["has_custom"]), ("Моя ферма", "mine", True))
        self.assertEqual(len(self.call("/api/farms/mine/today", cookie=ck2, base=base2)[1]["actions"]), 3)

    def test_dry_run_is_not_saved_and_accounts_are_isolated(self):
        a, _ = self.register("a@example.com")
        b, _ = self.register("b@example.com")
        self.call("/api/import", {"files": export_example(build_farm("cheese")), "profile": {"owner": "А", "farm_name": "Ф"}, "dry_run": True}, cookie=a)
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))["accounts"]
        self.assertTrue(all(x["custom_files"] is None for x in saved.values()))
        self.call("/api/register", {"owner": "А", "farm_name": "Ф", "demo_key": "honey"}, cookie=a)
        self.assertIsNone(self.call("/api/profile", cookie=b)[1]["profile"])
        self.assertEqual(self.call("/api/profile", cookie=a)[1]["active"], "honey")

    def test_cross_site_and_wrong_content_type_are_refused(self):
        ck, _ = self.register()
        st, body, _ = self.call("/api/farms/cheese/dismiss", {"key": "x", "reason": "later"}, cookie=ck, headers={"Origin": "http://evil.example"})
        self.assertEqual((st, body["error"]["code"]), (403, "forbidden"))
        host = self.base.split("//")[1]
        self.assertNotEqual(self.call("/api/farms/cheese/dismiss", {"key": "x", "reason": "later"}, cookie=ck, headers={"Origin": "http://" + host})[0], 403)
        req = urllib.request.Request(self.base + "/api/auth/login", method="POST", data=b'{"email":"a","password":"b"}',
                                     headers={"Content-Type": "text/plain", "X-Forwarded-For": "4.4.4.4"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 415)

    def test_login_attempts_are_limited_per_address(self):
        base = self.start(pathlib.Path(tempfile.mkdtemp()), limit=200)
        codes = [self.call("/api/auth/login", {"email": "x@example.com", "password": "неверный-пароль-9"}, base=base,
                           headers={"X-Forwarded-For": "7.7.7.7"})[0] for _ in range(24)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 401)

    def test_password_change_closes_other_sessions(self):
        ck1, _ = self.register()
        _, _, h = self.call("/api/auth/login", {"email": "anna@example.com", "password": GOOD})
        ck2 = h["Set-Cookie"].split(";")[0]
        st, _, h = self.call("/api/auth/password", {"old": GOOD, "new": "Novyi-Parol-55"}, cookie=ck2)
        self.assertEqual(st, 200)
        self.assertEqual(self.call("/api/farms", cookie=ck1)[0], 401)
        self.assertEqual(self.call("/api/farms", cookie=h["Set-Cookie"].split(";")[0])[0], 200)
        self.assertEqual(self.call("/api/auth/login", {"email": "anna@example.com", "password": GOOD})[0], 401)
        self.assertEqual(self.call("/api/auth/password", {"old": "x", "new": "y"})[0], 401)

    def test_weak_password_and_duplicate_email_messages(self):
        st, body, _ = self.call("/api/auth/register", {"email": "a@example.com", "password": "12345678"})
        self.assertEqual((st, body["error"]["code"]), (400, "weak_password"))
        self.register("a@example.com")
        st, body, _ = self.call("/api/auth/register", {"email": "A@example.com", "password": GOOD})
        self.assertEqual((st, body["error"]["code"]), (409, "email_taken"))


class Screens(unittest.TestCase):
    def test_login_and_tour_are_in_the_page(self):
        with open("web/index.html", encoding="utf-8") as fh:
            html = fh.read()
        for text in ("Создайте кабинет", "Войдите в кабинет", "Как это работает", "Понятно, начать", "Каждый день три дела", "Решаете вы"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
