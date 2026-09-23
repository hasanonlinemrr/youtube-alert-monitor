"""
YouTube Data API helpers.

Responsibilities:
- Parse channel URLs / handles / channel IDs
- Resolve channels by stable channel ID first
- Resolve channels by @handle
- Optional search fallback for unresolved handles
- Validate the resolved channel
- Retrieve uploads playlist
- Retrieve recent uploaded videos

This module does NOT send Telegram messages and does NOT run AI.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


CHANNEL_ID_RE = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")


def clean_text(value: Optional[str]) -> str:
    """Normalize a string for comparisons."""
    if not value:
        return ""

    return re.sub(r"\s+", " ", value).strip()


def normalize_handle(value: Optional[str]) -> str:
    """
    Convert a YouTube handle/url into a normalized handle.

    Examples:
        @Example       -> example
        youtube.com/@Example -> example
        https://youtube.com/@Example -> example
    """
    if not value:
        return ""

    value = value.strip()

    value = re.sub(
        r"^https?://(www\.)?youtube\.com/",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = value.strip("/")

    if value.lower().startswith("@"):
        value = value[1:]

    return value.strip().lower()


def extract_channel_id(value: Optional[str]) -> Optional[str]:
    """Return a channel ID if the supplied value contains one."""
    if not value:
        return None

    value = value.strip()

    # Direct channel ID
    if CHANNEL_ID_RE.fullmatch(value):
        return value

    # /channel/UCxxxxxxxx...
    match = re.search(
        r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})",
        value,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return None


def extract_handle(value: Optional[str]) -> Optional[str]:
    """Return a handle if the supplied value contains one."""
    if not value:
        return None

    value = value.strip()

    # Direct @handle
    if value.startswith("@"):
        return normalize_handle(value)

    # /@handle
    match = re.search(
        r"youtube\.com/@([^/?#]+)",
        value,
        flags=re.IGNORECASE,
    )

    if match:
        return normalize_handle(match.group(1))

    return None


def _channel_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a YouTube channel API item into our internal structure."""

    channel_id = item.get("id")

    if not channel_id or not CHANNEL_ID_RE.fullmatch(channel_id):
        return None

    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}

    related_playlists = content_details.get("relatedPlaylists") or {}

    uploads_playlist = related_playlists.get("uploads")

    if not uploads_playlist:
        return None

    title = clean_text(snippet.get("title"))
    custom_url = clean_text(snippet.get("customUrl"))

    return {
        "channel_id": channel_id,
        "name": title,
        "custom_url": custom_url,
        "uploads_playlist": uploads_playlist,
    }


def get_channel_by_id(
    youtube,
    channel_id: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a channel directly using its stable channel ID."""

    if not CHANNEL_ID_RE.fullmatch(channel_id or ""):
        return None

    response = (
        youtube.channels()
        .list(
            part="snippet,contentDetails",
            id=channel_id,
            maxResults=1,
        )
        .execute()
    )

    items = response.get("items") or []

    if not items:
        return None

    return _channel_from_item(items[0])


def get_channel_by_handle(
    youtube,
    handle: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a channel using the official YouTube handle lookup."""

    normalized = normalize_handle(handle)

    if not normalized:
        return None

    response = (
        youtube.channels()
        .list(
            part="snippet,contentDetails",
            forHandle=normalized,
            maxResults=1,
        )
        .execute()
    )

    items = response.get("items") or []

    if not items:
        return None

    return _channel_from_item(items[0])


def search_channel_candidates(
    youtube,
    query: str,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """
    Search for possible channels.

    IMPORTANT:
    search.list is expensive compared with channels.list.
    This function should therefore be used only during channel
    validation/building, NOT during every monitoring run.
    """

    query = clean_text(query)

    if not query:
        return []

    response = (
        youtube.search()
        .list(
            part="snippet",
            q=query,
            type="channel",
            maxResults=max_results,
        )
        .execute()
    )

    candidates = []

    for item in response.get("items") or []:
        channel_id = (
            item.get("id") or {}
        ).get("channelId")

        snippet = item.get("snippet") or {}

        if not channel_id:
            continue

        candidates.append(
            {
                "channel_id": channel_id,
                "name": clean_text(snippet.get("channelTitle")),
                "description": clean_text(
                    snippet.get("description")
                ),
            }
        )

    return candidates


def get_channel_details_by_id(
    youtube,
    channel_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch complete channel information after a search result
    gives us a candidate channel ID.
    """

    return get_channel_by_id(youtube, channel_id)


def validate_channel_candidate(
    youtube,
    channel: Dict[str, Any],
    expected_handle: Optional[str] = None,
    expected_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate a resolved channel.

    Returns:
        valid
        reason
        channel data
    """

    if not channel:
        return {
            "valid": False,
            "reason": "empty_channel",
        }

    if not channel.get("channel_id"):
        return {
            "valid": False,
            "reason": "missing_channel_id",
        }

    if not channel.get("uploads_playlist"):
        return {
            "valid": False,
            "reason": "missing_uploads_playlist",
        }

    # If a handle was supplied, compare it with customUrl where possible.
    if expected_handle:
        expected = normalize_handle(expected_handle)
        custom_url = normalize_handle(
            channel.get("custom_url")
        )

        if custom_url:
            # YouTube may return a custom URL that does not exactly
            # match the current @handle formatting.
            if expected != custom_url:
                return {
                    "valid": False,
                    "reason": "handle_mismatch",
                    "expected_handle": expected,
                    "returned_custom_url": custom_url,
                }

    return {
        "valid": True,
        "reason": "validated",
        "channel": channel,
    }


def resolve_channel(
    youtube,
    raw_entry: str,
    supplied_channel_id: Optional[str] = None,
    supplied_name: Optional[str] = None,
    allow_search_fallback: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Resolve and validate one channel.

    Resolution order:

        1. Supplied channel ID
        2. URL channel ID
        3. @handle lookup
        4. Search fallback

    The search fallback is deliberately limited to the builder.
    """

    raw_entry = clean_text(raw_entry)

    supplied_channel_id = clean_text(supplied_channel_id)
    supplied_name = clean_text(supplied_name)

    expected_handle = extract_handle(raw_entry)

    # ---------------------------------------------------------
    # 1. Supplied channel ID
    # ---------------------------------------------------------

    if supplied_channel_id:
        channel = get_channel_by_id(
            youtube,
            supplied_channel_id,
        )

        if channel:
            result = validate_channel_candidate(
                youtube,
                channel,
                expected_handle=expected_handle,
                expected_name=supplied_name,
            )

            if result["valid"]:
                channel["resolution_method"] = "channel_id"
                return channel

    # ---------------------------------------------------------
    # 2. Channel ID embedded in URL
    # ---------------------------------------------------------

    embedded_id = extract_channel_id(raw_entry)

    if embedded_id:
        channel = get_channel_by_id(
            youtube,
            embedded_id,
        )

        if channel:
            result = validate_channel_candidate(
                youtube,
                channel,
                expected_handle=expected_handle,
                expected_name=supplied_name,
            )

            if result["valid"]:
                channel["resolution_method"] = "embedded_channel_id"
                return channel

    # ---------------------------------------------------------
    # 3. Official @handle lookup
    # ---------------------------------------------------------

    if expected_handle:
        channel = get_channel_by_handle(
            youtube,
            expected_handle,
        )

        if channel:
            result = validate_channel_candidate(
                youtube,
                channel,
                expected_handle=expected_handle,
                expected_name=supplied_name,
            )

            if result["valid"]:
                channel["resolution_method"] = "handle"
                return channel

    # ---------------------------------------------------------
    # 4. Search fallback
    #
    # Only used during build/validation.
    # ---------------------------------------------------------

    if allow_search_fallback:
        search_query = supplied_name or expected_handle

        candidates = search_channel_candidates(
            youtube,
            search_query,
            max_results=5,
        )

        # We only accept an exact normalized title match when
        # a channel name was explicitly supplied.
        if supplied_name:
            expected_name_normalized = normalize_name(
                supplied_name
            )

            for candidate in candidates:
                candidate_name = normalize_name(
                    candidate.get("name")
                )

                if candidate_name != expected_name_normalized:
                    continue

                channel = get_channel_details_by_id(
                    youtube,
                    candidate["channel_id"],
                )

                if not channel:
                    continue

                result = validate_channel_candidate(
                    youtube,
                    channel,
                    expected_handle=None,
                    expected_name=supplied_name,
                )

                if result["valid"]:
                    channel["resolution_method"] = (
                        "search_exact_name"
                    )
                    return channel

        # If no name was supplied, search is intentionally NOT
        # accepted automatically because display names are not
        # unique and search results can be ambiguous.
        return None

    return None


def normalize_name(value: Optional[str]) -> str:
    """Normalize a channel name for conservative comparison."""

    if not value:
        return ""

    value = value.lower().strip()

    # Remove punctuation.
    value = re.sub(r"[^\w\s]+", " ", value)

    # Collapse spaces.
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def get_new_videos(
    youtube,
    uploads_playlist_id: str,
    max_results: int = 20,
) -> List[Dict[str, Any]]:
    """
    Retrieve recent uploads from a channel's uploads playlist.
    """

    if not uploads_playlist_id:
        return []

    response = (
        youtube.playlistItems()
        .list(
            part="snippet,contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=max_results,
        )
        .execute()
    )

    videos = []

    for item in response.get("items") or []:
        snippet = item.get("snippet") or {}
        content_details = item.get("contentDetails") or {}

        video_id = content_details.get("videoId")

        if not video_id:
            continue

        videos.append(
            {
                "video_id": video_id,
                "title": clean_text(
                    snippet.get("title")
                ),
                "description": clean_text(
                    snippet.get("description")
                ),
                "published_at": snippet.get(
                    "publishedAt"
                ),
                "channel_id": snippet.get(
                    "channelId"
                ),
                "channel_title": clean_text(
                    snippet.get("channelTitle")
                ),
                "thumbnail": (
                    snippet.get("thumbnails", {})
                    .get("high", {})
                    .get("url")
                ),
            }
        )

    return videos
