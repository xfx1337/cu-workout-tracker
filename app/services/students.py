from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import StudentIdentity
from app.config import settings
from app.models import Admin, AuditLog, Student
from app.texts import AUTO_BAN_REASON
from app.tz import now_utc


async def get_or_create(session: AsyncSession, tg_user_id: int, username: str | None) -> Student:
    student = await session.scalar(select(Student).where(Student.tg_user_id == tg_user_id))
    if student is None:
        student = Student(tg_user_id=tg_user_id, tg_username=username)
        session.add(student)
        await session.commit()
    elif student.tg_username != username:
        student.tg_username = username
        await session.commit()
    return student


async def attach_identity(session: AsyncSession, student: Student, identity: StudentIdentity) -> Student:
    student.external_student_id = identity.student_id
    student.full_name = identity.full_name or student.full_name
    student.email = identity.email or student.email
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
            func.lower(Student.tg_username).like(q),
            func.lower(Student.full_name).like(q),
            func.lower(Student.external_student_id).like(q),
            func.lower(Student.email).like(q),
        )
    ).order_by(Student.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def banned_list(session: AsyncSession) -> list[Student]:
    return list(await session.scalars(select(Student).where(Student.is_banned.is_(True)).order_by(Student.id)))


async def by_id(session: AsyncSession, student_id: int) -> Student | None:
    return await session.get(Student, student_id)


# ---------- админы ----------

async def is_admin(session: AsyncSession, tg_user_id: int, username: str | None) -> Admin | None:
    admin = await session.scalar(select(Admin).where(Admin.tg_user_id == tg_user_id))
    if admin:
        return admin
    if username:
        admin = await session.scalar(select(Admin).where(Admin.tg_username == username.lower()))
        if admin:
            # запоминаем tg_user_id при первом заходе — дальше username не нужен
            admin.tg_user_id = tg_user_id
            await session.commit()
            return admin
    return None


async def bootstrap_admins(session: AsyncSession) -> None:
    for username in settings.admin_usernames:
        exists = await session.scalar(select(Admin).where(Admin.tg_username == username))
        if not exists:
            session.add(Admin(tg_username=username, is_superadmin=True))
    await session.commit()


async def list_admins(session: AsyncSession) -> list[Admin]:
    return list(await session.scalars(select(Admin).order_by(Admin.id)))


async def add_admin(session: AsyncSession, username: str) -> Admin | None:
    username = username.strip().lstrip("@").lower()
    if not username:
        return None
    exists = await session.scalar(select(Admin).where(Admin.tg_username == username))
    if exists:
        return exists
    admin = Admin(tg_username=username)
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
    session: AsyncSession, actor_tg_id: int | None, actor_username: str | None, action: str, details: str = ""
) -> None:
    session.add(
        AuditLog(actor_tg_id=actor_tg_id, actor_username=actor_username, action=action, details=details)
    )
    await session.commit()
