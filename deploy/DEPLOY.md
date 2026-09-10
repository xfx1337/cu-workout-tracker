# Развёртывание: GitLab CI + Docker

Ветка **`mattermost`** — версия для TiMe. Ветка `main` — старая телеграмная,
её не разворачиваем.

Схема простая: GitLab гоняет тесты, собирает образ в свой registry и по кнопке
заходит на сервер, где `docker compose` подтягивает готовый образ. Код на сервер
не копируется, git там не нужен.

```
push в mattermost
      │
      ├─ тесты (домен, диспетчер, Postgres)
      ├─ сборка образа  →  registry.gitlab/<группа>/<проект>/bot:<sha>
      └─ выкладка (вручную одной кнопкой)
                │
                └─ ssh → docker compose pull && up -d
```

---

## 1. Что нужно получить у админов TiMe

1. **Бот-аккаунт и его personal access token.**
2. **Адрес TiMe**, например `https://time.centraluniversity.ru`.
3. **Разрешение на исходящие вызовы к боту.** При нажатии кнопки сервер TiMe сам
   делает POST на нашу ручку. Если бот стоит во внутренней сети на приватном
   адресе, Mattermost откажется его звать и напишет в лог:

   ```
   IP 10.x.x.x is in a reserved range and not in AllowedUntrustedInternalConnections
   ```

   Лечится в System Console → Environment → Developer, параметр
   `AllowedUntrustedInternalConnections` — туда вносят адрес бота.
   **Если бот доступен по публичному домену с HTTPS, эта настройка не нужна** —
   поэтому публичная ручка проще в согласовании.

---

## 2. Публичная ручка для кнопок

Наружу выставляется **только** приём кликов, ничего больше:

| путь | что это |
|---|---|
| `POST /mm/action/<токен>` | клик по кнопке, зовёт сервер Mattermost |
| `GET /health` | проверка живости для healthcheck и мониторинга |
| всё остальное | 404 |

Сам бот слушает только `127.0.0.1` — наружу его выводит обратный прокси с HTTPS.
За этим адресом нет ни админки, ни API: единственное, что можно сделать снаружи, —
прислать клик, а он проверяется тремя способами (см. раздел 6).

### Если своего прокси на сервере нет

Подними Caddy из комплекта — он сам получит сертификат Let's Encrypt:

```bash
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d
```

В `.env` нужен `BOT_DOMAIN=workout.example.ru`, а домен должен A-записью
смотреть на этот сервер.

### Если nginx/Caddy уже есть

Не подключай `docker-compose.caddy.yml`. Бот уже слушает `127.0.0.1:8080` —
направь туда свой прокси:

```nginx
location /mm/action/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
}
```

---

## 3. Подготовка сервера

Один раз, руками:

```bash
mkdir -p /opt/cu-workout-tracker && cd /opt/cu-workout-tracker
```

Положи туда `docker-compose.yml`, `docker-compose.caddy.yml`, `deploy/Caddyfile`
и `.env`. Всё остальное приезжает внутри образа.

`.env` (за образец — [.env.example](../.env.example)):

```dotenv
MM_URL=https://time.centraluniversity.ru
MM_TOKEN=токен_бот_аккаунта
ADMIN_EMAILS=olya@centraluniversity.ru,kir@centraluniversity.ru

# Адрес публичной ручки. Задан — кнопки; пусто — реакции-эмодзи.
MM_PUBLIC_URL=https://workout.example.ru
MM_LISTEN_PORT=8080
BOT_DOMAIN=workout.example.ru

POSTGRES_PASSWORD=длинный_случайный_пароль
TZ_NAME=Europe/Moscow

# Подставляется автоматически при выкладке, вручную трогать не нужно
BOT_IMAGE=registry.gitlab.example/группа/проект/bot:latest
```

Права на файл — только владельцу, внутри токен:

```bash
chmod 600 .env
```

Первый запуск:

```bash
docker login registry.gitlab.example
docker compose up -d
docker compose exec bot python seed_trainings.py --apply   # если нужно расписание
```

---

## 4. Настройка GitLab

**Settings → CI/CD → Variables:**

| переменная | тип | значение |
|---|---|---|
| `SSH_PRIVATE_KEY` | File, protected | приватный ключ для входа на сервер |
| `SSH_KNOWN_HOSTS` | File, protected | вывод `ssh-keyscan <сервер>` |
| `DEPLOY_HOST` | Variable | `deploy@10.0.0.5` |
| `DEPLOY_PATH` | Variable | `/opt/cu-workout-tracker` |

**Секреты бота в переменные CI не кладём.** `MM_TOKEN` и пароль базы живут
в `.env` на сервере: у CI нет причин их знать, а чем меньше мест с секретом,
тем лучше. Registry-логин GitLab подставляет сам через `CI_REGISTRY_*`.

Пользователь `deploy` на сервере должен состоять в группе `docker`.

---

## 5. Как выкладывать

Push в `mattermost` запускает тесты и сборку. Выкладка — **вручную**: в пайплайне
у задачи `выкладка` нажимается кнопка. Так релиз не случается сам по себе от
любого коммита.

Развёрнут всегда конкретный тег коммита, а не `latest`, — видно, что именно
работает, и можно откатиться:

```bash
cd /opt/cu-workout-tracker
sed -i 's|^BOT_IMAGE=.*|BOT_IMAGE=registry.../bot:<предыдущий-sha>|' .env
docker compose up -d bot
```

Проверка после выкладки:

```bash
docker compose ps
docker compose logs -f bot
curl -fsS https://workout.example.ru/health && echo   # должно ответить ok
```

---

## 6. Чем защищена ручка

Адрес публичный, поэтому клик проверяется трижды:

1. **Одноразовый токен в URL.** У каждого экрана свой. Mattermost вырезает блок
   `integration` из данных поста, поэтому в интерфейсе токен не виден — подсмотреть
   чужой нельзя.
2. **Привязка к пользователю.** Клик засчитывается, только если пришедший `user_id`
   совпадает с тем, кому этот экран отправляли.
3. **Белый список действий.** Действие должно быть с этого экрана: произвольную
   строку в `context` не подсунуть.

Токен предыдущего экрана отзывается при показе следующего, так что кнопки старых
сообщений не работают.

---

## 7. Резервные копии

```bash
docker compose exec -T db pg_dump -U workout workout > backup-$(date +%F).sql
```

Восстановление:

```bash
docker compose exec -T db psql -U workout -d workout < backup.sql
```

Данные лежат в volume `pgdata` и переживают пересборку образа. Схема создаётся
кодом при старте, миграций нет — новую колонку в существующей таблице придётся
добавлять руками через `ALTER TABLE`.

---

## 8. Если что-то не работает

**Кнопки не нажимаются, в логе бота тишина.** Клик до нас не доходит: сервер
Mattermost не может достучаться. Проверь снаружи `curl https://.../health`
и `AllowedUntrustedInternalConnections` (раздел 1).

**В логе Mattermost `invalid action type`.** Устаревший формат кнопок; у нас
проставлен `"type": "button"`, но при правках `app/ui.py` это легко потерять.

**Бот не видит сообщений.** На TiMe WebSocket недоступен — ingress режет upgrade,
события забираются опросом REST (`POLL_INTERVAL_SECONDS`). Проверь, что токен
живой: `curl -H "Authorization: Bearer $MM_TOKEN" $MM_URL/api/v4/users/me`.

**Экраны выглядят как список с эмодзи вместо кнопок.** Не задан `MM_PUBLIC_URL` —
бот ушёл в запасной режим. Это ожидаемое поведение, а не поломка.
