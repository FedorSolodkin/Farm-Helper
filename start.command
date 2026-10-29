#!/bin/bash
# Запуск двойным щелчком по файлу или командой ./start.command
# Берёт настройки из secrets.env (живая модель), печатает, что включено, и открывает страницу в браузере.
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8765}"
export PORT
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Порт $PORT уже занят (возможно, помощник уже запущен). Откройте http://127.0.0.1:$PORT"
  echo "или запустите на другом порту:  PORT=8766 ./start.command"
  [ -z "$NO_OPEN" ] && open "http://127.0.0.1:$PORT"
  exit 0
fi
[ -z "$NO_OPEN" ] && (sleep 1.5; open "http://127.0.0.1:$PORT") &
exec python3 -u -m engine.server
