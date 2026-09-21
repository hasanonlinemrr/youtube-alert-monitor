"""
classifier.py — Two-stage video classifier.
Stage 1: keyword-only (free, zero API cost)
Stage 2: AI classification via OpenRouter (minimal JSON prompt)
"""

import json
import os
import re
import logging
from typing import Optional

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from utils import (
    load_keywords, load_settings, normalize_exam_name,
    check_and_increment_ai, truncate, get_logger
)

logger = get_logger("classifier")

# ---------------------------------------------------------------------------
# AI client (OpenRouter uses OpenAI-compatible API)
# ---------------------------------------------------------------------------

def _get_openrouter_client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY environment variable is not set.")
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Stage 1 — Keyword filter (free)
# ---------------------------------------------------------------------------

def quick_filter(video: dict) -> dict:
    """
    Pure keyword/regex matching. Zero API cost.

    Returns:
        {
            'relevant': bool,
            'event_type': str | None,
            'confidence': float,
            'matched_include': list[str],
            'matched_exclude': list[str],
            'override': bool
        }
    """
    keywords = load_keywords()
    title = (video.get("title") or "").lower()
    description = (video.get("description") or "")[:1000].lower()
    text = title + " " + description

    include_patterns = keywords.get("include_patterns", {})
    exclude_patterns = keywords.get("exclude_patterns", {})
    strong_overrides = [
        p.lower() for p in keywords.get("strong_include_override", [])
        if not p.startswith("_")
    ]
    event_type_rules = keywords.get("event_type_rules", {})

    # Check strong override phrases first (definitive include signal)
    override_matched = [p for p in strong_overrides if p in text]
    if override_matched:
        event_type = _detect_event_type(text, event_type_rules)
        return {
            "relevant": True,
            "event_type": event_type,
            "confidence": 0.80,
            "matched_include": override_matched,
            "matched_exclude": [],
            "override": True,
        }

    # Collect include matches
    matched_include = []
    for category, patterns in include_patterns.items():
        if category.startswith("_"):
            continue
        for p in patterns:
            if p.lower() in text:
                matched_include.append(p)

    # Collect exclude matches
    matched_exclude = []
    for category, patterns in exclude_patterns.items():
        if category.startswith("_"):
            continue
        for p in patterns:
            if p.lower() in text:
                matched_exclude.append(p)

    if not matched_include:
        return {
            "relevant": False,
            "event_type": None,
            "confidence": 0.95,
            "matched_include": [],
            "matched_exclude": matched_exclude,
            "override": False,
        }

    # If strong excludes with no strong includes
    if matched_exclude and not override_matched:
        # Check if include signals outweigh exclude signals
        include_score = len(matched_include)
        exclude_score = len(matched_exclude)
        if exclude_score > include_score:
            return {
                "relevant": False,
                "event_type": None,
                "confidence": 0.70,
                "matched_include": matched_include,
                "matched_exclude": matched_exclude,
                "override": False,
            }

    event_type = _detect_event_type(text, event_type_rules)
    confidence = min(0.75, 0.40 + len(matched_include) * 0.10)

    return {
        "relevant": True,
        "event_type": event_type,
        "confidence": confidence,
        "matched_include": matched_include,
        "matched_exclude": matched_exclude,
        "override": False,
    }


def _detect_event_type(text: str, rules: dict) -> Optional[str]:
    """Determine event_type from keyword rules. First match wins."""
    for event_type, rule in rules.items():
        if event_type.startswith("_"):
            continue
        for pattern in rule.get("title_patterns", []):
            if pattern.lower() in text:
                return event_type
    return None


# ---------------------------------------------------------------------------
# Stage 2 — AI classification (OpenRouter, 1 request)
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = """You are an expert at analyzing Indian government job recruitment videos on YouTube, especially for West Bengal and SSC/railway/banking jobs. Your task is to classify whether a video is about an actionable recruitment event.

RELEVANT event types (classify as relevant=true):
- APPLICATION_OPEN: Application/form has started NOW
- APPLICATION_OPEN_SOON: Application will start within ~7 days, concrete date given
- ADMIT_CARD_RELEASED: Admit card is actually available now
- RESULT_DECLARED: Result has been published/declared

NOT RELEVANT (relevant=false):
- Study content (maths, reasoning, English, GK, mock tests)
- Preparation tips/strategy
- Coaching advertisements
- Exam analysis or cutoff predictions
- How-to-fill tutorials
- Motivational content
- Expected/upcoming without a concrete date

Respond ONLY with valid JSON. No explanation."""

CLASSIFY_USER_TEMPLATE = """Channel: {channel_name}
Title: {title}
Upload time: {published_at}
Description (first 400 chars): {description}
Matched keywords: {matched_keywords}

Respond with this exact JSON structure:
{{
  "relevant": true/false,
  "event_type": "APPLICATION_OPEN" | "APPLICATION_OPEN_SOON" | "ADMIT_CARD_RELEASED" | "RESULT_DECLARED" | null,
  "exam_name": "exact exam name as mentioned in video",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence"
}}"""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15))
def ai_classify(video: dict, keyword_result: dict, state: dict) -> dict:
    """
    Send compact video metadata to OpenRouter for AI classification.
    Uses 1 API request. Returns structured classification result.

    Falls back to keyword_result if AI request fails or budget exceeded.
    """
    if not check_and_increment_ai(state):
        logger.warning("AI budget exceeded, falling back to keyword result only")
        return _fallback(keyword_result, "AI budget exceeded")

    settings = load_settings()
    model = settings.get("openrouter", {}).get("model", "openrouter/auto")
    keywords = load_keywords()
    normalizations = keywords.get("exam_name_normalizations", {})

    prompt = CLASSIFY_USER_TEMPLATE.format(
        channel_name=video.get("channel_name", "Unknown"),
        title=video.get("title", ""),
        published_at=video.get("published_at", ""),
        description=truncate(video.get("description", ""), 400),
        matched_keywords=", ".join(keyword_result.get("matched_include", [])[:8]),
    )

    try:
        client = _get_openrouter_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
            temperature=0.1,
        )

        raw = response.choices[0].message.content
        result = json.loads(raw)

        # Normalize exam name using our canonical map
        if result.get("exam_name"):
            result["exam_name"] = normalize_exam_name(result["exam_name"], normalizations)

        # Merge AI result with keyword signals
        result["matched_include"] = keyword_result.get("matched_include", [])
        result["matched_exclude"] = keyword_result.get("matched_exclude", [])
        result["source"] = "ai"

        logger.info(
            f"AI classified '{video.get('title', '')[:60]}': "
            f"relevant={result.get('relevant')}, "
            f"event_type={result.get('event_type')}, "
            f"confidence={result.get('confidence')}"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI returned invalid JSON: {e}")
        return _fallback(keyword_result, f"JSON parse error: {e}")
    except Exception as e:
        logger.error(f"AI classification failed: {e}")
        return _fallback(keyword_result, str(e))


def _fallback(keyword_result: dict, reason: str) -> dict:
    """Return a safe fallback classification based on keyword results only."""
    return {
        "relevant": keyword_result.get("relevant", False),
        "event_type": keyword_result.get("event_type"),
        "exam_name": None,
        "confidence": keyword_result.get("confidence", 0.4),
        "reasoning": f"Keyword-only (AI unavailable: {reason})",
        "source": "keywords_only",
        "matched_include": keyword_result.get("matched_include", []),
        "matched_exclude": keyword_result.get("matched_exclude", []),
    }


# ---------------------------------------------------------------------------
# Full classification pipeline
# ---------------------------------------------------------------------------

def classify_video(video: dict, state: dict) -> dict:
    """
    Run the full two-stage classification pipeline for a single video.
    Returns the final classification result dict.
    """
    # Stage 1: free keyword filter
    kw_result = quick_filter(video)

    if not kw_result["relevant"]:
        logger.debug(f"Keyword filter rejected: {video.get('title', '')[:60]}")
        return kw_result

    logger.info(f"Keyword filter passed (confidence={kw_result['confidence']:.2f}): {video.get('title', '')[:60]}")

    # If very high keyword confidence (strong override), skip AI to save quota
    if kw_result.get("override") and kw_result["confidence"] >= 0.80:
        # Still need exam name — derive from title
        keywords = load_keywords()
        normalizations = keywords.get("exam_name_normalizations", {})
        title = video.get("title", "")
        kw_result["exam_name"] = normalize_exam_name(title, normalizations)
        kw_result["source"] = "keywords_strong"
        return kw_result

    # Stage 2: AI
    return ai_classify(video, kw_result, state)
