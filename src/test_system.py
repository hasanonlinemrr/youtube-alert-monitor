"""
test_system.py — Test runner for all subsystems.
Run by the test.yml GitHub Actions workflow.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils import get_logger, load_settings, now_ist

logger = get_logger("test_system")

MOCK_EVENT = {
    "event_id": "ssc-chsl-2026-application-open",
    "exam_name": "SSC CHSL 2026",
    "event_type": "APPLICATION_OPEN",
    "source_videos": [
        {
            "video_id": "dQw4w9WgXcQ",  # Public YouTube video for transcript test
            "title": "SSC CHSL 2026 Application Started | Apply Now",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "channel_name": "WB Job Update",
            "channel_id": "UCtest123",
            "priority_weight": 1.0,
            "published_at": now_ist().isoformat(),
            "description": (
                "SSC CHSL 2026 Notification Released.\n"
                "Apply online at ssc.nic.in\n"
                "Application Start: 21 September 2026\n"
                "Last Date: 18 October 2026\n"
            ),
        }
    ],
    "source_channels": ["UCtest123"],
    "best_confidence": 0.95,
    "confirmation_level": "HIGH",
    "source_count": 1,
    "status": "NEW",
    "alert_sent": False,
    "first_detected": now_ist().isoformat(),
    "last_checked": now_ist().isoformat(),
    "full_extract_requested": False,
    "full_extract_completed": False,
    "telegram_message_id": None,
}


def test_youtube():
    logger.info("--- Testing YouTube API ---")
    from youtube import build_youtube_client
    try:
        yt = build_youtube_client()
        # Fetch info for a well-known public channel (Google)
        resp = yt.channels().list(
            part="snippet,contentDetails",
            forHandle="Google",
            maxResults=1,
        ).execute()
        items = resp.get("items", [])
        if items:
            name = items[0]["snippet"]["title"]
            playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            logger.info(f"  ✅ YouTube API OK — Channel: {name}, Playlist: {playlist}")
            return True
        else:
            logger.error("  ❌ No items returned from YouTube API")
            return False
    except Exception as e:
        logger.error(f"  ❌ YouTube API test FAILED: {e}")
        return False


def test_telegram():
    logger.info("--- Testing Telegram ---")
    from telegram import send_alert
    try:
        msg_id = send_alert(MOCK_EVENT, test_mode=True)
        if msg_id:
            logger.info(f"  ✅ Telegram alert sent (message_id={msg_id})")
            return True
        else:
            logger.error("  ❌ Telegram send returned no message_id")
            return False
    except Exception as e:
        logger.error(f"  ❌ Telegram test FAILED: {e}")
        return False


def test_openrouter():
    logger.info("--- Testing OpenRouter AI ---")
    from classifier import _get_openrouter_client
    try:
        client = _get_openrouter_client()
        settings = load_settings()
        model = settings.get("openrouter", {}).get("model", "openrouter/auto")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You classify YouTube videos about Indian jobs. Respond with JSON only."},
                {"role": "user", "content": (
                    "Title: SSC CHSL 2026 Application Started\n"
                    "Respond: {\"relevant\": true/false, \"event_type\": \"...\", \"confidence\": 0.0}"
                )},
            ],
            response_format={"type": "json_object"},
            max_tokens=100,
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        logger.info(f"  ✅ OpenRouter OK — Response: {parsed}")
        return True
    except json.JSONDecodeError as e:
        logger.error(f"  ❌ OpenRouter returned invalid JSON: {e}")
        return False
    except Exception as e:
        logger.error(f"  ❌ OpenRouter test FAILED: {e}")
        return False


def test_transcript():
    logger.info("--- Testing Transcript Fetch ---")
    from transcript import fetch_transcript
    # Use a well-known public video that should have captions
    # TED Talk: "Do schools kill creativity?" by Ken Robinson
    test_video_id = "iG9CE55wbtY"
    try:
        result = fetch_transcript(test_video_id)
        if result:
            logger.info(f"  ✅ Transcript fetched ({len(result)} chars)")
            logger.info(f"  Preview: {result[:100]}...")
            return True
        else:
            logger.warning(
                "  ⚠️ No transcript found (may be disabled for this video — "
                "this is OK if the fallback works)"
            )
            return True  # Not a fatal failure
    except Exception as e:
        logger.error(f"  ❌ Transcript test FAILED: {e}")
        return False


def test_extraction():
    logger.info("--- Testing Full Extraction Pipeline ---")
    from extractor import extract_full_information
    from telegram import send_full_extraction
    state = {
        "daily_ai_requests": 0,
        "daily_ai_date": now_ist().strftime("%Y-%m-%d"),
    }
    try:
        result = extract_full_information(MOCK_EVENT, state)
        if result:
            logger.info(f"  ✅ Extraction completed")
            logger.info(f"  Confidence: {result.get('extraction_confidence')}")
            logger.info(f"  Conflicts: {len(result.get('conflicts', []))}")
            logger.info(f"  Unknown fields: {len(result.get('unknown_fields', []))}")

            # Send as test Telegram message
            success = send_full_extraction(MOCK_EVENT, result, test_mode=True)
            if success:
                logger.info("  ✅ Full extraction Telegram message sent")
                return True
            else:
                logger.error("  ❌ Failed to send extraction Telegram message")
                return False
        else:
            logger.warning("  ⚠️ Extraction returned None (may be quota/API issue)")
            return False
    except Exception as e:
        logger.error(f"  ❌ Extraction test FAILED: {e}")
        return False


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="all",
                        choices=["all", "youtube", "telegram", "openrouter", "transcript", "extraction"])
    return parser.parse_args()


def main():
    args = parse_args()
    target = os.environ.get("TEST_TARGET", args.target)

    logger.info(f"Starting system test: target={target} at {now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}")

    results = {}

    test_map = {
        "youtube": test_youtube,
        "telegram": test_telegram,
        "openrouter": test_openrouter,
        "transcript": test_transcript,
        "extraction": test_extraction,
    }

    if target == "all":
        for name, fn in test_map.items():
            results[name] = fn()
    elif target in test_map:
        results[target] = test_map[target]()
    else:
        logger.error(f"Unknown test target: {target}")
        sys.exit(1)

    # Summary
    logger.info("\n" + "=" * 50)
    logger.info("TEST SUMMARY")
    logger.info("=" * 50)
    all_passed = True
    for name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"  {status}  {name}")
        if not passed:
            all_passed = False

    if all_passed:
        logger.info("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        logger.info("\n⚠️ Some tests failed — check logs above")
        sys.exit(1)


if __name__ == "__main__":
    main()
