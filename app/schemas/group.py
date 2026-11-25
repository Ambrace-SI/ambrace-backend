"""Request and response shapes for groups (/api/v1/groups)."""

import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.db.profile_repository import MAX_CLASS_LEVEL, MIN_CLASS_LEVEL, EducatorType
from app.schemas.text import reject_control_characters

GroupName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)
]


class GroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: GroupName
    # Optional: a parent's group often spans classes.
    class_level: int | None = Field(
        default=None, ge=MIN_CLASS_LEVEL, le=MAX_CLASS_LEVEL, strict=True
    )

    @field_validator("name", mode="after")
    @classmethod
    def printable(cls, value: str) -> str:
        return reject_control_characters(value)


class GroupRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: GroupName

    @field_validator("name", mode="after")
    @classmethod
    def printable(cls, value: str) -> str:
        return reject_control_characters(value)


class JoinGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Loose here; the service normalises spacing and case before checking it.
    code: Annotated[str, StringConstraints(min_length=1, max_length=16)]


class GroupSummaryResponse(BaseModel):
    """A group as its owner sees it in their list."""

    model_config = ConfigDict(from_attributes=True)

    group_id: str
    name: str
    class_level: int | None
    member_count: int
    join_code: str
    created_at: datetime.datetime


class GroupMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    student_id: str
    name: str
    class_level: int | None
    joined_at: datetime.datetime


class GroupDetailResponse(GroupSummaryResponse):
    members: list[GroupMemberResponse]


class InvitePreviewResponse(BaseModel):
    """What a student sees before joining. Never includes the roster."""

    group_name: str
    owner_name: str
    owner_type: EducatorType
    class_level: int | None
    # So the page can say "you're already in this group" instead of offering
    # to join again.
    already_member: bool


class JoinedGroupResponse(BaseModel):
    """A group as a member sees it. No roster and no code: those are the owner's."""

    group_id: str
    name: str
    owner_name: str
    owner_type: EducatorType
    class_level: int | None
    joined_at: datetime.datetime


class JoinGroupResponse(JoinedGroupResponse):
    """The group just joined. already_member is true when nothing changed."""

    already_member: bool
