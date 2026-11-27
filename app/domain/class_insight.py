"""What a class-insight generator reads and writes, independent of any model.

Students appear only as pseudonymous refs ("S1", "S2", ...). The service maps
refs to real students before anything reaches a generator and back after, so
no generator, prompt or provider ever sees a name or a transcript.
"""

from pydantic import BaseModel, Field

# Output limits, shared by the response schema sent to models and by the
# validation of what comes back.
MAX_THEMES = 5
MAX_SUPPORT = 10
MAX_SUGGESTIONS = 4
MAX_TEXT = 600


class StudentEvidence(BaseModel):
    """One student's written report for the viva, with their name taken out."""

    ref: str
    score: int = Field(ge=0, le=10)
    summary: str
    strong_points: list[str] = Field(default_factory=list)
    areas_of_improvement: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    coverage_note: str | None = None


class InsightInput(BaseModel):
    """Everything a generator may use: the chapter and the class's reports."""

    subject_name: str
    class_level: int
    chapter: str
    topics: list[str] = Field(default_factory=list)
    students: list[StudentEvidence]


class ThemeDraft(BaseModel):
    """A strength shared by part of the class, and which students showed it."""

    point: str = Field(min_length=1, max_length=MAX_TEXT)
    student_refs: list[str] = Field(default_factory=list)


class GapDraft(ThemeDraft):
    """A learning gap shared by part of the class."""

    misconception: str | None = Field(default=None, max_length=MAX_TEXT)


class SupportDraft(BaseModel):
    student_ref: str
    reason: str = Field(min_length=1, max_length=MAX_TEXT)


class SuggestionDraft(BaseModel):
    """Something the teacher can do with the whole class."""

    action: str = Field(min_length=1, max_length=MAX_TEXT)
    addresses: str | None = Field(default=None, max_length=MAX_TEXT)


class InsightDraft(BaseModel):
    """A generator's answer, still in refs. The service checks it against the class."""

    overview: str = Field(min_length=1, max_length=1500)
    strengths: list[ThemeDraft] = Field(max_length=MAX_THEMES)
    gaps: list[GapDraft] = Field(max_length=MAX_THEMES)
    students_to_support: list[SupportDraft] = Field(max_length=MAX_SUPPORT)
    suggestions: list[SuggestionDraft] = Field(max_length=MAX_SUGGESTIONS)
    coverage_note: str = Field(min_length=1, max_length=MAX_TEXT)
