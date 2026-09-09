"""Готовит локальный Mattermost к разработке бота.

Создаёт админа, команду, бот-аккаунт с токеном и пару тестовых студентов,
после чего печатает готовые строки для .env.

    docker compose -f docker-compose.mattermost-dev.yml -p mmdev up -d
    python deploy/mm_dev_setup.py

Запускать повторно безопасно: всё уже существующее переиспользуется.
К проду отношения не имеет — только локальный стенд на localhost:8065.
"""
from __future__ import annotations

import sys

import httpx

# Пароли ниже — фикстуры одноразового контейнера, который слушает только
# 127.0.0.1 и сносится вместе с томом. Это не секреты: боевые MM_URL и MM_TOKEN
# живут в .env, который в репозиторий не попадает.
URL = "http://localhost:8065"
ADMIN = {"email": "admin@cu.local", "username": "admin", "password": "Admin12345!"}
TEAM = {"name": "cu", "display_name": "ЦУ", "type": "O"}
BOT = {"username": "workout", "display_name": "Запись на тренировки"}
STUDENTS = [
    {"email": "student1@cu.local", "username": "student1", "password": "Student12345!",
     "first_name": "Иван", "last_name": "Иванов"},
    {"email": "student2@cu.local", "username": "student2", "password": "Student12345!",
     "first_name": "Мария", "last_name": "Петрова"},
]


def main() -> int:
    http = httpx.Client(base_url=f"{URL}/api/v4", timeout=30)

    try:
        http.get("/system/ping").raise_for_status()
    except Exception as exc:
        print(f"Mattermost на {URL} не отвечает: {exc}")
        print("Подними его: docker compose -f docker-compose.mattermost-dev.yml -p mmdev up -d")
        return 1

    # --- админ ---
    resp = http.post("/users", json=ADMIN)
    if resp.status_code < 400:
        print(f"создан админ {ADMIN['username']}")
    else:
        print(f"админ уже есть ({resp.status_code}), логинюсь")

    resp = http.post("/users/login", json={"login_id": ADMIN["username"], "password": ADMIN["password"]})
    if resp.status_code >= 400:
        print(f"не смог залогиниться админом: {resp.status_code} {resp.text[:200]}")
        return 1
    http.headers["Authorization"] = f"Bearer {resp.headers['Token']}"
    admin_id = resp.json()["id"]

    # --- разрешаем ботов и персональные токены ---
    config = http.get("/config").json()
    config["ServiceSettings"]["EnableBotAccountCreation"] = True
    config["ServiceSettings"]["EnableUserAccessTokens"] = True
    config["ServiceSettings"]["EnablePostUsernameOverride"] = True
    config["ServiceSettings"]["EnablePostIconOverride"] = True
    saved = http.put("/config", json=config)
    print("боты и персональные токены разрешены" if saved.status_code < 400
          else f"не смог поправить конфиг: {saved.status_code}")

    # --- команда ---
    resp = http.post("/teams", json=TEAM)
    if resp.status_code < 400:
        team = resp.json()
        print(f"создана команда {TEAM['name']}")
    else:
        team = http.get(f"/teams/name/{TEAM['name']}").json()
        print(f"команда {TEAM['name']} уже есть")
    team_id = team["id"]
    http.post(f"/teams/{team_id}/members", json={"team_id": team_id, "user_id": admin_id})

    # --- бот ---
    resp = http.post("/bots", json=BOT)
    if resp.status_code < 400:
        bot_user_id = resp.json()["user_id"]
        print(f"создан бот @{BOT['username']}")
    else:
        found = http.get(f"/users/username/{BOT['username']}")
        if found.status_code >= 400:
            print(f"не смог ни создать, ни найти бота: {resp.status_code} {resp.text[:200]}")
            return 1
        bot_user_id = found.json()["id"]
        print(f"бот @{BOT['username']} уже есть")

    http.post(f"/teams/{team_id}/members", json={"team_id": team_id, "user_id": bot_user_id})

    resp = http.post(f"/users/{bot_user_id}/tokens", json={"description": "workout-bot dev"})
    if resp.status_code >= 400:
        print(f"не смог выпустить токен: {resp.status_code} {resp.text[:200]}")
        return 1
    token = resp.json()["token"]

    # --- тестовые студенты ---
    student_ids = []
    for student in STUDENTS:
        resp = http.post("/users", json=student)
        if resp.status_code < 400:
            uid = resp.json()["id"]
            print(f"создан студент @{student['username']}")
        else:
            uid = http.get(f"/users/username/{student['username']}").json()["id"]
            print(f"студент @{student['username']} уже есть")
        student_ids.append(uid)
        http.post(f"/teams/{team_id}/members", json={"team_id": team_id, "user_id": uid})

    print()
    print("=" * 64)
    print("Впиши в .env:")
    print()
    print(f"MM_URL={URL}")
    print(f"MM_TOKEN={token}")
    print(f"MM_TEAM={TEAM['name']}")
    print(f"ADMIN_EMAILS={ADMIN['email']}")
    print()
    print("Веб-интерфейс: " + URL)
    print(f"  админ:   {ADMIN['username']} / {ADMIN['password']}")
    for student in STUDENTS:
        print(f"  студент: {student['username']} / {student['password']}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
