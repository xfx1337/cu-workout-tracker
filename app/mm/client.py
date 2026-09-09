"""Клиент Mattermost: REST для действий и WebSocket для событий.

Telegram отдавал и сообщения, и нажатия кнопок одним long-polling соединением.
В Mattermost так не выйдет: интерактивные кнопки-attachments заставляют СЕРВЕР
ходить к нам по HTTP, а значит нужен адрес, доступный серверу Mattermost.

Пока такого адреса нет, кликом служат реакции-эмодзи: бот вешает их на сообщение,
а событие reaction_added приходит по тому же WebSocket. Наружу ничего открывать
не надо. Когда появится mm_public_url, тот же экран отрисуется настоящими кнопками.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
import websockets

log = logging.getLogger(__name__)


class MattermostError(RuntimeError):
    pass


@dataclass(slots=True)
class MMUser:
    id: str
    username: str
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    is_bot: bool = False

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.username


@dataclass(slots=True)
class MMPost:
    id: str
    channel_id: str
    user_id: str
    message: str
    props: dict[str, Any] = field(default_factory=dict)
    file_ids: list[str] = field(default_factory=list)
    root_id: str = ""


@dataclass(slots=True)
class MMEvent:
    """Событие из WebSocket, приведённое к удобному виду."""
    kind: str                      # posted | reaction_added | reaction_removed | other
    user_id: str = ""
    post: MMPost | None = None
    post_id: str = ""
    emoji: str = ""
    channel_type: str = ""         # D — личка, O/P — канал
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_direct(self) -> bool:
        return self.channel_type == "D"


def _user(data: dict[str, Any]) -> MMUser:
    return MMUser(
        id=data["id"],
        username=data.get("username", ""),
        email=data.get("email", ""),
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        is_bot=bool(data.get("is_bot")),
    )


def _post(data: dict[str, Any]) -> MMPost:
    return MMPost(
        id=data["id"],
        channel_id=data.get("channel_id", ""),
        user_id=data.get("user_id", ""),
        message=data.get("message", ""),
        props=data.get("props") or {},
        file_ids=data.get("file_ids") or [],
        root_id=data.get("root_id", ""),
    )


class MattermostClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0) -> None:
        if not base_url:
            raise MattermostError("Не задан MM_URL")
        if not token:
            raise MattermostError("Не задан MM_TOKEN")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        self.me: MMUser | None = None
        # кэш личных каналов: user_id -> channel_id, чтобы не дёргать API на каждое сообщение
        self._dm_cache: dict[str, str] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- REST

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise MattermostError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def whoami(self) -> MMUser:
        self.me = _user(await self._request("GET", "/users/me"))
        return self.me

    async def get_user(self, user_id: str) -> MMUser:
        return _user(await self._request("GET", f"/users/{user_id}"))

    async def get_user_by_username(self, username: str) -> MMUser | None:
        try:
            return _user(await self._request("GET", f"/users/username/{username}"))
        except MattermostError:
            return None

    async def direct_channel(self, user_id: str) -> str:
        """Личный канал с пользователем; создаётся при первом обращении."""
        if user_id in self._dm_cache:
            return self._dm_cache[user_id]
        if self.me is None:
            await self.whoami()
        data = await self._request("POST", "/channels/direct", json=[self.me.id, user_id])
        self._dm_cache[user_id] = data["id"]
        return data["id"]

    async def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list[str] | None = None,
        props: dict[str, Any] | None = None,
        root_id: str = "",
    ) -> MMPost:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if file_ids:
            body["file_ids"] = file_ids
        if props:
            body["props"] = props
        if root_id:
            body["root_id"] = root_id
        return _post(await self._request("POST", "/posts", json=body))

    async def update_post(
        self,
        post_id: str,
        message: str,
        *,
        file_ids: list[str] | None = None,
        props: dict[str, Any] | None = None,
    ) -> MMPost:
        """Правка сообщения на месте — аналог edit_message_text в Telegram."""
        body: dict[str, Any] = {"id": post_id, "message": message}
        if file_ids is not None:
            body["file_ids"] = file_ids
        if props is not None:
            body["props"] = props
        return _post(await self._request("PUT", f"/posts/{post_id}", json=body))

    async def delete_post(self, post_id: str) -> None:
        await self._request("DELETE", f"/posts/{post_id}")

    async def upload_file(self, channel_id: str, filename: str, data: bytes) -> str:
        """Заливает файл и возвращает его id для прикрепления к сообщению."""
        resp = await self._http.post(
            "/files",
            params={"channel_id": channel_id, "filename": filename},
            files={"files": (filename, data)},
        )
        if resp.status_code >= 400:
            raise MattermostError(f"upload {filename} -> {resp.status_code}: {resp.text[:300]}")
        infos = resp.json().get("file_infos") or []
        if not infos:
            raise MattermostError("Mattermost не вернул file_infos")
        return infos[0]["id"]

    # ---------- реакции: наш способ получить клик без входящего порта ----------

    async def add_reaction(self, post_id: str, emoji: str) -> None:
        if self.me is None:
            await self.whoami()
        try:
            await self._request(
                "POST", "/reactions",
                json={"user_id": self.me.id, "post_id": post_id, "emoji_name": emoji},
            )
        except MattermostError as exc:
            # реакция уже стоит — не повод падать
            log.debug("не смог поставить реакцию %s: %s", emoji, exc)

    async def remove_reaction(self, post_id: str, emoji: str) -> None:
        if self.me is None:
            await self.whoami()
        try:
            await self._request("DELETE", f"/users/{self.me.id}/posts/{post_id}/reactions/{emoji}")
        except MattermostError as exc:
            log.debug("не смог снять реакцию %s: %s", emoji, exc)

    async def set_reactions(self, post_id: str, emojis: list[str]) -> None:
        """Приводит набор реакций бота к нужному — это и есть «клавиатура»."""
        for emoji in emojis:
            await self.add_reaction(post_id, emoji)

    # ---------------------------------------------------------------- WebSocket

    async def events(self) -> AsyncIterator[MMEvent]:
        """Бесконечный поток событий с переподключением."""
        ws_url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/api/v4/websocket"
        delay = 1

        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=30, max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": self.token},
                    }))
                    log.info("websocket подключён")
                    delay = 1
                    async for raw in ws:
                        event = self._parse(raw)
                        if event is not None:
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("websocket отвалился (%s), переподключаюсь через %s c", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def _parse(self, raw: str | bytes) -> MMEvent | None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return None

        kind = payload.get("event")
        data = payload.get("data") or {}

        if kind == "posted":
            post_raw = data.get("post")
            if not post_raw:
                return None
            post = _post(json.loads(post_raw))
            # свои же сообщения игнорируем, иначе получится эхо
            if self.me and post.user_id == self.me.id:
                return None
            return MMEvent(
                kind="posted", user_id=post.user_id, post=post, post_id=post.id,
                channel_type=data.get("channel_type", ""), raw=payload,
            )

        if kind in ("reaction_added", "reaction_removed"):
            reaction_raw = data.get("reaction")
            if not reaction_raw:
                return None
            reaction = json.loads(reaction_raw)
            if self.me and reaction.get("user_id") == self.me.id:
                return None          # это мы сами расставили «клавиатуру»
            # тип канала в событии реакции не приходит — он тут и не нужен:
            # реакцию мы узнаём по post_id, который сами же и отправляли
            return MMEvent(
                kind=kind,
                user_id=reaction.get("user_id", ""),
                post_id=reaction.get("post_id", ""),
                emoji=reaction.get("emoji_name", ""),
                raw=payload,
            )

        return None
