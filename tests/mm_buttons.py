"""Сквозная проверка кнопок-attachments на живом Mattermost.

Проверяет всю петлю целиком: бот рисует экран с кнопками -> студент нажимает
кнопку через API -> СЕРВЕР Mattermost сам делает POST на наш HTTP-эндпоинт ->
действие доходит до диспетчера.

Именно поэтому тест ценен: он ловит то, что не видно при отправке поста, —
доступность бота для сервера, формат props и защиту от чужих кликов.

Стенд:
    docker compose -f docker-compose.mattermost-dev.yml -p mmdev up -d
    python deploy/mm_dev_setup.py
    # в .env: MM_PUBLIC_URL=http://host.docker.internal:8080
    python -m tests.mm_buttons
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.actions_server import ActionRegistry, ActionServer
from app.config import settings
from app.mm.client import MattermostClient
from app.runtime import BotContext
from app.ui import Choice, group, screen

STUDENT = ("student1", "Student12345!")
PASSED = 0


def check(label: str, condition: bool) -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    PASSED += 1
    print(f"  ok  {label}")


async def login_as(username: str, password: str) -> tuple[httpx.AsyncClient, str]:
    http = httpx.AsyncClient(base_url=f"{settings.mm_url}/api/v4", timeout=20)
    resp = await http.post("/users/login", json={"login_id": username, "password": password})
    resp.raise_for_status()
    http.headers["Authorization"] = f"Bearer {resp.headers['Token']}"
    return http, resp.json()["id"]


async def main() -> int:
    if not settings.mm_url or not settings.mm_token:
        print("MM_URL/MM_TOKEN не заданы — пропускаю.")
        return 0
    if not settings.use_buttons:
        print("MM_PUBLIC_URL не задан — режим кнопок выключен, пропускаю.")
        return 0

    got: list[tuple[str, str, str]] = []
    caught = asyncio.Event()

    async def on_action(user_id: str, post_id: str, action: str) -> None:
        got.append((user_id, post_id, action))
        caught.set()

    registry = ActionRegistry()
    server = ActionServer(registry, on_action, settings.mm_listen_port)
    await server.start()

    mm = MattermostClient(settings.mm_url, settings.mm_token)
    ctx = BotContext(mm, actions=registry)
    await mm.whoami()

    student = await mm.get_user_by_username(STUDENT[0])
    http, student_id = await login_as(*STUDENT)

    try:
        scr = screen(
            "Выбери, по какому вопросу нужна помощь:",
            group(
                Choice("Документы, справки, заявления", "nav:schedule"),
                Choice("Кампус-отели", "nav:my"),
                Choice("Учеба и карьера", "nav:help"),
                Choice("Другой вопрос", "nav:admin", style="primary"),
            ),
            group(
                Choice("Мои запросы", "nav:my"),
                Choice("Выйти", "fsm:cancel", style="danger"),
            ),
        )
        post_id = await ctx.show(student.id, scr)
        check("экран с кнопками отправлен", bool(post_id))

        raw = await mm._request("GET", f"/posts/{post_id}")
        attachments = raw["props"]["attachments"]
        check("две группы кнопок — как на макете", len(attachments) == 2)
        check("в первой группе четыре кнопки", len(attachments[0]["actions"]) == 4)
        check("подписи текстовые, а не эмодзи",
              attachments[0]["actions"][0]["name"] == "Документы, справки, заявления")
        check("акцентная кнопка помечена стилем",
              attachments[0]["actions"][3].get("style") == "primary")
        check("опасная кнопка помечена стилем",
              attachments[1]["actions"][1].get("style") == "danger")
        check("текст экрана без нумерованной легенды", "1️⃣" not in raw["message"])

        # --- сам клик: дальше работает сервер Mattermost ---
        action_id = attachments[0]["actions"][1]["id"]
        resp = await http.post(f"/posts/{post_id}/actions/{action_id}")
        if resp.status_code >= 400:
            print(f"      ответ Mattermost: {resp.status_code} {resp.text[:300]}")
        check("студент нажал кнопку", resp.status_code < 400)

        await asyncio.wait_for(caught.wait(), timeout=20)
        user_id, clicked_post, action = got[0]
        check("Mattermost достучался до бота и клик дошёл", action == "nav:my")
        check("известно, кто нажал", user_id == student_id)
        check("известно, на каком экране", clicked_post == post_id)

        # --- экран обновляется на месте, а не плодит сообщения ---
        second = await ctx.show(student.id, screen("Второй экран", group(Choice("Назад", "nav:schedule"))))
        check("следующий экран занял то же сообщение", second == post_id)
        raw2 = await mm._request("GET", f"/posts/{post_id}")
        check("текст сообщения обновился", raw2["message"] == "Второй экран")
        check("кнопки заменились", len(raw2["props"]["attachments"][0]["actions"]) == 1)

        # --- защита: старый токен больше не действует ---
        got.clear()
        caught.clear()
        resp = await http.post(f"/posts/{post_id}/actions/{action_id}")
        await asyncio.sleep(2)
        check("клик по кнопке устаревшего экрана игнорируется", not got)
    finally:
        await http.aclose()
        await mm.close()
        await server.stop()

    print(f"\nВсе проверки пройдены: {PASSED}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
