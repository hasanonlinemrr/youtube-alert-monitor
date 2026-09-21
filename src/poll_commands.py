"""
poll_commands.py — Telegram button press handler.
Run by telegram_commands.yml workflow.
Polls getUpdates, processes EXTRACT/SKIP callbacks, triggers extractor.
"""

import sys
import os
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from telegram import (
    poll_updates, answer_callback,
    send_processing_message, send_full_extraction
)
from extractor import extract_full_information
from utils import (
    load_state, save_state, load_events, save_events,
    load_settings, now_ist, get_logger
)

logger = get_logger("poll_commands")


def main():
    logger.info(f"Polling Telegram updates at {now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}")

    settings = load_settings()
    test_mode = settings.get("modes", {}).get("test_mode", False)

    state = load_state()
    events = load_events()

    updates = poll_updates()

    if not updates:
        logger.info("No new updates.")
        return

    events_by_id = {e["event_id"]: e for e in events}
    changed = False

    for update in updates:
        callback = update.get("callback_query")
        if not callback:
            continue

        callback_id = callback["id"]
        data = callback.get("data", "")
        parts = data.split(":", 1)

        if len(parts) != 2:
            logger.warning(f"Unexpected callback data: {data}")
            answer_callback(callback_id, "⚠️ Unknown action")
            continue

        action, event_id = parts

        if action == "skip":
            logger.info(f"SKIP pressed for event: {event_id}")
            answer_callback(callback_id, "⏭️ Skipped")
            if event_id in events_by_id:
                events_by_id[event_id]["status"] = "SKIPPED"
                changed = True

        elif action == "extract":
            logger.info(f"EXTRACT pressed for event: {event_id}")
            answer_callback(callback_id, "⏳ Extracting... please wait 30–60 seconds")
            send_processing_message(event_id)

            event = events_by_id.get(event_id)
            if not event:
                logger.error(f"Event not found in events.json: {event_id}")
                continue

            if event.get("full_extract_completed"):
                logger.info(f"Extraction already completed for {event_id}, re-sending")

            event["full_extract_requested"] = True

            extraction = extract_full_information(event, state)
            if extraction:
                success = send_full_extraction(event, extraction, test_mode=test_mode)
                if success:
                    event["full_extract_completed"] = True
                    event["status"] = "EXTRACTED"
                    changed = True
                    logger.info(f"Full extraction sent for {event_id}")
                else:
                    logger.error(f"Failed to send extraction for {event_id}")
            else:
                logger.error(f"Extraction failed for {event_id}")

        else:
            logger.warning(f"Unknown action: {action}")
            answer_callback(callback_id, "⚠️ Unknown action")

    if changed:
        save_state(state)
        save_events(list(events_by_id.values()))
        logger.info("State and events saved.")


if __name__ == "__main__":
    main()
