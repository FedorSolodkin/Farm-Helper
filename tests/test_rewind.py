"""Перемотка на 7 дней, отчёт «прогноз против факта» и калибровка.
Запуск: python3 -m unittest discover -s tests -t .
"""
import json
import statistics
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

from engine import calibration
from engine.errors import ApiError
from engine.llm import DemoLLM
from engine.report import measure, verdict
from engine.server import make_server
from engine.service import Assistant
from engine.simulate import advance, build_profile, counterfactual, simulate
from engine.signals import detect_all


def confirm(a, key, action_key):
    pv = a.preview(key, action_key)
    tok = a.confirm(key, action_key, pv["change_hash"], second_confirmation=not pv["reversible"])["token"]
    return a.execute(key, action_key, tok)["execution"]


def confirm_top(a, key, n=3):
    for _ in range(n):
        view = a.today_view(key)
        if not view["actions"]:
            break
        confirm(a, key, view["actions"][0]["key"])


class World(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=None)
        self.farm = self.a.farms["cheese"]

    def test_base_week_is_comparable_to_history(self):
        profile = build_profile(self.farm)
        revs = [simulate(self.farm, profile, 7, f"s{i}").revenue for i in range(4)]
        hist = sum(self.farm.revenue(p.id, self.farm.ago(28), self.farm.ago(1)) for p in self.farm.products) / 4
        self.assertTrue(0.7 * hist < statistics.mean(revs) < 1.3 * hist, (statistics.mean(revs), hist))

    def test_same_seed_same_week_different_seed_differs(self):
        profile = build_profile(self.farm)
        a1, a2 = simulate(self.farm, profile, 7, "x"), simulate(self.farm, profile, 7, "x")
        self.assertEqual([(o.customer, o.day, len(o.lines)) for o in a1.orders], [(o.customer, o.day, len(o.lines)) for o in a2.orders])
        self.assertNotEqual(a1.revenue, simulate(self.farm, profile, 7, "y").revenue)

    def test_without_actions_both_worlds_are_identical(self):
        profile = build_profile(self.farm)
        self.assertEqual(simulate(self.farm, profile, 7, "x").revenue, simulate(self.farm.fork(), profile, 7, "x").revenue)

    def test_counterfactual_does_not_touch_the_real_farm(self):
        confirm_top(self.a, "cheese")
        done = self.a.executors["cheese"].active()
        before = json.dumps({"p": [(p.id, p.price) for p in self.farm.products],
                             "promos": [(p.id, str(p.end)) for p in self.farm.promos], "state": self.farm.state}, default=str, sort_keys=True)
        for ex in done:
            counterfactual(self.farm, done, ex)
        counterfactual(self.farm, done, None)
        after = json.dumps({"p": [(p.id, p.price) for p in self.farm.products],
                            "promos": [(p.id, str(p.end)) for p in self.farm.promos], "state": self.farm.state}, default=str, sort_keys=True)
        self.assertEqual(before, after)

    def test_discount_raises_sales_of_the_product(self):
        ex = confirm(self.a, "cheese", "spoilage:tvorog")
        done = self.a.executors["cheese"].active()
        profile = build_profile(self.farm)
        gains = []
        for i in range(6):
            a = simulate(self.farm, profile, 7, f"k{i}")
            b = simulate(counterfactual(self.farm, done, done[0]), profile, 7, f"k{i}")
            gains.append(a.products["tvorog"].units - b.products["tvorog"].units)
        self.assertGreater(statistics.median(gains), 0)

    def test_expiring_batch_is_written_off_less_with_the_promo(self):
        confirm(self.a, "cheese", "spoilage:tvorog")
        done = self.a.executors["cheese"].active()
        profile = build_profile(self.farm)
        a = simulate(self.farm, profile, 7, "w")
        b = simulate(counterfactual(self.farm, done, done[0]), profile, 7, "w")
        self.assertLess(a.products["tvorog"].writeoff, b.products["tvorog"].writeoff)

    def test_offer_brings_customers_back_only_in_the_world_with_it(self):
        for k in ("stockout:mozzarella", "promo_loss:brynza", "spoilage:tvorog"):
            self.a.dismiss("cheese", k, "later")
        confirm(self.a, "cheese", "churn:regulars")
        done = self.a.executors["cheese"].active()
        segment = set(done[0].change["segment"])
        profile = build_profile(self.farm)
        returned = []
        for i in range(8):
            a = simulate(self.farm, profile, 7, f"r{i}")
            b = simulate(counterfactual(self.farm, done, None), profile, 7, f"r{i}")
            self.assertFalse({o.customer for o in b.orders} & segment)
            returned.append(len({o.customer for o in a.orders} & segment))
        self.assertGreater(statistics.mean(returned), 0)
        self.assertLess(statistics.mean(returned), len(segment))


class Rewind(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=None)
        self.farm = self.a.farms["cheese"]

    def test_nothing_to_check_without_confirmed_actions(self):
        with self.assertRaises(ApiError) as cm:
            self.a.rewind("cheese")
        self.assertEqual((cm.exception.code, cm.exception.status), ("nothing_to_check", 409))

    def test_report_matches_the_executions(self):
        confirm_top(self.a, "cheese")
        r = self.a.rewind("cheese")["report"]
        self.assertEqual([x["key"] for x in r["actions"]], ["spoilage:tvorog", "stockout:mozzarella", "promo_loss:brynza"])
        self.assertEqual(r["totals"]["forecast"], sum(x["forecast"]["mid"] for x in r["actions"]))
        self.assertEqual(r["totals"]["fact"], sum(x["fact"] for x in r["actions"]))
        self.assertEqual(r["totals"]["count"], 3)
        self.assertEqual(r["period"], {"from": "2026-10-26", "to": "2026-11-01", "days": 7})
        for x in r["actions"]:
            self.assertEqual(x["verdict"], verdict(x["fact"], x["forecast"]["low"], x["forecast"]["high"]))
            self.assertTrue(x["details"])

    def test_rewind_moves_time_and_closes_the_day(self):
        confirm_top(self.a, "cheese")
        orders = len(self.farm.orders)
        self.a.rewind("cheese")
        self.assertEqual(str(self.farm.today), "2026-11-02")
        self.assertGreater(len(self.farm.orders), orders)
        view = self.a.today_view("cheese")
        self.assertEqual((view["date"], len(view["done"]), len(view["actions"]), view["can_rewind"]), ("2026-11-02", 0, 3, False))
        self.assertEqual(view["last_report"]["count"], 3)
        with self.assertRaises(ApiError) as cm:
            self.a.rollback("cheese", self.a.executors["cheese"].executions[0].id)
        self.assertEqual(cm.exception.code, "archived")

    def test_rewind_is_deterministic(self):
        b = Assistant(secret=b"s", llm=None)
        for a in (self.a, b):
            confirm_top(a, "cheese")
        self.assertEqual(self.a.rewind("cheese")["report"], b.rewind("cheese")["report"])

    def test_world_seed_changes_the_outcome(self):
        other = Assistant(secret=b"s", llm=None, world_seed="zzz")
        for a in (self.a, other):
            confirm_top(a, "cheese")
        self.assertNotEqual(self.a.rewind("cheese")["report"]["totals"]["fact"], other.rewind("cheese")["report"]["totals"]["fact"])

    def test_week_totals_compare_with_and_without(self):
        confirm_top(self.a, "cheese")
        w = self.a.rewind("cheese")["report"]["week"]
        self.assertGreater(w["revenue_with"], 0)
        self.assertNotEqual(w["revenue_with"], w["revenue_without"])
        self.assertLessEqual(w["writeoff_with"], w["writeoff_without"])

    def test_audit_has_the_rewind_and_chain_is_intact(self):
        confirm_top(self.a, "cheese")
        self.a.rewind("cheese")
        last = self.a.audit.entries[-1]
        self.assertEqual((last["event"], last["details"]["to"]), ("rewind", "2026-11-02"))
        self.assertTrue(self.a.audit.verify()[0])

    def test_ended_preorders_and_hygiene_are_applied(self):
        for k in ("stockout:mozzarella", "promo_loss:brynza", "spoilage:tvorog", "churn:regulars", "leaky_card:kachotta"):
            self.a.dismiss("cheese", k, "later")
        confirm(self.a, "cheese", "hygiene:cards")
        self.a.rewind("cheese")
        self.assertTrue(all(p.has_photo and p.has_expiry and p.has_weight for p in self.farm.products))
        self.assertNotIn("hygiene_in_progress", self.farm.state)

    def test_second_rewind_needs_new_confirmations(self):
        confirm_top(self.a, "cheese", 1)
        self.a.rewind("cheese")
        with self.assertRaises(ApiError):
            self.a.rewind("cheese")
        confirm_top(self.a, "cheese", 1)
        self.a.rewind("cheese")
        self.assertEqual(len(self.a.reports["cheese"]), 2)
        self.assertEqual(str(self.farm.today), "2026-11-09")

    def test_reset_clears_reports_and_calibration(self):
        confirm_top(self.a, "cheese", 1)
        self.a.rewind("cheese")
        self.a.reset()
        self.assertEqual((self.a.reports["cheese"], self.a.cal["cheese"]), ([], {}))


class Learning(unittest.TestCase):
    def test_verdict_boundaries(self):
        self.assertEqual(verdict(0, 100, 200), "not_worked")
        self.assertEqual(verdict(-5, 100, 200), "not_worked")
        self.assertEqual(verdict(99, 100, 200), "below")
        self.assertEqual(verdict(100, 100, 200), "in_range")
        self.assertEqual(verdict(200, 100, 200), "in_range")
        self.assertEqual(verdict(201, 100, 200), "above")

    def test_stats_are_smoothed_and_bounded(self):
        cal = {}
        self.assertEqual(calibration.stats(cal, "spoilage")[:2], (0, 1.0))
        calibration.record(cal, "spoilage", 1000, 400)
        n, scale, acc = calibration.stats(cal, "spoilage")
        self.assertEqual(n, 1)
        self.assertAlmostEqual(scale, (0.4 + 2) / 3)  # одна проверка не ломает прогноз
        for _ in range(30):
            calibration.record(cal, "spoilage", 1000, 10)
        self.assertEqual(calibration.stats(cal, "spoilage")[1], 0.5)  # нижняя граница
        calibration.record(cal, "churn", 100, 99999)
        calibration.record(cal, "churn", 100, 99999)
        self.assertEqual(calibration.stats(cal, "churn")[1], 1.5)  # верхняя граница

    def test_apply_scales_effect_and_lowers_confidence_for_bad_forecasts(self):
        farm = Assistant(secret=b"s", llm=None).farms["cheese"]
        from engine.benchmarks import BENCHMARKS
        from engine.privacy import usable_benchmarks
        acts = detect_all(farm, usable_benchmarks(BENCHMARKS))
        a = next(x for x in acts if x.type == "spoilage")
        mid, conf = a.effect[1], a.confidence
        cal = {}
        for _ in range(3):
            calibration.record(cal, "spoilage", 1000, 300)
        calibration.apply([a], cal)
        self.assertLess(a.effect[1], mid)
        self.assertLess(a.confidence, conf)
        self.assertIn("поправлен", a.confidence_basis)
        other = next(x for x in acts if x.type == "stockout")
        before = other.effect
        calibration.apply([other], cal)
        self.assertEqual(other.effect, before)  # другой тип не затронут

    def test_next_forecast_uses_the_lesson(self):
        a = Assistant(secret=b"s", llm=None)
        first = next(x for x in a.today_view("cheese")["actions"] if x["key"] == "spoilage:tvorog")["effect"]["mid"]
        confirm_top(a, "cheese", 1)
        a.rewind("cheese")
        ratio = a.reports["cheese"][0]["actions"][0]["ratio"]
        scale = a.reports["cheese"][0]["actions"][0]["learning"]["scale_after"]
        self.assertAlmostEqual(scale, max(0.5, min(1.5, (ratio + 2) / 3)), places=1)
        later = a.cal["cheese"]["spoilage"]
        self.assertEqual(len(later), 1)
        self.assertGreater(first, 0)

    def test_calibrated_texts_still_pass_the_fact_checker(self):
        a = Assistant(secret=b"s", llm=DemoLLM("good"))
        confirm_top(a, "cheese")
        a.rewind("cheese")
        view = a.today_view("cheese")
        self.assertTrue(any("поправлен" in x["confidence"]["basis"] for x in view["actions"]))
        self.assertTrue(all(x["narration"]["source"] == "llm" for x in view["actions"]), [x["narration"] for x in view["actions"]])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(Assistant(secret=b"h", llm=None), port=0)
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

    def test_flow(self):
        self.call("/api/reset", {})
        self.assertEqual(self.call("/api/farms/cheese/rewind", {})[0], 409)
        self.assertEqual(self.call("/api/farms/cheese/report")[0], 404)
        st, today = self.call("/api/farms/cheese/today")
        self.assertFalse(today["can_rewind"])
        key = today["actions"][0]["key"]
        st, pv = self.call("/api/farms/cheese/preview", {"key": key})
        st, cf = self.call("/api/farms/cheese/confirm", {"key": key, "change_hash": pv["change_hash"]})
        self.call("/api/farms/cheese/execute", {"key": key, "token": cf["token"]})
        self.assertTrue(self.call("/api/farms/cheese/today")[1]["can_rewind"])
        st, res = self.call("/api/farms/cheese/rewind", {})
        self.assertEqual(st, 200)
        self.assertEqual(res["date"], "2026-11-02")
        st, rep = self.call("/api/farms/cheese/report")
        self.assertEqual(rep["report"]["totals"]["count"], 1)
        self.assertEqual(self.call("/api/farms/cheese/today")[1]["last_report"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
