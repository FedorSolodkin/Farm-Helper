"""Подтверждение, токены, исполнение, откат и журнал аудита.
Запуск: python3 -m unittest discover -s tests -t .
"""
import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from engine.audit import AuditLog
from engine.benchmarks import BENCHMARKS
from engine.errors import ApiError
from engine.executor import IRREVERSIBLE
from engine.generate import build_farm
from engine.privacy import usable_benchmarks
from engine.server import make_server
from engine.service import Assistant
from engine.signals import detect_all
from engine.tokens import TokenService, change_hash


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def snapshot(farm):
    return json.dumps({
        "prices": {p.id: (p.price, str(p.last_price_change)) for p in farm.products},
        "promos": [(p.id, str(p.start), str(p.end), p.discount) for p in farm.promos],
        "state": farm.state,
    }, sort_keys=True, default=str)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.a = Assistant(secret=b"test-secret", clock=self.clock)

    def run_flow(self, key, action_key):
        pv = self.a.preview(key, action_key)
        tok = self.a.confirm(key, action_key, pv["change_hash"], second_confirmation=not pv["reversible"])["token"]
        return self.a.execute(key, action_key, tok)["execution"]


class Tokens(Base):
    def issue(self, farmer="farmer-cheese", act="spoilage:tvorog", h="h1"):
        return self.a.tokens.issue(farmer, act, h)[0]

    def test_token_works_once(self):
        t = self.issue()
        self.a.tokens.consume(t, "farmer-cheese", "spoilage:tvorog", "h1")
        with self.assertRaises(ApiError) as cm:
            self.a.tokens.consume(t, "farmer-cheese", "spoilage:tvorog", "h1")
        self.assertEqual(cm.exception.code, "token_used")

    def test_token_expires_after_ten_minutes(self):
        t = self.issue()
        self.clock.advance(minutes=10, seconds=1)
        with self.assertRaises(ApiError) as cm:
            self.a.tokens.consume(t, "farmer-cheese", "spoilage:tvorog", "h1")
        self.assertEqual(cm.exception.code, "token_expired")

    def test_token_still_valid_before_expiry(self):
        t = self.issue()
        self.clock.advance(minutes=9, seconds=50)
        self.a.tokens.consume(t, "farmer-cheese", "spoilage:tvorog", "h1")

    def test_token_bound_to_change(self):
        t = self.issue()
        with self.assertRaises(ApiError) as cm:
            self.a.tokens.consume(t, "farmer-cheese", "spoilage:tvorog", "other-hash")
        self.assertEqual(cm.exception.code, "token_wrong_change")

    def test_token_bound_to_action_and_farmer(self):
        t = self.issue()
        with self.assertRaises(ApiError) as cm:
            self.a.tokens.consume(t, "farmer-cheese", "stockout:mozzarella", "h1")
        self.assertEqual(cm.exception.code, "token_wrong_change")
        with self.assertRaises(ApiError) as cm:
            self.a.tokens.consume(t, "farmer-veg", "spoilage:tvorog", "h1")
        self.assertEqual(cm.exception.code, "token_wrong_farmer")

    def test_tampered_and_foreign_tokens_rejected(self):
        t = self.issue()
        raw, sig = t.split(".")
        for bad in (raw + "." + sig[::-1], "garbage", raw + ".", ""):
            with self.assertRaises(ApiError):
                self.a.tokens.consume(bad, "farmer-cheese", "spoilage:tvorog", "h1")
        foreign = TokenService(b"another-secret", self.clock).issue("farmer-cheese", "spoilage:tvorog", "h1")[0]
        with self.assertRaises(ApiError) as cm:
            self.a.tokens.consume(foreign, "farmer-cheese", "spoilage:tvorog", "h1")
        self.assertEqual(cm.exception.code, "token_bad_signature")


class Execution(Base):
    def test_nothing_changes_before_confirmation(self):
        before = snapshot(self.a.farms["cheese"])
        self.a.today_view("cheese")
        self.a.preview("cheese", "spoilage:tvorog")
        self.a.confirm("cheese", "spoilage:tvorog", self.a.preview("cheese", "spoilage:tvorog")["change_hash"])
        self.assertEqual(snapshot(self.a.farms["cheese"]), before)

    def test_recommendation_engine_is_read_only(self):
        farm = build_farm("cheese")
        before = snapshot(farm)
        detect_all(farm, usable_benchmarks(BENCHMARKS))
        self.assertEqual(snapshot(farm), before)

    def test_execute_without_valid_token_changes_nothing(self):
        before = snapshot(self.a.farms["cheese"])
        with self.assertRaises(ApiError):
            self.a.execute("cheese", "spoilage:tvorog", "no-token")
        self.assertEqual(snapshot(self.a.farms["cheese"]), before)
        self.assertEqual(self.a.audit.entries[-1]["event"], "rejected")

    def test_promo_is_created_and_rolled_back_exactly(self):
        farm = self.a.farms["cheese"]
        before = snapshot(farm)
        ex = self.run_flow("cheese", "spoilage:tvorog")
        self.assertIsNotNone(farm.active_promo("tvorog"))
        self.assertNotEqual(snapshot(farm), before)
        self.a.rollback("cheese", ex["id"])
        self.assertEqual(snapshot(farm), before)

    def test_every_reversible_operation_rolls_back_exactly(self):
        covered = set()
        for key in ("cheese", "veg", "honey"):
            farm = self.a.farms[key]
            for action in detect_all(farm, self.a.bench):
                op = action.change["op"]
                if op in IRREVERSIBLE or op in covered:
                    continue
                try:
                    self.a._find(key, action.key)
                except ApiError:
                    continue
                before = snapshot(farm)
                ex = self.run_flow(key, action.key)
                self.assertNotEqual(snapshot(farm), before, op)
                self.a.rollback(key, ex["id"])
                self.assertEqual(snapshot(farm), before, op)
                covered.add(op)
        self.assertGreaterEqual(len(covered), 8, covered)

    def test_duplicate_execution_is_rejected(self):
        self.run_flow("cheese", "spoilage:tvorog")
        with self.assertRaises(ApiError):  # дело уже неактуально или заблокировано стоп-правилом
            self.run_flow("cheese", "spoilage:tvorog")

    def test_stop_rules_are_rechecked_at_execution(self):
        pv = self.a.preview("cheese", "spoilage:tvorog")
        token = self.a.confirm("cheese", "spoilage:tvorog", pv["change_hash"])["token"]
        # после подтверждения на товар успели запустить другую акцию
        self.run_flow("cheese", "promo_loss:brynza")  # не связано, проверим именно конфликт ниже
        from engine.models import Promo
        farm = self.a.farms["cheese"]
        farm.promos.append(Promo("manual", "tvorog", farm.today, farm.today + timedelta(days=2), 0.10))
        with self.assertRaises(ApiError) as cm:
            self.a.execute("cheese", "spoilage:tvorog", token)
        self.assertIn(cm.exception.code, ("blocked", "not_found"))
        self.assertEqual(sum(1 for p in farm.promos if p.product_id == "tvorog" and p.discount == 0.25), 0)

    def test_changed_data_invalidates_preview(self):
        pv = self.a.preview("cheese", "spoilage:tvorog")
        with self.assertRaises(ApiError) as cm:
            self.a.confirm("cheese", "spoilage:tvorog", "0" * 64)
        self.assertEqual(cm.exception.code, "changed")
        self.assertEqual(len(pv["change_hash"]), 64)

    def test_open_list_shrinks_and_refills_around_three(self):
        v = self.a.today_view("cheese")
        self.assertEqual(v["summary"]["total"], 3)
        ex = self.run_flow("cheese", v["actions"][0]["key"])
        v = self.a.today_view("cheese")
        self.assertEqual((len(v["done"]), len(v["actions"]), v["summary"]["total"]), (1, 2, 3))
        self.a.rollback("cheese", ex["id"])
        v = self.a.today_view("cheese")
        self.assertEqual((len(v["done"]), len(v["actions"])), (0, 3))

    def test_all_three_done_closes_the_day(self):
        for _ in range(3):
            self.run_flow("cheese", self.a.today_view("cheese")["actions"][0]["key"])
        v = self.a.today_view("cheese")
        self.assertEqual(len(v["done"]), 3)
        self.assertEqual(v["actions"], [])
        self.assertIn("выполнены", v["summary"]["note"])


class Irreversible(Base):
    def churn_to_front(self):
        for key in ("stockout:mozzarella", "promo_loss:brynza", "spoilage:tvorog"):
            self.a.dismiss("cheese", key, "no_stock")

    def test_second_confirmation_required(self):
        self.churn_to_front()
        pv = self.a.preview("cheese", "churn:regulars")
        self.assertFalse(pv["reversible"])
        self.assertTrue(10 <= pv["recipients"] <= 14)
        with self.assertRaises(ApiError) as cm:
            self.a.confirm("cheese", "churn:regulars", pv["change_hash"])
        self.assertEqual((cm.exception.code, cm.exception.status), ("second_confirmation_required", 428))

    def test_offer_is_sent_and_cannot_be_rolled_back(self):
        self.churn_to_front()
        ex = self.run_flow("cheese", "churn:regulars")
        self.assertFalse(ex["reversible"])
        with self.assertRaises(ApiError) as cm:
            self.a.rollback("cheese", ex["id"])
        self.assertEqual(cm.exception.code, "irreversible")
        self.assertEqual(self.a.farms["cheese"].state["offers"][0]["recipients"], 12)
        # получатели не повторяются: после отправки сигнал молчит
        self.assertNotIn("churn:regulars", [x["key"] for x in self.a.today_view("cheese")["actions"]])


class Audit(Base):
    def test_full_trail_of_a_flow(self):
        ex = self.run_flow("cheese", "spoilage:tvorog")
        self.a.rollback("cheese", ex["id"])
        self.assertEqual([e["event"] for e in self.a.audit.entries], ["preview", "confirmed", "executed", "rolled_back"])
        self.assertTrue(all(e["farmer"] == "farmer-cheese" for e in self.a.audit.entries))

    def test_chain_detects_edit_and_deletion(self):
        self.run_flow("cheese", "spoilage:tvorog")
        self.assertEqual(self.a.audit.verify(), (True, None))
        log = copy.deepcopy(self.a.audit)
        log.entries[1]["details"]["token"] = "forged"
        self.assertEqual(log.verify(), (False, 2))
        log = copy.deepcopy(self.a.audit)
        del log.entries[1]
        self.assertFalse(log.verify()[0])

    def test_audit_never_stores_the_token_itself(self):
        pv = self.a.preview("cheese", "spoilage:tvorog")
        token = self.a.confirm("cheese", "spoilage:tvorog", pv["change_hash"])["token"]
        self.assertNotIn(token, json.dumps(self.a.audit.entries))

    def test_audit_is_per_farmer(self):
        self.a.preview("cheese", "spoilage:tvorog")
        self.assertEqual(len(self.a.audit_view("veg")["entries"]), 0)
        self.assertEqual(len(self.a.audit_view("cheese")["entries"]), 1)

    def test_file_log_matches_memory(self):
        import tempfile, pathlib
        path = pathlib.Path(tempfile.mkdtemp()) / "audit.jsonl"
        log = AuditLog(self.clock, path)
        log.append("f", "preview", "x")
        log.append("f", "executed", "x")
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines, log.entries)


class Dismiss(Base):
    def test_dismiss_snoozes_and_is_audited(self):
        self.a.dismiss("cheese", "spoilage:tvorog", "no_discounts")
        keys = [x["key"] for x in self.a.today_view("cheese")["actions"]]
        self.assertNotIn("spoilage:tvorog", keys)
        self.assertEqual(self.a.audit.entries[-1]["details"], {"reason": "no_discounts", "days": 14})

    def test_unknown_reason_rejected(self):
        with self.assertRaises(ApiError):
            self.a.dismiss("cheese", "spoilage:tvorog", "because")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assistant = Assistant(secret=b"http-secret")
        cls.server = make_server(cls.assistant, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_full_flow_over_http(self):
        self.call("/api/reset", {})
        st, today = self.call("/api/farms/cheese/today")
        self.assertEqual(st, 200)
        key = today["actions"][0]["key"]
        self.assertNotIn("change", today["actions"][0])  # изменение остаётся на сервере
        st, pv = self.call("/api/farms/cheese/preview", {"key": key})
        st, cf = self.call("/api/farms/cheese/confirm", {"key": key, "change_hash": pv["change_hash"]})
        self.assertEqual(st, 200)
        st, ex = self.call("/api/farms/cheese/execute", {"key": key, "token": cf["token"]})
        self.assertEqual(st, 200)
        st, again = self.call("/api/farms/cheese/execute", {"key": key, "token": cf["token"]})
        self.assertIn(st, (404, 409, 422))
        st, rb = self.call("/api/farms/cheese/rollback", {"execution_id": ex["execution"]["id"]})
        self.assertEqual(st, 200)
        st, au = self.call("/api/farms/cheese/audit")
        self.assertTrue(au["chain_ok"])
        self.assertEqual(au["entries"][-1]["event"], "rolled_back")

    def test_errors_are_json_with_status(self):
        self.assertEqual(self.call("/api/farms/nope/today")[0], 404)
        st, body = self.call("/api/farms/cheese/execute", {"key": "spoilage:tvorog", "token": "x"})
        self.assertEqual(st, 403)
        self.assertIn("message", body["error"])
        self.assertEqual(self.call("/api/farms/cheese/unknown", {})[0], 404)

    def test_static_page_is_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("Три дела на сегодня", r.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
