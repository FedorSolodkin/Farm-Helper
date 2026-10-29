"""Автономная сборка для GitHub Pages: движок работает в браузере, без сервера и секретов."""
import json
import pathlib
import re
import subprocess
import sys
import unittest
import zipfile
from unittest import mock

from engine import narrator as narrator_module
from engine.api import dispatch
from engine.generate import build_farm
from engine.llm import DemoLLM
from engine.narrator import Narrator
from engine.service import Assistant
from engine.signals import detect_all
from engine.benchmarks import BENCHMARKS
from engine.privacy import usable_benchmarks

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.a = Assistant(secret=b"s", llm=DemoLLM())

    def test_routes_work_without_http(self):
        st, body = dispatch(self.a, "GET", "/api/farms")
        self.assertEqual((st, len(body["farms"])), (200, 3))
        st, today = dispatch(self.a, "GET", "/api/farms/cheese/today")
        key = today["actions"][0]["key"]
        st, pv = dispatch(self.a, "POST", "/api/farms/cheese/preview", {"key": key})
        st, cf = dispatch(self.a, "POST", "/api/farms/cheese/confirm", {"key": key, "change_hash": pv["change_hash"]})
        self.assertEqual(dispatch(self.a, "POST", "/api/farms/cheese/execute", {"key": key, "token": cf["token"]})[0], 200)
        self.assertEqual(dispatch(self.a, "POST", "/api/farms/cheese/rewind")[0], 200)
        self.assertEqual(dispatch(self.a, "POST", "/api/farms/cheese/chat", {"text": "почему творог?"})[1]["intent"], "why")

    def test_errors_become_status_and_message(self):
        self.assertEqual(dispatch(self.a, "GET", "/api/farms/nope/today")[0], 404)
        st, body = dispatch(self.a, "POST", "/api/farms/cheese/execute", {"key": "spoilage:tvorog", "token": "x"})
        self.assertEqual(st, 403)
        self.assertIn("message", body["error"])
        self.assertEqual(dispatch(self.a, "GET", "/api/unknown")[0], 404)

    def test_query_string_is_ignored(self):
        self.assertEqual(dispatch(self.a, "GET", "/api/farms?x=1")[0], 200)


class Browser(unittest.TestCase):
    def test_no_threads_in_the_browser(self):
        farm = build_farm("cheese")
        actions = detect_all(farm, usable_benchmarks(BENCHMARKS))[:3]
        with mock.patch.object(narrator_module.sys, "platform", "emscripten"), \
                mock.patch.object(narrator_module, "ThreadPoolExecutor", side_effect=AssertionError("потоки в браузере недоступны")):
            result = Narrator(DemoLLM()).narrate_many(farm, actions)
        self.assertEqual(len(result), 3)


class PagesBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_pages.py")], check=True, capture_output=True)
        cls.out = ROOT / "deploy_pages"

    def test_flat_without_server_parts_and_secrets(self):
        names = {p.name for p in self.out.iterdir()}
        self.assertTrue({"index.html", "boot.js", "webapi.py", "api.py", "service.py", ".nojekyll", "README.txt"} <= names)
        self.assertFalse({"server.py", "hub.py", "secrets.env", "telegram_bot.py"} & names)
        self.assertTrue(all(p.is_file() for p in self.out.iterdir()))
        with zipfile.ZipFile(ROOT / "deploy_pages.zip") as zf:
            self.assertTrue(all("/" not in n for n in zf.namelist()))
        blob = "".join(p.read_text(encoding="utf-8") for p in self.out.iterdir() if p.suffix in (".py", ".js", ".html", ".txt"))
        for pattern in (r"AQVN[A-Za-z0-9_\-]{20,}", r"b1g[a-z0-9]{17}", r"\d{8,10}:[A-Za-z0-9_\-]{30,}"):  # ключ Yandex, ID каталога, токен бота
            self.assertIsNone(re.search(pattern, blob), pattern)

    def test_page_loads_the_engine_and_lists_every_module(self):
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="boot.js"'), html.index("const $ ="))
        boot = (self.out / "boot.js").read_text(encoding="utf-8")
        modules = json.loads(boot.split("var MODULES = ")[1].split(";")[0])
        self.assertEqual(sorted(modules), sorted(p.name for p in self.out.glob("*.py")))

    def test_modules_import_and_answer_from_the_flat_folder(self):
        code = ("import json, webapi;"
                "r = json.loads(webapi.handle('GET', '/api/farms/cheese/today'));"
                "print(r['status'], len(r['payload']['actions']))")
        run = subprocess.run([sys.executable, "-c", code], cwd=self.out, capture_output=True, text=True)
        self.assertEqual(run.stdout.strip(), "200 3", run.stderr)


if __name__ == "__main__":
    unittest.main()
