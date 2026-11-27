"""Run class-insight generators side by side on the same class, for comparison.

    uv run python scripts/eval_insights.py \
        scripts/fixtures/class_insight_sample.json \
        --provider heuristic --provider gemini:gemini-3.8-flash

Each provider is a registry name, optionally with a model after a colon. The
input is an `InsightInput` JSON (refs only, no names). Each provider's draft is
printed and written to `--out` (default: the scratch folder `eval-output/`,
which is gitignored by its name) with its timing, so prompts and models can be
compared on identical evidence. Use synthetic or anonymised classes only.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.domain.class_insight import InsightInput  # noqa: E402
from app.interfaces.insight_generator import InsightGenerationFailed  # noqa: E402
from app.services.insights.registry import PROVIDERS, build_generator  # noqa: E402


def _generator(spec: str):
    provider, _, model = spec.partition(":")
    if provider not in PROVIDERS:
        raise SystemExit(f"Unknown provider {provider!r}; known: {sorted(PROVIDERS)}")
    return build_generator(
        SimpleNamespace(
            CLASS_INSIGHT_PROVIDER=provider,
            CLASS_INSIGHT_MODEL=model or settings.CLASS_INSIGHT_MODEL,
            GOOGLE_API_KEY=settings.GOOGLE_API_KEY,
        )
    )


async def _run(data: InsightInput, specs: list[str], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        generator = _generator(spec)
        started = time.perf_counter()
        try:
            draft = (await generator.generate(data)).model_dump()
            error = None
        except InsightGenerationFailed as failure:
            draft, error = None, str(failure)
        record = {
            "generator_id": generator.id,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "error": error,
            "draft": draft,
        }
        path = out / f"{generator.id.replace(':', '_')}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), "utf-8")
        print(json.dumps(record, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", type=Path, help="InsightInput JSON file")
    parser.add_argument(
        "--provider",
        action="append",
        required=True,
        help="registry name, optionally name:model (repeatable)",
    )
    parser.add_argument("--out", type=Path, default=Path("eval-output"))
    args = parser.parse_args()
    data = InsightInput.model_validate_json(args.input.read_text("utf-8"))
    asyncio.run(_run(data, args.provider, args.out))


if __name__ == "__main__":
    main()
