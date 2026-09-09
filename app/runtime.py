"""Рантайм бота поверх транспорта Mattermost.

Связывает app/mm/client.py с обработчиками: хранит состояние экрана каждого
пользователя, умеет отправлять «экран» (сообщение + реакции-кнопки) и превращает
пришедшую реакцию в действие через карту emoji -> action.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.actions_server import ActionRegistry
from app.config import settings
from app.mm.client import MattermostClient, MattermostError, MMUser
from app.ui import Screen, build_props, build_reactions

log = logging.getLogger(__name__)


@dataclass
class UserSession:
    """Состояние одного диалога с пользователем.

    active_post_id — последний интерактивный пост: реакции только на него
    считаются кликами. reactions — карта emoji -> action для этого экрана.
    fsm — если бот ждёт текст (создание тренировки и т.п.), имя шага.
    data — контекст экрана (offset недели, weekday, id тренировки и т.п.).
    """

    user_id: str
    active_post_id: str = ""
    reactions: dict[str, str] = field(default_factory=dict)
    fsm: str = ""
    fsm_data: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    def __init__(self) -> None:
        self._by_user: dict[str, UserSession] = {}

    def get(self, user_id: str) -> UserSession:
        s = self._by_user.get(user_id)
        if s is None:
            s = UserSession(user_id=user_id)
            self._by_user[user_id] = s
        return s

    def drop(self, user_id: str) -> None:
        self._by_user.pop(user_id, None)

    def iter_sessions(self) -> list[tuple[str, UserSession]]:
        return list(self._by_user.items())


class BotContext:
    """Слой между транспортом и обработчиками: экраны с реакциями и личность."""

    def __init__(
        self,
        mm: MattermostClient,
        store: SessionStore | None = None,
        actions: ActionRegistry | None = None,
    ) -> None:
        self.mm = mm
        self.store = store or SessionStore()
        self.actions = actions or ActionRegistry()
        self._user_cache: dict[str, MMUser] = {}

    async def user(self, user_id: str) -> MMUser:
        cached = self._user_cache.get(user_id)
        if cached is None:
            cached = await self.mm.get_user(user_id)
            self._user_cache[user_id] = cached
        return cached

    async def _channel(self, user_id: str) -> str:
        return await self.mm.direct_channel(user_id)

    async def dm(self, user_id: str) -> str:
        """Публичный личный канал пользователя (для прямой отправки файлов)."""
        return await self.mm.direct_channel(user_id)

    async def send(
        self,
        user_id: str,
        text: str,
        *,
        reactions: dict[str, str] | None = None,
        file_bytes: bytes | None = None,
        filename: str = "file.png",
        fsm: str = "",
        fsm_data: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> str:
        """Новый экран: сообщение (+вложение) с реакциями-«кнопками».

        Каждый экран — новое сообщение, чтобы не тащить за собой старые реакции
        пользователя и не ловить «лишние» клики. Возвращает id поста.
        """
        channel = await self._channel(user_id)
        file_ids: list[str] | None = None
        if file_bytes is not None:
            file_id = await self.mm.upload_file(channel, filename, file_bytes)
            file_ids = [file_id]
        post = await self.mm.create_post(channel, text, file_ids=file_ids)

        if reactions:
            for emoji in reactions:
                await self.mm.add_reaction(post.id, emoji)

        s = self.store.get(user_id)
        s.active_post_id = post.id
        s.reactions = dict(reactions or {})
        s.fsm = fsm
        if fsm_data is not None:
            s.fsm_data = fsm_data
        if data is not None:
            s.data = data
        return post.id

    async def show(
        self,
        user_id: str,
        scr: Screen,
        *,
        fsm: str = "",
        fsm_data: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        new_message: bool = False,
    ) -> str:
        """Показывает экран.

        С кнопками правим прошлое сообщение — диалог остаётся одним постом,
        как это было в Telegram. С реакциями так нельзя: чужие реакции с него
        не снять, поэтому каждый экран уходит новым сообщением.
        """
        s = self.store.get(user_id)
        channel = await self._channel(user_id)

        file_ids: list[str] = []
        if scr.image is not None:
            file_ids = [await self.mm.upload_file(channel, scr.filename, scr.image)]

        mapping: dict[str, str] = {}
        if settings.use_buttons:
            token = self.actions.issue(user_id)
            url = f"{settings.mm_public_url.rstrip('/')}/mm/action/{token}"
            text = scr.text
            props = build_props(scr, url)
        else:
            token = ""
            text, mapping = build_reactions(scr)
            props = {}

        post = None
        if settings.use_buttons and s.active_post_id and not new_message:
            try:
                post = await self.mm.update_post(
                    s.active_post_id, text, file_ids=file_ids, props=props
                )
            except MattermostError as exc:
                log.info("не смог обновить экран %s, пришлю новый: %s", s.active_post_id, exc)

        if post is None:
            post = await self.mm.create_post(
                channel, text, file_ids=file_ids or None, props=props or None
            )
            for emoji in mapping:
                await self.mm.add_reaction(post.id, emoji)

        if settings.use_buttons:
            self.actions.bind(token, user_id, post.id, {c.action for c in scr.choices})

        s.active_post_id = post.id
        s.reactions = mapping
        s.fsm = fsm
        if fsm_data is not None:
            s.fsm_data = fsm_data
        if data is not None:
            s.data = data
        return post.id

    async def notify(self, user_id: str, text: str) -> None:
        """Пассивное уведомление без «кнопок» — не трогает активный экран."""
        channel = await self._channel(user_id)
        await self.mm.create_post(channel, text)

    async def send_message(self, mm_user_id: str, text: str, **kwargs) -> None:
        """Алиас notify() — чтобы BotContext годился как «бот» для enforcement."""
        await self.notify(mm_user_id, text)

    async def download(self, file_id: str) -> bytes:
        return await self.mm.download_file(file_id)


class Notifier:
    """Реализация интерфейса scheduler.Notifier поверх BotContext.

    Подставляется в планировщик и в enforcement вместо заглушки из тестов.
    """

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    async def send_message(self, mm_user_id: str, text: str, **kwargs) -> None:
        await self.ctx.notify(mm_user_id, text)

    async def send_reminder(self, mm_user_id: str, text: str, training_id: int) -> None:
        # на напоминалке — кнопка «не смогу прийти»
        await self.ctx.send(
            mm_user_id, text,
            reactions={"x": f"tr:cancel:{training_id}"},
            data={"screen": "reminder"},
        )

    async def send_poll(self, mm_user_id: str, text: str, booking_id: int) -> None:
        await self.ctx.send(
            mm_user_id, text,
            reactions={
                "white_check_mark": f"poll:yes:{booking_id}",
                "x": f"poll:no:{booking_id}",
            },
            data={"screen": "poll"},
        )