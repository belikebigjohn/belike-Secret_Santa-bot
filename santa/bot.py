from datetime import UTC, datetime
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from santa.models import Mode, utcnow
from santa.service import AccessDenied, InvalidState, Service


class Form(StatesGroup):
    title = State()
    budget = State()
    registration_date = State()
    gift_date = State()
    celebration_date = State()
    profile = State()
    announcement = State()
    edit_budget = State()


POSTER = Path(__file__).resolve().parent.parent / "assets" / "host_modes.png"
MODE_NAMES = {"blind": "🎁 Играю вслепую", "open": "🎩 Играю и всё знаю",
              "host": "👑 Только организатор"}


def local_date(value: str) -> datetime:
    """Convert 18:00 in the server's local timezone to naive UTC."""
    return datetime.strptime(value.strip(), "%d.%m.%Y").replace(hour=18).astimezone(UTC).replace(tzinfo=None)


def display_date(value: datetime | None) -> str:
    return value.replace(tzinfo=UTC).astimezone().strftime("%d.%m.%Y, %H:%M") if value else "не указана"


def kb(*items: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)] for label, data in items
    ])


def create_router(service: Service, bot_username: str) -> Router:
    router = Router()

    async def safe_delete(message: Message):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    async def screen(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None) -> Message:
        if message.from_user and message.from_user.is_bot:
            try:
                if message.photo:
                    return await message.edit_caption(caption=text, reply_markup=keyboard,
                                                      parse_mode="HTML")
                return await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return message
        replacement = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        if message.from_user and message.from_user.is_bot:
            await safe_delete(message)
        return replacement

    async def remember(state: FSMContext, panel: Message):
        await state.update_data(panel_id=panel.message_id, panel_chat=panel.chat.id,
                                panel_photo=bool(panel.photo))

    async def update_panel(message: Message, state: FSMContext, text: str,
                           keyboard: InlineKeyboardMarkup | None = None) -> None:
        data = await state.get_data()
        try:
            if data.get("panel_photo"):
                await message.bot.edit_message_caption(
                    chat_id=data["panel_chat"], message_id=data["panel_id"],
                    caption=text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.bot.edit_message_text(
                    chat_id=data["panel_chat"], message_id=data["panel_id"],
                    text=text, reply_markup=keyboard, parse_mode="HTML")
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await safe_delete(message)
                return
            replacement = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
            await remember(state, replacement)
        except KeyError:
            replacement = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
            await remember(state, replacement)
        await safe_delete(message)

    async def show_game(message: Message, game_id: int, viewer_id: int):
        game = service.game(game_id)
        if game is None or game_id not in {g.id for g in service.my_games(viewer_id)}:
            await screen(message, "Игра не найдена или доступ закрыт.")
            return
        member = service.member(game_id, viewer_id)
        total = len(service.members(game_id, viewer_id)) if game.host_id == viewer_id else None
        lines = [f"🎄 <b>{escape(game.title)}</b>",
                 f"<i>{MODE_NAMES[game.mode]}</i>"]
        if game.description:
            lines.extend(["", escape(game.description[:300])])
        lines.extend(["", f"💰 <b>Бюджет:</b> {escape(game.budget)}",
                      f"🎲 <b>Статус:</b> {'регистрация' if game.status == 'registration' else 'жеребьёвка проведена'}"])
        if total is not None:
            lines.extend(["", f"👥 <b>Участников:</b> {total}",
                          f"🔗 <b>Приглашение:</b> <code>https://t.me/{bot_username}?start=join_{game.token}</code>"])
        lines.extend(["", "📅 <b>Даты по времени сервера</b>"])
        for label, value in (("Регистрация", game.registration_deadline),
                             ("Подарок", game.gift_deadline), ("Вручение", game.celebration_at)):
            lines.append(f"{label} — {display_date(value)}")
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
        buttons.append(("⬅️ Мои игры", "games"))
        return await screen(message, "\n".join(lines), kb(*buttons))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        service.ensure_user(message.from_user.id, message.from_user.full_name)
        if message.text and "join_" in message.text:
            token = message.text.split("join_", 1)[1].split()[0]
            game = service.game_by_token(token)
            if game:
                await screen(message, f"🎄 <b>{escape(game.title)}</b>\n{escape(game.description)}\n"
                             f"Бюджет: {escape(game.budget)}",
                             kb(("Участвовать", f"join:{game.id}"),
                                ("⬅️ Главное меню", "home")))
                return
        await screen(message, "🎅 <b>Тайный Санта</b>\n\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games"),
                        ("⚙️ Мои напоминания", "personal")))

    @router.callback_query(F.data == "home")
    async def home(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        await screen(call.message, "🎅 <b>Тайный Санта</b>\n\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games"),
                        ("⚙️ Мои напоминания", "personal")))

    @router.callback_query(F.data == "games")
    async def games(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        games_ = service.my_games(call.from_user.id)
        if not games_:
            await screen(call.message, "🎁 <b>Мои игры</b>\n\nУ вас пока нет игр.",
                         kb(("🎄 Создать игру", "create"), ("⬅️ Главное меню", "home")))
            return
        await screen(call.message, "🎁 <b>Мои игры</b>\n\nВыберите игру:",
                     kb(*[(g.title[:45], f"game:{g.id}") for g in games_],
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "create")
    async def create(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        caption = ("🎄 <b>Выберите роль ведущего</b>\n\n"
                   "На картинке показано, кто участвует и кто видит пары. "
                   "Роль можно изменить до жеребьёвки.")
        keyboard = kb(
            ("🎁 Играю вслепую", "mode:blind"),
            ("🎩 Играю и всё знаю", "mode:open"),
            ("👑 Только организатор", "mode:host"),
            ("⬅️ Главное меню", "home"))
        if call.message.photo or not POSTER.is_file():
            await screen(call.message, caption, keyboard)
        else:
            await call.message.answer_photo(FSInputFile(POSTER), caption=caption,
                                            reply_markup=keyboard, parse_mode="HTML")
            await safe_delete(call.message)

    @router.callback_query(F.data.startswith("mode:"))
    async def mode(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.update_data(mode=call.data.split(":")[1])
        await state.set_state(Form.title)
        panel = await screen(call.message, "✏️ <b>Название игры</b>\n\nОтправьте название одним сообщением.",
                             kb(("⬅️ Назад", "create")))
        await remember(state, panel)

    @router.callback_query(F.data.startswith("wizard:"))
    async def wizard_back(call: CallbackQuery, state: FSMContext):
        await call.answer()
        step = call.data.split(":")[1]
        prompts = {
            "title": (Form.title, "✏️ <b>Название игры</b>\n\nОтправьте название одним сообщением.", "create"),
            "budget": (Form.budget, "💰 <b>Бюджет подарка</b>\n\nНапример: до 2000 ₽", "wizard:title"),
            "registration": (Form.registration_date, ("📅 <b>Когда закончится регистрация?</b>\n\n"
                             "Одна дата: <code>01.12.2026</code>\n<i>18:00 по времени сервера.</i>"), "wizard:budget"),
            "gift": (Form.gift_date, ("🎁 <b>К какому дню подготовить подарок?</b>\n\n"
                     "Одна дата: <code>20.12.2026</code>\n<i>18:00 по времени сервера.</i>"), "wizard:registration"),
        }
        if step in prompts:
            form, text, back = prompts[step]
            await state.set_state(form)
            panel = await screen(call.message, text, kb(("⬅️ Назад", back)))
            await remember(state, panel)

    async def next_step(message: Message, state: FSMContext, form: State,
                        text: str, back: str):
        await state.set_state(form)
        await update_panel(message, state, text, kb(("⬅️ Назад", back)))

    @router.message(Form.title)
    async def title(message: Message, state: FSMContext):
        if not message.text or not 1 <= len(message.text.strip()) <= 120:
            await update_panel(message, state, "Название должно быть от 1 до 120 символов.",
                               kb(("⬅️ Назад", "create")))
            return
        await state.update_data(title=message.text.strip())
        await next_step(message, state, Form.budget,
                        "💰 <b>Бюджет подарка</b>\n\nНапример: до 2000 ₽", "wizard:title")

    @router.message(Form.budget)
    async def budget(message: Message, state: FSMContext):
        if not message.text or not 1 <= len(message.text.strip()) <= 80:
            await update_panel(message, state, "Бюджет должен быть от 1 до 80 символов.",
                               kb(("⬅️ Назад", "wizard:title")))
            return
        await state.update_data(budget=message.text.strip())
        await next_step(message, state, Form.registration_date,
                        "📅 <b>Когда закончится регистрация?</b>\n\n"
                        "Отправьте одну дату: <code>01.12.2026</code>\n<i>18:00 по времени сервера.</i>",
                        "wizard:budget")

    async def accept_date(message: Message, state: FSMContext, key: str,
                          previous: str | None, next_form: State | None,
                          next_text: str, back: str, next_back: str):
        data = await state.get_data()
        try:
            value = local_date(message.text or "")
            if value <= utcnow() or (previous and value <= data[previous]):
                raise ValueError
        except (ValueError, KeyError):
            await update_panel(message, state,
                               "Проверьте дату. Введите одну будущую дату: <code>ДД.ММ.ГГГГ</code>. "
                               "Следующая дата должна быть позже предыдущей.",
                               kb(("⬅️ Назад", back)))
            return
        await state.update_data(**{key: value})
        if next_form:
            await next_step(message, state, next_form, next_text, next_back)
            return
        data = await state.get_data()
        game = service.create_game(
            message.from_user.id, message.from_user.full_name,
            data["title"], Mode(data["mode"]), data["budget"],
            registration_deadline=data["registration_deadline"],
            gift_deadline=data["gift_deadline"], celebration_at=data["celebration_at"])
        await update_panel(message, state,
                           f"🎄 <b>Игра «{escape(game.title)}» создана!</b>\n\nПриглашайте друзей.",
                           kb(("Открыть игру", f"game:{game.id}")))
        await state.clear()

    @router.message(Form.registration_date)
    async def registration_date(message: Message, state: FSMContext):
        await accept_date(message, state, "registration_deadline", None, Form.gift_date,
                          "🎁 <b>К какому дню подготовить подарок?</b>\n\n"
                          "Отправьте одну дату: <code>20.12.2026</code>\n<i>18:00 по времени сервера.</i>",
                          "wizard:budget", "wizard:registration")

    @router.message(Form.gift_date)
    async def gift_date(message: Message, state: FSMContext):
        await accept_date(message, state, "gift_deadline", "registration_deadline",
                          Form.celebration_date,
                          "🎄 <b>Когда вручение подарков?</b>\n\n"
                          "Отправьте одну дату: <code>25.12.2026</code>\n<i>18:00 по времени сервера.</i>",
                          "wizard:registration", "wizard:gift")

    @router.message(Form.celebration_date)
    async def celebration_date(message: Message, state: FSMContext):
        await accept_date(message, state, "celebration_at", "gift_deadline", None,
                          "", "wizard:gift", "")

    @router.callback_query(F.data.startswith("game:"))
    async def game(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        await show_game(call.message, int(call.data.split(":")[1]), call.from_user.id)

    @router.callback_query(F.data.startswith("join:"))
    async def join(call: CallbackQuery):
        try:
            member = service.join(int(call.data.split(":")[1]), call.from_user.id, call.from_user.full_name)
            await call.answer("Вы участвуете!")
            await show_game(call.message, member.game_id, call.from_user.id)
        except (ValueError, InvalidState) as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("profile:"))
    async def profile(call: CallbackQuery, state: FSMContext):
        await state.clear()
        game_id = int(call.data.split(":")[1])
        if service.member(game_id, call.from_user.id) is None:
            await call.answer("Вы не участвуете в этой игре", show_alert=True)
            return
        await call.answer()
        member = service.member(game_id, call.from_user.id)
        await screen(call.message, "📝 <b>Моя анкета</b>\n\n"
                     f"<b>Пожелания:</b> {escape(member.wishes or 'не указаны')}\n\n"
                     f"<b>Интересы:</b> {escape(member.interests or 'не указаны')}\n\n"
                     f"<b>Не дарить:</b> {escape(member.avoid or 'не указано')}\n\n"
                     f"<b>Вишлист:</b> {escape(member.wishlist_url or 'не указан')}",
                     kb(("🎁 Пожелания", f"field:{game_id}:wishes"),
                        ("🎨 Интересы", f"field:{game_id}:interests"),
                        ("🚫 Не дарить", f"field:{game_id}:avoid"),
                        ("🔗 Вишлист", f"field:{game_id}:wishlist_url"),
                        ("⬅️ К игре", f"game:{game_id}")))

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
        panel = await screen(call.message, "📝 <b>Редактирование анкеты</b>\n\n"
                             "Отправьте новое значение одним сообщением. Для очистки отправьте «-».",
                             kb(("⬅️ К анкете", f"profile:{game_id}")))
        await remember(state, panel)

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
            await update_panel(message, state, escape(str(exc)),
                               kb(("⬅️ К анкете", f"profile:{data['game_id']}")))
            return
        await update_panel(message, state, "✅ Анкета обновлена.",
                           kb(("📝 К анкете", f"profile:{data['game_id']}"),
                              ("⬅️ К игре", f"game:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data.startswith("mission:"))
    async def mission(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        result = service.assignment(game_id, call.from_user.id)
        await call.answer()
        if result is None:
            await screen(call.message, "🎅 <b>Моя миссия</b>\n\nНазначение ещё не готово.",
                         kb(("⬅️ К игре", f"game:{game_id}")))
            return
        receiver, game_ = result
        await screen(call.message, f"🎁 <b>Твоя миссия</b>\n\n"
                     f"<b>Получатель:</b> {escape(receiver.nickname)}\n\n"
                     f"<b>Пожелания:</b> {escape(receiver.wishes or 'не указаны')}\n\n"
                     f"<b>Не дарить:</b> {escape(receiver.avoid or 'не указано')}\n\n"
                     f"<b>Бюджет:</b> {escape(game_.budget)}",
                     kb(("✅ Подарок готов", f"ready:{game_id}"),
                        ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("ready:"))
    async def ready(call: CallbackQuery):
        try:
            game_id = int(call.data.split(":")[1])
            service.set_gift_status(game_id, call.from_user.id, "ready")
            await call.answer("Отмечено! 🎁")
            await screen(call.message, "✅ <b>Подарок готов!</b>\n\nСпасибо, что подготовились заранее.",
                         kb(("⬅️ К игре", f"game:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("members:"))
    async def members(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            people = service.members(game_id, call.from_user.id)
            await call.answer()
            await screen(call.message, "👥 <b>Участники</b>\n\n" + "\n".join(
                f"• {escape(p.nickname)}" for p in people),
                kb(("⬅️ К игре", f"game:{game_id}")))
        except AccessDenied as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("settings:"))
    async def settings(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status != "registration":
            await call.answer("Настройки недоступны", show_alert=True)
            return
        await call.answer()
        await state.clear()
        await screen(call.message, f"⚙️ <b>Настройки до жеребьёвки</b>\n\n"
                     f"<b>Роль:</b> {MODE_NAMES[game_.mode]}\n"
                     f"<b>Бюджет:</b> {escape(game_.budget)}\n\n"
                     "<i>После жеребьёвки изменить их нельзя.</i>", kb(
            ("🎁 Играю вслепую", f"setmode:{game_id}:blind"),
            ("🎩 Играю и всё знаю", f"setmode:{game_id}:open"),
            ("👑 Только организатор", f"setmode:{game_id}:host"),
            ("💰 Изменить бюджет", f"editbudget:{game_id}"),
            ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("setmode:"))
    async def setmode(call: CallbackQuery):
        try:
            _, game_id, mode_ = call.data.split(":")
            service.set_mode(int(game_id), call.from_user.id, Mode(mode_))
            await call.answer("Режим изменён")
            game_ = service.game(int(game_id))
            await screen(call.message, f"⚙️ <b>Настройки до жеребьёвки</b>\n\n"
                         f"<b>Роль:</b> {MODE_NAMES[game_.mode]}\n"
                         f"<b>Бюджет:</b> {escape(game_.budget)}",
                         kb(("🎁 Играю вслепую", f"setmode:{game_id}:blind"),
                            ("🎩 Играю и всё знаю", f"setmode:{game_id}:open"),
                            ("👑 Только организатор", f"setmode:{game_id}:host"),
                            ("💰 Изменить бюджет", f"editbudget:{game_id}"),
                            ("⬅️ К игре", f"game:{game_id}")))
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
        panel = await screen(call.message,
                             f"💰 <b>Изменить бюджет</b>\n\nСейчас: {escape(game_.budget)}\n\n"
                             "Напишите новый бюджет и валюту.",
                             kb(("⬅️ К настройкам", f"settings:{game_id}")))
        await remember(state, panel)

    @router.message(Form.edit_budget)
    async def editbudget_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            service.set_budget(data["game_id"], message.from_user.id, message.text or "")
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)),
                               kb(("⬅️ К настройкам", f"settings:{data['game_id']}")))
            return
        await update_panel(message, state, "✅ Бюджет обновлён.",
                           kb(("⚙️ К настройкам", f"settings:{data['game_id']}"),
                              ("⬅️ К игре", f"game:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data.startswith("draw:"))
    async def draw_prompt(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await screen(call.message, "🎲 <b>Жеребьёвка</b>\n\nПроверьте участников. "
                     "После жеребьёвки регистрация закроется, а назначения изменить нельзя.",
                     kb(("✅ Подтвердить жеребьёвку", f"confirm:{game_id}"),
                        ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("confirm:"))
    async def confirm(call: CallbackQuery):
        try:
            game_id = int(call.data.split(":")[1])
            service.run_draw(game_id, call.from_user.id)
            await call.answer("Жеребьёвка проведена")
            await screen(call.message,
                         "🎲 <b>Жеребьёвка завершена</b>\n\nЛичные назначения поставлены в очередь доставки.",
                         kb(("⬅️ К игре", f"game:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("pairs:"))
    async def pairs(call: CallbackQuery):
        try:
            game_id = int(call.data.split(":")[1])
            pairs_ = service.all_assignments(game_id, call.from_user.id)
            await call.answer()
            await screen(call.message, "🔐 <b>Распределение</b>\n\n" + "\n".join(
                f"{escape(giver)} → {escape(receiver)}" for giver, receiver in pairs_),
                kb(("⬅️ К игре", f"game:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("progress:"))
    async def progress(call: CallbackQuery):
        try:
            game_id = int(call.data.split(":")[1])
            total, ready_ = service.progress(game_id, call.from_user.id)
            await call.answer()
            await screen(call.message, f"🎁 <b>Готовность подарков</b>\n\n"
                         f"Подготовлено: <b>{ready_} из {total}</b>.",
                         kb(("⬅️ К игре", f"game:{game_id}")))
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
        panel = await screen(call.message,
                             "📣 <b>Объявление</b>\n\nНапишите текст для участников игры (до 3000 символов).",
                             kb(("⬅️ К игре", f"game:{game_id}")))
        await remember(state, panel)

    @router.message(Form.announcement)
    async def announce_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            count = service.announce(data["game_id"], message.from_user.id, message.text or "")
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)),
                               kb(("⬅️ К игре", f"game:{data['game_id']}")))
            return
        await update_panel(message, state,
                           f"📣 Сообщение поставлено в очередь для <b>{count}</b> участников.",
                           kb(("⬅️ К игре", f"game:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data == "personal")
    async def personal(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        enabled = service.daily_reminders_enabled(call.from_user.id)
        await screen(call.message, "⚙️ <b>Мои напоминания</b>\n\n"
                     f"Ежедневное напоминание о подарке: <b>{'включено' if enabled else 'выключено'}</b>.\n\n"
                     "<i>Приходит после 09:00 по времени сервера, пока подарок не готов.</i>",
                     kb(("🔕 Выключить" if enabled else "🔔 Включить", "daily:toggle"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "daily:toggle")
    async def daily_toggle(call: CallbackQuery):
        enabled = not service.daily_reminders_enabled(call.from_user.id)
        try:
            service.set_daily_reminders(call.from_user.id, enabled)
        except AccessDenied as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer("Сохранено")
        await screen(call.message, "⚙️ <b>Мои напоминания</b>\n\n"
                     f"Ежедневное напоминание о подарке: <b>{'включено' if enabled else 'выключено'}</b>.\n\n"
                     "<i>Приходит после 09:00 по времени сервера, пока подарок не готов.</i>",
                     kb(("🔕 Выключить" if enabled else "🔔 Включить", "daily:toggle"),
                        ("⬅️ Главное меню", "home")))

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
