from datetime import UTC, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

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
    InputMediaPhoto,
    Message,
)

from santa.formatting import profile_link
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
    announcement_date = State()
    edit_budget = State()
    edit_game = State()
    ask = State()
    answer = State()
    transfer = State()


POSTER = Path(__file__).resolve().parent.parent / "assets" / "host_modes.png"
WELCOME_ART = POSTER.with_name("welcome.png")
MISSION_ART = POSTER.with_name("mission.png")
MODE_NAMES = {"blind": "🎁 Играю вслепую", "open": "🎩 Играю и всё знаю",
              "host": "👑 Только организатор"}
MOSCOW = ZoneInfo("Europe/Moscow")


def local_date(value: str) -> datetime:
    """Convert 18:00 Moscow time to naive UTC for database storage."""
    return datetime.strptime(value.strip(), "%d.%m.%Y").replace(
        hour=18, tzinfo=MOSCOW).astimezone(UTC).replace(tzinfo=None)


def display_date(value: datetime | None) -> str:
    return value.replace(tzinfo=UTC).astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M") if value else "не указана"


def kb(*items: tuple[str, str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pending: InlineKeyboardButton | None = None
    for label, data in items:
        button = InlineKeyboardButton(text=label, callback_data=data)
        full_row = label.startswith(("⬅️", "✅ Подтвердить", "🏁", "🗄")) or len(label) > 24
        if full_row:
            if pending:
                rows.append([pending])
                pending = None
            rows.append([button])
        elif pending:
            if len(pending.text) + len(label) > 36:
                rows.append([pending])
                pending = button
            else:
                rows.append([pending, button])
                pending = None
        else:
            pending = button
    if pending:
        rows.append([pending])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_router(service: Service, bot_username: str) -> Router:
    router = Router()
    art_file_ids: dict[Path, str] = {}

    async def safe_delete(message: Message):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    async def screen(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None,
                     art: Path | str | None = None) -> Message:
        # Telegram captions are limited to 1024 characters; long profiles stay text-only.
        if len(text) > 1000 or (isinstance(art, Path) and not art.is_file()):
            art = None
        media = (art_file_ids.get(art) or FSInputFile(art)) if isinstance(art, Path) else art

        def cache_art(sent: Message) -> Message:
            if isinstance(art, Path) and sent.photo:
                art_file_ids[art] = sent.photo[-1].file_id
            return sent
        if message.from_user and message.from_user.is_bot:
            try:
                if art is not None:
                    if message.photo:
                        return cache_art(await message.edit_media(
                            media=InputMediaPhoto(media=media, caption=text,
                                                  parse_mode="HTML"),
                            reply_markup=keyboard))
                    replacement = await message.answer_photo(media, caption=text,
                                                             reply_markup=keyboard, parse_mode="HTML")
                    await safe_delete(message)
                    return cache_art(replacement)
                if message.photo and len(text) <= 1000:
                    return await message.edit_caption(caption=text, reply_markup=keyboard,
                                                      parse_mode="HTML")
                if message.photo:
                    replacement = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
                    await safe_delete(message)
                    return replacement
                return await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return message
        if art is not None:
            replacement = cache_art(await message.answer_photo(media, caption=text,
                                                   reply_markup=keyboard, parse_mode="HTML"))
        else:
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
        view = service.game_view(game_id, viewer_id)
        if view is None:
            await screen(message, "Игра не найдена или доступ закрыт.")
            return
        game, member, total = view
        lines = [f"🎄 <b>{escape(game.title)}</b>",
                 f"<i>{MODE_NAMES[game.mode]}</i>"]
        if game.description:
            lines.extend(["", escape(game.description[:300])])
        if game.rules:
            lines.extend(["", f"📜 <b>Правила:</b> {escape(game.rules[:300])}"])
        lines.extend(["", f"💰 <b>Бюджет:</b> {escape(game.budget)}",
                       f"🎲 <b>Статус:</b> { {'registration': 'регистрация', 'drawn': 'жеребьёвка проведена', 'finished': 'завершена', 'archived': 'архив'}[game.status] }"])
        if total is not None:
            lines.extend(["", f"👥 <b>Участников:</b> {total}",
                          f"🔗 <b>Приглашение:</b> <code>https://t.me/{bot_username}?start=join_{game.token}</code>"])
        lines.extend(["", "📅 <b>Время московское</b>"])
        for label, value in (("Регистрация", game.registration_deadline),
                             ("Подарок", game.gift_deadline), ("Вручение", game.celebration_at)):
            lines.append(f"{label} — {display_date(value)}")
        buttons = []
        if member:
            buttons.extend([("📝 Анкета", f"profile:{game_id}"), ("🎅 Моя миссия", f"mission:{game_id}")])
            if game.status == "drawn":
                buttons.extend([("🎭 Вопросы", f"questions:{game_id}"),
                                ("📦 Подтвердить получение", f"received:{game_id}")])
        if game.host_id == viewer_id:
            buttons.extend([("👥 Участники", f"members:{game_id}"),
                            ("📊 Готовность", f"progress:{game_id}"),
                            ("📣 Объявление", f"announce:{game_id}")])
            if game.status == "registration":
                buttons.extend([("⚙️ Настройки", f"settings:{game_id}"),
                                ("🎲 Жеребьёвка", f"draw:{game_id}")])
            if game.status == "drawn":
                buttons.append(("🏁 Завершить игру", f"finish:{game_id}"))
            if game.status == "finished":
                buttons.append(("🗄 Архивировать и удалить", f"archive:{game_id}"))
            if game.mode != Mode.BLIND.value:
                buttons.append(("🔐 Распределение", f"pairs:{game_id}"))
        buttons.append(("⬅️ Мои игры", "games"))
        return await screen(message, "\n".join(lines), kb(*buttons),
                            art=game.cover_file_id or WELCOME_ART)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        service.ensure_user(message.from_user.id, message.from_user.full_name)
        if message.text and "join_" in message.text:
            token = message.text.split("join_", 1)[1].split()[0]
            game = service.game_by_token(token)
            if game:
                await screen(message, f"🎄 <b>{escape(game.title)}</b>\n{escape(game.description)}\n"
                             f"Бюджет: {escape(game.budget)}\n"
                             f"Правила: {escape(game.rules or 'не указаны')}",
                             kb(("Участвовать", f"join:{game.id}"),
                                ("⬅️ Главное меню", "home")), art=game.cover_file_id or WELCOME_ART)
                return
        await screen(message, "🎅 <b>Тайный Санта</b>\n\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games"),
                        ("⚙️ Мои напоминания", "personal")), art=WELCOME_ART)

    @router.callback_query(F.data == "home")
    async def home(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        await screen(call.message, "🎅 <b>Тайный Санта</b>\n\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games"),
                        ("⚙️ Мои напоминания", "personal")), art=WELCOME_ART)

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
        await screen(call.message, caption, keyboard, art=POSTER)

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
                             "Одна дата: <code>01.12.2026</code>\n<i>18:00 по Москве.</i>"), "wizard:budget"),
            "gift": (Form.gift_date, ("🎁 <b>К какому дню подготовить подарок?</b>\n\n"
                     "Одна дата: <code>20.12.2026</code>\n<i>18:00 по Москве.</i>"), "wizard:registration"),
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
                        "Отправьте одну дату: <code>01.12.2026</code>\n<i>18:00 по Москве.</i>",
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
                          "Отправьте одну дату: <code>20.12.2026</code>\n<i>18:00 по Москве.</i>",
                          "wizard:budget", "wizard:registration")

    @router.message(Form.gift_date)
    async def gift_date(message: Message, state: FSMContext):
        await accept_date(message, state, "gift_deadline", "registration_deadline",
                          Form.celebration_date,
                          "🎄 <b>Когда вручение подарков?</b>\n\n"
                          "Отправьте одну дату: <code>25.12.2026</code>\n<i>18:00 по Москве.</i>",
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
                     f"<b>Получатель:</b> {profile_link(receiver.user_id, receiver.nickname)}\n\n"
                     f"<b>Пожелания:</b> {escape(receiver.wishes or 'не указаны')}\n\n"
                     f"<b>Не дарить:</b> {escape(receiver.avoid or 'не указано')}\n\n"
                     f"<b>Бюджет:</b> {escape(game_.budget)}",
                      kb(("🛍 Выбираю подарок", f"giftstatus:{game_id}:choosing"),
                         ("🛒 Подарок куплен", f"giftstatus:{game_id}:bought"),
                         ("✅ Подарок готов", f"giftstatus:{game_id}:ready"),
                         ("📮 Подарок отправлен", f"giftstatus:{game_id}:sent"),
                         ("🎁 Подарок вручён", f"giftstatus:{game_id}:given"),
                         ("🎭 Задать вопрос", f"ask:{game_id}"),
                        ("⬅️ К игре", f"game:{game_id}")), art=MISSION_ART)

    @router.callback_query(F.data.startswith("giftstatus:"))
    async def gift_status(call: CallbackQuery):
        _, game_text, status = call.data.split(":")
        try:
            service.set_gift_status(int(game_text), call.from_user.id, status)
            await call.answer("Статус подарка сохранён")
            await screen(call.message, "✅ Статус подарка обновлён.",
                         kb(("⬅️ К миссии", f"mission:{game_text}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

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
                f"• {profile_link(p.user_id, p.nickname)}" for p in people),
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
            ("✏️ Название", f"editgame:{game_id}:title"),
            ("📝 Описание", f"editgame:{game_id}:description"),
            ("📜 Правила", f"editgame:{game_id}:rules"),
            ("🖼 Обложка", f"editgame:{game_id}:cover_file_id"),
            ("🚫 Запреты пар", f"exclusions:{game_id}"),
            ("👑 Передать роль", f"transfer:{game_id}"),
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
                         kb(("⬅️ К игре", f"game:{game_id}")), art=MISSION_ART)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("pairs:"))
    async def pairs(call: CallbackQuery):
        try:
            game_id = int(call.data.split(":")[1])
            pairs_ = service.all_assignments(game_id, call.from_user.id)
            await call.answer()
            await screen(call.message, "🔐 <b>Распределение</b>\n\n" + "\n".join(
                f"{profile_link(giver.user_id, giver.nickname)} → "
                f"{profile_link(receiver.user_id, receiver.nickname)}" for giver, receiver in pairs_),
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
        await screen(call.message, "📣 <b>Объявление</b>\n\nКогда отправить сообщение?",
                     kb(("Сейчас", f"annnow:{game_id}"),
                        ("Запланировать", f"annlater:{game_id}"),
                        ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("annnow:"))
    async def announce_now(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id, announcement_due=None)
        await state.set_state(Form.announcement)
        panel = await screen(call.message, "📣 Напишите объявление (до 3000 символов).",
                             kb(("⬅️ К игре", f"game:{game_id}")))
        await remember(state, panel)

    @router.callback_query(F.data.startswith("annlater:"))
    async def announce_later(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.announcement_date)
        panel = await screen(call.message,
                             "📅 Введите дату отправки в формате ДД.ММ.ГГГГ. Время — 18:00 по Москве.",
                             kb(("⬅️ К игре", f"game:{game_id}")))
        await remember(state, panel)

    @router.message(Form.announcement_date)
    async def announcement_date(message: Message, state: FSMContext):
        try:
            due = local_date(message.text or "")
            if due <= utcnow():
                raise ValueError
        except ValueError:
            await update_panel(message, state, "Введите будущую дату: ДД.ММ.ГГГГ.")
            return
        await state.update_data(announcement_due=due)
        await state.set_state(Form.announcement)
        await update_panel(message, state, "📣 Напишите объявление (до 3000 символов).")

    @router.message(Form.announcement)
    async def announce_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            count = service.announce(data["game_id"], message.from_user.id, message.text or "",
                                     due_at=data.get("announcement_due"))
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
                     "<i>Приходит после 09:00 по Москве, пока подарок не готов.</i>\n\n"
                     f"Новости бота: <b>{'включены' if service.marketing_enabled(call.from_user.id) else 'выключены'}</b>.",
                     kb(("🔕 Выключить" if enabled else "🔔 Включить", "daily:toggle"),
                        ("📬 Настроить новости", "marketing:toggle"),
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
                     "<i>Приходит после 09:00 по Москве, пока подарок не готов.</i>",
                     kb(("🔕 Выключить" if enabled else "🔔 Включить", "daily:toggle"),
                        ("📬 Настроить новости", "marketing:toggle"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "marketing:toggle")
    async def marketing_toggle(call: CallbackQuery):
        enabled = not service.marketing_enabled(call.from_user.id)
        try:
            service.set_marketing(call.from_user.id, enabled)
        except AccessDenied as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer("Сохранено")
        await screen(call.message,
                     f"📬 Новости бота {'включены' if enabled else 'выключены'}. Это не влияет на сообщения по вашим играм.",
                     kb(("📬 Изменить настройку", "marketing:toggle"),
                        ("⬅️ Главное меню", "home")))

    @router.message(Command("admin"))
    async def admin(message: Message):
        if not service.is_owner(message.from_user.id):
            await screen(message, "Доступ закрыт.")
            return
        stats = service.owner_stats(message.from_user.id)
        await screen(message, "🛠 <b>Сводка бота</b>\n\n"
                     f"Пользователей: {stats['users']}\n"
                     f"Игр: {stats['games']}\n"
                     f"Активных игр: {stats['active']}\n"
                     f"Сообщений в очереди: {stats['deliveries_pending']}")

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

    @router.callback_query(F.data.startswith("editgame:"))
    async def edit_game_prompt(call: CallbackQuery, state: FSMContext):
        _, game_id_text, field = call.data.split(":")
        game_id = int(game_id_text)
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status != "registration":
            await call.answer("Настройки недоступны", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id, field=field)
        await state.set_state(Form.edit_game)
        prompt = "Отправьте фотографию" if field == "cover_file_id" else "Отправьте новый текст (для очистки — «-»)"
        panel = await screen(call.message, f"✏️ <b>Настройка игры</b>\n\n{prompt}.",
                             kb(("⬅️ К настройкам", f"settings:{game_id}")))
        await remember(state, panel)

    @router.message(Form.edit_game)
    async def edit_game_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            if data["field"] == "cover_file_id" and not message.photo and message.text != "-":
                raise ValueError("Отправьте фотографию или «-» для удаления обложки")
            value = message.photo[-1].file_id if data["field"] == "cover_file_id" and message.photo else message.text or ""
            service.edit_game(data["game_id"], message.from_user.id,
                              **{data["field"]: "" if value == "-" else value})
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)),
                               kb(("⬅️ К настройкам", f"settings:{data['game_id']}")))
            return
        await update_panel(message, state, "✅ Игра обновлена.",
                           kb(("⬅️ К игре", f"game:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data.startswith("exclusions:"))
    async def exclusions(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            people = service.members(game_id, call.from_user.id)
            existing = service.exclusions(game_id, call.from_user.id)
            names = {p.user_id: p for p in people}
            lines = [f"{index}. {profile_link(p.user_id, p.nickname)}" for index, p in enumerate(people[:30], 1)]
            excluded = [f"{index}. {profile_link(names[e.giver_id].user_id, names[e.giver_id].nickname)}"
                        f" → {profile_link(names[e.receiver_id].user_id, names[e.receiver_id].nickname)}"
                        for index, e in enumerate(existing[:30], 1)
                        if e.giver_id in names and e.receiver_id in names]
            buttons = [(f"➕ №{index}", f"exgiver:{game_id}:{p.user_id}")
                       for index, p in enumerate(people[:30], 1)]
            buttons += [(f"✖ №{index}", f"exremove:{game_id}:{e.giver_id}:{e.receiver_id}")
                        for index, e in enumerate(existing[:30], 1)]
            await call.answer()
            await screen(call.message, "🚫 <b>Запреты направленных пар</b>\n\n"
                         "<b>Выберите дарителя:</b>\n" + ("\n".join(lines) or "Участников нет.")
                         + "\n\n<b>Текущие запреты:</b>\n" + ("\n".join(excluded) or "Нет."),
                         kb(*buttons, ("⬅️ К настройкам", f"settings:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("exgiver:"))
    async def ex_giver(call: CallbackQuery):
        _, game_text, giver_text = call.data.split(":")
        game_id, giver_id = int(game_text), int(giver_text)
        try:
            people = service.members(game_id, call.from_user.id)
            if giver_id not in {p.user_id for p in people}:
                raise ValueError("Участник не найден")
            await call.answer()
            candidates = [p for p in people if p.user_id != giver_id]
            await screen(call.message, "Кому нельзя дарить подарок?\n\n" +
                         "\n".join(f"{i}. {profile_link(p.user_id, p.nickname)}"
                                   for i, p in enumerate(candidates[:30], 1)),
                         kb(*[(f"Выбрать №{i}", f"exadd:{game_id}:{giver_id}:{p.user_id}")
                              for i, p in enumerate(candidates[:30], 1)],
                            ("⬅️ К запретам", f"exclusions:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("exadd:") | F.data.startswith("exremove:"))
    async def ex_change(call: CallbackQuery):
        action, game_text, giver_text, receiver_text = call.data.split(":")
        game_id = int(game_text)
        try:
            if action == "exadd":
                service.add_exclusion(game_id, call.from_user.id, int(giver_text), int(receiver_text))
            else:
                service.remove_exclusion(game_id, call.from_user.id, int(giver_text), int(receiver_text))
            await call.answer("Сохранено")
            await screen(call.message, "🚫 Запреты обновлены.",
                         kb(("⬅️ К запретам", f"exclusions:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("questions:"))
    async def questions(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        if service.member(game_id, call.from_user.id) is None:
            await call.answer("Нет доступа", show_alert=True)
            return
        incoming = service.incoming_questions(game_id, call.from_user.id)
        await call.answer()
        await screen(call.message, "🎭 <b>Анонимные вопросы</b>\n\n" +
                     ("\n".join(f"#{q.id}: {escape(q.text[:100])}" for q in incoming[:10])
                      or "Новых вопросов нет."),
                     kb(("✉️ Спросить получателя", f"ask:{game_id}"),
                        *[(f"Ответить на #{q.id}", f"answer:{q.id}") for q in incoming[:10]],
                        ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("ask:"))
    async def ask_prompt(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        if service.assignment(game_id, call.from_user.id) is None:
            await call.answer("Нет назначения", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.ask)
        panel = await screen(call.message, "🎭 Напишите вопрос получателю. Ваше имя не будет показано.",
                             kb(("⬅️ К вопросам", f"questions:{game_id}")))
        await remember(state, panel)

    @router.message(Form.ask)
    async def ask_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            service.ask_receiver(data["game_id"], message.from_user.id, message.text or "")
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)))
            return
        await update_panel(message, state, "✅ Вопрос отправлен анонимно.",
                           kb(("⬅️ К игре", f"game:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data.startswith("answer:"))
    async def answer_prompt(call: CallbackQuery, state: FSMContext):
        question_id = int(call.data.split(":")[1])
        await call.answer()
        await state.update_data(question_id=question_id)
        await state.set_state(Form.answer)
        panel = await screen(call.message, "🎭 Напишите ответ на вопрос.")
        await remember(state, panel)

    @router.message(Form.answer)
    async def answer_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            service.answer_question(data["question_id"], message.from_user.id, message.text or "")
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)))
            return
        await update_panel(message, state, "✅ Ответ отправлен.")
        await state.clear()

    @router.callback_query(F.data.startswith("received:"))
    async def received(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            service.confirm_received(game_id, call.from_user.id)
            await call.answer("Получение подтверждено")
            await screen(call.message, "📦 Подарок получен!", kb(("⬅️ К игре", f"game:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("finish:") | F.data.startswith("archive:"))
    async def finish_or_archive_prompt(call: CallbackQuery):
        action, game_text = call.data.split(":")
        game_id = int(game_text)
        game = service.game(game_id)
        expected = "drawn" if action == "finish" else "finished"
        if game is None or game.host_id != call.from_user.id or game.status != expected:
            await call.answer("Действие недоступно", show_alert=True)
            return
        await call.answer()
        if action == "finish":
            await screen(call.message,
                         f"🏁 <b>Завершить игру «{escape(game.title)}»?</b>\n\n"
                         "Участники больше не смогут менять статус подарка.",
                         kb(("✅ Подтвердить завершение", f"finishconfirm:{game_id}"),
                            ("⬅️ Отмена", f"game:{game_id}")))
        else:
            await screen(call.message,
                         f"🗄 <b>Архивировать и удалить «{escape(game.title)}»?</b>\n\n"
                         "Игра, участники, назначения, вопросы и запланированные сообщения "
                         "будут удалены без возможности восстановления.",
                         kb(("✅ Подтвердить удаление", f"archiveconfirm:{game_id}"),
                            ("⬅️ Отмена", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("finishconfirm:") | F.data.startswith("archiveconfirm:"))
    async def finish_or_archive_confirm(call: CallbackQuery):
        action, game_text = call.data.split(":")
        game_id = int(game_text)
        try:
            if action == "finishconfirm":
                service.close_game(game_id, call.from_user.id)
                await call.answer("Игра завершена")
                await show_game(call.message, game_id, call.from_user.id)
            else:
                service.archive_game(game_id, call.from_user.id)
                await call.answer("Игра удалена")
                await screen(call.message, "🗄 Игра удалена из архива без возможности восстановления.",
                             kb(("⬅️ Мои игры", "games")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("transfer:"))
    async def transfer_prompt(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            people = service.members(game_id, call.from_user.id)
            await call.answer()
            candidates = [p for p in people if p.user_id != call.from_user.id]
            await screen(call.message, "👑 Выберите нового ведущего:\n\n" +
                         "\n".join(f"{i}. {profile_link(p.user_id, p.nickname)}"
                                   for i, p in enumerate(candidates[:30], 1)),
                         kb(*[(f"Выбрать №{i}", f"sethost:{game_id}:{p.user_id}")
                              for i, p in enumerate(candidates[:30], 1)],
                            ("⬅️ К настройкам", f"settings:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("sethost:"))
    async def set_host(call: CallbackQuery):
        _, game_text, user_text = call.data.split(":")
        try:
            service.transfer_host(int(game_text), call.from_user.id, int(user_text))
            await call.answer("Роль передана")
            await show_game(call.message, int(game_text), call.from_user.id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    return router
