"""Groups through the HTTP API, with the real service over a moto table.

`signed_in` switches who the token belongs to, so ownership rules are tested
the way an attacker would probe them: same URLs, different identity.
"""

import asyncio
import logging
from types import SimpleNamespace

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.api.deps import get_group_service
from app.api.v1.groups import limiter
from app.core.auth import get_current_user
from app.db.group_repository import GroupRepository
from app.db.profile_repository import ProfileRepository, UserProfile
from app.main import app
from app.services.group_service import GroupService, normalise_join_code

TABLE = "test-sessions"
PROFILES = {
    "teacher": UserProfile(name="Meera Sharma", account_type="teacher"),
    "other-teacher": UserProfile(name="Arun Rao", account_type="parent"),
    "riya": UserProfile(name="Riya Verma", class_level=9),
    "aman": UserProfile(name="Aman Gupta", class_level=9),
}


@pytest.fixture
def api():
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
        service = GroupService(GroupRepository(client, TABLE), profiles)
        identity = SimpleNamespace(user_id="teacher")
        app.dependency_overrides[get_current_user] = lambda: identity
        app.dependency_overrides[get_group_service] = lambda: service
        limiter.reset()

        def as_user(user_id):
            identity.user_id = user_id
            return TestClient(app)

        yield as_user
        app.dependency_overrides.clear()
        limiter.reset()


def create_group(api, name="Class 9-A", **body):
    response = api("teacher").post("/api/v1/groups", json={"name": name, **body})
    assert response.status_code == 201
    return response.json()


def test_groups_require_authentication():
    assert TestClient(app).get("/api/v1/groups").status_code == 401
    assert (
        TestClient(app).post("/api/v1/groups/join", json={"code": "x"}).status_code
        == 401
    )


def test_an_educator_creates_a_group_with_a_join_code(api):
    group = create_group(api, class_level=9)

    assert group["name"] == "Class 9-A"
    assert group["class_level"] == 9
    assert group["member_count"] == 0
    assert group["members"] == []
    assert normalise_join_code(group["join_code"]) == group["join_code"]
    listed = api("teacher").get("/api/v1/groups")
    assert [g["group_id"] for g in listed.json()] == [group["group_id"]]
    assert listed.headers["cache-control"] == "no-store"


def test_a_teacher_can_own_many_groups(api):
    first = create_group(api, "9-A")
    second = create_group(api, "9-B")

    listed = api("teacher").get("/api/v1/groups").json()

    assert [g["group_id"] for g in listed] == [first["group_id"], second["group_id"]]
    assert first["join_code"] != second["join_code"]


def test_a_student_cannot_create_a_group(api):
    response = api("riya").post("/api/v1/groups", json={"name": "Mine"})

    assert response.status_code == 403


def test_a_user_without_a_profile_cannot_create_or_join(api):
    assert api("stranger").post("/api/v1/groups", json={"name": "x"}).status_code == 403
    assert (
        api("stranger").post("/api/v1/groups/join", json={"code": "ABCDEF"}).status_code
        == 403
    )


def test_a_student_previews_then_joins_with_the_code_as_typed(api):
    group = create_group(api, class_level=9)
    typed = group["join_code"][:3].lower() + " " + group["join_code"][3:]

    preview = api("riya").get(f"/api/v1/groups/invite/{typed}")
    joined = api("riya").post("/api/v1/groups/join", json={"code": typed})

    assert preview.status_code == 200
    assert preview.json() == {
        "group_name": "Class 9-A",
        "owner_name": "Meera Sharma",
        "owner_type": "teacher",
        "class_level": 9,
        "already_member": False,
    }
    assert joined.status_code == 200
    assert joined.json()["group_id"] == group["group_id"]
    assert joined.json()["owner_name"] == "Meera Sharma"
    assert "join_code" not in joined.json()
    roster = api("teacher").get(f"/api/v1/groups/{group['group_id']}").json()
    assert roster["member_count"] == 1
    assert [(m["student_id"], m["name"]) for m in roster["members"]] == [
        ("riya", "Riya Verma")
    ]


def test_joining_twice_is_harmless_and_says_so(api):
    group = create_group(api)
    code = group["join_code"]

    first = api("riya").post("/api/v1/groups/join", json={"code": code})
    second = api("riya").post("/api/v1/groups/join", json={"code": code})

    assert (first.status_code, first.json()["already_member"]) == (200, False)
    assert (second.status_code, second.json()["already_member"]) == (200, True)
    assert api("riya").get(f"/api/v1/groups/invite/{code}").json()["already_member"]
    assert not api("aman").get(f"/api/v1/groups/invite/{code}").json()["already_member"]

    assert (
        api("teacher").get(f"/api/v1/groups/{group['group_id']}").json()["member_count"]
        == 1
    )


def test_a_student_can_belong_to_many_groups(api):
    class_group = create_group(api, "9-A")
    tuition = (
        api("other-teacher").post("/api/v1/groups", json={"name": "Tuition"}).json()
    )

    for code in (class_group["join_code"], tuition["join_code"]):
        assert (
            api("riya").post("/api/v1/groups/join", json={"code": code}).status_code
            == 200
        )

    joined = api("riya").get("/api/v1/groups/joined").json()
    assert {g["name"] for g in joined} == {"9-A", "Tuition"}


def test_an_educator_cannot_join_a_group(api):
    group = create_group(api)

    response = api("other-teacher").post(
        "/api/v1/groups/join", json={"code": group["join_code"]}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("code", ["ZZZZZZ", "abc", "O0O0O0", "K7QX2M-EXTRA"])
def test_unknown_or_malformed_codes_are_not_found(api, code):
    create_group(api)

    assert (
        api("riya").post("/api/v1/groups/join", json={"code": code}).status_code == 404
    )
    assert api("riya").get(f"/api/v1/groups/invite/{code}").status_code == 404


def test_another_educator_cannot_see_or_change_a_group(api):
    group = create_group(api)
    api("riya").post("/api/v1/groups/join", json={"code": group["join_code"]})
    gid = group["group_id"]
    intruder = api("other-teacher")

    assert intruder.get(f"/api/v1/groups/{gid}").status_code == 404
    assert (
        intruder.patch(f"/api/v1/groups/{gid}", json={"name": "x"}).status_code == 404
    )
    assert intruder.post(f"/api/v1/groups/{gid}/join-code").status_code == 404
    assert intruder.delete(f"/api/v1/groups/{gid}/members/riya").status_code == 404
    assert intruder.get("/api/v1/groups").json() == []
    assert api("teacher").get(f"/api/v1/groups/{gid}").json()["member_count"] == 1


def test_a_member_student_cannot_read_the_roster(api):
    group = create_group(api)
    api("riya").post("/api/v1/groups/join", json={"code": group["join_code"]})

    assert api("riya").get(f"/api/v1/groups/{group['group_id']}").status_code == 404


def test_replacing_the_code_stops_the_old_link(api):
    group = create_group(api)

    replaced = api("teacher").post(f"/api/v1/groups/{group['group_id']}/join-code")

    assert replaced.status_code == 200
    new_code = replaced.json()["join_code"]
    assert new_code != group["join_code"]
    old = api("riya").post("/api/v1/groups/join", json={"code": group["join_code"]})
    assert old.status_code == 404
    assert (
        api("riya").post("/api/v1/groups/join", json={"code": new_code}).status_code
        == 200
    )


def test_the_owner_renames_a_group(api):
    group = create_group(api)

    response = api("teacher").patch(
        f"/api/v1/groups/{group['group_id']}", json={"name": "  9-A Science  "}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "9-A Science"


@pytest.mark.parametrize(
    "body",
    [{"name": ""}, {"name": "x" * 61}, {"name": "a‮b"}, {"name": "ok", "owner_id": "x"}],
)
def test_group_names_are_validated(api, body):
    assert api("teacher").post("/api/v1/groups", json=body).status_code == 422


def test_the_owner_removes_a_student_and_the_student_can_leave(api):
    group = create_group(api)
    gid = group["group_id"]
    for student in ("riya", "aman"):
        api(student).post("/api/v1/groups/join", json={"code": group["join_code"]})

    assert (
        api("teacher").delete(f"/api/v1/groups/{gid}/members/riya").status_code == 204
    )
    assert api("aman").delete(f"/api/v1/groups/joined/{gid}").status_code == 204

    assert api("teacher").get(f"/api/v1/groups/{gid}").json()["members"] == []
    assert api("riya").get("/api/v1/groups/joined").json() == []
    assert api("aman").delete(f"/api/v1/groups/joined/{gid}").status_code == 404


def test_joining_is_rate_limited(api):
    create_group(api)

    statuses = [
        api("riya").post("/api/v1/groups/join", json={"code": "ZZZZZZ"}).status_code
        for _ in range(6)
    ]

    assert statuses[:5] == [404] * 5
    assert statuses[5] == 429


def test_names_and_codes_are_never_logged(api, caplog):
    # The level the service runs at; DEBUG would include the AWS SDK's own wire logs.
    caplog.set_level(logging.INFO)
    group = create_group(api, "Secret Class Name")
    api("riya").post("/api/v1/groups/join", json={"code": group["join_code"]})
    api("riya").post("/api/v1/groups/join", json={"code": "ZZZZZZ"})

    for private in ("Secret Class Name", group["join_code"], "Riya Verma", "Meera"):
        assert private not in caplog.text


def test_the_owner_deletes_a_group_and_its_link_stops_working(api):
    group = create_group(api)
    gid = group["group_id"]
    api("riya").post("/api/v1/groups/join", json={"code": group["join_code"]})

    assert api("other-teacher").delete(f"/api/v1/groups/{gid}").status_code == 404
    assert api("teacher").delete(f"/api/v1/groups/{gid}").status_code == 204

    assert api("teacher").get("/api/v1/groups").json() == []
    assert api("riya").get("/api/v1/groups/joined").json() == []
    joined = api("aman").post("/api/v1/groups/join", json={"code": group["join_code"]})
    assert joined.status_code == 404
