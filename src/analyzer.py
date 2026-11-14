"""Claude Haiku analysis with structured output into a strict Pydantic model."""

from __future__ import annotations

import json
import logging
from typing import List, Literal

import anthropic
from pydantic import BaseModel, Field

from src.fetcher import RawItem

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """\
You are a Senior Staff Engineer writing the morning tech-watch brief. Your job is to \
separate signal from noise.

The reader is {audience}

You receive a JSON array of items published in the last 24 hours (GitHub releases and \
RSS entries). For each item, decide whether this reader would act on it or genuinely \
want to know about it today.

Dismiss marketing copy, generic tutorials, opinion pieces, and anything without concrete \
technical substance. It is fine, and expected, to keep only a few items, or none at all.

Classify each kept item into exactly one severity tier:
- CRITICAL: security vulnerabilities (CVEs) or breaking changes that require immediate refactoring.
- MAJOR: key new feature releases or major framework version bumps.
- INFO: tooling improvements, performance optimizations, or notable technical releases.

For each kept item, write impact_summary as exactly two punchy sentences: what changed, \
then what a developer should do or watch for. Copy source, title, and url verbatim from \
the input item. Write a one-sentence headline summarizing the morning overall.
"""


class TechAlert(BaseModel):
    source: str
    title: str
    url: str
    importance: Literal["CRITICAL", "MAJOR", "INFO"]
    impact_summary: str = Field(description="Two-sentence developer takeaway")


class DailyTechReport(BaseModel):
    headline: str = Field(description="Single-sentence overview of the morning")
    alerts: List[TechAlert]


class AnalysisError(RuntimeError):
    pass


_SEVERITY_ORDER = {"CRITICAL": 0, "MAJOR": 1, "INFO": 2}


def analyze(
    items: List[RawItem],
    api_key: str,
    model: str,
    effort: str = "medium",
    audience: str = "a full-stack Python engineer",
) -> DailyTechReport:
    client = anthropic.Anthropic(api_key=api_key)
    payload = json.dumps(items, ensure_ascii=False, indent=1)

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT_TEMPLATE.format(audience=audience.strip()),
            output_config={"effort": effort},
            messages=[
                {
                    "role": "user",
                    "content": f"Here are today's items:\n<items>\n{payload}\n</items>",
                }
            ],
            output_format=DailyTechReport,
        )
    except anthropic.RateLimitError as exc:
        raise AnalysisError(f"Anthropic rate limit hit: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise AnalysisError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalysisError(f"Could not reach the Anthropic API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise AnalysisError(f"Model refused the request: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("Model output was truncated (max_tokens reached)")

    report = response.parsed_output
    if report is None:
        raise AnalysisError("Model returned no parseable report")

    logger.info(
        "Claude usage: %d input / %d output tokens",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    # Guard against hallucinated links: keep only alerts pointing to an ingested item.
    known_urls = {item["url"] for item in items}
    kept = [a for a in report.alerts if a.url in known_urls]
    if len(kept) != len(report.alerts):
        logger.warning("Dropped %d alert(s) with unknown URLs", len(report.alerts) - len(kept))
    kept.sort(key=lambda a: _SEVERITY_ORDER[a.importance])
    return DailyTechReport(headline=report.headline, alerts=kept)
