import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

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
        job.attempts += 1
        job_id, user_id, content, parse_mode = job.id, job.user_id, job.text, job.parse_mode
    try:
        await bot.send_message(user_id, content, parse_mode=parse_mode or None)
    except TelegramForbiddenError:
        with service.sessions.begin() as db:
            db.get(User, user_id).blocked = True
            job = db.get(Delivery, job_id)
            job.state = "failed"
            job.last_error = "forbidden"
    except TelegramRetryAfter as exc:
        with service.sessions.begin() as db:
            job = db.get(Delivery, job_id)
            job.state = "pending"
            job.due_at = utcnow() + timedelta(seconds=exc.retry_after)
    except Exception as exc:  # noqa: BLE001 - Telegram transports can raise various network errors.
        log.warning("Delivery failed: %s", type(exc).__name__)
        with service.sessions.begin() as db:
            job = db.get(Delivery, job_id)
            job.state = "pending" if job.attempts < 5 else "failed"
            job.due_at = utcnow() + timedelta(minutes=min(60, 2 ** job.attempts))
            job.last_error = type(exc).__name__[:160]
    else:
        with service.sessions.begin() as db:
            db.get(Delivery, job_id).state = "sent"
    await asyncio.sleep(0.05)
    return 1


async def worker(service: Service, bot: Bot) -> None:
    # A process may stop after send_message but before marking a job as sent.
    # Requeueing in that narrow window can duplicate a message; operational docs explain it.
    with service.sessions.begin() as db:
        for job in db.scalars(select(Delivery).where(Delivery.state == "sending")):
            job.state = "pending"
    while True:
        try:
            service.schedule_reminders()
            if not await deliver_once(service, bot):
                await asyncio.sleep(5)
        except Exception:
            log.exception("Worker iteration failed")
            await asyncio.sleep(5)
