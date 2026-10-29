"""Чат, параметры дела, сессии, лимиты и Telegram-бот.
Запуск: python3 -m unittest discover -s tests -t .
"""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from engine.errors import ApiError
from engine.hub import Hub, RateLimiter
from engine.llm import DemoLLM
from engine.server import make_server
from engine.service import Assistant
from engine.signals import spoilage_net


def snapshot(a, key="cheese"):
    farm = a.farms[key]
    return json.dumps({"p": [(p.id, p.price) for p in farm.products], "promos": [(p.id, str(p.end)) for p in farm.promos],
                       "state": farm.state, "audit": len(a.audit.entries)}, default=str, sort_keys=True)


class Scripted:
    name = "scripted"

    def __init__(self, reply):
        self.reply = reply

    def cache_token(self):
        return "s"

    def complete(self, system, user):
        return self.reply


class Chat(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=DemoLLM())

    def test_discount_what_if_uses_the_same_numbers_as_the_engine(self):
        r = self.a.chat("cheese", "а если скидка 15%?")
        farm = self.a.farms["cheese"]
        net = spoilage_net(farm, farm.product("tvorog"), 0.15)["net"]
        self.assertEqual(r["intent"], "whatif_discount")
        self.assertIn("+" + f"{round(net / 100) * 100:,.0f}".replace(",", " ") + " ₽", r["text"])
        self.assertEqual(r["apply"]["params"], {"discount": 0.15})
        self.assertEqual(r["apply"]["action_key"], "spoilage:tvorog")

    def test_what_if_at_recommended_discount_matches_the_card(self):
        view = self.a.today_view("cheese")["actions"][0]
        pct = view["title"].split("скидку ")[1].split("%")[0]
        r = self.a.chat("cheese", f"а если скидка {pct}%?")
        mid = view["effect"]["mid"]
        self.assertIn(f"{round(mid / 100) * 100:,.0f}".replace(",", " "), r["text"])

    def test_forbidden_discount_is_explained_not_offered(self):
        r = self.a.chat("cheese", "а если скидка 40%")
        self.assertIsNone(r["apply"])
        self.assertIn("лимит", r["text"])

    def test_product_name_in_any_case_is_understood(self):
        r = self.a.chat("cheese", "скидка 20% на творог")
        self.assertEqual(r["action_key"], "spoilage:tvorog")
        r = self.a.chat("cheese", "а на сулугуни скидка 10%?")
        self.assertIn("срок годности не поджимает", r["text"])

    def test_price_what_if(self):
        r = self.a.chat("honey", "а если цену 1100?")
        self.assertEqual(r["apply"]["params"], {"price": 1100.0})
        self.assertIn("1100", r["text"])
        self.assertIn("только для снижения", self.a.chat("honey", "цена 1300")["text"])

    def test_explanations_and_lists(self):
        self.assertIn("Почему:", self.a.chat("cheese", "почему творог?")["text"])
        self.assertIn("Эффект =", self.a.chat("cheese", "как посчитан эффект?")["text"])
        self.assertIn("Сегодня:", self.a.chat("cheese", "что сегодня делать")["text"])
        self.assertIn("в очереди", self.a.chat("cheese", "что ещё в очереди")["text"])
        self.assertIn("Отчёта пока нет", self.a.chat("cheese", "итоги прошлой недели")["text"])
        self.assertEqual(self.a.chat("cheese", "помощь")["intent"], "help")
        self.assertEqual(self.a.chat("cheese", "расскажи анекдот")["intent"], "unknown")
        self.assertEqual(self.a.chat("cheese", "сколько стоит доставка")["intent"], "unknown")

    def test_chat_never_changes_anything(self):
        before = snapshot(self.a)
        for q in ["подтверди", "да", "запусти скидку 25%", "а если скидка 10%?", "применить", "цена 900", "почему?"]:
            self.a.chat("cheese", q)
        self.assertEqual(snapshot(self.a), before)
        r = self.a.chat("cheese", "подтверди")
        self.assertEqual(r["intent"], "confirm")
        self.assertIn("Сам я ничего не меняю", r["text"])

    def test_report_after_rewind(self):
        v = self.a.today_view("cheese")["actions"][0]
        pv = self.a.preview("cheese", v["key"])
        self.a.execute("cheese", v["key"], self.a.confirm("cheese", v["key"], pv["change_hash"])["token"])
        self.a.rewind("cheese")
        self.assertIn("Прошлая неделя: выполнено 1", self.a.chat("cheese", "итоги")["text"])

    def test_empty_and_long_messages(self):
        with self.assertRaises(ApiError):
            self.a.chat("cheese", "   ")
        self.assertEqual(self.a.chat("cheese", "привет " + "а" * 5000)["intent"], "greeting")

    def test_model_answers_outside_the_rules_are_checked(self):
        good = Assistant(secret=b"s", llm=Scripted("Остаток творога 34 кг, срок истекает 29.10."))
        r = good.chat("cheese", "расскажи про остатки")
        self.assertEqual((r["intent"], r["text"]), ("llm", "Остаток творога 34 кг, срок истекает 29.10."))
        liar = Assistant(secret=b"s", llm=Scripted("Продайте 777 кг и заработаете 99 900 ₽."))
        self.assertEqual(liar.chat("cheese", "расскажи про остатки")["intent"], "unknown")
        unsafe = Assistant(secret=b"s", llm=Scripted("Результат гарантирован."))
        self.assertEqual(unsafe.chat("cheese", "расскажи про остатки")["intent"], "unknown")


class Params(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=None)

    def test_custom_discount_changes_hash_title_and_effect(self):
        base = self.a.preview("cheese", "spoilage:tvorog")
        low = self.a.preview("cheese", "spoilage:tvorog", {"discount": 0.10})
        self.assertNotEqual(base["change_hash"], low["change_hash"])
        self.assertIn("10%", low["title"])
        self.assertEqual(low["adjustable"]["value"], 0.10)
        self.assertEqual(low["params"], {"discount": 0.10})

    def test_confirmed_params_are_what_gets_executed(self):
        p = {"discount": 0.15}
        pv = self.a.preview("cheese", "spoilage:tvorog", p)
        tok = self.a.confirm("cheese", "spoilage:tvorog", pv["change_hash"], params=p)["token"]
        self.a.execute("cheese", "spoilage:tvorog", tok, params=p)
        promo = self.a.farms["cheese"].active_promo("tvorog")
        self.assertEqual(promo.discount, 0.15)

    def test_token_for_one_discount_does_not_work_for_another(self):
        pv = self.a.preview("cheese", "spoilage:tvorog", {"discount": 0.15})
        tok = self.a.confirm("cheese", "spoilage:tvorog", pv["change_hash"], params={"discount": 0.15})["token"]
        with self.assertRaises(ApiError) as cm:
            self.a.execute("cheese", "spoilage:tvorog", tok, params={"discount": 0.30})
        self.assertEqual(cm.exception.code, "token_wrong_change")
        self.assertIsNone(self.a.farms["cheese"].active_promo("tvorog"))

    def test_stop_rules_apply_to_custom_params(self):
        with self.assertRaises(ApiError) as cm:
            self.a.preview("cheese", "spoilage:tvorog", {"discount": 0.40})
        self.assertEqual((cm.exception.code, cm.exception.status), ("blocked", 422))
        for bad in ({"discount": "abc"}, {"discount": 1.5}, {"discount": 0}):
            with self.assertRaises(ApiError):
                self.a.preview("cheese", "spoilage:tvorog", bad)

    def test_params_are_rejected_where_they_make_no_sense(self):
        with self.assertRaises(ApiError) as cm:
            self.a.preview("cheese", "stockout:mozzarella", {"discount": 0.1})
        self.assertEqual(cm.exception.code, "bad_params")

    def test_custom_price(self):
        pv = self.a.preview("honey", "price_off:lipa", {"price": 1100})
        tok = self.a.confirm("honey", "price_off:lipa", pv["change_hash"], params={"price": 1100})["token"]
        self.a.execute("honey", "price_off:lipa", tok, params={"price": 1100})
        self.assertEqual(self.a.farms["honey"].product("lipa").price, 1100)

    def test_price_below_cost_is_blocked(self):
        with self.assertRaises(ApiError):
            self.a.preview("honey", "price_off:lipa", {"price": 300})


class Sessions(unittest.TestCase):
    def test_shared_hub_gives_one_assistant(self):
        hub = Hub(lambda: Assistant(llm=None), sessions=False)
        self.assertIs(hub.get("a")[0], hub.get("b")[0])

    def test_session_hub_isolates_visitors(self):
        hub = Hub(lambda: Assistant(llm=None), sessions=True)
        a, b = hub.get("a")[0], hub.get("b")[0]
        self.assertIsNot(a, b)
        self.assertIs(a, hub.get("a")[0])

    def test_oldest_session_is_evicted(self):
        hub = Hub(lambda: Assistant(llm=None), sessions=True, max_sessions=2)
        first = hub.get("1")[0]
        hub.get("2")
        hub.get("3")
        self.assertEqual(len(hub), 2)
        self.assertIsNot(first, hub.get("1")[0])

    def test_idle_sessions_expire(self):
        hub = Hub(lambda: Assistant(llm=None), sessions=True, ttl=1)
        first = hub.get("1")[0]
        with mock.patch("engine.hub.time.time", return_value=time.time() + 5):
            self.assertIsNot(first, hub.get("1")[0])

    def test_rate_limiter(self):
        rl = RateLimiter(3, 60)
        self.assertEqual([rl.allow("ip", t) for t in (0, 1, 2, 3)], [True, True, True, False])
        self.assertTrue(rl.allow("other", 3))
        self.assertTrue(rl.allow("ip", 61))


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hub = Hub(lambda: Assistant(secret=b"h", llm=DemoLLM()), sessions=True)
        cls.server = make_server(hub, port=0, limit=60, chat_limit=5)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None, cookie=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def session(self):
        st, _, headers = self.call("/api/farms/cheese/today")
        return headers["Set-Cookie"].split(";")[0]

    def test_cookie_is_httponly_and_visitors_are_isolated(self):
        st, _, headers = self.call("/api/farms/cheese/today")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        a, b = self.session(), self.session()
        self.assertNotEqual(a, b)
        key = self.call("/api/farms/cheese/today", cookie=a)[1]["actions"][0]["key"]
        pv = self.call("/api/farms/cheese/preview", {"key": key}, cookie=a)[1]
        tok = self.call("/api/farms/cheese/confirm", {"key": key, "change_hash": pv["change_hash"]}, cookie=a)[1]["token"]
        self.call("/api/farms/cheese/execute", {"key": key, "token": tok}, cookie=a)
        self.assertEqual(len(self.call("/api/farms/cheese/today", cookie=a)[1]["done"]), 1)
        self.assertEqual(len(self.call("/api/farms/cheese/today", cookie=b)[1]["done"]), 0)
        self.call("/api/reset", {}, cookie=b)
        self.assertEqual(len(self.call("/api/farms/cheese/today", cookie=a)[1]["done"]), 1)  # сброс не задел чужую сессию

    def test_chat_endpoint_and_its_stricter_limit(self):
        c = self.session()
        st, r, _ = self.call("/api/farms/cheese/chat", {"text": "почему творог?"}, cookie=c, headers={"X-Forwarded-For": "9.9.9.9"})
        self.assertEqual((st, r["intent"]), (200, "why"))
        codes = [self.call("/api/farms/cheese/chat", {"text": "привет"}, cookie=c, headers={"X-Forwarded-For": "9.9.9.9"})[0] for _ in range(6)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.call("/api/farms/cheese/chat", {"text": ""}, cookie=c, headers={"X-Forwarded-For": "8.8.8.8"})[0], 400)

    def test_oversized_body_is_refused(self):
        st, body, _ = self.call("/api/farms/cheese/chat", {"text": "x" * 20000}, headers={"X-Forwarded-For": "7.7.7.7"})
        self.assertEqual(st, 413)

    def test_health_and_security_headers(self):
        st, body, headers = self.call("/healthz")
        self.assertEqual((st, body), (200, {"ok": True}))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_secure_cookie_behind_https_proxy(self):
        _, _, headers = self.call("/api/farms/cheese/today", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
