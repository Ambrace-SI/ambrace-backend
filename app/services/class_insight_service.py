"""A written insight into how a class did on one set viva, for its owner.

What this service guarantees, whichever generator is plugged in:
- Only the group's owner can read or create it.
- It is built from reports of students now in the group, never transcripts.
- Students reach the generator as S1, S2, ...; their names are taken out of
  the report text too. Refs are mapped back to students here.
- The generator's answer is checked against the class: unknown refs are
  dropped and every count is computed here.
- A saved insight is reused until the results it was built from change, so
  asking twice costs one model call.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable

from app.db.assignment_repository import AssignmentRepository, AssignmentResult
from app.db.group_repository import Group, GroupMember, GroupNotFound, GroupRepository
from app.db.insight_repository import (
    InsightRepository,
    StoredInsight,
    StoredSuggestion,
    StoredSupport,
    StoredTheme,
)
from app.db.models import utc_now
from app.domain.class_insight import (
    InsightDraft,
    InsightInput,
    StudentEvidence,
    ThemeDraft,
)
from app.interfaces.insight_generator import (
    ClassInsightGenerator,
    InsightGenerationFailed,
)
from app.schemas.class_insight import (
    ClassInsight,
    ClassInsightResponse,
    InsightStudent,
    InsightTheme,
    StudentToSupport,
    TeachingSuggestion,
)

logger = logging.getLogger(__name__)

# With fewer reports an "insight" would just repeat one student's report.
MIN_RESULTS = 3
# Inside the 30 s Lambda and API Gateway limits, with room to save the result.
GENERATION_TIMEOUT_SECONDS = 20
# A student ref in model-written text ("S3"), and the marker it is stored as,
# so the text reads with the student's current name.
_REF = re.compile(r"\bS\d+\b")
_MARK = re.compile(r"\{student:([^{}]+)\}")
# Name parts shorter than this ("Al") are too likely to match ordinary words.
_MIN_NAME_PART = 3


def _mark(student_id: str) -> str:
    return f"{{student:{student_id}}}"


def _optional[T](convert: Callable[[str], T], value: str | None) -> T | None:
    return None if value is None else convert(value)


class NotEnoughResults(Exception):
    """Fewer than MIN_RESULTS students in the group have finished the viva."""


class InsightUnavailable(Exception):
    """The generator failed or ran out of time. Nothing was saved; retry later."""


def _redactor(members: Iterable[GroupMember]) -> re.Pattern[str] | None:
    """Matches any student's full name or name part, longest first."""
    parts = {m.name.strip() for m in members if m.name.strip()}
    parts |= {
        part for m in members for part in m.name.split() if len(part) >= _MIN_NAME_PART
    }
    if not parts:
        return None
    alternatives = sorted(parts, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(map(re.escape, alternatives)) + r")\b", re.IGNORECASE
    )


class ClassInsightService:
    def __init__(
        self,
        groups: GroupRepository,
        assignments: AssignmentRepository,
        insights: InsightRepository,
        generator: ClassInsightGenerator,
    ):
        self.groups = groups
        self.assignments = assignments
        self.insights = insights
        self.generator = generator

    async def read(
        self, user_id: str, group_id: str, assignment_id: str
    ) -> ClassInsightResponse:
        await self._owned_group(user_id, group_id)
        await self.assignments.get_assignment(group_id, assignment_id)
        members, results = await self._class_results(group_id, assignment_id)
        stored = await self.insights.get(group_id, assignment_id)
        return self._response(members, results, stored)

    async def generate(
        self, user_id: str, group_id: str, assignment_id: str
    ) -> ClassInsightResponse:
        await self._owned_group(user_id, group_id)
        assignment = await self.assignments.get_assignment(group_id, assignment_id)
        members, results = await self._class_results(group_id, assignment_id)
        if len(results) < MIN_RESULTS:
            raise NotEnoughResults
        stored = await self.insights.get(group_id, assignment_id)
        sources = sorted(r.session_id for r in results)
        if stored is not None and stored.source_session_ids == sources:
            return self._response(members, results, stored)

        # Oldest first, so refs follow the order students finished.
        results.sort(key=lambda r: (r.completed_at, r.session_id))
        ref_to_student = {f"S{i}": r.student_id for i, r in enumerate(results, 1)}
        student_to_ref = {sid: ref for ref, sid in ref_to_student.items()}
        redact = _redactor(members)

        def clean(text: str, student_id: str) -> str:
            if redact is None:
                return text
            return redact.sub(student_to_ref[student_id], text)

        selection = assignment.curriculum_selection
        data = InsightInput(
            subject_name=selection.subject_name,
            class_level=selection.class_level,
            chapter=assignment.title,
            topics=[t.name for c in selection.chapters for t in c.topics],
            students=[
                StudentEvidence(
                    ref=student_to_ref[r.student_id],
                    score=r.feedback.score,
                    summary=clean(r.feedback.summary, r.student_id),
                    strong_points=[
                        clean(p, r.student_id) for p in r.feedback.strong_points
                    ],
                    areas_of_improvement=[
                        clean(p, r.student_id) for p in r.feedback.areas_of_improvement
                    ],
                    next_steps=[clean(p, r.student_id) for p in r.feedback.next_steps],
                    coverage_note=clean(r.feedback.coverage_note, r.student_id)
                    if r.feedback.coverage_note
                    else None,
                )
                for r in results
            ],
        )

        draft = await self._draft(data)
        insight = _ground(draft, ref_to_student, assignment_id, self.generator.id)
        insight.source_session_ids = sources
        await self.insights.save(group_id, insight)
        return self._response(members, results, insight)

    async def _draft(self, data: InsightInput) -> InsightDraft:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(GENERATION_TIMEOUT_SECONDS):
                draft = await self.generator.generate(data)
        except (InsightGenerationFailed, TimeoutError) as error:
            # The message names only an error type or schema path, never report text.
            logger.error(
                "event=class_insight_failed generator=%s error_type=%s detail=%s",
                self.generator.id,
                type(error).__name__,
                str(error)[:300],
            )
            raise InsightUnavailable from None
        logger.info(
            "event=class_insight_generated generator=%s students=%d duration_ms=%d",
            self.generator.id,
            len(data.students),
            round((time.perf_counter() - started) * 1000),
        )
        return draft

    async def _owned_group(self, user_id: str, group_id: str) -> Group:
        group = await self.groups.get_group(group_id)
        if group is None or group.owner_id != user_id:
            raise GroupNotFound
        return group

    async def _class_results(
        self, group_id: str, assignment_id: str
    ) -> tuple[list[GroupMember], list[AssignmentResult]]:
        """The group now, and its members' results. Leavers are not the class."""
        members = await self.groups.list_members(group_id)
        in_group = {m.student_id for m in members}
        results = [
            r
            for r in await self.assignments.list_results(group_id, assignment_id)
            if r.student_id in in_group
        ]
        return members, results

    @staticmethod
    def _response(
        members: list[GroupMember],
        results: list[AssignmentResult],
        stored: StoredInsight | None,
    ) -> ClassInsightResponse:
        base = {
            "results_count": len(results),
            "member_count": len(members),
            "min_results": MIN_RESULTS,
        }
        if stored is None:
            status = (
                "ready_to_generate"
                if len(results) >= MIN_RESULTS
                else "not_enough_results"
            )
            return ClassInsightResponse(status=status, **base)

        current = {r.session_id for r in results}
        sources = set(stored.source_session_ids)
        names = {m.student_id: m.name for m in members}
        # Someone who left since keeps their place in the saved insight.
        for r in results:
            names.setdefault(r.student_id, r.student_name or "Former student")

        def student(student_id: str) -> InsightStudent:
            return InsightStudent(
                student_id=student_id, name=names.get(student_id, "Former student")
            )

        def text(value: str) -> str:
            return _MARK.sub(lambda m: names.get(m[1], "a former student"), value)

        def theme(t: StoredTheme) -> InsightTheme:
            return InsightTheme(
                point=text(t.point),
                student_count=len(t.student_ids),
                students=[student(sid) for sid in t.student_ids],
                misconception=_optional(text, t.misconception),
            )

        return ClassInsightResponse(
            status="ready",
            is_stale=current != sources,
            new_results=len(current - sources),
            insight=ClassInsight(
                overview=text(stored.overview),
                strengths=[theme(t) for t in stored.strengths],
                gaps=[theme(t) for t in stored.gaps],
                students_to_support=[
                    StudentToSupport(
                        student=student(s.student_id), reason=text(s.reason)
                    )
                    for s in stored.students_to_support
                ],
                suggestions=[
                    TeachingSuggestion(
                        action=text(s.action), addresses=_optional(text, s.addresses)
                    )
                    for s in stored.suggestions
                ],
                coverage_note=text(stored.coverage_note),
                based_on=len(sources),
                generator_id=stored.generator_id,
                generated_at=stored.generated_at,
            ),
            **base,
        )


def _ground(
    draft: InsightDraft,
    ref_to_student: dict[str, str],
    assignment_id: str,
    generator_id: str,
) -> StoredInsight:
    """Keep only what the class supports: known refs, each once, no empty themes."""

    def text(value: str) -> str:
        return _REF.sub(
            lambda m: _mark(ref_to_student[m[0]]) if m[0] in ref_to_student else m[0],
            value,
        )

    def known(refs: Iterable[str]) -> list[str]:
        return list(
            dict.fromkeys(ref_to_student[r] for r in refs if r in ref_to_student)
        )

    def themes(drafts: Iterable[ThemeDraft]) -> list[StoredTheme]:
        kept = []
        for t in drafts:
            student_ids = known(t.student_refs)
            if student_ids:
                kept.append(
                    StoredTheme(
                        point=text(t.point),
                        student_ids=student_ids,
                        misconception=_optional(
                            text, getattr(t, "misconception", None)
                        ),
                    )
                )
        return kept

    support: dict[str, StoredSupport] = {}
    for s in draft.students_to_support:
        student_id = ref_to_student.get(s.student_ref)
        if student_id is not None and student_id not in support:
            support[student_id] = StoredSupport(
                student_id=student_id, reason=text(s.reason)
            )

    return StoredInsight(
        assignment_id=assignment_id,
        overview=text(draft.overview),
        strengths=themes(draft.strengths),
        gaps=themes(draft.gaps),
        students_to_support=list(support.values()),
        suggestions=[
            StoredSuggestion(
                action=text(s.action), addresses=_optional(text, s.addresses)
            )
            for s in draft.suggestions
        ],
        coverage_note=text(draft.coverage_note),
        generator_id=generator_id,
        source_session_ids=[],
        generated_at=utc_now(),
    )
