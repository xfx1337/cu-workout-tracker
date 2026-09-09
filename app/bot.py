from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app import scheduler
from app.config import settings
from app.db import SessionMaker, init_db
from app.handlers import admin, common, student
from app.middlewares import DbSessionMiddleware, UserMiddleware
from app.services import students as students_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать / перезапустить"),
            BotCommand(command="schedule", description="Расписание тренировок"),
            BotCommand(command="my", description="Мои записи"),
            BotCommand(command="help", description="Как это работает"),
            BotCommand(command="admin", description="Админка (для администраторов)"),
        ]
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Именно outer: inner-мидлварь выполняется ПОСЛЕ фильтров, и тогда фильтр
    # IsAdmin не видит admin в данных — админский роутер молча съедает апдейты.
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(DbSessionMiddleware())
        observer.outer_middleware(UserMiddleware())

    # порядок важен: админский роутер раньше студенческого,
    # чтобы FSM-шаги создания тренировки не перехватывались кнопками меню
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(student.router)
    return dp


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await students_svc.bootstrap_admins(session)

    if settings.auth_is_stub:
        log.warning("АВТОРИЗАЦИЯ В РЕЖИМЕ ЗАГЛУШКИ: AUTH_API_URL не задан, пускаем всех")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()
    scheduler_task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        log.info("запускаемся как @%s", me.username)
        await set_commands(bot)
        scheduler_task = asyncio.create_task(scheduler.run(bot))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        log.error("Telegram не принял BOT_TOKEN — проверь значение в .env")
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("bye")
