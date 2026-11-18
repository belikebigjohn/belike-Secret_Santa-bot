from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from santa.models import Mode
from santa.service import AccessDenied, InvalidState, Service


class Form(StatesGroup):
    title = State()
    budget = State()
    dates = State()
    profile = State()
    announcement = State()
    edit_budget = State()


def kb(*items: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in items
    ])


def create_router(service: Service, bot_username: str) -> Router:
    router = Router()

    async def screen(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None):
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    async def show_game(message: Message, game_id: int, viewer_id: int):
        game = service.game(game_id)
        if game is None or game_id not in {g.id for g in service.my_games(viewer_id)}:
            await screen(message, "Игра не найдена или доступ закрыт.")
            return
        member = service.member(game_id, viewer_id)
        total = len(service.members(game_id, viewer_id)) if game.host_id == viewer_id else None
        lines = [f"🎄 <b>{escape(game.title)}</b>", escape(game.description),
                 f"Бюджет: {escape(game.budget)}", f"Статус: {escape(game.status)}"]
        if total is not None:
            lines.append(f"Участников: {total}")
            lines.append(f"Приглашение: https://t.me/{bot_username}?start=join_{game.token}")
        for label, value in (("Регистрация", game.registration_deadline),
                             ("Подарок", game.gift_deadline), ("Вручение", game.celebration_at)):
            if value:
                local = value.replace(tzinfo=UTC).astimezone(ZoneInfo(game.timezone))
                lines.append(f"{label}: {local:%d.%m.%Y %H:%M} ({escape(game.timezone)})")
        buttons = []
        if member:
            buttons.extend([("📝 Анкета", f"profile:{game_id}"), ("🎅 Моя миссия", f"mission:{game_id}")])
        if game.host_id == viewer_id:
            buttons.extend([("👥 Участники", f"members:{game_id}"),
                            ("🎲 Жеребьёвка", f"draw:{game_id}"),
                            ("📊 Готовность", f"progress:{game_id}"),
                            ("📣 Объявление", f"announce:{game_id}")])
            if game.status == "registration":
                buttons.append(("⚙️ Настройки", f"settings:{game_id}"))
            if game.mode != Mode.BLIND.value:
                buttons.append(("🔐 Распределение", f"pairs:{game_id}"))
        await screen(message, "\n".join(line for line in lines if line), kb(*buttons))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        if message.text and "join_" in message.text:
            token = message.text.split("join_", 1)[1].split()[0]
            game = service.game_by_token(token)
            if game:
                await screen(message, f"🎄 <b>{escape(game.title)}</b>\n{escape(game.description)}\n"
                             f"Бюджет: {escape(game.budget)}",
                             kb(("Участвовать", f"join:{game.id}")))
                return
        await screen(message, "🎅 Тайный Санта\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games")))

    @router.callback_query(F.data == "games")
    async def games(call: CallbackQuery):
        await call.answer()
        games_ = service.my_games(call.from_user.id)
        if not games_:
            await screen(call.message, "У вас пока нет игр.", kb(("🎄 Создать игру", "create")))
            return
        await screen(call.message, "🎁 Ваши игры", kb(*[(g.title[:45], f"game:{g.id}") for g in games_]))

    @router.callback_query(F.data == "create")
    async def create(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        await screen(call.message, "Выберите роль ведущего:", kb(
            ("🎁 Играю вслепую", "mode:blind"),
            ("🎩 Играю и всё знаю", "mode:open"),
            ("👑 Только организатор", "mode:host")))

    @router.callback_query(F.data.startswith("mode:"))
    async def mode(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.update_data(mode=call.data.split(":")[1])
        await state.set_state(Form.title)
        await screen(call.message, "Как назовём игру? Отправьте название одним сообщением.")

    @router.message(Form.title)
    async def title(message: Message, state: FSMContext):
        if not message.text or len(message.text) > 120:
            await screen(message, "Название должно быть не длиннее 120 символов.")
            return
        await state.update_data(title=message.text)
        await state.set_state(Form.budget)
        await screen(message, "Укажите бюджет и валюту, например «до 2000 ₽».")

    @router.message(Form.budget)
    async def budget(message: Message, state: FSMContext):
        if not message.text or len(message.text) > 80:
            await screen(message, "Бюджет должен быть не длиннее 80 символов.")
            return
        await state.update_data(budget=message.text)
        await state.set_state(Form.dates)
        await screen(message, "Напишите три даты в формате ДД.ММ.ГГГГ через пробел: "
                     "конец регистрации, подготовка подарка, вручение. Время каждой даты — 18:00 "
                     "по Екатеринбургу. Например: 01.12.2026 20.12.2026 25.12.2026")

    @router.message(Form.dates)
    async def dates(message: Message, state: FSMContext):
        try:
            parts = message.text.split()
            if len(parts) != 3:
                raise ValueError
            local = [datetime.strptime(p, "%d.%m.%Y").replace(hour=18, tzinfo=ZoneInfo("Asia/Yekaterinburg")) for p in parts]
            values = [d.astimezone(UTC).replace(tzinfo=None) for d in local]
            if values != sorted(values) or values[0] <= datetime.now(UTC).replace(tzinfo=None):
                raise ValueError
        except (ValueError, AttributeError, ZoneInfoNotFoundError):
            await screen(message, "Проверьте формат и порядок будущих дат. Пример: 01.12.2026 20.12.2026 25.12.2026")
            return
        data = await state.get_data()
        game = service.create_game(message.from_user.id, message.from_user.full_name,
                                   data["title"], Mode(data["mode"]), data["budget"],
                                   registration_deadline=values[0], gift_deadline=values[1],
                                   celebration_at=values[2])
        await state.clear()
        await screen(message, "Игра создана. Приглашайте друзей! 🎄")
        await show_game(message, game.id, message.from_user.id)

    @router.callback_query(F.data.startswith("game:"))
    async def game(call: CallbackQuery):
        await call.answer()
        await show_game(call.message, int(call.data.split(":")[1]), call.from_user.id)

    @router.callback_query(F.data.startswith("join:"))
    async def join(call: CallbackQuery):
        try:
            member = service.join(int(call.data.split(":")[1]), call.from_user.id, call.from_user.full_name)
            await call.answer("Вы участвуете!")
            await screen(call.message, f"Добро пожаловать, {escape(member.nickname)}! Заполните пожелания.",
                         kb(("📝 Анкета", f"profile:{member.game_id}")))
        except (ValueError, InvalidState) as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("profile:"))
    async def profile(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        if service.member(game_id, call.from_user.id) is None:
            await call.answer("Вы не участвуете в этой игре", show_alert=True)
            return
        await call.answer()
        member = service.member(game_id, call.from_user.id)
        await screen(call.message, "📝 Ваша анкета\n"
                     f"Пожелания: {escape(member.wishes or 'не указаны')}\n"
                     f"Интересы: {escape(member.interests or 'не указаны')}\n"
                     f"Не дарить: {escape(member.avoid or 'не указано')}\n"
                     f"Вишлист: {escape(member.wishlist_url or 'не указан')}",
                     kb(("🎁 Пожелания", f"field:{game_id}:wishes"),
                        ("🎨 Интересы", f"field:{game_id}:interests"),
                        ("🚫 Не дарить", f"field:{game_id}:avoid"),
                        ("🔗 Вишлист", f"field:{game_id}:wishlist_url")))

    @router.callback_query(F.data.startswith("field:"))
    async def profile_field(call: CallbackQuery, state: FSMContext):
        _, id_text, field = call.data.split(":")
        game_id = int(id_text)
        if field not in {"wishes", "interests", "avoid", "wishlist_url"} or service.member(game_id, call.from_user.id) is None:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id, field=field)
        await state.set_state(Form.profile)
        await screen(call.message, "Отправьте новое значение одним сообщением. Для очистки отправьте «-».")

    @router.message(Form.profile)
    async def profile_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            value = message.text or ""
            if data["field"] == "wishlist_url" and value != "-" and value and not value.startswith("https://"):
                raise ValueError("Ссылка должна начинаться с https://")
            service.profile(data["game_id"], message.from_user.id,
                            **{data["field"]: "" if value == "-" else value})
        except ValueError as exc:
            await screen(message, escape(str(exc)))
            return
        await state.clear()
        await screen(message, "Пожелания сохранены 🎁", kb(("К игре", f"game:{data['game_id']}")))

    @router.callback_query(F.data.startswith("mission:"))
    async def mission(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        result = service.assignment(game_id, call.from_user.id)
        await call.answer()
        if result is None:
            await screen(call.message, "Назначение ещё не готово.")
            return
        receiver, game_ = result
        await screen(call.message, f"🎁 <b>Твоя миссия</b>\nПолучатель: {escape(receiver.nickname)}\n"
                     f"Пожелания: {escape(receiver.wishes or 'не указаны')}\n"
                     f"Не дарить: {escape(receiver.avoid or 'не указано')}\n"
                     f"Бюджет: {escape(game_.budget)}",
                     kb(("✅ Подарок готов", f"ready:{game_id}"), ("К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("ready:"))
    async def ready(call: CallbackQuery):
        try:
            service.set_gift_status(int(call.data.split(":")[1]), call.from_user.id, "ready")
            await call.answer("Отмечено! 🎁")
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("members:"))
    async def members(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            people = service.members(game_id, call.from_user.id)
            await call.answer()
            await screen(call.message, "👥 Участники:\n" + "\n".join(
                f"• {escape(p.nickname)}" for p in people))
        except AccessDenied as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("settings:"))
    async def settings(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status != "registration":
            await call.answer("Настройки недоступны", show_alert=True)
            return
        await call.answer()
        await screen(call.message, "⚙️ Настройки до жеребьёвки", kb(
            ("🎁 Играю вслепую", f"setmode:{game_id}:blind"),
            ("🎩 Играю и всё знаю", f"setmode:{game_id}:open"),
            ("👑 Только организатор", f"setmode:{game_id}:host"),
            ("💰 Изменить бюджет", f"editbudget:{game_id}")))

    @router.callback_query(F.data.startswith("setmode:"))
    async def setmode(call: CallbackQuery):
        try:
            _, game_id, mode_ = call.data.split(":")
            service.set_mode(int(game_id), call.from_user.id, Mode(mode_))
            await call.answer("Режим изменён")
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("editbudget:"))
    async def editbudget(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status != "registration":
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.edit_budget)
        await screen(call.message, "Напишите новый бюджет и валюту.")

    @router.message(Form.edit_budget)
    async def editbudget_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            service.set_budget(data["game_id"], message.from_user.id, message.text or "")
        except ValueError as exc:
            await screen(message, escape(str(exc)))
            return
        await state.clear()
        await show_game(message, data["game_id"], message.from_user.id)

    @router.callback_query(F.data.startswith("draw:"))
    async def draw_prompt(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await screen(call.message, "Проверьте участников. После жеребьёвки регистрация закроется, "
                     "а назначения изменять будет нельзя.", kb(("Подтвердить жеребьёвку", f"confirm:{game_id}")))

    @router.callback_query(F.data.startswith("confirm:"))
    async def confirm(call: CallbackQuery):
        try:
            game_id = int(call.data.split(":")[1])
            service.run_draw(game_id, call.from_user.id)
            await call.answer("Жеребьёвка проведена")
            await screen(call.message, "🎲 Готово! Личные назначения поставлены в очередь доставки.")
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("pairs:"))
    async def pairs(call: CallbackQuery):
        try:
            pairs_ = service.all_assignments(int(call.data.split(":")[1]), call.from_user.id)
            await call.answer()
            await screen(call.message, "🔐 Распределение:\n" + "\n".join(
                f"{escape(giver)} → {escape(receiver)}" for giver, receiver in pairs_))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("progress:"))
    async def progress(call: CallbackQuery):
        try:
            total, ready_ = service.progress(int(call.data.split(":")[1]), call.from_user.id)
            await call.answer()
            await screen(call.message, f"🎁 Готово: {ready_} из {total}.")
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("announce:"))
    async def announce_prompt(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.announcement)
        await screen(call.message, "Напишите объявление для участников игры (до 3000 символов).")

    @router.message(Form.announcement)
    async def announce_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            count = service.announce(data["game_id"], message.from_user.id, message.text or "")
        except ValueError as exc:
            await screen(message, escape(str(exc)))
            return
        await state.clear()
        await screen(message, f"📣 Сообщение поставлено в очередь для {count} участников.")

    @router.message(Command("admin"))
    async def admin(message: Message):
        if not service.is_owner(message.from_user.id):
            await screen(message, "Доступ закрыт.")
            return
        await screen(message, "🛠 Владелец бота. Расширенная панель запланирована в ROADMAP.md.")

    @router.message(Command("help"))
    async def help_(message: Message):
        await screen(message, "🎄 Создайте игру через /start, поделитесь приглашением, заполните пожелания "
                     "и проведите жеребьёвку. Вопросы по конкретной игре задавайте её ведущему.")

    @router.message(Command("reminders"))
    async def reminders(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 2 or parts[1] not in {"on", "off"}:
            await screen(message, "Напоминания: /reminders on или /reminders off. "
                         "Назначения и объявления ведущего продолжат приходить.")
            return
        try:
            service.set_reminders(message.from_user.id, parts[1] == "on")
        except AccessDenied as exc:
            await screen(message, escape(str(exc)))
            return
        await screen(message, "Настройка напоминаний сохранена.")

    return router
