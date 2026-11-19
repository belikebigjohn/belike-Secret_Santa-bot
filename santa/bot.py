import re
from datetime import UTC, datetime
from html import escape, unescape
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

from santa.formatting import limited_html, profile_link
from santa.models import Game, Mode, utcnow
from santa.service import AccessDenied, InvalidState, Service


class Form(StatesGroup):
    title = State()
    budget = State()
    registration_date = State()
    gift_date = State()
    celebration_date = State()
    profile = State()
    edit_budget = State()
    edit_game = State()
    ask = State()
    answer = State()
    transfer = State()
    edit_capacity = State()
    edit_date = State()
    admin_date = State()
    admin_text = State()
    admin_link = State()


POSTER = Path(__file__).resolve().parent.parent / "assets" / "host_modes.png"
WELCOME_ART = POSTER.with_name("welcome.png")
MISSION_ART = POSTER.with_name("mission.png")
MODE_NAMES = {"blind": "🎁 Играю вслепую", "open": "🎩 Играю и всё знаю",
              "host": "👑 Только организатор"}
MOSCOW = ZoneInfo("Europe/Moscow")
GAME_TEXT_FIELDS = {
    "title": ("название", 120),
    "description": ("описание", 2000),
    "rules": ("правила", 3000),
}
PROFILE_TEXT_LIMITS = {"wishes": 500, "interests": 500, "avoid": 500,
                       "wishlist_url": 500}
CAPTION_BUDGET = 950


def expandable_quote(value: str) -> str:
    return f"<blockquote expandable>{escape(value)}</blockquote>"


def visible_length(markup: str) -> int:
    """Count caption characters after Telegram has parsed HTML entities."""
    plain = unescape(re.sub(r"<[^>]+>", "", markup))
    return len(plain.encode("utf-16-le")) // 2


def game_card_text(game: Game, total: int | None, bot_username: str) -> tuple[str, bool]:
    lines = [f"🎄 <b>{escape(game.title)}</b>", f"<i>{MODE_NAMES[game.mode]}</i>"]
    text_sections = []
    if game.description:
        text_sections.extend(["", "📝 <b>Описание:</b>", expandable_quote(game.description)])
    if game.rules:
        text_sections.extend(["", "📜 <b>Правила:</b>", expandable_quote(game.rules)])
    lines.extend(["", f"💰 <b>Бюджет:</b> {escape(game.budget)}",
                  f"🎲 <b>Статус:</b> { {'draft': 'черновик', 'registration': 'регистрация', 'drawn': 'жеребьёвка проведена', 'finished': 'завершена', 'archived': 'архив'}[game.status] }"])
    if total is not None:
        lines.extend(["", f"👥 <b>Участников:</b> {total}"])
        if game.status != "draft":
            lines.append(f"🔗 <b>Приглашение:</b> <code>https://t.me/{bot_username}?start=join_{game.token}</code>")
    lines.extend(["", "📅 <b>Время московское</b>"])
    for label, value in (("Регистрация", game.registration_deadline),
                         ("Подарок", game.gift_deadline), ("Вручение", game.celebration_at)):
        lines.append(f"{label} — {display_date(value)}")
    separate_text = visible_length("\n".join(lines[:2] + text_sections + lines[2:])) > CAPTION_BUDGET
    if separate_text:
        for preview_length in (140, 70, 0):
            text_sections = []
            for label, value in (("📝 <b>Описание:</b>", game.description),
                                 ("📜 <b>Правила:</b>", game.rules)):
                if value:
                    text_sections.extend(["", label])
                    if preview_length:
                        text_sections.append(expandable_quote(
                            value[:preview_length] + ("…" if len(value) > preview_length else "")))
            if visible_length("\n".join(lines[:2] + text_sections + lines[2:])) <= CAPTION_BUDGET:
                break
    return "\n".join(lines[:2] + text_sections + lines[2:]), separate_text


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


def page_buttons(prefix: str, page: int, total: int, size: int = 8) -> list[tuple[str, str]]:
    buttons = []
    if page > 0:
        buttons.append(("◀️ Назад", f"{prefix}:{page - 1}"))
    if (page + 1) * size < total:
        buttons.append(("Далее ▶️", f"{prefix}:{page + 1}"))
    return buttons


def page_slice(items: list, page: int, size: int = 8) -> list:
    return items[page * size:(page + 1) * size]


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
        # Telegram captions are limited to 1024 characters after HTML parsing.
        if visible_length(text) > CAPTION_BUDGET or (isinstance(art, Path) and not art.is_file()):
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
                if message.photo and visible_length(text) <= CAPTION_BUDGET:
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
        helper = service.is_helper(game_id, viewer_id)
        caption, separate_text = game_card_text(game, total, bot_username)
        buttons = []
        if separate_text:
            if game.description:
                buttons.append(("📝 Описание полностью", f"gametext:{game_id}:description"))
            if game.rules:
                buttons.append(("📜 Правила полностью", f"gametext:{game_id}:rules"))
        if member:
            buttons.extend([("📝 Анкета", f"profile:{game_id}"), ("🎅 Моя миссия", f"mission:{game_id}")])
            if game.status == "drawn":
                buttons.extend([("🎭 Вопросы", f"questions:{game_id}"),
                                ("📦 Подтвердить получение", f"received:{game_id}")])
        if game.host_id == viewer_id or helper:
            buttons.extend([("👥 Участники", f"members:{game_id}"),
                            ("📊 Готовность", f"progress:{game_id}")])
        if game.host_id == viewer_id:
            buttons.append(("🤝 Помощники", f"helpers:{game_id}"))
            buttons.append(("⚠️ Жалобы", f"reports:{game_id}"))
            if game.status in {"registration", "draft"}:
                buttons.append(("⚙️ Настройки", f"settings:{game_id}"))
            if game.status == "registration":
                buttons.append(("🎲 Жеребьёвка", f"draw:{game_id}"))
            if game.status == "draft":
                buttons.append(("✅ Опубликовать", f"publish:{game_id}"))
            if game.status == "drawn":
                buttons.append(("🏁 Завершить игру", f"finish:{game_id}"))
                buttons.append(("🔄 Заменить участника", f"replace:{game_id}"))
            if game.status == "finished":
                buttons.append(("🗄 Архивировать и удалить", f"archive:{game_id}"))
            if game.mode != Mode.BLIND.value:
                buttons.append(("🔐 Распределение", f"pairs:{game_id}"))
        buttons.append(("⬅️ Мои игры", "games"))
        return await screen(message, caption, kb(*buttons),
                            art=game.cover_file_id or WELCOME_ART)

    @router.callback_query(F.data.startswith("gametext:"))
    async def game_text(call: CallbackQuery):
        _, game_id_text, field = call.data.split(":")
        game_id = int(game_id_text)
        if field not in {"description", "rules"}:
            await call.answer("Раздел не найден", show_alert=True)
            return
        view = service.game_view(game_id, call.from_user.id)
        if view is None:
            await call.answer("Нет доступа", show_alert=True)
            return
        game_, _, _ = view
        label = "Описание" if field == "description" else "Правила"
        await call.answer()
        await screen(call.message, f"<b>{label} игры «{escape(game_.title)}»</b>\n\n"
                     + expandable_quote(getattr(game_, field) or "Не указаны"),
                     kb(("⬅️ К игре", f"game:{game_id}")))

    async def show_invite(message: Message, token: str):
        game_ = service.game_by_token(token)
        if game_ is None:
            await screen(message, "Приглашение больше не действует.",
                         kb(("⬅️ Главное меню", "home")))
            return
        description = game_.description or "не указано"
        rules = game_.rules or "не указаны"
        for preview_length in (None, 300, 120, 0):
            shown_description = description if preview_length is None else description[:preview_length]
            shown_rules = rules if preview_length is None else rules[:preview_length]
            invitation = (f"🎄 <b>{limited_html(game_.title, 300)}</b>\n"
                          f"📝 <b>Описание:</b>\n"
                          f"{expandable_quote(shown_description + ('…' if len(description) > len(shown_description) else '')) if shown_description else ''}\n"
                          f"Бюджет: {limited_html(game_.budget, 250)}\n"
                          f"📜 <b>Правила:</b>\n"
                          f"{expandable_quote(shown_rules + ('…' if len(rules) > len(shown_rules) else '')) if shown_rules else ''}")
            if visible_length(invitation) <= CAPTION_BUDGET:
                break
        buttons = []
        if len(shown_description) < len(description):
            buttons.append(("📝 Описание полностью", f"invitetext:{token}:description"))
        if len(shown_rules) < len(rules):
            buttons.append(("📜 Правила полностью", f"invitetext:{token}:rules"))
        buttons.extend([("Участвовать", f"join:{game_.id}"),
                        ("⬅️ Главное меню", "home")])
        await screen(message, invitation, kb(*buttons), art=game_.cover_file_id or WELCOME_ART)

    @router.callback_query(F.data.startswith("invitetext:"))
    async def invite_text(call: CallbackQuery):
        _, token, field = call.data.split(":")
        game_ = service.game_by_token(token)
        if game_ is None or field not in {"description", "rules"}:
            await call.answer("Приглашение недоступно", show_alert=True)
            return
        label = "Описание" if field == "description" else "Правила"
        await call.answer()
        await screen(call.message, f"<b>{label} игры «{escape(game_.title)}»</b>\n\n"
                     + expandable_quote(getattr(game_, field) or "Не указаны"),
                     kb(("Участвовать", f"join:{game_.id}"),
                        ("⬅️ К приглашению", f"inviteback:{token}")))

    @router.callback_query(F.data.startswith("inviteback:"))
    async def invite_back(call: CallbackQuery):
        await call.answer()
        await show_invite(call.message, call.data.split(":", 1)[1])

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        service.ensure_user(message.from_user.id, message.from_user.full_name)
        if message.text and "replace_" in message.text:
            token = message.text.split("replace_", 1)[1].split()[0]
            replacement = service.replacement_invite(token)
            if replacement:
                game_, leaver = replacement
                await screen(message,
                             f"🔄 <b>Замена в игре «{escape(game_.title)}»</b>\n\n"
                             f"Вы займёте место участника {profile_link(leaver.user_id, leaver.nickname)}. "
                             "Вам назначат получателя, а затронутым людям придёт уведомление.",
                             kb(("✅ Принять участие", f"acceptreplace:{token}"),
                                ("⬅️ Главное меню", "home")))
                return
        if message.text and "join_" in message.text:
            token = message.text.split("join_", 1)[1].split()[0]
            if service.game_by_token(token):
                await show_invite(message, token)
                return
        await screen(message, "🎅 <b>Тайный Санта</b>\n\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games")), art=WELCOME_ART)

    @router.callback_query(F.data == "home")
    async def home(call: CallbackQuery, state: FSMContext):
        await call.answer()
        await state.clear()
        await screen(call.message, "🎅 <b>Тайный Санта</b>\n\nСоздайте игру или откройте свои игры.",
                     kb(("🎄 Создать игру", "create"), ("🎁 Мои игры", "games")), art=WELCOME_ART)

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
        panel = await screen(call.message, "✏️ <b>Название игры</b>\n\nОтправьте название одним сообщением (до 120 символов).",
                             kb(("⬅️ Назад", "create")))
        await remember(state, panel)

    @router.callback_query(F.data.startswith("wizard:"))
    async def wizard_back(call: CallbackQuery, state: FSMContext):
        await call.answer()
        step = call.data.split(":")[1]
        prompts = {
            "title": (Form.title, "✏️ <b>Название игры</b>\n\nОтправьте название одним сообщением (до 120 символов).", "create"),
            "budget": (Form.budget, "💰 <b>Бюджет подарка</b>\n\nНапример: до 2000 ₽. До 80 символов.", "wizard:title"),
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
                        "💰 <b>Бюджет подарка</b>\n\nНапример: до 2000 ₽. До 80 символов.", "wizard:title")

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
        await update_panel(message, state,
                           f"🎄 <b>Проверьте игру «{escape(data['title'])}»</b>\n\n"
                           f"Бюджет: {escape(data['budget'])}\n"
                           f"Регистрация: {display_date(data['registration_deadline'])}\n"
                           f"Подарок: {display_date(data['gift_deadline'])}\n"
                           f"Вручение: {display_date(data['celebration_at'])}",
                           kb(("✅ Создать и открыть", "wizardcommit:publish"),
                              ("⚙️ Дополнительные настройки", "wizardcommit:advanced"),
                              ("💾 Сохранить черновик", "wizardcommit:draft"),
                              ("⬅️ Изменить дату", "wizard:gift")))
        await state.set_state(None)

    @router.callback_query(F.data.startswith("wizardcommit:"))
    async def wizard_commit(call: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not {"title", "mode", "budget", "registration_deadline", "gift_deadline", "celebration_at"} <= data.keys():
            await call.answer("Мастер устарел. Начните создание заново.", show_alert=True)
            return
        advanced = call.data.endswith(":advanced")
        draft = advanced or call.data.endswith(":draft")
        try:
            game_ = service.create_game(
                call.from_user.id, call.from_user.full_name,
                data["title"], Mode(data["mode"]), data["budget"],
                registration_deadline=data["registration_deadline"],
                gift_deadline=data["gift_deadline"], celebration_at=data["celebration_at"],
                draft=draft)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await call.answer("Черновик сохранён" if draft else "Игра создана")
        if advanced:
            await screen(call.message, "⚙️ Черновик создан. Настройте даты, состав и правила перед публикацией.",
                         kb(("⚙️ Открыть настройки", f"settings:{game_.id}"),
                            ("⬅️ К игре", f"game:{game_.id}")))
        else:
            await show_game(call.message, game_.id, call.from_user.id)

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

    @router.callback_query(F.data.startswith("publish:"))
    async def publish_prompt(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status != "draft":
            await call.answer("Черновик недоступен", show_alert=True)
            return
        await call.answer()
        await screen(call.message,
                     f"✅ Опубликовать игру «{escape(game_.title)}» и открыть регистрацию?",
                     kb(("✅ Подтвердить публикацию", f"publishconfirm:{game_id}"),
                        ("⬅️ К черновику", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("publishconfirm:"))
    async def publish_confirm(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            service.publish_game(game_id, call.from_user.id)
            await call.answer("Регистрация открыта")
            await show_game(call.message, game_id, call.from_user.id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("join:"))
    async def join(call: CallbackQuery):
        try:
            member = service.join(int(call.data.split(":")[1]), call.from_user.id, call.from_user.full_name)
            await call.answer("Вы участвуете!" if member.approved else "Заявка отправлена ведущему")
            await show_game(call.message, member.game_id, call.from_user.id)
        except (ValueError, InvalidState) as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("replace:"))
    async def replace_prompt(call: CallbackQuery):
        parts = call.data.split(":")
        game_id = int(parts[1])
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status != "drawn":
            await call.answer("Замена недоступна", show_alert=True)
            return
        people = [p for p in service.members(game_id, call.from_user.id)
                  if p.user_id != call.from_user.id]
        await call.answer()
        await screen(call.message, "🔄 <b>Кого заменить?</b>\n\n"
                     "Новый участник должен сам открыть приглашение. "
                     "После принятия назначения изменятся и затронутые люди получат уведомления.\n\n"
                     + "\n".join(f"{i}. {profile_link(p.user_id, p.nickname[:50])}"
                                 for i, p in enumerate(page_slice(people, page), page * 8 + 1)),
                     kb(*[(f"Выбрать №{i}", f"replacechoose:{game_id}:{p.user_id}")
                          for i, p in enumerate(page_slice(people, page), page * 8 + 1)],
                        *page_buttons(f"replace:{game_id}", page, len(people)),
                        ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("replacechoose:"))
    async def replace_choose(call: CallbackQuery):
        _, game_text, user_text = call.data.split(":")
        try:
            token = service.create_replacement_invite(int(game_text), call.from_user.id,
                                                       int(user_text))
            await call.answer("Ссылка создана")
            await screen(call.message, "🔄 Отправьте ссылку новому участнику. "
                         "Она действует 7 дней и сработает один раз:\n\n"
                         f"<code>https://t.me/{bot_username}?start=replace_{token}</code>",
                         kb(("⬅️ К игре", f"game:{game_text}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("acceptreplace:"))
    async def accept_replacement(call: CallbackQuery):
        token = call.data.split(":", 1)[1]
        try:
            game_id = service.accept_replacement(token, call.from_user.id,
                                                  call.from_user.full_name)
            await call.answer("Вы участвуете в игре")
            await show_game(call.message, game_id, call.from_user.id)
        except ValueError as exc:
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
                     f"<b>Пожелания:</b> {limited_html(member.wishes or 'не указаны', 900)}\n\n"
                     f"<b>Интересы:</b> {limited_html(member.interests or 'не указаны', 900)}\n\n"
                     f"<b>Не дарить:</b> {limited_html(member.avoid or 'не указано', 900)}\n\n"
                     f"<b>Вишлист:</b> {limited_html(member.wishlist_url or 'не указан', 450)}",
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
        limit = PROFILE_TEXT_LIMITS[field]
        panel = await screen(call.message, "📝 <b>Редактирование анкеты</b>\n\n"
                             f"Отправьте новое значение (до {limit} символов) одним сообщением. "
                             "Для очистки отправьте «-».",
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
        wishes = receiver.wishes or "не указаны"
        avoid = receiver.avoid or "не указано"
        for preview_length in (None, 250, 100, 0):
            shown_wishes = wishes if preview_length is None else wishes[:preview_length]
            shown_avoid = avoid if preview_length is None else avoid[:preview_length]
            mission_text = (f"🎁 <b>Твоя миссия</b>\n\n"
                            f"<b>Получатель:</b> {profile_link(receiver.user_id, receiver.nickname[:70])}\n\n"
                            f"<b>Пожелания:</b> {escape(shown_wishes)}{'…' if len(wishes) > len(shown_wishes) else ''}\n\n"
                            f"<b>Не дарить:</b> {escape(shown_avoid)}{'…' if len(avoid) > len(shown_avoid) else ''}\n\n"
                            f"<b>Бюджет:</b> {limited_html(game_.budget, 250)}")
            if visible_length(mission_text) <= CAPTION_BUDGET:
                break
        details_buttons = []
        if len(shown_wishes) < len(wishes):
            details_buttons.append(("🎁 Пожелания целиком", f"missiontext:{game_id}:wishes"))
        if len(shown_avoid) < len(avoid):
            details_buttons.append(("🚫 Запреты целиком", f"missiontext:{game_id}:avoid"))
        await screen(call.message, mission_text,
                      kb(*details_buttons,
                         ("🛍 Выбираю подарок", f"giftstatus:{game_id}:choosing"),
                         ("🛒 Подарок куплен", f"giftstatus:{game_id}:bought"),
                         ("✅ Подарок готов", f"giftstatus:{game_id}:ready"),
                         ("📮 Подарок отправлен", f"giftstatus:{game_id}:sent"),
                         ("🎁 Подарок вручён", f"giftstatus:{game_id}:given"),
                         ("🎭 Задать вопрос", f"ask:{game_id}"),
                        ("⬅️ К игре", f"game:{game_id}")), art=MISSION_ART)

    @router.callback_query(F.data.startswith("missiontext:"))
    async def mission_text_detail(call: CallbackQuery):
        _, game_id_text, field = call.data.split(":")
        game_id = int(game_id_text)
        if field not in {"wishes", "avoid"}:
            await call.answer("Раздел не найден", show_alert=True)
            return
        result = service.assignment(game_id, call.from_user.id)
        if result is None:
            await call.answer("Нет назначения", show_alert=True)
            return
        receiver, _ = result
        label = "Пожелания" if field == "wishes" else "Не дарить"
        await call.answer()
        await screen(call.message, f"<b>{label} получателя</b>\n\n"
                     + escape(getattr(receiver, field) or "Не указано"),
                     kb(("⬅️ К миссии", f"mission:{game_id}")))

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
        parts = call.data.split(":")
        game_id = int(parts[1])
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
        try:
            people = service.members(game_id, call.from_user.id)
            registration = service.game(game_id).status == "registration"
            await call.answer()
            await screen(call.message, f"👥 <b>Участники: {len(people)}</b>\n\n" + "\n".join(
                f"{i}. {profile_link(p.user_id, p.nickname[:55])}"
                f"{' ⏳ ждёт решения' if not p.approved else ''}"
                for i, p in enumerate(page_slice(people, page), page * 8 + 1)),
                kb(*[(f"✅ №{i}", f"review:{game_id}:{p.user_id}:yes")
                     for i, p in enumerate(page_slice(people, page), page * 8 + 1)
                     if not p.approved and registration],
                   *[(f"❌ №{i}", f"review:{game_id}:{p.user_id}:no")
                     for i, p in enumerate(page_slice(people, page), page * 8 + 1)
                     if not p.approved and registration],
                   *page_buttons(f"members:{game_id}", page, len(people)),
                   ("⬅️ К игре", f"game:{game_id}")))
        except AccessDenied as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("settings:"))
    async def settings(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status not in {"registration", "draft"}:
            await call.answer("Настройки недоступны", show_alert=True)
            return
        await call.answer()
        await state.clear()
        await screen(call.message, f"⚙️ <b>Настройки до жеребьёвки</b>\n\n"
                     f"<b>Роль:</b> {MODE_NAMES[game_.mode]}\n"
                     f"<b>Бюджет:</b> {escape(game_.budget)}\n"
                     f"<b>Мест:</b> {game_.max_members or 'без лимита'}\n"
                     f"<b>Заявки:</b> {'подтверждаются' if game_.approval_required else 'входят сразу'}\n\n"
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
            ("📅 Даты", f"dates:{game_id}"),
            ("👥 Лимит мест", f"capacity:{game_id}"),
            ("✅ Заявки", f"approval:{game_id}"),
            ("🤝 Помощники", f"helpers:{game_id}"),
            ("👑 Передать роль", f"transfer:{game_id}"),
            *(("✅ Опубликовать игру", f"publish:{game_id}"),) if game_.status == "draft" else (),
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
        if game_ is None or game_.host_id != call.from_user.id or game_.status not in {"registration", "draft"}:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.edit_budget)
        panel = await screen(call.message,
                             f"💰 <b>Изменить бюджет</b>\n\nСейчас: {escape(game_.budget)}\n\n"
                             "Напишите новый бюджет и валюту (до 80 символов).",
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

    @router.callback_query(F.data.startswith("dates:"))
    async def dates(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status not in {"registration", "draft"}:
            await call.answer("Настройки недоступны", show_alert=True)
            return
        labels = (("Начало регистрации", "registration_start"),
                  ("Конец регистрации", "registration_deadline"),
                  ("План жеребьёвки", "draw_at"),
                  ("Пожелания", "wishes_deadline"),
                  ("Подготовка подарка", "gift_deadline"),
                  ("Отправка подарка", "shipping_deadline"),
                  ("Вручение", "celebration_at"))
        await call.answer()
        await screen(call.message, "📅 <b>Даты по Москве</b>\n\n" + "\n".join(
            f"{label}: {display_date(getattr(game_, field))}" for label, field in labels)
            + "\n\nПлан жеребьёвки — ориентир. Ведущий запускает её вручную.",
            kb(*[(label, f"datefield:{game_id}:{field}") for label, field in labels],
               ("⬅️ К настройкам", f"settings:{game_id}")))

    @router.callback_query(F.data.startswith("datefield:"))
    async def date_prompt(call: CallbackQuery, state: FSMContext):
        _, game_text, field = call.data.split(":")
        game_id = int(game_text)
        game_ = service.game(game_id)
        if (game_ is None or game_.host_id != call.from_user.id or
                game_.status not in {"registration", "draft"} or field not in service.DATE_FIELDS):
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id, date_field=field)
        await state.set_state(Form.edit_date)
        panel = await screen(call.message,
                             "📅 Отправьте дату в формате ДД.ММ.ГГГГ. Время — 18:00 по Москве. "
                             "Для очистки необязательной даты отправьте «-».",
                             kb(("⬅️ К датам", f"dates:{game_id}")))
        await remember(state, panel)

    @router.message(Form.edit_date)
    async def date_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            value = None if message.text == "-" else local_date(message.text or "")
            service.set_game_date(data["game_id"], message.from_user.id, data["date_field"], value)
        except ValueError as exc:
            await update_panel(message, state, f"Проверьте дату: {escape(str(exc))}",
                               kb(("⬅️ К датам", f"dates:{data['game_id']}")))
            return
        await update_panel(message, state, "✅ Дата сохранена.",
                           kb(("⬅️ К датам", f"dates:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data.startswith("capacity:"))
    async def capacity_prompt(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None or game_.host_id != call.from_user.id or game_.status not in {"registration", "draft"}:
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.edit_capacity)
        panel = await screen(call.message, "👥 Укажите число мест от 2 до 1000. «-» убирает лимит.",
                             kb(("⬅️ К настройкам", f"settings:{game_id}")))
        await remember(state, panel)

    @router.message(Form.edit_capacity)
    async def capacity_text(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            maximum = None if message.text == "-" else int(message.text or "")
            service.set_max_members(data["game_id"], message.from_user.id, maximum)
        except ValueError as exc:
            await update_panel(message, state, f"Проверьте лимит: {escape(str(exc))}")
            return
        await update_panel(message, state, "✅ Лимит мест сохранён.",
                           kb(("⬅️ К настройкам", f"settings:{data['game_id']}")))
        await state.clear()

    @router.callback_query(F.data.startswith("approval:"))
    async def approval_toggle(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        game_ = service.game(game_id)
        if game_ is None:
            await call.answer("Игра не найдена", show_alert=True)
            return
        try:
            service.set_approval_required(game_id, call.from_user.id, not game_.approval_required)
            await call.answer("Сохранено")
            await screen(call.message, "✅ Подтверждение заявок "
                         + ("включено." if not game_.approval_required else "выключено."),
                         kb(("⬅️ К настройкам", f"settings:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("review:"))
    async def review_member(call: CallbackQuery):
        _, game_text, user_text, decision = call.data.split(":")
        try:
            service.review_member(int(game_text), call.from_user.id, int(user_text), decision == "yes")
            await call.answer("Заявка обработана")
            await screen(call.message, "✅ Заявка обработана.",
                         kb(("⬅️ К участникам", f"members:{game_text}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("helpers:"))
    async def helpers(call: CallbackQuery):
        parts = call.data.split(":")
        game_id = int(parts[1])
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
        try:
            people = service.members(game_id, call.from_user.id)
            if service.game(game_id).host_id != call.from_user.id:
                raise AccessDenied("Только ведущий назначает помощников")
            selected = {helper.user_id for helper in service.helpers(game_id, call.from_user.id)}
            candidates = [p for p in people if p.user_id != call.from_user.id]
            await call.answer()
            await screen(call.message,
                         "🤝 <b>Помощники ведущего</b>\n\n"
                         "Помощник видит участников и общую готовность, обрабатывает заявки, "
                         "назначает команды и отправляет объявления. Пары и жеребьёвка ему недоступны.\n\n"
                         + "\n".join(f"{i}. {profile_link(p.user_id, p.nickname[:45])}"
                                     f" {'✅' if p.user_id in selected else ''}"
                                     for i, p in enumerate(page_slice(candidates, page), page * 8 + 1)),
                         kb(*[(f"{'Убрать' if p.user_id in selected else 'Назначить'} №{i}",
                               f"helper:{game_id}:{p.user_id}:{'off' if p.user_id in selected else 'on'}")
                              for i, p in enumerate(page_slice(candidates, page), page * 8 + 1)],
                            *page_buttons(f"helpers:{game_id}", page, len(candidates)),
                            ("⬅️ К игре", f"game:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("helper:"))
    async def helper_change(call: CallbackQuery):
        _, game_text, user_text, enabled_text = call.data.split(":")
        try:
            service.set_helper(int(game_text), call.from_user.id, int(user_text), enabled_text == "on")
            await call.answer("Сохранено")
            await screen(call.message, "🤝 Список помощников обновлён.",
                         kb(("⬅️ К помощникам", f"helpers:{game_text}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

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
            parts = call.data.split(":")
            game_id = int(parts[1])
            page = max(0, int(parts[2])) if len(parts) > 2 else 0
            pairs_ = service.all_assignments(game_id, call.from_user.id)
            await call.answer()
            await screen(call.message, f"🔐 <b>Распределение: {len(pairs_)}</b>\n\n" + "\n".join(
                f"{profile_link(giver.user_id, giver.nickname[:30])} → "
                f"{profile_link(receiver.user_id, receiver.nickname[:30])}"
                for giver, receiver in page_slice(pairs_, page)),
                kb(*page_buttons(f"pairs:{game_id}", page, len(pairs_)),
                   ("⬅️ К игре", f"game:{game_id}")))
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

    @router.message(Command("admin"))
    async def admin(message: Message):
        if not service.is_owner(message.from_user.id):
            await screen(message, "Доступ закрыт.")
            return
        service.ensure_user(message.from_user.id, message.from_user.full_name)
        stats = service.owner_stats(message.from_user.id)
        await screen(message, "🛠 <b>Сводка бота</b>\n\n"
                     f"Пользователей: {stats['users']}\n"
                     f"Новых за 7 дней: {stats['users_new_week']}\n"
                     f"Доступны для рассылки: {stats['reachable_users']}\n"
                     f"Заблокировали бота: {stats['blocked_users']}\n"
                     f"Игр: {stats['games']}\n"
                     f"Новых игр за 7 дней: {stats['games_new_week']}\n"
                     f"Регистраций в играх: {stats['registrations']}\n"
                     f"Активных игр: {stats['active']}\n"
                     f"После жеребьёвки: {stats['drawn']}\n"
                     f"Завершённых: {stats['finished']}\n"
                     f"Доставлено сообщений: {stats['deliveries_sent']}\n"
                     f"Сообщений в очереди: {stats['deliveries_pending']}\n"
                     f"Отправляются: {stats['deliveries_sending']}\n"
                     f"Ошибок доставки: {stats['deliveries_failed']}",
                     kb(("📬 Новая рассылка", "adminbroadcast:new"),
                        ("📊 Рассылки", "adminbroadcast:list"),
                        ("🩺 Состояние", "adminhealth"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "adminhealth")
    async def admin_health(call: CallbackQuery):
        try:
            status = service.owner_health(call.from_user.id)
        except AccessDenied as exc:
            await call.answer(str(exc), show_alert=True)
            return
        worker_age = status["worker_age_seconds"]
        overdue = status["oldest_overdue_seconds"]
        await call.answer()
        await screen(call.message, "🩺 <b>Состояние бота</b>\n\n"
                     f"Фоновая отправка: {'работает' if worker_age is not None and worker_age < 30 else 'нет свежего сигнала'}"
                     f"{f' · {worker_age} с назад' if worker_age is not None else ''}\n"
                     f"Самое старое просроченное сообщение: {f'{overdue} с' if overdue is not None else 'нет'}\n"
                     f"Пауза Telegram API: {status['rate_limit_seconds']} с\n"
                     f"Временных ошибок для повтора: {status['retryable_failures']}",
                     kb(("🔄 Обновить", "adminhealth"),
                        ("📊 Рассылки", "adminbroadcast:list"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "adminbroadcast:new")
    async def admin_broadcast_new(call: CallbackQuery, state: FSMContext):
        if not service.is_owner(call.from_user.id):
            await call.answer("Нет доступа", show_alert=True)
            return
        await state.clear()
        try:
            count = service.broadcast_audience_count(call.from_user.id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await state.update_data(broadcast_all=True)
        await call.answer()
        await screen(call.message, f"📬 <b>Новая рассылка</b>\n\nПолучатели: все {count} человек, "
                     "которые запускали бота и не заблокировали его.\n\nКогда отправить?",
                     kb(("Сейчас", "adminbroadcast:now"),
                        ("Запланировать", "adminbroadcast:later"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "adminbroadcast:now")
    async def admin_broadcast_now(call: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not service.is_owner(call.from_user.id) or not data.get("broadcast_all"):
            await call.answer("Начните рассылку заново", show_alert=True)
            return
        await state.update_data(broadcast_due=None)
        await state.set_state(Form.admin_text)
        await call.answer()
        panel = await screen(call.message, "📬 Отправьте текст (до 3000 символов) или фото/видео "
                             "с подписью (до 900 символов).")
        await remember(state, panel)

    @router.callback_query(F.data == "adminbroadcast:later")
    async def admin_broadcast_later(call: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not service.is_owner(call.from_user.id) or not data.get("broadcast_all"):
            await call.answer("Начните рассылку заново", show_alert=True)
            return
        await state.set_state(Form.admin_date)
        await call.answer()
        panel = await screen(call.message, "📅 Укажите одну дату в формате ДД.ММ.ГГГГ. "
                             "Время отправки — 18:00 по Москве.")
        await remember(state, panel)

    @router.message(Form.admin_date)
    async def admin_date_text(message: Message, state: FSMContext):
        try:
            due = local_date(message.text or "")
            if due <= utcnow():
                raise ValueError
        except ValueError:
            await update_panel(message, state, "Введите будущую дату ДД.ММ.ГГГГ.")
            return
        await state.update_data(broadcast_due=due)
        await state.set_state(Form.admin_text)
        await update_panel(message, state, "📬 Отправьте текст (до 3000 символов) или фото/видео "
                           "с подписью (до 900 символов).")

    @router.message(Form.admin_text)
    async def admin_text(message: Message, state: FSMContext):
        content = (message.caption or message.text or "").strip()
        media_type = "photo" if message.photo else "video" if message.video else ""
        media_file_id = (message.photo[-1].file_id if message.photo else
                         message.video.file_id if message.video else "")
        try:
            if not service.is_owner(message.from_user.id):
                raise AccessDenied("Нет доступа")
            service.validate_broadcast(content, media_type, media_file_id)
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)))
            return
        await state.update_data(broadcast_text=content, broadcast_media_type=media_type,
                                broadcast_media_file_id=media_file_id)
        await state.set_state(Form.admin_link)
        await update_panel(message, state, "🔗 Если нужна кнопка, отправьте строку вида "
                           "<code>Открыть сайт | https://example.com</code>. "
                           "Название — до 64 символов, ссылка — до 500. "
                           "Без кнопки отправьте <code>-</code>.")

    @router.message(Form.admin_link)
    async def admin_link(message: Message, state: FSMContext):
        data = await state.get_data()
        raw = (message.text or "").strip()
        if not service.is_owner(message.from_user.id):
            await update_panel(message, state, "Нет доступа")
            return
        if raw == "-":
            button_text, button_url = "", ""
        elif "|" in raw:
            button_text, button_url = [part.strip() for part in raw.split("|", 1)]
        else:
            await update_panel(message, state, "Введите название | https://ссылка или -.")
            return
        try:
            service.validate_broadcast(data["broadcast_text"],
                                       data.get("broadcast_media_type", ""),
                                       data.get("broadcast_media_file_id", ""),
                                       button_text, button_url)
        except ValueError as exc:
            await update_panel(message, state, escape(str(exc)))
            return
        await state.update_data(broadcast_button_text=button_text,
                                broadcast_button_url=button_url)
        count = service.broadcast_audience_count(message.from_user.id)
        media_note = (f"\nМедиа: {data['broadcast_media_type']}" if
                      data.get("broadcast_media_type") else "")
        button_note = (f"\nКнопка: {escape(button_text)} → {escape(button_url)}" if button_text else "")
        await update_panel(message, state,
                           f"📬 <b>Предпросмотр для {count} пользователей</b>\n\n"
                           f"{limited_html(data['broadcast_text'], 3000)}\n\n"
                           f"Отправка: {display_date(data.get('broadcast_due')) if data.get('broadcast_due') else 'сейчас'}"
                           f"{media_note}{button_note}",
                           kb(("🧪 Отправить себе", "adminbroadcast:test"),
                              ("✅ Подтвердить рассылку", "adminbroadcast:confirm"),
                              ("⬅️ Отмена", "home")))
        await state.set_state(None)

    @router.callback_query(F.data == "adminbroadcast:test")
    async def admin_broadcast_test(call: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not service.is_owner(call.from_user.id) or not data.get("broadcast_text"):
            await call.answer("Предпросмотр устарел", show_alert=True)
            return
        markup = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=data["broadcast_button_text"], url=data["broadcast_button_url"])]])
            if data.get("broadcast_button_text") else None)
        caption = "🧪 Тест рассылки\n\n" + data["broadcast_text"]
        media_type = data.get("broadcast_media_type")
        if media_type == "photo":
            await call.bot.send_photo(call.from_user.id, data["broadcast_media_file_id"],
                                      caption=caption, reply_markup=markup)
        elif media_type == "video":
            await call.bot.send_video(call.from_user.id, data["broadcast_media_file_id"],
                                      caption=caption, reply_markup=markup)
        else:
            await call.bot.send_message(call.from_user.id, caption, reply_markup=markup)
        await call.answer("Тест отправлен вам")

    @router.callback_query(F.data == "adminbroadcast:confirm")
    async def admin_broadcast_confirm(call: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if not data.get("broadcast_text") or not data.get("broadcast_all"):
            await call.answer("Предпросмотр устарел", show_alert=True)
            return
        try:
            campaign_id, count = service.create_broadcast(
                call.from_user.id, data["broadcast_text"],
                data.get("broadcast_due"), media_type=data.get("broadcast_media_type", ""),
                media_file_id=data.get("broadcast_media_file_id", ""),
                button_text=data.get("broadcast_button_text", ""),
                button_url=data.get("broadcast_button_url", ""))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await call.answer("Рассылка запланирована")
        await screen(call.message, f"📬 Рассылка #{campaign_id} поставлена в очередь для {count} пользователей.",
                     kb(("📊 Статус", f"adminstats:{campaign_id}"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data == "adminbroadcast:list")
    async def admin_broadcast_list(call: CallbackQuery):
        try:
            campaigns = service.broadcasts(call.from_user.id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer()
        await screen(call.message, "📊 <b>Последние рассылки</b>\n\n"
                     + ("Выберите рассылку." if campaigns else "Пока нет рассылок."),
                     kb(*[(f"#{item.id} · {' '.join(item.text.split())[:25]}", f"adminstats:{item.id}")
                          for item in campaigns], ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data.startswith("adminstats:"))
    async def admin_broadcast_stats(call: CallbackQuery):
        campaign_id = int(call.data.split(":")[1])
        try:
            stats = service.broadcast_stats(call.from_user.id, campaign_id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer()
        names = {"queued": "в очереди", "sending": "отправляется", "finished": "завершена",
                 "scheduled": "запланирована", "failed": "есть ошибки", "cancelled": "отменена"}
        error_note = f"\nПричины ошибок: {escape(stats['errors'])}" if stats["errors"] else ""
        actions = []
        if stats["failed"] and stats["state"] != "cancelled":
            actions.append(("🔁 Повторить ошибки", f"adminretry:{campaign_id}"))
        if stats["pending"]:
            actions.append(("✖ Отменить ожидающие", f"admincancel:{campaign_id}"))
        await screen(call.message, f"📊 <b>Рассылка #{campaign_id}</b>\n\n"
                     f"Состояние: {names.get(stats['state'], stats['state'])}\n"
                     f"Всего: {stats['total']}\nОтправлено: {stats['sent']}\n"
                     f"Ожидают: {stats['pending']}\nОтправляются: {stats['sending']}\n"
                     f"Ошибки: {stats['failed']}\n"
                     f"Отменено: {stats['cancelled']}{error_note}",
                     kb(*actions, ("⬅️ К рассылкам", "adminbroadcast:list")))

    @router.callback_query(F.data.startswith("admincancel:"))
    async def admin_broadcast_cancel_prompt(call: CallbackQuery):
        campaign_id = int(call.data.split(":")[1])
        try:
            stats = service.broadcast_stats(call.from_user.id, campaign_id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer()
        await screen(call.message, f"✖ Отменить {stats['pending']} ожидающих сообщений рассылки #{campaign_id}?",
                     kb(("✅ Подтвердить отмену", f"admincancelconfirm:{campaign_id}"),
                        ("⬅️ К рассылке", f"adminstats:{campaign_id}")))

    @router.callback_query(F.data.startswith("admincancelconfirm:"))
    async def admin_broadcast_cancel(call: CallbackQuery):
        campaign_id = int(call.data.split(":")[1])
        try:
            count = service.cancel_broadcast(call.from_user.id, campaign_id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer("Отменено")
        await screen(call.message, f"✖ Отменено {count} ожидающих сообщений.",
                     kb(("📊 Статус", f"adminstats:{campaign_id}"),
                        ("⬅️ Главное меню", "home")))

    @router.callback_query(F.data.startswith("adminretry:"))
    async def admin_retry_prompt(call: CallbackQuery):
        campaign_id = int(call.data.split(":")[1])
        try:
            stats = service.broadcast_stats(call.from_user.id, campaign_id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer()
        await screen(call.message, f"🔁 Повторить временные ошибки рассылки #{campaign_id}?\n\n"
                     f"Неудачных отправок: {stats['failed']}. Блокировки Telegram повторяться не будут.",
                     kb(("✅ Подтвердить повтор", f"adminretryconfirm:{campaign_id}"),
                        ("⬅️ К рассылке", f"adminstats:{campaign_id}")))

    @router.callback_query(F.data.startswith("adminretryconfirm:"))
    async def admin_retry_confirm(call: CallbackQuery):
        campaign_id = int(call.data.split(":")[1])
        try:
            count = service.retry_failed_broadcast(call.from_user.id, campaign_id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer("Поставлено в очередь")
        await screen(call.message, f"🔁 Повторно поставлено в очередь: {count}.",
                     kb(("📊 Статус", f"adminstats:{campaign_id}"),
                        ("⬅️ Главное меню", "home")))

    @router.message(Command("help"))
    async def help_(message: Message):
        await screen(message, "🎄 Создайте игру через /start, поделитесь приглашением, заполните пожелания "
                     "и проведите жеребьёвку. Вопросы по конкретной игре задавайте её ведущему.")

    @router.message(Command("reminders"))
    async def retired_reminders(message: Message):
        await screen(message, "Настройки напоминаний удалены. Откройте /start для главного меню.")

    @router.callback_query(F.data.startswith("editgame:"))
    async def edit_game_prompt(call: CallbackQuery, state: FSMContext):
        _, game_id_text, field = call.data.split(":")
        game_id = int(game_id_text)
        game_ = service.game(game_id)
        if (game_ is None or game_.host_id != call.from_user.id or
                game_.status not in {"registration", "draft"} or
                field not in {*GAME_TEXT_FIELDS, "cover_file_id"}):
            await call.answer("Настройки недоступны", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id, field=field)
        await state.set_state(Form.edit_game)
        if field == "cover_file_id":
            prompt = "Отправьте фотографию или «-» для удаления обложки"
        else:
            label, limit = GAME_TEXT_FIELDS[field]
            prompt = f"Отправьте {label} одним сообщением (до {limit} символов); для очистки — «-»"
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
        parts = call.data.split(":")
        game_id = int(parts[1])
        people_page = max(0, int(parts[2])) if len(parts) > 2 else 0
        exclusion_page = max(0, int(parts[3])) if len(parts) > 3 else 0
        try:
            people = service.members(game_id, call.from_user.id)
            existing = service.exclusions(game_id, call.from_user.id)
            names = {p.user_id: p for p in people}
            lines = [f"{index}. {profile_link(p.user_id, p.nickname[:30])}"
                     for index, p in enumerate(page_slice(people, people_page), people_page * 8 + 1)]
            excluded = [f"{index}. {profile_link(names[e.giver_id].user_id, names[e.giver_id].nickname[:30])}"
                        f" → {profile_link(names[e.receiver_id].user_id, names[e.receiver_id].nickname[:30])}"
                        for index, e in enumerate(page_slice(existing, exclusion_page, 4), exclusion_page * 4 + 1)
                        if e.giver_id in names and e.receiver_id in names]
            buttons = [(f"➕ №{index}", f"exgiver:{game_id}:{p.user_id}")
                       for index, p in enumerate(page_slice(people, people_page), people_page * 8 + 1)]
            if people_page > 0:
                buttons.append(("◀️ Участники", f"exclusions:{game_id}:{people_page - 1}:{exclusion_page}"))
            if (people_page + 1) * 8 < len(people):
                buttons.append(("Участники ▶️", f"exclusions:{game_id}:{people_page + 1}:{exclusion_page}"))
            buttons += [(f"✖ №{index}", f"exremove:{game_id}:{e.giver_id}:{e.receiver_id}")
                        for index, e in enumerate(page_slice(existing, exclusion_page, 4), exclusion_page * 4 + 1)]
            if exclusion_page > 0:
                buttons.append(("◀️ Запреты", f"exclusions:{game_id}:{people_page}:{exclusion_page - 1}"))
            if (exclusion_page + 1) * 4 < len(existing):
                buttons.append(("Запреты ▶️", f"exclusions:{game_id}:{people_page}:{exclusion_page + 1}"))
            await call.answer()
            await screen(call.message, "🚫 <b>Запреты направленных пар</b>\n\n"
                         f"<b>Выберите дарителя ({len(people)}):</b>\n" + ("\n".join(lines) or "Участников нет.")
                         + f"\n\n<b>Текущие запреты ({len(existing)}):</b>\n" + ("\n".join(excluded) or "Нет."),
                         kb(*buttons, ("⬅️ К настройкам", f"settings:{game_id}")))
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("exgiver:"))
    async def ex_giver(call: CallbackQuery):
        parts = call.data.split(":")
        _, game_text, giver_text = parts[:3]
        game_id, giver_id = int(game_text), int(giver_text)
        page = max(0, int(parts[3])) if len(parts) > 3 else 0
        try:
            people = service.members(game_id, call.from_user.id)
            if giver_id not in {p.user_id for p in people}:
                raise ValueError("Участник не найден")
            await call.answer()
            candidates = [p for p in people if p.user_id != giver_id]
            await screen(call.message, "Кому нельзя дарить подарок?\n\n" +
                         "\n".join(f"{i}. {profile_link(p.user_id, p.nickname[:65])}"
                                   for i, p in enumerate(page_slice(candidates, page), page * 8 + 1)),
                         kb(*[(f"Выбрать №{i}", f"exadd:{game_id}:{giver_id}:{p.user_id}")
                              for i, p in enumerate(page_slice(candidates, page), page * 8 + 1)],
                            *page_buttons(f"exgiver:{game_id}:{giver_id}", page, len(candidates)),
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
                        *[(f"🔕 Стоп #{q.id}", f"stopquestion:{q.id}") for q in incoming[:10]],
                        *[(f"⚠️ Жалоба #{q.id}", f"reportquestion:{q.id}") for q in incoming[:10]],
                        ("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("stopquestion:") | F.data.startswith("reportquestion:"))
    async def stop_question(call: CallbackQuery):
        action, question_text = call.data.split(":")
        try:
            service.stop_questions(int(question_text), call.from_user.id,
                                   report=action == "reportquestion")
            await call.answer("Диалог закрыт")
            await screen(call.message, "🔕 Отправитель больше не сможет писать вам анонимно "
                         "в этой игре. Жалоба передана ведущему." if action == "reportquestion"
                         else "🔕 Анонимный диалог закрыт.")
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("reports:"))
    async def reports(call: CallbackQuery):
        game_id = int(call.data.split(":")[1])
        try:
            items = service.reported_questions(game_id, call.from_user.id)
        except ValueError as exc:
            await call.answer(str(exc), show_alert=True)
            return
        await call.answer()
        await screen(call.message, "⚠️ <b>Жалобы на вопросы</b>\n\n" +
                     ("\n\n".join(f"#{item_id}: {limited_html(content, 300)}"
                                  for item_id, content in items[-8:]) or "Жалоб нет."),
                     kb(("⬅️ К игре", f"game:{game_id}")))

    @router.callback_query(F.data.startswith("ask:"))
    async def ask_prompt(call: CallbackQuery, state: FSMContext):
        game_id = int(call.data.split(":")[1])
        if service.assignment(game_id, call.from_user.id) is None:
            await call.answer("Нет назначения", show_alert=True)
            return
        await call.answer()
        await state.update_data(game_id=game_id)
        await state.set_state(Form.ask)
        panel = await screen(call.message, "🎭 Напишите вопрос получателю (до 1000 символов). "
                             "Ваше имя не будет показано.",
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
        panel = await screen(call.message, "🎭 Напишите ответ на вопрос (до 1000 символов).")
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
        parts = call.data.split(":")
        game_id = int(parts[1])
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
        try:
            people = service.members(game_id, call.from_user.id)
            await call.answer()
            candidates = [p for p in people if p.user_id != call.from_user.id]
            await screen(call.message, "👑 Выберите нового ведущего:\n\n" +
                         "\n".join(f"{i}. {profile_link(p.user_id, p.nickname[:65])}"
                                   for i, p in enumerate(page_slice(candidates, page), page * 8 + 1)),
                         kb(*[(f"Выбрать №{i}", f"sethost:{game_id}:{p.user_id}")
                              for i, p in enumerate(page_slice(candidates, page), page * 8 + 1)],
                            *page_buttons(f"transfer:{game_id}", page, len(candidates)),
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

    retired_callbacks = {"personal", "daily", "marketing", "copygame", "mood", "advent",
                         "adventtoggle", "prediction", "quiz", "teampolicy", "setpolicy",
                         "memberteam", "announce", "annnow", "annlater", "annteams",
                         "annteam", "annsegment", "annsend", "adminsegment"}

    @router.callback_query(lambda call: bool(call.data) and
                           call.data.split(":", 1)[0] in retired_callbacks)
    async def retired_callback(call: CallbackQuery):
        await call.answer("Эта функция удалена. Откройте /start.", show_alert=True)

    return router
