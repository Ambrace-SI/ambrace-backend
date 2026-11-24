import asyncio
import datetime
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

from app.db.group_repository import (
    Group,
    GroupFull,
    GroupMember,
    GroupNotFound,
    GroupRepository,
    JoinCodeNotFound,
    JoinCodeTaken,
    MemberNotFound,
)
from app.db.session_repository import RepositoryUnavailable

TABLE = "test-sessions"
NOW = datetime.datetime(2026, 11, 24, 9, 0, tzinfo=datetime.UTC)


@pytest.fixture
def client():
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
        yield client
        client.close()


@pytest.fixture
def repository(client):
    return GroupRepository(client, TABLE)


def group(group_id="a" * 32, code="K7QX2M", **changes):
    values = {
        "group_id": group_id,
        "name": "Class 9-A",
        "owner_id": "teacher",
        "owner_name": "Meera Sharma",
        "owner_type": "teacher",
        "class_level": 9,
        "join_code": code,
        "created_at": NOW,
    }
    return Group(**{**values, **changes})


def member(student_id="riya", **changes):
    values = {
        "student_id": student_id,
        "name": "Riya",
        "class_level": 9,
        "joined_at": NOW,
    }
    return GroupMember(**{**values, **changes})


def run(coroutine):
    return asyncio.run(coroutine)


def test_a_created_group_is_listed_for_its_owner_and_found_by_code(repository):
    run(repository.create_group(group()))

    assert run(repository.get_group("a" * 32)) == group()
    assert run(repository.list_owned_group_ids("teacher")) == ["a" * 32]
    assert run(repository.find_group_id_by_code("K7QX2M")) == "a" * 32
    assert run(repository.list_owned_group_ids("someone-else")) == []


def test_a_colliding_join_code_creates_nothing(repository):
    run(repository.create_group(group()))

    with pytest.raises(JoinCodeTaken):
        run(repository.create_group(group(group_id="b" * 32)))

    assert run(repository.get_group("b" * 32)) is None
    assert run(repository.list_owned_group_ids("teacher")) == ["a" * 32]


def test_get_groups_keeps_the_requested_order_and_skips_missing(repository):
    run(repository.create_group(group(group_id="a" * 32, code="AAAAAA")))
    run(repository.create_group(group(group_id="b" * 32, code="BBBBBB", name="9-B")))

    found = run(repository.get_groups(["b" * 32, "c" * 32, "a" * 32]))

    assert [g.group_id for g in found] == ["b" * 32, "a" * 32]


def test_a_student_joins_once_and_both_sides_agree(repository):
    run(repository.create_group(group()))

    assert run(repository.add_member("a" * 32, "K7QX2M", member(), 200)) is True
    assert run(repository.add_member("a" * 32, "K7QX2M", member(), 200)) is False

    assert run(repository.list_members("a" * 32)) == [member()]
    assert [m.group_id for m in run(repository.list_memberships("riya"))] == ["a" * 32]
    assert run(repository.get_group("a" * 32)).member_count == 1


def test_a_student_can_belong_to_many_groups_and_a_group_to_many_students(repository):
    run(repository.create_group(group(group_id="a" * 32, code="AAAAAA")))
    run(repository.create_group(group(group_id="b" * 32, code="BBBBBB")))

    for gid, code in (("a" * 32, "AAAAAA"), ("b" * 32, "BBBBBB")):
        run(repository.add_member(gid, code, member("riya"), 200))
    run(repository.add_member("a" * 32, "AAAAAA", member("aman", name="Aman"), 200))

    assert {m.group_id for m in run(repository.list_memberships("riya"))} == {
        "a" * 32,
        "b" * 32,
    }
    assert {m.student_id for m in run(repository.list_members("a" * 32))} == {
        "riya",
        "aman",
    }
    assert run(repository.get_group("a" * 32)).member_count == 2


def test_a_full_group_refuses_new_members(repository):
    run(repository.create_group(group()))
    run(repository.add_member("a" * 32, "K7QX2M", member("riya"), 1))

    with pytest.raises(GroupFull):
        run(repository.add_member("a" * 32, "K7QX2M", member("aman"), 1))

    assert run(repository.list_memberships("aman")) == []


def test_a_replaced_code_stops_working_and_the_new_one_works(repository):
    run(repository.create_group(group()))

    run(repository.replace_join_code("a" * 32, "teacher", "K7QX2M", "P3RT9W"))

    assert run(repository.find_group_id_by_code("K7QX2M")) is None
    assert run(repository.find_group_id_by_code("P3RT9W")) == "a" * 32
    with pytest.raises(JoinCodeNotFound):
        run(repository.add_member("a" * 32, "K7QX2M", member(), 200))
    assert run(repository.add_member("a" * 32, "P3RT9W", member(), 200)) is True


def test_only_the_owner_can_replace_the_code_or_rename(repository):
    run(repository.create_group(group()))

    with pytest.raises(GroupNotFound):
        run(repository.replace_join_code("a" * 32, "intruder", "K7QX2M", "P3RT9W"))
    with pytest.raises(GroupNotFound):
        run(repository.rename_group("a" * 32, "intruder", "Mine now"))

    assert run(repository.find_group_id_by_code("K7QX2M")) == "a" * 32
    assert run(repository.find_group_id_by_code("P3RT9W")) is None
    assert run(repository.rename_group("a" * 32, "teacher", "9-A Science")).name == (
        "9-A Science"
    )


def test_removing_a_member_clears_both_sides(repository):
    run(repository.create_group(group()))
    run(repository.add_member("a" * 32, "K7QX2M", member(), 200))

    run(repository.remove_member("a" * 32, "riya"))

    assert run(repository.list_members("a" * 32)) == []
    assert run(repository.list_memberships("riya")) == []
    assert run(repository.get_group("a" * 32)).member_count == 0
    with pytest.raises(MemberNotFound):
        run(repository.remove_member("a" * 32, "riya"))
    assert run(repository.get_group("a" * 32)).member_count == 0


def test_storage_failures_are_hidden_behind_repository_unavailable():
    client = MagicMock()
    client.get_item.side_effect = EndpointConnectionError(endpoint_url="private")
    repository = GroupRepository(client, TABLE)

    with pytest.raises(RepositoryUnavailable):
        run(repository.get_group("a" * 32))


def test_deleting_a_group_removes_it_everywhere(repository):
    run(repository.create_group(group()))
    for student in ("riya", "aman"):
        run(repository.add_member("a" * 32, "K7QX2M", member(student), 200))

    run(repository.delete_group(group()))

    assert run(repository.get_group("a" * 32)) is None
    assert run(repository.list_owned_group_ids("teacher")) == []
    assert run(repository.find_group_id_by_code("K7QX2M")) is None
    assert run(repository.list_members("a" * 32)) == []
    assert run(repository.list_memberships("riya")) == []
    assert run(repository.list_memberships("aman")) == []


def test_only_the_owner_can_delete_a_group(repository):
    run(repository.create_group(group()))

    with pytest.raises(GroupNotFound):
        run(repository.delete_group(group(owner_id="intruder")))

    assert run(repository.get_group("a" * 32)) is not None


def test_names_shown_in_groups_follow_profile_changes(repository):
    run(repository.create_group(group()))
    run(repository.add_member("a" * 32, "K7QX2M", member(), 200))

    run(repository.update_owner_details("a" * 32, "teacher", "Mrs Sharma", "parent"))
    run(repository.update_member_details("a" * 32, "riya", "Riya V", 10))
    # A group the student has left, or one deleted meanwhile, is skipped.
    run(repository.update_member_details("a" * 32, "gone", "X", 9))
    run(repository.update_owner_details("b" * 32, "teacher", "X", "teacher"))

    loaded = run(repository.get_group("a" * 32))
    assert (loaded.owner_name, loaded.owner_type) == ("Mrs Sharma", "parent")
    assert run(repository.list_members("a" * 32))[0].name == "Riya V"
    assert run(repository.list_members("a" * 32))[0].class_level == 10
    assert len(run(repository.list_members("a" * 32))) == 1
