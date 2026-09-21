"""
utils.py — Shared utilities for WB YouTube Job Alert Agent
"""

import json
import os
import re
import fcntl
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytz

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"

SETTINGS_PATH = CONFIG_DIR / "settings.json"
KEYWORDS_PATH = CONFIG_DIR / "keywords.json"
STATE_PATH = DATA_DIR / "state.json"
EVENTS_PATH = DATA_DIR / "events.json"
TELEGRAM_OFFSET_PATH = DATA_DIR / "telegram_offset.json"
CHANNELS_CSV = ROOT_DIR / "channels.csv"

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """Return a pre-configured logger."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)

logger = get_logger("utils")

# ---------------------------------------------------------------------------
# JSON I/O with file locking (safe for concurrent workflow runs)
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    """Load a JSON file. Returns None if file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    """Save data as JSON with atomic write (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_settings() -> dict:
    """Load config/settings.json."""
    data = load_json(SETTINGS_PATH)
    if data is None:
        raise FileNotFoundError(f"settings.json not found at {SETTINGS_PATH}")
    return data


def load_keywords() -> dict:
    """Load config/keywords.json."""
    data = load_json(KEYWORDS_PATH)
    if data is None:
        raise FileNotFoundError(f"keywords.json not found at {KEYWORDS_PATH}")
    return data


def load_state() -> dict:
    """Load data/state.json with defaults."""
    default = {
        "last_run": None,
        "bootstrap_complete": False,
        "processed_video_ids": [],
        "daily_quota_used": 0,
        "daily_quota_date": None,
        "daily_ai_requests": 0,
        "daily_ai_date": None,
    }
    data = load_json(STATE_PATH)
    if data is None:
        return default
    return {**default, **data}


def save_state(state: dict) -> None:
    save_json(STATE_PATH, state)


def load_events() -> list:
    """Load data/events.json — returns list of event dicts."""
    data = load_json(EVENTS_PATH)
    if data is None or "events" not in data:
        return []
    return data["events"]


def save_events(events: list) -> None:
    save_json(EVENTS_PATH, {"events": events})


def load_telegram_offset() -> int:
    data = load_json(TELEGRAM_OFFSET_PATH)
    if data is None:
        return 0
    return data.get("offset", 0)


def save_telegram_offset(offset: int) -> None:
    save_json(TELEGRAM_OFFSET_PATH, {"offset": offset})

# ---------------------------------------------------------------------------
# Exam name normalization
# ---------------------------------------------------------------------------

def normalize_exam_name(raw_name: str, normalizations: dict) -> str:
    """
    Map a raw exam name to its canonical form using the keywords.json
    exam_name_normalizations map. Falls back to title-cased raw_name.
    """
    if not raw_name:
        return "Unknown Exam"
    lower = raw_name.lower().strip()
    for canonical, variants in normalizations.items():
        if canonical.startswith("_"):
            continue
        for variant in variants:
            if isinstance(variant, str) and variant.lower() in lower:
                return canonical
    # Fallback: clean up and title-case
    return re.sub(r"\s+", " ", raw_name.strip()).title()


def make_event_id(exam_name: str, event_type: str) -> str:
    """Create a stable slug-style event ID."""
    slug = re.sub(r"[^a-z0-9]+", "-", (exam_name + "-" + event_type).lower()).strip("-")
    return slug

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def now_ist() -> datetime:
    """Current time in IST."""
    return datetime.now(IST)


def format_date_display(date_str: str) -> str:
    """
    Try to parse a date string and return it in a friendly format.
    Returns the original string if parsing fails.
    """
    if not date_str or date_str.strip().lower() in ("not announced", "not found", "tba", ""):
        return date_str
    try:
        from dateutil import parser as dateparser
        dt = dateparser.parse(date_str, dayfirst=True)
        if dt:
            return dt.strftime("%-d %B %Y")  # e.g. "21 September 2026"
    except Exception:
        pass
    return date_str


def is_valid_date(date_str: str) -> bool:
    """Return True if date_str looks like a real date."""
    if not date_str or date_str.strip().lower() in (
        "not announced", "not found", "tba", "n/a", "", "not available"
    ):
        return True  # "not found" is a valid explicit answer
    try:
        from dateutil import parser as dateparser
        dateparser.parse(date_str, dayfirst=True)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Emoji helpers
# ---------------------------------------------------------------------------

EVENT_EMOJIS = {
    "APPLICATION_OPEN": "🟢",
    "APPLICATION_OPEN_SOON": "🟡",
    "ADMIT_CARD_RELEASED": "🔵",
    "RESULT_DECLARED": "🏆",
}

CONFIDENCE_EMOJIS = {
    "HIGH": "🟢",
    "MEDIUM": "🟡",
    "LOW": "🔴",
}


def event_emoji(event_type: str) -> str:
    return EVENT_EMOJIS.get(event_type, "🔔")


def confidence_emoji(level: str) -> str:
    return CONFIDENCE_EMOJIS.get(level.upper(), "⚪")


def event_type_label(event_type: str) -> str:
    labels = {
        "APPLICATION_OPEN": "Application Started",
        "APPLICATION_OPEN_SOON": "Application Starting Soon",
        "ADMIT_CARD_RELEASED": "Admit Card Released",
        "RESULT_DECLARED": "Result Declared",
    }
    return labels.get(event_type, event_type.replace("_", " ").title())

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from a text block."""
    pattern = r'https?://[^\s\)\]\>\"\']+' 
    return re.findall(pattern, text or "")


def is_pdf_url(url: str) -> bool:
    return url.lower().endswith(".pdf") or "pdf" in url.lower()

# ---------------------------------------------------------------------------
# Quota tracking
# ---------------------------------------------------------------------------

def check_and_increment_quota(state: dict, amount: int = 1) -> bool:
    """
    Increment YouTube API quota counter. Returns False if budget exceeded.
    Resets counter daily.
    """
    settings = load_settings()
    budget = settings.get("youtube", {}).get("daily_quota_budget", 8000)
    today = now_ist().strftime("%Y-%m-%d")

    if state.get("daily_quota_date") != today:
        state["daily_quota_used"] = 0
        state["daily_quota_date"] = today

    if state["daily_quota_used"] + amount > budget:
        logger.warning(
            f"YouTube quota budget reached: {state['daily_quota_used']}/{budget}"
        )
        return False

    state["daily_quota_used"] += amount
    return True


def check_and_increment_ai(state: dict) -> bool:
    """
    Increment OpenRouter AI request counter. Returns False if budget exceeded.
    Resets counter daily.
    """
    settings = load_settings()
    budget = settings.get("openrouter", {}).get("daily_request_budget", 45)
    today = now_ist().strftime("%Y-%m-%d")

    if state.get("daily_ai_date") != today:
        state["daily_ai_requests"] = 0
        state["daily_ai_date"] = today

    if state["daily_ai_requests"] >= budget:
        logger.warning(
            f"AI request budget reached: {state['daily_ai_requests']}/{budget}"
        )
        return False

    state["daily_ai_requests"] += 1
    return True

# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_transcript(raw: str) -> str:
    """Remove repeated filler lines and excessive whitespace from transcripts."""
    if not raw:
        return ""
    lines = raw.splitlines()
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Remove timestamp markers like [00:01] or (00:01)
        stripped = re.sub(r"[\[\(]\d{1,2}:\d{2}(:\d{2})?[\]\)]", "", stripped).strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            out.append(stripped)
    return " ".join(out)


def truncate(text: str, max_chars: int = 500) -> str:
    """Truncate text to max_chars, appending '...' if needed."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars].rsplit(" ", 1)[0] + "..."
