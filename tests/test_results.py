"""Сводка «что принесли принятые дела»: копится по дням, не ломается откатом, журнал убран из интерфейса."""
import pathlib
import unittest

from engine.api import dispatch
from engine.service import Assistant

ROOT = pathlib.Path(__file__).resolve().parent.parent


def confirm_first(a, n=1, farm="cheese"):
    acts = dispatch(a, "GET", f"/api/farms/{farm}/today")[1]["actions"][:n]
    for act in acts:
        prev = dispatch(a, "POST", f"/api/farms/{farm}/preview", {"key": act["key"]})[1]
        conf = dispatch(a, "POST", f"/api/farms/{farm}/confirm", {"key": act["key"], "change_hash": prev["change_hash"]})[1]
        st, _ = dispatch(a, "POST", f"/api/farms/{farm}/execute", {"key": act["key"], "token": conf["token"]})
        assert st == 200
    return acts


def results(a, farm="cheese"):
    return dispatch(a, "GET", f"/api/farms/{farm}/today")[1]["results"]


class Results(unittest.TestCase):
    def test_empty_until_something_is_confirmed(self):
        a = Assistant(llm=None)
        self.assertEqual(results(a)["items"], [])

    def test_confirmed_today_waits_for_the_first_day(self):
        a = Assistant(llm=None)
        confirm_first(a)
        r = results(a)
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual((r["items"][0]["status"], r["items"][0]["days"], r["days"]), ("wait", 0, 0))

    def test_effect_accumulates_day_by_day_and_survives_next_days(self):
        a = Assistant(llm=None)
        confirm_first(a, 2)
        dispatch(a, "POST", "/api/farms/cheese/nextday", {})
        r1 = results(a)
        self.assertEqual({i["days"] for i in r1["items"]}, {1})
        dispatch(a, "POST", "/api/farms/cheese/nextday", {})
        r2 = results(a)
        self.assertEqual({i["days"] for i in r2["items"]}, {2})
        self.assertEqual(r2["days"], 2)
        self.assertEqual(r2["total_fact"], sum(i["fact"] for i in r2["items"]))
        self.assertTrue(all(i["status"] in ("works", "none") for i in r2["items"]))  # до недели вердикта нет

    def test_week_gives_a_verdict_and_next_day_after_rewind_keeps_counting(self):
        a = Assistant(llm=None)
        confirm_first(a)
        self.assertEqual(dispatch(a, "POST", "/api/farms/cheese/rewind", {})[0], 200)
        item = results(a)["items"][0]
        self.assertEqual(item["days"], 7)
        self.assertIn(item["status"], ("in_range", "above", "below", "not_worked"))
        dispatch(a, "POST", "/api/farms/cheese/nextday", {})
        self.assertEqual(results(a)["items"][0]["days"], 8)

    def test_weekly_report_covers_actions_from_earlier_days_once(self):
        a = Assistant(llm=None)
        first = confirm_first(a)[0]
        dispatch(a, "POST", "/api/farms/cheese/nextday", {})
        second = confirm_first(a)[0]
        self.assertNotEqual(first["key"], second["key"])
        st, body = dispatch(a, "POST", "/api/farms/cheese/rewind", {})
        self.assertEqual(st, 200)
        rows = {r["key"]: r for r in body["report"]["actions"]}
        self.assertEqual({first["key"], second["key"]}, set(rows))
        self.assertEqual((rows[first["key"]]["days"], rows[second["key"]]["days"]), (8, 7))  # факт копится с дня подтверждения
        self.assertEqual(sum(len(v) for v in a.cal["cheese"].values()), 2)  # в калибровку каждое дело попало один раз
        self.assertEqual(dispatch(a, "POST", "/api/farms/cheese/rewind", {})[0], 409)  # все дела уже проверены

    def test_rolled_back_action_is_not_tracked_and_other_farms_are_separate(self):
        a = Assistant(llm=None)
        acts = dispatch(a, "GET", "/api/farms/cheese/today")[1]["actions"]
        prev = dispatch(a, "POST", "/api/farms/cheese/preview", {"key": acts[0]["key"]})[1]
        conf = dispatch(a, "POST", "/api/farms/cheese/confirm", {"key": acts[0]["key"], "change_hash": prev["change_hash"]})[1]
        ex = dispatch(a, "POST", "/api/farms/cheese/execute", {"key": acts[0]["key"], "token": conf["token"]})[1]
        dispatch(a, "POST", "/api/farms/cheese/rollback", {"execution_id": ex["execution"]["id"] if "execution" in ex else ex["id"]})
        self.assertEqual(results(a)["items"], [])
        self.assertEqual(results(a, "veg")["items"], [])

    def test_reset_clears_the_summary(self):
        a = Assistant(llm=None)
        confirm_first(a)
        dispatch(a, "POST", "/api/reset", {})
        self.assertEqual(results(a)["items"], [])


class Interface(unittest.TestCase):
    def test_action_journal_is_gone_from_the_screen_but_kept_in_the_backend(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Журнал действий", html)
        self.assertNotIn("auditBody", html)
        self.assertIn('id="results"', html)
        a = Assistant(llm=None)
        self.assertEqual(dispatch(a, "GET", "/api/farms/cheese/audit")[0], 200)  # запись событий продолжается


if __name__ == "__main__":
    unittest.main()
