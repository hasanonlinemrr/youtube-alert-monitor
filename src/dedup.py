"""
dedup.py — Event deduplication and merge engine.

Groups multiple videos reporting the same exam+event into ONE event.
Detects status changes (APPLICATION_OPEN_SOON → APPLICATION_OPEN) as NEW events.
"""

import hashlib
import re
import logging
from datetime import datetime
from typing import Optional

from fuzzywuzzy import fuzz

from utils import (
    load_keywords, normalize_exam_name, make_event_id,
    load_settings, now_ist, get_logger
)

logger = get_logger("dedup")

FUZZY_MATCH_THRESHOLD = 85  # Score (0-100) for fuzzy exam name matching


def merge_events(
    classified_videos: list[dict],
    existing_events: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Given a list of classified videos (each with exam_name, event_type),
    merge them with existing events and detect:
      - Truly new events
      - Status changes that should generate new alerts

    Returns:
        new_or_changed_events: events that need to be alerted
        updated_all_events: full updated events list (for saving to events.json)
    """
    keywords = load_keywords()
    normalizations = keywords.get("exam_name_normalizations", {})

    # Group classified videos by (normalized_exam_name, event_type)
    groups: dict[str, dict] = {}

    for video in classified_videos:
        if not video.get("relevant"):
            continue

        exam_raw = video.get("exam_name") or _extract_exam_from_title(
            video.get("title", ""), normalizations
        )
        exam_name = normalize_exam_name(exam_raw, normalizations)
        event_type = video.get("event_type")

        if not exam_name or not event_type:
            logger.debug(f"Skipping video with missing exam_name or event_type: {video.get('title', '')}")
            continue

        group_key = f"{exam_name}|||{event_type}"

        if group_key not in groups:
            groups[group_key] = {
                "exam_name": exam_name,
                "event_type": event_type,
                "source_videos": [],
                "source_channels": [],
                "best_confidence": 0.0,
            }

        g = groups[group_key]
        g["source_videos"].append({
            "video_id": video["video_id"],
            "title": video.get("title", ""),
            "url": video.get("url", ""),
            "channel_name": video.get("channel_name", ""),
            "channel_id": video.get("channel_id", ""),
            "priority_weight": video.get("priority_weight", 0.6),
            "published_at": video.get("published_at", ""),
        })

        if video.get("channel_id") not in g["source_channels"]:
            g["source_channels"].append(video["channel_id"])

        if video.get("confidence", 0) > g["best_confidence"]:
            g["best_confidence"] = video["confidence"]

    # For each group, check against existing events
    new_or_changed = []
    all_events = list(existing_events)

    for group_key, group in groups.items():
        exam_name = group["exam_name"]
        event_type = group["event_type"]

        existing = _find_existing_event(exam_name, event_type, all_events)

        if existing:
            # Update source list with any new videos
            _merge_sources(existing, group)
            existing["last_checked"] = now_ist().isoformat()
            logger.debug(f"Updated existing event: {existing['event_id']}")
            # No new alert for an already-alerted event unless status changed
            # (status changes are detected by having different event_type → different group_key)
        else:
            # Brand new event
            event = _create_event(exam_name, event_type, group)
            all_events.append(event)
            new_or_changed.append(event)
            logger.info(f"New event detected: {event['event_id']} ({len(group['source_channels'])} sources)")

    return new_or_changed, all_events


def _create_event(exam_name: str, event_type: str, group: dict) -> dict:
    """Create a new event record."""
    event_id = make_event_id(exam_name, event_type)
    source_videos = group["source_videos"]

    # Sort by priority_weight descending (tier 1 channels first)
    source_videos.sort(key=lambda v: v.get("priority_weight", 0), reverse=True)

    confidence = _compute_confidence(group)

    return {
        "event_id": event_id,
        "exam_name": exam_name,
        "event_type": event_type,
        "source_videos": source_videos,
        "source_channels": group["source_channels"],
        "best_confidence": group["best_confidence"],
        "confirmation_level": confidence["level"],
        "source_count": len(group["source_channels"]),
        "status": "NEW",
        "alert_sent": False,
        "first_detected": now_ist().isoformat(),
        "last_checked": now_ist().isoformat(),
        "full_extract_requested": False,
        "full_extract_completed": False,
        "telegram_message_id": None,
    }


def _merge_sources(existing: dict, group: dict) -> None:
    """Add new source videos and channels to an existing event."""
    existing_video_ids = {v["video_id"] for v in existing.get("source_videos", [])}
    for vid in group["source_videos"]:
        if vid["video_id"] not in existing_video_ids:
            existing.setdefault("source_videos", []).append(vid)

    existing_channels = set(existing.get("source_channels", []))
    for ch in group["source_channels"]:
        if ch not in existing_channels:
            existing.setdefault("source_channels", []).append(ch)

    # Update confidence if improved
    if group["best_confidence"] > existing.get("best_confidence", 0):
        existing["best_confidence"] = group["best_confidence"]

    # Recalculate confirmation level
    conf = _compute_confidence({
        "source_channels": existing["source_channels"],
        "best_confidence": existing["best_confidence"],
    })
    existing["confirmation_level"] = conf["level"]
    existing["source_count"] = len(existing["source_channels"])


def _find_existing_event(
    exam_name: str, event_type: str, all_events: list[dict]
) -> Optional[dict]:
    """
    Find an existing event matching exam_name + event_type.
    Uses fuzzy matching to handle slight name variations.
    """
    for event in all_events:
        if event.get("event_type") != event_type:
            continue
        existing_name = event.get("exam_name", "")
        score = fuzz.ratio(exam_name.lower(), existing_name.lower())
        if score >= FUZZY_MATCH_THRESHOLD:
            return event
    return None


def _compute_confidence(group: dict) -> dict:
    """
    Determine confirmation level based on source count and AI confidence.
    Returns {'level': 'HIGH'|'MEDIUM'|'LOW', 'auto_send': bool}
    """
    settings = load_settings()
    min_sources = settings.get("confirmation", {}).get("min_sources", 1)
    high_conf_threshold = settings.get("confirmation", {}).get("high_confidence_threshold", 0.85)
    auto_send_high = settings.get("confirmation", {}).get("auto_send_high_confidence", True)

    source_count = len(group.get("source_channels", []))
    best_confidence = group.get("best_confidence", 0.0)

    if source_count >= 2:
        return {"level": "HIGH", "auto_send": True}
    elif source_count >= 1 and best_confidence >= high_conf_threshold:
        return {"level": "HIGH", "auto_send": auto_send_high}
    elif source_count >= 1 and best_confidence >= 0.65:
        return {"level": "MEDIUM", "auto_send": True}
    else:
        return {"level": "LOW", "auto_send": False}


def _extract_exam_from_title(title: str, normalizations: dict) -> str:
    """
    Attempt to extract exam name from video title when AI didn't provide one.
    Uses the normalization map.
    """
    title_lower = title.lower()
    for canonical, variants in normalizations.items():
        if canonical.startswith("_"):
            continue
        for variant in variants:
            if isinstance(variant, str) and variant.lower() in title_lower:
                return canonical
    # Return first meaningful words of title as fallback
    words = title.split()[:6]
    return " ".join(words) if words else "Unknown Exam"


def should_send_alert(event: dict) -> bool:
    """
    Determine whether an event should be sent as a Telegram alert.
    Respects confirmation logic and auto_send settings.
    """
    # Don't re-alert
    if event.get("alert_sent"):
        return False

    conf = _compute_confidence({
        "source_channels": event.get("source_channels", []),
        "best_confidence": event.get("best_confidence", 0.0),
    })

    return conf.get("auto_send", False)
