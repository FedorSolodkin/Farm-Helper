"""python3 tools/build_pages.py — автономная сборка для GitHub Pages: deploy_pages/ и deploy_pages.zip.

Pages отдаёт только файлы, а серверу нужен Python. Поэтому тот же движок запускается в браузере (Pyodide).
Без секретов: ИИ работает в демо-режиме, состояние живёт в странице."""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy_pages"
SKIP = {"__init__.py", "server.py", "hub.py", "run.py", "accounts.py"}  # серверные части в браузере не нужны

README = """GITHUB PAGES
============

1. Создайте на GitHub репозиторий (Public) и загрузите ВСЕ файлы из архива в его корень
   (кнопка Add file -> Upload files, перетащите файлы, Commit changes).
2. Settings -> Pages -> Build and deployment -> Source: Deploy from a branch -> Branch: main, папка / (root) -> Save.
3. Через 1-2 минуты сайт откроется по адресу https://ИМЯ.github.io/РЕПОЗИТОРИЙ/

Как это работает: Pages отдаёт только файлы, поэтому движок на Python запускается прямо в браузере (Pyodide).
Ничего не отправляется на сервер, ключей в файлах нет, ИИ работает в демо-режиме.
Первый запуск занимает 5-15 секунд (загрузка около 10 МБ с cdn.jsdelivr.net), потом быстрее.
Состояние сбрасывается при обновлении страницы.
"""


def flatten(src: str) -> str:
    src = re.sub(r"^(\s*)from \.(\w+) import", r"\1from \2 import", src, flags=re.M)
    return re.sub(r"^(\s*)from \. import (\w+)", r"\1import \2", src, flags=re.M)


def main() -> Path:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    modules = []
    for path in sorted((ROOT / "engine").glob("*.py")):
        if path.name in SKIP:
            continue
        (OUT / path.name).write_text(flatten(path.read_text(encoding="utf-8")), encoding="utf-8")
        modules.append(path.name)
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    marker = "<script>\nconst $ ="
    assert marker in html
    (OUT / "index.html").write_text(html.replace(marker, '<script src="boot.js"></script>\n' + marker, 1), encoding="utf-8")
    page = (ROOT / "web" / "delivery.html").read_text(encoding="utf-8")
    marker2 = "<script>\nconst $ ="
    assert marker2 in page
    (OUT / "delivery.html").write_text(page.replace(marker2, '<script src="boot.js"></script>\n' + marker2, 1), encoding="utf-8")
    boot = (ROOT / "tools" / "pages_boot.js").read_text(encoding="utf-8")
    (OUT / "boot.js").write_text(boot.replace("__MODULES__", repr(modules).replace("'", '"')), encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    (OUT / "README.txt").write_text(README, encoding="utf-8")
    archive = ROOT / "deploy_pages.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(OUT.iterdir()):
            zf.write(f, f.name)
    print(f"Папка: {OUT}\nАрхив: {archive}\nФайлов: {len(list(OUT.iterdir()))}")
    return archive


if __name__ == "__main__":
    main()
