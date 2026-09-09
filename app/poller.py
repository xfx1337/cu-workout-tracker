"""Опрос REST вместо WebSocket.

TiMe (Mattermost за ingress ycalb/Istio) не отдаёт web-socket-upgrade — канал
событий в реальном времени недоступен. Поэтому бот опрашивает REST:

- раз в POLL_SECONDS тянет список своих каналов и новые посты в личках;
- посты из личек превращает в событие posted (сообщение от пользователя);
- реакции на «активных» экранах пользователей опрашивает напрямую и разницу
  превращает в событие reaction_added (клик по «кнопке»).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from app.config import settings
from app.mm.client import MMEvent
from app.runtime import BotContext, SessionStore

log = logging.getLogger(__name__)

POLL_SECONDS = settings.poll_interval_seconds


class Poller:
    def __init__(self, ctx: BotContext, store: SessionStore | None = None) -> None:
        self.ctx = ctx
        self.store = store or ctx.store
        self._start_ms = int(time.time() * 1000)
        self._last_post: dict[str, int] = {}                 # channel_id -> last create_at
        self._seen_reactions: dict[str, set[tuple[str, str]]] = {}  # post_id -> {(user, emoji)}

    async def events(self) -> AsyncIterator[MMEvent]:
        while True:
            try:
                for event in await self._poll():
                    yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка опроса событий")
            await asyncio.sleep(POLL_SECONDS)

    async def _poll(self) -> list[MMEvent]:
        events: list[MMEvent] = []
        mm = self.ctx.mm
        me = mm.me
        if me is None:
            await mm.whoami()

        try:
            channels = await mm.list_my_channels()
        except Exception:
            return events
        dms = [c for c in channels if c.get("type") == "D"]

        for ch in dms:
            chid = ch["id"]
            since = self._last_post.get(chid, self._start_ms)
            try:
                posts = await mm.channel_posts_since(chid, since)
            except Exception:
                continue
            if not posts:
                continue
            self._last_post[chid] = max(p.create_at for p in posts)
            for p in sorted(posts, key=lambda x: x.create_at):
                if p.user_id == me.id:
                    continue
                events.append(MMEvent(kind="posted", user_id=p.user_id, post=p, channel_type="D"))

        # клики-реакции по активным экранам пользователей
        for user_id, s in self.store.iter_sessions():
            if not s.active_post_id:
                continue
            try:
                reacts = await mm.post_reactions(s.active_post_id)
            except Exception:
                continue
            key_set = {
                (r.get("user_id", ""), r.get("emoji_name", ""))
                for r in reacts if r.get("user_id") and r.get("user_id") != me.id
            }
            prev = self._seen_reactions.setdefault(s.active_post_id, set())
            for ruid, emoji in key_set - prev:
                if ruid == user_id and emoji:
                    events.append(
                        MMEvent(kind="reaction_added", user_id=ruid, post_id=s.active_post_id, emoji=emoji)
                    )
            prev |= key_set

        return events