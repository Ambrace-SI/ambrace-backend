"""A class insight without any model: repeated phrases, grouped and counted.

Deterministic and free. It is the baseline a model has to beat, a fallback
when no model is configured, and a stand-in for local development.
"""

import re
from collections.abc import Iterable

from app.domain.class_insight import (
    MAX_SUGGESTIONS,
    MAX_THEMES,
    GapDraft,
    InsightDraft,
    InsightInput,
    SuggestionDraft,
    SupportDraft,
    ThemeDraft,
)

# Below this score, out of 10, a student is listed for support.
SUPPORT_BELOW = 5
# Two phrases are the same point when this share of their words overlap.
SAME_POINT = 0.5

_STOPWORDS = frozenset(
    "a an and are as at be by can for from has have how in into is it its of on or "
    "that the their them they this to was were what when which while with "
    "does doesn't not still needs need forgets forgot clearly understand "
    "understands understanding unclear between explain explains explained".split()
)


def _words(text: str) -> frozenset[str]:
    return frozenset(
        word
        for word in re.findall(r"[a-z0-9']+", text.casefold())
        if word not in _STOPWORDS and len(word) > 1
    )


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= SAME_POINT


def _group(points: Iterable[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Group (ref, phrase) pairs into (label, refs), most shared first.

    The label is the group's shortest phrase: the part everyone in it shares,
    not one student's longer version.
    """
    groups: list[tuple[list[str], frozenset[str], list[str]]] = []
    for ref, phrase in points:
        words = _words(phrase)
        for phrases, first_words, refs in groups:
            if _similar(words, first_words):
                phrases.append(phrase)
                if ref not in refs:
                    refs.append(ref)
                break
        else:
            groups.append(([phrase], words, [ref]))
    ranked = sorted(groups, key=lambda group: -len(group[2]))
    return [(min(phrases, key=len), refs) for phrases, _, refs in ranked]


class HeuristicInsightGenerator:
    id = "heuristic:v1"

    async def generate(self, data: InsightInput) -> InsightDraft:
        students = data.students
        strengths = _group(
            (s.ref, point) for s in students for point in s.strong_points
        )
        gaps = _group(
            (s.ref, point) for s in students for point in s.areas_of_improvement
        )
        shared_gaps = [(label, refs) for label, refs in gaps if len(refs) > 1]
        top_gaps = (shared_gaps or gaps)[:MAX_THEMES]
        average = sum(s.score for s in students) / len(students) if students else 0

        overview = (
            f"{len(students)} students finished the viva on {data.chapter}, "
            f"averaging {average:.1f} out of 10."
        )
        if top_gaps:
            label, refs = top_gaps[0]
            overview += f' The most common gap ({len(refs)} students): "{label}".'

        return InsightDraft(
            overview=overview,
            strengths=[
                ThemeDraft(point=label, student_refs=refs)
                for label, refs in strengths[:MAX_THEMES]
                if len(refs) > 1
            ],
            gaps=[GapDraft(point=label, student_refs=refs) for label, refs in top_gaps],
            students_to_support=[
                SupportDraft(student_ref=s.ref, reason=f"Scored {s.score} out of 10.")
                for s in sorted(students, key=lambda s: s.score)
                if s.score < SUPPORT_BELOW
            ],
            suggestions=[
                SuggestionDraft(
                    action=f"Revisit with the class: {label}", addresses=label
                )
                for label, _ in top_gaps[:MAX_SUGGESTIONS]
            ],
            coverage_note=(
                "Built by grouping repeated phrases in the reports, without an "
                "AI model. Each report covers one short viva."
            ),
        )
