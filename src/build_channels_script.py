"""
build_channels_script.py — Resolves channels_raw.txt → channels.csv
Run by the build_channels.yml workflow.
"""

import csv
import sys
import os
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from youtube import build_youtube_client, resolve_channel
from utils import ROOT_DIR, get_logger

logger = get_logger("build_channels")

CHANNELS_RAW = ROOT_DIR / "channels_raw.txt"
CHANNELS_CSV = ROOT_DIR / "channels.csv"

# Source priority rules: keywords in channel name → tier
PRIORITY_RULES = [
    # (keywords_in_name_lower, tier, weight, language, region)
    (["west bengal", "wb", "bengali", "বাংলা", "বাংলাদেশ"], 1, 1.0, "Bengali", "WB"),
    (["bengal", "bangla", "বাংলা"], 2, 0.9, "Bengali", "Other"),
    (["west bengal"], 3, 0.8, "English", "WB"),
    (["ssc", "rrb", "bank", "upsc", "railway", "police"], 4, 0.7, "English", "National"),
]
DEFAULT_TIER = 5
DEFAULT_WEIGHT = 0.6


def detect_priority(channel_name: str, handle: str) -> tuple[int, float, str, str]:
    combined = (channel_name + " " + handle).lower()
    for keywords, tier, weight, lang, region in PRIORITY_RULES:
        if any(kw in combined for kw in keywords):
            return tier, weight, lang, region
    return DEFAULT_TIER, DEFAULT_WEIGHT, "English", "National"


def main():
    if not CHANNELS_RAW.exists():
        logger.error(f"channels_raw.txt not found at {CHANNELS_RAW}")
        sys.exit(1)

    # Read raw entries
    raw_lines = CHANNELS_RAW.read_text(encoding="utf-8").splitlines()
    entries = [
        line.strip() for line in raw_lines
        if line.strip() and not line.strip().startswith("#")
    ]

    logger.info(f"Processing {len(entries)} channel entries...")

    try:
        youtube = build_youtube_client()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    resolved = []
    failed = []

    for i, entry in enumerate(entries, 1):
        logger.info(f"[{i}/{len(entries)}] Resolving: {entry[:60]}")
        info = resolve_channel(youtube, entry)

        if info:
            tier, weight, lang, region = detect_priority(info["name"], info.get("handle", ""))
            info["priority_tier"] = tier
            info["priority_weight"] = weight
            info["language"] = lang
            info["region"] = region
            info["active"] = "true"
            info["raw_entry"] = entry
            resolved.append(info)
            logger.info(
                f"  ✅ {info['name']} "
                f"(tier={tier}, playlist={info['uploads_playlist_id']})"
            )
        else:
            failed.append(entry)
            logger.warning(f"  ❌ Could not resolve: {entry}")

        # Polite rate limiting — YouTube API allows burst but be respectful
        if i % 10 == 0:
            time.sleep(2)

    # Write channels.csv
    fieldnames = [
        "channel_id", "handle", "name", "uploads_playlist_id",
        "priority_tier", "priority_weight", "language", "region",
        "active", "raw_entry",
    ]

    with open(CHANNELS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        # Sort by priority tier (best sources first)
        resolved.sort(key=lambda r: r["priority_tier"])
        for row in resolved:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    logger.info(f"\n{'=' * 50}")
    logger.info(f"✅ Resolved: {len(resolved)} channels")
    logger.info(f"❌ Failed:   {len(failed)} channels")
    if failed:
        logger.info("Failed entries (check manually):")
        for f_entry in failed:
            logger.info(f"  - {f_entry}")
    logger.info(f"channels.csv written to: {CHANNELS_CSV}")


if __name__ == "__main__":
    main()
