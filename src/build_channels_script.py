"""
Build and validate the final YouTube channel list.

Input:
    channels_raw.txt

Outputs:
    channels.csv
    channels_validation.csv

The builder NEVER silently removes a channel.

Every input channel receives a validation status.
"""

from __future__ import annotations

import csv
import os
import sys
from typing import Dict, List, Optional, Tuple

from googleapiclient.discovery import build

from youtube import (
    clean_text,
    extract_channel_id,
    extract_handle,
    normalize_name,
    resolve_channel,
)


ROOT_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

RAW_FILE = os.path.join(
    ROOT_DIR,
    "channels_raw.txt",
)

OUTPUT_FILE = os.path.join(
    ROOT_DIR,
    "channels.csv",
)

VALIDATION_FILE = os.path.join(
    ROOT_DIR,
    "channels_validation.csv",
)


def parse_raw_line(
    line: str,
) -> Optional[Dict[str, str]]:
    """
    Parse one channels_raw.txt line.

    Supported:

        URL
        @handle
        UCxxxxxxxxxxxxxxxxxxxxxxxx

    Optional:

        NAME | URL | CHANNEL_ID
    """

    line = line.strip()

    if not line:
        return None

    if line.startswith("#"):
        return None

    # ---------------------------------------------------------
    # Structured format:
    #
    # NAME | URL | CHANNEL_ID
    # ---------------------------------------------------------

    if "|" in line:
        parts = [
            clean_text(part)
            for part in line.split("|")
        ]

        while len(parts) < 3:
            parts.append("")

        name = parts[0]
        url_or_handle = parts[1]
        channel_id = parts[2]

        if not url_or_handle and channel_id:
            url_or_handle = channel_id

        return {
            "name": name,
            "source": url_or_handle,
            "channel_id": channel_id,
        }

    # ---------------------------------------------------------
    # Simple one-value format.
    # ---------------------------------------------------------

    return {
        "name": "",
        "source": line,
        "channel_id": (
            extract_channel_id(line) or ""
        ),
    }


def read_raw_channels() -> List[Dict[str, str]]:
    if not os.path.exists(RAW_FILE):
        raise FileNotFoundError(
            f"Missing file: {RAW_FILE}"
        )

    records = []

    with open(
        RAW_FILE,
        "r",
        encoding="utf-8",
    ) as f:
        for line_number, line in enumerate(
            f,
            start=1,
        ):
            parsed = parse_raw_line(line)

            if parsed is None:
                continue

            parsed["line_number"] = str(
                line_number
            )

            records.append(parsed)

    return records


def create_youtube_client():
    api_key = os.environ.get(
        "YOUTUBE_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "YOUTUBE_API_KEY environment variable is missing."
        )

    return build(
        "youtube",
        "v3",
        developerKey=api_key,
        cache_discovery=False,
    )


def validate_one(
    youtube,
    record: Dict[str, str],
) -> Tuple[Dict[str, str], Optional[Dict[str, str]]]:
    """
    Return:

        validation_row
        clean_channel_row
    """

    source = record["source"]
    supplied_name = record["name"]
    supplied_id = record["channel_id"]

    handle = extract_handle(source)

    validation = {
        "input_line": record["line_number"],
        "input_name": supplied_name,
        "input_source": source,
        "input_channel_id": supplied_id,
        "input_handle": handle or "",
        "status": "UNRESOLVED",
        "resolution_method": "",
        "verified_name": "",
        "verified_channel_id": "",
        "uploads_playlist": "",
        "custom_url": "",
        "reason": "",
    }

    try:
        channel = resolve_channel(
            youtube=youtube,
            raw_entry=source,
            supplied_channel_id=supplied_id,
            supplied_name=supplied_name,
            allow_search_fallback=True,
        )

    except Exception as exc:
        validation["status"] = "ERROR"
        validation["reason"] = (
            f"{type(exc).__name__}: {exc}"
        )

        return validation, None

    if not channel:
        validation["reason"] = (
            "Could not resolve and validate channel"
        )

        return validation, None

    verified_id = channel.get(
        "channel_id",
        "",
    )

    verified_name = channel.get(
        "name",
        "",
    )

    uploads_playlist = channel.get(
        "uploads_playlist",
        "",
    )

    custom_url = channel.get(
        "custom_url",
        "",
    )

    method = channel.get(
        "resolution_method",
        "",
    )

    # ---------------------------------------------------------
    # Check supplied ID against verified ID.
    # ---------------------------------------------------------

    if supplied_id and supplied_id != verified_id:
        validation["status"] = "MISMATCH"
        validation["resolution_method"] = method
        validation["verified_name"] = verified_name
        validation["verified_channel_id"] = verified_id
        validation["uploads_playlist"] = uploads_playlist
        validation["custom_url"] = custom_url
        validation["reason"] = (
            "Supplied channel ID does not match "
            "the resolved channel"
        )

        return validation, None

    # ---------------------------------------------------------
    # Successful validation.
    # ---------------------------------------------------------

    validation["status"] = "RESOLVED"
    validation["resolution_method"] = method
    validation["verified_name"] = verified_name
    validation["verified_channel_id"] = verified_id
    validation["uploads_playlist"] = uploads_playlist
    validation["custom_url"] = custom_url
    validation["reason"] = "Validated"

    clean_row = {
        "name": verified_name,
        "channel_id": verified_id,
        "handle": handle or custom_url,
        "uploads_playlist": uploads_playlist,
        "source": source,
        "resolution_method": method,
    }

    return validation, clean_row


def write_validation_csv(
    rows: List[Dict[str, str]],
):
    fieldnames = [
        "input_line",
        "input_name",
        "input_source",
        "input_channel_id",
        "input_handle",
        "status",
        "resolution_method",
        "verified_name",
        "verified_channel_id",
        "uploads_playlist",
        "custom_url",
        "reason",
    ]

    with open(
        VALIDATION_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(row)


def write_channels_csv(
    rows: List[Dict[str, str]],
):
    fieldnames = [
        "name",
        "channel_id",
        "handle",
        "uploads_playlist",
        "source",
        "resolution_method",
    ]

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(row)


def print_summary(
    total: int,
    resolved: int,
    unresolved: int,
    mismatches: int,
    errors: int,
):
    print()
    print("=" * 70)
    print("CHANNEL VALIDATION SUMMARY")
    print("=" * 70)

    print(f"Total input channels : {total}")
    print(f"Resolved             : {resolved}")
    print(f"Unresolved           : {unresolved}")
    print(f"ID mismatches        : {mismatches}")
    print(f"Errors               : {errors}")

    print()

    if unresolved == 0 and mismatches == 0 and errors == 0:
        print(
            "STATUS: READY FOR BOOTSTRAP"
        )
        print(
            "All input channels were successfully validated."
        )
    else:
        print(
            "STATUS: DO NOT BOOTSTRAP YET"
        )
        print(
            "Review channels_validation.csv first."
        )

    print()
    print(
        f"Clean list: {OUTPUT_FILE}"
    )

    print(
        f"Validation report: {VALIDATION_FILE}"
    )

    print("=" * 70)
    print()


def main():
    print()
    print("=" * 70)
    print("WB YOUTUBE JOB ALERT AGENT")
    print("CHANNEL VALIDATION / BUILD")
    print("=" * 70)
    print()

    records = read_raw_channels()

    if not records:
        raise RuntimeError(
            "No channel entries found in channels_raw.txt"
        )

    print(
        f"Loaded {len(records)} channel entries."
    )
    print()

    youtube = create_youtube_client()

    validation_rows = []
    clean_rows = []

    resolved = 0
    unresolved = 0
    mismatches = 0
    errors = 0

    for index, record in enumerate(
        records,
        start=1,
    ):

        display_name = (
            record["name"]
            or extract_handle(record["source"])
            or record["source"]
        )

        print(
            f"[{index}/{len(records)}] "
            f"Validating: {display_name}"
        )

        validation, clean = validate_one(
            youtube,
            record,
        )

        validation_rows.append(
            validation
        )

        status = validation["status"]

        if status == "RESOLVED":
            resolved += 1

            clean_rows.append(clean)

            print(
                f"  ✓ RESOLVED | "
                f"{validation['verified_name']} | "
                f"{validation['verified_channel_id']} | "
                f"{validation['resolution_method']}"
            )

        elif status == "MISMATCH":
            mismatches += 1

            print(
                f"  ! MISMATCH | "
                f"{validation['reason']}"
            )

        elif status == "ERROR":
            errors += 1

            print(
                f"  ! ERROR | "
                f"{validation['reason']}"
            )

        else:
            unresolved += 1

            print(
                f"  ✗ UNRESOLVED | "
                f"{validation['reason']}"
            )

    # Remove duplicate channel IDs.
    unique_rows = []
    seen_ids = set()

    for row in clean_rows:
        channel_id = row["channel_id"]

        if not channel_id:
            continue

        if channel_id in seen_ids:
            continue

        seen_ids.add(channel_id)
        unique_rows.append(row)

    if len(unique_rows) != len(clean_rows):
        print()
        print(
            f"Removed "
            f"{len(clean_rows) - len(unique_rows)} "
            f"duplicate resolved channel(s)."
        )

    write_validation_csv(
        validation_rows
    )

    write_channels_csv(
        unique_rows
    )

    print_summary(
        total=len(records),
        resolved=resolved,
        unresolved=unresolved,
        mismatches=mismatches,
        errors=errors,
    )

    # ---------------------------------------------------------
    # IMPORTANT:
    #
    # We deliberately DO NOT fail the GitHub Action.
    #
    # The validation report must be generated even if some
    # channels are unresolved.
    #
    # Once the list is clean, Bootstrap can be enabled.
    # ---------------------------------------------------------


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)

    except Exception as exc:
        print()
        print(
            f"FATAL ERROR: {type(exc).__name__}: {exc}"
        )
        sys.exit(1)
