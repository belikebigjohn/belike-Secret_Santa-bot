import asyncio
import logging

from aiogram import Bot, Dispatcher

from santa.bot import create_router
from santa.config import Config
from santa.service import Service
from santa.worker import worker


async def main() -> None:
    config = Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = Service(config.database_url, config.owner_ids)
    service.create_schema()
    bot = Bot(config.token)
    me = await bot.get_me()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(service, me.username))
    task = asyncio.create_task(worker(service, bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
