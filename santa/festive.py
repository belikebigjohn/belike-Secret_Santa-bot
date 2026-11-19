"""Small optional activities that do not touch assignments."""

from datetime import datetime
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")

ADVENT = (
    "Напиши короткое доброе сообщение человеку, с которым давно не общался.",
    "Загадка: что становится больше, если его перевернуть? Ответ: число 6.",
    "Составь список трёх маленьких радостей этой недели.",
    "Выбери праздничный фильм и пригласи кого-нибудь посмотреть вместе.",
    "Сделай комплимент коллеге за конкретное дело.",
    "Устрой вечер без спешки: чай, музыка и 15 минут для себя.",
    "Подготовь открытку для человека, которому хочется сказать спасибо.",
    "Поделись любимым зимним рецептом с другом.",
    "Загадка: без рук, без ног, а рисовать умеет. Ответ: мороз.",
    "Помоги кому-нибудь с небольшой задачей без просьбы о помощи.",
    "Вспомни лучший момент уходящего года и запиши его.",
    "Укрась рабочее место одной праздничной деталью.",
)

PREDICTIONS = (
    "В этом сезоне тебя ждёт приятная встреча.",
    "Неожиданная идея превратится в хороший подарок.",
    "Один тёплый разговор запомнится надолго.",
    "Небольшая забота о другом вернётся улыбкой.",
    "Праздничный день подарит повод для новой традиции.",
)

QUIZ = (
    ("Какой месяц в России начинает календарную зиму?", ("Декабрь", "Январь", "Ноябрь"), 0),
    ("Сколько лучей обычно изображают у снежинки?", ("Пять", "Шесть", "Восемь"), 1),
    ("Что принято вешать на дверь как праздничное украшение?", ("Гирю", "Венок", "Флаг"), 1),
)


def days_until(moment: datetime | None, now: datetime | None = None) -> int | None:
    if moment is None:
        return None
    today = (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date()
    target = moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(MOSCOW).date()
    return max(0, (target - today).days)


def new_year_days(now: datetime | None = None) -> int:
    local = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    year = local.year if local.month == 1 and local.day == 1 else local.year + 1
    return (datetime(year, 1, 1, tzinfo=MOSCOW).date() - local.date()).days


def advent_card(game_id: int, now: datetime | None = None) -> str:
    local = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    index = (local.toordinal() + game_id) % len(ADVENT)
    return ADVENT[index]
