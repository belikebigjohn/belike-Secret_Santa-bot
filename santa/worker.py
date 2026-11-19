import asyncio
import logging
import time
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select

from santa.models import Delivery, User, utcnow
from santa.service import Service

log = logging.getLogger(__name__)


async def deliver_once(service: Service, bot: Bot) -> int:
    """Claim one message at a time; pending jobs survive process restarts."""
    with service.sessions.begin() as db:
        job = db.scalar(select(Delivery).where(
            Delivery.state == "pending", Delivery.due_at <= utcnow()
        ).order_by(Delivery.id).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return 0
        job.state = "sending"
        job.claimed_at = utcnow()
        job.attempts += 1
        job_id, user_id, content, parse_mode = job.id, job.user_id, job.text, job.parse_mode
        media_type, media_file_id = job.media_type, job.media_file_id
        button_text, button_url = job.button_text, job.button_url
    markup = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=button_text, url=button_url)]]) if button_text else None)
    try:
        if media_type == "photo":
            await bot.send_photo(user_id, media_file_id, caption=content,
                                 parse_mode=parse_mode or None, reply_markup=markup)
        elif media_type == "video":
            await bot.send_video(user_id, media_file_id, caption=content,
                                 parse_mode=parse_mode or None, reply_markup=markup)
        else:
            await bot.send_message(user_id, content, parse_mode=parse_mode or None,
                                   reply_markup=markup)
    except TelegramForbiddenError:
        with service.sessions.begin() as db:
            user = db.get(User, user_id)
            if user is not None:
                user.blocked = True
            job = db.get(Delivery, job_id)
            if job is not None:
                job.state = "failed"
                job.claimed_at = None
                job.last_error = "forbidden"
    except TelegramRetryAfter as exc:
        with service.sessions.begin() as db:
            job = db.get(Delivery, job_id)
            if job is not None:
                job.state = "pending"
                job.claimed_at = None
                job.due_at = utcnow() + timedelta(seconds=exc.retry_after)
    except Exception as exc:  # noqa: BLE001 - Telegram transports can raise various network errors.
        log.warning("Delivery failed: %s", type(exc).__name__)
        with service.sessions.begin() as db:
            job = db.get(Delivery, job_id)
            if job is not None:
                job.state = "pending" if job.attempts < 5 else "failed"
                job.claimed_at = None
                job.due_at = utcnow() + timedelta(minutes=min(60, 2 ** job.attempts))
                job.last_error = type(exc).__name__[:160]
    else:
        with service.sessions.begin() as db:
            job = db.get(Delivery, job_id)
            if job is not None:
                job.state = "sent"
                job.claimed_at = None
    await asyncio.sleep(0.05)
    return 1


def recover_stale(service: Service) -> int:
    """Requeue only abandoned claims, leaving other live workers undisturbed."""
    stale_before = utcnow() - timedelta(minutes=5)
    with service.sessions.begin() as db:
        jobs = list(db.scalars(select(Delivery).where(
            Delivery.state == "sending",
            or_(Delivery.claimed_at.is_(None), Delivery.claimed_at < stale_before)
        ).with_for_update(skip_locked=True)))
        for job in jobs:
            job.state = "pending"
            job.claimed_at = None
        return len(jobs)


async def worker(service: Service, bot: Bot) -> None:
    # A process may stop after Telegram accepts a message but before its DB commit.
    # A stale claim is retried; this narrow case can duplicate a message.
    recover_stale(service)
    next_reminder_check = 0.0
    next_recovery_check = 0.0
    while True:
        try:
            if time.monotonic() >= next_recovery_check:
                recover_stale(service)
                next_recovery_check = time.monotonic() + 60
            if time.monotonic() >= next_reminder_check:
                service.schedule_reminders()
                next_reminder_check = time.monotonic() + 60
            if not await deliver_once(service, bot):
                await asyncio.sleep(5)
        except Exception:
            log.exception("Worker iteration failed")
            await asyncio.sleep(5)
