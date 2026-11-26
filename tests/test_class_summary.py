"""The class summary is plain arithmetic over what one group query returns."""

import datetime

from app.db.assignment_repository import Assignment, AssignmentResult
from app.db.group_repository import GroupMember
from app.db.models import VivaFeedback
from app.domain.curriculum import CurriculumSelection
from app.services.class_summary import summarise

TODAY = datetime.date(2026, 11, 24)
JOINED = datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC)


def member(student_id, name, joined=JOINED):
    return GroupMember(
        student_id=student_id, name=name, class_level=9, joined_at=joined
    )


def viva(assignment_id, title, due_on=None):
    return Assignment(
        assignment_id=assignment_id,
        group_id="g",
        title=title,
        curriculum_selection=CurriculumSelection.model_validate(
            {
                "catalog_id": "ncert-cbse",
                "class_level": 9,
                "subject_id": "science",
                "subject_name": "Science",
                "chapters": [{"id": "motion", "name": title, "topics": []}],
            }
        ),
        due_on=due_on,
        created_at=JOINED,
    )


def result(assignment_id, student_id, score, name=None):
    return AssignmentResult(
        assignment_id=assignment_id,
        student_id=student_id,
        student_name=name,
        session_id=f"s-{assignment_id}-{student_id}",
        feedback=VivaFeedback(score=score, summary="-"),
        completed_at=datetime.datetime(2026, 11, 20, tzinfo=datetime.UTC),
    )


RIYA = member("riya", "Riya Verma")
AMAN = member("aman", "Aman Gupta")
ZOYA = member("zoya", "Zoya Khan")


def test_an_empty_group_has_nothing_to_average():
    summary = summarise([], [], [], TODAY)

    assert summary.member_count == 0
    assert summary.completed == summary.expected == 0
    assert summary.average_score is None
    assert summary.students == summary.assignments == []


def test_completion_average_and_bands_cover_current_members():
    summary = summarise(
        [RIYA, AMAN, ZOYA],
        [viva("a2", "Force"), viva("a1", "Motion")],
        [
            result("a1", "riya", 9),
            result("a1", "aman", 3),
            result("a2", "riya", 6),
            # Someone who left: their result is history, not the class picture.
            result("a1", "gone", 0, name="Left Student"),
        ],
        TODAY,
    )

    assert (summary.completed, summary.expected) == (3, 6)
    assert summary.average_score == 6.0
    assert summary.score_bands.model_dump() == {"low": 1, "middle": 1, "high": 1}
    assert [(a.title, a.completed, a.average_score) for a in summary.assignments] == [
        ("Force", 1, 6.0),
        ("Motion", 2, 6.0),
    ]


def test_low_averages_and_overdue_vivas_need_attention_and_come_first():
    summary = summarise(
        [RIYA, AMAN, ZOYA],
        [viva("a1", "Motion", due_on=TODAY - datetime.timedelta(days=1))],
        [result("a1", "riya", 8), result("a1", "aman", 4)],
        TODAY,
    )

    rows = [
        (s.name, s.completed, s.average_score, s.overdue, s.needs_attention)
        for s in summary.students
    ]
    assert rows == [
        ("Aman Gupta", 1, 4.0, 0, True),
        ("Zoya Khan", 0, None, 1, True),
        ("Riya Verma", 1, 8.0, 0, False),
    ]


def test_a_viva_due_today_or_before_someone_joined_is_not_overdue():
    late_joiner = member(
        "late",
        "Late Joiner",
        joined=datetime.datetime(2026, 11, 23, tzinfo=datetime.UTC),
    )
    summary = summarise(
        [RIYA, late_joiner],
        [
            viva("a1", "Motion", due_on=TODAY),
            viva("a2", "Force", due_on=datetime.date(2026, 11, 22)),
        ],
        [result("a2", "riya", 7)],
        TODAY,
    )

    assert {s.name: s.overdue for s in summary.students} == {
        "Late Joiner": 0,
        "Riya Verma": 0,
    }


def test_the_latest_score_is_the_most_recent_viva_done():
    summary = summarise(
        [RIYA],
        [viva("a2", "Force"), viva("a1", "Motion")],
        [result("a1", "riya", 9), result("a2", "riya", 5)],
        TODAY,
    )

    assert summary.students[0].latest_score == 5
    assert summary.students[0].average_score == 7.0
