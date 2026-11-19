from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Mode(str, Enum):
    BLIND = "blind"
    OPEN = "open"
    HOST = "host"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    reminders_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReminderPreference(Base):
    __tablename__ = "reminder_preferences"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    daily_gift: Mapped[bool] = mapped_column(Boolean, default=False)


class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    host_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    rules: Mapped[str] = mapped_column(Text, default="")
    cover_file_id: Mapped[str] = mapped_column(String(255), default="")
    mode: Mapped[str] = mapped_column(String(12))
    budget: Mapped[str] = mapped_column(String(80), default="По договорённости")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    registration_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registration_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    draw_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wishes_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gift_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipping_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    celebration_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    team_policy: Mapped[str] = mapped_column(String(12), default="mixed")
    advent_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="registration")
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("game_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    nickname: Mapped[str] = mapped_column(String(120))
    team: Mapped[str] = mapped_column(String(80), default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=True)
    wishes: Mapped[str] = mapped_column(Text, default="")
    interests: Mapped[str] = mapped_column(Text, default="")
    avoid: Mapped[str] = mapped_column(Text, default="")
    wishlist_url: Mapped[str] = mapped_column(String(500), default="")
    gift_status: Mapped[str] = mapped_column(String(20), default="assigned")
    received: Mapped[bool] = mapped_column(Boolean, default=False)


class GameHelper(Base):
    __tablename__ = "game_helpers"
    __table_args__ = (UniqueConstraint("game_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))


class ReplacementInvite(Base):
    __tablename__ = "replacement_invites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    leaver_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    giver_id: Mapped[int] = mapped_column(BigInteger)
    receiver_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    reported: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QuestionMute(Base):
    __tablename__ = "question_mutes"
    __table_args__ = (UniqueConstraint("game_id", "giver_id", "receiver_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    giver_id: Mapped[int] = mapped_column(BigInteger)
    receiver_id: Mapped[int] = mapped_column(BigInteger)


class Exclusion(Base):
    __tablename__ = "exclusions"
    __table_args__ = (UniqueConstraint("game_id", "giver_id", "receiver_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    giver_id: Mapped[int] = mapped_column(BigInteger)
    receiver_id: Mapped[int] = mapped_column(BigInteger)


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("game_id", "giver_id"),
        UniqueConstraint("game_id", "receiver_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    giver_id: Mapped[int] = mapped_column(BigInteger)
    receiver_id: Mapped[int] = mapped_column(BigInteger)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    parse_mode: Mapped[str] = mapped_column(String(16), default="")
    media_type: Mapped[str] = mapped_column(String(12), default="")
    media_file_id: Mapped[str] = mapped_column(String(255), default="")
    button_text: Mapped[str] = mapped_column(String(64), default="")
    button_url: Mapped[str] = mapped_column(String(500), default="")
    due_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(160), default="")


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(12), default="")
    media_file_id: Mapped[str] = mapped_column(String(255), default="")
    button_text: Mapped[str] = mapped_column(String(64), default="")
    button_url: Mapped[str] = mapped_column(String(500), default="")
    segment: Mapped[str] = mapped_column(String(20))
    due_at: Mapped[datetime] = mapped_column(DateTime)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
