"""The two seams for experimenting with class insights.

`ClassInsightGenerator` is the one the service depends on: swap it to try a
different approach entirely (one prompt, map-reduce, a LangChain chain, no
model at all). `StructuredTextModel` is narrower: swap it to run the same
prompt on another provider or model.
"""

from typing import Any, Protocol

from app.domain.class_insight import InsightDraft, InsightInput


class InsightGenerationFailed(Exception):
    """The generator could not produce a usable insight. Callers may retry.

    The message is for logs only and must not contain report text.
    """


class ClassInsightGenerator(Protocol):
    # Stored with each insight, e.g. "gemini-3.8-flash:prompt-v1", so outputs
    # of different experiments can be told apart and compared.
    @property
    def id(self) -> str: ...

    async def generate(self, data: InsightInput) -> InsightDraft:
        """Raise InsightGenerationFailed when no usable answer comes back."""
        ...


class StructuredTextModel(Protocol):
    """A text model that answers with JSON matching a given JSON Schema."""

    @property
    def id(self) -> str: ...

    async def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Raise InsightGenerationFailed on transport, provider or parse errors."""
        ...
