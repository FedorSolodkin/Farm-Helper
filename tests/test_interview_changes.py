"""Правки по интервью: сезонный излишек → опт, «маржа сжалась», «цена осознанная»."""
import statistics
import unittest
from datetime import timedelta

from engine.benchmarks import BENCHMARKS
from engine.errors import ApiError
from engine.executor import IRREVERSIBLE
from engine.generate import build_farm
from engine.privacy import usable_benchmarks
from engine.scoring import validate_change
from engine.service import Assistant
from engine.signals import detect_all
from engine.simulate import build_profile, counterfactual, simulate

BENCH = usable_benchmarks(BENCHMARKS)


def find(farm, key):
    return next((a for a in detect_all(farm, BENCH) if a.key == key), None)


def run(a, key, action_key, second=False):
    pv = a.preview(key, action_key)
    tok = a.confirm(key, action_key, pv["change_hash"], second_confirmation=second)["token"]
    return a.execute(key, action_key, tok)["execution"]


class Glut(unittest.TestCase):
    def setUp(self):
        self.farm = build_farm("veg")

    def test_planted_harvest_glut_is_found(self):
        a = find(self.farm, "glut:broccoli")
        p = self.farm.product("broccoli")
        rate, days = self.farm.rate("broccoli"), (self.farm.batches_of("broccoli")[0].expires - self.farm.today).days
        self.assertEqual(a.change["op"], "offer_wholesale")
        self.assertAlmostEqual(a.change["qty"], float(int(p_stock(self.farm) - rate * days)))
        self.assertLessEqual(a.change["discount"], 0.30)
        self.assertGreaterEqual(p.price * (1 - a.change["discount"]), p.cost * 1.05)
        self.assertGreaterEqual(len(a.change["segment"]), 3)
        self.assertTrue(all(len(c) == 12 for c in a.change["segment"]))  # только хэши

    def test_only_seasonal_products_with_a_real_excess(self):
        self.farm.product("broccoli").seasonal = False
        self.assertIsNone(find(self.farm, "glut:broccoli"))
        farm = build_farm("veg")
        for b in farm.batches:
            if b.product_id == "broccoli":
                b.qty = 5  # хватает на розницу
        self.assertIsNone(find(farm, "glut:broccoli"))
        farm = build_farm("veg")
        for b in farm.batches:
            if b.product_id == "broccoli":
                b.expires = farm.today + timedelta(days=4)  # срок поджимает: это «риск списания», не опт
        self.assertIsNone(find(farm, "glut:broccoli"))

    def test_wholesale_is_irreversible_and_needs_second_confirmation(self):
        a = Assistant(secret=b"s", llm=None)
        self.assertIn("offer_wholesale", IRREVERSIBLE)
        pv = a.preview("veg", "glut:broccoli")
        self.assertFalse(pv["reversible"])
        self.assertEqual(pv["recipients"], 15)
        with self.assertRaises(ApiError) as cm:
            a.confirm("veg", "glut:broccoli", pv["change_hash"])
        self.assertEqual(cm.exception.status, 428)
        ex = run(a, "veg", "glut:broccoli", second=True)
        self.assertFalse(ex["reversible"])
        self.assertEqual(a.farms["veg"].state["wholesale"][0]["recipients"], 15)
        with self.assertRaises(ApiError) as cm:
            a.rollback("veg", ex["id"])
        self.assertEqual(cm.exception.code, "irreversible")

    def test_offer_is_not_repeated(self):
        a = Assistant(secret=b"s", llm=None)
        run(a, "veg", "glut:broccoli", second=True)
        self.assertIsNone(find(a.farms["veg"], "glut:broccoli"))

    def test_stop_rules_for_wholesale(self):
        a = find(self.farm, "glut:broccoli")
        bad = dict(a.change)
        for patch, text in (({"discount": 0.4}, "лимит"), ({"discount": 0.30}, "ниже себестоимости"), ({"qty": 999}, "больше остатка"), ({"segment": []}, "пустой")):
            change = {**bad, **patch}
            if patch == {"discount": 0.30}:
                self.farm.product("broccoli").cost = 290  # цена со скидкой уйдёт ниже себестоимости
            reasons = validate_change(change, self.farm)
            self.assertTrue(any(text in r for r in reasons), (patch, reasons))
        self.assertEqual(validate_change(a.change, build_farm("veg")), [])

    def test_week_shows_wholesale_sales_and_less_waste(self):
        a = Assistant(secret=b"s", llm=None)
        run(a, "veg", "glut:broccoli", second=True)
        done = a.executors["veg"].active()
        profile = build_profile(a.farms["veg"])
        gains = []
        for i in range(6):
            with_lot = simulate(a.farms["veg"], profile, 7, f"g{i}")
            without = simulate(counterfactual(a.farms["veg"], done, None), profile, 7, f"g{i}")
            self.assertEqual(without.products["broccoli"].wholesale, 0)
            self.assertGreater(with_lot.products["broccoli"].wholesale, 0)
            self.assertLessEqual(with_lot.products["broccoli"].writeoff, without.products["broccoli"].writeoff)
            gains.append(with_lot.products["broccoli"].revenue - without.products["broccoli"].revenue)
        self.assertGreater(statistics.median(gains), 0)
        report = a.rewind("veg")["report"]
        row = next(x for x in report["actions"] if x["key"] == "glut:broccoli")
        self.assertTrue(any("Оптом продано" in d for d in row["details"]))


def p_stock(farm):
    return farm.stock("broccoli")


class MarginSqueeze(unittest.TestCase):
    def test_planted_cost_increase_is_found_and_is_secondary(self):
        farm = build_farm("cheese")
        a = find(farm, "margin:mozzarella")
        self.assertIn("себестоимость выросла", a.title)
        self.assertEqual(a.change["op"], "set_price")
        self.assertLessEqual(a.change["price"], farm.product("mozzarella").price * 1.10 + 10)
        self.assertGreater(a.effect_mid, 0)
        self.assertIn("margin:mozzarella", [x.key for x in Assistant(llm=None)._candidates("cheese")])

    def test_skips_when_not_applicable(self):
        farm = build_farm("cheese")
        p = farm.product("mozzarella")
        p.last_price_change = farm.today - timedelta(days=5)  # цену уже пересмотрели после роста
        self.assertIsNone(find(farm, "margin:mozzarella"))
        farm = build_farm("cheese")
        farm.product("mozzarella").cost_prev = farm.product("mozzarella").cost + 10  # себестоимость упала
        self.assertIsNone(find(farm, "margin:mozzarella"))
        farm = build_farm("cheese")
        farm.state["premium_products"] = ["mozzarella"]
        self.assertIsNone(find(farm, "margin:mozzarella"))

    def test_no_advice_when_customers_would_leave_faster_than_margin_grows(self):
        farm = build_farm("honey")  # мёд очень чувствителен к цене
        p = farm.product("grech")
        # маржа была 80%: при такой марже повышение цены не окупает ухода покупателей (эластичность мёда 2.0)
        p.cost_prev, p.cost_changed, p.cost = p.price * 0.20, farm.today - timedelta(days=20), p.price * 0.40
        self.assertIsNone(find(farm, "margin:grech"))

    def test_price_change_goes_through_the_normal_confirmation(self):
        a = Assistant(secret=b"s", llm=None)
        ex = run(a, "cheese", "margin:mozzarella")
        self.assertTrue(ex["reversible"])
        self.assertGreater(a.farms["cheese"].product("mozzarella").price, 980)
        a.rollback("cheese", ex["id"])
        self.assertEqual(a.farms["cheese"].product("mozzarella").price, 980)


class ConsciousPrice(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=None)

    def test_price_advice_is_softer_and_points_to_the_alternative(self):
        act = next(x for x in self.a._candidates("honey") if x.key == "price_off:lipa")
        self.assertIn("Проверьте цену", act.title)
        self.assertTrue(any("Цена осознанная" in w for w in act.why))

    def test_reason_silences_price_advice_for_the_product(self):
        self.a.dismiss("honey", "price_off:lipa", "premium_price")
        self.assertEqual(self.a.farms["honey"].state["premium_products"], ["lipa"])
        keys = [x.key for x in self.a._candidates("honey")]
        self.assertNotIn("price_off:lipa", keys)
        view = self.a.today_view("honey")
        self.assertNotIn("price_off:lipa", [x["key"] for x in view["actions"]])
        self.assertEqual(self.a.audit.entries[-1]["details"], {"reason": "premium_price", "days": 60})

    def test_reason_also_covers_margin_advice(self):
        self.a.dismiss("cheese", "margin:mozzarella", "premium_price")
        self.assertNotIn("margin:mozzarella", [x.key for x in self.a._candidates("cheese")])

    def test_chat_understands_and_never_changes_anything(self):
        before = dict(self.a.farms["honey"].state)
        r = self.a.chat("honey", "я не хочу снижать цену, у меня премиум")
        self.assertEqual(r["intent"], "premium")
        self.assertIn("Цена осознанная", r["text"])
        self.assertEqual(self.a.farms["honey"].state, before)

    def test_ui_offers_the_reason(self):
        with open("web/index.html", encoding="utf-8") as fh:
            self.assertIn("Цена осознанная", fh.read())


class AllEleven(unittest.TestCase):
    def test_eleven_detectors_and_all_fire_somewhere(self):
        types = set()
        for k in ("cheese", "veg", "honey"):
            types |= {a.type for a in detect_all(build_farm(k), BENCH)}
        self.assertEqual(len(types), 11, types)


if __name__ == "__main__":
    unittest.main()
