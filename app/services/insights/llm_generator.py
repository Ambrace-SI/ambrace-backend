"""A class insight from one structured call to any text model."""

from pydantic import ValidationError

from app.domain.class_insight import InsightDraft, InsightInput
from app.interfaces.insight_generator import (
    InsightGenerationFailed,
    StructuredTextModel,
)
from app.services.insights.prompt import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_prompt,
)


class LLMInsightGenerator:
    def __init__(self, model: StructuredTextModel):
        self.model = model

    @property
    def id(self) -> str:
        return f"{self.model.id}:prompt-{PROMPT_VERSION}"

    async def generate(self, data: InsightInput) -> InsightDraft:
        output = await self.model.generate_json(
            system=SYSTEM_INSTRUCTION,
            prompt=build_prompt(data),
            schema=RESPONSE_SCHEMA,
        )
        try:
            return InsightDraft.model_validate(output)
        except ValidationError as error:
            # Field paths only: the error's input values would be report text.
            fields = sorted({".".join(map(str, e["loc"])) for e in error.errors()})
            raise InsightGenerationFailed(
                f"Answer did not match the schema at: {', '.join(fields)[:300]}"
            ) from None
