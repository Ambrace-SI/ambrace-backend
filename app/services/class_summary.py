"""The class at a glance, computed from one group's members, vivas and results.

Pure arithmetic, no I/O, so every number a teacher sees is easy to check.
The picture is of the class as it is now: results of students who have left
stay in each viva's results, but not in these totals.
"""

import datetime
from collections import defaultdict
from statistics import fmean

from app.db.assignment_repository import Assignment, AssignmentResult
from app.db.group_repository import GroupMember
from app.schemas.assignment import (
    AssignmentSummary,
    ClassSummaryResponse,
    ScoreBands,
    StudentSummary,
)

# An average below this, out of 10, asks for a closer look.
NEEDS_ATTENTION_BELOW = 5.0


def average(scores: list[int]) -> float | None:
    """A mean to one decimal place, or None when there is nothing to average."""
    return round(fmean(scores), 1) if scores else None


def _band(score: int) -> str:
    if score <= 4:
        return "low"
    return "middle" if score <= 7 else "high"


def summarise(
    members: list[GroupMember],
    assignments: list[Assignment],
    results: list[AssignmentResult],
    today: datetime.date,
) -> ClassSummaryResponse:
    member_ids = {m.student_id for m in members}
    assignment_ids = {a.assignment_id for a in assignments}
    current = [
        r
        for r in results
        if r.student_id in member_ids and r.assignment_id in assignment_ids
    ]

    by_student: dict[str, list[AssignmentResult]] = defaultdict(list)
    by_assignment: dict[str, list[int]] = defaultdict(list)
    bands = ScoreBands()
    for r in current:
        by_student[r.student_id].append(r)
        by_assignment[r.assignment_id].append(r.feedback.score)
        band = _band(r.feedback.score)
        setattr(bands, band, getattr(bands, band) + 1)

    students = [
        _student(m, by_student[m.student_id], assignments, today) for m in members
    ]
    # Who needs a look first, then by name, so the list reads like a register.
    students.sort(key=lambda s: (not s.needs_attention, s.name.casefold()))

    return ClassSummaryResponse(
        member_count=len(members),
        assignment_count=len(assignments),
        completed=len(current),
        expected=len(members) * len(assignments),
        average_score=average([r.feedback.score for r in current]),
        score_bands=bands,
        students=students,
        assignments=[
            AssignmentSummary(
                assignment_id=a.assignment_id,
                title=a.title,
                due_on=a.due_on,
                completed=len(by_assignment[a.assignment_id]),
                average_score=average(by_assignment[a.assignment_id]),
            )
            for a in assignments
        ],
    )


def _student(
    member: GroupMember,
    done: list[AssignmentResult],
    assignments: list[Assignment],
    today: datetime.date,
) -> StudentSummary:
    done_ids = {r.assignment_id for r in done}
    joined = member.joined_at.date()
    # Overdue only if it was due after they joined and the day has passed.
    overdue = sum(
        1
        for a in assignments
        if a.due_on is not None
        and joined <= a.due_on < today
        and a.assignment_id not in done_ids
    )
    mean = average([r.feedback.score for r in done])
    latest = max(done, key=lambda r: (r.completed_at, r.assignment_id), default=None)
    return StudentSummary(
        student_id=member.student_id,
        name=member.name,
        completed=len(done),
        average_score=mean,
        latest_score=latest.feedback.score if latest else None,
        overdue=overdue,
        needs_attention=overdue > 0
        or (mean is not None and mean < NEEDS_ATTENTION_BELOW),
    )
