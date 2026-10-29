"""Доставка: ранжирование заказов, рейсы, API, раздача страницы и история совета в карточке."""
import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta

from engine import calibration, delivery
from engine.api import dispatch
from engine.generate import SPECS, build_farm
from engine.server import make_server
from engine.service import Assistant

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 29)


class Planning(unittest.TestCase):
    def setUp(self):
        self.data = {k: delivery.build(build_farm(k, TODAY)) for k in SPECS}

    def test_deterministic_for_same_farm_and_day_but_changes_next_day(self):
        again = delivery.build(build_farm("cheese", TODAY))
        self.assertEqual(json.dumps(again, default=str), json.dumps(self.data["cheese"], default=str))
        nxt = delivery.build(build_farm("cheese", TODAY + timedelta(days=1)))
        self.assertNotEqual([o["id"] + o["district"] for o in nxt["orders"]], [o["id"] + o["district"] for o in self.data["cheese"]["orders"]])

    def test_every_order_rides_exactly_once_inside_its_window(self):
        for key, d in self.data.items():
            for mode in ("plan", "as_is"):
                stops = [s["order_id"] for t in d[mode] for s in t["stops"]]
                self.assertEqual(sorted(stops), sorted(o["id"] for o in d["orders"]), (key, mode))
            for o in d["orders"]:
                self.assertTrue(o["wish"] <= o["planned"] <= o["deadline"], (key, o))
                self.assertGreaterEqual(o["planned"], TODAY.isoformat())

    def test_plan_never_longer_than_habit_and_savings_add_up(self):
        for key, d in self.data.items():
            s = d["summary"]
            self.assertLessEqual(s["plan"]["km"], s["as_is"]["km"], key)
            self.assertAlmostEqual(s["saved_km"], s["as_is"]["km"] - s["plan"]["km"], places=1)
            self.assertGreaterEqual(s["saved_days"], 0, key)
            self.assertEqual(s["orders"], len(d["orders"]))
            self.assertEqual(s["sum"], sum(o["sum"] for o in d["orders"]))
            for t in d["plan"]:
                self.assertLessEqual(t["minutes"], delivery.MAX_TRIP_MIN, key)
                self.assertLessEqual(len(t["stops"]), delivery.MAX_STOPS, key)

    def test_ranking_is_by_deadline_and_perishable_do_not_wait_long(self):
        for key, d in self.data.items():
            ranks = [o["deadline"] for o in d["orders"]]
            self.assertEqual(ranks, sorted(ranks), key)
            self.assertEqual([o["rank"] for o in d["orders"]], list(range(1, len(d["orders"]) + 1)))
            for o in d["orders"]:
                if o["perishable"]:
                    self.assertLessEqual(o["flex"], 1)
                if o["priority"] == "urgent":
                    self.assertLessEqual((date.fromisoformat(o["deadline"]) - TODAY).days, 2)

    def test_orders_carry_no_personal_data(self):
        blob = json.dumps(self.data["cheese"], ensure_ascii=False, default=str)
        self.assertNotIn("+7", blob)
        self.assertNotIn("@", blob)
        for o in self.data["cheese"]["orders"]:
            self.assertTrue(o["customer"].startswith("Клиент "))

    def test_route_is_not_longer_than_naive_order(self):
        farm = build_farm("veg", TODAY)
        stops = [dict(id=o["id"], district=o["district"], x=o["x"], y=o["y"], sum=o["sum"]) for o in delivery.make_orders(farm)[:7]]
        smart = delivery._trip(TODAY, stops)["km"]
        naive = delivery._route_len([(s["x"], s["y"]) for s in stops])
        self.assertLessEqual(smart, naive + 0.05)


class Api(unittest.TestCase):
    def test_route_returns_plan_and_unknown_farm_is_404(self):
        a = Assistant(llm=None)
        status, body = dispatch(a, "GET", "/api/farms/veg/delivery")
        self.assertEqual(status, 200)
        self.assertIn("plan", body)
        self.assertEqual(dispatch(a, "GET", "/api/farms/nope/delivery")[0], 404)
        self.assertEqual(dispatch(a, "POST", "/api/farms/veg/delivery", {})[0], 404)  # только чтение: ничего не отправляется

    def test_viewing_delivery_changes_nothing_in_state_or_audit(self):
        a = Assistant(llm=None)
        before = len(a.audit.for_farmer(a.farmer("veg")))
        dispatch(a, "GET", "/api/farms/veg/delivery")
        after = len(a.audit.for_farmer(a.farmer("veg")))
        self.assertEqual(before, after)


class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(Assistant(llm=None), port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_delivery_page_is_served_and_main_page_links_to_it(self):
        for path in ("/delivery", "/delivery.html", "/delivery.html?farm=veg"):
            status, html = self.fetch(path)
            self.assertEqual(status, 200, path)
            self.assertIn("Доставка на неделю", html)
        self.assertIn('id="deliveryBtn"', self.fetch("/")[1])
        self.assertEqual(self.fetch("/delivery.py")[0], 404)

    def test_static_pages_are_revalidated_so_updates_show_at_once(self):
        with urllib.request.urlopen(self.base + "/delivery.html") as r:
            self.assertEqual(r.headers["Cache-Control"], "no-cache")

    def test_page_explains_why_it_exists(self):
        html = (ROOT / "web" / "delivery.html").read_text(encoding="utf-8")
        self.assertIn("После кастдев-интервью я узнал", html)
        self.assertIn("основные проблемы у фермеров возникают с доставкой", html)

    def test_delivery_page_has_no_user_html_injection_paths(self):
        html = (ROOT / "web" / "delivery.html").read_text(encoding="utf-8")
        body = html.split("<script>")[1]
        self.assertEqual(body.count("innerHTML"), 1)  # только иконки из собственного словаря


class AdviceHistory(unittest.TestCase):
    def test_track_without_and_with_checks(self):
        self.assertEqual(calibration.track({}, "spoilage")["n"], 0)
        t = calibration.track({"spoilage": [1.0, 0.8, 1.4]}, "spoilage")
        self.assertEqual((t["n"], t["avg_pct"], t["hit"]), (3, 107, 2))

    def test_card_gets_history_and_it_grows_after_a_checked_week(self):
        a = Assistant(llm=None)
        first = dispatch(a, "GET", "/api/farms/cheese/today")[1]["actions"]
        self.assertTrue(all(x["track"]["n"] == 0 for x in first))
        key = first[0]
        prev = dispatch(a, "POST", "/api/farms/cheese/preview", {"key": key["key"]})[1]
        conf = dispatch(a, "POST", "/api/farms/cheese/confirm", {"key": key["key"], "change_hash": prev["change_hash"]})
        self.assertEqual(conf[0], 200)
        token = conf[1]["token"]
        self.assertEqual(dispatch(a, "POST", "/api/farms/cheese/execute", {"key": key["key"], "token": token})[0], 200)
        self.assertEqual(dispatch(a, "POST", "/api/farms/cheese/rewind", {})[0], 200)
        self.assertEqual(len(a.cal["cheese"][key["type"]]), 1)
        for x in dispatch(a, "GET", "/api/farms/cheese/today")[1]["actions"]:
            if x["type"] == key["type"]:
                self.assertEqual(x["track"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
