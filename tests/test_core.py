from datetime import timedelta

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
