"""
telegram.py — Telegram Bot API wrapper.
Handles sending simple alerts, inline keyboard buttons, polling for callbacks,
and sending full extraction results.
"""

import os
import json
import logging
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from utils import (
    load_settings, event_emoji, event_type_label,
    confidence_emoji, load_telegram_offset, save_telegram_offset,
    get_logger
)

logger = get_logger("telegram")

TELEGRAM_BASE = "https://api.telegram.org/bot{token}"
MAX_MSG_LEN = 4096  # Telegram's message length limit


def _get_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")
    return token


def _get_chat_id(settings: dict) -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or settings.get("telegram", {}).get("admin_chat_id", "")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID not set in environment or settings.")
    return chat_id


def _base_url() -> str:
    return TELEGRAM_BASE.format(token=_get_token())


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _post(endpoint: str, payload: dict) -> dict:
    """Make a Telegram Bot API POST request."""
    url = f"{_base_url()}/{endpoint}"
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description')}")
    return data


def _get(endpoint: str, params: dict = None) -> dict:
    """Make a Telegram Bot API GET request."""
    url = f"{_base_url()}/{endpoint}"
    response = requests.get(url, params=params or {}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description')}")
    return data


# ---------------------------------------------------------------------------
# Simple alert (Phase 1 — lightweight)
# ---------------------------------------------------------------------------

def send_alert(event: dict, test_mode: bool = False) -> Optional[int]:
    """
    Send the simple first-alert Telegram message with inline keyboard.
    Returns the Telegram message_id if successful, else None.
    """
    settings = load_settings()
    chat_id = _get_chat_id(settings)

    emoji = event_emoji(event["event_type"])
    label = event_type_label(event["event_type"])
    exam = event["exam_name"]
    conf_emoji = confidence_emoji(event.get("confirmation_level", "MEDIUM"))

    # Best source (highest priority channel)
    sources = event.get("source_videos", [])
    best_source = sources[0] if sources else {}
    source_label = best_source.get("channel_name", "Unknown Channel")
    source_count = len(event.get("source_channels", []))

    prefix = "🧪 *TEST ALERT*\n\n" if test_mode else ""

    # Date hints from source videos titles (opportunistic)
    text = (
        f"{prefix}"
        f"{emoji} *{exam}* — {label}\n\n"
        f"{conf_emoji} Confidence: {event.get('confirmation_level', 'MEDIUM')}"
        f" ({source_count} source{'s' if source_count != 1 else ''})\n\n"
        f"🎥 Source: {source_label}\n"
    )

    if source_count > 1:
        other_channels = [
            v.get("channel_name", "") for v in sources[1:3]
        ]
        text += f"Also reported by: {', '.join(other_channels)}\n"

    text += f"\n📎 [{best_source.get('title', 'Watch video')}]({best_source.get('url', '')})\n"
    text += f"\n✅ Need complete A–Z application information?"

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ EXTRACT FULL INFORMATION",
                    "callback_data": f"extract:{event['event_id']}",
                },
                {
                    "text": "❌ SKIP",
                    "callback_data": f"skip:{event['event_id']}",
                },
            ]
        ]
    }

    try:
        result = _post("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
            "disable_web_page_preview": False,
        })
        msg_id = result["result"]["message_id"]
        logger.info(f"Alert sent for {event['event_id']} (message_id={msg_id})")
        return msg_id
    except Exception as e:
        logger.error(f"Failed to send alert for {event['event_id']}: {e}")
        return None


# ---------------------------------------------------------------------------
# Full extraction result message
# ---------------------------------------------------------------------------

def send_full_extraction(event: dict, extraction: dict, test_mode: bool = False) -> bool:
    """
    Send the complete A–Z information message after deep extraction.
    Splits into multiple messages if over Telegram's 4096-char limit.
    """
    settings = load_settings()
    chat_id = _get_chat_id(settings)
    exam = event["exam_name"]
    emoji = event_emoji(event["event_type"])
    label = event_type_label(event["event_type"])
    prefix = "🧪 *TEST — " if test_mode else ""

    def _field(label: str, value) -> str:
        if not value or str(value).strip().lower() in ("", "none", "null", "not found", "n/a"):
            return f"• {label}: _Not found in available source_\n"
        return f"• {label}: {value}\n"

    def _list_field(label: str, items: list) -> str:
        if not items:
            return f"• {label}: _Not found in available source_\n"
        return f"• {label}:\n" + "".join(f"  — {item}\n" for item in items)

    # Build message in sections
    sections = []

    # Header
    sections.append(
        f"{prefix}{emoji} *{exam}*\n"
        f"_{label}_\n"
    )

    # Dates
    if extraction.get("dates"):
        dates = extraction["dates"]
        sec = "📅 *Important Dates*\n"
        sec += _field("Notification Date", dates.get("notification_date"))
        sec += _field("Application Start", dates.get("application_start"))
        sec += _field("Last Date to Apply", dates.get("application_last_date"))
        sec += _field("Fee Payment Last Date", dates.get("fee_last_date"))
        sec += _field("Correction Window", dates.get("correction_window"))
        sec += _field("Exam Date", dates.get("exam_date"))
        sec += _field("Admit Card Date", dates.get("admit_card_date"))
        sec += _field("Result Date", dates.get("result_date"))
        if dates.get("other_dates"):
            for od in dates["other_dates"]:
                sec += _field(od.get("label", "Other"), od.get("value"))
        sections.append(sec)

    # Vacancy & Post
    if extraction.get("post_info"):
        pi = extraction["post_info"]
        sec = "📋 *Post & Vacancy*\n"
        sec += _field("Organization", pi.get("organization"))
        sec += _field("Post Name", pi.get("post_name"))
        sec += _field("Total Vacancy", pi.get("vacancy"))
        sec += _field("Notification Number", pi.get("notification_number"))
        sec += _field("Application Mode", pi.get("application_mode"))
        sections.append(sec)

    # Eligibility
    if extraction.get("eligibility"):
        el = extraction["eligibility"]
        sec = "🎓 *Eligibility*\n"
        sec += _field("Qualification", el.get("qualification"))
        sec += _field("Required Subjects", el.get("subjects"))
        sec += _field("Percentage", el.get("percentage"))
        sec += _field("Age Limit", el.get("age_limit"))
        sec += _field("Age Relaxation", el.get("age_relaxation"))
        sec += _field("Nationality", el.get("nationality"))
        if el.get("other_conditions"):
            for cond in el["other_conditions"]:
                sec += f"  — {cond}\n"
        sections.append(sec)

    # Fees
    if extraction.get("fees"):
        fees = extraction["fees"]
        sec = "💰 *Application Fee*\n"
        fee_cats = [
            ("General", fees.get("general")),
            ("OBC", fees.get("obc")),
            ("EWS", fees.get("ews")),
            ("SC", fees.get("sc")),
            ("ST", fees.get("st")),
            ("Female", fees.get("female")),
            ("PwBD", fees.get("pwbd")),
            ("Ex-Serviceman", fees.get("ex_serviceman")),
        ]
        for cat, val in fee_cats:
            if val is not None:
                sec += _field(cat, val)
        if fees.get("processing_fee"):
            sec += _field("Processing Fee", fees["processing_fee"])
        if fees.get("total"):
            sec += _field("Total Payable", fees["total"])
        sec += _field("Payment Mode", fees.get("payment_mode"))
        sections.append(sec)

    # Documents
    if extraction.get("documents"):
        sec = "📄 *Documents Required*\n"
        for doc in extraction["documents"]:
            sec += f"  — {doc}\n"
        sections.append(sec)

    # Photo requirements
    if extraction.get("photo"):
        ph = extraction["photo"]
        sec = "🖼️ *Photo Upload*\n"
        sec += _field("Format", ph.get("format"))
        sec += _field("File Size", ph.get("size"))
        sec += _field("Dimensions", ph.get("dimensions"))
        sec += _field("Resolution/DPI", ph.get("resolution"))
        sec += _field("Background", ph.get("background"))
        if ph.get("other"):
            sec += _field("Other Instructions", ph["other"])
        sections.append(sec)

    # Signature requirements
    if extraction.get("signature"):
        sig = extraction["signature"]
        sec = "✍️ *Signature Upload*\n"
        sec += _field("Format", sig.get("format"))
        sec += _field("File Size", sig.get("size"))
        sec += _field("Dimensions", sig.get("dimensions"))
        if sig.get("other"):
            sec += _field("Other Instructions", sig["other"])
        sections.append(sec)

    # Application steps
    if extraction.get("application_steps"):
        sec = "📝 *Application Steps*\n"
        for i, step in enumerate(extraction["application_steps"], 1):
            sec += f"{i}. {step}\n"
        sections.append(sec)

    # Important instructions
    if extraction.get("important_instructions"):
        sec = "⚠️ *Important Instructions*\n"
        for inst in extraction["important_instructions"]:
            sec += f"  — {inst}\n"
        sections.append(sec)

    # Official links
    links_sec = "🌐 *Official Links*\n"
    if extraction.get("official_website"):
        links_sec += f"Website: {extraction['official_website']}\n"
    if extraction.get("official_notification"):
        links_sec += f"Notification: {extraction['official_notification']}\n"
    sections.append(links_sec)

    # Conflicts/warnings
    if extraction.get("conflicts"):
        sec = "⚠️ *Verification Required*\n"
        for conflict in extraction["conflicts"]:
            sec += f"  ⚠️ {conflict}\n"
        sections.append(sec)

    # Unknown/missing fields
    if extraction.get("unknown_fields"):
        sec = "❓ *Not Found in Sources*\n"
        for uf in extraction["unknown_fields"]:
            sec += f"  — {uf}: Not found in available source\n"
        sections.append(sec)

    # Footer
    sources = event.get("source_videos", [])
    source_lines = "\n".join(
        f"  [{v.get('channel_name', '')}]({v.get('url', '')})"
        for v in sources[:3]
    )
    official_found = bool(extraction.get("official_website") or extraction.get("official_notification"))
    confidence = extraction.get("extraction_confidence", "Unknown")

    sections.append(
        f"🎥 *Sources*\n{source_lines}\n\n"
        f"_Information confidence: {confidence}_\n"
        f"_Official document found: {'Yes ✅' if official_found else 'No ❌'}_"
    )

    # Send in chunks respecting Telegram's 4096-char limit
    full_text = "\n\n".join(sections)
    chunks = _split_message(full_text)

    success = True
    for i, chunk in enumerate(chunks):
        try:
            _post("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
        except Exception as e:
            logger.error(f"Failed to send extraction chunk {i+1}: {e}")
            success = False

    return success


def _split_message(text: str) -> list[str]:
    """Split a long message into Telegram-safe chunks."""
    if len(text) <= MAX_MSG_LEN:
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > MAX_MSG_LEN:
            if current:
                chunks.append(current.strip())
            current = paragraph
        else:
            current += ("\n\n" if current else "") + paragraph
    if current:
        chunks.append(current.strip())
    return chunks


# ---------------------------------------------------------------------------
# Telegram update polling (for button callbacks)
# ---------------------------------------------------------------------------

def poll_updates() -> list[dict]:
    """
    Poll Telegram getUpdates using the stored offset.
    Returns a list of callback_query updates.
    """
    offset = load_telegram_offset()
    try:
        result = _get("getUpdates", {
            "offset": offset,
            "timeout": 5,
            "allowed_updates": ["callback_query"],
        })
        updates = result.get("result", [])
        if updates:
            new_offset = updates[-1]["update_id"] + 1
            save_telegram_offset(new_offset)
            logger.info(f"Received {len(updates)} update(s), new offset: {new_offset}")
        return updates
    except Exception as e:
        logger.error(f"Error polling Telegram updates: {e}")
        return []


def answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a Telegram callback query (removes the loading spinner)."""
    try:
        _post("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text,
        })
    except Exception as e:
        logger.warning(f"Could not answer callback {callback_id}: {e}")


def send_processing_message(event_id: str) -> None:
    """Send a 'Processing...' message when EXTRACT is pressed."""
    settings = load_settings()
    chat_id = _get_chat_id(settings)
    try:
        _post("sendMessage", {
            "chat_id": chat_id,
            "text": f"⏳ Extracting full information for `{event_id}`\\.\\.\\.\nThis may take 30–60 seconds\\.",
            "parse_mode": "MarkdownV2",
        })
    except Exception as e:
        logger.warning(f"Could not send processing message: {e}")


def get_chat_ids_from_updates() -> list[dict]:
    """
    Helper for get_telegram_chat_id workflow.
    Returns a list of {chat_id, chat_title, message_text} from recent updates.
    """
    try:
        result = _get("getUpdates", {"allowed_updates": ["message", "callback_query"]})
        updates = result.get("result", [])
        seen = set()
        chats = []
        for u in updates:
            msg = u.get("message") or u.get("callback_query", {}).get("message", {})
            if not msg:
                continue
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                chats.append({
                    "chat_id": chat_id,
                    "chat_type": chat.get("type"),
                    "chat_title": chat.get("title") or chat.get("first_name", ""),
                    "sample_message": msg.get("text", "")[:50],
                })
        return chats
    except Exception as e:
        logger.error(f"Error getting chat IDs: {e}")
        return []
