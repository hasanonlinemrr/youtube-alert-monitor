"""
extractor.py — Deep extraction engine (triggered only by EXTRACT FULL INFORMATION button).
Fetches transcript + official documents → sends to AI → validates → returns structured result.
"""

import json
import logging
import os
import re
from typing import Optional

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from transcript import get_transcript_with_fallback
from official_docs import fetch_official_docs, build_source_context
from utils import (
    load_settings, load_keywords, check_and_increment_ai,
    is_valid_date, get_logger, truncate
)

logger = get_logger("extractor")

MAX_CONTEXT_CHARS = 40_000  # Max chars to send to AI for extraction


def _get_openrouter_client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set.")
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM_PROMPT = """You are an expert at extracting recruitment/job application information from Indian government job videos and official documents. You are extracting information for a service called HASAN ONLINE that helps people in West Bengal apply for government jobs.

CRITICAL RULES:
1. NEVER invent information. If something is not mentioned in the source, use null.
2. Dates must be extracted EXACTLY as mentioned — do not calculate or guess.
3. If two sources disagree on a fact, add it to the "conflicts" array.
4. If a field is not mentioned anywhere in the source text, set it to null and add the field name to "unknown_fields".
5. Fees must be category-wise — don't combine them.
6. For photo/signature, extract EXACTLY what is stated — pixel dimensions, KB limits, etc.
7. Return ONLY valid JSON. No explanation, no prose.

Source priority (highest to lowest factual authority):
1. Official government PDF/notification
2. Official government website
3. Video transcript
4. Video description
5. Video title"""

EXTRACT_USER_TEMPLATE = """Extract ALL recruitment information from the following source text.

EXAM: {exam_name}
EVENT TYPE: {event_type}

SOURCE TEXT:
{source_text}

Return this exact JSON structure (use null for any field not found):
{{
  "exam_name": "",
  "event_type": "",
  "post_info": {{
    "organization": null,
    "post_name": null,
    "vacancy": null,
    "notification_number": null,
    "application_mode": null
  }},
  "dates": {{
    "notification_date": null,
    "application_start": null,
    "application_last_date": null,
    "fee_last_date": null,
    "correction_window": null,
    "exam_date": null,
    "admit_card_date": null,
    "result_date": null,
    "other_dates": []
  }},
  "eligibility": {{
    "qualification": null,
    "subjects": null,
    "percentage": null,
    "age_limit": null,
    "age_relaxation": null,
    "nationality": null,
    "other_conditions": []
  }},
  "fees": {{
    "general": null,
    "obc": null,
    "ews": null,
    "sc": null,
    "st": null,
    "female": null,
    "pwbd": null,
    "ex_serviceman": null,
    "processing_fee": null,
    "total": null,
    "payment_mode": null
  }},
  "documents": [],
  "photo": {{
    "format": null,
    "size": null,
    "dimensions": null,
    "resolution": null,
    "background": null,
    "other": null
  }},
  "signature": {{
    "format": null,
    "size": null,
    "dimensions": null,
    "resolution": null,
    "other": null
  }},
  "application_steps": [],
  "important_instructions": [],
  "official_website": null,
  "official_notification": null,
  "conflicts": [],
  "unknown_fields": [],
  "extraction_confidence": "High/Medium/Low"
}}"""


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_full_information(event: dict, state: dict) -> Optional[dict]:
    """
    Full extraction pipeline:
    1. Fetch transcript(s) from best source videos
    2. Fetch official documents from description links
    3. Build combined source context
    4. Send to OpenRouter AI with structured schema
    5. Validate output
    6. Return extraction result dict

    Returns None if AI budget exceeded or fatal error.
    """
    settings = load_settings()

    if not check_and_increment_ai(state):
        logger.warning("AI budget exceeded, cannot perform extraction")
        return None

    # --- Step 1: Collect source material ---
    source_videos = event.get("source_videos", [])
    if not source_videos:
        logger.error(f"No source videos for event {event['event_id']}")
        return None

    # Use top 2 videos (highest priority channel first)
    videos_to_use = source_videos[:2]
    context_parts = []
    all_docs = []

    for video in videos_to_use:
        logger.info(f"Fetching content for video: {video['video_id']}")

        # Get transcript
        content = get_transcript_with_fallback(video)
        source_note = content["source_note"]
        text = content["text"]

        if text:
            context_parts.append(
                f"=== VIDEO SOURCE: {video.get('channel_name', '')} ===\n"
                f"Source: {source_note}\n\n{text}"
            )

        # Get official documents
        docs = fetch_official_docs(video)
        all_docs.extend(docs)

    # --- Step 2: Build official docs context ---
    official_context = build_source_context(all_docs)
    if official_context:
        # Put official docs FIRST (higher priority)
        context_parts.insert(0, official_context)

    # --- Step 3: Combine and truncate ---
    combined_source = "\n\n".join(context_parts)
    if len(combined_source) > MAX_CONTEXT_CHARS:
        combined_source = combined_source[:MAX_CONTEXT_CHARS] + "\n\n[SOURCE TRUNCATED]"

    if not combined_source.strip():
        logger.warning(f"No extractable content found for {event['event_id']}")
        return _empty_extraction(event, "No content available")

    # --- Step 4: AI extraction ---
    extraction = _ai_extract(
        exam_name=event["exam_name"],
        event_type=event["event_type"],
        source_text=combined_source,
        state=state,
    )

    if not extraction:
        return _empty_extraction(event, "AI extraction failed")

    # --- Step 5: Validation ---
    extraction = _validate_extraction(extraction)

    # Add official doc status
    has_official = any(d.is_official for d in all_docs if d.text)
    extraction["official_doc_found"] = has_official

    logger.info(
        f"Extraction complete for {event['event_id']}: "
        f"confidence={extraction.get('extraction_confidence')}, "
        f"official_doc={has_official}, "
        f"conflicts={len(extraction.get('conflicts', []))}"
    )
    return extraction


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=5, max=20))
def _ai_extract(exam_name: str, event_type: str, source_text: str, state: dict) -> Optional[dict]:
    """Call OpenRouter with the full extraction schema. Returns parsed dict or None."""
    settings = load_settings()
    model = settings.get("openrouter", {}).get("model", "openrouter/auto")

    prompt = EXTRACT_USER_TEMPLATE.format(
        exam_name=exam_name,
        event_type=event_type,
        source_text=source_text,
    )

    try:
        client = _get_openrouter_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.05,  # Very low temp for factual extraction
        )

        raw = response.choices[0].message.content
        result = json.loads(raw)
        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI returned invalid JSON during extraction: {e}")
        return None
    except Exception as e:
        logger.error(f"AI extraction API error: {e}")
        raise  # Let tenacity retry


def _validate_extraction(data: dict) -> dict:
    """
    Validate the AI extraction output:
    - Check date fields look like real dates
    - Check fee fields look numeric
    - Flag conflicts
    - Replace invalid values with null
    """
    conflicts = data.get("conflicts", [])

    # Validate date fields
    dates = data.get("dates", {})
    date_fields = [
        "notification_date", "application_start", "application_last_date",
        "fee_last_date", "exam_date", "admit_card_date", "result_date",
    ]
    for field in date_fields:
        val = dates.get(field)
        if val and not is_valid_date(val):
            logger.warning(f"Invalid date value for {field}: {val}")
            conflicts.append(f"Suspicious date value for '{field}': {val} — please verify")

    # Check application_last_date comes after application_start (sanity check)
    try:
        from dateutil import parser as dateparser
        start = dates.get("application_start")
        end = dates.get("application_last_date")
        if start and end:
            d_start = dateparser.parse(start, dayfirst=True)
            d_end = dateparser.parse(end, dayfirst=True)
            if d_start and d_end and d_end < d_start:
                conflicts.append(
                    f"Last date ({end}) appears to be before start date ({start}) — please verify"
                )
    except Exception:
        pass

    # Validate fee fields (should be numeric or "Free/Exempt/Nil")
    fees = data.get("fees", {})
    fee_fields = ["general", "obc", "ews", "sc", "st", "female", "pwbd", "ex_serviceman"]
    for field in fee_fields:
        val = fees.get(field)
        if val and not _looks_like_fee(val):
            conflicts.append(f"Unexpected fee value for '{field}': {val} — please verify")

    data["conflicts"] = conflicts
    return data


def _looks_like_fee(value) -> bool:
    """Return True if value looks like a valid fee (number, zero, or exemption word)."""
    if value is None:
        return True
    s = str(value).lower().strip()
    exempt_words = ("free", "exempt", "nil", "no fee", "0", "zero", "waived", "exempted")
    if any(w in s for w in exempt_words):
        return True
    # Check for numeric (possibly with ₹ and commas)
    cleaned = re.sub(r"[₹,\s]", "", s)
    return cleaned.isdigit() or re.match(r"^\d+(\.\d+)?$", cleaned) is not None


def _empty_extraction(event: dict, reason: str) -> dict:
    """Return a safely-empty extraction result when something goes wrong."""
    return {
        "exam_name": event.get("exam_name"),
        "event_type": event.get("event_type"),
        "post_info": {},
        "dates": {},
        "eligibility": {},
        "fees": {},
        "documents": [],
        "photo": {},
        "signature": {},
        "application_steps": [],
        "important_instructions": [],
        "official_website": None,
        "official_notification": None,
        "conflicts": [],
        "unknown_fields": ["All fields — extraction unavailable"],
        "extraction_confidence": "Low",
        "official_doc_found": False,
        "error": reason,
    }
