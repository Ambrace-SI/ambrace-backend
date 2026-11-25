"""One profile item per user, in the same table and partition as their sessions.

Key: pk = USER#<user id>, sk = PROFILE. Session history only lists SESSION#
items, so the profile never appears there.
"""

import asyncio
import datetime
from typing import Any, Literal, TypeIs

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from app.db.models import utc_now
from app.db.session_repository import RepositoryUnavailable

Board = Literal["CBSE", "ICSE", "STATE_BOARD", "OTHER"]
# Who the person is, as onboarding asks it. Teachers and parents have the same
# powers (see is_educator); the difference only changes wording. A future
# school admin is a role inside an organisation, not another account type.
AccountType = Literal["student", "teacher", "parent"]
EducatorType = Literal["teacher", "parent"]


def is_educator(account_type: AccountType) -> TypeIs[EducatorType]:
    """The one place that decides who may own groups. Never check "teacher" alone."""
    return account_type != "student"


MIN_CLASS_LEVEL = 5
MAX_CLASS_LEVEL = 12


class UserProfile(BaseModel):
    """What a person tells us once, after signing in."""

    name: str
    # Profiles saved before account types existed are students.
    account_type: AccountType = "student"
    class_level: int | None = None
    # Optional: onboarding only insists on a name and a class.
    board: Board | None = None
    school_name: str | None = None
    school_address: str | None = None
    # Set by the repository. Values supplied by a caller are ignored on save.
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None


class ProfileRepository:
    """Read and replace the caller's own profile.

    The user id always comes from the verified token, never from a request
    body, so one user cannot read or overwrite another user's profile.
    """

    def __init__(self, client: Any, table_name: str):
        self.client = client
        self.table_name = table_name

    @staticmethod
    def _key(user_id: str) -> dict[str, Any]:
        return {"pk": {"S": f"USER#{user_id}"}, "sk": {"S": "PROFILE"}}

    @staticmethod
    def _decode(item: dict[str, Any]) -> UserProfile:
        profile = UserProfile.model_validate_json(item["data"]["S"])
        profile.created_at = datetime.datetime.fromisoformat(item["created_at"]["S"])
        profile.updated_at = datetime.datetime.fromisoformat(item["updated_at"]["S"])
        return profile

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        """Keep the synchronous SDK off the event loop and hide provider details."""
        try:
            return await asyncio.to_thread(
                getattr(self.client, operation), TableName=self.table_name, **kwargs
            )
        except (ClientError, BotoCoreError):
            raise RepositoryUnavailable("Profile storage is unavailable") from None

    async def get(self, user_id: str) -> UserProfile | None:
        response = await self._call(
            "get_item", Key=self._key(user_id), ConsistentRead=True
        )
        if "Item" not in response:
            return None
        return self._decode(response["Item"])

    async def save(self, user_id: str, profile: UserProfile) -> UserProfile:
        """Create or replace the profile in one atomic write.

        The first save records when the profile was created; later saves keep
        that time and only move updated_at. The latest save wins: a profile has
        a single owner, so there is no concurrent editor to protect against.
        """
        now = utc_now().isoformat()
        response = await self._call(
            "update_item",
            Key=self._key(user_id),
            UpdateExpression=(
                "SET #data = :data, updated_at = :now, "
                "created_at = if_not_exists(created_at, :now)"
            ),
            ExpressionAttributeNames={"#data": "data"},
            ExpressionAttributeValues={
                ":data": {
                    "S": profile.model_dump_json(exclude={"created_at", "updated_at"})
                },
                ":now": {"S": now},
            },
            ReturnValues="ALL_NEW",
        )
        return self._decode(response["Attributes"])
