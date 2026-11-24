import asyncio
import datetime
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from app.db.models import VivaSession, new_session_id, utc_now
from app.db.profile_repository import ProfileRepository, UserProfile
from app.db.session_repository import RepositoryUnavailable, SessionRepository

TABLE = "test-sessions"


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
    return ProfileRepository(client, TABLE)


def profile(**changes):
    values = {
        "name": "Asha Rao",
        "class_level": 9,
        "board": "CBSE",
        "school_name": "Hill View School",
        "school_address": "12 Lake Road, Pune",
    }
    return UserProfile(**{**values, **changes})


def test_missing_profile_reads_as_none(repository):
    assert asyncio.run(repository.get("owner")) is None


def test_saved_profile_round_trips_with_timestamps(repository):
    saved = asyncio.run(repository.save("owner", profile()))
    loaded = asyncio.run(repository.get("owner"))

    assert loaded == saved
    assert loaded.name == "Asha Rao"
    assert loaded.class_level == 9
    assert loaded.board == "CBSE"
    assert loaded.school_name == "Hill View School"
    assert loaded.school_address == "12 Lake Road, Pune"
    assert loaded.created_at is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at == loaded.created_at


def test_optional_school_details_can_be_absent(repository):
    asyncio.run(
        repository.save("owner", profile(school_name=None, school_address=None))
    )
    loaded = asyncio.run(repository.get("owner"))

    assert loaded.school_name is None
    assert loaded.school_address is None


def test_board_can_be_left_for_later(repository):
    asyncio.run(repository.save("owner", profile(board=None)))

    assert asyncio.run(repository.get("owner")).board is None


def test_saving_again_replaces_details_but_keeps_the_creation_time(repository):
    first = asyncio.run(repository.save("owner", profile()))
    second = asyncio.run(
        repository.save("owner", profile(class_level=10, school_name=None))
    )

    assert second.class_level == 10
    assert second.school_name is None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert asyncio.run(repository.get("owner")) == second


def test_a_client_supplied_timestamp_cannot_backdate_the_profile(repository):
    forged = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
    saved = asyncio.run(
        repository.save("owner", profile(created_at=forged, updated_at=forged))
    )

    assert saved.created_at > forged
    assert saved.updated_at > forged


def test_profiles_are_isolated_per_user(repository):
    asyncio.run(repository.save("owner", profile()))

    assert asyncio.run(repository.get("someone-else")) is None


def test_profile_is_stored_under_the_users_partition(repository, client):
    asyncio.run(repository.save("owner", profile()))

    item = client.get_item(
        TableName=TABLE, Key={"pk": {"S": "USER#owner"}, "sk": {"S": "PROFILE"}}
    )["Item"]
    assert set(item) == {"pk", "sk", "data", "created_at", "updated_at"}


def test_profile_never_appears_in_session_history(repository, client):
    """History lists SESSION# items only; a profile in the same partition is not one."""
    sessions = SessionRepository(client, TABLE)
    now = utc_now()
    session = VivaSession(
        id=new_session_id(now),
        user_id="owner",
        student_name="Asha",
        title="Light",
        topic="Light",
        class_level="9",
        started_at=now,
    )
    asyncio.run(sessions.create_session(session))
    asyncio.run(repository.save("owner", profile()))

    listed, cursor = asyncio.run(sessions.list_sessions_for_user("owner"))

    assert [item.id for item in listed] == [session.id]
    assert cursor is None


@pytest.mark.parametrize(
    "error",
    [
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
        ),
        EndpointConnectionError(endpoint_url="https://private-endpoint"),
    ],
)
def test_storage_failures_are_reported_without_provider_details(error):
    client = MagicMock()
    client.get_item.side_effect = error
    client.update_item.side_effect = error
    repository = ProfileRepository(client, TABLE)

    for operation in (repository.get("owner"), repository.save("owner", profile())):
        with pytest.raises(RepositoryUnavailable) as caught:
            asyncio.run(operation)
        assert caught.value.__cause__ is None
        assert "private-endpoint" not in str(caught.value)
        assert "Throughput" not in str(caught.value)


def test_profiles_saved_before_account_types_read_as_students(repository, client):
    client.put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "USER#legacy"},
            "sk": {"S": "PROFILE"},
            "data": {"S": '{"name": "Asha Rao", "class_level": 9}'},
            "created_at": {"S": "2026-11-21T10:30:00+00:00"},
            "updated_at": {"S": "2026-11-21T10:30:00+00:00"},
        },
    )

    loaded = asyncio.run(repository.get("legacy"))

    assert loaded.account_type == "student"
    assert loaded.class_level == 9


def test_a_parent_profile_round_trips_without_a_class(repository):
    parent = UserProfile(name="Meera", account_type="parent")

    asyncio.run(repository.save("owner", parent))
    loaded = asyncio.run(repository.get("owner"))

    assert (loaded.account_type, loaded.class_level) == ("parent", None)
