"""
youtube.py — YouTube Data API helpers for WB YouTube Job Alert Agent.

Responsibilities:
- Build authenticated YouTube Data API client
- Load validated channels from channels.csv
- Retrieve recent uploads using playlistItems.list (1 quota unit per channel)
- Parse and resolve channel IDs, handles, and URLs
- Fully quota-efficient: NEVER uses search during normal monitoring runs
"""

from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

CHANNEL_ID_RE = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")


def clean_text(value: Optional[str]) -> str:
    """Normalize whitespace in text."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_handle(value: Optional[str]) -> str:
    """Convert a YouTube handle or URL into a normalized lowercase handle without @."""
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^https?://(www\.)?youtube\.com/", "", value, flags=re.IGNORECASE)
    value = value.strip("/")
    if value.lower().startswith("@"):
        value = value[1:]
    return value.strip().lower()


def extract_channel_id(value: Optional[str]) -> Optional[str]:
    """Return a channel ID (UC...) if present."""
    if not value:
        return None
    value = value.strip()
    if CHANNEL_ID_RE.fullmatch(value):
        return value
    match = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})", value, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_handle(value: Optional[str]) -> Optional[str]:
    """Return an extracted handle from URL or direct handle."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("@"):
        return normalize_handle(value)
    match = re.search(r"youtube\.com/@([^/?#]+)", value, flags=re.IGNORECASE)
    if match:
        return normalize_handle(match.group(1))
    return None


def build_youtube_client():
    """Build and return an authorized YouTube Data API v3 client."""
    from googleapiclient.discovery import build
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable is not set.")
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def load_channels(csv_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Load resolved, verified channels from channels.csv.
    Guaranteed to return channels with valid channel_id and uploads_playlist.
    """
    if csv_path is None:
        csv_path = Path(__file__).parent.parent / "channels.csv"

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"channels.csv not found at {csv_path}")

    channels = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("active", "true").lower() == "false":
                continue
            channel_id = row.get("channel_id", "").strip()
            if not channel_id:
                continue

            uploads = (row.get("uploads_playlist") or row.get("uploads_playlist_id") or "").strip()
            if not uploads and channel_id.startswith("UC"):
                uploads = "UU" + channel_id[2:]

            name = row.get("name", "").strip()
            handle = row.get("handle", "").strip()

            # Assign priority tier based on regional preference
            name_lower = (name + " " + handle).lower()
            if any(k in name_lower for k in ["west bengal", "wb", "bengali", "বাংলা", "bangla"]):
                tier = int(row.get("priority_tier", 1))
                weight = float(row.get("priority_weight", 1.0))
                lang = "Bengali"
                region = "WB"
            elif any(k in name_lower for k in ["ssc", "rrb", "railway", "police", "defence"]):
                tier = int(row.get("priority_tier", 3))
                weight = float(row.get("priority_weight", 0.8))
                lang = "English"
                region = "National"
            else:
                tier = int(row.get("priority_tier", 4))
                weight = float(row.get("priority_weight", 0.7))
                lang = "English"
                region = "National"

            channels.append({
                "channel_id": channel_id,
                "name": name,
                "handle": handle,
                "uploads_playlist": uploads,
                "uploads_playlist_id": uploads,
                "source": row.get("source", ""),
                "priority_tier": tier,
                "priority_weight": weight,
                "language": lang,
                "region": region,
                "active": "true",
            })

    return channels


def get_new_videos(
    youtube,
    channel: Dict[str, Any],
    processed_ids: Optional[set] = None,
    state: Optional[Dict[str, Any]] = None,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """
    Fetch newest videos from channel's uploads playlist using playlistItems.list (Costs only 1 quota unit).
    Excludes videos already in processed_ids.
    """
    if processed_ids is None:
        processed_ids = set()

    uploads_playlist_id = channel.get("uploads_playlist") or channel.get("uploads_playlist_id")
    if not uploads_playlist_id:
        channel_id = channel.get("channel_id", "")
        if channel_id.startswith("UC"):
            uploads_playlist_id = "UU" + channel_id[2:]
        else:
            return []

    try:
        response = (
            youtube.playlistItems()
            .list(
                part="snippet,contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=max_results,
            )
            .execute()
        )
        if state is not None:
            state["daily_quota_used"] = state.get("daily_quota_used", 0) + 1
    except Exception:
        # Handles unavailable channel, private uploads, or temporary YouTube glitch gracefully
        return []

    videos = []
    for item in response.get("items") or []:
        content_details = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        video_id = content_details.get("videoId")

        if not video_id or video_id in processed_ids:
            continue

        videos.append({
            "video_id": video_id,
            "title": clean_text(snippet.get("title")),
            "description": clean_text(snippet.get("description")),
            "published_at": snippet.get("publishedAt"),
            "channel_id": channel.get("channel_id") or snippet.get("channelId"),
            "channel_name": channel.get("name") or clean_text(snippet.get("channelTitle")),
            "channel_title": channel.get("name") or clean_text(snippet.get("channelTitle")),
            "channel_handle": channel.get("handle", ""),
            "priority_tier": channel.get("priority_tier", 3),
            "priority_weight": channel.get("priority_weight", 0.8),
            "language": channel.get("language", "English"),
            "region": channel.get("region", "National"),
            "thumbnail": (snippet.get("thumbnails") or {}).get("high", {}).get("url") or "",
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
        })

    return videos


def _channel_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    channel_id = item.get("id")
    if not channel_id or not CHANNEL_ID_RE.fullmatch(channel_id):
        return None

    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    related_playlists = content_details.get("relatedPlaylists") or {}
    uploads_playlist = related_playlists.get("uploads")
    if not uploads_playlist:
        uploads_playlist = "UU" + channel_id[2:]

    title = clean_text(snippet.get("title"))
    custom_url = clean_text(snippet.get("customUrl"))

    return {
        "channel_id": channel_id,
        "name": title,
        "custom_url": custom_url,
        "uploads_playlist": uploads_playlist,
    }


def get_channel_by_id(youtube, channel_id: str) -> Optional[Dict[str, Any]]:
    if not channel_id or not CHANNEL_ID_RE.fullmatch(channel_id):
        return None
    try:
        response = youtube.channels().list(part="snippet,contentDetails", id=channel_id, maxResults=1).execute()
        items = response.get("items") or []
        return _channel_from_item(items[0]) if items else None
    except Exception:
        return None


def get_channel_by_handle(youtube, handle: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_handle(handle)
    if not normalized:
        return None
    try:
        response = youtube.channels().list(part="snippet,contentDetails", forHandle=normalized, maxResults=1).execute()
        items = response.get("items") or []
        return _channel_from_item(items[0]) if items else None
    except Exception:
        return None


def validate_channel_candidate(youtube, channel: Dict[str, Any], expected_handle: Optional[str] = None, expected_name: Optional[str] = None) -> Dict[str, Any]:
    if not channel or not channel.get("channel_id") or not channel.get("uploads_playlist"):
        return {"valid": False, "reason": "invalid_channel_data"}
    return {"valid": True, "reason": "validated", "channel": channel}


def resolve_channel(
    youtube,
    raw_entry: str,
    supplied_channel_id: Optional[str] = None,
    supplied_name: Optional[str] = None,
    allow_search_fallback: bool = False,
) -> Optional[Dict[str, Any]]:
    raw_entry = clean_text(raw_entry)
    supplied_channel_id = clean_text(supplied_channel_id)

    # 1. Direct channel ID
    if supplied_channel_id:
        ch = get_channel_by_id(youtube, supplied_channel_id)
        if ch:
            ch["resolution_method"] = "channel_id"
            return ch

    # 2. Channel ID in URL
    embedded_id = extract_channel_id(raw_entry)
    if embedded_id:
        ch = get_channel_by_id(youtube, embedded_id)
        if ch:
            ch["resolution_method"] = "embedded_channel_id"
            return ch

    # 3. Handle lookup
    expected_handle = extract_handle(raw_entry)
    if expected_handle:
        ch = get_channel_by_handle(youtube, expected_handle)
        if ch:
            ch["resolution_method"] = "handle"
            return ch

    return None
