"""
main.py — Main orchestrator for WB YouTube Job Alert Agent.
Entry point for the monitor.yml GitHub Actions workflow.

Usage:
    python src/main.py [--bootstrap] [--test] [--dry-run]
"""

import argparse
import logging
import sys
import os
from pathlib import Path

# Ensure src/ is in path when running from repo root
sys.path.insert(0, str(Path(__file__).parent))

from youtube import build_youtube_client, load_channels, get_new_videos
from classifier import classify_video
from dedup import merge_events, should_send_alert
from telegram import send_alert
from utils import (
    load_state, save_state, load_events, save_events,
    load_settings, now_ist, get_logger
)

logger = get_logger("main")


def parse_args():
    parser = argparse.ArgumentParser(description="WB YouTube Job Alert Monitor")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Bootstrap mode: mark all videos as seen without sending alerts")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: send alerts with 🧪 TEST prefix")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry run: classify videos but do not send any Telegram messages")
    parser.add_argument("--channel-limit", type=int, default=0,
                        help="Limit processing to N channels (for testing)")
    return parser.parse_args()


def main():
    args = parse_args()
    settings = load_settings()

    # Mode resolution: CLI args override settings.json
    bootstrap_mode = args.bootstrap or settings.get("modes", {}).get("bootstrap_mode", False)
    test_mode = args.test or settings.get("modes", {}).get("test_mode", False)
    dry_run = args.dry_run

    if bootstrap_mode:
        logger.info("=" * 60)
        logger.info("BOOTSTRAP MODE — No alerts will be sent.")
        logger.info("All current videos will be marked as seen.")
        logger.info("=" * 60)

    if test_mode:
        logger.info("TEST MODE — All alerts will have 🧪 prefix.")

    logger.info(f"Run started at: {now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}")

    # ------------------------------------------------------------------ #
    # Load state and events
    # ------------------------------------------------------------------ #
    state = load_state()
    existing_events = load_events()
    processed_ids = set(state.get("processed_video_ids", []))

    logger.info(
        f"State: {len(processed_ids)} processed videos, "
        f"{len(existing_events)} existing events"
    )

    # ------------------------------------------------------------------ #
    # Load channels
    # ------------------------------------------------------------------ #
    try:
        channels = load_channels()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.channel_limit:
        channels = channels[:args.channel_limit]
        logger.info(f"Channel limit set: processing {len(channels)} channels")

    # ------------------------------------------------------------------ #
    # Build YouTube client
    # ------------------------------------------------------------------ #
    try:
        youtube = build_youtube_client()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # Phase 1: Collect new videos from all channels
    # ------------------------------------------------------------------ #
    all_new_videos = []
    channels_checked = 0
    channels_with_new = 0

    for channel in channels:
        new_videos = get_new_videos(
            youtube=youtube,
            channel=channel,
            processed_ids=processed_ids,
            state=state,
        )

        channels_checked += 1
        if new_videos:
            channels_with_new += 1
            all_new_videos.extend(new_videos)
            logger.info(
                f"  {channel.get('name', channel.get('channel_id'))}: "
                f"{len(new_videos)} new video(s)"
            )

        # Mark all fetched videos as processed regardless of classification
        # This prevents re-checking the same video next run
        for v in new_videos:
            processed_ids.add(v["video_id"])

    logger.info(
        f"Checked {channels_checked} channels. "
        f"{channels_with_new} had new videos. "
        f"Total new: {len(all_new_videos)}"
    )

    # ------------------------------------------------------------------ #
    # Bootstrap: save state and exit without alerting
    # ------------------------------------------------------------------ #
    if bootstrap_mode:
        state["processed_video_ids"] = list(processed_ids)
        state["bootstrap_complete"] = True
        state["last_run"] = now_ist().isoformat()
        save_state(state)
        logger.info(
            f"Bootstrap complete. {len(processed_ids)} video IDs saved. "
            "Set bootstrap_mode=false in settings.json before the next run."
        )
        return

    # ------------------------------------------------------------------ #
    # Phase 2: Classify new videos (2-stage: keywords → AI)
    # ------------------------------------------------------------------ #
    classified = []
    skipped_irrelevant = 0

    for video in all_new_videos:
        result = classify_video(video, state)
        if result.get("relevant"):
            # Merge classification info into video dict
            video.update({
                "relevant": True,
                "event_type": result.get("event_type"),
                "exam_name": result.get("exam_name"),
                "confidence": result.get("confidence", 0.5),
                "classification_source": result.get("source", "unknown"),
                "classification_reasoning": result.get("reasoning", ""),
            })
            classified.append(video)
            logger.info(
                f"  ✅ RELEVANT: [{result.get('event_type')}] "
                f"{result.get('exam_name', '?')} — "
                f"'{video['title'][:60]}'"
            )
        else:
            skipped_irrelevant += 1
            logger.debug(f"  ❌ Irrelevant: '{video['title'][:60]}'")

    logger.info(
        f"Classification: {len(classified)} relevant, "
        f"{skipped_irrelevant} irrelevant out of {len(all_new_videos)} new videos"
    )

    # ------------------------------------------------------------------ #
    # Phase 3: Deduplication — group into events
    # ------------------------------------------------------------------ #
    new_events, updated_events = merge_events(classified, existing_events)

    logger.info(
        f"Deduplication: {len(new_events)} new/changed events from "
        f"{len(classified)} classified videos"
    )

    # ------------------------------------------------------------------ #
    # Phase 4: Send alerts
    # ------------------------------------------------------------------ #
    alerts_sent = 0

    for event in new_events:
        if not should_send_alert(event):
            logger.info(
                f"  ⏭️ Not sending alert for {event['event_id']} "
                f"(confidence: {event.get('confirmation_level')})"
            )
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] Would send alert for: {event['event_id']}")
            event["alert_sent"] = False
            continue

        logger.info(f"  📣 Sending alert: {event['event_id']}")
        msg_id = send_alert(event, test_mode=test_mode)
        if msg_id:
            event["alert_sent"] = True
            event["telegram_message_id"] = msg_id
            event["status"] = "ALERTED"
            alerts_sent += 1
        else:
            logger.error(f"Failed to send alert for {event['event_id']}")

    # ------------------------------------------------------------------ #
    # Phase 5: Save state
    # ------------------------------------------------------------------ #
    state["processed_video_ids"] = list(processed_ids)
    state["last_run"] = now_ist().isoformat()
    save_state(state)
    save_events(updated_events)

    logger.info(
        f"Run complete. Alerts sent: {alerts_sent}. "
        f"State saved ({len(processed_ids)} processed IDs, "
        f"{len(updated_events)} events)."
    )
    logger.info(
        f"Quota used today: {state.get('daily_quota_used', 0)} YouTube units, "
        f"{state.get('daily_ai_requests', 0)} AI requests."
    )


if __name__ == "__main__":
    main()
