from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from santa.draw import ImpossibleDraw, draw
from santa.models import Assignment, Delivery, Mode, utcnow
from santa.service import AccessDenied, InvalidState, Service


@pytest.fixture
def service(tmp_path):
    result = Service(f"sqlite:///{tmp_path / 'test.db'}", frozenset({99}))
    result.create_schema()
    return result


def game_with_players(service, mode=Mode.BLIND):
    game = service.create_game(1, "Host", "Holiday", mode)
    for number in (2, 3, 4):
        service.join(game.id, number, f"Player {number}")
    return game


@pytest.mark.parametrize("mode,host_plays,sees_all", [
    (Mode.BLIND, True, False),
    (Mode.OPEN, True, True),
    (Mode.HOST, False, True),
])
def test_modes(service, mode, host_plays, sees_all):
    game = game_with_players(service, mode)
    result = service.run_draw(game.id, 1)
    assert (1 in result) is host_plays
    assert set(result) == set(result.values())
    assert all(giver != receiver for giver, receiver in result.items())
    if sees_all:
        assert len(service.all_assignments(game.id, 1)) == len(result)
    else:
        with pytest.raises(AccessDenied):
            service.all_assignments(game.id, 1)
    with pytest.raises(InvalidState):
        service.run_draw(game.id, 1)
    with pytest.raises(InvalidState):
        service.set_mode(game.id, 1, Mode.HOST)


def test_constraints_and_impossible_draw(service):
    game = game_with_players(service)
    service.add_exclusion(game.id, 1, 2, 3)
    assert service.run_draw(game.id, 1)[2] != 3
    with pytest.raises(ImpossibleDraw):
        draw([1, 2], {(1, 2)})


def test_game_isolation_and_broadcast_auth(service):
    first = game_with_players(service)
    second = service.create_game(10, "Other Host", "Other", Mode.HOST)
    service.join(second.id, 11, "Someone")
    service.join(second.id, 12, "Someone else")
    assert service.assignment(second.id, 2) is None
    with pytest.raises(AccessDenied):
        service.announce(second.id, 1, "Hello")
    with pytest.raises(AccessDenied):
        service.members(first.id, 10)


def test_reminders_idempotent_and_persistent(service):
    service.create_game(1, "Host", "Holiday", Mode.BLIND,
                        registration_deadline=utcnow() + timedelta(hours=12))
    assert service.schedule_reminders() == 1
    assert service.schedule_reminders() == 0
    restarted = Service(str(service.engine.url))
    assert restarted.schedule_reminders() == 0
    with restarted.sessions() as db:
        assert len(db.scalars(select(Delivery)).all()) == 1


def test_owner_allowlist(service):
    assert service.is_owner(99)
    assert not service.is_owner(1)


def test_failed_draw_leaves_no_assignments(service):
    game = service.create_game(1, "Host", "Holiday", Mode.HOST)
    service.join(game.id, 2, "Two")
    service.join(game.id, 3, "Three")
    service.add_exclusion(game.id, 1, 2, 3)
    with pytest.raises(ImpossibleDraw):
        service.run_draw(game.id, 1)
    with service.sessions() as db:
        assert db.scalars(select(Assignment)).all() == []
    assert service.game(game.id).status == "registration"


def test_profile_is_scoped_to_member(service):
    game = game_with_players(service)
    service.profile(game.id, 2, wishes="Чай", avoid="Орехи")
    member = service.member(game.id, 2)
    assert (member.wishes, member.avoid) == ("Чай", "Орехи")
    with pytest.raises(AccessDenied):
        service.profile(game.id, 99, wishes="Чужая анкета")


def test_announcement_queues_only_own_members(service):
    game = game_with_players(service)
    assert service.announce(game.id, 1, "Привет") == 4
    with service.sessions() as db:
        jobs = db.scalars(select(Delivery)).all()
        assert {job.user_id for job in jobs} == {1, 2, 3, 4}


def test_change_mode_and_budget_before_draw(service):
    game = game_with_players(service)
    service.set_mode(game.id, 1, Mode.HOST)
    assert service.member(game.id, 1) is None
    service.set_budget(game.id, 1, "3000 ₽")
    assert service.game(game.id).budget == "3000 ₽"
    service.set_mode(game.id, 1, Mode.OPEN)
    assert service.member(game.id, 1) is not None
    service.run_draw(game.id, 1)
    with pytest.raises(InvalidState):
        service.set_budget(game.id, 1, "5000 ₽")


def test_reminder_opt_out(service):
    game = service.create_game(1, "Host", "Holiday", Mode.BLIND,
                               registration_deadline=utcnow() + timedelta(hours=12))
    service.join(game.id, 2, "Two")
    service.set_reminders(2, False)
    assert service.schedule_reminders() == 1
    with service.sessions() as db:
        assert {job.user_id for job in db.scalars(select(Delivery))} == {1}


def test_opt_out_cancels_pending_reminder(service):
    service.create_game(1, "Host", "Holiday", Mode.BLIND,
                        registration_deadline=utcnow() + timedelta(hours=12))
    service.schedule_reminders()
    service.set_reminders(1, False)
    with service.sessions() as db:
        assert db.scalar(select(Delivery)).state == "cancelled"


def test_daily_gift_reminder_once_per_day_until_ready(service):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC).replace(tzinfo=None)
    game = service.create_game(1, "Host", "Holiday", Mode.BLIND,
                               gift_deadline=now + timedelta(days=5))
    service.join(game.id, 2, "Two")
    service.join(game.id, 3, "Three")
    service.run_draw(game.id, 1)
    service.set_daily_reminders(2, True)
    assert service.daily_reminders_enabled(2)
    assert service.schedule_reminders(now) == 1
    assert service.schedule_reminders(now) == 0
    service.set_gift_status(game.id, 2, "ready")
    assert service.schedule_reminders(now + timedelta(days=1)) == 0


def test_daily_toggle_cancels_waiting_job(service):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC).replace(tzinfo=None)
    game = service.create_game(1, "Host", "Holiday", Mode.HOST,
                               gift_deadline=now + timedelta(days=5))
    service.join(game.id, 2, "Two")
    service.join(game.id, 3, "Three")
    service.run_draw(game.id, 1)
    service.set_daily_reminders(2, True)
    service.schedule_reminders(now)
    service.set_daily_reminders(2, False)
    with service.sessions() as db:
        job = db.scalar(select(Delivery).where(Delivery.key.like("daily:%")))
        assert job.state == "cancelled"


def test_questions_are_anonymous_and_answer_is_scoped(service):
    game = game_with_players(service)
    assignments = service.run_draw(game.id, 1)
    giver, receiver = next(iter(assignments.items()))
    question_id = service.ask_receiver(game.id, giver, "Какой цвет выбрать?")
    incoming = service.incoming_questions(game.id, receiver)
    assert [item.id for item in incoming] == [question_id]
    with service.sessions() as db:
        job = db.scalar(select(Delivery).where(Delivery.key == f"question:{question_id}"))
        assert job.user_id == receiver
        assert str(giver) not in job.text
    with pytest.raises(AccessDenied):
        service.answer_question(question_id, 99, "Синий")
    service.answer_question(question_id, receiver, "Синий")
    assert service.incoming_questions(game.id, receiver) == []
    with service.sessions() as db:
        reply = db.scalar(select(Delivery).where(Delivery.key == f"answer:{question_id}"))
        assert reply.user_id == giver
    with pytest.raises(InvalidState):
        service.answer_question(question_id, receiver, "Красный")


def test_lifecycle_and_transfer(service):
    game = game_with_players(service)
    service.edit_game(game.id, 1, description="Party", rules="No spoilers")
    assert service.game(game.id).rules == "No spoilers"
    with pytest.raises(AccessDenied):
        service.edit_game(game.id, 2, title="Hijacked")
    service.transfer_host(game.id, 1, 2)
    with pytest.raises(AccessDenied):
        service.run_draw(game.id, 1)
    service.run_draw(game.id, 2)
    with pytest.raises(InvalidState):
        service.edit_game(game.id, 2, title="Late")
    service.confirm_received(game.id, 3)
    assert service.member(game.id, 3).received
    service.close_game(game.id, 2)
    assert service.game(game.id).status == "finished"
    service.archive_game(game.id, 2)
    assert service.game(game.id).status == "archived"


def test_exclusion_removal_and_owner_stats(service):
    game = game_with_players(service)
    service.add_exclusion(game.id, 1, 2, 3)
    assert len(service.exclusions(game.id, 1)) == 1
    service.remove_exclusion(game.id, 1, 2, 3)
    assert service.exclusions(game.id, 1) == []
    with pytest.raises(AccessDenied):
        service.owner_stats(1)
    assert service.owner_stats(99)["games"] == 1


def test_scheduled_announcement_waits_for_due_time(service):
    game = game_with_players(service)
    due = utcnow() + timedelta(days=2)
    assert service.announce(game.id, 1, "Встречаемся у ёлки", due_at=due) == 4
    with service.sessions() as db:
        jobs = db.scalars(select(Delivery)).all()
        assert all(job.due_at == due for job in jobs)
    with pytest.raises(ValueError):
        service.announce(game.id, 1, "Поздно", due_at=utcnow() - timedelta(seconds=1))


def test_draw_notification_links_to_receiver(service):
    game = game_with_players(service)
    assignments = service.run_draw(game.id, 1)
    giver, receiver = next(iter(assignments.items()))
    with service.sessions() as db:
        job = db.scalar(select(Delivery).where(Delivery.key == f"draw:{game.id}:{giver}"))
        assert job.parse_mode == "HTML"
        assert f'tg://user?id={receiver}' in job.text


def test_game_view_preserves_access_and_host_count(service):
    game = game_with_players(service)
    host_view = service.game_view(game.id, 1)
    assert host_view is not None and host_view[2] == 4
    member_view = service.game_view(game.id, 2)
    assert member_view is not None and member_view[1].user_id == 2
    assert member_view[2] is None
    assert service.game_view(game.id, 99) is None
