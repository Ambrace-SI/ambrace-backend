import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_profile_service
from app.core.auth import get_current_user
from app.db.profile_repository import UserProfile
from app.db.session_repository import RepositoryUnavailable
from app.main import app
from app.services.profile_service import ProfileService

NOW = datetime.datetime(2026, 11, 21, 10, 30, tzinfo=datetime.UTC)
VALID = {
    "name": "Asha Rao",
    "class_level": 9,
    "board": "CBSE",
    "school_name": "Hill View School",
    "school_address": "12 Lake Road, Pune",
}


def stored(**changes):
    return UserProfile(**{**VALID, "created_at": NOW, "updated_at": NOW, **changes})


@pytest.fixture
def repository():
    """The real service over a fake repository, signed in as `owner`."""
    repository = SimpleNamespace(get=AsyncMock(return_value=None), save=AsyncMock())
    # No groups unless a test says otherwise.
    repository.groups = SimpleNamespace(
        list_owned_group_ids=AsyncMock(return_value=[]),
        list_memberships=AsyncMock(return_value=[]),
        update_owner_details=AsyncMock(),
        update_member_details=AsyncMock(),
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="owner"
    )
    app.dependency_overrides[get_profile_service] = lambda: ProfileService(
        repository, repository.groups
    )
    yield repository
    app.dependency_overrides.clear()


@pytest.mark.parametrize("method", ["get", "put"])
def test_profile_requires_authentication_before_touching_storage(method):
    response = TestClient(app).request(method, "/api/v1/me", json=VALID)

    assert response.status_code == 401


def test_new_user_has_no_profile_and_onboarding_is_pending(repository):
    repository.get.return_value = None

    response = TestClient(app).get("/api/v1/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "owner",
        "onboarding_complete": False,
        "profile": None,
    }
    repository.get.assert_awaited_once_with("owner")


def test_existing_profile_is_returned_and_onboarding_is_complete(repository):
    repository.get.return_value = stored()

    response = TestClient(app).get("/api/v1/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "owner",
        "onboarding_complete": True,
        "profile": {
            **VALID,
            "account_type": "student",
            "created_at": "2026-11-21T10:30:00Z",
            "updated_at": "2026-11-21T10:30:00Z",
        },
    }


def test_saving_a_profile_uses_the_token_identity_and_completes_onboarding(repository):
    repository.save.return_value = stored()

    response = TestClient(app).put("/api/v1/me", json=VALID)

    assert response.status_code == 200
    assert response.json()["onboarding_complete"] is True
    assert response.json()["profile"]["name"] == "Asha Rao"
    user_id, saved = repository.save.await_args.args
    assert user_id == "owner"
    assert saved.model_dump(exclude={"created_at", "updated_at"}) == {
        **VALID,
        "account_type": "student",
    }


def test_school_details_are_optional_and_blank_text_means_absent(repository):
    repository.save.return_value = stored(school_name=None, school_address=None)

    for body in (
        {"name": "Asha Rao", "class_level": 9, "board": "CBSE"},
        {**VALID, "school_name": "   ", "school_address": ""},
    ):
        assert TestClient(app).put("/api/v1/me", json=body).status_code == 200
        saved = repository.save.await_args.args[1]
        assert saved.school_name is None
        assert saved.school_address is None


def test_only_name_and_class_are_needed_to_complete_onboarding(repository):
    repository.save.return_value = stored(
        board=None, school_name=None, school_address=None
    )

    response = TestClient(app).put(
        "/api/v1/me", json={"name": "Asha Rao", "class_level": 9}
    )

    assert response.status_code == 200
    assert response.json()["onboarding_complete"] is True
    assert response.json()["profile"]["board"] is None
    assert repository.save.await_args.args[1].board is None


def test_an_explicit_null_board_means_not_chosen(repository):
    repository.save.return_value = stored(board=None)

    response = TestClient(app).put("/api/v1/me", json={**VALID, "board": None})

    assert response.status_code == 200
    assert repository.save.await_args.args[1].board is None


def test_name_is_trimmed(repository):
    repository.save.return_value = stored()

    TestClient(app).put("/api/v1/me", json={**VALID, "name": "  Asha Rao  "})

    assert repository.save.await_args.args[1].name == "Asha Rao"


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"class_level": 4},
        {"class_level": 13},
        {"class_level": "9"},
        {"class_level": 9.5},
        {"board": "Hogwarts"},
        {"board": "cbse"},
        {"school_name": "x" * 151},
        {"school_address": "x" * 301},
        # Identity and timestamps come from the server, never from the client.
        {"user_id": "someone-else"},
        {"created_at": "2000-01-01T00:00:00Z"},
        {"onboarding_complete": True},
    ],
)
def test_invalid_or_unexpected_fields_are_rejected(repository, change):
    response = TestClient(app).put("/api/v1/me", json={**VALID, **change})

    assert response.status_code == 422
    repository.save.assert_not_awaited()


@pytest.mark.parametrize("missing", ["name", "class_level"])
def test_required_fields_cannot_be_omitted(repository, missing):
    body = {key: value for key, value in VALID.items() if key != missing}

    assert TestClient(app).put("/api/v1/me", json=body).status_code == 422
    repository.save.assert_not_awaited()


@pytest.mark.parametrize("method", ["get", "put"])
def test_storage_outage_is_a_retryable_503_without_details(repository, method, caplog):
    error = RepositoryUnavailable("private-data")
    repository.get.side_effect = error
    repository.save.side_effect = error

    response = TestClient(app).request(method, "/api/v1/me", json=VALID)

    assert response.status_code == 503
    assert "private-data" not in response.text + caplog.text


@pytest.mark.parametrize("method", ["get", "put"])
def test_unexpected_failure_is_a_500_that_leaks_no_profile_data(
    repository, method, caplog
):
    error = RuntimeError("Asha Rao lives at 12 Lake Road")
    repository.get.side_effect = error
    repository.save.side_effect = error

    response = TestClient(app, raise_server_exceptions=False).request(
        method, "/api/v1/me", json=VALID
    )

    assert response.status_code == 500
    assert "Lake Road" not in response.text + caplog.text


def test_profile_text_is_never_logged(repository, caplog):
    repository.save.return_value = stored()
    repository.get.return_value = stored()
    caplog.set_level("DEBUG")

    TestClient(app).put("/api/v1/me", json=VALID)
    TestClient(app).get("/api/v1/me")

    for private in ("Asha Rao", "Hill View School", "Lake Road"):
        assert private not in caplog.text


@pytest.mark.parametrize("method", ["get", "put"])
def test_personal_data_responses_are_never_cached(repository, method):
    repository.get.return_value = stored()
    repository.save.return_value = stored()

    response = TestClient(app).request(method, "/api/v1/me", json=VALID)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Asha\nRao"},
        {"name": "Asha\x00Rao"},
        {"name": "Asha‮Rao"},
        {"school_name": "Hill\tView"},
        {"school_address": "12 Lake Road\x1b[31m"},
    ],
)
def test_control_characters_are_rejected(repository, change):
    response = TestClient(app).put("/api/v1/me", json={**VALID, **change})

    assert response.status_code == 422
    repository.save.assert_not_awaited()


def test_names_in_indian_scripts_and_multi_line_addresses_are_accepted(repository):
    repository.save.return_value = stored()
    body = {
        **VALID,
        "name": "आशा राव",
        "school_name": "கேந்திரிய வித்யாலயா",
        "school_address": "12 Lake Road\nPune 411001",
    }

    assert TestClient(app).put("/api/v1/me", json=body).status_code == 200
    saved = repository.save.await_args.args[1]
    assert saved.name == "आशा राव"
    assert saved.school_address == "12 Lake Road\nPune 411001"


def test_a_field_added_to_storage_is_not_published_until_the_api_declares_it(
    repository,
):
    class StoredWithInternalField(UserProfile):
        internal_note: str = "staff only"

    repository.get.return_value = StoredWithInternalField(
        **VALID, created_at=NOW, updated_at=NOW
    )

    profile = TestClient(app).get("/api/v1/me").json()["profile"]

    assert "internal_note" not in profile
    assert profile["name"] == "Asha Rao"


EDUCATOR = {
    "name": "Meera Sharma",
    "account_type": "teacher",
}


def test_an_educator_completes_onboarding_with_a_name_and_kind_only(repository):
    repository.save.return_value = stored(
        **EDUCATOR, class_level=None, board=None, school_name=None, school_address=None
    )

    response = TestClient(app).put("/api/v1/me", json=EDUCATOR)

    assert response.status_code == 200
    assert response.json()["onboarding_complete"] is True
    assert response.json()["profile"]["account_type"] == "teacher"
    assert response.json()["profile"]["class_level"] is None
    saved = repository.save.await_args.args[1]
    assert (saved.account_type, saved.class_level) == ("teacher", None)


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Asha Rao"},  # a student without a class
        {**VALID, "account_type": "teacher"},  # a teacher with a personal class
        {**VALID, "account_type": "educator"},
        {**EDUCATOR, "class_level": 9},  # an educator with a personal class
        {**EDUCATOR, "account_type": "principal"},
        {**VALID, "account_type": "admin"},
        {**EDUCATOR, "educator_kind": "teacher"},  # the old two-field shape
    ],
)
def test_profile_fields_must_fit_the_account_type(repository, body):
    response = TestClient(app).put("/api/v1/me", json=body)

    assert response.status_code == 422
    repository.save.assert_not_awaited()


TEACHER = stored(**EDUCATOR, class_level=None)
PARENT_BODY = {**EDUCATOR, "account_type": "parent"}


@pytest.mark.parametrize(
    "existing,body,saved",
    [
        (stored(), EDUCATOR, TEACHER),
        (TEACHER, VALID, stored()),
        (TEACHER, PARENT_BODY, stored(**PARENT_BODY, class_level=None)),
    ],
)
def test_the_account_type_can_be_changed_when_nothing_depends_on_it(
    repository, existing, body, saved
):
    repository.get.return_value = existing
    repository.save.return_value = saved

    response = TestClient(app).put("/api/v1/me", json=body)

    assert response.status_code == 200
    assert response.json()["profile"]["account_type"] == saved.account_type


def test_a_student_in_a_group_must_leave_before_becoming_a_teacher(repository):
    repository.get.return_value = stored()
    repository.groups.list_memberships.return_value = [object()]

    response = TestClient(app).put("/api/v1/me", json=EDUCATOR)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Leave your groups before switching to a teacher or parent account."
    )
    repository.save.assert_not_awaited()


def test_a_teacher_with_groups_must_delete_them_before_becoming_a_student(
    repository,
):
    repository.get.return_value = TEACHER
    repository.groups.list_owned_group_ids.return_value = ["g1"]

    response = TestClient(app).put("/api/v1/me", json=VALID)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Delete your groups before switching to a student account."
    )
    repository.save.assert_not_awaited()


def test_a_teacher_with_groups_can_become_a_parent_and_students_see_it(
    repository,
):
    repository.get.return_value = TEACHER
    repository.save.return_value = stored(**PARENT_BODY, class_level=None)
    repository.groups.list_owned_group_ids.return_value = ["g1", "g2"]

    response = TestClient(app).put("/api/v1/me", json=PARENT_BODY)

    assert response.status_code == 200
    updated = repository.groups.update_owner_details.await_args_list
    assert [call.args for call in updated] == [
        ("g1", "owner", "Meera Sharma", "parent"),
        ("g2", "owner", "Meera Sharma", "parent"),
    ]


def test_a_student_who_renames_is_renamed_in_their_groups(repository):
    from app.db.group_repository import Membership

    repository.get.return_value = stored()
    repository.save.return_value = stored(name="Asha R", class_level=10)
    repository.groups.list_memberships.return_value = [
        Membership(group_id="g1", joined_at=NOW)
    ]

    response = TestClient(app).put(
        "/api/v1/me", json={**VALID, "name": "Asha R", "class_level": 10}
    )

    assert response.status_code == 200
    repository.groups.update_member_details.assert_awaited_once_with(
        "g1", "owner", "Asha R", 10
    )


@pytest.mark.parametrize("account_type", ["teacher", "parent"])
def test_a_teacher_or_parent_can_create_groups_a_student_cannot(account_type):
    from app.db.profile_repository import is_educator

    assert is_educator(account_type) is True
    assert is_educator("student") is False


def test_an_existing_user_can_still_edit_their_profile(repository):
    repository.get.return_value = stored()
    repository.save.return_value = stored(name="Asha R")

    response = TestClient(app).put("/api/v1/me", json={**VALID, "name": "Asha R"})

    assert response.status_code == 200
