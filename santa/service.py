import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, delete, func, inspect, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from santa.draw import draw
from santa.formatting import limited_html, profile_link
from santa.models import (
    Assignment,
    Base,
    Delivery,
    Exclusion,
    Game,
    GameHelper,
    Member,
    Mode,
    Question,
    ReminderPreference,
    ReplacementInvite,
    User,
    utcnow,
)


class AccessDenied(ValueError):
    pass


class InvalidState(ValueError):
    pass


class Service:
    DATE_FIELDS = ("registration_start", "registration_deadline", "draw_at",
                   "wishes_deadline", "gift_deadline", "shipping_deadline", "celebration_at")
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
            for name in ("registration_start", "draw_at", "wishes_deadline", "shipping_deadline"):
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE games ADD COLUMN {name} TIMESTAMP"))
            if "max_members" not in columns:
                connection.execute(text("ALTER TABLE games ADD COLUMN max_members INTEGER"))
            if "approval_required" not in columns:
                connection.execute(text("ALTER TABLE games ADD COLUMN approval_required BOOLEAN DEFAULT FALSE NOT NULL"))
            if "team_policy" not in columns:
                connection.execute(text("ALTER TABLE games ADD COLUMN team_policy VARCHAR(12) DEFAULT 'mixed' NOT NULL"))
            columns = {column["name"] for column in inspect(connection).get_columns("members")}
            if "received" not in columns:
                connection.execute(text("ALTER TABLE members ADD COLUMN received BOOLEAN DEFAULT FALSE NOT NULL"))
            if "team" not in columns:
                connection.execute(text("ALTER TABLE members ADD COLUMN team VARCHAR(80) DEFAULT '' NOT NULL"))
            if "approved" not in columns:
                connection.execute(text("ALTER TABLE members ADD COLUMN approved BOOLEAN DEFAULT TRUE NOT NULL"))
            columns = {column["name"] for column in inspect(connection).get_columns("deliveries")}
            if "parse_mode" not in columns:
                connection.execute(text("ALTER TABLE deliveries ADD COLUMN parse_mode VARCHAR(16) DEFAULT '' NOT NULL"))

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
                    registration_start: datetime | None = None,
                    registration_deadline: datetime | None = None,
                    draw_at: datetime | None = None,
                    wishes_deadline: datetime | None = None,
                    gift_deadline: datetime | None = None,
                    shipping_deadline: datetime | None = None,
                    celebration_at: datetime | None = None,
                    timezone: str = "Europe/Moscow", max_members: int | None = None,
                    approval_required: bool = False, team_policy: str = "mixed",
                    draft: bool = False) -> Game:
        if not title.strip():
            raise ValueError("Укажите название игры")
        date_values = {"registration_start": registration_start,
                       "registration_deadline": registration_deadline, "draw_at": draw_at,
                       "wishes_deadline": wishes_deadline, "gift_deadline": gift_deadline,
                       "shipping_deadline": shipping_deadline, "celebration_at": celebration_at}
        self._validate_dates(date_values)
        if max_members is not None and max_members < (1 if mode == Mode.HOST else 2):
            raise ValueError("Слишком маленький лимит участников")
        if team_policy not in {"mixed", "within", "cross"}:
            raise ValueError("Неизвестное правило команд")
        with self.sessions.begin() as db:
            self.user(db, host_id, host_name)
            game = Game(host_id=host_id, title=title.strip()[:120], description=description[:2000],
                        mode=mode.value, budget=budget[:80], token=secrets.token_urlsafe(24),
                        timezone=timezone, max_members=max_members,
                        approval_required=approval_required, team_policy=team_policy,
                        status="draft" if draft else "registration",
                        **date_values)
            db.add(game)
            db.flush()
            if mode != Mode.HOST:
                db.add(Member(game_id=game.id, user_id=host_id, nickname=host_name[:120]))
            return game

    def game_by_token(self, token: str) -> Game | None:
        with self.sessions() as db:
            return db.scalar(select(Game).where(Game.token == token, Game.status != "draft"))

    def clone_game(self, game_id: int, host_id: int) -> Game:
        with self.sessions() as db:
            source = self._game(db, game_id)
            self._require_host(source, host_id)
            host = db.get(User, host_id)
            values = (source.title, Mode(source.mode), source.budget, source.description,
                      source.rules, source.cover_file_id, source.max_members,
                      source.approval_required, source.team_policy, host.name)
        title, mode, budget, description, rules, cover, maximum, approval, policy, host_name = values
        clone = self.create_game(host_id, host_name, title, mode, budget=budget,
                                 description=description, max_members=maximum,
                                 approval_required=approval, team_policy=policy, draft=True)
        self.edit_game(clone.id, host_id, rules=rules, cover_file_id=cover)
        return self.game(clone.id)

    def publish_game(self, game_id: int, host_id: int) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "draft":
                raise InvalidState("Игра уже опубликована")
            if not game.registration_deadline or not game.gift_deadline or not game.celebration_at:
                raise InvalidState("Укажите сроки регистрации, подарка и вручения")
            if game.registration_deadline <= utcnow():
                raise InvalidState("Срок регистрации уже прошёл")
            game.status = "registration"

    def game(self, game_id: int) -> Game | None:
        with self.sessions() as db:
            return db.get(Game, game_id)

    def game_view(self, game_id: int, viewer_id: int) -> tuple[Game, Member | None, int | None] | None:
        """Fetch a game card with its access check in one database session."""
        with self.sessions() as db:
            game = db.get(Game, game_id)
            if game is None:
                return None
            member = db.scalar(select(Member).where(Member.game_id == game_id,
                                                    Member.user_id == viewer_id))
            helper = db.scalar(select(GameHelper.id).where(GameHelper.game_id == game_id,
                                                            GameHelper.user_id == viewer_id))
            if game.host_id != viewer_id and member is None and helper is None:
                return None
            total = (db.scalar(select(func.count()).select_from(Member).where(
                Member.game_id == game_id)) or 0) if game.host_id == viewer_id or helper else None
            return game, member, total

    def my_games(self, user_id: int) -> list[Game]:
        with self.sessions() as db:
            return list(db.scalars(select(Game).outerjoin(Member, Member.game_id == Game.id)
                                   .outerjoin(GameHelper, GameHelper.game_id == Game.id)
                                   .where((Game.host_id == user_id) | (Member.user_id == user_id)
                                          | (GameHelper.user_id == user_id))
                                   .distinct().order_by(Game.id.desc())))

    def members(self, game_id: int, viewer_id: int) -> list[Member]:
        with self.sessions() as db:
            game = self._game(db, game_id)
            self._require_manager(db, game, viewer_id)
            return list(db.scalars(select(Member).where(Member.game_id == game_id).order_by(Member.id)))

    def helpers(self, game_id: int, host_id: int) -> list[GameHelper]:
        with self.sessions() as db:
            self._require_host(self._game(db, game_id), host_id)
            return list(db.scalars(select(GameHelper).where(GameHelper.game_id == game_id)))

    def is_helper(self, game_id: int, user_id: int) -> bool:
        with self.sessions() as db:
            return bool(db.scalar(select(GameHelper.id).where(GameHelper.game_id == game_id,
                                                          GameHelper.user_id == user_id)))

    def set_helper(self, game_id: int, host_id: int, user_id: int, enabled: bool) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if user_id == host_id:
                raise ValueError("Ведущий уже управляет игрой")
            if not db.scalar(select(Member.id).where(Member.game_id == game_id,
                                                       Member.user_id == user_id)):
                raise ValueError("Помощник должен участвовать в игре")
            helper = db.scalar(select(GameHelper).where(GameHelper.game_id == game_id,
                                                        GameHelper.user_id == user_id))
            if enabled and helper is None:
                db.add(GameHelper(game_id=game_id, user_id=user_id))
            elif not enabled and helper is not None:
                db.delete(helper)

    def join(self, game_id: int, user_id: int, name: str) -> Member:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            now = utcnow()
            if (game.status != "registration" or
                    (game.registration_start and game.registration_start > now) or
                    (game.registration_deadline and game.registration_deadline < now)):
                raise InvalidState("Регистрация в эту игру закрыта")
            if game.host_id == user_id and game.mode == Mode.HOST.value:
                raise InvalidState("В этом режиме ведущий не участвует")
            self.user(db, user_id, name)
            member = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))
            if member is None:
                count = db.scalar(select(func.count()).select_from(Member).where(Member.game_id == game_id)) or 0
                if game.max_members is not None and count >= game.max_members:
                    raise InvalidState("В игре больше нет свободных мест")
                member = Member(game_id=game_id, user_id=user_id, nickname=name[:120],
                                approved=not game.approval_required)
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
            if game.status not in {"draft", "registration"}:
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
            if game.status not in {"draft", "registration"}:
                raise InvalidState("После жеребьёвки бюджет менять нельзя")
            game.budget = budget.strip()

    def edit_game(self, game_id: int, host_id: int, **fields: str) -> None:
        limits = {"title": 120, "description": 2000, "rules": 3000, "cover_file_id": 255}
        if not fields or not fields.keys() <= limits.keys():
            raise ValueError("Недопустимое поле игры")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status not in {"draft", "registration"}:
                raise InvalidState("Редактирование доступно до жеребьёвки")
            for key, value in fields.items():
                value = value.strip()
                if len(value) > limits[key] or (key == "title" and not value):
                    raise ValueError("Проверьте длину текста")
                setattr(game, key, value)

    @classmethod
    def _validate_dates(cls, values: dict[str, datetime | None]) -> None:
        dates = [values.get(field) for field in cls.DATE_FIELDS if values.get(field) is not None]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("Даты игры должны идти по порядку")

    def set_game_date(self, game_id: int, host_id: int, field: str,
                      value: datetime | None) -> None:
        if field not in self.DATE_FIELDS:
            raise ValueError("Неизвестная дата игры")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status not in {"draft", "registration"}:
                raise InvalidState("После жеребьёвки даты менять нельзя")
            values = {name: getattr(game, name) for name in self.DATE_FIELDS}
            values[field] = value
            self._validate_dates(values)
            if field == "registration_deadline" and value and value <= utcnow():
                raise ValueError("Дедлайн регистрации должен быть в будущем")
            setattr(game, field, value)

    def set_max_members(self, game_id: int, host_id: int, maximum: int | None) -> None:
        if maximum is not None and not 2 <= maximum <= 1000:
            raise ValueError("Лимит должен быть от 2 до 1000")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status not in {"draft", "registration"}:
                raise InvalidState("После жеребьёвки лимит менять нельзя")
            count = db.scalar(select(func.count()).select_from(Member).where(Member.game_id == game_id)) or 0
            if maximum is not None and maximum < count:
                raise ValueError("Лимит меньше текущего числа участников")
            game.max_members = maximum

    def set_approval_required(self, game_id: int, host_id: int, enabled: bool) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status not in {"draft", "registration"}:
                raise InvalidState("После жеребьёвки настройку менять нельзя")
            game.approval_required = enabled

    def set_team_policy(self, game_id: int, host_id: int, policy: str) -> None:
        if policy not in {"mixed", "within", "cross"}:
            raise ValueError("Неизвестное правило команд")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status not in {"draft", "registration"}:
                raise InvalidState("После жеребьёвки настройку менять нельзя")
            game.team_policy = policy

    def set_member_team(self, game_id: int, host_id: int, user_id: int, team: str) -> None:
        if len(team.strip()) > 80:
            raise ValueError("Название команды слишком длинное")
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_manager(db, game, host_id)
            if game.status != "registration":
                raise InvalidState("После жеребьёвки команды менять нельзя")
            member = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))
            if member is None:
                raise ValueError("Участник не найден")
            member.team = team.strip()

    def review_member(self, game_id: int, host_id: int, user_id: int, approve: bool) -> None:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_manager(db, game, host_id)
            if game.status != "registration":
                raise InvalidState("Регистрация закрыта")
            member = db.scalar(select(Member).where(Member.game_id == game_id, Member.user_id == user_id))
            if member is None or member.approved:
                raise ValueError("Заявка не найдена")
            if approve:
                member.approved = True
            else:
                db.delete(member)
            db.add(Delivery(key=f"review:{game_id}:{user_id}:{secrets.token_hex(4)}",
                            user_id=user_id, text=("✅ Заявка одобрена" if approve else "❌ Заявка отклонена")
                            + f" в игре «{game.title}»."))

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
        """Permanently remove a finished game and its queued messages."""
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "finished":
                raise InvalidState("Сначала завершите игру")
            question_ids = list(db.scalars(select(Question.id).where(Question.game_id == game_id)))
            delivery_filters = [Delivery.key.like(f"{prefix}:{game_id}:%") for prefix in
                                ("draw", "announcement", "reminder", "daily", "review", "replacement")]
            for question_id in question_ids:
                delivery_filters.extend((Delivery.key == f"question:{question_id}",
                                         Delivery.key == f"answer:{question_id}"))
            db.execute(delete(Delivery).where(or_(*delivery_filters)))
            for model in (Question, Assignment, Exclusion, GameHelper, ReplacementInvite, Member):
                db.execute(delete(model).where(model.game_id == game_id))
            db.delete(game)

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

    def create_replacement_invite(self, game_id: int, host_id: int, leaver_id: int) -> str:
        with self.sessions.begin() as db:
            game = self._game(db, game_id, lock=True)
            self._require_host(game, host_id)
            if game.status != "drawn":
                raise InvalidState("Замена доступна после жеребьёвки")
            if leaver_id == host_id:
                raise InvalidState("Ведущего сначала нужно передать другому участнику")
            if not db.scalar(select(Member.id).where(Member.game_id == game_id,
                                                       Member.user_id == leaver_id)):
                raise ValueError("Участник не найден")
            token = secrets.token_urlsafe(24)
            db.add(ReplacementInvite(token=token, game_id=game_id, leaver_id=leaver_id))
            return token

    def replacement_invite(self, token: str) -> tuple[Game, Member] | None:
        with self.sessions() as db:
            invite = db.scalar(select(ReplacementInvite).where(ReplacementInvite.token == token,
                                                                 ReplacementInvite.state == "pending"))
            if invite is None or invite.created_at < utcnow() - timedelta(days=7):
                return None
            game = self._game(db, invite.game_id)
            leaver = db.scalar(select(Member).where(Member.game_id == game.id,
                                                     Member.user_id == invite.leaver_id))
            if game.status != "drawn" or leaver is None:
                return None
            return game, leaver

    def accept_replacement(self, token: str, user_id: int, name: str) -> int:
        with self.sessions.begin() as db:
            invite = db.scalar(select(ReplacementInvite).where(ReplacementInvite.token == token))
            if invite is None:
                raise ValueError("Приглашение не найдено")
            game = self._game(db, invite.game_id, lock=True)
            if (invite.state != "pending" or invite.created_at < utcnow() - timedelta(days=7)
                    or game.status != "drawn"):
                raise InvalidState("Приглашение больше не действует")
            if game.host_id == user_id and game.mode == Mode.HOST.value:
                raise InvalidState("В этом режиме ведущий не участвует")
            if db.scalar(select(Member.id).where(Member.game_id == game.id,
                                                  Member.user_id == user_id)):
                raise InvalidState("Вы уже участвуете в этой игре")
            leaver = db.scalar(select(Member).where(Member.game_id == game.id,
                                                     Member.user_id == invite.leaver_id))
            if leaver is None:
                raise InvalidState("Заменяемый участник уже выбыл")
            outgoing = db.scalar(select(Assignment).where(Assignment.game_id == game.id,
                                                           Assignment.giver_id == leaver.user_id))
            incoming = db.scalar(select(Assignment).where(Assignment.game_id == game.id,
                                                           Assignment.receiver_id == leaver.user_id))
            if outgoing is None or incoming is None:
                raise InvalidState("Назначения игры повреждены")
            target = db.scalar(select(Member).where(Member.game_id == game.id,
                                                     Member.user_id == outgoing.receiver_id))
            donor = db.scalar(select(Member).where(Member.game_id == game.id,
                                                    Member.user_id == incoming.giver_id))
            if target is None or donor is None:
                raise InvalidState("Участник назначения не найден")
            self.user(db, user_id, name)
            db.add(Member(game_id=game.id, user_id=user_id, nickname=name[:120],
                          team=leaver.team, approved=True))
            outgoing.giver_id = user_id
            incoming.receiver_id = user_id
            questions = list(db.scalars(select(Question).where(Question.game_id == game.id,
                (Question.giver_id == leaver.user_id) | (Question.receiver_id == leaver.user_id))))
            for question in questions:
                db.execute(delete(Delivery).where(Delivery.key.in_((f"question:{question.id}",
                                                                     f"answer:{question.id}"))))
                db.delete(question)
            db.execute(delete(Exclusion).where(Exclusion.game_id == game.id,
                (Exclusion.giver_id == leaver.user_id) | (Exclusion.receiver_id == leaver.user_id)))
            db.execute(delete(GameHelper).where(GameHelper.game_id == game.id,
                                                 GameHelper.user_id == leaver.user_id))
            for job in db.scalars(select(Delivery).where(Delivery.user_id == leaver.user_id,
                                                          Delivery.state == "pending")):
                if any(job.key.startswith(f"{prefix}:{game.id}:") for prefix in
                       ("draw", "announcement", "reminder", "daily")):
                    job.state = "cancelled"
            stale_donor_job = db.scalar(select(Delivery).where(
                Delivery.key == f"draw:{game.id}:{donor.user_id}", Delivery.state == "pending"))
            if stale_donor_job is not None:
                stale_donor_job.state = "cancelled"
            db.delete(leaver)
            for other in db.scalars(select(ReplacementInvite).where(
                    ReplacementInvite.game_id == game.id,
                    ReplacementInvite.leaver_id == invite.leaver_id,
                    ReplacementInvite.state == "pending")):
                other.state = "used" if other.id == invite.id else "cancelled"
            batch = secrets.token_hex(8)
            db.add(Delivery(key=f"replacement:{game.id}:{batch}:{user_id}", user_id=user_id,
                            text=f"🎁 Вы заменили участника в игре «{limited_html(game.title, 300)}». "
                                 f"Ваш получатель: {profile_link(target.user_id, target.nickname[:70])}.\n"
                                 f"Пожелания: {limited_html(target.wishes or 'не указаны', 1000)}",
                            parse_mode="HTML"))
            db.add(Delivery(key=f"replacement:{game.id}:{batch}:{donor.user_id}",
                            user_id=donor.user_id,
                            text=f"🔄 В игре «{limited_html(game.title, 300)}» назначение изменилось. "
                                 f"Теперь ваш получатель: {profile_link(user_id, name[:70])}. "
                                 "Предыдущее назначение больше не действует.", parse_mode="HTML"))
            db.add(Delivery(key=f"replacement:{game.id}:{batch}:{leaver.user_id}",
                            user_id=leaver.user_id,
                            text=f"🔄 Вы выбыли из игры «{game.title}». Старое назначение больше не действует."))
            return game.id

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
            if any(not member.approved for member in members):
                raise InvalidState("Сначала обработайте все заявки на участие")
            if game.team_policy != "mixed" and any(not member.team for member in members):
                raise InvalidState("Укажите команду каждому участнику")
            exclusions = set(db.execute(select(Exclusion.giver_id, Exclusion.receiver_id)
                                        .where(Exclusion.game_id == game_id)).all())
            if game.team_policy != "mixed":
                exclusions.update((giver.user_id, receiver.user_id)
                                  for giver in members for receiver in members
                                  if (giver.team != receiver.team) == (game.team_policy == "within"))
            result = draw([m.user_id for m in members], exclusions)
            by_id = {m.user_id: m for m in members}
            for giver, receiver in result.items():
                db.add(Assignment(game_id=game_id, giver_id=giver, receiver_id=receiver))
                target = by_id[receiver]
                db.add(Delivery(key=f"draw:{game_id}:{giver}", user_id=giver,
                                text=f"🎁 Твоя миссия в игре «{limited_html(game.title, 300)}»: подарок для "
                                     f"{profile_link(target.user_id, target.nickname[:70])}.\n"
                                     f"Пожелания: {limited_html(target.wishes or 'пока не заполнены', 1000)}\n"
                                     f"Не дарить: {limited_html(target.avoid or 'не указано', 1000)}\n"
                                     f"Бюджет: {limited_html(game.budget, 250)}", parse_mode="HTML"))
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

    def all_assignments(self, game_id: int, viewer_id: int) -> list[tuple[Member, Member]]:
        with self.sessions() as db:
            game = self._game(db, game_id)
            self._require_host(game, viewer_id)
            if game.mode == Mode.BLIND.value:
                raise AccessDenied("В режиме «Играю вслепую» общая таблица скрыта")
            members = {m.user_id: m for m in db.scalars(select(Member).where(Member.game_id == game_id))}
            return [(members[a.giver_id], members[a.receiver_id]) for a in
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
            self._require_manager(db, self._game(db, game_id), host_id)
            total = db.scalar(select(func.count()).select_from(Member).where(Member.game_id == game_id))
            ready = db.scalar(select(func.count()).select_from(Member).where(
                Member.game_id == game_id, Member.gift_status.in_(["ready", "sent", "given"])))
            return total or 0, ready or 0

    def teams(self, game_id: int, host_id: int) -> list[str]:
        with self.sessions() as db:
            self._require_manager(db, self._game(db, game_id), host_id)
            return list(db.scalars(select(Member.team).where(Member.game_id == game_id,
                                                               Member.team != "").distinct().order_by(Member.team)))

    def announce(self, game_id: int, host_id: int, text: str,
                 due_at: datetime | None = None, audience: str = "all",
                 team: str = "") -> int:
        if not text.strip() or len(text) > 3000:
            raise ValueError("Сообщение должно быть от 1 до 3000 символов")
        if due_at is not None and due_at <= utcnow():
            raise ValueError("Время отправки должно быть в будущем")
        if audience not in {"all", "unready", "team"} or (audience == "team" and not team):
            raise ValueError("Выберите получателей объявления")
        with self.sessions.begin() as db:
            game = self._game(db, game_id)
            self._require_manager(db, game, host_id)
            if audience == "unready" and game.status != "drawn":
                raise InvalidState("Выбор по готовности доступен после жеребьёвки")
            statement = select(Member).where(Member.game_id == game_id, Member.approved.is_(True))
            if audience == "unready":
                statement = statement.where(Member.gift_status.not_in(["ready", "sent", "given"]))
            elif audience == "team":
                statement = statement.where(Member.team == team)
            members = list(db.scalars(statement))
            if not members:
                raise ValueError("В выбранной группе нет участников")
            batch = secrets.token_hex(8)
            for member in members:
                db.add(Delivery(key=f"announcement:{game_id}:{batch}:{member.user_id}",
                                user_id=member.user_id, text=f"📣 {game.title}\n{text.strip()}",
                                due_at=due_at or utcnow()))
            return len(members)

    def schedule_reminders(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        local_now = now.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Moscow"))
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

    @staticmethod
    def _require_manager(db: Session, game: Game, user_id: int) -> None:
        if game.host_id == user_id:
            return
        if not db.scalar(select(GameHelper.id).where(GameHelper.game_id == game.id,
                                                      GameHelper.user_id == user_id)):
            raise AccessDenied("Только ведущий или помощник может выполнить действие")
