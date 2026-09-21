"""
transcript.py — YouTube transcript/caption fetcher with graceful fallback.
Uses youtube-transcript-api (unofficial) with fallback to title+description.
"""

import logging
from typing import Optional

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)
from youtube_transcript_api._errors import RequestBlocked

from utils import clean_transcript, get_logger

logger = get_logger("transcript")

# Preferred language order: manual Bengali > auto Bengali > manual English > auto English
PREFERRED_LANGUAGES = ["bn", "en"]


def fetch_transcript(video_id: str) -> Optional[str]:
    """
    Fetch the transcript/captions for a YouTube video.

    Tries in order:
    1. Manual Bengali captions
    2. Auto-generated Bengali captions
    3. Manual English captions
    4. Auto-generated English captions
    5. Any available transcript

    Returns cleaned plain text, or None if nothing is available.
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except (TranscriptsDisabled, VideoUnavailable) as e:
        logger.info(f"No transcripts available for {video_id}: {e}")
        return None
    except RequestBlocked as e:
        logger.warning(f"Transcript request blocked for {video_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error listing transcripts for {video_id}: {e}")
        return None

    # Try manual transcripts first (higher quality)
    for lang in PREFERRED_LANGUAGES:
        try:
            transcript = transcript_list.find_manually_created_transcript([lang])
            raw = _extract_text(transcript.fetch())
            logger.info(f"Got manual '{lang}' transcript for {video_id} ({len(raw)} chars)")
            return clean_transcript(raw)
        except NoTranscriptFound:
            pass
        except Exception as e:
            logger.warning(f"Error fetching manual '{lang}' transcript for {video_id}: {e}")

    # Try auto-generated transcripts
    for lang in PREFERRED_LANGUAGES:
        try:
            transcript = transcript_list.find_generated_transcript([lang])
            raw = _extract_text(transcript.fetch())
            logger.info(f"Got auto-generated '{lang}' transcript for {video_id} ({len(raw)} chars)")
            return clean_transcript(raw)
        except NoTranscriptFound:
            pass
        except Exception as e:
            logger.warning(f"Error fetching auto '{lang}' transcript for {video_id}: {e}")

    # Last resort: any available transcript
    try:
        all_transcripts = list(transcript_list)
        if all_transcripts:
            t = all_transcripts[0]
            raw = _extract_text(t.fetch())
            logger.info(
                f"Got fallback transcript [{t.language_code}] for {video_id} ({len(raw)} chars)"
            )
            return clean_transcript(raw)
    except Exception as e:
        logger.warning(f"Could not get any transcript for {video_id}: {e}")

    return None


def _extract_text(segments: list) -> str:
    """Join transcript segments into a single text string."""
    return " ".join(seg.get("text", "") for seg in segments if seg.get("text"))


def get_transcript_with_fallback(video: dict) -> dict:
    """
    High-level function that returns both the transcript (if available)
    and a source_note explaining what was used.

    video: dict with at minimum 'video_id', 'title', 'description'

    Returns:
        {
            'text': str,           # combined usable text
            'transcript': str | None,
            'source_note': str     # e.g. "Transcript (Bengali auto-generated)"
        }
    """
    video_id = video["video_id"]
    title = video.get("title", "")
    description = video.get("description", "")

    transcript = fetch_transcript(video_id)

    if transcript:
        combined = f"VIDEO TITLE: {title}\n\nDESCRIPTION:\n{description}\n\nTRANSCRIPT:\n{transcript}"
        source_note = "Transcript + Description"
    else:
        combined = f"VIDEO TITLE: {title}\n\nDESCRIPTION:\n{description}"
        source_note = "Title + Description only (transcript unavailable)"
        logger.info(f"Using title+description fallback for {video_id}")

    return {
        "text": combined,
        "transcript": transcript,
        "source_note": source_note,
    }
