"""Движок обязан находить заложенные проблемы и соблюдать стоп-правила.
Запуск: python3 -m unittest discover -s tests -t .
"""
import copy
import json
import unittest
from datetime import timedelta

from engine.benchmarks import BENCHMARKS
from engine.generate import SPECS, build_farm
from engine.privacy import K_MIN, pseudonymize, usable_benchmarks
from engine.render import report, selection_to_dict
from engine.run import recommend
from engine.scoring import MAX_DISCOUNT, prioritize, snooze, validate
from engine.signals import DETECTORS, Action, detect_all

BENCH = usable_benchmarks(BENCHMARKS)


def keys(actions):
    return {a.key for a in actions}


class PlantedProblems(unittest.TestCase):
    """Каждая заложенная проблема должна быть найдена своим детектором."""

    @classmethod
    def setUpClass(cls):
        cls.found = {k: keys(detect_all(build_farm(k), BENCH)) for k in SPECS}

    def test_cheese_farm(self):
        want = {"spoilage:tvorog", "stockout:mozzarella", "leaky_card:kachotta", "churn:regulars",
                "promo_loss:brynza", "bundle:honey+kachotta", "hygiene:cards"}
        self.assertTrue(want <= self.found["cheese"], want - self.found["cheese"])

    def test_veg_farm(self):
        want = {"spoilage:tomato", "seasonal:Тыква и бахча"}
        self.assertTrue(want <= self.found["veg"], want - self.found["veg"])

    def test_honey_farm(self):
        want = {"price_off:lipa", "seasonal:grech", "churn:regulars"}
        self.assertTrue(want <= self.found["honey"], want - self.found["honey"])

    def test_all_nine_detectors_fire_somewhere(self):
        types = set()
        for k in SPECS:
            types |= {a.type for a in detect_all(build_farm(k), BENCH)}
        self.assertEqual(len(types), len(DETECTORS), types)

    def test_no_false_positives_on_healthy_products(self):
        cheese = keys(detect_all(build_farm("cheese"), BENCH))
        for healthy in ("spoilage:mozzarella", "leaky_card:suluguni", "stockout:tvorog", "price_off:honey"):
            self.assertNotIn(healthy, cheese)

    def test_churn_counts_the_planted_customers(self):
        churn = next(a for a in detect_all(build_farm("cheese"), BENCH) if a.type == "churn")
        self.assertTrue(10 <= len(churn.change["segment"]) <= 14, len(churn.change["segment"]))


class Selection(unittest.TestCase):
    def test_cheese_top_three_and_order(self):
        _, sel = recommend("cheese")
        self.assertEqual([a.key for a in sel.selected][0], "spoilage:tvorog")
        self.assertEqual(len(sel.selected), 3)
        self.assertEqual(sel.selected, sorted(sel.selected, key=lambda a: a.priority, reverse=True))

    def test_urgency_beats_bigger_effect(self):
        _, sel = recommend("cheese")
        urgent, leaky = sel.selected[0], next(a for a in sel.deferred if a.type == "leaky_card")
        self.assertLess(urgent.effect_mid, leaky.effect_mid)
        self.assertGreater(urgent.priority, leaky.priority)

    def test_diversity_one_per_type_and_product(self):
        for k in SPECS:
            _, sel = recommend(k)
            self.assertEqual(len({a.type for a in sel.selected}), len(sel.selected))
            products = [p for a in sel.selected for p in a.product_ids]
            self.assertEqual(len(products), len(set(products)))

    def test_every_farm_has_a_quick_win(self):
        for k in SPECS:
            _, sel = recommend(k)
            self.assertTrue(any(a.effort <= 2 for a in sel.selected), k)

    def test_rejected_action_is_snoozed_for_14_days(self):
        farm, sel = recommend("cheese")
        first = sel.selected[0]
        snoozed = snooze(first, farm.today)
        _, again = recommend("cheese", snoozed=snoozed)
        self.assertNotIn(first.key, keys(again.selected))
        self.assertIn(first.key, keys(again.snoozed))

    def test_snooze_lifts_when_effect_doubles(self):
        farm, sel = recommend("cheese")
        first = sel.selected[0]
        snoozed = {first.key: {"until": farm.today + timedelta(days=14), "effect": first.effect_mid / 2.5}}
        _, again = recommend("cheese", snoozed=snoozed)
        self.assertIn(first.key, keys(again.selected))

    def test_honesty_no_signals_no_padding(self):
        farm = build_farm("cheese")
        sel = prioritize([], farm)
        self.assertEqual(sel.selected, [])
        self.assertIn("всё в порядке", sel.note)

    def test_fewer_than_three_when_signals_are_weak(self):
        farm = build_farm("cheese")
        actions = [a for a in detect_all(farm, BENCH) if a.key == "spoilage:tvorog"]
        sel = prioritize(actions, farm)
        self.assertEqual(len(sel.selected), 1)
        self.assertIsNotNone(sel.note)


class StopRules(unittest.TestCase):
    def setUp(self):
        self.farm = build_farm("cheese")
        self.base = next(a for a in detect_all(self.farm, BENCH) if a.key == "spoilage:tvorog")

    def _with(self, **change):
        a = copy.deepcopy(self.base)
        a.change.update(change)
        return a

    def test_detector_output_is_valid(self):
        for k in SPECS:
            farm = build_farm(k)
            for a in detect_all(farm, BENCH):
                self.assertEqual(validate(a, farm), [], a.key)

    def test_discount_cap(self):
        self.assertTrue(validate(self._with(discount=MAX_DISCOUNT + 0.05), self.farm))

    def test_price_below_cost_plus_margin(self):
        # скидка 45% опускает цену ниже себестоимости: срабатывают оба правила
        reasons = validate(self._with(discount=0.45), self.farm)
        self.assertTrue(any("себестоимост" in r for r in reasons), reasons)

    def test_conflict_with_active_promo(self):
        a = copy.deepcopy(self.base)
        a.change["product_id"] = "brynza"
        self.assertTrue(any("уже действует" in r for r in validate(a, self.farm)))

    def test_price_change_cooldown(self):
        farm = build_farm("honey")
        a = next(a for a in detect_all(farm, BENCH) if a.key == "price_off:lipa")
        farm.product("lipa").last_price_change = farm.today - timedelta(days=3)
        self.assertTrue(any("меньше 7 дней" in r for r in validate(a, farm)))
        sel = prioritize([a], farm)
        self.assertEqual(sel.selected, [])
        self.assertEqual(len(sel.blocked), 1)

    def test_weak_effect_is_hidden(self):
        a = copy.deepcopy(self.base)
        a.effect = (100, 200, 300)
        sel = prioritize([a], self.farm)
        self.assertEqual(sel.selected, [])
        self.assertEqual(len(sel.weak), 1)


class DataAndPrivacy(unittest.TestCase):
    def test_deterministic(self):
        a, b = recommend("cheese")[1], recommend("cheese")[1]
        self.assertEqual([x.key for x in a.selected], [x.key for x in b.selected])
        self.assertEqual([round(x.effect_mid) for x in a.selected], [round(x.effect_mid) for x in b.selected])

    def test_customers_are_hashed(self):
        farm = build_farm("cheese")
        for o in farm.orders:
            self.assertRegex(o.customer, r"^[0-9a-f]{12}$")
        self.assertEqual(pseudonymize("x"), pseudonymize("x"))
        self.assertNotEqual(pseudonymize("x"), pseudonymize("y"))

    def test_k_anonymity_filters_small_categories(self):
        self.assertIn("Мясо", BENCHMARKS)
        self.assertNotIn("Мясо", BENCH)
        self.assertTrue(all(b.n_farms >= K_MIN for b in BENCH.values()))

    def test_small_category_never_drives_a_recommendation(self):
        farm = build_farm("cheese")
        self.assertFalse([a for a in detect_all(farm, BENCH) if "Мясо" in a.title])

    def test_json_export_requires_confirmation(self):
        farm, sel = recommend("cheese")
        data = json.loads(json.dumps(selection_to_dict(farm, sel), ensure_ascii=False))
        self.assertEqual(len(data["actions"]), 3)
        self.assertTrue(all(a["requires_confirmation"] for a in data["actions"]))
        self.assertTrue(all("op" in a["change"] for a in data["actions"]))

    def test_report_renders_all_cards(self):
        farm, sel = recommend("cheese")
        text = report(farm, sel, audit=True)
        for a in sel.selected:
            self.assertIn(a.title, text)
        self.assertIn("ПОЧЕМУ", text)
        self.assertIn("ЧТО ИЗМЕНИТСЯ ПОСЛЕ ВАШЕГО ПОДТВЕРЖДЕНИЯ", text)


if __name__ == "__main__":
    unittest.main()
