import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from santa.draw import draw
from santa.models import (
    Assignment,
    Base,
    Delivery,
    Exclusion,
    Game,
    Member,
    Mode,
    Question,
    ReminderPreference,
    User,
    utcnow,
)


class AccessDenied(ValueError):
    pass


class InvalidState(ValueError):
    pass


class Service:
    def __init__(self, url: str, owner_ids: frozenset[int] = frozenset()):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.owner_ids = owner_ids

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        # Upgrade databases created by earlier releases without dropping game data.
        with self.engine.begin() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("games")}
            for name, sql_type in (("rules", "TEXT"), ("cover_file_id", "VARCHAR(255)")):
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE games ADD COLUMN {name} {sql_type} DEFAULT '' NOT NULL"))
            columns = {column["name"] for column in inspect(connection).get_columns("members")}
            if "received" not in columns:
                connection.execute(text("ALTER TABLE members ADD COLUMN received BOOLEAN DEFAULT FALSE NOT NULL"))

    def ensure_user(self, user_id: int, name: str) -> None:
        with self.sessions.begin() as db:
            self.user(db, user_id, name)

    @staticmethod
    def user(db: Session, user_id: int, name: str) -> User:
        user = db.get(User, user_id)
        if user is None:
            user = User(id=user_id, name=name[:120])
            db.add(user)
            db.flush()
        elif name and user.name != name[:120]:
            user.name = name[:120]
        return user

    def create_game(self, host_id: int, host_name: str, title: str, mode: Mode,
                    budget: str = "По договорённости", description: str = "",
                    registration_deadline: datetime | None = None,
                    gift_deadline: datetime | None = None,
                    celebration_at: datetime | None = None,
                    timezone: str = "server") -> Game:
        if not title.strip():
            raise ValueError("Укажите название игры")
        dates = [d for d in (registration_deadline, gift_deadline, celebration_at) if d]
        if dates != sorted(dates):
            raise ValueError("Даты должны идти по порядку")
        with self.sessions.begin() as db:
            self.user(db, host_id, host_name)
            game = Game(host_id=host_id, title=title.strip()[:120], description=description[:2000],
                        mode=mode.value, budget=budget[:80], token=secrets.token_urlsafe(24),
                        registration_deadline=registration_deadline, gift_deadline=gift_deadline,
                        celebration_at=celebration_at, timezone=timezone)
            db.add(game)
            db.flush()
            if mode != Mode.HOST:
                db.add(Member(game_id=game.id, user_id=host_id, nickname=host_name[:120]))
            return game

    def game_by_token(self, token: str) -> Game | None:
        with self.sessions() as db:
            return db.scalar(select(Game).where(Game.token == token))

    def game(self, game_id: int) -> Game | None:
        with self.sessions() as db:
            return db.get(Game, game_id)

    def my_games(self, user_id: int) -> list[Game]:
        with self.sessions() as db:
            return list(db.scalars(select(Game).outerjoin(Member, Member.game_id == Game.id)
                                   .where((Game.host_id == user_id) | (Member.user_id == user_id))
                                   .distinct().order_by(Game.id.desc())))

    def members(self, game_id: int, viewer_id: int) -> list[Member]:
        with self.sessions() as db:
            game = self._game(db, game_id)
            self._require_host(game, viewer_id)
            return list(db.scalars(select(Member).where(Member.game_id == game_id).order_by(Member.id)))

    def join(self, game_id: int, user_id: int, name: str) -> Member:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            if game.status != "registration" or (game.registration_deadline and game.registration_deadline < utcnow()):
                raise InvalidState("Регистрация в эту игру закрыта")
            if game.host_id == user_id and game.mode == Mode.HOST.value:
                raise InvalidState("В этом режиме ведущий не участвует")
            self.user(db, user_id, name)
            member = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))
            if member is None:
                member = Member(game_id=game_id, user_id=user_id, nickname=name[:120])
                db.add(member)
                db.flush()
            return member

    def member(self, game_id: int, user_id: int) -> Member | None:
        with self.sessions() as db:
            return db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))

    def profile(self, game_id: int, user_id: int, **fields: str) -> None:
        allowed = {"nickname", "wishes", "interests", "avoid", "wishlist_url"}
        if not fields.keys() <= allowed:
            raise ValueError("Недопустимое поле анкеты")
        with self.sessions.begin() as db:
            member = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))
            if member is None:
                raise AccessDenied("Вы не участвуете в этой игре")
            for key, value in fields.items():
                if len(value) > (500 if key == "wishlist_url" else 2000):
                    raise ValueError("Текст слишком длинный")
                setattr(member, key, value.strip())

    def set_mode(self, game_id: int, host_id: int, mode: Mode) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("После жеребьёвки режим менять нельзя")
            existing = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == host_id))
            if mode == Mode.HOST and existing:
                db.delete(existing)
            elif mode != Mode.HOST and existing is None:
                host = db.get(User, host_id)
                db.add(Member(game_id=game_id, user_id=host_id, nickname=host.name))
            game.mode = mode.value

    def set_budget(self, game_id: int, host_id: int, budget: str) -> None:
        if not budget.strip() or len(budget) > 80:
            raise ValueError("Бюджет должен быть от 1 до 80 символов")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("После жеребьёвки бюджет менять нельзя")
            game.budget = budget.strip()

    def edit_game(self, game_id: int, host_id: int, **fields: str) -> None:
        limits = {"title": 120, "description": 2000, "rules": 3000, "cover_file_id": 255}
        if not fields or not fields.keys() <= limits.keys():
            raise ValueError("Недопустимое поле игры")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("Редактирование доступно до жеребьёвки")
            for key, value in fields.items():
                value = value.strip()
                if len(value) > limits[key] or (key == "title" and not value):
                    raise ValueError("Проверьте длину текста")
                setattr(game, key, value)

    def remove_exclusion(self, game_id: int, host_id: int, giver_id: int, receiver_id: int) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("После жеребьёвки ограничения менять нельзя")
            exclusion = db.scalar(select(Exclusion).where(Exclusion.game_id == game_id,
                Exclusion.giver_id == giver_id, Exclusion.receiver_id == receiver_id))
            if exclusion:
                db.delete(exclusion)

    def exclusions(self, game_id: int, host_id: int) -> list[Exclusion]:
        with self.sessions() as db:
            self._require_host(self._game(db, game_id), host_id)
            return list(db.scalars(select(Exclusion).where(Exclusion.game_id == game_id)))

    def close_game(self, game_id: int, host_id: int) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "drawn":
                raise InvalidState("Завершить можно только игру после жеребьёвки")
            game.status = "finished"

    def archive_game(self, game_id: int, host_id: int) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "finished":
                raise InvalidState("Сначала завершите игру")
            game.status = "archived"

    def transfer_host(self, game_id: int, host_id: int, new_host_id: int) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("Передача роли доступна до жеребьёвки")
            if not db.scalar(select(Member.id).where(Member.game_id == game_id,
                                                       Member.user_id == new_host_id)):
                raise ValueError("Новый ведущий должен участвовать в игре")
            if game.mode == Mode.HOST.value:
                raise InvalidState("В режиме организатора смените режим перед передачей")
            game.host_id = new_host_id

    def set_marketing(self, user_id: int, enabled: bool) -> None:
        with self.sessions.begin() as db:
            user = db.get(User, user_id)
            if user is None:
                raise AccessDenied("Сначала откройте /start")
            user.marketing_opt_in = enabled

    def marketing_enabled(self, user_id: int) -> bool:
        with self.sessions() as db:
            user = db.get(User, user_id)
            return bool(user and user.marketing_opt_in)

    def ask_receiver(self, game_id: int, giver_id: int, question: str) -> int:
        question = question.strip()
        if not 1 <= len(question) <= 1000:
            raise ValueError("Вопрос должен быть от 1 до 1000 символов")
        with self.sessions.begin() as db:
            game = self._game(db, game_id)
            if game.status != "drawn":
                raise InvalidState("Вопросы доступны после жеребьёвки")
            assignment = db.scalar(select(Assignment).where(Assignment.game_id == game_id,
                                                            Assignment.giver_id == giver_id))
            if assignment is None:
                raise AccessDenied("Нет назначения в этой игре")
            item = Question(game_id=game_id, giver_id=giver_id,
                            receiver_id=assignment.receiver_id, text=question)
            db.add(item)
            db.flush()
            db.add(Delivery(key=f"question:{item.id}", user_id=item.receiver_id,
                            text=f"🎭 Анонимный вопрос по игре «{game.title}»:\n{question}\n\nОтветьте через /start → игра → Вопросы."))
            return item.id

    def incoming_questions(self, game_id: int, user_id: int) -> list[Question]:
        with self.sessions() as db:
            return list(db.scalars(select(Question).where(Question.game_id == game_id,
                        Question.receiver_id == user_id, Question.answer == "").order_by(Question.id)))

    def answer_question(self, question_id: int, user_id: int, answer: str) -> None:
        answer = answer.strip()
        if not 1 <= len(answer) <= 1000:
            raise ValueError("Ответ должен быть от 1 до 1000 символов")
        with self.sessions.begin() as db:
            item = db.get(Question, question_id)
            if item is None or item.receiver_id != user_id:
                raise AccessDenied("Вопрос не найден")
            if item.answer:
                raise InvalidState("Ответ уже отправлен")
            item.answer = answer
            game = self._game(db, item.game_id)
            db.add(Delivery(key=f"answer:{item.id}", user_id=item.giver_id,
                            text=f"🎭 Ответ получателя в игре «{game.title}»:\n{answer}"))

    def confirm_received(self, game_id: int, user_id: int) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id)
            member = db.scalar(select(Member).where(Member.game_id == game_id,
                                                     Member.user_id == user_id))
            if member is None or game.status not in {"drawn", "finished"}:
                raise AccessDenied("Подтверждение недоступно")
            member.received = True

    def set_reminders(self, user_id: int, enabled: bool) -> None:
        with self.sessions.begin() as db:
            user = db.get(User, user_id)
            if user is None:
                raise AccessDenied("Сначала откройте /start")
            user.reminders_opt_in = enabled
            if not enabled:
                for job in db.scalars(select(Delivery).where(
                    Delivery.user_id == user_id, Delivery.state == "pending",
                    (Delivery.key.like("reminder:%")) | (Delivery.key.like("daily:%")))):
                    job.state = "cancelled"

    def daily_reminders_enabled(self, user_id: int) -> bool:
        with self.sessions() as db:
            preference = db.get(ReminderPreference, user_id)
            return bool(preference and preference.daily_gift)

    def set_daily_reminders(self, user_id: int, enabled: bool) -> None:
        with self.sessions.begin() as db:
            user = db.get(User, user_id)
            if user is None:
                raise AccessDenied("Сначала откройте /start")
            preference = db.get(ReminderPreference, user_id)
            if preference is None:
                preference = ReminderPreference(user_id=user_id)
                db.add(preference)
            preference.daily_gift = enabled
            if enabled:
                user.reminders_opt_in = True
            else:
                for job in db.scalars(select(Delivery).where(
                    Delivery.user_id == user_id, Delivery.state == "pending",
                    Delivery.key.like("daily:%"))):
                    job.state = "cancelled"

    def add_exclusion(self, game_id: int, host_id: int, giver_id: int, receiver_id: int) -> None:
        if giver_id == receiver_id:
            raise ValueError("Нельзя запретить пару участника с самим собой")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("После жеребьёвки ограничения менять нельзя")
            ids = set(db.scalars(select(Member.user_id).where(Member.game_id == game_id)))
            if giver_id not in ids or receiver_id not in ids:
                raise ValueError("Оба участника должны быть в игре")
            if not db.scalar(select(Exclusion).where(Exclusion.game_id == game_id,
                                                     Exclusion.giver_id == giver_id,
                                                     Exclusion.receiver_id == receiver_id)):
                db.add(Exclusion(game_id=game_id, giver_id=giver_id, receiver_id=receiver_id))

    def run_draw(self, game_id: int, host_id: int) -> dict[int, int]:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "registration":
                raise InvalidState("Жеребьёвка уже проведена или регистрация закрыта")
            members = list(db.scalars(select(Member).where(Member.game_id == game_id)))
            exclusions = set(db.execute(select(Exclusion.giver_id, Exclusion.receiver_id)
                                        .where(Exclusion.game_id == game_id)).all())
            result = draw([m.user_id for m in members], exclusions)
            by_id = {m.user_id: m for m in members}
            for giver, receiver in result.items():
                db.add(Assignment(game_id=game_id, giver_id=giver, receiver_id=receiver))
                target = by_id[receiver]
                db.add(Delivery(key=f"draw:{game_id}:{giver}", user_id=giver,
                                text=f"🎁 Твоя миссия в игре «{game.title}»: подарок для {target.nickname}.\n"
                                     f"Пожелания: {target.wishes or 'пока не заполнены'}\n"
                                     f"Не дарить: {target.avoid or 'не указано'}\nБюджет: {game.budget}"))
            game.status = "drawn"
            return result

    def assignment(self, game_id: int, user_id: int) -> tuple[Member, Game] | None:
        with self.sessions() as db:
            assignment = db.scalar(select(Assignment).where(Assignment.game_id == game_id,
                                                             Assignment.giver_id == user_id))
            if assignment is None:
                return None
            receiver = db.scalar(select(Member).where(Member.game_id == game_id,
                                                       Member.user_id == assignment.receiver_id))
            return receiver, self._game(db, game_id)

    def all_assignments(self, game_id: int, viewer_id: int) -> list[tuple[str, str]]:
        with self.sessions() as db:
            game = self._game(db, game_id)
            self._require_host(game, viewer_id)
            if game.mode == Mode.BLIND.value:
                raise AccessDenied("В режиме «Играю вслепую» общая таблица скрыта")
            names = {m.user_id: m.nickname for m in db.scalars(select(Member).where(Member.game_id == game_id))}
            return [(names[a.giver_id], names[a.receiver_id]) for a in
                    db.scalars(select(Assignment).where(Assignment.game_id == game_id))]

    def set_gift_status(self, game_id: int, user_id: int, status: str) -> None:
        if status not in {"choosing", "bought", "ready", "sent", "given"}:
            raise ValueError("Неизвестный статус")
        with self.sessions.begin() as db:
            member = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))
            if member is None or self._game(db, game_id).status != "drawn":
                raise AccessDenied("Нет активного назначения")
            member.gift_status = status

    def progress(self, game_id: int, host_id: int) -> tuple[int, int]:
        with self.sessions() as db:
            self._require_host(self._game(db, game_id), host_id)
            total = db.scalar(select(func.count()).select_from(Member).where(Member.game_id == game_id))
            ready = db.scalar(select(func.count()).select_from(Member).where(
                Member.game_id == game_id, Member.gift_status.in_(["ready", "sent", "given"])))
            return total or 0, ready or 0

    def announce(self, game_id: int, host_id: int, text: str,
                 due_at: datetime | None = None) -> int:
        if not text.strip() or len(text) > 3000:
            raise ValueError("Сообщение должно быть от 1 до 3000 символов")
        if due_at is not None and due_at <= utcnow():
            raise ValueError("Время отправки должно быть в будущем")
        with self.sessions.begin() as db:
            game = self._game(db, game_id)
            self._require_host(game, host_id)
            members = list(db.scalars(select(Member).where(Member.game_id == game_id)))
            batch = secrets.token_hex(8)
            for member in members:
                db.add(Delivery(key=f"announcement:{game_id}:{batch}:{member.user_id}",
                                user_id=member.user_id, text=f"📣 {game.title}\n{text.strip()}",
                                due_at=due_at or utcnow()))
            return len(members)

    def schedule_reminders(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        local_now = now.replace(tzinfo=UTC).astimezone()
        count = 0
        with self.sessions.begin() as db:
            games = db.scalars(select(Game).where(Game.reminders_enabled.is_(True),
                                                   Game.status.in_(["registration", "drawn"]))).all()
            for game in games:
                events = [("registration", game.registration_deadline, "⏰ Регистрация скоро закроется"),
                          ("gift", game.gift_deadline, "🎁 Скоро срок подготовки подарка"),
                          ("celebration", game.celebration_at, "🎄 Скоро вручение подарков")]
                for kind, deadline, message in events:
                    if not deadline or not deadline - timedelta(days=1) <= now < deadline:
                        continue
                    if kind == "registration" and game.status != "registration":
                        continue
                    for user_id in db.scalars(select(Member.user_id).where(Member.game_id == game.id)):
                        user = db.get(User, user_id)
                        key = f"reminder:{game.id}:{kind}:{user_id}"
                        if user.reminders_opt_in and not db.scalar(select(Delivery.id).where(Delivery.key == key)):
                            db.add(Delivery(key=key, user_id=user_id, text=f"{message} в игре «{game.title}»."))
                            count += 1
                if game.status != "drawn" or not game.gift_deadline or game.gift_deadline <= now:
                    continue
                if local_now.hour < 9:
                    continue
                for member in db.scalars(select(Member).where(Member.game_id == game.id)):
                    if member.gift_status in {"ready", "sent", "given"}:
                        continue
                    user = db.get(User, member.user_id)
                    preference = db.get(ReminderPreference, member.user_id)
                    if not user.reminders_opt_in or not preference or not preference.daily_gift:
                        continue
                    key = f"daily:{game.id}:{local_now:%Y-%m-%d}:{member.user_id}"
                    if not db.scalar(select(Delivery.id).where(Delivery.key == key)):
                        db.add(Delivery(key=key, user_id=member.user_id,
                                        text=f"🎁 Напоминание: подготовьте подарок для игры «{game.title}»."))
                        count += 1
        return count

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    def owner_stats(self, user_id: int) -> dict[str, int]:
        if not self.is_owner(user_id):
            raise AccessDenied("Доступ закрыт")
        with self.sessions() as db:
            return {
                "users": db.scalar(select(func.count()).select_from(User)) or 0,
                "games": db.scalar(select(func.count()).select_from(Game)) or 0,
                "active": db.scalar(select(func.count()).select_from(Game).where(
                    Game.status.in_(["registration", "drawn"]))) or 0,
                "deliveries_pending": db.scalar(select(func.count()).select_from(Delivery).where(
                    Delivery.state == "pending")) or 0,
            }

    @staticmethod
    def _game(db: Session, game_id: int, lock: bool = False) -> Game:
        statement = select(Game).where(Game.id == game_id)
        game = db.scalar(statement.with_for_update() if lock else statement)
        if game is None:
            raise ValueError("Игра не найдена")
        return game

    @staticmethod
    def _require_host(game: Game, user_id: int) -> None:
        if game.host_id != user_id:
            raise AccessDenied("Только ведущий этой игры может выполнить действие")
