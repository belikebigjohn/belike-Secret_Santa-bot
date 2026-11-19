import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from santa.draw import ImpossibleDraw, draw
from santa.models import Assignment, Delivery, Mode, User, utcnow
from santa.service import AccessDenied, InvalidState, Service
from santa.worker import recover_stale


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


def test_game_isolation_and_manager_auth(service):
    first = game_with_players(service)
    second = service.create_game(10, "Other Host", "Other", Mode.HOST)
    service.join(second.id, 11, "Someone")
    service.join(second.id, 12, "Someone else")
    assert service.assignment(second.id, 2) is None
    with pytest.raises(AccessDenied):
        service.members(first.id, 10)


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


def test_description_and_rules_accept_documented_limits(service):
    game = service.create_game(1, "Host", "Holiday", Mode.BLIND)
    service.edit_game(game.id, 1, description="О" * 2000, rules="П" * 3000)
    assert len(service.game(game.id).description) == 2000
    assert len(service.game(game.id).rules) == 3000
    with pytest.raises(ValueError, match="до 2000 символов"):
        service.edit_game(game.id, 1, description="О" * 2001)
    with pytest.raises(ValueError, match="до 3000 символов"):
        service.edit_game(game.id, 1, rules="П" * 3001)



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
        assert f"tg://user?id={giver}" not in job.text
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
    question_id = service.ask_receiver(game.id, 3, "Чай или кофе?")
    with pytest.raises(InvalidState):
        service.edit_game(game.id, 2, title="Late")
    with pytest.raises(InvalidState):
        service.archive_game(game.id, 2)
    service.confirm_received(game.id, 3)
    assert service.member(game.id, 3).received
    service.close_game(game.id, 2)
    assert service.game(game.id).status == "finished"
    with pytest.raises(AccessDenied):
        service.archive_game(game.id, 1)
    service.archive_game(game.id, 2)
    assert service.game(game.id) is None
    assert service.game_by_token(game.token) is None
    assert service.my_games(2) == []
    with service.sessions() as db:
        assert db.scalars(select(Assignment).where(Assignment.game_id == game.id)).all() == []
        assert db.scalars(select(Delivery).where(Delivery.key == f"question:{question_id}")).all() == []


def test_exclusion_removal_and_owner_stats(service):
    game = game_with_players(service)
    service.add_exclusion(game.id, 1, 2, 3)
    assert len(service.exclusions(game.id, 1)) == 1
    service.remove_exclusion(game.id, 1, 2, 3)
    assert service.exclusions(game.id, 1) == []
    with pytest.raises(AccessDenied):
        service.owner_stats(1)
    assert service.owner_stats(99)["games"] == 1



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


@pytest.mark.parametrize("mode", [Mode.BLIND, Mode.HOST])
def test_eighty_player_game_has_exactly_one_giver_and_receiver(service, mode):
    game = service.create_game(1, "Host", "Company holiday", mode)
    expected = set(range(1, 81)) if mode == Mode.BLIND else set(range(2, 82))
    for user_id in sorted(expected - {1}):
        service.join(game.id, user_id, f"Player {user_id}")
    result = service.run_draw(game.id, 1)
    assert len(result) == 80
    assert set(result) == expected
    assert set(result.values()) == expected
    assert all(giver != receiver for giver, receiver in result.items())
    with service.sessions() as db:
        assignments = db.scalars(select(Assignment).where(Assignment.game_id == game.id)).all()
        deliveries = db.scalars(select(Delivery).where(Delivery.key.like(f"draw:{game.id}:%"))).all()
        assert len(assignments) == len(deliveries) == 80
        assert {item.giver_id for item in assignments} == expected
        assert {item.receiver_id for item in assignments} == expected
        assert {item.user_id for item in deliveries} == expected
    with pytest.raises(InvalidState):
        service.join(game.id, 999, "Late")
    if mode == Mode.BLIND:
        with pytest.raises(AccessDenied):
            service.all_assignments(game.id, 1)
    else:
        assert len(service.all_assignments(game.id, 1)) == 80


def test_eighty_player_constrained_matching_without_single_cycle():
    ids = list(range(80))
    forbidden = {(giver, receiver) for giver in ids for receiver in ids
                 if giver // 40 != receiver // 40}
    for _ in range(5):
        result = draw(ids, forbidden)
        assert set(result) == set(result.values()) == set(ids)
        assert all(giver != receiver and (giver, receiver) not in forbidden
                   for giver, receiver in result.items())


def test_impossible_eighty_player_matching_fails_cleanly():
    ids = list(range(80))
    # Two givers share the same sole possible receiver: no perfect matching exists.
    forbidden = {(giver, receiver) for giver in (0, 1) for receiver in ids if receiver != 79}
    with pytest.raises(ImpossibleDraw):
        draw(ids, forbidden)


def test_draw_notification_stays_within_telegram_limit(service):
    game = service.create_game(1, "Host", "<" * 120, Mode.BLIND, budget="&" * 80)
    service.join(game.id, 2, "<" * 120)
    service.profile(game.id, 2, wishes="<" * 2000, avoid="&" * 2000)
    service.run_draw(game.id, 1)
    with service.sessions() as db:
        jobs = db.scalars(select(Delivery).where(Delivery.key.like(f"draw:{game.id}:%"))).all()
        assert len(jobs) == 2
        assert all(len(job.text) <= 4096 for job in jobs)


def test_capacity_and_approval_gate_draw(service):
    game = service.create_game(1, "Host", "Team", Mode.HOST,
                               max_members=2, approval_required=True)
    first = service.join(game.id, 2, "Two")
    second = service.join(game.id, 3, "Three")
    assert not first.approved and not second.approved
    with pytest.raises(InvalidState):
        service.join(game.id, 4, "Four")
    with pytest.raises(InvalidState):
        service.run_draw(game.id, 1)
    service.review_member(game.id, 1, 2, True)
    service.review_member(game.id, 1, 3, True)
    assert len(service.run_draw(game.id, 1)) == 2



def test_registration_dates_and_capacity_edits(service):
    now = utcnow()
    game = service.create_game(1, "Host", "Dates", Mode.BLIND)
    service.set_game_date(game.id, 1, "registration_start", now + timedelta(days=1))
    with pytest.raises(InvalidState):
        service.join(game.id, 2, "Two")
    service.set_game_date(game.id, 1, "registration_start", None)
    service.set_game_date(game.id, 1, "registration_deadline", now + timedelta(days=2))
    with pytest.raises(ValueError):
        service.set_game_date(game.id, 1, "draw_at", now + timedelta(days=1))
    service.set_max_members(game.id, 1, 2)
    service.join(game.id, 2, "Two")
    with pytest.raises(InvalidState):
        service.join(game.id, 3, "Three")
    with pytest.raises(ValueError):
        service.set_max_members(game.id, 1, 1)



def test_helper_can_manage_logistics_but_not_see_secret_pairs(service):
    game = game_with_players(service)
    service.set_helper(game.id, 1, 2, True)
    assert service.is_helper(game.id, 2)
    assert len(service.members(game.id, 2)) == 4
    with pytest.raises(AccessDenied):
        service.run_draw(game.id, 2)
    service.run_draw(game.id, 1)
    assert service.progress(game.id, 2)[0] == 4
    with pytest.raises(AccessDenied):
        service.all_assignments(game.id, 2)
    with pytest.raises(AccessDenied):
        service.set_helper(game.id, 2, 3, True)
    service.set_helper(game.id, 1, 2, False)
    with pytest.raises(AccessDenied):
        service.members(game.id, 2)


def test_replacement_requires_consent_and_preserves_matching(service):
    game = game_with_players(service, Mode.OPEN)
    original = service.run_draw(game.id, 1)
    leaver = 2
    donor = next(giver for giver, receiver in original.items() if receiver == leaver)
    target = original[leaver]
    with pytest.raises(AccessDenied):
        service.create_replacement_invite(game.id, 3, leaver)
    token = service.create_replacement_invite(game.id, 1, leaver)
    assert service.replacement_invite(token)[0].id == game.id
    assert service.member(game.id, leaver) is not None
    assert service.accept_replacement(token, 5, "New") == game.id
    assert service.member(game.id, leaver) is None
    assert service.member(game.id, 5) is not None
    with service.sessions() as db:
        assignments = db.scalars(select(Assignment).where(Assignment.game_id == game.id)).all()
        result = {item.giver_id: item.receiver_id for item in assignments}
        assert len(result) == 4
        assert set(result) == set(result.values()) == {1, 3, 4, 5}
        assert result[5] == target
        assert result[donor] == 5
        stale = db.scalar(select(Delivery).where(Delivery.key == f"draw:{game.id}:{donor}"))
        assert stale.state == "cancelled"
        replacement_jobs = db.scalars(select(Delivery).where(
            Delivery.key.like(f"replacement:{game.id}:%"))).all()
        assert {item.user_id for item in replacement_jobs} == {leaver, donor, 5}
    with pytest.raises(InvalidState):
        service.accept_replacement(token, 6, "Too late")


def test_draft_and_publish(service):
    now = utcnow()
    clone = service.create_game(1, "Host", "Draft", Mode.BLIND, draft=True)
    assert clone.status == "draft"
    assert service.game_by_token(clone.token) is None
    with pytest.raises(InvalidState):
        service.join(clone.id, 2, "Two")
    with pytest.raises(InvalidState):
        service.publish_game(clone.id, 1)
    service.set_game_date(clone.id, 1, "registration_deadline", now + timedelta(days=3))
    service.set_game_date(clone.id, 1, "gift_deadline", now + timedelta(days=11))
    service.set_game_date(clone.id, 1, "celebration_at", now + timedelta(days=13))
    service.publish_game(clone.id, 1)
    assert service.game_by_token(clone.token).id == clone.id
    service.join(clone.id, 2, "Two")


def test_owner_broadcast_reaches_all_started_users_and_can_be_cancelled(service):
    service.ensure_user(99, "Owner")
    game = service.create_game(1, "Host", "Holiday", Mode.HOST)
    for user_id in (2, 3):
        service.join(game.id, user_id, str(user_id))
    assert service.broadcast_audience_count(99) == 4
    with service.sessions.begin() as db:
        db.get(User, 3).blocked = True
    assert service.broadcast_audience_count(99) == 3
    service.ensure_user(3, "Three returned")
    assert service.broadcast_audience_count(99) == 4
    with pytest.raises(AccessDenied):
        service.create_broadcast(1, "Unauthorized")
    campaign_id, count = service.create_broadcast(
        99, "News", due_at=utcnow() + timedelta(days=2))
    assert count == 4
    assert service.broadcast_stats(99, campaign_id)["pending"] == 4
    assert service.cancel_broadcast(99, campaign_id) == 4
    assert service.broadcast_stats(99, campaign_id)["pending"] == 0
    with service.sessions() as db:
        jobs = db.scalars(select(Delivery).where(Delivery.key.like(f"broadcast:{campaign_id}:%"))).all()
        assert {job.user_id for job in jobs} == {1, 2, 3, 99}
        assert all(job.state == "cancelled" for job in jobs)
        assert all("Отключить новости" not in job.text for job in jobs)


def test_broadcast_media_and_button_payload(service):
    service.ensure_user(99, "Owner")
    service.ensure_user(1, "Subscriber")
    with pytest.raises(ValueError):
        service.create_broadcast(99, "Hello", media_type="photo")
    with pytest.raises(ValueError):
        service.create_broadcast(99, "Hello", button_text="Open",
                                 button_url="javascript:alert(1)")
    with pytest.raises(ValueError):
        service.create_broadcast(99, "x" * 901, media_type="photo",
                                 media_file_id="telegram-photo")
    campaign_id, count = service.create_broadcast(
        99, "Hello", media_type="photo", media_file_id="telegram-photo",
        button_text="Open", button_url="https://example.com/path")
    assert count == 2
    with service.sessions() as db:
        job = db.scalar(select(Delivery).where(Delivery.key == f"broadcast:{campaign_id}:1"))
        assert job.media_type == "photo"
        assert job.media_file_id == "telegram-photo"
        assert job.button_text == "Open"
        assert job.button_url == "https://example.com/path"


def test_admin_health_and_retry_transient_broadcast_failures(service):
    for user_id in (99, 1, 2):
        service.ensure_user(user_id, str(user_id))
    with pytest.raises(AccessDenied):
        service.owner_health(1)
    assert service.owner_health(99)["worker_age_seconds"] is None
    service.worker_heartbeat = utcnow()
    assert service.owner_health(99)["worker_age_seconds"] < 5
    service.rate_limited_until = time.monotonic() + 10
    assert service.owner_health(99)["rate_limit_seconds"] > 0
    campaign_id, count = service.create_broadcast(99, "Hello")
    assert count == 3
    with service.sessions.begin() as db:
        transient = db.scalar(select(Delivery).where(
            Delivery.key == f"broadcast:{campaign_id}:1"))
        transient.state = "failed"
        transient.last_error = "TimeoutError"
        transient.attempts = 5
        forbidden = db.scalar(select(Delivery).where(
            Delivery.key == f"broadcast:{campaign_id}:2"))
        forbidden.state = "failed"
        forbidden.last_error = "forbidden"
    assert service.broadcast_stats(99, campaign_id)["failed"] == 2
    assert "TimeoutError: 1" in service.broadcast_stats(99, campaign_id)["errors"]
    assert service.owner_health(99)["retryable_failures"] == 1
    with pytest.raises(AccessDenied):
        service.retry_failed_broadcast(1, campaign_id)
    assert service.retry_failed_broadcast(99, campaign_id) == 1
    with service.sessions() as db:
        job = db.scalar(select(Delivery).where(
            Delivery.key == f"broadcast:{campaign_id}:1"))
        assert (job.state, job.attempts, job.last_error) == ("pending", 0, "")
    assert service.cancel_broadcast(99, campaign_id) == 2
    with pytest.raises(InvalidState):
        service.retry_failed_broadcast(99, campaign_id)


def test_recover_stale_delivery_preserves_recent_claim(service):
    service.ensure_user(1, "One")
    with service.sessions.begin() as db:
        db.add_all([
            Delivery(key="old-claim", user_id=1, text="Old", state="sending",
                     claimed_at=utcnow() - timedelta(minutes=6)),
            Delivery(key="recent-claim", user_id=1, text="Recent", state="sending",
                     claimed_at=utcnow()),
        ])
    assert recover_stale(service) == 1
    with service.sessions() as db:
        states = {job.key: job.state for job in db.scalars(select(Delivery))}
    assert states == {"old-claim": "pending", "recent-claim": "sending"}


def test_anonymous_questions_rate_limit_and_report_keep_sender_secret(service):
    game = game_with_players(service)
    assignments = service.run_draw(game.id, 1)
    giver, receiver = next(iter(assignments.items()))
    question_ids = [service.ask_receiver(game.id, giver, f"Question {index}")
                    for index in range(3)]
    with pytest.raises(InvalidState):
        service.ask_receiver(game.id, giver, "Fourth")
    service.stop_questions(question_ids[0], receiver, report=True)
    with pytest.raises(InvalidState):
        service.ask_receiver(game.id, giver, "After mute")
    with pytest.raises(AccessDenied):
        service.stop_questions(question_ids[0], 99, report=True)
    reports = service.reported_questions(game.id, 1)
    assert reports == [(question_ids[0], "Question 0")]
    with service.sessions() as db:
        job = db.scalar(select(Delivery).where(Delivery.key == f"question-report:{question_ids[0]}"))
        assert f"tg://user?id={giver}" not in job.text
