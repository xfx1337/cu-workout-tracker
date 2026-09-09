from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Admin, AuditLog, Student
from app.texts import AUTO_BAN_REASON
from app.tz import now_utc


async def get_or_create(
    session: AsyncSession,
    mm_user_id: str,
    *,
    username: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
) -> Student:
    """Заводит студента по данным из Mattermost.

    Отдельной авторизации нет: почта в корпоративном мессенджере уже подтверждена
    организацией, поэтому первого сообщения боту достаточно, чтобы его опознать.
    """
    student = await session.scalar(select(Student).where(Student.mm_user_id == mm_user_id))
    if student is None:
        student = Student(
            mm_user_id=mm_user_id, mm_username=username, email=email, full_name=full_name
        )
        session.add(student)
        await session.commit()
        return student

    # профиль в мессенджере мог поменяться — подтягиваем свежее
    changed = False
    for field, value in (("mm_username", username), ("email", email), ("full_name", full_name)):
        if value and getattr(student, field) != value:
            setattr(student, field, value)
            changed = True
    if changed:
        await session.commit()
    return student


async def accept_consent(session: AsyncSession, student: Student) -> None:
    student.consent_accepted_at = now_utc()
    await session.commit()


async def add_strike(session: AsyncSession, student: Student) -> tuple[int, bool]:
    """Возвращает (текущее число страйков, сработал ли автобан)."""
    student.no_show_count += 1
    just_banned = False
    if student.no_show_count >= settings.no_show_limit and not student.is_banned:
        student.is_banned = True
        student.ban_reason = AUTO_BAN_REASON
        just_banned = True
    await session.commit()
    return student.no_show_count, just_banned


async def set_ban(session: AsyncSession, student: Student, banned: bool, reason: str = "") -> None:
    student.is_banned = banned
    student.ban_reason = reason if banned else None
    if not banned:
        student.no_show_count = 0
    await session.commit()


async def reset_strikes(session: AsyncSession, student: Student) -> None:
    student.no_show_count = 0
    if student.ban_reason == AUTO_BAN_REASON:
        student.is_banned = False
        student.ban_reason = None
    await session.commit()


async def search(session: AsyncSession, query: str, limit: int = 20) -> list[Student]:
    q = f"%{query.strip().lstrip('@').lower()}%"
    stmt = select(Student).where(
        or_(
            func.lower(Student.mm_username).like(q),
            func.lower(Student.full_name).like(q),
            func.lower(Student.email).like(q),
        )
    ).order_by(Student.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def banned_list(session: AsyncSession) -> list[Student]:
    return list(await session.scalars(select(Student).where(Student.is_banned.is_(True)).order_by(Student.id)))


async def by_id(session: AsyncSession, student_id: int) -> Student | None:
    return await session.get(Student, student_id)


# ---------- админы ----------

async def is_admin(
    session: AsyncSession, mm_user_id: str, email: str | None = None, username: str | None = None
) -> Admin | None:
    """Ищет админа сначала по id Mattermost, потом по почте.

    Почта — основной ключ: её выдаёт организация, и именно она указана в ADMIN_EMAILS.
    id мессенджера запоминаем при первом заходе, чтобы дальше не сверять строки.
    """
    admin = await session.scalar(select(Admin).where(Admin.mm_user_id == mm_user_id))
    if admin:
        return admin
    if email:
        admin = await session.scalar(select(Admin).where(Admin.email == email.lower()))
        if admin:
            admin.mm_user_id = mm_user_id
            admin.mm_username = username or admin.mm_username
            await session.commit()
            return admin
    return None


async def bootstrap_admins(session: AsyncSession) -> None:
    for email in settings.admin_emails:
        exists = await session.scalar(select(Admin).where(Admin.email == email))
        if not exists:
            session.add(Admin(email=email, is_superadmin=True))
    await session.commit()


async def list_admins(session: AsyncSession) -> list[Admin]:
    return list(await session.scalars(select(Admin).order_by(Admin.id)))


async def add_admin(session: AsyncSession, email: str) -> Admin | None:
    email = email.strip().lower()
    if "@" not in email:
        return None
    exists = await session.scalar(select(Admin).where(Admin.email == email))
    if exists:
        return exists
    admin = Admin(email=email)
    session.add(admin)
    await session.commit()
    return admin


async def remove_admin(session: AsyncSession, admin_id: int) -> bool:
    admin = await session.get(Admin, admin_id)
    if admin is None or admin.is_superadmin:
        return False
    await session.delete(admin)
    await session.commit()
    return True


async def log_action(
    session: AsyncSession,
    actor_email: str | None,
    actor_username: str | None,
    action: str,
    details: str = "",
) -> None:
    session.add(
        AuditLog(
            actor_email=actor_email, actor_username=actor_username, action=action, details=details
        )
    )
    await session.commit()
