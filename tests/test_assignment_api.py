"""Set vivas through the HTTP API, with the real services over a moto table.

The whole loop: a teacher sets a viva, a student sees it, starts it, the
examiner concludes it, and the result reaches the teacher. Only Gemini's
token minting is faked.
"""

import asyncio
import datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.api.deps import (
    get_assignment_service,
    get_group_service,
    get_viva_service,
)
from app.api.v1.assignments import limiter as assignment_limiter
from app.api.v1.groups import limiter as group_limiter
from app.api.v1.viva import limiter as viva_limiter
from app.core.auth import get_current_user
from app.db.assignment_repository import AssignmentRepository
from app.db.group_repository import GroupRepository
from app.db.models import AssignmentRef
from app.db.profile_repository import ProfileRepository, UserProfile
from app.db.session_repository import SessionRepository
from app.main import app
from app.services.assignment_service import (
    AssignmentResultRecorder,
    AssignmentService,
)
from app.services.group_service import GroupService
from app.services.viva_service import VivaService

TABLE = "test-sessions"
PROFILES = {
    "teacher": UserProfile(name="Meera Sharma", account_type="teacher"),
    "other-teacher": UserProfile(name="Arun Rao", account_type="teacher"),
    "riya": UserProfile(name="Riya Verma", class_level=9),
    "aman": UserProfile(name="Aman Gupta", class_level=9),
    "outsider": UserProfile(name="Kiran", class_level=9),
}


def selection(class_level=9, chapters=None):
    return {
        "catalog_id": "ncert-cbse",
        "class_level": class_level,
        "subject_id": "science",
        "subject_name": "Science",
        "chapters": chapters
        or [
            {
                "id": "motion",
                "name": "Motion",
                "topics": [{"id": "speed", "name": "Speed and velocity"}],
            }
        ],
    }


TOKEN = {
    "token": "ephemeral-token",
    "model_name": "gemini-live",
    "session_duration_minutes": 10,
    "voice_name": "Kore",
}


@pytest.fixture
def world():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1")
        client.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        )
        profiles = ProfileRepository(client, TABLE)
        for user_id, profile in PROFILES.items():
            asyncio.run(profiles.save(user_id, profile))
        groups = GroupRepository(client, TABLE)
        assignments = AssignmentRepository(client, TABLE)
        sessions = SessionRepository(client, TABLE)
        llm = SimpleNamespace(create_ephemeral_token=AsyncMock(return_value=TOKEN))
        viva = VivaService(llm, sessions, AssignmentResultRecorder(assignments))
        identity = SimpleNamespace(user_id="teacher")
        app.dependency_overrides[get_current_user] = lambda: identity
        app.dependency_overrides[get_group_service] = lambda: GroupService(
            groups, profiles
        )
        app.dependency_overrides[get_viva_service] = lambda: viva
        app.dependency_overrides[get_assignment_service] = lambda: AssignmentService(
            groups, assignments, profiles, viva
        )
        for limiter in (assignment_limiter, group_limiter, viva_limiter):
            limiter.reset()

        def as_user(user_id):
            identity.user_id = user_id
            return TestClient(app)

        yield SimpleNamespace(
            api=as_user, llm=llm, sessions=sessions, assignments=assignments
        )
        app.dependency_overrides.clear()


@pytest.fixture
def group(world):
    """A Class 9 group owned by the teacher, with Riya and Aman in it."""
    created = (
        world.api("teacher")
        .post("/api/v1/groups", json={"name": "9-A", "class_level": 9})
        .json()
    )
    for student in ("riya", "aman"):
        world.api(student).post(
            "/api/v1/groups/join", json={"code": created["join_code"]}
        )
    return created["group_id"]


def set_viva(world, group_id, **body):
    response = world.api("teacher").post(
        f"/api/v1/groups/{group_id}/assignments",
        json={"curriculum_selection": selection(), **body},
    )
    assert response.status_code == 201, response.text
    return response.json()


def conclude(world, student, session_id, score=7):
    return world.api(student).post(
        "/api/v1/viva/conclude-viva",
        json={
            "viva_session_id": session_id,
            "score": score,
            "summary": "Clear on speed; mixed up velocity.",
            "strong_points": ["Defined speed"],
            "areas_of_improvement": ["Velocity has a direction"],
            "transcript": "private words",
        },
    )


def start(world, student, group_id, assignment_id):
    return world.api(student).post(
        f"/api/v1/groups/{group_id}/assignments/{assignment_id}/start"
    )


def test_the_whole_loop_reaches_the_teacher(world, group):
    viva = set_viva(world, group, due_on="2099-01-01")
    assert viva["title"] == "Motion"
    assert viva["completed_count"] == 0

    assigned = world.api("riya").get("/api/v1/groups/assigned").json()
    assert [(a["title"], a["status"], a["owner_name"]) for a in assigned] == [
        ("Motion", "pending", "Meera Sharma")
    ]

    started = start(world, "riya", group, viva["assignment_id"])
    assert started.status_code == 200
    assert started.json()["ephemeral_token"] == "ephemeral-token"
    session_id = started.json()["viva_session_id"]
    # The viva was built on the server, from the assignment, for this student.
    request = world.llm.create_ephemeral_token.await_args.args[0]
    assert request.student_name == "Riya Verma"
    assert request.topic == "Motion: Speed and velocity"
    stored = asyncio.run(world.sessions.get_session_for_user("riya", session_id))
    assert stored.assignment == AssignmentRef(
        group_id=group, assignment_id=viva["assignment_id"]
    )

    assert conclude(world, "riya", session_id).status_code == 200

    done = world.api("riya").get("/api/v1/groups/assigned").json()[0]
    assert (done["status"], done["score"], done["session_id"]) == (
        "done",
        7,
        session_id,
    )
    listed = world.api("teacher").get(f"/api/v1/groups/{group}/assignments").json()
    assert listed[0]["completed_count"] == 1
    results = asyncio.run(world.assignments.list_results(group, viva["assignment_id"]))
    assert [(r.student_id, r.feedback.score) for r in results] == [("riya", 7)]
    assert "private words" not in results[0].model_dump_json()


def test_only_the_first_finished_attempt_counts(world, group):
    viva = set_viva(world, group)
    first = start(world, "riya", group, viva["assignment_id"]).json()
    conclude(world, "riya", first["viva_session_id"], score=4)
    again = start(world, "riya", group, viva["assignment_id"]).json()
    conclude(world, "riya", again["viva_session_id"], score=9)

    done = world.api("riya").get("/api/v1/groups/assigned").json()[0]
    assert (done["score"], done["session_id"]) == (4, first["viva_session_id"])
    listed = world.api("teacher").get(f"/api/v1/groups/{group}/assignments").json()
    assert listed[0]["completed_count"] == 1


def test_a_viva_must_fit_the_groups_class(world, group):
    response = world.api("teacher").post(
        f"/api/v1/groups/{group}/assignments",
        json={"curriculum_selection": selection(class_level=10)},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "This group is for Class 9. Pick a Class 9 chapter."
    )


@pytest.mark.parametrize(
    "body",
    [
        {"curriculum_selection": selection(), "due_on": "2000-01-01"},
        {
            "curriculum_selection": selection(
                chapters=[
                    {"id": "motion", "name": "Motion"},
                    {"id": "force", "name": "Force"},
                ]
            )
        },
        {"curriculum_selection": selection(), "owner_id": "x"},
    ],
)
def test_bad_requests_are_refused(world, group, body):
    response = world.api("teacher").post(
        f"/api/v1/groups/{group}/assignments", json=body
    )

    assert response.status_code == 422


def test_nobody_but_the_owner_sets_or_sees_the_groups_vivas(world, group):
    set_viva(world, group)

    for user in ("other-teacher", "riya"):
        assert (
            world.api(user)
            .post(
                f"/api/v1/groups/{group}/assignments",
                json={"curriculum_selection": selection()},
            )
            .status_code
            == 404
        )
        assert (
            world.api(user).get(f"/api/v1/groups/{group}/assignments").status_code
            == 404
        )


def test_only_students_in_the_group_can_start_its_vivas(world, group):
    viva = set_viva(world, group)

    assert start(world, "outsider", group, viva["assignment_id"]).status_code == 404
    assert start(world, "teacher", group, viva["assignment_id"]).status_code == 403
    assert world.api("outsider").get("/api/v1/groups/assigned").json() == []
    world.llm.create_ephemeral_token.assert_not_awaited()


def test_a_removed_viva_disappears_with_its_results(world, group):
    viva = set_viva(world, group)
    session = start(world, "riya", group, viva["assignment_id"]).json()
    conclude(world, "riya", session["viva_session_id"])

    response = world.api("teacher").delete(
        f"/api/v1/groups/{group}/assignments/{viva['assignment_id']}"
    )

    assert response.status_code == 204
    assert world.api("riya").get("/api/v1/groups/assigned").json() == []
    assert world.api("teacher").get(f"/api/v1/groups/{group}/assignments").json() == []
    assert start(world, "riya", group, viva["assignment_id"]).status_code == 404
    assert asyncio.run(world.assignments.list_completions("riya")) == []


def test_a_student_who_left_keeps_their_report_but_it_is_not_passed_on(world, group):
    viva = set_viva(world, group)
    session = start(world, "riya", group, viva["assignment_id"]).json()
    world.api("riya").delete(f"/api/v1/groups/joined/{group}")

    response = conclude(world, "riya", session["viva_session_id"])

    assert response.status_code == 200
    assert response.json()["score"] == 7
    listed = world.api("teacher").get(f"/api/v1/groups/{group}/assignments").json()
    assert listed[0]["completed_count"] == 0


def test_a_failure_to_pass_on_the_result_never_costs_the_student_their_report(
    world, group, caplog
):
    viva = set_viva(world, group)
    session = start(world, "riya", group, viva["assignment_id"]).json()
    world.assignments.record_result = AsyncMock(side_effect=RuntimeError("down"))
    caplog.set_level(logging.INFO)

    response = conclude(world, "riya", session["viva_session_id"])

    assert response.status_code == 200
    assert "event=assignment_result_record_failed" in caplog.text
    assert "Riya" not in caplog.text


def test_pending_vivas_come_first_soonest_due_first(world, group):
    later = set_viva(world, group, due_on="2099-12-31")
    sooner = set_viva(world, group, due_on="2099-01-01")
    undated = set_viva(world, group)
    finished = set_viva(world, group)
    session = start(world, "riya", group, finished["assignment_id"]).json()
    conclude(world, "riya", session["viva_session_id"])

    order = [
        a["assignment_id"]
        for a in world.api("riya").get("/api/v1/groups/assigned").json()
    ]

    assert order == [
        sooner["assignment_id"],
        later["assignment_id"],
        undated["assignment_id"],
        finished["assignment_id"],
    ]


def test_deleting_a_group_takes_its_vivas_and_results_with_it(world, group):
    viva = set_viva(world, group)
    session = start(world, "riya", group, viva["assignment_id"]).json()
    conclude(world, "riya", session["viva_session_id"])

    world.api("teacher").delete(f"/api/v1/groups/{group}")

    assert world.api("riya").get("/api/v1/groups/assigned").json() == []
    assert asyncio.run(world.assignments.list_completions("riya")) == []


def test_assigned_is_not_mistaken_for_a_group_id(world):
    response = world.api("riya").get("/api/v1/groups/assigned")

    assert response.status_code == 200
    assert response.json() == []


def test_due_dates_are_days_not_moments(world, group):
    today = datetime.datetime.now(datetime.UTC).date().isoformat()

    assert set_viva(world, group, due_on=today)["due_on"] == today
