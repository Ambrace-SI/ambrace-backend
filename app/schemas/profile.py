"""Request and response shapes for the signed-in user's profile (/api/v1/me)."""

import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.db.profile_repository import (
    MAX_CLASS_LEVEL,
    MIN_CLASS_LEVEL,
    AccountType,
    Board,
)
from app.schemas.text import reject_control_characters


class ProfileUpdateRequest(BaseModel):
    """Everything onboarding collects. Sending it again replaces the profile.

    A student needs a name and a class: they are what a viva needs. A teacher
    or parent needs only a name; their classes belong to their groups. The
    board and the school can be added later, so nobody is blocked.

    The user id is NOT part of this request: it comes from the verified token.
    Unknown fields are rejected so a client cannot set server-owned values.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    # Defaults to student so clients written before educators existed keep working.
    account_type: AccountType = "student"
    class_level: int | None = Field(
        default=None, ge=MIN_CLASS_LEVEL, le=MAX_CLASS_LEVEL, strict=True
    )
    board: Board | None = None
    school_name: Annotated[str, StringConstraints(max_length=150)] | None = None
    school_address: Annotated[str, StringConstraints(max_length=300)] | None = None

    @field_validator("name", "school_name", mode="after")
    @classmethod
    def single_line_text(cls, value: str | None) -> str | None:
        return None if value is None else reject_control_characters(value)

    @field_validator("school_address", mode="after")
    @classmethod
    def multi_line_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return reject_control_characters(value, allow_newline=True)

    @model_validator(mode="after")
    def fits_account_type(self) -> Self:
        if self.account_type == "student" and self.class_level is None:
            raise ValueError("class_level is required for a student")
        if self.account_type != "student" and self.class_level is not None:
            raise ValueError("class_level applies only to a student")
        return self

    @field_validator("school_name", "school_address", mode="after")
    @classmethod
    def blank_means_absent(cls, value: str | None) -> str | None:
        """An empty optional box in a form is the same as leaving it out."""
        if value is None:
            return None
        return value.strip() or None


class ProfileResponse(BaseModel):
    """The profile as the API publishes it.

    Deliberately separate from the stored model: a field added to storage later
    (a role, a consent record) is not published until it is added here.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    account_type: AccountType
    class_level: int | None
    board: Board | None
    school_name: str | None
    school_address: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class MeResponse(BaseModel):
    """The signed-in user and their profile, if onboarding has been completed."""

    user_id: str
    onboarding_complete: bool
    profile: ProfileResponse | None
