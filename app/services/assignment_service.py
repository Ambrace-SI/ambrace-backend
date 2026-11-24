"""Vivas a teacher or parent sets for a group, and how students take them.

- Only the group's owner can set, list or remove its vivas.
- Only a student in the group can see and start them. The viva is built on
  the server from the assignment, so a student cannot change its scope.
- The first finished viva per student is passed back to the owner (report
  only, never the transcript); later attempts are the student's practice.
"""

import datetime

from app.db.assignment_repository import (
    Assignment,
    AssignmentRepository,
    AssignmentResult,
)
from app.db.group_repository import Group, GroupNotFound, GroupRepository
from app.db.models import AssignmentRef, VivaSession, new_session_id, utc_now
from app.db.profile_repository import ProfileRepository, is_educator
from app.schemas.assignment import (
    AssignmentCreateRequest,
    AssignmentResponse,
    StudentAssignmentResponse,
)
from app.schemas.viva import CurriculumSelectionRequest, VivaStartRequest
from app.services.group_service import AccountTypeRequired
from app.services.viva_service import VivaService

MAX_ASSIGNMENTS_PER_GROUP = 100


class AssignmentInvalid(ValueError):
    """The request is well formed but does not fit this group; the message says why."""


class AssignmentResultRecorder:
    """Hands a finished viva's report to the assignment it was started from."""

    def __init__(self, assignments: AssignmentRepository):
        self.assignments = assignments

    async def record(self, session: VivaSession) -> None:
        if session.assignment is None or session.feedback is None:
            return
        await self.assignments.record_result(
            session.assignment.group_id,
            AssignmentResult(
                assignment_id=session.assignment.assignment_id,
                student_id=session.user_id,
                session_id=session.id,
                feedback=session.feedback,
                completed_at=session.ended_at or utc_now(),
            ),
        )


class AssignmentService:
    def __init__(
        self,
        groups: GroupRepository,
        assignments: AssignmentRepository,
        profiles: ProfileRepository,
        viva: VivaService,
    ):
        self.groups = groups
        self.assignments = assignments
        self.profiles = profiles
        self.viva = viva

    async def _owned_group(self, user_id: str, group_id: str) -> Group:
        group = await self.groups.get_group(group_id)
        if group is None or group.owner_id != user_id:
            raise GroupNotFound
        return group

    # ---------------------------------------------------------------- owner

    async def create(
        self, user_id: str, group_id: str, request: AssignmentCreateRequest
    ) -> AssignmentResponse:
        group = await self._owned_group(user_id, group_id)
        selection = request.curriculum_selection
        if group.class_level is not None and selection.class_level != group.class_level:
            raise AssignmentInvalid(
                f"This group is for Class {group.class_level}. "
                f"Pick a Class {group.class_level} chapter."
            )
        if request.due_on is not None and request.due_on < utc_now().date():
            raise AssignmentInvalid("The due date has already passed.")
        existing = await self.assignments.list_assignments(group_id)
        if len(existing) >= MAX_ASSIGNMENTS_PER_GROUP:
            raise AssignmentInvalid("This group has reached the limit of vivas.")
        now = utc_now()
        assignment = Assignment(
            assignment_id=new_session_id(now),
            group_id=group_id,
            title=selection.chapters[0].name,
            curriculum_selection=selection,
            due_on=request.due_on,
            created_at=now,
        )
        await self.assignments.create_assignment(assignment)
        return self._owner_view(assignment, 0)

    @staticmethod
    def _owner_view(assignment: Assignment, completed: int) -> AssignmentResponse:
        return AssignmentResponse(
            assignment_id=assignment.assignment_id,
            title=assignment.title,
            curriculum_selection=assignment.curriculum_selection,
            due_on=assignment.due_on,
            created_at=assignment.created_at,
            completed_count=completed,
        )

    async def list_for_group(
        self, user_id: str, group_id: str
    ) -> list[AssignmentResponse]:
        await self._owned_group(user_id, group_id)
        assignments = await self.assignments.list_assignments(group_id)
        counts = await self.assignments.count_results(group_id)
        return [
            self._owner_view(a, counts.get(a.assignment_id, 0)) for a in assignments
        ]

    async def delete(self, user_id: str, group_id: str, assignment_id: str) -> None:
        await self._owned_group(user_id, group_id)
        await self.assignments.get_assignment(group_id, assignment_id)
        await self.assignments.delete_assignment(group_id, assignment_id)

    # -------------------------------------------------------------- student

    async def list_for_student(self, user_id: str) -> list[StudentAssignmentResponse]:
        """Every viva set in the student's groups: not done first, soonest due first."""
        memberships = await self.groups.list_memberships(user_id)
        if not memberships:
            return []
        groups = {
            g.group_id: g
            for g in await self.groups.get_groups([m.group_id for m in memberships])
        }
        done = {
            (c.group_id, c.assignment_id): c
            for c in await self.assignments.list_completions(user_id)
        }
        views: list[StudentAssignmentResponse] = []
        for group_id, group in groups.items():
            for assignment in await self.assignments.list_assignments(group_id):
                completion = done.get((group_id, assignment.assignment_id))
                views.append(
                    StudentAssignmentResponse(
                        assignment_id=assignment.assignment_id,
                        group_id=group_id,
                        group_name=group.name,
                        owner_name=group.owner_name,
                        owner_type=group.owner_type,
                        title=assignment.title,
                        subject_name=assignment.curriculum_selection.subject_name,
                        curriculum_selection=assignment.curriculum_selection,
                        due_on=assignment.due_on,
                        created_at=assignment.created_at,
                        status="done" if completion else "pending",
                        score=completion.score if completion else None,
                        session_id=completion.session_id if completion else None,
                    )
                )
        # Newest first, then (the sort is stable) not-done before done and the
        # soonest due date first; a viva with no due date comes after dated ones.
        views.sort(key=lambda view: view.assignment_id, reverse=True)
        views.sort(
            key=lambda view: (view.status == "done", view.due_on or datetime.date.max)
        )
        return views

    async def start(self, user_id: str, group_id: str, assignment_id: str) -> dict:
        """Start the set viva for this student, scoped exactly as it was set."""
        student = await self.profiles.get(user_id)
        if student is None or is_educator(student.account_type):
            raise AccountTypeRequired("student")
        if not await self.groups.is_member(user_id, group_id):
            raise GroupNotFound
        assignment = await self.assignments.get_assignment(group_id, assignment_id)
        selection = CurriculumSelectionRequest.model_validate(
            assignment.curriculum_selection.model_dump()
        )
        request = VivaStartRequest(
            student_name=student.name,
            # Replaced by the chapter and topics when the request is validated.
            topic=assignment.title,
            class_level=str(selection.class_level),
            curriculum_selection=selection,
        )
        return await self.viva.start_new_viva_session(
            request,
            user_id,
            AssignmentRef(group_id=group_id, assignment_id=assignment_id),
        )
