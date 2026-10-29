"""Знакомство, выбор предприятия и загрузка своих данных по шаблону."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from engine.api import dispatch
from engine.benchmarks import BENCHMARKS
from engine.errors import ApiError
from engine.generate import build_farm
from engine.hub import Hub
from engine.importer import MAX_ERRORS_SHOWN, MAX_PRODUCTS, export_example, parse, template, templates
from engine.llm import DemoLLM
from engine.privacy import usable_benchmarks
from engine.server import make_server
from engine.service import Assistant
from engine.signals import detect_all

BENCH = usable_benchmarks(BENCHMARKS)
PROFILE = {"owner": "Пётр", "farm_name": "Ферма Петрова"}


def files():
    return export_example(build_farm("cheese"))


class Parsing(unittest.TestCase):
    def test_example_rows_in_templates_are_ignored(self):
        data = files()
        data["products"] += "ПРИМЕР X;Товар;Сыры;кг;1;1;1;да;да;да;нет\n"
        farm, rep = parse(data, PROFILE)
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(len(farm.products), 11)
        self.assertTrue(template("products")["content"].startswith("﻿"))
        self.assertEqual(len(templates()), 5)

    def test_latin_headers_commas_delimiter_and_decimal_commas(self):
        data = {
            "products": "sku,name,price,cost\nA,Сыр,\"1 450,50\",800\nB,Мёд,900 ₽,500\n",
            "stock": "sku\tqty\nA\t5\nB\t10\n",
            "sales": "date;customer;sku;qty;price\n" + "".join(
                f"{d:02d}.10.2026;c{d};A;1,5;1450\n{d:02d}.10.2026;c{d};B;1;900\n" for d in range(1, 21)),
        }
        farm, rep = parse(data, PROFILE)
        self.assertTrue(rep.ok, rep.errors)
        self.assertEqual(farm.product("A").price, 1450.5)
        self.assertEqual(farm.product("B").price, 900)
        self.assertEqual(str(farm.today), "2026-10-21")  # на следующий день после последней продажи
        self.assertEqual(farm.product("A").unit, "шт")  # значения по умолчанию

    def test_round_trip_keeps_the_same_advice_for_every_demo_farm(self):
        for key in ("cheese", "veg", "honey"):
            src = build_farm(key)
            farm, rep = parse(export_example(src), PROFILE)
            self.assertTrue(rep.ok, (key, rep.errors))
            self.assertEqual({a.key for a in detect_all(src, BENCH)}, {a.key for a in detect_all(farm, BENCH)}, key)

    def test_customers_become_hashes(self):
        data = files()
        farm, _ = parse(data, PROFILE)
        self.assertTrue(all(len(o.customer) == 12 for o in farm.orders))
        data["sales"] = data["sales"].replace(data["sales"].splitlines()[1].split(";")[1], "+7 900 111-22-33")
        farm2, _ = parse(data, PROFILE)
        blob = json.dumps([(o.customer, o.day.isoformat()) for o in farm2.orders])
        self.assertNotIn("111-22-33", blob)  # телефон нигде не хранится
        farm3, _ = parse(data, PROFILE)
        self.assertEqual([o.customer for o in farm2.orders][:5], [o.customer for o in farm3.orders][:5])  # хэш стабилен

    def test_names_are_sanitized(self):
        data = files()
        data["products"] = data["products"].replace("Творог 9%", "Творог <script>alert(1)</script>", 1)
        farm, _ = parse(data, {"owner": "<b>Пётр</b>", "farm_name": "Ферма <i>X</i>"})
        names = [p.name for p in farm.products] + [farm.name, farm.owner]
        self.assertTrue(all("<" not in n and ">" not in n for n in names), names)


class Errors(unittest.TestCase):
    def test_missing_required_files_and_columns(self):
        _, rep = parse({"products": files()["products"]}, PROFILE)
        self.assertFalse(rep.ok)
        self.assertEqual({e["file"] for e in rep.errors}, {"Остатки", "Продажи"})
        _, rep = parse({**files(), "stock": "артикул;шт\nA;5\n"}, PROFILE)
        self.assertTrue(any("нет столбцов" in e["message"] and "количество" in e["message"] for e in rep.errors))

    def test_row_numbers_and_human_messages(self):
        data = files()
        lines = data["sales"].splitlines()
        lines[4] = lines[4].replace(";", ";", 1).rsplit(";", 3)[0] + ";abc;100;x"
        lines[7] = "32.13.2026" + lines[7][lines[7].index(";"):]
        data["sales"] = "\n".join(lines) + "\n"
        _, rep = parse(data, PROFILE)
        msgs = {(e["row"], e["message"]) for e in rep.errors}
        self.assertTrue(any(r == 5 and "количество" in m for r, m in msgs), msgs)
        self.assertTrue(any(r == 8 and "дата" in m and "ДД.ММ.ГГГГ" in m for r, m in msgs), msgs)
        self.assertTrue(all(e["file"] == "Продажи" for e in rep.errors))

    def test_bad_product_does_not_flood_the_report(self):
        data = files()
        data["products"] = data["products"].replace("1450", "abc", 1)
        _, rep = parse(data, PROFILE)
        self.assertEqual(rep.total_errors, 1)
        self.assertIn("Товары", rep.errors[0]["file"])

    def test_unknown_sku_and_duplicates(self):
        data = files()
        data["stock"] += "ЧУЖОЙ;5;\n"
        data["products"] += data["products"].splitlines()[1] + "\n"
        _, rep = parse(data, PROFILE)
        text = " | ".join(e["message"] for e in rep.errors)
        self.assertIn("повторяется", text)
        self.assertIn("«ЧУЖОЙ» не найден", text)

    def test_needs_two_weeks_of_history(self):
        data = files()
        head = data["sales"].splitlines()
        data["sales"] = "\n".join(head[:1] + [l for l in head[1:] if l.split(";")[0].startswith(("25.10", "26.10", "27.10"))]) + "\n"
        _, rep = parse(data, PROFILE)
        self.assertTrue(any("минимум 14" in e["message"] for e in rep.errors), rep.errors)

    def test_error_list_is_capped_but_counted(self):
        data = files()
        data["sales"] = data["sales"].split("\n")[0] + "\n" + "\n".join(f"xx;c;kachotta;1;100" for _ in range(200)) + "\n"
        _, rep = parse(data, PROFILE)
        self.assertEqual(len(rep.errors), MAX_ERRORS_SHOWN)
        self.assertGreater(rep.total_errors, MAX_ERRORS_SHOWN)

    def test_limits(self):
        data = files()
        rows = ["артикул;название;цена;себестоимость"] + [f"S{i};Т{i};10;5" for i in range(MAX_PRODUCTS + 1)]
        _, rep = parse({**data, "products": "\n".join(rows)}, PROFILE)
        self.assertTrue(any("слишком много товаров" in e["message"] for e in rep.errors))
        _, rep = parse({**data, "sales": "a" * 4_100_000}, PROFILE)
        self.assertTrue(any("слишком большой" in e["message"] for e in rep.errors))

    def test_warnings_not_errors(self):
        data = files()
        data["products"] = data["products"].replace(";Сыры;", ";Экзотика;", 1)
        farm, rep = parse({k: v for k, v in data.items() if k != "views"}, PROFILE)
        self.assertTrue(rep.ok)
        text = " ".join(rep.warnings)
        self.assertIn("Экзотика", text)
        self.assertIn("просмотров", text)


class Service(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=DemoLLM())

    def test_demo_registration(self):
        v = self.a.register({"owner": "Пётр", "farm_name": "Ферма", "region": "Тула", "interests": ["Мёд и пчеловодство"], "demo_key": "honey"})
        self.assertEqual((v["active"], v["profile"]["owner"]), ("honey", "Пётр"))
        view = self.a.today_view("honey")
        self.assertEqual(view["farm"]["owner"], "Пётр")  # приветствие по имени пользователя, а не демо-персонажа
        self.assertIn("Пётр", self.a.chat("honey", "привет")["text"])

    def test_registration_validation(self):
        for bad in ({"farm_name": "Ф", "demo_key": "veg"}, {"owner": "П", "demo_key": "veg"}, {"owner": "П", "farm_name": "Ф", "demo_key": "нет"}):
            with self.assertRaises(ApiError):
                self.a.register(bad)

    def test_dry_run_does_not_install_but_import_does(self):
        r = self.a.import_data(files(), PROFILE, dry_run=True)
        self.assertTrue(r["ok"] and not r["imported"])
        self.assertNotIn("mine", self.a.farms)
        r = self.a.import_data(files(), PROFILE)
        self.assertTrue(r["imported"])
        self.assertEqual((r["active"], r["profile"]["farm_name"]), ("mine", "Ферма Петрова"))
        self.assertTrue(any(f["custom"] for f in r["farms"]))
        view = self.a.today_view("mine")
        self.assertEqual(view["farm"]["name"], "Ферма Петрова")
        self.assertEqual(len(view["actions"]), 3)

    def test_bad_files_are_not_installed(self):
        r = self.a.import_data({"products": "x"}, PROFILE)
        self.assertFalse(r["ok"] or r["imported"])
        self.assertNotIn("mine", self.a.farms)

    def test_import_needs_names(self):
        with self.assertRaises(ApiError):
            self.a.import_data(files(), {"owner": "", "farm_name": ""})

    def test_whole_pipeline_works_on_uploaded_data(self):
        self.a.import_data(files(), PROFILE)
        for _ in range(2):
            act = self.a.today_view("mine")["actions"][0]
            pv = self.a.preview("mine", act["key"])
            self.a.execute("mine", act["key"], self.a.confirm("mine", act["key"], pv["change_hash"],
                                                              second_confirmation=not pv["reversible"])["token"])
        self.assertEqual(len(self.a.today_view("mine")["done"]), 2)
        report = self.a.rewind("mine")["report"]
        self.assertEqual(report["totals"]["count"], 2)
        self.assertEqual(self.a.chat("mine", "что сегодня?")["intent"], "today")
        self.assertTrue(self.a.audit.verify()[0])

    def test_reset_rebuilds_uploaded_farm_and_clear_removes_it(self):
        self.a.import_data(files(), PROFILE)
        act = self.a.today_view("mine")["actions"][0]
        pv = self.a.preview("mine", act["key"])
        self.a.execute("mine", act["key"], self.a.confirm("mine", act["key"], pv["change_hash"])["token"])
        self.a.reset()
        self.assertIn("mine", self.a.farms)
        self.assertEqual(len(self.a.today_view("mine")["done"]), 0)
        self.assertEqual(self.a.profile["owner"], "Пётр")
        v = self.a.clear_profile()
        self.assertEqual((v["profile"], v["active"], v["has_custom"]), (None, None, False))
        with self.assertRaises(ApiError):
            self.a.today_view("mine")

    def test_second_upload_replaces_the_first(self):
        self.a.import_data(files(), PROFILE)
        self.a.import_data(export_example(build_farm("veg")), {"owner": "Пётр", "farm_name": "Новая"})
        self.assertEqual(self.a.farms["mine"].name, "Новая")
        self.assertIn("glut:broccoli", [x["key"] for x in self.a.today_view("mine")["actions"] + self.a.today_view("mine")["deferred"]])

    def test_routes(self):
        self.assertEqual(dispatch(self.a, "GET", "/api/profile")[1]["profile"], None)
        st, body = dispatch(self.a, "GET", "/api/templates")
        self.assertEqual((st, len(body["templates"])), (200, 5))
        self.assertEqual(dispatch(self.a, "GET", "/api/example/cheese")[0], 200)
        self.assertEqual(dispatch(self.a, "GET", "/api/example/нет")[0], 404)
        self.assertEqual(dispatch(self.a, "POST", "/api/register", {"owner": "", "farm_name": "", "demo_key": "veg"})[0], 400)
        self.assertEqual(dispatch(self.a, "POST", "/api/import", {"files": "строка"})[0], 413)
        self.assertEqual(dispatch(self.a, "POST", "/api/import", {"files": files(), "profile": PROFILE, "dry_run": True})[0], 200)


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hub = Hub(lambda: Assistant(secret=b"h", llm=DemoLLM()), sessions=True)
        cls.server = make_server(hub, port=0, limit=500)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None, cookie=None, raw=None):
        h = {"Content-Type": "application/json", "X-Forwarded-For": "5.5.5.5"}
        if cookie:
            h["Cookie"] = cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method="POST" if data is not None else "GET", data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_upload_over_http_and_session_isolation(self):
        _, _, h1 = self.call("/api/profile")
        a = h1["Set-Cookie"].split(";")[0]
        _, _, h2 = self.call("/api/profile")
        b = h2["Set-Cookie"].split(";")[0]
        st, body, _ = self.call("/api/import", {"files": files(), "profile": PROFILE}, cookie=a)
        self.assertEqual((st, body["imported"]), (200, True))
        self.assertTrue(any(f["custom"] for f in self.call("/api/farms", cookie=a)[1]["farms"]))
        self.assertFalse(any(f["custom"] for f in self.call("/api/farms", cookie=b)[1]["farms"]))
        self.assertEqual(self.call("/api/farms/mine/today", cookie=b)[0], 404)

    def test_big_body_allowed_only_for_import(self):
        big = json.dumps({"files": {"sales": "x" * 100_000}}).encode()
        self.assertEqual(self.call("/api/farms/cheese/chat", raw=big)[0], 413)
        self.assertEqual(self.call("/api/import", raw=big)[0], 200)
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        conn.putrequest("POST", "/api/import")  # заявляем 13 МБ и не шлём тело: сервер должен отказать сразу
        conn.putheader("Content-Length", str(13 * 1024 * 1024))
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Forwarded-For", "6.6.6.6")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()


class Screen(unittest.TestCase):
    def test_wizard_is_in_the_page(self):
        with open("web/index.html", encoding="utf-8") as fh:
            html = fh.read()
        for text in ("Давайте знакомиться", "Загрузите свои данные", "Готовое предприятие", "Подставить пример", "Сбросить профиль"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
