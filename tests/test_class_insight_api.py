"""The AI class insight through the HTTP API, over a moto table.

The generator is faked: these tests pin what the service promises whatever
model sits behind it. Names never leave the backend, the model's output is
checked against the class, and an insight is only rebuilt when results change.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.api.deps import (
    get_assignment_service,
    get_class_insight_service,
    get_group_service,
    get_viva_service,
)
from app.api.v1.assignments import limiter as assignment_limiter
from app.api.v1.groups import limiter as group_limiter
from app.api.v1.viva import limiter as viva_limiter
from app.core.auth import get_current_user
from app.db.assignment_repository import AssignmentRepository
from app.db.group_repository import GroupRepository
from app.db.insight_repository import InsightRepository
from app.db.profile_repository import ProfileRepository, UserProfile
from app.db.session_repository import SessionRepository
from app.domain.class_insight import (
    GapDraft,
    InsightDraft,
    InsightInput,
    SuggestionDraft,
    SupportDraft,
    ThemeDraft,
)
from app.interfaces.insight_generator import InsightGenerationFailed
from app.main import app
from app.services.assignment_service import (
    AssignmentResultRecorder,
    AssignmentService,
)
from app.services.class_insight_service import ClassInsightService
from app.services.group_service import GroupService
from app.services.viva_service import VivaService
from tests.test_assignment_api import TOKEN, selection

TABLE = "test-sessions"
PROFILES = {
    "teacher": UserProfile(name="Meera Sharma", account_type="teacher"),
    "other-teacher": UserProfile(name="Arun Rao", account_type="teacher"),
    "riya": UserProfile(name="Riya Verma", class_level=9),
    "aman": UserProfile(name="Aman Gupta", class_level=9),
    "zoya": UserProfile(name="Zoya Khan", class_level=9),
    "dev": UserProfile(name="Dev Patel", class_level=9),
}


def draft(**overrides) -> InsightDraft:
    base = {
        "overview": "Most of the class can define speed.",
        "strengths": [ThemeDraft(point="Defines speed", student_refs=["S1", "S2"])],
        "gaps": [
            GapDraft(
                point="Velocity has a direction",
                misconception="Treats velocity as speed",
                student_refs=["S1", "S2", "S3"],
            )
        ],
        "students_to_support": [SupportDraft(student_ref="S3", reason="Low score")],
        "suggestions": [
            SuggestionDraft(
                action="Walk the class around the room and compare paths",
                addresses="Velocity has a direction",
            )
        ],
        "coverage_note": "One short viva on Motion.",
    }
    return InsightDraft(**{**base, **overrides})


class FakeGenerator:
    id = "fake:prompt-v1"

    def __init__(self):
        self.inputs: list[InsightInput] = []
        self.result: InsightDraft | Exception = draft()

    async def generate(self, data: InsightInput) -> InsightDraft:
        self.inputs.append(data)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


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
        insights = InsightRepository(client, TABLE)
        llm = SimpleNamespace(create_ephemeral_token=AsyncMock(return_value=TOKEN))
        viva = VivaService(
            llm,
            SessionRepository(client, TABLE),
            AssignmentResultRecorder(assignments),
        )
        generator = FakeGenerator()
        identity = SimpleNamespace(user_id="teacher")
        app.dependency_overrides[get_current_user] = lambda: identity
        app.dependency_overrides[get_group_service] = lambda: GroupService(
            groups, profiles
        )
        app.dependency_overrides[get_viva_service] = lambda: viva
        app.dependency_overrides[get_assignment_service] = lambda: AssignmentService(
            groups, assignments, profiles, viva
        )
        app.dependency_overrides[get_class_insight_service] = lambda: (
            ClassInsightService(groups, assignments, insights, generator)
        )
        for limiter in (assignment_limiter, group_limiter, viva_limiter):
            limiter.reset()

        def as_user(user_id):
            identity.user_id = user_id
            return TestClient(app)

        yield SimpleNamespace(api=as_user, generator=generator, insights=insights)
        app.dependency_overrides.clear()


@pytest.fixture
def viva(world):
    """A Class 9 group with four students and one set viva. Returns its paths."""
    group = (
        world.api("teacher")
        .post("/api/v1/groups", json={"name": "9-A", "class_level": 9})
        .json()
    )
    for student in ("riya", "aman", "zoya", "dev"):
        world.api(student).post(
            "/api/v1/groups/join", json={"code": group["join_code"]}
        )
    assignment = (
        world.api("teacher")
        .post(
            f"/api/v1/groups/{group['group_id']}/assignments",
            json={"curriculum_selection": selection()},
        )
        .json()
    )
    base = (
        f"/api/v1/groups/{group['group_id']}/assignments/{assignment['assignment_id']}"
    )
    return SimpleNamespace(
        group_id=group["group_id"],
        assignment_id=assignment["assignment_id"],
        base=base,
        insight=f"{base}/insight",
    )


def finish(world, viva, student, score=6, summary="Clear on speed."):
    started = world.api(student).post(f"{viva.base}/start").json()
    response = world.api(student).post(
        "/api/v1/viva/conclude-viva",
        json={
            "viva_session_id": started["viva_session_id"],
            "score": score,
            "summary": summary,
            "strong_points": ["Defined speed"],
            "areas_of_improvement": ["Velocity has a direction"],
            "transcript": "private spoken words",
        },
    )
    assert response.status_code == 200, response.text


def teacher(world):
    return world.api("teacher")


def test_fewer_than_three_results_is_not_enough_for_an_insight(world, viva):
    finish(world, viva, "riya")
    finish(world, viva, "aman")

    read = teacher(world).get(viva.insight).json()
    assert (read["status"], read["results_count"], read["member_count"]) == (
        "not_enough_results",
        2,
        4,
    )
    assert read["min_results"] == 3 and read["insight"] is None

    response = teacher(world).post(viva.insight)
    assert response.status_code == 409
    assert world.generator.inputs == []


def test_the_insight_is_written_from_reports_named_and_counted_by_the_server(
    world, viva
):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    assert teacher(world).get(viva.insight).json()["status"] == "ready_to_generate"

    response = teacher(world).post(viva.insight)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["is_stale"], body["new_results"]) == (
        "ready",
        False,
        0,
    )
    insight = body["insight"]
    assert insight["based_on"] == 3
    assert insight["generator_id"] == "fake:prompt-v1"
    gap = insight["gaps"][0]
    assert gap["student_count"] == 3
    assert sorted(s["name"] for s in gap["students"]) == [
        "Aman Gupta",
        "Riya Verma",
        "Zoya Khan",
    ]
    [support] = insight["students_to_support"]
    assert support["student"]["name"] in {"Aman Gupta", "Riya Verma", "Zoya Khan"}
    # The same insight is there on the next read, without another model call.
    assert teacher(world).get(viva.insight).json()["insight"] == insight
    assert len(world.generator.inputs) == 1


def test_the_model_sees_pseudonyms_and_reports_never_names_or_transcripts(world, viva):
    finish(
        world, viva, "riya", summary="Riya Verma explained speed; Riya mixed up units."
    )
    finish(world, viva, "aman")
    finish(world, viva, "zoya")

    teacher(world).post(viva.insight)

    [sent] = world.generator.inputs
    assert sorted(s.ref for s in sent.students) == ["S1", "S2", "S3"]
    assert sent.chapter == "Motion" and sent.class_level == 9
    text = sent.model_dump_json()
    for secret in ("Riya", "Verma", "Aman", "Zoya", "riya", "private spoken words"):
        assert secret not in text
    riya = next(s for s in sent.students if "explained" in s.summary)
    assert riya.summary == f"{riya.ref} explained speed; {riya.ref} mixed up units."


def test_asking_again_without_new_results_reuses_the_insight(world, viva):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    first = teacher(world).post(viva.insight).json()

    second = teacher(world).post(viva.insight).json()

    assert second == first
    assert len(world.generator.inputs) == 1


def test_a_new_result_marks_the_insight_stale_until_it_is_refreshed(world, viva):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    teacher(world).post(viva.insight)

    finish(world, viva, "dev")
    stale = teacher(world).get(viva.insight).json()
    assert (stale["status"], stale["is_stale"], stale["new_results"]) == (
        "ready",
        True,
        1,
    )
    assert stale["insight"]["based_on"] == 3

    fresh = teacher(world).post(viva.insight).json()
    assert (fresh["is_stale"], fresh["insight"]["based_on"]) == (False, 4)
    assert len(world.generator.inputs) == 2
    assert len(world.generator.inputs[1].students) == 4


def test_made_up_students_are_dropped_and_empty_themes_go_with_them(world, viva):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    world.generator.result = draft(
        strengths=[
            ThemeDraft(point="Real", student_refs=["S1", "S1", "S9"]),
            ThemeDraft(point="Invented", student_refs=["S7"]),
        ],
        students_to_support=[
            SupportDraft(student_ref="S8", reason="Nobody"),
            SupportDraft(student_ref="S2", reason="Real"),
            SupportDraft(student_ref="S2", reason="Twice"),
        ],
    )

    insight = teacher(world).post(viva.insight).json()["insight"]

    assert [(t["point"], t["student_count"]) for t in insight["strengths"]] == [
        ("Real", 1)
    ]
    assert [s["reason"] for s in insight["students_to_support"]] == ["Real"]


def test_refs_in_written_text_read_as_names(world, viva):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    world.generator.result = draft(
        overview="S1 and S2 explained speed well; S9 is not a student.",
    )

    insight = teacher(world).post(viva.insight).json()["insight"]

    refs = {s.ref for s in world.generator.inputs[0].students}
    assert refs == {"S1", "S2", "S3"}
    overview = insight["overview"]
    assert "S1" not in overview and "S2" not in overview
    assert overview.endswith("S9 is not a student.")
    assert sum(n in overview for n in ("Riya Verma", "Aman Gupta", "Zoya Khan")) == 2


def test_a_generator_failure_is_a_retryable_error_and_nothing_is_saved(world, viva):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    world.generator.result = InsightGenerationFailed("model said no")

    response = teacher(world).post(viva.insight)

    assert response.status_code == 503
    assert "model said no" not in response.text
    assert teacher(world).get(viva.insight).json()["insight"] is None


def test_students_who_left_are_not_part_of_the_class_insight(world, viva):
    for student in ("riya", "aman", "zoya", "dev"):
        finish(world, viva, student)
    world.api("dev").delete(f"/api/v1/groups/joined/{viva.group_id}")

    body = teacher(world).post(viva.insight).json()

    assert body["results_count"] == 3 and body["member_count"] == 3
    assert len(world.generator.inputs[0].students) == 3


@pytest.mark.parametrize("user", ["other-teacher", "riya"])
def test_only_the_owner_reads_or_generates_the_insight(world, viva, user):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)

    assert world.api(user).get(viva.insight).status_code == 404
    assert world.api(user).post(viva.insight).status_code == 404
    assert world.generator.inputs == []


def test_removing_the_viva_removes_its_insight(world, viva):
    for student in ("riya", "aman", "zoya"):
        finish(world, viva, student)
    teacher(world).post(viva.insight)

    assert teacher(world).delete(viva.base).status_code == 204

    assert teacher(world).get(viva.insight).status_code == 404
    stored = asyncio.run(world.insights.get(viva.group_id, viva.assignment_id))
    assert stored is None
