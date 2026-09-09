from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.tz import now_utc


class Base(DeclarativeBase):
    pass


class BookingStatus(str, enum.Enum):
    BOOKED = "booked"
    CANCELLED_BY_STUDENT = "cancelled_by_student"
    CANCELLED_BY_ADMIN = "cancelled_by_admin"


class Attendance(str, enum.Enum):
    UNKNOWN = "unknown"        # опрос ещё не прошёл / ответа нет
    ATTENDED = "attended"      # пришёл
    NO_SHOW = "no_show"        # не пришёл и не отменил -> страйк
    EXCUSED = "excused"        # не пришёл, но предупредил / админ простил -> без страйка


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(64))

    # то, что вернула ручка ЦУ
    external_student_id: Mapped[str | None] = mapped_column(String(64), index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))

    # «есть справка + расписался в журнале по ТБ»
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime)

    no_show_count: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="student")

    @property
    def is_authorized(self) -> bool:
        return bool(self.external_student_id)

    @property
    def display_name(self) -> str:
        name = self.full_name or (f"@{self.tg_username}" if self.tg_username else None)
        return name or f"id{self.tg_user_id}"


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Training(Base):
    __tablename__ = "trainings"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    instructor: Mapped[str] = mapped_column(String(255), default="")
    location: Mapped[str] = mapped_column(String(255), default="")

    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # наивный UTC
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    capacity: Mapped[int] = mapped_column(Integer, default=25)

    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="training")


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (UniqueConstraint("training_id", "student_id", name="uq_booking_training_student"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    training_id: Mapped[int] = mapped_column(ForeignKey("trainings.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)

    status: Mapped[BookingStatus] = mapped_column(Enum(BookingStatus), default=BookingStatus.BOOKED)
    attendance: Mapped[Attendance] = mapped_column(Enum(Attendance), default=Attendance.UNKNOWN)
    counted_as_strike: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    poll_sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    training: Mapped[Training] = relationship(back_populates="bookings")
    student: Mapped[Student] = relationship(back_populates="bookings")


class AuditLog(Base):
    """Кто из админов что сделал — чтобы потом можно было разобраться."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
