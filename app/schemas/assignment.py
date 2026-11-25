"""Request and response shapes for vivas set for a group."""

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.db.models import VivaFeedback
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
    # The mean score of those who finished it, to one decimal place.
    average_score: float | None = None


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


# ------------------------------------------------------- what the owner reads


class StudentResultRow(BaseModel):
    """One student against one set viva. Students who left but finished stay."""

    student_id: str
    name: str
    status: Literal["done", "not_started"]
    in_group: bool
    score: int | None = None
    completed_at: datetime.datetime | None = None


class AssignmentResultsResponse(BaseModel):
    assignment: AssignmentResponse
    average_score: float | None
    students: list[StudentResultRow]


class StudentReportResponse(BaseModel):
    """A student's report as the owner reads it. There is never a transcript."""

    student_id: str
    student_name: str
    in_group: bool
    assignment_id: str
    title: str
    curriculum_selection: CurriculumSelection
    feedback: VivaFeedback
    completed_at: datetime.datetime


class ScoreBands(BaseModel):
    """How many results fell in each band: 0-4, 5-7 and 8-10."""

    low: int = 0
    middle: int = 0
    high: int = 0


class StudentSummary(BaseModel):
    student_id: str
    name: str
    completed: int
    average_score: float | None
    latest_score: int | None
    # Vivas past their due day that the student has not done.
    overdue: int
    needs_attention: bool


class AssignmentSummary(BaseModel):
    assignment_id: str
    title: str
    due_on: datetime.date | None
    completed: int
    average_score: float | None


class ClassSummaryResponse(BaseModel):
    """The class at a glance: computed, not written by a model."""

    member_count: int
    assignment_count: int
    # Finished vivas by current members, out of members x vivas set.
    completed: int
    expected: int
    average_score: float | None
    score_bands: ScoreBands
    students: list[StudentSummary]
    assignments: list[AssignmentSummary]
