"""
official_docs.py — Fetch and extract text from official government documents/PDFs
linked in YouTube video descriptions. Official PDFs receive higher factual priority
than YouTube explanations.
"""

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import fitz  # PyMuPDF
import requests
from requests.exceptions import RequestException

from utils import extract_urls, is_pdf_url, load_keywords, get_logger

logger = get_logger("official_docs")

REQUEST_TIMEOUT = 15  # seconds
MAX_PDF_PAGES = 20    # don't extract more than this many pages
MAX_TEXT_CHARS = 50_000  # cap text size sent to AI


@dataclass
class OfficialDoc:
    url: str
    doc_type: str        # "pdf", "html", "unknown"
    domain: str
    text: str
    is_official: bool    # True if domain is in official_domains list
    priority: int        # higher = more trusted (pdf > html; official > unofficial)
    error: Optional[str] = None


def fetch_official_docs(video: dict) -> list[OfficialDoc]:
    """
    Parse a video's description for URLs, fetch official government documents,
    and return extracted text.

    Priority:
      - Official domain PDF    → priority 10
      - Official domain HTML   → priority 7
      - Unofficial PDF         → priority 4
      - Unofficial HTML        → priority 2
    """
    description = video.get("description", "")
    urls = extract_urls(description)

    if not urls:
        logger.info(f"No URLs found in description for video {video.get('video_id')}")
        return []

    keywords = load_keywords()
    official_domains = set(keywords.get("official_domains", []))
    # Strip the _comment entry
    official_domains.discard("_comment")

    docs = []
    for url in urls[:10]:  # cap at 10 URLs per video
        doc = _fetch_url(url, official_domains)
        if doc:
            docs.append(doc)

    # Sort by priority descending
    docs.sort(key=lambda d: d.priority, reverse=True)
    logger.info(
        f"Found {len(docs)} document(s) for {video.get('video_id')}: "
        + ", ".join(f"{d.doc_type}@{d.domain}" for d in docs)
    )
    return docs


def _fetch_url(url: str, official_domains: set) -> Optional[OfficialDoc]:
    """Fetch a single URL and return an OfficialDoc, or None on failure."""
    parsed = urlparse(url)
    domain = parsed.netloc.lstrip("www.")
    is_official = any(domain.endswith(od) for od in official_domains)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; WBJobAlertBot/1.0; "
            "+https://github.com/hasan-online/wb-youtube-job-alert-agent)"
        )
    }

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()

        if "pdf" in content_type or is_pdf_url(url):
            text = _extract_pdf_text(response.content)
            doc_type = "pdf"
            priority = 10 if is_official else 4
        elif "html" in content_type or "text" in content_type:
            text = _extract_html_text(response.text)
            doc_type = "html"
            priority = 7 if is_official else 2
        else:
            return None  # Binary or unknown format

        if not text or len(text.strip()) < 50:
            return None

        return OfficialDoc(
            url=url,
            doc_type=doc_type,
            domain=domain,
            text=text[:MAX_TEXT_CHARS],
            is_official=is_official,
            priority=priority,
        )

    except RequestException as e:
        logger.warning(f"Could not fetch {url}: {e}")
        return OfficialDoc(
            url=url,
            doc_type="unknown",
            domain=domain,
            text="",
            is_official=is_official,
            priority=0,
            error=str(e),
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching {url}: {e}")
        return None


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from a PDF binary using PyMuPDF."""
    try:
        doc = fitz.open(stream=io.BytesIO(content), filetype="pdf")
        pages = []
        for i, page in enumerate(doc):
            if i >= MAX_PDF_PAGES:
                break
            pages.append(page.get_text())
        doc.close()
        text = "\n".join(pages)
        # Clean up excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


def _extract_html_text(html: str) -> str:
    """
    Extract readable text from HTML — simple approach without BeautifulSoup
    to avoid an extra dependency.
    """
    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&rsquo;", "'")
        .replace("&ldquo;", '"')
        .replace("&rdquo;", '"')
    )
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_source_context(docs: list[OfficialDoc]) -> str:
    """
    Combine extracted official documents into a single labeled text block
    for sending to the AI extractor.
    """
    if not docs:
        return ""

    parts = []
    for i, doc in enumerate(docs, 1):
        label = (
            f"[OFFICIAL DOCUMENT {i}]" if doc.is_official
            else f"[LINKED DOCUMENT {i}]"
        )
        type_label = f"({doc.doc_type.upper()} from {doc.domain})"
        if doc.text:
            parts.append(f"{label} {type_label}\nURL: {doc.url}\n\n{doc.text}")

    return "\n\n" + ("=" * 60) + "\n\n".join(parts) if parts else ""
