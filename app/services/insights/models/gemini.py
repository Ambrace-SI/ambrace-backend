"""Gemini as a `StructuredTextModel`, through the google-genai SDK.

Uses generateContent with a JSON Schema rather than the Interactions API:
generateContent is stateless, while interactions are stored by default and
student reports should not be kept on the provider's side.
"""

import asyncio
import json
import logging
import random
from typing import Any

import google.genai as genai
import httpx
from google.genai import errors, types

from app.interfaces.insight_generator import InsightGenerationFailed

logger = logging.getLogger(__name__)

# Per attempt. The service's own deadline bounds the whole call, retry included.
REQUEST_TIMEOUT_MS = 15_000
ATTEMPTS = 2


def _transient(error: Exception) -> bool:
    """Busy or briefly unreachable: worth one more try. Bad keys or requests are not."""
    return isinstance(
        error, (TimeoutError, httpx.TimeoutException, httpx.TransportError)
    ) or (
        isinstance(error, errors.APIError)
        and error.code in {408, 429, 500, 502, 503, 504}
    )


class GeminiStructuredModel:
    def __init__(self, api_key: str, model: str, temperature: float = 0.2):
        self._api_key = api_key
        self._model = model
        self._temperature = temperature

    @property
    def id(self) -> str:
        return self._model

    async def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self._temperature,
            response_mime_type="application/json",
            response_json_schema=schema,
            # No tools are declared; this also silences the SDK's AFC warning.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        # One client per call, as for Live tokens: async clients are not shared.
        client = genai.Client(
            api_key=self._api_key,
            http_options={
                "timeout": REQUEST_TIMEOUT_MS,
                # This loop is the one retry owner.
                "retry_options": {"attempts": 1},
            },
        )
        try:
            text = await self._generate(client, prompt, config)
        finally:
            try:
                await client.aio.aclose()
            except Exception:
                logger.warning("event=gemini_client_cleanup_failed")
        try:
            output = json.loads(text)
        except ValueError:
            raise InsightGenerationFailed("Gemini answered with invalid JSON") from None
        if not isinstance(output, dict):
            raise InsightGenerationFailed("Gemini answered with a non-object")
        return output

    async def _generate(
        self, client: genai.Client, prompt: str, config: types.GenerateContentConfig
    ) -> str:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = await client.aio.models.generate_content(
                    model=self._model, contents=prompt, config=config
                )
                return response.text or ""
            except Exception as error:
                code = getattr(error, "code", None)
                if not _transient(error) or attempt == ATTEMPTS:
                    raise InsightGenerationFailed(
                        f"Gemini call failed: {type(error).__name__} {code or ''}".strip()
                    ) from None
                logger.warning(
                    "event=class_insight_model_retry model=%s attempt=%d code=%s",
                    self._model,
                    attempt,
                    code,
                )
                await asyncio.sleep(random.uniform(0.5, 1.5))
        raise AssertionError("At least one attempt is made")
