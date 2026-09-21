"""
youtube.py — YouTube Data API v3 wrapper
Uses uploads-playlist approach (recommended by Google) to avoid expensive search.list calls.
Each playlistItems.list call costs 1 quota unit vs search.list which costs 100.
"""

import csv
import os
import time
import logging
from pathlib import Path
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from utils import (
    ROOT_DIR, CHANNELS_CSV, load_settings,
    check_and_increment_quota, get_logger
)

logger = get_logger("youtube")


def build_youtube_client():
    """Build the YouTube API client using the API key from environment."""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is not set.")
    return build("youtube", "v3", developerKey=api_key)


# ---------------------------------------------------------------------------
# Channel CSV loader
# ---------------------------------------------------------------------------

def load_channels() -> list[dict]:
    """
    Load resolved channel data from channels.csv.
    Returns list of dicts with keys: channel_id, handle, name, uploads_playlist_id,
    language, region, priority_tier, priority_weight
    """
    if not CHANNELS_CSV.exists():
        raise FileNotFoundError(
            f"channels.csv not found at {CHANNELS_CSV}. "
            "Please run the build_channels workflow first."
        )
    channels = []
    with open(CHANNELS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("active", "true").lower() == "false":
                continue
            channels.append(row)
    logger.info(f"Loaded {len(channels)} active channels from channels.csv")
    return channels


# ---------------------------------------------------------------------------
# Core API calls with retry logic
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(HttpError),
)
def _api_call_with_retry(request):
    """Execute an API request with exponential backoff on transient errors."""
    try:
        return request.execute()
    except HttpError as e:
        if e.resp.status in (429, 500, 503):
            logger.warning(f"Transient API error {e.resp.status}, will retry...")
            raise  # Let tenacity retry
        raise  # Other errors are fatal


# ---------------------------------------------------------------------------
# Channel resolution (used by build_channels workflow)
# ---------------------------------------------------------------------------

def resolve_channel(youtube, raw_entry: str) -> Optional[dict]:
    """
    Given a raw channel URL, handle, or ID, resolve to full channel info.
    Returns dict with channel_id, handle, name, uploads_playlist_id or None.
    """
    raw = raw_entry.strip()
    if not raw or raw.startswith("#"):
        return None

    channel_id = None
    handle = None

    # Extract from URL formats
    if "youtube.com/@" in raw:
        handle = raw.split("youtube.com/@")[-1].split("/")[0].split("?")[0]
    elif "youtube.com/channel/" in raw:
        channel_id = raw.split("youtube.com/channel/")[-1].split("/")[0].split("?")[0]
    elif raw.startswith("@"):
        handle = raw[1:]
    elif raw.startswith("UC") and len(raw) == 24:
        channel_id = raw
    else:
        # Try as handle anyway
        handle = raw.lstrip("@")

    try:
        if channel_id:
            response = _api_call_with_retry(
                youtube.channels().list(
                    part="snippet,contentDetails",
                    id=channel_id,
                    maxResults=1,
                )
            )
        elif handle:
            response = _api_call_with_retry(
                youtube.channels().list(
                    part="snippet,contentDetails",
                    forHandle=handle,
                    maxResults=1,
                )
            )
        else:
            return None

        items = response.get("items", [])
        if not items:
            logger.warning(f"Could not resolve channel: {raw}")
            return None

        item = items[0]
        return {
            "channel_id": item["id"],
            "handle": item["snippet"].get("customUrl", handle or ""),
            "name": item["snippet"]["title"],
            "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"],
        }

    except HttpError as e:
        logger.error(f"API error resolving {raw}: {e}")
        return None


# ---------------------------------------------------------------------------
# New video fetching
# ---------------------------------------------------------------------------

def get_new_videos(
    youtube,
    channel: dict,
    processed_ids: set,
    state: dict,
    max_results: int = 5,
) -> list[dict]:
    """
    Fetch the most recent videos from a channel's uploads playlist.
    Returns only videos not in processed_ids.
    Uses playlistItems.list (1 quota unit per page).
    """
    playlist_id = channel.get("uploads_playlist_id")
    if not playlist_id:
        logger.warning(f"No uploads_playlist_id for channel {channel.get('name')}")
        return []

    if not check_and_increment_quota(state, amount=1):
        logger.warning("Quota limit reached, skipping channel fetch")
        return []

    settings = load_settings()
    max_results = min(
        max_results,
        settings.get("youtube", {}).get("max_videos_per_channel", 5),
    )

    try:
        response = _api_call_with_retry(
            youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=max_results,
                order="date",
            )
        )
    except HttpError as e:
        logger.error(f"Error fetching playlist {playlist_id}: {e}")
        return []

    new_videos = []
    for item in response.get("items", []):
        video_id = item["contentDetails"]["videoId"]
        if video_id in processed_ids:
            continue

        snippet = item["snippet"]
        new_videos.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "published_at": snippet.get("publishedAt", ""),
            "channel_id": channel["channel_id"],
            "channel_name": channel.get("name", ""),
            "channel_handle": channel.get("handle", ""),
            "priority_tier": int(channel.get("priority_tier", 5)),
            "priority_weight": float(channel.get("priority_weight", 0.6)),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })

    return new_videos


def get_video_details(youtube, video_id: str, state: dict) -> Optional[dict]:
    """
    Fetch full video details (description, tags, etc.) via videos.list.
    1 quota unit.
    """
    if not check_and_increment_quota(state, amount=1):
        return None
    try:
        response = _api_call_with_retry(
            youtube.videos().list(
                part="snippet,contentDetails",
                id=video_id,
            )
        )
        items = response.get("items", [])
        if not items:
            return None
        item = items[0]
        snippet = item["snippet"]
        return {
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "tags": snippet.get("tags", []),
            "published_at": snippet.get("publishedAt", ""),
            "channel_id": snippet.get("channelId", ""),
            "channel_title": snippet.get("channelTitle", ""),
            "duration": item.get("contentDetails", {}).get("duration", ""),
        }
    except HttpError as e:
        logger.error(f"Error fetching video details {video_id}: {e}")
        return None
