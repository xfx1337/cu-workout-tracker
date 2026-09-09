"""HTTP-приём нажатий на кнопки Mattermost.

Кнопка-attachment устроена так: при клике СЕРВЕР Mattermost делает POST на адрес,
записанный в кнопке. Поэтому боту нужен адрес, доступный этому серверу (MM_PUBLIC_URL).

Безопасность. Адрес может оказаться доступен не только Mattermost, поэтому:
  * в URL каждого экрана лежит одноразовый токен. Mattermost не отдаёт клиентам
    блок integration, так что подсмотреть чужой токен в интерфейсе нельзя;
  * токен привязан к пользователю и посту: клик засчитывается, только если
    пришедший user_id совпадает с тем, кому этот экран отправляли;
  * действие должно входить в набор действий этого экрана — произвольную строку
    подсунуть не выйдет.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

PATH = "/mm/action/{token}"


@dataclass(slots=True)
class PendingScreen:
    """Экран, ожидающий клика."""

    user_id: str
    post_id: str
    actions: frozenset[str]


class ActionRegistry:
    """Токены активных экранов. Держим по одному на пользователя.

    Старый токен пользователя отзывается при показе нового экрана: клик по
    кнопкам предыдущего сообщения не должен уводить диалог назад.
    """

    def __init__(self) -> None:
        self._by_token: dict[str, PendingScreen] = {}
        self._by_user: dict[str, str] = {}

    def issue(self, user_id: str) -> str:
        old = self._by_user.get(user_id)
        if old:
            self._by_token.pop(old, None)
        token = secrets.token_urlsafe(24)
        self._by_user[user_id] = token
        return token

    def bind(self, token: str, user_id: str, post_id: str, actions: set[str]) -> None:
        self._by_token[token] = PendingScreen(user_id, post_id, frozenset(actions))

    def resolve(self, token: str, user_id: str, action: str) -> PendingScreen | None:
        screen = self._by_token.get(token)
        if screen is None:
            return None
        if screen.user_id != user_id:
            log.warning("клик с чужим user_id: токен %s…, пришёл %s", token[:6], user_id)
            return None
        if action not in screen.actions:
            log.warning("действие %r не с этого экрана", action)
            return None
        return screen

    def drop_user(self, user_id: str) -> None:
        token = self._by_user.pop(user_id, None)
        if token:
            self._by_token.pop(token, None)


Handler = Callable[[str, str, str], Awaitable[None]]
"""(user_id, post_id, action) -> None — сюда уходит подтверждённый клик."""


class ActionServer:
    def __init__(self, registry: ActionRegistry, on_action: Handler, port: int) -> None:
        self.registry = registry
        self.on_action = on_action
        self.port = port
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return web.json_response({"ephemeral_text": "Не понял запрос"}, status=400)

        user_id = body.get("user_id", "")
        context = body.get("context") or {}
        action = context.get("action", "")

        screen = self.registry.resolve(token, user_id, action)
        if screen is None:
            # экран устарел: пользователь жмёт кнопку старого сообщения
            return web.json_response(
                {"ephemeral_text": "Эта кнопка уже неактуальна — открой меню заново."}
            )

        try:
            await self.on_action(user_id, screen.post_id, action)
        except Exception:
            log.exception("ошибка обработки кнопки %s", action)
            return web.json_response({"ephemeral_text": "Что-то пошло не так, попробуй ещё раз"})

        # Экран мы перерисовываем сами через REST, поэтому Mattermost'у отвечать нечем:
        # пустой ответ просто гасит «часики» на кнопке.
        return web.json_response({})

    async def _health(self, _: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(PATH, self._handle)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # host=None — слушаем и IPv4, и IPv6: в некоторых сетях (и в Docker Desktop)
        # имя хоста резолвится в IPv6, и привязка только к 0.0.0.0 остаётся невидимой
        site = web.TCPSite(self._runner, None, self.port)
        await site.start()
        log.info("приём кнопок слушает :%s", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
