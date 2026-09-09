from __future__ import annotations

import asyncio
import logging

from app import scheduler
from app.actions_server import ActionRegistry, ActionServer
from app.config import settings
from app.db import SessionMaker, init_db
from app.handlers import common as common_h
from app.handlers import dispatch_action
from app.mm.client import MattermostClient
from app.poller import Poller
from app.runtime import BotContext, Notifier
from app.services import students as students_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# тихий поллинг: httpx пишет каждый запрос — при опросе раз в 2 сек это спам
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")


async def handle_event(ctx: BotContext, event) -> None:
    if event.kind == "posted":
        if not event.is_direct or event.post is None:
            return
        s = ctx.store.get(event.user_id)
        async with SessionMaker() as session:
            user = await ctx.user(event.user_id)
            student, admin = await common_h.ensure_identity(session, user)
            if s.fsm:
                from app.handlers import admin as admin_h

                await admin_h.handle_fsm_post(
                    ctx, session, s, student, admin, event.post.message or "", event.post.file_ids
                )
            else:
                await common_h.handle_text(ctx, session, s, student, admin, event.post.message or "")
        return

    if event.kind == "reaction_added":
        s = ctx.store.get(event.user_id)
        if event.post_id != s.active_post_id:
            return  # реакция на старый экран — игнорируем
        action = s.reactions.get(event.emoji)
        if not action:
            return
        async with SessionMaker() as session:
            user = await ctx.user(event.user_id)
            student, admin = await common_h.ensure_identity(session, user)
            await dispatch_action(ctx, session, s, student, admin, action)
        return


async def handle_button(ctx: BotContext, user_id: str, post_id: str, action: str) -> None:
    """Подтверждённый клик по кнопке. Проверки уже сделал ActionServer."""
    s = ctx.store.get(user_id)
    s.active_post_id = post_id
    async with SessionMaker() as session:
        user = await ctx.user(user_id)
        student, admin = await common_h.ensure_identity(session, user)
        await dispatch_action(ctx, session, s, student, admin, action)


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await students_svc.bootstrap_admins(session)

    if not settings.admin_emails:
        log.warning("ADMIN_EMAILS не задан — никого не заведу админом через .env")

    mm = MattermostClient(settings.mm_url, settings.mm_token)
    registry = ActionRegistry()
    ctx = BotContext(mm, actions=registry)
    notifier = Notifier(ctx)
    poller = Poller(ctx)

    # Кнопки-attachments работают, только если сервер Mattermost может к нам
    # достучаться. Нет адреса — молча откатываемся на реакции.
    server: ActionServer | None = None
    if settings.use_buttons:
        server = ActionServer(
            registry,
            lambda uid, pid, act: handle_button(ctx, uid, pid, act),
            settings.mm_listen_port,
        )
    else:
        log.warning("MM_PUBLIC_URL не задан — вместо кнопок будут реакции-эмодзи")

    try:
        me = await mm.whoami()
        log.info("запускаемся как @%s (%s)", me.username, me.email or "-")
    except Exception as exc:
        log.error("не могу подключиться к Mattermost (%s): %s", settings.mm_url, exc)
        await mm.close()
        return

    scheduler_task: asyncio.Task | None = None
    try:
        if server is not None:
            await server.start()
            log.info("кнопки включены, Mattermost будет стучаться на %s", settings.mm_public_url)
        scheduler_task = asyncio.create_task(scheduler.run(notifier))
        async for event in poller.events():
            try:
                await handle_event(ctx, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка обработки события %s", event.kind)
    except asyncio.CancelledError:
        raise
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
        if server is not None:
            await server.stop()
        await mm.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("bye")