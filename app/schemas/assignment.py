"""Request and response shapes for vivas set for a group."""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.db.profile_repository import EducatorType
from app.domain.curriculum import CurriculumSelection
from app.schemas.viva import CurriculumSelectionRequest


class AssignmentCreateRequest(BaseModel):
    """One chapter (and optional topics), exactly as a student would pick it."""

    model_config = ConfigDict(extra="forbid")

    curriculum_selection: CurriculumSelectionRequest
    due_on: datetime.date | None = None

    @field_validator("curriculum_selection", mode="after")
    @classmethod
    def one_chapter(
        cls, value: CurriculumSelectionRequest
    ) -> CurriculumSelectionRequest:
        if len(value.chapters) != 1:
            raise ValueError("Select exactly one chapter for a viva")
        return value


class AssignmentResponse(BaseModel):
    """A set viva as its owner sees it, with how many have finished it."""

    assignment_id: str
    title: str
    curriculum_selection: CurriculumSelection
    due_on: datetime.date | None
    created_at: datetime.datetime
    completed_count: int


class StudentAssignmentResponse(BaseModel):
    """A set viva as a student sees it: who set it, and whether they have done it."""

    assignment_id: str
    group_id: str
    group_name: str
    owner_name: str
    owner_type: EducatorType
    title: str
    subject_name: str
    # What the viva covers, for the live screen and the student's report.
    curriculum_selection: CurriculumSelection
    due_on: datetime.date | None
    created_at: datetime.datetime
    status: Literal["pending", "done"]
    # Present once done: their score, and the session whose report they can open.
    score: int | None = None
    session_id: str | None = None
