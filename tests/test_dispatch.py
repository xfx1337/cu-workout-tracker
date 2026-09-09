"""Тест слоя обработчиков поверх фейкового Mattermost-клиента.

Смоук (tests/smoke.py) гоняет только домен/сервисы. Этот тест прогоняет то,
что на них держится: BotContext + диспетчер реакций + обработчики student/admin.
Сеть не нужна — клиент Mattermost подменяется записью вызовов.

Запуск: python -m tests.test_dispatch
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import timedelta

DB_PATH = os.path.join(tempfile.mkdtemp(prefix="cu-dispatch-"), "smoke.db")
os.environ["DATABASE_URL"] = os.getenv("SMOKE_DATABASE_URL") or f"sqlite+aiosqlite:///{DB_PATH}"
os.environ["ADMIN_EMAILS"] = "boss@cu.local"
os.environ["NO_SHOW_LIMIT"] = "3"

from app.db import SessionMaker, init_db  # noqa: E402
from app.handlers import admin as admin_h  # noqa: E402
from app.handlers import common as common_h  # noqa: E402
from app.handlers import dispatch_action  # noqa: E402
from app.mm.client import MMUser  # noqa: E402
from app.runtime import BotContext  # noqa: E402
from app.services import bookings as bookings_svc  # noqa: E402
from app.services import students as students_svc  # noqa: E402
from app.services import trainings as trainings_svc  # noqa: E402
from app.tz import now_utc, to_local  # noqa: E402

PASSED = 0


def check(label: str, condition: bool) -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {label}")
    PASSED += 1
    print(f"  ok  {label}")


class FakeMM:
    """Клиент Mattermost-заглушка: ничего не шлёт, только записывает."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.reactions: dict[str, set[str]] = {}
        self.channels: list[dict] = []
        self.posts_by_channel: dict[str, list] = {}
        self.reactions_by_post: dict[str, list] = {}
        self.users: dict[str, MMUser] = {
            "student1": MMUser(id="student1", username="user1", email="user1@cu.local",
                               first_name="Иван", last_name="Иванов"),
            "admin1": MMUser(id="admin1", username="boss", email="Boss@CU.local",
                             first_name="Босс", last_name=""),
        }
        self.me = MMUser(id="bot1", username="workout", email="", is_bot=True)

    async def get_user(self, user_id: str) -> MMUser:
        return self.users[user_id]

    async def whoami(self) -> MMUser:
        return self.me

    async def direct_channel(self, user_id: str) -> str:
        return f"dm-{user_id}"

    async def create_post(self, channel_id: str, message: str, file_ids=None, **kw) -> object:
        from app.mm.client import MMPost

        post = MMPost(id=f"p{len(self.posts)}", channel_id=channel_id, user_id=self.me.id,
                      message=message, file_ids=file_ids or [])
        self.posts.append({"id": post.id, "message": message, "files": post.file_ids})
        return post

    async def upload_file(self, channel_id: str, filename: str, data: bytes) -> str:
        return f"f-{filename}"

    async def add_reaction(self, post_id: str, emoji: str) -> None:
        self.reactions.setdefault(post_id, set()).add(emoji)

    async def download_file(self, file_id: str) -> bytes:
        return b"xlsx-data"

    # --- поллинг ---
    async def list_my_channels(self) -> list[dict]:
        return self.channels

    async def channel_posts_since(self, channel_id: str, since_ms: int) -> list:
        return [p for p in self.posts_by_channel.get(channel_id, []) if p.create_at > since_ms]

    async def post_reactions(self, post_id: str) -> list[dict]:
        return self.reactions_by_post.get(post_id, [])


async def make_student(session, user: MMUser):
    student = await students_svc.get_or_create(
        session, user.id, username=user.username, email=user.email, full_name=user.display_name
    )
    await students_svc.accept_consent(session, student)
    return student


def action_for(s, action: str) -> str:
    """Возвращает эмодзи, который на активном экране соответствует действию."""
    for emoji, act in s.reactions.items():
        if act == action:
            return emoji
    raise AssertionError(f"нет реакции для {action!r}, есть: {s.reactions}")


async def main() -> int:
    from app.models import Base
    from app.db import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()

    mm = FakeMM()
    ctx = BotContext(mm)

    async with SessionMaker() as session:
        await students_svc.bootstrap_admins(session)
        admin = await students_svc.is_admin(session, "admin1", email="Boss@CU.local")
        check("админ распознан по почте", admin is not None and admin.mm_user_id == "admin1")

        student = await make_student(session, mm.users["student1"])
        training = await trainings_svc.create(
            session, title="Йога", description="расслабление", location="Зал",
            instructor="Анна", starts_at=now_utc() + timedelta(days=1), duration_min=60, capacity=5,
        )

    # ---- студент: расписание картинкой → день → тренировка → запись ----
    wd = to_local(training.starts_at).weekday()
    async with SessionMaker() as session:
        s = ctx.store.get("student1")
        await common_h.handle_text(ctx, session, s, student, None, "расписание")
        check("расписание ушло картинкой", bool(s.active_post_id) and bool(mm.posts[-1]["files"]))
        check("на неделе есть реакция-день", bool(action_for(s, f"wk:day:{wd}")))

        await dispatch_action(ctx, session, s, student, None, f"wk:day:{wd}")
        check("открылся день", s.data.get("screen") == "day")

        await dispatch_action(ctx, session, s, student, None, f"tr:view:{training.id}")
        check("открылась тренировка", s.data.get("screen") == "training")

        # записываемся
        await dispatch_action(ctx, session, s, student, None, f"tr:book:{training.id}")
        async with SessionMaker() as check_sess:
            check("запись появилась",
                  await bookings_svc.get_booking(check_sess, student.id, training.id) is not None)

        # мои записи
        await common_h.nav_my(ctx, session, s, student, None, 0)
        check("экран «мои записи»", s.data.get("screen") == "my")

        # опрос
        async with SessionMaker() as sess2:
            booking = await bookings_svc.get_booking(sess2, student.id, training.id)
            await dispatch_action(ctx, sess2, s, student, None, f"poll:yes:{booking.id}")

    # ---- админ: меню → неделя → статистика → создание тренировки (FSM) ----
    async with SessionMaker() as session:
        s = ctx.store.get("admin1")
        admin = await students_svc.is_admin(session, "admin1", email="Boss@CU.local")
        await common_h.handle_text(ctx, session, s, student, admin, "админка")
        check("админка открылась", s.data.get("screen") == "admin_menu")

        await dispatch_action(ctx, session, s, student, admin, "adm:trainings")
        check("админ-неделя картинкой", s.data.get("screen") == "admin_week")

        await dispatch_action(ctx, session, s, student, admin, "adm:stats")
        check("статистика", s.data.get("screen") == "admin_stats")

        await dispatch_action(ctx, session, s, student, admin, "adm:export")
        check("выгрузка CSV создала файл", any(p["files"] for p in mm.posts))

        # создание тренировки по шагам (FSM)
        await dispatch_action(ctx, session, s, student, admin, "adm:new")
        check("FSM шаг 1", s.fsm == "new_training.title")
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "Бокс", [])
        check("FSM шаг 2 после названия", s.fsm == "new_training.instructor")
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "-", [])
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "-", [])
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "-", [])
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "10.10.2026 19:00", [])
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "60", [])
        await admin_h.handle_fsm_post(ctx, session, s, student, admin, "10", [])
        check("после создания FSM сброшен", s.fsm == "")

    # ---- поллинг REST: посты из лички и клики-реакции ----
    from app.mm.client import MMPost
    from app.poller import Poller

    pm = FakeMM()
    pm.me = MMUser(id="bot1", username="workout", email="", is_bot=True)
    pctx = BotContext(pm)
    pl = Poller(pctx)

    # новый пост от пользователя в личке
    pm.channels = [{"id": "dm-s1", "type": "D"}]
    pm.posts_by_channel["dm-s1"] = [
        MMPost(id="p1", channel_id="dm-s1", user_id="student1",
               message="расписание", create_at=pl._start_ms + 1000)
    ]
    evs = await pl._poll()
    check("поллинг достаёт новый пост из лички",
          any(e.kind == "posted" and e.user_id == "student1"
              and e.post.message == "расписание" for e in evs))

    # клик-реакция на активном экране
    ps = pctx.store.get("student1")
    ps.active_post_id = "active-1"
    ps.reactions = {"one": "wk:day:0"}
    pm.reactions_by_post["active-1"] = [{"user_id": "student1", "emoji_name": "one"}]
    evs2 = await pl._poll()
    check("поллинг достаёт клик-реакцию",
          any(e.kind == "reaction_added" and e.emoji == "one" and e.user_id == "student1" for e in evs2))
    evs3 = await pl._poll()
    check("повторный опрос не дублирует клик",
          not any(e.kind == "reaction_added" for e in evs3))

    print(f"\nВсе проверки пройдены: {PASSED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))