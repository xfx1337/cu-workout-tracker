#!/usr/bin/env bash
# Сводка по стенду. Запуск: bash deploy/status.sh
cd "$(dirname "$0")/.." || exit 1

echo "=== КОНТЕЙНЕРЫ ==="
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' \
    $(docker compose ps -q) 2>/dev/null
echo "docker автозапуск: $(systemctl is-enabled docker 2>/dev/null)"

echo
echo "=== ВРЕМЯ В КОНТЕЙНЕРЕ ==="
docker compose exec -T bot date

echo
echo "=== БАЗА ==="
docker compose exec -T db psql -U workout -d workout -tAc \
  "select 'тренировок: '||count(*) from trainings
   union all select 'ближайших: '||count(*) from trainings where starts_at > now() and not is_cancelled
   union all select 'студентов: '||count(*) from students
   union all select 'записей: '||count(*) from bookings
   union all select 'админов: '||count(*) from admins" 2>/dev/null || echo "база недоступна"

echo
echo "=== ОШИБКИ ЗА 2 МИНУТЫ ==="
errors=$(docker compose logs bot --since 2m 2>/dev/null | grep -E 'ERROR|Traceback|Conflict' | tail -5)
if [ -n "$errors" ]; then echo "$errors"; else echo "чисто"; fi

echo
echo "=== ПОСЛЕДНИЕ СТРОКИ ЛОГА ==="
docker compose logs bot --tail 5
