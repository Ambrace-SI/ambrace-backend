"""Class-insight generators: each one honours the same contract.

The service only knows `ClassInsightGenerator`. These tests pin the parts
behind it: the no-model baseline, the model-backed generator, the prompt and
the Gemini adapter (with its SDK client stubbed out).
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import errors

from app.domain.class_insight import InsightDraft, InsightInput, StudentEvidence
from app.interfaces.insight_generator import InsightGenerationFailed
from app.services.insights import registry
from app.services.insights.heuristic import HeuristicInsightGenerator
from app.services.insights.llm_generator import LLMInsightGenerator
from app.services.insights.models import gemini as gemini_module
from app.services.insights.models.gemini import GeminiStructuredModel
from app.services.insights.prompt import (
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_prompt,
)


def evidence(ref, score, strong=(), gaps=()):
    return StudentEvidence(
        ref=ref,
        score=score,
        summary=f"{ref} took the viva.",
        strong_points=list(strong),
        areas_of_improvement=list(gaps),
    )


CLASS = InsightInput(
    subject_name="Science",
    class_level=9,
    chapter="Motion",
    topics=["Speed and velocity"],
    students=[
        evidence("S1", 8, ["Defines speed clearly"], ["Velocity needs a direction"]),
        evidence("S2", 6, ["Defines speed"], ["Forgets that velocity has a direction"]),
        evidence("S3", 3, [], ["Velocity has a direction", "Unit conversion"]),
    ],
)

VALID_OUTPUT = {
    "overview": "The class knows speed; velocity is shaky.",
    "strengths": [{"point": "Defines speed", "student_refs": ["S1", "S2"]}],
    "gaps": [
        {
            "point": "Velocity has a direction",
            "misconception": "Velocity treated as speed",
            "student_refs": ["S1", "S2", "S3"],
        }
    ],
    "students_to_support": [{"student_ref": "S3", "reason": "Scored 3"}],
    "suggestions": [{"action": "Draw two paths", "addresses": "Velocity"}],
    "coverage_note": "One short viva.",
}


class FakeModel:
    id = "fake-model"

    def __init__(self, output):
        self.output = output
        self.calls = []

    async def generate_json(self, *, system, prompt, schema):
        self.calls.append((system, prompt, schema))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


# ------------------------------------------------------------------ heuristic


def test_the_heuristic_groups_repeated_gaps_across_students():
    insight = asyncio.run(HeuristicInsightGenerator().generate(CLASS))

    top = insight.gaps[0]
    assert sorted(top.student_refs) == ["S1", "S2", "S3"]
    assert [s.student_ref for s in insight.students_to_support] == ["S3"]
    assert insight.suggestions
    assert "without an AI model" in insight.coverage_note


def test_the_heuristic_output_fits_the_shared_schema():
    insight = asyncio.run(HeuristicInsightGenerator().generate(CLASS))

    assert InsightDraft.model_validate(insight.model_dump()) == insight


# -------------------------------------------------------------- model-backed


def test_the_llm_generator_sends_prompt_and_schema_and_parses_the_reply():
    model = FakeModel(VALID_OUTPUT)
    generator = LLMInsightGenerator(model)

    insight = asyncio.run(generator.generate(CLASS))

    assert insight.gaps[0].student_refs == ["S1", "S2", "S3"]
    system, prompt, schema = model.calls[0]
    assert system == SYSTEM_INSTRUCTION
    assert schema == RESPONSE_SCHEMA
    assert "Motion" in prompt and "S3" in prompt
    assert generator.id == f"fake-model:prompt-{PROMPT_VERSION}"


@pytest.mark.parametrize(
    "output",
    [
        {"overview": "missing everything else"},
        {**VALID_OUTPUT, "gaps": "not a list"},
        InsightGenerationFailed("transport"),
    ],
)
def test_a_bad_or_failed_reply_is_a_generation_failure(output):
    with pytest.raises(InsightGenerationFailed):
        asyncio.run(LLMInsightGenerator(FakeModel(output)).generate(CLASS))


def test_the_response_schema_accepts_what_the_draft_accepts():
    assert set(RESPONSE_SCHEMA["required"]) == set(InsightDraft.model_fields)
    InsightDraft.model_validate(VALID_OUTPUT)


def test_the_prompt_carries_reports_as_data_not_instructions():
    prompt = build_prompt(CLASS)

    payload = json.loads(prompt.split("<reports>")[1].split("</reports>")[0])
    assert [s["ref"] for s in payload["students"]] == ["S1", "S2", "S3"]
    assert payload["chapter"] == "Motion"


# ------------------------------------------------------------------- gemini


def stub_client(monkeypatch, text="", error=None):
    generate = AsyncMock(side_effect=error, return_value=SimpleNamespace(text=text))
    close = AsyncMock()
    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=generate), aclose=close
        )
    )
    created = []

    def make(**kwargs):
        created.append(kwargs)
        return client

    monkeypatch.setattr(gemini_module.genai, "Client", make)
    return SimpleNamespace(generate=generate, close=close, created=created)


def test_gemini_asks_for_schema_constrained_json(monkeypatch):
    stub = stub_client(monkeypatch, text=json.dumps(VALID_OUTPUT))
    model = GeminiStructuredModel(api_key="k", model="gemini-test")

    output = asyncio.run(
        model.generate_json(system="sys", prompt="prompt", schema=RESPONSE_SCHEMA)
    )

    assert output == VALID_OUTPUT
    call = stub.generate.await_args.kwargs
    assert call["model"] == "gemini-test"
    assert call["contents"] == "prompt"
    config = call["config"]
    assert config.system_instruction == "sys"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == RESPONSE_SCHEMA
    assert stub.created[0]["api_key"] == "k"
    stub.close.assert_awaited_once()
    assert model.id == "gemini-test"


@pytest.mark.parametrize(
    ("text", "error"),
    [("not json", None), ("[1, 2]", None), ("", RuntimeError("secret detail"))],
)
def test_gemini_failures_are_sanitised(monkeypatch, text, error):
    stub = stub_client(monkeypatch, text=text, error=error)
    model = GeminiStructuredModel(api_key="k", model="gemini-test")

    with pytest.raises(InsightGenerationFailed) as raised:
        asyncio.run(model.generate_json(system="s", prompt="p", schema={}))

    assert "secret detail" not in str(raised.value)
    stub.close.assert_awaited_once()
    assert stub.generate.await_count == 1


def busy():
    return errors.ServerError(503, {"error": {"code": 503, "message": "busy"}})


def test_gemini_tries_once_more_when_busy(monkeypatch):
    stub = stub_client(monkeypatch)
    stub.generate.side_effect = [busy(), SimpleNamespace(text=json.dumps({"a": 1}))]
    monkeypatch.setattr(gemini_module.asyncio, "sleep", AsyncMock())
    model = GeminiStructuredModel(api_key="k", model="gemini-test")

    output = asyncio.run(model.generate_json(system="s", prompt="p", schema={}))

    assert output == {"a": 1}
    assert stub.generate.await_count == 2
    stub.close.assert_awaited_once()


def test_gemini_gives_up_after_a_second_busy_answer(monkeypatch):
    stub = stub_client(monkeypatch)
    stub.generate.side_effect = [busy(), busy()]
    monkeypatch.setattr(gemini_module.asyncio, "sleep", AsyncMock())
    model = GeminiStructuredModel(api_key="k", model="gemini-test")

    with pytest.raises(InsightGenerationFailed, match="503"):
        asyncio.run(model.generate_json(system="s", prompt="p", schema={}))

    assert stub.generate.await_count == 2


# ----------------------------------------------------------------- registry


def test_the_registry_builds_the_configured_generator():
    heuristic = registry.build_generator(
        SimpleNamespace(CLASS_INSIGHT_PROVIDER="heuristic")
    )
    gemini = registry.build_generator(
        SimpleNamespace(
            CLASS_INSIGHT_PROVIDER="gemini",
            CLASS_INSIGHT_MODEL="gemini-test",
            GOOGLE_API_KEY="k",
        )
    )

    assert heuristic.id.startswith("heuristic")
    assert gemini.id == f"gemini-test:prompt-{PROMPT_VERSION}"
