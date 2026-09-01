#!/usr/bin/env python3
"""
Backfill setlist metadata for existing GD comment data.

For each show ID in a comments JSON file, fetches the IA metadata
and extracts the setlist from the description field (HTML-cleaned
using BeautifulSoup, matching the reference repo approach).

Usage:
    python3 backfill_setlists.py data/gd_comments_20260830-233931.json
    python3 backfill_setlists.py data/gd_comments_20260831-005050.json
"""

import json
import sys
import time
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://archive.org"
DELAY = 2.0  # IA-friendly rate limit


def clean_html(text: str) -> str:
    """Clean HTML from text using BeautifulSoup (matches reference repo)."""
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        cleaned = soup.get_text(separator=" ", strip=True)
        cleaned = " ".join(cleaned.split())
        return cleaned
    except Exception:
        return str(text) if text else ""


def parse_setlist(description: str) -> list[str]:
    """
    Parse setlist from IA description field.
    After HTML cleaning, descriptions contain comma-separated song names
    with -> for transitions (e.g., "Bertha, Dark Star->Space, ...").
    """
    desc = clean_html(description).strip()
    if not desc:
        return []

    # Split by comma into song entries
    raw_songs = desc.split(",")
    songs = []
    for raw in raw_songs:
        raw = raw.strip()
        if not raw:
            continue
        # Handle transitions: "Song A->Song B->Song C"
        sub_songs = raw.split("->")
        for s in sub_songs:
            s = s.strip()
            if s:
                songs.append(s)
    return songs


def fetch_setlist(identifier: str) -> list[str]:
    """Fetch setlist for a single show ID from IA metadata."""
    url = f"{BASE_URL}/metadata/{identifier}"
    headers = {"User-Agent": "GD-RAT-Backfill/1.0"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    desc = data.get("metadata", {}).get("description", "")
    return parse_setlist(desc)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 backfill_setlists.py <data_json> [<data_json2> ...]")
        sys.exit(1)

    for data_path in sys.argv[1:]:
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        show_ids = data.get("shows_processed", [])
        if not show_ids:
            print(f"No shows_processed in {data_path}")
            continue

        # Merge with any existing setlists
        existing_setlists = data.get("setlists", {})

        setlists = {}
        for i, show_id in enumerate(show_ids, 1):
            # Skip if already fetched
            if show_id in existing_setlists:
                setlists[show_id] = existing_setlists[show_id]
                continue
            try:
                setlist = fetch_setlist(show_id)
                if setlist:
                    setlists[show_id] = setlist
                    print(f"  [{i}/{len(show_ids)}] {show_id}: {len(setlist)} songs — {setlist[0]}...")
                else:
                    print(f"  [{i}/{len(show_ids)}] {show_id}: no setlist found")
                time.sleep(DELAY)
            except Exception as e:
                print(f"  [{i}/{len(show_ids)}] {show_id}: ERROR - {e}")
                time.sleep(DELAY)

        # Merge into existing data
        data["setlists"] = setlists

        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"\nDone! Setlists for {len(setlists)}/{len(show_ids)} shows merged into {data_path}")


if __name__ == "__main__":
    main()
