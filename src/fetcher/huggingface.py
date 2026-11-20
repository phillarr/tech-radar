"""New models published by a lab's own Hugging Face organization."""

from __future__ import annotations

import logging
import re
from datetime import datetime

import requests

from src.config_loader import HuggingFaceAuthor
from src.fetcher.base import (
    USER_AGENT,
    RawItem,
    SourceHealth,
    clean_text,
    http_get,
    make_health,
    parse_iso8601,
)

logger = logging.getLogger(__name__)

HF_API = "https://huggingface.co/api/models"
_HF_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _model_card_text(card: str) -> str:
    card = _FRONT_MATTER_RE.sub("", card)
    return _MD_LINK_RE.sub(r"\1", _MD_IMAGE_RE.sub(" ", card)).replace("#", " ")


def _is_model_of(model_id: object, author: str) -> bool:
    """A model is official only if it belongs to the organization itself, and its id is
    used in URLs: anything other than "<author>/<plain name>" is refused."""
    if not isinstance(model_id, str) or "/" not in model_id:
        return False
    owner, name = model_id.split("/", 1)
    return owner == author and bool(_HF_NAME_RE.fullmatch(name)) and ".." not in name


def _model_card(model_id: str, timeout: float) -> str:
    try:
        resp = http_get(
            f"https://huggingface.co/{model_id}/raw/main/README.md",
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.info("No model card for %s: %s", model_id, exc)
        return ""
    return _model_card_text(resp.text) if resp.ok else ""


def fetch_huggingface_models(
    authors: list[HuggingFaceAuthor],
    cutoff: datetime,
    max_items: int,
    max_chars: int,
    timeout: float,
) -> tuple[list[RawItem], list[SourceHealth]]:
    """For labs with no feed (e.g. DeepSeek, Qwen), their Hugging Face organization is the
    official release channel of their open models.
    """
    items: list[RawItem] = []
    health: list[SourceHealth] = []
    for org in authors:
        name, page = f"Hugging Face · {org.name}", f"https://huggingface.co/{org.author}"
        # The schema's source kind is 'rss' or 'github': a model listing is closest to a feed.
        try:
            resp = http_get(
                HF_API,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                params={
                    "author": org.author,
                    "sort": "createdAt",
                    "direction": "-1",
                    "limit": str(max_items * 2),
                },
            )
            resp.raise_for_status()
            models = resp.json()
            if not isinstance(models, list):
                raise ValueError("unexpected response")
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Hugging Face fetch failed for %s: %s", org.author, exc)
            health.append(make_health(name, page, "rss", "OFFICIAL", ok=False, error=str(exc)))
            continue

        dated = [
            (d, m)
            for m in models
            if isinstance(m, dict)
            and _is_model_of(m.get("id"), org.author)
            and (d := parse_iso8601(m.get("createdAt"))) is not None
        ]
        recent = sorted(((d, m) for d, m in dated if d >= cutoff), key=lambda p: p[0], reverse=True)
        selected = recent[:max_items]
        newest = max((d for d, _ in dated), default=None)
        health.append(make_health(name, page, "rss", "OFFICIAL", items=len(selected), last=newest))

        for created, model in selected:
            model_id = model["id"]
            task = model.get("pipeline_tag") or "unspecified task"
            content = (
                f"{org.name} published a new model on its official Hugging Face page: "
                f"{model_id} ({task}). {_model_card(model_id, timeout)}"
            )
            items.append(
                RawItem(
                    source=name,
                    source_type="OFFICIAL",
                    title=f"{org.name} releases {model_id.split('/', 1)[-1]} on Hugging Face",
                    url=f"https://huggingface.co/{model_id}",
                    published_at=created.isoformat(),
                    content=clean_text(content, max_chars),
                )
            )
    return items, health
