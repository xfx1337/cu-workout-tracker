"""Интеграционный тест транспорта Mattermost на живом сервере.

В отличие от tests/smoke.py, который гоняет домен без сети, этот тест проверяет
ровно то, на чём держится интерфейс: личку, картинку, правку сообщения на месте
и реакции как «кнопки» — включая то, что клик долетает до бота по WebSocket.

Поднять стенд и прогнать:
    docker compose -f docker-compose.mattermost-dev.yml -p mmdev up -d
    python deploy/mm_dev_setup.py     # создаст бота, команду и студентов
    python -m tests.mm_live

Тестовые студенты берутся из deploy/mm_dev_setup.py.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import settings
from app.mm.client import MattermostClient

STUDENT = ("student1", "Student12345!")

PASSED = 0


def check(label: str, condition: bool) -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    PASSED += 1
    print(f"  ok  {label}")


async def login_as(username: str, password: str) -> tuple[httpx.AsyncClient, str]:
    """Отдельная сессия от имени человека — чтобы имитировать его клик."""
    http = httpx.AsyncClient(base_url=f"{settings.mm_url}/api/v4", timeout=20)
    resp = await http.post("/users/login", json={"login_id": username, "password": password})
    resp.raise_for_status()
    http.headers["Authorization"] = f"Bearer {resp.headers['Token']}"
    return http, resp.json()["id"]


async def main() -> int:
    if not settings.mm_url or not settings.mm_token:
        print("MM_URL и MM_TOKEN не заданы — стенд не поднят, пропускаю.")
        return 0

    mm = MattermostClient(settings.mm_url, settings.mm_token)

    me = await mm.whoami()
    check(f"бот представился: @{me.username}", bool(me.id))
    check("аккаунт помечен как бот", me.is_bot)

    student = await mm.get_user_by_username(STUDENT[0])
    check("студент найден по username", student is not None)
    check("почта студента видна — это его личность из мессенджера", bool(student.email))

    channel = await mm.direct_channel(student.id)
    check("личный канал создан", bool(channel))
    check("второй вызов берёт канал из кэша", await mm.direct_channel(student.id) == channel)

    post = await mm.create_post(channel, "Проверка связи")
    check("сообщение отправлено", bool(post.id))

    edited = await mm.update_post(post.id, "**Расписание**\nВыбери день")
    check("сообщение правится на месте", edited.id == post.id and "Расписание" in edited.message)

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64      # содержимое не важно, важен факт загрузки
    file_id = await mm.upload_file(channel, "schedule.png", png)
    check("файл загружается", bool(file_id))
    with_file = await mm.create_post(channel, "Расписание:", file_ids=[file_id])
    check("сообщение с вложением отправлено", bool(with_file.file_ids))

    # --- главное: реакции как кнопки ---
    events: list = []
    caught = asyncio.Event()

    async def listen() -> None:
        async for event in mm.events():
            if event.post_id == with_file.id and event.kind.startswith("reaction"):
                events.append(event)
                caught.set()

    task = asyncio.create_task(listen())
    # ждём готовности websocket, а не «спим и надеемся»
    await asyncio.wait_for(mm.connected.wait(), timeout=15)
    check("websocket подключён и аутентифицирован", mm.connected.is_set())

    await mm.set_reactions(with_file.id, ["one", "two", "three"])
    check("«клавиатура» из реакций расставлена", True)

    http, student_id = await login_as(*STUDENT)
    try:
        resp = await http.post(
            "/reactions",
            json={"user_id": student_id, "post_id": with_file.id, "emoji_name": "two"},
        )
        check("студент нажал реакцию", resp.status_code < 400)

        await asyncio.wait_for(caught.wait(), timeout=15)
        event = events[0]
        check("клик долетел до бота по websocket", event.kind == "reaction_added")
        check("эмодзи распознан", event.emoji == "two")
        check("известно, кто нажал", event.user_id == student_id)
        check("известно, на каком сообщении", event.post_id == with_file.id)

        # эхо: свои реакции бот обязан игнорировать, иначе зациклится
        events.clear()
        caught.clear()
        await mm.add_reaction(with_file.id, "four")
        await asyncio.sleep(3)
        check("собственные реакции бота игнорируются", not events)
    finally:
        task.cancel()
        await http.aclose()
        await mm.close()

    print(f"\nВсе проверки пройдены: {PASSED}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
