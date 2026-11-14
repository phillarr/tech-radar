"""Claude Haiku analysis with structured output into a strict Pydantic model."""

from __future__ import annotations

import json
import logging
from typing import List, Literal

import anthropic
from pydantic import BaseModel, Field

from src.fetcher import RawItem

logger = logging.getLogger(__name__)

Axis = Literal[
    "AI & GenAI",
    "AI Product Management",
    "AI Engineering",
    "Cloud & Infrastructure",
    "Tech & Startups",
    "AI Governance",
]
SourceType = Literal["OFFICIAL", "EXPERIMENT", "BENCHMARK", "NEWS", "OPINION"]

SYSTEM_PROMPT_TEMPLATE = """\
You are a senior technology intelligence analyst writing the morning brief. Your job is \
to separate signal from noise and explain why each item matters.

The reader is {audience}

You receive a JSON array of recent items (GitHub releases, RSS entries from labs, \
vendors, tech media, regulators, and incident reports from service status pages). \
Decide which items this reader would act on or genuinely want to know about today.

Selection rules:
- Keep at most {max_alerts} items, usually 5 to {max_alerts}. On a quiet day keep fewer; \
keeping none is acceptable.
- Dismiss marketing copy, generic tutorials, listicles, and anything without concrete \
substance. When several items cover the same story, keep only the most authoritative one \
(prefer the official announcement).
- LLM news: keep new model releases, model deprecations or retirements, API changes, \
pricing or rate-limit changes, and agent/tooling launches from major providers. Dismiss \
generic commentary about AI.
- Status-page incidents: keep only major outages or significant degradations of core \
services (API, auth, git operations, CDN, DNS). Dismiss minor or narrowly scoped incidents \
that are already resolved, and always dismiss scheduled maintenance. Say in the summary \
whether the incident is resolved.
- Regulation (CNIL, European Commission, NIST): keep binding rules, enforcement actions, \
official guidance, and deadlines that affect AI products.

For each kept item, fill these fields:
- axis: the single best-fitting axis.
  * AI & GenAI: foundation models, multimodal, agents, open-source models, research.
  * AI Product Management: AI product launches and strategy, pricing and packaging, \
adoption and usage data, AI UX, competitive moves.
  * AI Engineering: RAG, evals, fine-tuning, inference, MLOps, AI dev tooling, and the \
reader's Python stack (FastAPI, Pydantic, SQLModel, Python itself).
  * Cloud & Infrastructure: GPUs and accelerators, cloud platforms, data centers, \
service outages, security incidents.
  * Tech & Startups: funding rounds, acquisitions, notable startups, big-tech strategy.
  * AI Governance: regulation, the EU AI Act, privacy and GDPR, AI safety, standards.
- source_type: OFFICIAL (announcement by the company, lab, project, or regulator itself), \
EXPERIMENT (research results or a hands-on test), BENCHMARK (independent evaluation or \
measurement), NEWS (third-party reporting), OPINION (analysis, commentary, newsletter take).
- importance:
  * CRITICAL: security vulnerabilities (CVEs), breaking changes that require immediate \
work, a major outage that is still unresolved, or a market-shifting launch.
  * MAJOR: important model or product releases, major version bumps, deprecations with a \
deadline, significant regulation, large funding or acquisitions.
  * INFO: everything else worth knowing.
- score: an integer from 0 to 100 estimating overall relevance for this reader, weighing \
innovation (25), market impact (20), relevance to AI product management (20), novelty \
(15), business impact (10), and source credibility (10). CRITICAL items score at least 85. \
Use the full range so items can be ranked.
- impact_summary: exactly two punchy sentences: what happened, then what it means for \
the reader (what to do, decide, or watch for).
- source, title, url: copied verbatim from the input item.

Then write signals: 0 to 3 emerging trends or weak signals that connect at least two input \
items (kept or not). Each has a one-sentence trend, a one-sentence product_opportunity \
(a concrete product idea or decision this trend opens up), and related_urls copied verbatim \
from the input items. Only report signals that are genuinely supported by today's items; an \
empty list is fine.

Finally, write a one-sentence headline summarizing the morning overall.
"""


class TechAlert(BaseModel):
    source: str
    title: str
    url: str
    axis: Axis
    source_type: SourceType
    importance: Literal["CRITICAL", "MAJOR", "INFO"]
    score: int = Field(description="Relevance score from 0 to 100")
    impact_summary: str = Field(description="Two-sentence takeaway")


class Signal(BaseModel):
    trend: str = Field(description="One-sentence emerging trend or weak signal")
    product_opportunity: str = Field(description="One-sentence product idea or decision")
    related_urls: List[str]


class DailyTechReport(BaseModel):
    headline: str = Field(description="Single-sentence overview of the morning")
    alerts: List[TechAlert]
    signals: List[Signal]


class AnalysisError(RuntimeError):
    pass


def analyze(
    items: List[RawItem],
    api_key: str,
    model: str,
    effort: str = "medium",
    audience: str = "a full-stack Python engineer",
    max_alerts: int = 10,
) -> DailyTechReport:
    client = anthropic.Anthropic(api_key=api_key)
    payload = json.dumps(items, ensure_ascii=False, indent=1)
    system = SYSTEM_PROMPT_TEMPLATE.format(audience=audience.strip(), max_alerts=max_alerts)

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=system,
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
    return postprocess(report, {item["url"] for item in items}, max_alerts)


def postprocess(report: DailyTechReport, known_urls: set[str], max_alerts: int) -> DailyTechReport:
    """Drop hallucinated links, clamp scores, rank by score and enforce the item cap."""
    kept = [a for a in report.alerts if a.url in known_urls]
    if len(kept) != len(report.alerts):
        logger.warning("Dropped %d alert(s) with unknown URLs", len(report.alerts) - len(kept))
    for alert in kept:
        alert.score = max(0, min(100, alert.score))
    kept.sort(key=lambda a: a.score, reverse=True)
    if len(kept) > max_alerts:
        logger.info("Trimmed %d alert(s) beyond the cap of %d", len(kept) - max_alerts, max_alerts)
        kept = kept[:max_alerts]

    signals = []
    for signal in report.signals[:3]:
        urls = [u for u in signal.related_urls if u in known_urls]
        if urls:
            signals.append(signal.model_copy(update={"related_urls": urls}))
    return DailyTechReport(headline=report.headline, alerts=kept, signals=signals)
