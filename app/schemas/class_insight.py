"""The class insight as a teacher or parent reads it."""

import datetime
from typing import Literal

from pydantic import BaseModel


class InsightStudent(BaseModel):
    student_id: str
    name: str


class InsightTheme(BaseModel):
    point: str
    # Counted by the server from the students listed, never taken from the model.
    student_count: int
    students: list[InsightStudent]
    misconception: str | None = None


class StudentToSupport(BaseModel):
    student: InsightStudent
    reason: str


class TeachingSuggestion(BaseModel):
    action: str
    addresses: str | None = None


class ClassInsight(BaseModel):
    overview: str
    strengths: list[InsightTheme]
    gaps: list[InsightTheme]
    students_to_support: list[StudentToSupport]
    suggestions: list[TeachingSuggestion]
    coverage_note: str
    # How many students' reports it was written from.
    based_on: int
    generator_id: str
    generated_at: datetime.datetime


class ClassInsightResponse(BaseModel):
    status: Literal["not_enough_results", "ready_to_generate", "ready"]
    # Finished vivas by students now in the group, out of member_count.
    results_count: int
    member_count: int
    min_results: int
    # Results that arrived after the insight was written, or its sources changed.
    is_stale: bool = False
    new_results: int = 0
    insight: ClassInsight | None = None
