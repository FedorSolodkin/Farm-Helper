"""Раздача файлов, secrets.env и плоская сборка для хостинга."""
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile

from engine.server import load_env_file, make_server
from engine.service import Assistant

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Static(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(Assistant(llm=None), port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def status(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_only_the_page_is_public(self):
        self.assertEqual(self.status("/"), 200)
        self.assertEqual(self.status("/index.html"), 200)
        for path in ("/data.js", "/secrets.env", "/../engine/server.py", "/engine/service.py", "/.env", "/PLAN.md", "/index.html/../x"):
            self.assertEqual(self.status(path), 404, path)


class EnvFile(unittest.TestCase):
    def test_reads_values_without_overriding_and_ignores_junk(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "secrets.env"
        path.write_text("# комментарий\nA=1\nB='два слова'\nC=\n  D = 4 \nбез знака равно\nKEEP=new\n", encoding="utf-8")
        env = {"KEEP": "old"}
        self.assertEqual(load_env_file([path], env), ["A", "B", "C", "D"])
        self.assertEqual(env, {"KEEP": "old", "A": "1", "B": "два слова", "C": "", "D": "4"})

    def test_missing_file_is_fine_and_env_file_variable_wins(self):
        self.assertEqual(load_env_file([pathlib.Path("/nonexistent/secrets.env")], {}), [])
        path = pathlib.Path(tempfile.mkdtemp()) / "custom.env"
        path.write_text("X=1\n", encoding="utf-8")
        env = {"ENV_FILE": str(path)}
        self.assertEqual(load_env_file([], env), ["X"])


class FlatBundle(unittest.TestCase):
    def test_bundle_is_flat_without_secrets_and_imports_cleanly(self):
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_flat.py")], check=True, capture_output=True)
        out = ROOT / "deploy_flat"
        names = {p.name for p in out.iterdir()}
        self.assertTrue({"server.py", "index.html", "service.py", "secrets.env.example", "README.txt", "Dockerfile"} <= names)
        self.assertTrue(all(p.is_file() for p in out.iterdir()))  # внутренних папок нет
        self.assertNotIn("secrets.env", names)
        with zipfile.ZipFile(ROOT / "deploy_flat.zip") as zf:
            self.assertTrue(all("/" not in n for n in zf.namelist()))
        text = "".join(p.read_text(encoding="utf-8") for p in out.glob("*.py"))
        self.assertNotIn("from .", text)
        check = subprocess.run([sys.executable, "-c", "import server, service, chat, simulate; print('ok')"],
                               cwd=out, capture_output=True, text=True)
        self.assertEqual(check.stdout.strip(), "ok", check.stderr)


if __name__ == "__main__":
    unittest.main()
