"""python3 tools/build_flat.py — собирает «плоскую» папку deploy_flat/ и архив deploy_flat.zip:
все файлы лежат в одной папке, без вложенных (для загрузки на хостинг одним махом).
Секреты в архив не попадают: ключи задаются на сервере в secrets.env или в панели хостинга."""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy_flat"

README = """ЗАПУСК НА ХОСТИНГЕ
==================

Что внутри: server.py (запуск), index.html (экран), остальные .py — логика. Вложенных папок нет.

Нужен сервер, где можно держать запущенным Python 3.9+ (VPS, облачный сервер, контейнер).
Обычный виртуальный хостинг с PHP так не умеет: там Python-сервер работать не будет.

1. Загрузите ВСЕ файлы из архива в одну папку на сервере.
2. Скопируйте secrets.env.example в secrets.env и впишите значения (ключ ИИ, токен бота, ID каталога).
   Файл secrets.env никому не отправляйте и не выкладывайте в интернет.
   Либо задайте те же переменные в панели хостинга (раздел «Переменные окружения»).
3. Запустите:  python3 server.py
   Порт берётся из переменной PORT (по умолчанию 8765), адрес из HOST.
4. Проверка: откройте http://АДРЕС:ПОРТ/healthz — должно быть {"ok": true}.

Постоянный запуск на VPS (systemd), файл /etc/systemd/system/farmer.service:

    [Unit]
    Description=Farmer growth assistant
    After=network.target

    [Service]
    WorkingDirectory=/путь/к/папке
    ExecStart=/usr/bin/python3 server.py
    Restart=always

    [Install]
    WantedBy=multi-user.target

    sudo systemctl enable --now farmer

Docker: docker build -t farmer . && docker run -p 8765:8765 --env-file secrets.env farmer
"""

DOCKERFILE = """FROM python:3.11-slim
WORKDIR /app
COPY . .
ENV HOST=0.0.0.0 SESSIONS=1 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "server.py"]
"""


def flatten(src: str) -> str:
    src = re.sub(r"^(\s*)from \.(\w+) import", r"\1from \2 import", src, flags=re.M)
    return re.sub(r"^(\s*)from \. import (\w+)", r"\1import \2", src, flags=re.M)


def main() -> Path:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    for path in sorted((ROOT / "engine").glob("*.py")):
        if path.name == "__init__.py":
            continue
        (OUT / path.name).write_text(flatten(path.read_text(encoding="utf-8")), encoding="utf-8")
    shutil.copy(ROOT / "web" / "index.html", OUT / "index.html")
    shutil.copy(ROOT / "web" / "delivery.html", OUT / "delivery.html")
    shutil.copy(ROOT / "secrets.env.example", OUT / "secrets.env.example")
    (OUT / "README.txt").write_text(README, encoding="utf-8")
    (OUT / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (OUT / "requirements.txt").write_text("# зависимостей нет\n", encoding="utf-8")
    archive = ROOT / "deploy_flat.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(OUT.iterdir()):
            zf.write(f, f.name)  # без путей: все файлы в корне архива
    print(f"Папка: {OUT}\nАрхив: {archive}\nФайлов: {len(list(OUT.iterdir()))}")
    return archive


if __name__ == "__main__":
    main()
