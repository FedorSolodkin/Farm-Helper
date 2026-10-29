"""Чат не помнит прошлых дней; кнопка «Следующий день» для теста."""
import unittest
from datetime import date

from engine.api import dispatch
from engine.errors import ApiError
from engine.llm import DemoLLM
from engine.service import Assistant


def confirm(a, key, action_key, second=False):
    pv = a.preview(key, action_key)
    tok = a.confirm(key, action_key, pv["change_hash"], second_confirmation=second)["token"]
    return a.execute(key, action_key, tok)["execution"]


class ChatAcrossDays(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=DemoLLM())

    def test_total_earnings_question_sums_todays_actions(self):
        r = self.a.chat("cheese", "сколько денег мы можем заработать?")
        self.assertEqual(r["intent"], "total")
        view = self.a.today_view("cheese")
        for x in view["actions"]:
            self.assertIn(x["title"], r["text"])
        self.assertIn("прогноз", r["text"])

    def test_this_deal_question_still_goes_to_the_deal(self):
        self.a.chat("cheese", "почему творог?")
        r = self.a.chat("cheese", "сколько мы заработаем по этому делу?")
        self.assertEqual(r["intent"], "effect")
        self.assertIn("Творог", r["text"])

    def test_effect_answer_names_the_deal(self):
        r = self.a.chat("cheese", "как посчитан эффект?")
        self.assertTrue(r["text"].startswith("Дело: "))

    def test_context_from_a_closed_deal_is_not_used(self):
        self.a.chat("cheese", "почему творог?")
        confirm(self.a, "cheese", "spoilage:tvorog")  # дело сделано и ушло из списка
        r = self.a.chat("cheese", "как посчитан эффект?")
        shown = [x["title"] for x in self.a.today_view("cheese")["actions"]]
        self.assertTrue(any(t in r["text"] for t in shown), r["text"])
        self.assertNotIn("скидку", r["text"].split("\n")[0].lower() if "Творог" in r["text"] else "")

    def test_rewind_forgets_the_previous_week(self):
        self.a.chat("cheese", "почему творог?")
        self.assertTrue(self.a.chat_ctx["cheese"])
        confirm(self.a, "cheese", "spoilage:tvorog")
        self.a.rewind("cheese")
        self.assertEqual(self.a.chat_ctx["cheese"], {})
        view = self.a.today_view("cheese")
        r = self.a.chat("cheese", "сколько денег мы можем заработать?")
        self.assertEqual(r["intent"], "total")
        for x in view["actions"]:
            self.assertIn(x["title"], r["text"])


class NextDay(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=None)
        self.farm = self.a.farms["cheese"]

    def test_moves_one_day_and_adds_orders(self):
        start, orders = self.farm.today, len(self.farm.orders)
        res = self.a.next_day("cheese")
        self.assertEqual(self.farm.today, date.fromordinal(start.toordinal() + 1))
        self.assertEqual(res["date"], self.farm.today.isoformat())
        self.assertGreater(len(self.farm.orders), orders)
        self.assertEqual(self.a.today_view("cheese")["date"], self.farm.today.isoformat())

    def test_yesterdays_confirmations_do_not_carry_over(self):
        confirm(self.a, "cheese", "spoilage:tvorog")
        self.assertEqual(len(self.a.today_view("cheese")["done"]), 1)
        self.a.next_day("cheese")
        view = self.a.today_view("cheese")
        self.assertEqual((len(view["done"]), view["summary"]["total"]), (0, 3))
        self.assertFalse(view["can_rewind"])
        with self.assertRaises(ApiError) as cm:
            self.a.rewind("cheese")
        self.assertEqual(cm.exception.code, "nothing_to_check")

    def test_no_report_is_created_and_audit_has_the_step(self):
        self.a.next_day("cheese")
        self.assertEqual(self.a.reports["cheese"], [])
        self.assertEqual(self.a.audit.entries[-1]["event"], "next_day")
        self.assertTrue(self.a.audit.verify()[0])

    def test_can_step_several_days_and_still_recommends(self):
        for _ in range(5):
            self.a.next_day("cheese")
        view = self.a.today_view("cheese")
        self.assertTrue(view["actions"])
        self.assertEqual(self.farm.today.toordinal() - date(2026, 10, 26).toordinal(), 5)

    def test_forgets_the_chat_context(self):
        self.a.chat("cheese", "почему творог?")
        self.a.next_day("cheese")
        self.assertEqual(self.a.chat_ctx["cheese"], {})

    def test_route_and_screen(self):
        status, body = dispatch(self.a, "POST", "/api/farms/cheese/nextday", {})
        self.assertEqual(status, 200)
        self.assertIn("date", body)
        self.assertEqual(dispatch(self.a, "POST", "/api/farms/nope/nextday", {})[0], 404)
        with open("web/index.html", encoding="utf-8") as fh:
            self.assertIn("Следующий день (для теста)", fh.read())


if __name__ == "__main__":
    unittest.main()
