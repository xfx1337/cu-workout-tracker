#!/usr/bin/env bash
# Подготовка чистого сервера под бота. Запускать на сервере от root:
#   bash /opt/cu-workout-tracker/deploy/bootstrap.sh
#
# Ставит Docker, включает его автозапуск и поднимает бота.
# Повторный запуск безопасен: всё уже установленное пропускается.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cu-workout-tracker}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Запускай от root (или через sudo)." >&2
    exit 1
fi

say "Проверяю Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "Docker уже стоит: $(docker --version)"
else
    echo "Ставлю Docker с get.docker.com…"
    curl -fsSL https://get.docker.com | sh
fi

say "Включаю автозапуск Docker"
systemctl enable --now docker

say "Проверяю проект в $APP_DIR"
cd "$APP_DIR"
if [ ! -f .env ]; then
    echo "Нет файла .env — положи его рядом с docker-compose.yml и запусти снова." >&2
    echo "Шаблон: .env.example" >&2
    exit 1
fi

# Пароль базы по умолчанию в прод пускать нельзя
if grep -qE '^POSTGRES_PASSWORD=workout$' .env; then
    echo "В .env стоит дефолтный пароль Postgres. Поменяй POSTGRES_PASSWORD и запусти снова." >&2
    exit 1
fi
if ! grep -qE '^BOT_TOKEN=.+' .env; then
    echo "В .env не задан BOT_TOKEN." >&2
    exit 1
fi

say "Собираю и запускаю"
docker compose up -d --build

say "Жду, пока база станет healthy"
for _ in $(seq 1 40); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q db)" 2>/dev/null)" = "healthy" ]; then
        break
    fi
    sleep 3
done

say "Статус"
docker compose ps

say "Последние строки лога бота"
docker compose logs bot --tail 20

cat <<'EOF'

Готово. Дальше:
  docker compose logs -f bot     # смотреть лог
  docker compose restart bot     # перезапуск после правки .env
  docker compose down            # остановить (данные останутся)

Расписание заливается так:
  docker compose exec bot python seed_trainings.py --apply
или из админки бота кнопкой «📥 Загрузить расписание».
EOF
