from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.db import SessionMaker
from app.services import students as students_svc


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия БД на апдейт."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with SessionMaker() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Кладёт в data студента и (если есть) запись админа."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        session = data["session"]
        data["student"] = await students_svc.get_or_create(session, tg_user.id, tg_user.username)
        data["admin"] = await students_svc.is_admin(session, tg_user.id, tg_user.username)
        return await handler(event, data)
