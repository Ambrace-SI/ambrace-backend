"""Choose the class-insight generator from settings.

To try another provider or approach: implement `StructuredTextModel` (same
prompt, new model) or `ClassInsightGenerator` (new approach), then add one
entry below and set CLASS_INSIGHT_PROVIDER.
"""

from collections.abc import Callable
from typing import Any

from app.interfaces.insight_generator import ClassInsightGenerator
from app.services.insights.heuristic import HeuristicInsightGenerator
from app.services.insights.llm_generator import LLMInsightGenerator
from app.services.insights.models.gemini import GeminiStructuredModel


def _gemini(settings: Any) -> ClassInsightGenerator:
    return LLMInsightGenerator(
        GeminiStructuredModel(
            api_key=settings.GOOGLE_API_KEY, model=settings.CLASS_INSIGHT_MODEL
        )
    )


def _heuristic(settings: Any) -> ClassInsightGenerator:
    return HeuristicInsightGenerator()


PROVIDERS: dict[str, Callable[[Any], ClassInsightGenerator]] = {
    "gemini": _gemini,
    "heuristic": _heuristic,
}


def build_generator(settings: Any) -> ClassInsightGenerator:
    return PROVIDERS[settings.CLASS_INSIGHT_PROVIDER](settings)
