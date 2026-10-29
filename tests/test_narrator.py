"""ИИ-тексты: фактчекер, модерация, запасной шаблон, автомат защиты и контроль качества.
Запуск: python3 -m unittest discover -s tests -t .
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from engine.benchmarks import BENCHMARKS
from engine.factcheck import allowed_from_action, allowed_from_texts, check_text
from engine.generate import SPECS, build_farm
from engine.llm import DemoLLM, LLMError, provider_from_env
from engine.moderation import moderate
from engine.narrator import BREAKER_FAILS, BREAKER_PAUSE, Narrator
from engine.privacy import usable_benchmarks
from engine.service import Assistant
from engine.signals import detect_all
from engine.templates import explanation

BENCH = usable_benchmarks(BENCHMARKS)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Scripted:
    """Модель, которая отвечает по сценарию; считает вызовы."""
    name = "scripted"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def cache_token(self):
        return "s"

    def complete(self, system, user):
        self.calls += 1
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


def tvorog():
    farm = build_farm("cheese")
    return farm, next(a for a in detect_all(farm, BENCH) if a.key == "spoilage:tvorog")


class FactCheck(unittest.TestCase):
    def setUp(self):
        self.farm, self.a = tvorog()
        self.allowed = allowed_from_action(self.a)

    def test_text_built_from_facts_passes(self):
        ok = "Творог 9% скоро испортится: 34 кг, срок истекает через 3 дня (чт, 29.10). Ожидаем около 3 700 ₽ за неделю."
        self.assertTrue(check_text(ok.replace("Творог 9%", "«Творог 9%»"), self.allowed).ok)

    def test_invented_number_is_caught(self):
        res = check_text("Продаётся 12 кг в день, ожидаем 3 700 ₽.", self.allowed)
        self.assertEqual([i.token for i in res.issues if i.kind == "number"], ["12"])

    def test_invented_percent_and_big_sum(self):
        res = check_text("Это на 43% больше и принесёт 99 900 ₽.", self.allowed)
        self.assertEqual({i.token for i in res.issues}, {"43", "99900"})

    def test_thousands_notation_and_rounding(self):
        self.assertTrue(check_text("Около 3,7 тыс. ₽ за неделю.", self.allowed).ok)
        self.assertTrue(check_text("Около 3 715 ₽ за неделю.", self.allowed).ok)
        self.assertFalse(check_text("Около 6 700 ₽ за неделю.", self.allowed).ok)

    def test_dates_must_come_from_facts(self):
        self.assertTrue(check_text("Срок истекает 29.10.", self.allowed).ok)
        res = check_text("Срок истекает 30.10.", self.allowed)
        self.assertTrue(any(i.kind == "date" for i in res.issues))

    def test_number_words_are_checked(self):
        self.assertTrue(check_text("Срок истекает через три дня.", self.allowed).ok)
        self.assertFalse(check_text("Срок истекает через пять дней.", self.allowed).ok)
        self.assertTrue(check_text("Это одно из дел на сегодня.", self.allowed).ok)

    def test_unknown_product_name_is_caught(self):
        res = check_text("Скидка на «Сулугуни» поможет.", self.allowed)
        self.assertTrue(any(i.kind == "name" for i in res.issues))
        self.assertTrue(check_text("Скидка на «Творог 9%» поможет.", self.allowed).ok)

    def test_style_limits(self):
        self.assertFalse(check_text("", self.allowed).ok)
        self.assertFalse(check_text("a" * 50, self.allowed).ok)
        self.assertFalse(check_text("Очень " * 120, self.allowed).ok)

    def test_horizon_week_is_not_an_invention(self):
        self.assertTrue(check_text("Оценка на 7 дней.", self.allowed).ok)


class Moderation(unittest.TestCase):
    def test_each_forbidden_claim_is_caught(self):
        bad = ["Органический сыр", "Эко-продукт", "Лечебный мёд", "Укрепляет иммунитет", "Без ГМО и без консервантов",
               "Самый вкусный сыр", "Лучший сыр региона", "Гарантированно продадите", "100% результат",
               "Вдвое больше продаж", "Пишите на https://example.com", "Звоните +7 (900) 123-45-67"]
        for text in bad:
            self.assertTrue(moderate(text), text)

    def test_normal_words_are_not_flagged(self):
        for text in ["Лучше остановить акцию.", "Экономия времени.", "Сыр созревает 60 дней.", "Самовывоз по пятницам."]:
            self.assertEqual(moderate(text), [], text)

    def test_confirmed_claims_are_allowed(self):
        self.assertTrue(moderate("Органический сыр"))
        self.assertEqual(moderate("Органический сыр", confirmed=["organic"]), [])
        self.assertTrue(moderate("Лечебный мёд", confirmed=["organic", "free_from"]))  # медицинские обещания не снимаются


class NarratorFlow(unittest.TestCase):
    def setUp(self):
        self.farm, self.a = tvorog()
        self.clock = Clock()
        self.events = []

    def narrator(self, llm):
        return Narrator(llm, self.clock, lambda *e: self.events.append(e))

    def test_good_text_is_accepted(self):
        n = self.narrator(DemoLLM("good")).narrate(self.farm, self.a)
        self.assertEqual((n.source, n.reason, n.attempts), ("llm", None, 1))

    def test_hallucination_falls_back_to_template(self):
        n = self.narrator(DemoLLM("hallucinate")).narrate(self.farm, self.a)
        self.assertEqual((n.source, n.reason), ("template", "factcheck"))
        self.assertEqual(n.text, explanation(self.a))
        self.assertEqual(self.events[0][0], "llm_rejected")

    def test_unsafe_text_falls_back(self):
        n = self.narrator(DemoLLM("unsafe")).narrate(self.farm, self.a)
        self.assertEqual((n.source, n.reason), ("template", "moderation"))

    def test_unavailable_model_falls_back(self):
        n = self.narrator(DemoLLM("down")).narrate(self.farm, self.a)
        self.assertEqual((n.source, n.reason), ("template", "unavailable"))
        self.assertEqual(self.events[0][0], "llm_fallback")

    def test_no_model_means_templates(self):
        n = self.narrator(None).narrate(self.farm, self.a)
        self.assertEqual((n.source, n.reason, n.provider), ("template", "disabled", None))

    def test_second_attempt_can_fix_the_text(self):
        good = "Срок годности истекает 29.10, ожидаем около 3 700 ₽ за неделю."
        llm = Scripted("Принесёт 99 900 ₽.", good)
        n = self.narrator(llm).narrate(self.farm, self.a)
        self.assertEqual((n.source, n.attempts, llm.calls), ("llm", 2, 2))
        self.assertEqual(self.narrator(llm).quality()["provider"], "scripted")

    def test_retry_prompt_contains_the_reason(self):
        seen = []

        class Spy(Scripted):
            def complete(self, system, user):
                seen.append(user)
                return super().complete(system, user)

        self.narrator(Spy("Принесёт 99 900 ₽.", "Принесёт 77 700 ₽.")).narrate(self.farm, self.a)
        self.assertIn("отклонён", seen[1])
        self.assertIn("99900", seen[1])

    def test_circuit_breaker_stops_calling_a_dead_model(self):
        llm = Scripted(LLMError("boom"))
        n = self.narrator(llm)
        farm = build_farm("cheese")
        acts = detect_all(farm, BENCH)[:BREAKER_FAILS + 2]
        for a in acts:
            n.narrate(farm, a)
        self.assertEqual(llm.calls, BREAKER_FAILS)  # дальше модель не дёргаем
        self.assertEqual(n.narrate(farm, acts[-1]).source, "template")
        self.clock.now += BREAKER_PAUSE + timedelta(seconds=1)
        llm.replies = ["Ок, ожидаем около 3 700 ₽."]
        n.clear_cache()
        farm2, a2 = tvorog()
        self.assertEqual(n.narrate(farm2, a2).source, "llm")

    def test_results_are_cached(self):
        llm = Scripted("Срок истекает 29.10, ожидаем около 3 700 ₽.")
        n = self.narrator(llm)
        n.narrate(self.farm, self.a)
        n.narrate(self.farm, self.a)
        self.assertEqual(llm.calls, 1)

    def test_narration_never_changes_the_numbers(self):
        before = (self.a.effect, self.a.title, list(self.a.why), dict(self.a.change))
        self.narrator(DemoLLM("hallucinate")).narrate(self.farm, self.a)
        self.assertEqual(before, (self.a.effect, self.a.title, list(self.a.why), dict(self.a.change)))

    def test_templates_always_pass_our_own_checks(self):
        for key in SPECS:
            farm = build_farm(key)
            for a in detect_all(farm, BENCH):
                text = explanation(a)
                res = check_text(text, allowed_from_action(a))
                self.assertTrue(res.ok, (a.key, [i.message for i in res.issues]))
                self.assertEqual(moderate(text), [], a.key)

    def test_our_own_facts_pass_moderation(self):
        # DemoLLM и настоящая модель пересказывают факты: запретная формулировка в них отклонила бы любой текст
        for key in SPECS:
            farm = build_farm(key)
            for a in detect_all(farm, BENCH):
                self.assertEqual(moderate(" ".join([a.title, *a.why, a.effect_note])), [], a.key)

    def test_quality_counters(self):
        n = self.narrator(DemoLLM("good"))
        farm = build_farm("cheese")
        for a in detect_all(farm, BENCH)[:3]:
            n.narrate(farm, a)
        q = n.quality()
        self.assertEqual((q["texts"], q["accepted"], q["accept_rate"]), (3, 3, 1.0))


class Drafts(unittest.TestCase):
    def setUp(self):
        self.farm = build_farm("cheese")
        self.a = next(a for a in detect_all(self.farm, BENCH) if a.type == "leaky_card")

    def draft(self, mode):
        return Narrator(DemoLLM(mode), Clock()).narrate(self.farm, self.a).draft

    def test_good_draft_is_accepted_and_keeps_placeholders(self):
        d = self.draft("good")
        self.assertEqual(d["source"], "llm")
        self.assertIn("[укажите состав]", d["description"])
        self.assertIn("60 дн.", d["description"])  # срок годности берём из карточки

    def test_unsafe_draft_is_replaced_by_template(self):
        d = self.draft("unsafe")
        self.assertEqual(d["source"], "template")
        for word in ("органическ", "лечебн", "иммунитет", "ГМО", "Самый"):
            self.assertNotIn(word, d["description"])

    def test_invented_characteristics_are_rejected(self):
        d = self.draft("hallucinate")  # «срок годности 45 дней, вес 2 кг»
        self.assertEqual(d["source"], "template")
        self.assertIn("60 дн.", d["description"])

    def test_garbage_format_falls_back(self):
        n = Narrator(Scripted("не json"), Clock())
        self.assertEqual(n.narrate(self.farm, self.a).draft["source"], "template")

    def test_json_in_code_fence_is_understood(self):
        reply = '```json\n{"title": "Качотта", "description": "Качотта — [вкус]. Срок годности: 60 дн."}\n```'
        self.assertEqual(Narrator(Scripted(reply), Clock()).narrate(self.farm, self.a).draft["source"], "llm")

    def test_confirmed_organic_is_allowed_in_draft(self):
        reply = json.dumps({"title": "Качотта", "description": "Органический сыр Качотта, срок годности 60 дн."}, ensure_ascii=False)
        self.assertEqual(Narrator(Scripted(reply), Clock()).narrate(self.farm, self.a).draft["source"], "template")
        self.farm.state["certifications"] = ["organic"]
        self.assertEqual(Narrator(Scripted(reply), Clock()).narrate(self.farm, self.a).draft["source"], "llm")


class Integration(unittest.TestCase):
    def test_today_view_has_narrations_and_audits_rejections(self):
        a = Assistant(secret=b"s", llm=DemoLLM("hallucinate"))
        view = a.today_view("cheese")
        self.assertTrue(all(x["narration"]["source"] == "template" for x in view["actions"]))
        events = [e["event"] for e in a.audit.entries]
        self.assertEqual(events.count("llm_rejected"), 3)
        self.assertTrue(a.audit.verify()[0])
        a.set_llm_mode("good")
        view = a.today_view("cheese")
        self.assertTrue(all(x["narration"]["source"] == "llm" for x in view["actions"]))
        self.assertEqual(a.quality()["rejected_numbers"], 3)

    def test_preview_returns_checked_draft(self):
        a = Assistant(secret=b"s", llm=DemoLLM("good"))
        for k in ("stockout:mozzarella", "promo_loss:brynza", "spoilage:tvorog"):
            a.dismiss("cheese", k, "later")
        self.assertEqual(a.preview("cheese", "leaky_card:kachotta")["draft"]["source"], "llm")

    def test_llm_mode_only_for_demo(self):
        a = Assistant(secret=b"s", llm=None)
        self.assertEqual(a.llm_info()["provider"], None)
        with self.assertRaises(Exception):
            a.set_llm_mode("good")
        self.assertIsNone(a.today_view("cheese")["actions"][0]["narration"]["provider"])

    def test_provider_from_env(self):
        self.assertEqual(provider_from_env({"LLM_PROVIDER": "off"}), None)
        self.assertEqual(provider_from_env({}).name, "demo")
        real = provider_from_env({"LLM_BASE_URL": "http://x/v1/", "LLM_API_KEY": "k", "LLM_MODEL": "m"})
        self.assertEqual((real.base_url, real.model), ("http://x/v1", "m"))

    def test_unreachable_real_model_means_templates_not_errors(self):
        real = provider_from_env({"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_TIMEOUT": "1"})
        a = Assistant(secret=b"s", llm=real)
        view = a.today_view("cheese")
        self.assertEqual(len(view["actions"]), 3)
        self.assertTrue(all(x["narration"]["source"] == "template" for x in view["actions"]))


if __name__ == "__main__":
    unittest.main()


class Declensions(unittest.TestCase):
    def test_inflected_product_names_are_accepted_but_other_products_are_not(self):
        farm, a = tvorog()
        allowed = allowed_from_action(a)
        self.assertTrue(check_text("На складе 34 кг «Творога 9%», срок истекает 29.10.", allowed).ok)
        self.assertTrue(check_text("Скидка на «Творог 9%» поможет.", allowed).ok)
        self.assertFalse(check_text("На складе 34 кг «Сулугуни».", allowed).ok)
        self.assertFalse(check_text("Скидка на «Творог 20%» поможет.", allowed).ok)
